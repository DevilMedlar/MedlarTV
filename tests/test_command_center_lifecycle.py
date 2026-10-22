"""Offline Command Center lifecycle checks using fake child processes."""

import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, mock_open, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

try:
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QLineEdit, QMainWindow, QPlainTextEdit, QPushButton
except ImportError:
    QApplication = None


class StopFileProcess:
    """A child that exits only after observing its private stop request."""

    def __init__(self, stop_file):
        self.pid = 43210
        self.stop_file = stop_file
        self.saw_stop_file = False
        self.exited = False

    def poll(self):
        if self.stop_file.exists():
            self.saw_stop_file = True
            self.exited = True
        return 0 if self.exited else None

    def wait(self, timeout=None):
        result = self.poll()
        if result is None:
            raise subprocess.TimeoutExpired("fake launcher", timeout)
        return result


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class CommandCenterLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from MedlarTV.app import medlar_app

        cls.medlar_app = medlar_app
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # Skip ControlCenter.__init__: it loads settings, starts timers and
        # installs app-wide quit hooks. Lifecycle methods need only this state.
        self.window = self.medlar_app.ControlCenter.__new__(self.medlar_app.ControlCenter)
        QMainWindow.__init__(self.window)
        self.window.status_label = QLabel("Status: Idle")
        self.window.chat_status = QLabel("Ready")
        self.window.launcher_proc = None
        self.window._stop_file = None
        self.window.ollama_proc = None
        self.window._ollama_stop_file = None
        self.window._ollama_ready = False
        self.window._ollama_starting = False
        self.window._ollama_error = None
        self.window._ollama_start_deadline = 0.0
        self.window._ollama_timer = Mock()
        self.window._shutdown_failure = None
        self.window._chat_closed = False
        endpoint = patch.object(self.medlar_app, "_ollama_endpoint_open", return_value=False)
        endpoint.start()
        self.addCleanup(endpoint.stop)

    def tearDown(self):
        if hasattr(self.window, "_stop_timer"):
            self.window._stop_timer.stop()
        self.window.deleteLater()
        self.app.processEvents()

    def expire_stop(self):
        """Advance shutdown with a synchronous fake worker, never real taskkill."""
        for job in self.window._stop_jobs.values():
            job["deadline"] = 0
        with patch.object(self.medlar_app.threading, "Thread",
                          side_effect=lambda **kwargs: types.SimpleNamespace(start=kwargs["target"])):
            self.window._poll_stops()
            self.window._poll_stops()

    def test_main_shows_window_without_topmost_flag(self):
        fake_app = Mock()
        fake_app.exec.return_value = 0
        fake_window = Mock()
        fake_timer = Mock()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(Path, "cwd", return_value=Path(directory)), \
             patch.object(self.medlar_app, "QApplication", return_value=fake_app), \
             patch.object(self.medlar_app, "ControlCenter", return_value=fake_window), \
             patch.object(self.medlar_app, "QTimer", return_value=fake_timer), \
             patch.object(self.medlar_app.sys, "exit") as exit_app:
            self.medlar_app.main()

        fake_window.setWindowFlag.assert_not_called()
        fake_window.show.assert_called_once_with()
        fake_window.raise_.assert_called_once_with()
        fake_window.activateWindow.assert_called_once_with()
        fake_app.setQuitOnLastWindowClosed.assert_called_once_with(True)
        fake_timer.setSingleShot.assert_called_once_with(True)
        fake_timer.start.assert_called_once_with(250)
        fake_timer.timeout.connect.assert_called_once()
        fake_app.exec.assert_called_once_with()
        exit_app.assert_called_once_with(0)

    def ready_brain(self, directory):
        stop_file = Path(directory) / "ollama.stop"
        brain = StopFileProcess(stop_file)
        self.window.ollama_proc = brain
        self.window._ollama_stop_file = stop_file
        self.window._ollama_ready = True
        return brain

    def test_start_does_not_spawn_second_live_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            existing = StopFileProcess(Path(directory) / "not-requested")
            self.window.launcher_proc = existing
            with patch.object(self.medlar_app.subprocess, "Popen") as popen:
                self.window.on_start()
            popen.assert_not_called()
            self.assertIs(self.window.launcher_proc, existing)

    def test_failed_spawn_does_not_leave_a_running_status(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {}, clear=True), \
             patch.object(Path, "open", mock_open()), \
             patch.object(Path, "mkdir"), \
             patch.object(self.medlar_app.subprocess, "Popen", side_effect=OSError("spawn failed")):
            self.ready_brain(directory)
            self.window.on_start()

        self.assertIsNone(self.window.launcher_proc)
        self.assertNotIn("Starting", self.window.status_label.text())

    def test_repeated_start_stop_uses_a_new_private_stop_file(self):
        spawned = []
        real_path_open = Path.open
        fake_log_open = mock_open()

        def open_logs_only(path, *args, **kwargs):
            if path.name in ("launcher_spawn.log", "twitch_spawn.log", "control_center.log"):
                return fake_log_open(path, *args, **kwargs)
            return real_path_open(path, *args, **kwargs)

        def fake_spawn(_args, **kwargs):
            self.assertEqual(_args[-1], "--mode=twitch")
            self.assertEqual(kwargs["env"]["MEDLARTV_OWNER_PID"], str(os.getpid()))
            child = StopFileProcess(Path(kwargs["env"]["MEDLARTV_STOP_FILE"]))
            spawned.append(child)
            return child

        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {}, clear=True), \
             patch.object(self.window, "_service_root", return_value=Path(directory)), \
             patch.object(self.medlar_app.tempfile, "gettempdir", return_value=directory), \
             patch.object(Path, "open", open_logs_only), \
             patch.object(Path, "mkdir"), \
             patch.object(self.medlar_app.subprocess, "Popen", side_effect=fake_spawn), \
             patch.object(self.medlar_app.QTimer, "singleShot"), \
             patch.object(self.window, "_force_stop_owned_launcher") as fallback:
            brain = self.ready_brain(directory)
            for _ in range(2):
                self.window.on_start()
                self.assertIsNotNone(self.window.launcher_proc)
                self.assertTrue(self.window.on_stop())
                self.assertIs(self.window.ollama_proc, brain)
                self.assertIsNone(brain.poll())
            fallback.assert_not_called()

        self.assertEqual(len(spawned), 2)
        self.assertNotEqual(spawned[0].stop_file, spawned[1].stop_file)
        self.assertTrue(all(child.saw_stop_file for child in spawned))

    def test_stop_requests_shutdown_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            stop_file = Path(directory) / "stop.request"
            child = StopFileProcess(stop_file)
            self.window.launcher_proc = child
            self.window._stop_file = stop_file
            with patch.object(self.medlar_app.subprocess, "run") as run:
                self.window.on_stop()
                self.window.on_stop()

            self.assertTrue(child.saw_stop_file)
            self.assertIsNone(self.window.launcher_proc)
            self.assertIn("Twitch stopped", self.window.status_label.text())
            self.assertFalse(any("taskkill" in repr(call).lower() for call in run.call_args_list))

    def test_twitch_stop_keeps_app_ollama_running(self):
        with tempfile.TemporaryDirectory() as directory:
            stop_file = Path(directory) / "stop.request"
            child = StopFileProcess(stop_file)
            self.window.launcher_proc = child
            self.window._stop_file = stop_file
            brain = self.ready_brain(directory)
            with patch.object(self.medlar_app, "_ollama_endpoint_open", side_effect=AssertionError("Twitch Stop checked Ollama")), \
                 patch.object(self.medlar_app.subprocess, "run") as run:
                result = self.window.on_stop()

            self.assertTrue(result)
            self.assertTrue(child.saw_stop_file)
            self.assertIsNone(self.window.launcher_proc)
            self.assertIs(self.window.ollama_proc, brain)
            self.assertIsNone(brain.poll())
            self.assertIn("Ollama ready", self.window.status_label.text())
            run.assert_not_called()

    def test_fallback_targets_only_owned_launcher_pid_tree(self):
        class OwnedProcess:
            pid = 43211

            def __init__(self):
                self.exited = False

            def poll(self):
                return 0 if self.exited else None

            def wait(self, timeout=None):
                return 0 if self.exited else None

        child = OwnedProcess()

        def fake_taskkill(_args, **_kwargs):
            child.exited = True

        self.window.launcher_proc = child
        with patch.object(self.medlar_app.subprocess, "run", side_effect=fake_taskkill) as run:
            self.window._force_stop_owned_launcher(child)

        self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "43211", "/T", "/F"])

    def test_close_refuses_when_owned_launcher_survives(self):
        class StuckProcess:
            pid = 43212

            def poll(self):
                return None

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("fake launcher", timeout)

        with tempfile.TemporaryDirectory() as directory:
            self.window.launcher_proc = StuckProcess()
            self.window._stop_file = Path(directory) / "stop.request"
            event = QCloseEvent()
            with patch.object(self.window, "_force_stop_owned_launcher") as fallback:
                self.window.closeEvent(event)
                self.expire_stop()

            fallback.assert_called_once_with(self.window.launcher_proc)
            self.assertFalse(event.isAccepted())
            self.assertFalse(self.window._chat_closed)
            self.assertIn("Shutdown incomplete", self.window.status_label.text())

    def test_close_without_prior_stop_requests_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            stop_file = Path(directory) / "stop.request"
            child = StopFileProcess(stop_file)
            self.window.launcher_proc = child
            self.window._stop_file = stop_file
            with patch.object(self.medlar_app.subprocess, "run") as run:
                self.window.closeEvent(QCloseEvent())

            self.assertTrue(self.window._chat_closed)
            self.assertTrue(child.saw_stop_file)
            self.assertIsNone(self.window.launcher_proc)
            self.assertFalse(any("taskkill" in repr(call).lower() for call in run.call_args_list))

    def test_window_schedules_local_brain_after_constructor(self):
        control_center = self.medlar_app.ControlCenter
        with patch.object(self.medlar_app, "load_dotenv", return_value=False), \
             patch.object(control_center, "_build_pages"), \
             patch.object(control_center, "refresh_settings"), \
             patch.object(self.medlar_app.QTimer, "singleShot") as scheduled, \
             patch.object(self.medlar_app.subprocess, "Popen", side_effect=AssertionError("Constructor spawned a service")) as spawn:
            window = control_center()
            try:
                scheduled.assert_called_once()
                self.assertEqual(scheduled.call_args.args[0], 0)
                self.assertIs(scheduled.call_args.args[1].__self__, window)
                self.assertEqual(scheduled.call_args.args[1].__name__, "_start_ollama")
                spawn.assert_not_called()
                self.assertIsNone(window.launcher_proc)
                self.assertIsNone(window.ollama_proc)
            finally:
                window.timer.stop()
                window.close()
                window.deleteLater()

    def test_ollama_helper_uses_own_mode_without_twitch_credentials(self):
        fake_core = types.ModuleType("MedlarTV.core")
        fake_core.__path__ = []
        fake_credentials = types.ModuleType("MedlarTV.core.local_credentials")
        fake_credentials.CREDENTIAL_FIELDS = {"SYNTHETIC_TWITCH_TOKEN": "test only"}
        child = Mock(pid=43214)
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(sys.modules, {
                 "MedlarTV.core": fake_core,
                 "MedlarTV.core.local_credentials": fake_credentials,
             }), \
             patch.dict(os.environ, {"SYNTHETIC_TWITCH_TOKEN": "test value"}, clear=True), \
             patch.object(self.window, "_service_root", return_value=Path(directory)), \
             patch.object(self.medlar_app.tempfile, "gettempdir", return_value=directory), \
             patch.object(Path, "mkdir"), \
             patch.object(Path, "open", mock_open()), \
             patch.object(self.medlar_app.subprocess, "Popen", return_value=child) as popen:
            spawned, stop_file = self.window._spawn_launcher_mode("ollama")

        self.assertIs(spawned, child)
        self.assertEqual(stop_file.parent, Path(directory))
        self.assertEqual(popen.call_args.args[0][-1], "--mode=ollama")
        self.assertEqual(popen.call_args.kwargs["env"]["MEDLARTV_OWNER_PID"], str(os.getpid()))
        self.assertNotIn("SYNTHETIC_TWITCH_TOKEN", popen.call_args.kwargs["env"])

    def test_local_brain_startup_waits_for_readiness_without_twitch(self):
        with tempfile.TemporaryDirectory() as directory:
            stop_file = Path(directory) / "ollama.stop"
            brain = StopFileProcess(stop_file)
            with patch.object(self.window, "_service_root", return_value=Path(directory)), \
                 patch.object(self.window, "_spawn_launcher_mode", return_value=(brain, stop_file)) as spawn, \
                 patch.object(self.medlar_app.time, "monotonic", return_value=10), \
                 patch.object(self.medlar_app.subprocess, "Popen", side_effect=AssertionError("Unmocked service spawn")):
                self.window._start_ollama()

            spawn.assert_called_once_with("ollama")
            self.assertIs(self.window.ollama_proc, brain)
            self.assertTrue(self.window._ollama_starting)
            self.assertFalse(self.window._ollama_ready)
            self.assertIsNone(self.window.launcher_proc)
            self.window._ollama_timer.start.assert_called_once_with(250)

            with patch.object(self.medlar_app, "_ollama_endpoint_open", return_value=True):
                self.window._check_ollama_startup()
            self.assertTrue(self.window._ollama_ready)
            self.assertFalse(self.window._ollama_starting)
            self.assertIs(self.window.ollama_proc, brain)
            self.assertIn("Ollama ready", self.window.status_label.text())

    def test_external_ollama_endpoint_is_not_adopted(self):
        with patch.object(self.window, "_service_root", return_value=Path(tempfile.gettempdir())), \
             patch.object(self.medlar_app, "_ollama_endpoint_open", return_value=True), \
             patch.object(self.window, "_spawn_launcher_mode") as spawn:
            self.window._start_ollama()

        spawn.assert_not_called()
        self.assertIsNone(self.window.ollama_proc)
        self.assertFalse(self.window._ollama_ready)
        self.assertIn("outside Command Center", self.window._ollama_error)

    def test_local_brain_spawn_failure_blocks_twitch_start(self):
        with patch.object(self.window, "_service_root", return_value=Path(tempfile.gettempdir())), \
             patch.object(self.window, "_spawn_launcher_mode", side_effect=OSError("offline spawn failure")) as spawn:
            self.window._start_ollama()
            self.window.on_start()

        spawn.assert_called_once_with("ollama")
        self.assertIsNone(self.window.ollama_proc)
        self.assertIsNone(self.window.launcher_proc)
        self.assertFalse(self.window._ollama_ready)
        self.assertIn("offline spawn failure", self.window._ollama_error)

    def test_local_brain_readiness_timeout_cleans_partial_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            stop_file = Path(directory) / "ollama.stop"
            brain = StopFileProcess(stop_file)
            self.window.ollama_proc = brain
            self.window._ollama_stop_file = stop_file
            self.window._ollama_starting = True
            self.window._ollama_start_deadline = 10
            with patch.object(self.medlar_app.time, "monotonic", return_value=11), \
                 patch.object(self.medlar_app.subprocess, "run") as taskkill:
                self.window._check_ollama_startup()

        self.assertTrue(brain.saw_stop_file)
        self.assertIsNone(self.window.ollama_proc)
        self.assertFalse(self.window._ollama_ready)
        self.assertIn("did not become ready", self.window._ollama_error)
        taskkill.assert_not_called()

    def test_chat_works_when_twitch_is_stopped_and_brain_is_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            self.ready_brain(directory)
            self.window.chat_input = QLineEdit("hello")
            self.window.btn_chat_send = QPushButton("Send")
            self.window.btn_chat_clear = QPushButton("New chat")
            self.window.chat_transcript = QPlainTextEdit()
            self.window.chk_llm = QCheckBox("LLM Brain")
            self.window.chk_llm.setChecked(True)
            self.window.chk_filter = QCheckBox("Content Filter")
            self.window._chat_history = []
            self.window._chat_busy = False
            with patch.object(self.medlar_app.threading, "Thread") as thread, \
                 patch.object(self.medlar_app.subprocess, "Popen", side_effect=AssertionError("Chat spawned a service")):
                self.window.send_chat_message()

        thread.return_value.start.assert_called_once_with()
        self.assertIsNone(self.window.launcher_proc)
        self.assertTrue(self.window._chat_busy)
        self.assertIn("You: hello", self.window.chat_transcript.toPlainText())

    def test_close_without_twitch_stop_cleans_both_owned_helpers(self):
        with tempfile.TemporaryDirectory() as directory:
            twitch_stop = Path(directory) / "twitch.stop"
            twitch = StopFileProcess(twitch_stop)
            self.window.launcher_proc = twitch
            self.window._stop_file = twitch_stop
            brain = self.ready_brain(directory)
            event = QCloseEvent()
            with patch.object(self.medlar_app.subprocess, "run") as taskkill:
                self.window.closeEvent(event)

            self.assertTrue(event.isAccepted())
            self.assertTrue(self.window._chat_closed)
            self.assertTrue(twitch.saw_stop_file)
            self.assertTrue(brain.saw_stop_file)
            self.assertIsNone(self.window.launcher_proc)
            self.assertIsNone(self.window.ollama_proc)
            taskkill.assert_not_called()

    def test_partial_twitch_shutdown_still_stops_local_brain(self):
        class StuckTwitch:
            pid = 43213

            def poll(self):
                return None

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("fake Twitch launcher", timeout)

        with tempfile.TemporaryDirectory() as directory:
            self.window.launcher_proc = StuckTwitch()
            self.window._stop_file = Path(directory) / "twitch.stop"
            brain = self.ready_brain(directory)
            event = QCloseEvent()
            with patch.object(self.window, "_force_stop_owned_launcher") as fallback:
                self.window.closeEvent(event)
                self.expire_stop()

            fallback.assert_called_once_with(self.window.launcher_proc)
            self.assertFalse(event.isAccepted())
            self.assertFalse(self.window._chat_closed)
            self.assertTrue(brain.saw_stop_file)
            self.assertIsNone(self.window.ollama_proc)
            self.assertIn("Shutdown incomplete", self.window.status_label.text())

    def test_windows_post_exit_rc2_allows_closing_both_owned_helpers(self):
        twitch = Mock(pid=43215)
        twitch.poll.return_value = 2
        brain = Mock(pid=43216)
        brain.poll.return_value = 2
        self.window.launcher_proc = twitch
        self.window.ollama_proc = brain
        self.window._ollama_ready = True
        event = QCloseEvent()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(self.medlar_app, "os", types.SimpleNamespace(name="nt")), \
             patch.object(Path, "cwd", return_value=Path(directory)), \
             patch.object(Path, "open", mock_open()), \
             patch.object(self.window, "_force_stop_owned_launcher") as fallback, \
             patch.object(self.medlar_app.subprocess, "run") as taskkill:
            self.window._post_start_check()
            self.assertIsNone(self.window._shutdown_failure)
            self.window.closeEvent(event)

        self.assertTrue(event.isAccepted())
        self.assertTrue(self.window._chat_closed)
        self.assertIsNone(self.window.launcher_proc)
        self.assertIsNone(self.window.ollama_proc)
        self.assertIsNone(self.window._shutdown_failure)
        fallback.assert_not_called()
        taskkill.assert_not_called()

    def test_non_windows_post_exit_rc2_still_refuses_close(self):
        twitch = Mock(pid=43217)
        twitch.poll.return_value = 2
        self.window.launcher_proc = twitch
        event = QCloseEvent()
        with patch.object(self.medlar_app, "os", types.SimpleNamespace(name="posix")):
            self.window.closeEvent(event)

        self.assertFalse(event.isAccepted())
        self.assertFalse(self.window._chat_closed)
        self.assertIs(self.window.launcher_proc, twitch)
        self.assertIn("Shutdown incomplete", self.window.status_label.text())


if __name__ == "__main__":
    unittest.main()
