"""Native shutdown with fake consoles, signal handlers and vendor entries."""
import ast
import ctypes
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, mock_open, patch

import launcher
from MedlarTV.app import medlar_app

ROOT = Path(__file__).resolve().parents[1]
entry = types.ModuleType('offline_python_service_entry')
entry.__dict__.update(__file__=str(ROOT / 'MedlarTV/tools/python_service_entry.py'))
exec(compile(Path(entry.__file__).read_text(encoding='utf-8'), entry.__file__, 'exec'), entry.__dict__)


def source_function(path, name, namespace, owner=None):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    nodes = tree.body if owner is None else next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == owner).body
    node = next(n for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(path), 'exec'), namespace)
    return namespace[name]


class Child:
    def __init__(self, pid=43270):
        self.pid, self.exited = pid, False
        self.send_signal = Mock()
    def poll(self):
        return 0 if self.exited else None
    def wait(self, timeout=None):
        if not self.exited:
            raise AssertionError('Only fake APIs may exit a child')
        return 0


class ConsoleAndOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.child = Child()
        self.owned = {'proc': self.child, 'pid': self.child.pid, 'console_group': True}
        for ctx in (patch.object(launcher, 'processes', [self.owned]),
                    patch.object(launcher.platform, 'system', return_value='Windows'),
                    patch.object(launcher, '_child_environment', side_effect=lambda **kw: {}),
                    patch.object(launcher.time, 'sleep')):
            ctx.start()
            self.addCleanup(ctx.stop)

    def test_read_only_console_probe(self):
        probe = source_function(ROOT / 'launcher.py', '_windows_console_available', {})
        api = Mock()
        for value in (65001, 0):
            api.GetConsoleCP.return_value = value
            with patch.object(ctypes, 'WinDLL', return_value=api) as dll:
                self.assertEqual(probe(), bool(value))
            dll.assert_called_once_with('kernel32', use_last_error=True)
        self.assertEqual(api.method_calls, [('GetConsoleCP', (), {}), ('GetConsoleCP', (), {})])

    def test_console_probe_error(self):
        probe = source_function(ROOT / 'launcher.py', '_windows_console_available', {})
        with patch.object(ctypes, 'WinDLL', side_effect=OSError):
            self.assertFalse(probe())

    def spawn(self, console, name='Ollama Server', command=None):
        with patch.object(launcher, '_windows_console_available', return_value=console), \
             patch('builtins.open', mock_open()), \
             patch.object(launcher.subprocess, 'Popen', return_value=self.child) as popen:
            self.assertTrue(launcher.start_process(name, command or ['ollama', 'serve'], wait=0))
        return popen.call_args

    def test_owned_child_inherits_console_as_separate_group(self):
        call = self.spawn(True)
        self.assertEqual(call.kwargs['creationflags'], subprocess.CREATE_NEW_PROCESS_GROUP)
        self.assertFalse(call.kwargs['shell'])
        self.assertFalse(call.kwargs['start_new_session'])
        self.assertTrue(launcher.processes[-1]['console_group'])
        self.assertEqual(launcher.processes[-1]['pid'], self.child.pid)

    def test_no_console_fallback_stays_hidden_and_never_signals(self):
        call = self.spawn(False)
        self.assertEqual(call.kwargs['creationflags'], subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
        launcher.processes.pop(0)
        launcher._request_owned_stop(launcher.processes[-1])
        self.child.send_signal.assert_not_called()

    def test_each_native_owned_group_gets_targeted_break(self):
        for service in ('Ollama Server', 'LibreTranslate Server', 'Avatar Bridge Server'):
            self.owned['name'] = service
            launcher._request_owned_stop(self.owned)
        self.assertEqual(self.child.send_signal.call_count, 3)
        self.child.send_signal.assert_called_with(signal.CTRL_BREAK_EVENT)

    def test_unknown_same_pid_process_is_not_adopted(self):
        other = Child(self.child.pid)
        with patch.object(launcher.subprocess, 'run') as force:
            self.assertFalse(launcher._stop_owned_process(other))
        other.send_signal.assert_not_called()
        force.assert_not_called()

    def test_copied_entry_cannot_request_signal(self):
        launcher._request_owned_stop(dict(self.owned))
        self.child.send_signal.assert_not_called()

    def test_changed_pid_invalidates_ownership(self):
        self.child.pid += 1
        with patch.object(launcher.subprocess, 'run') as force:
            self.assertFalse(launcher._stop_owned_process(self.child))
        self.child.send_signal.assert_not_called()
        force.assert_not_called()

    def test_invalid_pid_never_broadcasts_or_forces(self):
        for pid in (0, -1, True, '43270', os.getpid()):
            with self.subTest(pid=pid), patch.object(launcher.subprocess, 'run') as force:
                self.child.pid = self.owned['pid'] = pid
                self.assertFalse(launcher._stop_owned_process(self.child))
                launcher._request_owned_stop(self.owned)
                force.assert_not_called()
        self.child.send_signal.assert_not_called()

    def test_unsupported_signal_reports_type_only(self):
        self.child.send_signal.side_effect = ValueError('private synthetic detail')
        output = io.StringIO()
        with redirect_stdout(output):
            launcher._request_owned_stop(self.owned)
        self.assertIn('ValueError', output.getvalue())
        self.assertNotIn('private synthetic detail', output.getvalue())

    def test_cooperative_exit_never_needs_force(self):
        self.child.send_signal.side_effect = lambda event: setattr(self.child, 'exited', True)
        with patch.object(launcher.subprocess, 'run') as force:
            self.assertTrue(launcher._stop_owned_process(self.child))
        force.assert_not_called()

    def test_missing_console_still_waits_full_grace_before_owned_force(self):
        self.owned['console_group'] = False
        clock = types.SimpleNamespace(now=0.0)
        def sleep(delay):
            clock.now += delay
        def force(args, **kwargs):
            self.assertGreaterEqual(clock.now, 15)
            self.assertLessEqual(kwargs['timeout'], 5)
            self.child.exited = True
        with patch.object(launcher, 'time', types.SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep)), \
             patch.object(launcher.subprocess, 'run', side_effect=force) as kill:
            self.assertTrue(launcher._stop_owned_process(self.child))
        self.child.send_signal.assert_not_called()
        kill.assert_called_once()

    def test_ownership_removed_during_grace_blocks_force(self):
        def sleep(delay):
            launcher.processes.clear()
        with patch.object(launcher, 'time', types.SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 16, 16]), sleep=sleep)), \
             patch.object(launcher.subprocess, 'run') as force:
            self.assertFalse(launcher._stop_owned_process(self.child))
        force.assert_not_called()

    def test_libre_wrapper_preserves_interpreter_and_vendor_arguments(self):
        call = self.spawn(True, 'LibreTranslate Server', ['isolated-python', '-m', 'libretranslate.main', '--port', '5000'])
        self.assertEqual(call.args[0], ['isolated-python', str(ROOT / 'MedlarTV/tools/python_service_entry.py'), 'libretranslate', '--port', '5000'])

    def test_avatar_wrapper_uses_exact_production_label(self):
        call = self.spawn(True, 'Avatar Bridge Server', ['venv-python', 'MedlarTV/avatar/bridge/server.py'])
        self.assertEqual(call.args[0][-1], 'avatar')
        self.assertEqual(call.args[0][0], 'venv-python')

    def test_unknown_targets_and_core_keep_original_commands(self):
        for name, command in [('LibreTranslate Server', ['python', '-m', 'other']),
                              ('Avatar Bridge Server', ['python', 'other.py']),
                              ('Medlar Core', ['python', 'MedlarTV/core/main.py']),
                              ('other', ['python', '-m', 'libretranslate.main'])]:
            self.assertEqual(launcher._python_service_command(name, command), command)

    def test_non_windows_spawn_keeps_original_entry(self):
        with patch.object(launcher.platform, 'system', return_value='Linux'):
            call = self.spawn(True, 'LibreTranslate Server', ['python', '-m', 'libretranslate.main'])
        self.assertEqual(call.args[0], ['python', '-m', 'libretranslate.main'])
        self.assertEqual(call.kwargs['creationflags'], 0)
        self.assertTrue(call.kwargs['start_new_session'])

    def test_gui_requests_hidden_private_console_without_terminal_shell(self):
        root = Path('C:/synthetic-medlar')
        window = types.SimpleNamespace(_service_root=lambda: root)
        with patch.object(medlar_app.subprocess, 'Popen', return_value=self.child) as popen, \
             patch.object(Path, 'mkdir'), patch.object(Path, 'open', mock_open()), \
             patch.object(Path, 'stat', side_effect=OSError):
            medlar_app.ControlCenter._spawn_launcher_mode(window, 'twitch')
        call = popen.call_args
        self.assertEqual(call.kwargs['creationflags'], subprocess.CREATE_NEW_CONSOLE)
        startup = call.kwargs['startupinfo']
        self.assertTrue(startup.dwFlags & subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(startup.wShowWindow, subprocess.SW_HIDE)
        self.assertFalse(call.kwargs['shell'])
        self.assertEqual(call.args[0], [str(root / '.venv/Scripts/python.exe'), str(root / 'launcher.py'), '--mode=twitch'])

    def test_gui_rejects_unowned_launcher_before_force(self):
        window = types.SimpleNamespace(launcher_proc=None, ollama_proc=None)
        with patch.object(medlar_app.subprocess, 'run') as force:
            with self.assertRaises(ValueError):
                medlar_app.ControlCenter._force_stop_owned_launcher(window, self.child)
        force.assert_not_called()


class PythonEntryTests(unittest.TestCase):
    def setUp(self):
        self.handlers, self.previous = [], object()
        self.signals = types.SimpleNamespace(SIGBREAK=21, getsignal=Mock(return_value=self.previous),
            signal=Mock(side_effect=lambda event, handler: self.handlers.append(handler)))
        self.vendor = types.SimpleNamespace(run_module=Mock(), run_path=Mock())
        for ctx in (patch.object(entry, 'signal', self.signals), patch.object(entry, 'runpy', self.vendor)):
            ctx.start()
            self.addCleanup(ctx.stop)
        self.argv_before, self.path_before = sys.argv, sys.path[:]

    def test_invalid_service_does_not_install_or_import(self):
        for args in ([], ['arbitrary.module']):
            self.assertEqual(entry.main(args), 2)
        self.signals.signal.assert_not_called()
        self.vendor.run_module.assert_not_called()
        self.vendor.run_path.assert_not_called()

    def test_libre_preserves_options_and_restores_state(self):
        def check(*args, **kwargs):
            self.assertEqual(sys.argv, ['libretranslate.main', '--port', '5001'])
            self.assertEqual(sys.path[0], str(Path.cwd()))
            self.assertTrue(callable(self.handlers[0]))
        self.vendor.run_module.side_effect = check
        self.assertEqual(entry.main(['libretranslate', '--port', '5001']), 0)
        self.vendor.run_module.assert_called_once_with('libretranslate.main', run_name='__main__', alter_sys=True)
        self.assertIs(self.handlers[-1], self.previous)
        self.assertIs(sys.argv, self.argv_before)
        self.assertEqual(sys.path, self.path_before)

    def test_avatar_preserves_script_path_and_options(self):
        target = ROOT / 'MedlarTV/avatar/bridge/server.py'
        def check(*args, **kwargs):
            self.assertEqual(sys.argv, [str(target), 'synthetic-option'])
            self.assertEqual(sys.path[:2], [str(ROOT), str(target.parent)])
        self.vendor.run_path.side_effect = check
        self.assertEqual(entry.main(['avatar', 'synthetic-option']), 0)
        self.vendor.run_path.assert_called_once_with(str(target), run_name='__main__')
        self.assertEqual(sys.path, self.path_before)

    def test_console_adapter_preserves_path_and_cleans_up_on_break(self):
        target = ROOT / 'MedlarTV/avatar_client/console_client.py'
        def check(*args, **kwargs):
            self.assertEqual(sys.argv, [str(target)])
            self.assertEqual(sys.path[:2], [str(ROOT), str(target.parent)])
            self.handlers[0](21, None)
        self.vendor.run_path.side_effect = check
        self.assertEqual(entry.main(['avatar-console']), 0)
        self.vendor.run_path.assert_called_once_with(str(target), run_name='__main__')
        self.assertIs(self.handlers[-1], self.previous)
        self.assertEqual(sys.path, self.path_before)

    def test_first_break_unwinds_and_repeated_break_leaves_cleanup_running(self):
        cleanup = Mock()
        def run(*args, **kwargs):
            try:
                self.handlers[0](21, None)
            finally:
                self.handlers[0](21, None)
                cleanup()
        self.vendor.run_path.side_effect = run
        self.assertEqual(entry.main(['avatar']), 0)
        cleanup.assert_called_once()
        self.assertIs(self.handlers[-1], self.previous)

    def test_vendor_error_restores_state_and_propagates(self):
        self.vendor.run_module.side_effect = RuntimeError('synthetic failure')
        with self.assertRaises(RuntimeError):
            entry.main(['libretranslate'])
        self.assertIs(self.handlers[-1], self.previous)
        self.assertIs(sys.argv, self.argv_before)
        self.assertEqual(sys.path, self.path_before)

    def test_vendor_system_exit_code_preserved(self):
        self.vendor.run_module.side_effect = SystemExit(7)
        with self.assertRaises(SystemExit) as result:
            entry.main(['libretranslate'])
        self.assertEqual(result.exception.code, 7)
        self.assertIs(self.handlers[-1], self.previous)

    def test_unsupported_handler_keeps_vendor_entry_available(self):
        self.signals.signal.side_effect = ValueError('private synthetic failure')
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(entry.main(['libretranslate']), 0)
        self.vendor.run_module.assert_called_once()
        self.assertIn('reason=ValueError', output.getvalue())
        self.assertNotIn('private synthetic failure', output.getvalue())

    def test_missing_sigbreak_keeps_vendor_entry_available(self):
        del self.signals.SIGBREAK
        self.assertEqual(entry.main(['avatar']), 0)
        self.vendor.run_path.assert_called_once()
        self.signals.signal.assert_not_called()

    def test_restore_error_reports_type_without_vendor_error_detail(self):
        def register(event, handler):
            if handler is self.previous:
                raise OSError('private restore detail')
            self.handlers.append(handler)
        self.signals.signal.side_effect = register
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(entry.main(['avatar']), 0)
        self.assertIn('reason=OSError', output.getvalue())
        self.assertNotIn('private restore detail', output.getvalue())

    def test_installed_waitress_dispatcher_cleanup_receives_interrupt(self):
        server = ROOT / '.libretranslate/.venv/Lib/site-packages/waitress/server.py'
        if not server.is_file():
            self.skipTest('Installed vendor source unavailable')
        vendor_run = source_function(server, 'run', {}, 'BaseWSGIServer')
        receiver = types.SimpleNamespace(asyncore=types.SimpleNamespace(loop=Mock(side_effect=lambda **kw: self.handlers[0](21, None))),
            adj=types.SimpleNamespace(asyncore_loop_timeout=1, asyncore_use_poll=False), _map={}, task_dispatcher=Mock())
        self.vendor.run_module.side_effect = lambda *a, **kw: vendor_run(receiver)
        self.assertEqual(entry.main(['libretranslate']), 0)
        receiver.task_dispatcher.shutdown.assert_called_once()

    def test_avatar_async_context_unwinds_without_event_loop(self):
        events = []
        class Context:
            async def __aenter__(self):
                events.append('entered')
            async def __aexit__(self, kind, value, traceback):
                events.append(kind)
        class Pending:
            def __await__(self):
                yield 'pending'
        serve = Mock(return_value=Context())
        logger = Mock()
        namespace = {'DEBUG': False, 'handler': Mock(), 'SERVER_SESSION': object(),
            '_install_cooperative_stop': lambda stop: lambda: None,
            'BootstrapError': RuntimeError, 'transport_logger': lambda: logger,
            'MAX_MESSAGE_SIZE': 16 * 1024, 'MAX_QUEUE': 16,
            'websockets': types.SimpleNamespace(serve=serve),
            'asyncio': types.SimpleNamespace(Future=Pending)}
        run_server = source_function(ROOT / 'MedlarTV/avatar/bridge/server.py', 'run_server', namespace)
        def vendor(*args, **kwargs):
            coroutine = run_server()
            try:
                self.assertEqual(coroutine.send(None), 'pending')
                try:
                    self.handlers[0](21, None)
                except KeyboardInterrupt as interrupt:
                    coroutine.throw(interrupt)
            finally:
                coroutine.close()
        self.vendor.run_path.side_effect = vendor
        self.assertEqual(entry.main(['avatar']), 0)
        self.assertEqual(events, ['entered', KeyboardInterrupt])
        serve.assert_called_once_with(namespace['handler'], '127.0.0.1', 8765,
                                      max_size=16 * 1024, max_queue=16, logger=logger,
                                      origins=[None], close_timeout=1)


if __name__ == '__main__':
    unittest.main()
