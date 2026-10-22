"""Owned shutdown checks with fake processes/clocks and an offscreen Qt window."""
from __future__ import annotations

import ast
import io
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, mock_open, patch
from types import FunctionType
from typing import Callable, ClassVar, Literal, NoReturn, NotRequired, Protocol, TypedDict, cast, overload, runtime_checkable

import launcher as _launcher_module
from MedlarTV.app import medlar_app
from PySide6.QtCore import QEvent, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow

ROOT = Path(__file__).resolve().parents[1]


class _OwnedProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...
    def send_signal(self, value: int, /) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


class _OwnedEntry(TypedDict):
    proc: _OwnedProcess
    pid: int
    name: NotRequired[str]
    log: NotRequired[io.TextIOBase]
    console_group: NotRequired[bool]
    stop_file: NotRequired[Path | None]
    required: NotRequired[bool]


@runtime_checkable
class _LauncherFixture(Protocol):
    processes: list[_OwnedEntry]
    subprocess: object
    platform: object
    CORE_STOP_FILE_ENV: str

    def stop_all(self) -> bool: ...
    def start_process(self, name: str, command: list[str], wait: float = 2) -> bool: ...
    def _stop_owned_process(self, proc: _OwnedProcess) -> bool: ...


def _launcher_fixture(value: object) -> _LauncherFixture:
    # The real module is retained; only the synthetic process boundary is modeled.
    assert isinstance(value, _LauncherFixture)
    return value


launcher = _launcher_fixture(_launcher_module)


class _StopJob(TypedDict):
    proc: _OwnedProcess
    marker: Path | None
    deadline: float
    forced: bool
    done: threading.Event
    error: str | None
    request_error: NotRequired[str]


class _Label(Protocol):
    def setText(self, text: str, /) -> None: ...


@runtime_checkable
class _Application(Protocol):
    def processEvents(self) -> None: ...


class _CloseEvent(Protocol):
    def accept(self) -> None: ...
    def ignore(self) -> None: ...
    def isAccepted(self) -> bool: ...


class _Window(Protocol):
    status_label: _Label
    chat_status: _Label
    launcher_proc: _OwnedProcess | None
    ollama_proc: _OwnedProcess | None
    _ollama_timer: Mock
    _stop_timer: Mock
    _stop_jobs: dict[str, _StopJob]
    _stop_file: Path | None
    _ollama_stop_file: Path | None
    _ollama_ready: bool
    _ollama_starting: bool
    _ollama_error: str | None
    _shutdown_failure: str | None
    _restart_pending: bool
    _close_pending: bool
    _close_ready: bool
    _chat_closed: bool

    def on_stop(self) -> bool: ...
    def on_start(self) -> None: ...
    def on_restart(self) -> None: ...
    def closeEvent(self, event: _CloseEvent) -> None: ...
    def eventFilter(self, watched: object, event: object) -> bool: ...
    def refresh_status(self) -> None: ...
    def deleteLater(self) -> None: ...
    def _poll_stops(self) -> None: ...
    def _start_ollama(self) -> None: ...


@runtime_checkable
class _CallRecord(Protocol):
    args: tuple[object, ...]
    kwargs: dict[str, object]


def called(mock: Mock) -> _CallRecord:
    call = mock.call_args
    assert call is not None, 'expected mock invocation before inspecting arguments'
    assert isinstance(call, _CallRecord)
    return call


def call_environment(mock: Mock) -> dict[str, str]:
    value = called(mock).kwargs['env']
    assert isinstance(value, dict), 'spawn must supply an environment dictionary'
    environment: dict[str, str] = {}
    # dict's dynamic contents are objects until individually validated below.
    for key, item in cast(dict[object, object], value).items():
        assert isinstance(key, str)
        assert isinstance(item, str)
        environment[key] = item
    return environment


def worker_target(worker: Mock) -> Callable[[], None]:
    target = called(worker).kwargs['target']
    assert isinstance(target, FunctionType), 'expected the captured force closure'
    return target


