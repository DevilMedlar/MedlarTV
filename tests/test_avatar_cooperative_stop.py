"""Bridge stop and dependency cancellation regression, with no loops or sockets."""
import ast
import asyncio
import contextlib
import io
from pathlib import Path
import types
import unittest
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]


def finish(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coroutine.close()
    raise AssertionError('Unexpected suspended await in mock')


def definitions(namespace):
    source = ROOT / 'MedlarTV/avatar/bridge/server.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    selected = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name in ('_install_cooperative_stop', 'run_server')]
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace


class StopFuture:
    def __init__(self): self.completed, self.set_calls = False, 0
    def done(self): return self.completed
    def set_result(self, value):
        if self.completed: raise AssertionError('Repeated completion')
        self.completed = True
        self.set_calls += 1
    def __await__(self):
        while not self.completed: yield 'waiting-for-stop'


class StopTests(unittest.TestCase):
    def setUp(self):
        self.signals = types.SimpleNamespace(SIGBREAK=21, getsignal=Mock(return_value='adapter-handler'), signal=Mock())
        self.loop = Mock()
        self.stop = StopFuture()
        self.events = []
        test = self
        class Context:
            async def __aenter__(self): test.events.append('started')
            async def __aexit__(self, kind, value, traceback): test.events.append(('closed', kind))
        self.serve = Mock(return_value=Context())
        self.ns = definitions({'signal': self.signals,
            'asyncio': types.SimpleNamespace(get_running_loop=lambda: self.loop, Future=lambda: self.stop),
            'SERVER_SESSION': object(), 'BootstrapError': ValueError, 'handler': Mock(),
            'websockets': types.SimpleNamespace(serve=self.serve), 'MAX_MESSAGE_SIZE': 16384,
            'MAX_QUEUE': 16, 'transport_logger': lambda: 'disabled-transport-logger'})

    def request_handler(self): return self.signals.signal.call_args_list[0].args[1]

    def test_native_break_completes_wait_before_runner_global_cancellation(self):
        coroutine = self.ns['run_server']()
        self.assertEqual(coroutine.send(None), 'waiting-for-stop')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.request_handler()(21, None)  # Does not raise KeyboardInterrupt.
            self.request_handler()(21, None)
        self.loop.call_soon_threadsafe.assert_called_once()
        self.assertEqual(self.events, ['started'])
        self.assertFalse(self.stop.done())
        self.loop.call_soon_threadsafe.call_args.args[0]()
        with self.assertRaises(StopIteration): coroutine.send(None)
        self.assertEqual(self.events, ['started', ('closed', None)])
        self.assertEqual(self.stop.set_calls, 1)
        self.assertEqual(output.getvalue().count('Cooperative stop requested'), 1)
        self.signals.signal.assert_called_with(21, 'adapter-handler')
        self.serve.assert_called_once_with(self.ns['handler'], '127.0.0.1', 8765,
            max_size=16384, max_queue=16, logger='disabled-transport-logger', origins=[None], close_timeout=1)

    def test_late_scheduled_stop_on_completed_future_is_harmless(self):
        restore = self.ns['_install_cooperative_stop'](self.stop)
        with contextlib.redirect_stdout(io.StringIO()): self.request_handler()(21, None)
        self.stop.set_result(None)
        self.loop.call_soon_threadsafe.call_args.args[0]()
        self.assertEqual(self.stop.set_calls, 1)
        restore()

    def test_signal_before_bind_is_retained_and_cleanup_restores_adapter(self):
        def startup(*args, **kwargs):
            with contextlib.redirect_stdout(io.StringIO()): self.request_handler()(21, None)
            self.loop.call_soon_threadsafe.call_args.args[0]()
            return types.SimpleNamespace()  # Fail startup after the scheduled stop.
        self.serve.side_effect = startup
        with self.assertRaises(TypeError): finish(self.ns['run_server']())
        self.assertTrue(self.stop.done())
        self.signals.signal.assert_called_with(21, 'adapter-handler')

    def test_cancelled_main_wait_unwinds_context_and_restores_adapter(self):
        coroutine = self.ns['run_server']()
        self.assertEqual(coroutine.send(None), 'waiting-for-stop')
        with self.assertRaises(asyncio.CancelledError): coroutine.throw(asyncio.CancelledError())
        coroutine.close()
        self.assertEqual(self.events, ['started', ('closed', asyncio.CancelledError)])
        self.signals.signal.assert_called_with(21, 'adapter-handler')

    def test_failed_bind_restores_adapter_without_swallowing_denial(self):
        self.serve.side_effect = PermissionError('fake fixture denial')
        with self.assertRaises(PermissionError): finish(self.ns['run_server']())
        self.signals.signal.assert_called_with(21, 'adapter-handler')

    def test_handler_setup_and_restore_errors_are_secret_free(self):
        for operation in ('getsignal', 'signal'):
            with self.subTest(operation=operation):
                self.signals.getsignal.reset_mock(side_effect=True)
                self.signals.getsignal.return_value = 'adapter-handler'
                self.signals.signal.reset_mock(side_effect=True)
                getattr(self.signals, operation).side_effect = ValueError('fake-secret-canary')
                output = io.StringIO()
                with contextlib.redirect_stdout(output): self.ns['_install_cooperative_stop'](self.stop)()
                self.assertIn('ValueError', output.getvalue())
                self.assertNotIn('fake-secret-canary', output.getvalue())
        self.signals.signal.reset_mock(side_effect=True)
        self.signals.getsignal.reset_mock(side_effect=True)
        restore = self.ns['_install_cooperative_stop'](self.stop)
        self.signals.signal.side_effect = OSError('fake-secret-canary')
        with contextlib.redirect_stdout(io.StringIO()) as output: restore()
        self.assertNotIn('fake-secret-canary', output.getvalue())

    def test_platform_without_break_keeps_existing_behavior(self):
        self.ns['signal'] = types.SimpleNamespace()
        self.ns['_install_cooperative_stop'](self.stop)()
        self.loop.call_soon_threadsafe.assert_not_called()

    def test_no_auth_session_never_installs_stop_handler_or_binds(self):
        self.ns['SERVER_SESSION'] = None
        with self.assertRaises(ValueError): finish(self.ns['run_server']())
        self.signals.signal.assert_not_called()
        self.serve.assert_not_called()


class DependencyCancellationTests(unittest.TestCase):
    def test_installed_handler_cancellation_can_leave_untracked_open_transport(self):
        source = ROOT / '.venv/Lib/site-packages/websockets/asyncio/server.py'
        if not source.exists(): self.skipTest('Installed dependency source unavailable')
        tree = ast.parse(source.read_text(encoding='utf-8'))
        factory = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'serve')
        protocol = next(n for n in factory.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'protocol_handler')
        class Timeout:
            async def __aenter__(self): pass
            async def __aexit__(self, *args): pass
        peer = Mock(handshake=AsyncMock(), protocol=types.SimpleNamespace(state='OPEN'),
             transport=types.SimpleNamespace(abort=Mock()), close=AsyncMock(), start_keepalive=Mock())
        server = types.SimpleNamespace(all_connections=set(), handler_tasks={'fixture-task'})
        ns = {'ServerConnection': object, 'asyncio': types.SimpleNamespace(timeout=lambda n: Timeout(),
              current_task=lambda: 'fixture-task'), 'open_timeout': 10, 'process_request': None,
              'process_response': None, 'server_header': None, 'OPEN': 'OPEN', 'server': server,
              'handler': AsyncMock(side_effect=asyncio.CancelledError())}
        exec(compile(ast.Module(body=[protocol], type_ignores=[]), str(source), 'exec'), ns)
        with self.assertRaises(asyncio.CancelledError): finish(ns['protocol_handler'](peer))
        self.assertFalse(server.all_connections)
        self.assertFalse(server.handler_tasks)
        peer.close.assert_not_awaited()
        peer.transport.abort.assert_not_called()
        # Execute the installed close method against this state: it has no tracked
        # peer to close, then suspends waiting for the still-open TCP transport.
        owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Server')
        close = next(n for n in owner.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_close')
        class Waiting:
            def __await__(self): yield 'untracked-transport-still-open'
        async def wait_closed(): await Waiting()
        underlying = types.SimpleNamespace(close=Mock(), wait_closed=wait_closed)
        server.server, server.logger, server.handlers_waiter = underlying, Mock(), Mock()
        ns.update({'CloseCode': types.SimpleNamespace(GOING_AWAY=1001)})
        exec(compile(ast.Module(body=[close], type_ignores=[]), str(source), 'exec'), ns)
        coroutine = ns['_close'](server)
        try: self.assertEqual(coroutine.send(None), 'untracked-transport-still-open')
        finally: coroutine.close()
        underlying.close.assert_called_once()
        server.handlers_waiter.set_result.assert_not_called()


if __name__ == '__main__': unittest.main()