def _entry_log(entry: _OwnedEntry) -> io.TextIOBase:
    assert 'log' in entry, 'owned stop-all fixture must retain its log handle'
    return entry['log']


def _empty_environment(**_kwargs: object) -> dict[str, str]:
    return {}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
    def monotonic(self) -> float:
        return self.now
    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Child:
    pid = 43219
    def __init__(self, marker: Path | None = None) -> None:
        self.marker = marker
        self.exited = False
        self.signals: list[int] = []
        self.poll: Callable[[], int | None] = self._poll
        self.send_signal: Callable[[int], None] = self._send_signal
    def _poll(self) -> int | None:
        if self.marker is not None and self.marker.is_file():
            self.exited = True
        return 0 if self.exited else None
    def _send_signal(self, value: int) -> None:
        self.signals.append(value)
    def wait(self, timeout: float | None = None) -> NoReturn:
        raise AssertionError('No blocking wait is allowed on the GUI thread')


@overload
def definition(relative: str, name: Literal['_stop_requested', 'stop_listener'], namespace: dict[str, object]) -> Callable[[], bool]: ...


@overload
def definition(relative: str, name: Literal['main', 'shutdown'], namespace: dict[str, object]) -> Callable[[], None]: ...


def definition(relative: str, name: str, namespace: dict[str, object]) -> Callable[[], object]:
    tree = ast.parse((ROOT / relative).read_text(encoding='utf-8'))
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    # Parsed nodes are already fully located; do not recurse over them to repair locations.
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROOT / relative), 'exec'), namespace)
    result = namespace[name]
    assert isinstance(result, FunctionType), 'expected an extracted source function'
    return result


# Reviewed white-box access is required to verify private ownership, stop budgets,
# flags and lifecycle jobs. Inline exemptions name reportPrivateUsage only; no
# import-resolution, unknown-type, argument or optional-value diagnostic is hidden.
class LauncherCooperativeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        for ctx in (patch.object(launcher, 'time', self.clock),
                    patch.object(launcher.platform, 'system', return_value='Windows'),
                    patch.object(launcher, '_child_environment', side_effect=_empty_environment),
                    patch.object(launcher, 'processes', []),
                    patch.object(launcher, '_windows_console_available', return_value=True),
                    patch.object(launcher, '_windows_job_handle', None)):
            ctx.start()
            self.addCleanup(ctx.stop)

    def test_core_marker_exits_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'core.stop'
            child = Child(marker)
            entry: _OwnedEntry = {'proc': child, 'pid': child.pid, 'name': 'Medlar Core', 'stop_file': marker, 'log': io.StringIO()}
            with patch.object(launcher, 'processes', [entry]), patch.object(launcher.subprocess, 'run') as force:
                self.assertTrue(launcher.stop_all())
            force.assert_not_called()
            self.assertFalse(marker.exists())
            self.assertTrue(_entry_log(entry).closed)

    def test_hung_child_gets_signal_then_owned_force_after_grace(self) -> None:
        child = Child()
        launcher.processes.append({'proc': child, 'pid': child.pid, 'console_group': True})
        def kill(*_args: object, timeout: float, **_kwargs: object) -> None:
            self.assertGreaterEqual(self.clock.now, 15)
            self.assertLessEqual(timeout, 5)
            child.exited = True
        with patch.object(launcher.subprocess, 'run', side_effect=kill) as force:
            self.assertTrue(launcher._stop_owned_process(child))  # pyright: ignore[reportPrivateUsage]  # exercise owned process shutdown and its bounded budget
        self.assertEqual(child.signals, [signal.CTRL_BREAK_EVENT])
        self.assertEqual(called(force).args[0], ['taskkill', '/PID', '43219', '/T', '/F'])

    def test_missing_console_still_allows_grace(self) -> None:
        child = Child()
        launcher.processes.append({'proc': child, 'pid': child.pid, 'console_group': True})
        child.send_signal = Mock(side_effect=OSError('no console'))
        def finish_child(*_args: object, **_kwargs: object) -> None:
            child.exited = True
        with patch.object(launcher.subprocess, 'run', side_effect=finish_child):
            self.assertTrue(launcher._stop_owned_process(child))  # pyright: ignore[reportPrivateUsage]  # exercise owned process shutdown and its bounded budget
        self.assertGreaterEqual(self.clock.now, 15)

    def test_already_exited_never_signals_or_forces(self) -> None:
        child = Child()
        child.exited = True
        with patch.object(launcher.subprocess, 'run') as force:
            self.assertTrue(launcher._stop_owned_process(child))  # pyright: ignore[reportPrivateUsage]  # exercise owned process shutdown and its bounded budget
        self.assertEqual(child.signals, [])
        force.assert_not_called()

    def test_stop_all_requests_every_owned_child_before_waiting(self) -> None:
        children = [Child(), Child()]
        entries: list[_OwnedEntry] = [{'proc': child, 'pid': child.pid, 'console_group': True, 'name': 'synthetic', 'log': io.StringIO()} for child in children]
        def kill(*_args: object, **_kwargs: object) -> None:
            self.assertTrue(all(child.signals for child in children))
            children[-1].exited = True
            children[0].exited = True
        with patch.object(launcher, 'processes', entries), patch.object(launcher.subprocess, 'run', side_effect=kill):
            self.assertTrue(launcher.stop_all())
        self.assertLessEqual(self.clock.now, 20.1)

    def test_failure_keeps_live_ownership_and_does_not_claim_success(self) -> None:
        child = Child()
        entry: _OwnedEntry = {'proc': child, 'pid': child.pid, 'console_group': True, 'name': 'synthetic', 'log': io.StringIO()}
        with patch.object(launcher, 'processes', [entry]), patch.object(launcher.subprocess, 'run', side_effect=OSError('failed')):
            self.assertFalse(launcher.stop_all())
            self.assertEqual(len(launcher.processes), 1)

    def test_core_start_gets_unique_stop_marker_only_for_core(self) -> None:
        with patch.object(launcher, 'processes', []), patch('builtins.open', mock_open()), \
             patch.object(launcher.subprocess, 'Popen', return_value=Child()) as spawn:
            launcher.start_process('Medlar Core', ['python', '-m', 'core'], wait=0)
            first = call_environment(spawn)[launcher.CORE_STOP_FILE_ENV]
            launcher.start_process('Medlar Core', ['python', '-m', 'core'], wait=0)
            second = call_environment(spawn)[launcher.CORE_STOP_FILE_ENV]
            self.assertNotEqual(first, second)
            self.assertEqual(str(launcher.processes[0].get('stop_file')), first)
            launcher.start_process('Ollama Server', ['ollama', 'serve'], wait=0)
            self.assertNotIn(launcher.CORE_STOP_FILE_ENV, call_environment(spawn))

    def test_escalation_uses_one_shared_budget_for_multiple_hung_children(self) -> None:
        children = [Child(), Child(), Child()]
        entries: list[_OwnedEntry] = [{'proc': child, 'pid': child.pid, 'console_group': True, 'name': 'synthetic', 'log': io.StringIO()} for child in children]
        def slow_failed_force(*_args: object, timeout: float, **_kwargs: object) -> None:
            self.clock.now += timeout
            raise subprocess.TimeoutExpired('synthetic taskkill', timeout)
        with patch.object(launcher, 'processes', entries), patch.object(launcher.subprocess, 'run', side_effect=slow_failed_force) as force:
            self.assertFalse(launcher.stop_all())
        force.assert_called_once()
        self.assertLessEqual(self.clock.now, 20.1)

    def test_empty_ownership_does_not_probe_or_stop_reused_services(self) -> None:
        with patch.object(launcher, 'processes', []), patch.object(launcher, '_ollama_endpoint_open') as probe, \
             patch.object(launcher.subprocess, 'run') as force:
            self.assertTrue(launcher.stop_all())
            self.assertTrue(launcher.stop_all())
        probe.assert_not_called()
        force.assert_not_called()


class ResponsiveStopTests(unittest.TestCase):
    app: ClassVar[_Application]
    @classmethod
    def setUpClass(cls) -> None:
        application = QApplication.instance() or QApplication([])
        assert isinstance(application, _Application)
        cls.app = application
    def setUp(self) -> None:
        self.clock = Clock()
        raw_window = medlar_app.ControlCenter.__new__(medlar_app.ControlCenter)
        QMainWindow.__init__(raw_window)
        # Precise fixture view: this suite initializes all lifecycle fields below.
        self.window = cast(_Window, raw_window)
        self.window.status_label = QLabel()
        self.window.chat_status = QLabel()
        self.window._ollama_timer = Mock()  # pyright: ignore[reportPrivateUsage]  # inject a synthetic startup timer
        self.window._stop_timer = Mock()  # pyright: ignore[reportPrivateUsage]  # inject a synthetic stop polling timer
        self.window._stop_jobs = {}  # pyright: ignore[reportPrivateUsage]  # inspect the owned shutdown jobs and worker completion
        self.window._stop_file = self.window._ollama_stop_file = None  # pyright: ignore[reportPrivateUsage]  # initialize or inspect the owned Twitch stop marker
        self.window.launcher_proc = self.window.ollama_proc = None
        self.window._ollama_ready = True  # pyright: ignore[reportPrivateUsage]  # control the fixture brain readiness state
        self.window._ollama_starting = False  # pyright: ignore[reportPrivateUsage]  # control the fixture startup state
        self.window._ollama_error = self.window._shutdown_failure = None  # pyright: ignore[reportPrivateUsage]  # control the fixture startup failure state
        self.window._restart_pending = self.window._close_pending = self.window._close_ready = self.window._chat_closed = False  # pyright: ignore[reportPrivateUsage]  # verify pending restart is cancelled or delayed
        ctx = patch.object(medlar_app, 'time', self.clock)
        ctx.start()
        self.addCleanup(ctx.stop)
    def tearDown(self) -> None:
        self.window._stop_jobs.clear()  # pyright: ignore[reportPrivateUsage]  # inspect the owned shutdown jobs and worker completion
        self.window.deleteLater()
        self.app.processEvents()
    def owned(self, directory: str, mode: Literal['twitch', 'ollama'] = 'twitch', cooperative: bool = False) -> Child:
        marker = Path(directory) / (mode + '.stop')
        child = Child(marker if cooperative else None)
        if mode == 'twitch':
            self.window.launcher_proc, self.window._stop_file = child, marker  # pyright: ignore[reportPrivateUsage]  # initialize or inspect the owned Twitch stop marker
        else:
            self.window.ollama_proc, self.window._ollama_stop_file = child, marker  # pyright: ignore[reportPrivateUsage]  # initialize or inspect the owned Ollama stop marker
        return child
    def marker(self, mode: Literal['twitch', 'ollama'] = 'twitch') -> Path:
        marker = self.window._stop_file if mode == 'twitch' else self.window._ollama_stop_file  # pyright: ignore[reportPrivateUsage]  # initialize or inspect the owned Twitch stop marker
        assert marker is not None, 'expected this owned launcher to have a stop marker'
        return marker

    def shutdown_failure(self) -> str:
        failure = self.window._shutdown_failure  # pyright: ignore[reportPrivateUsage]  # assert incomplete shutdown without claiming success
        assert failure is not None, 'expected shutdown failure before inspecting its text'
        return failure

    def test_stop_returns_immediately_and_qt_processes_a_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            child = self.owned(directory)
            self.assertFalse(self.window.on_stop())
            seen: list[str] = []
            QTimer.singleShot(0, lambda: seen.append('heartbeat'))
            self.app.processEvents()
            self.assertEqual(seen, ['heartbeat'])
            self.assertIs(self.window.launcher_proc, child)
            self.assertTrue(self.marker().exists())
    def test_repeat_stop_does_not_extend_deadline_or_start_second_force(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.owned(directory)
            self.window.on_stop()
            self.clock.now = 10
            self.window.on_stop()
            self.assertEqual(self.window._stop_jobs['twitch']['deadline'], 25)  # pyright: ignore[reportPrivateUsage]  # inspect the owned shutdown jobs and worker completion
            self.clock.now = 25
            with patch.object(medlar_app.threading, 'Thread') as worker:
                self.window._poll_stops()  # pyright: ignore[reportPrivateUsage]  # advance the real shutdown state machine without blocking
                self.window._poll_stops()  # pyright: ignore[reportPrivateUsage]  # advance the real shutdown state machine without blocking
                worker.assert_called_once()
    def test_restart_waits_then_spawns_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(self.window, 'on_start') as start:
            child = self.owned(directory)
            self.window.on_restart()
            start.assert_not_called()
            child.exited = True
            self.window._poll_stops()  # pyright: ignore[reportPrivateUsage]  # advance the real shutdown state machine without blocking
            self.window._poll_stops()  # pyright: ignore[reportPrivateUsage]  # advance the real shutdown state machine without blocking
            start.assert_called_once()
    def test_explicit_stop_cancels_queued_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(self.window, 'on_start') as start:
            child = self.owned(directory)
            self.window.on_restart()
            self.window.on_stop()
            child.exited = True
            self.window._poll_stops()  # pyright: ignore[reportPrivateUsage]  # advance the real shutdown state machine without blocking
            start.assert_not_called()
    def test_close_requests_both_together_then_defers_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(medlar_app.QTimer, 'singleShot') as later:
            twitch = self.owned(directory)
            brain = self.owned(directory, 'ollama')
            event = QCloseEvent()
            self.window.closeEvent(event)
            self.assertFalse(event.isAccepted())
            self.assertTrue(self.window._close_pending)  # pyright: ignore[reportPrivateUsage]  # verify close remains deferred until shutdown completes
            self.assertEqual(set(self.window._stop_jobs), {'twitch', 'ollama'})  # pyright: ignore[reportPrivateUsage]  # inspect the owned shutdown jobs and worker completion
            self.assertTrue(self.marker().exists())
            self.assertTrue(self.marker('ollama').exists())
            twitch.exited = brain.exited = True
            self.window._poll_stops()  # pyright: ignore[reportPrivateUsage]  # advance the real shutdown state machine without blocking
            later.assert_called_once()
            self.assertTrue(self.window._close_ready)  # pyright: ignore[reportPrivateUsage]  # verify completion permits exactly one deferred close
    def test_close_cancels_pending_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(self.window, 'on_start') as start, \
             patch.object(medlar_app.QTimer, 'singleShot'):
            child = self.owned(directory)
            self.window.on_restart()
            self.window.closeEvent(QCloseEvent())
            child.exited = True
            self.window._poll_stops()  # pyright: ignore[reportPrivateUsage]  # advance the real shutdown state machine without blocking
            start.assert_not_called()
    def test_forcing_runs_in_worker_and_ui_never_waits(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(medlar_app.threading, 'Thread') as worker, \
             patch.object(self.window, '_force_stop_owned_launcher') as force:
            child = self.owned(directory)
            self.window.on_stop()
            self.clock.now = 25
            self.window._poll_stops()  # pyright: ignore[reportPrivateUsage]  # advance the real shutdown state machine without blocking
            force.assert_not_called()
            worker.return_value.start.assert_called_once()
            def finish_child(proc: Child) -> None:
                proc.exited = True
            force.side_effect = finish_child
            worker_target(worker)()
            self.window._poll_stops()  # pyright: ignore[reportPrivateUsage]  # advance the real shutdown state machine without blocking
            force.assert_called_once_with(child)
            self.assertIsNone(self.window.launcher_proc)
    def test_failed_force_retains_process_and_blocks_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(medlar_app.threading, 'Thread') as worker:
            child = self.owned(directory)
            self.window.on_restart()
            self.clock.now = 25
            self.window._poll_stops()  # pyright: ignore[reportPrivateUsage]  # advance the real shutdown state machine without blocking
            with patch.object(self.window, '_force_stop_owned_launcher', side_effect=OSError('failure')):
                worker_target(worker)()
            self.window._poll_stops()  # pyright: ignore[reportPrivateUsage]  # advance the real shutdown state machine without blocking
            self.assertIs(self.window.launcher_proc, child)
            self.assertIn('Shutdown incomplete', self.shutdown_failure())
            self.assertFalse(self.window._restart_pending)  # pyright: ignore[reportPrivateUsage]  # verify pending restart is cancelled or delayed
    def test_force_worker_overrun_keeps_ownership_until_it_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(medlar_app.threading, 'Thread'):
            self.owned(directory)
            self.window.on_stop()
            self.clock.now = 25
            self.window._poll_stops()  # pyright: ignore[reportPrivateUsage]  # advance the real shutdown state machine without blocking
            self.clock.now = 33
            self.window._poll_stops()  # pyright: ignore[reportPrivateUsage]  # advance the real shutdown state machine without blocking
            self.assertIn('twitch', self.window._stop_jobs)  # pyright: ignore[reportPrivateUsage]  # inspect the owned shutdown jobs and worker completion
            self.assertIn('Shutdown incomplete', self.shutdown_failure())
    def test_ongoing_stop_blocks_start_and_status_health_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.owned(directory)
            self.window.on_stop()
            with patch.object(self.window, '_spawn_launcher_mode') as spawn, patch.object(Path, 'open', side_effect=AssertionError('health scan')):
                self.window.on_start()
                self.window.refresh_status()
                spawn.assert_not_called()
    def test_quit_event_uses_deferred_close(self) -> None:
        with patch.object(self.window, 'close') as close:
            self.assertTrue(self.window.eventFilter(self.app, QEvent(QEvent.Type.Quit)))
            close.assert_called_once()

    def test_close_during_startup_cancels_deferred_ollama_start(self) -> None:
        self.window.closeEvent(QCloseEvent())
        with patch.object(self.window, '_spawn_launcher_mode') as spawn:
            self.window._start_ollama()  # pyright: ignore[reportPrivateUsage]  # verify deferred startup stays cancelled after close
        spawn.assert_not_called()

    def test_marker_failure_uses_owned_worker_without_waiting(self) -> None:
        child = Child()
        self.window.launcher_proc = child
        with patch.object(medlar_app.threading, 'Thread') as worker, patch.object(self.window, '_force_stop_owned_launcher') as force:
            self.assertFalse(self.window.on_stop())
            force.assert_not_called()
            worker.assert_not_called()
            self.clock.now = 25
            self.window._poll_stops()  # pyright: ignore[reportPrivateUsage]  # advance the real shutdown state machine without blocking
            worker.return_value.start.assert_called_once()

    def test_failure_in_first_close_request_survives_second_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(medlar_app, 'os', types.SimpleNamespace(name='posix')):
            twitch = self.owned(directory)
            twitch.poll = Mock(return_value=2)
            self.owned(directory, 'ollama', cooperative=True)
            event = QCloseEvent()
            self.window.closeEvent(event)
            self.assertFalse(event.isAccepted())
            self.assertIn('Shutdown incomplete', self.shutdown_failure())
            self.assertFalse(self.window._close_pending)  # pyright: ignore[reportPrivateUsage]  # verify close remains deferred until shutdown completes

    def test_no_owned_processes_close_without_stopping_external_service(self) -> None:
        with patch.object(self.window, '_force_stop_owned_launcher') as force, patch.object(medlar_app, '_ollama_endpoint_open') as probe:
            event = QCloseEvent()
            self.window.closeEvent(event)
        self.assertTrue(event.isAccepted())
        force.assert_not_called()
        probe.assert_not_called()


class CoreCleanupTests(unittest.TestCase):
    def test_core_marker_detection_does_not_read_marker_contents(self) -> None:
        fn = definition('MedlarTV/core/main.py', '_stop_requested', {'os': os, 'Path': Path})
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'core.stop'
            with patch.dict(os.environ, {'MEDLARTV_CORE_STOP_FILE': str(marker)}, clear=True):
                self.assertFalse(fn())
                marker.touch()
                self.assertTrue(fn())
    def test_core_start_cancelled_before_network_initialization(self) -> None:
        shutdown, initialize, start = Mock(), Mock(), Mock()
        namespace: dict[str, object] = {'os': os, 'configure_logging': Mock(), '_stop_requested': lambda: True,
                     'shutdown': shutdown, 'initialize_systems': initialize, 'start_listener': start}
        fn = definition('MedlarTV/core/main.py', 'main', namespace)
        with patch.dict(os.environ, {}, clear=True):
            fn()
        shutdown.assert_called_once()
        initialize.assert_not_called()
        start.assert_not_called()
    def test_core_reports_incomplete_cleanup_with_nonzero_exit(self) -> None:
        for stopped, code in ((True, 0), (False, 1)):
            exit_core = Mock()
            namespace: dict[str, object] = {'os': os, 'log': Mock(), 'stop_listener': Mock(return_value=stopped), 'sys': types.SimpleNamespace(exit=exit_core)}
            fn = definition('MedlarTV/core/main.py', 'shutdown', namespace)
            fn()
            exit_core.assert_called_once_with(code)
    def test_listener_store_closes_only_after_workers_finish(self) -> None:
        for busy in (False, True):
            clock = Clock()
            instance = Mock()
            instance._has_live_background_workers.return_value = busy
            store = Mock()
            instance._viewer_profiles = store
            namespace: dict[str, object] = {'_listener_instance': instance, '_listener_thread': None,
                         '_listener_lock': threading.Lock(), 'threading': threading, 'time': clock, 'log': Mock()}
            fn = definition('MedlarTV/tools/twitch_listener.py', 'stop_listener', namespace)
            self.assertEqual(fn(), not busy)
            if busy:
                store.close.assert_not_called()
                self.assertIs(namespace['_listener_instance'], instance)
                self.assertLessEqual(clock.now, 10.1)
            else:
                store.close.assert_called_once()
                self.assertIsNone(namespace['_listener_instance'])

    def test_store_close_failure_retains_instance_and_reports_incomplete(self) -> None:
        instance = Mock()
        instance._has_live_background_workers.return_value = False
        instance._viewer_profiles.close.side_effect = OSError('synthetic close failure')
        namespace: dict[str, object] = {'_listener_instance': instance, '_listener_thread': None,
                     '_listener_lock': threading.Lock(), 'threading': threading, 'time': Clock(), 'log': Mock()}
        fn = definition('MedlarTV/tools/twitch_listener.py', 'stop_listener', namespace)
        self.assertFalse(fn())
        self.assertIs(namespace['_listener_instance'], instance)

    def test_core_cancellation_after_initialization_skips_listener_start(self) -> None:
        shutdown, initialize, start = Mock(), Mock(), Mock()
        namespace: dict[str, object] = {'os': os, 'configure_logging': Mock(), '_stop_requested': Mock(side_effect=[False, True]),
                     'shutdown': shutdown, 'initialize_systems': initialize, 'start_listener': start}
        fn = definition('MedlarTV/core/main.py', 'main', namespace)
        with patch.dict(os.environ, {}, clear=True):
            fn()
        initialize.assert_called_once()
        shutdown.assert_called_once()
        start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
