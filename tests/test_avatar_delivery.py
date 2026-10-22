"""Avatar delivery contracts, using AST extraction and entirely in-memory mocks."""
import ast
import contextlib
import io
import json
from pathlib import Path
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch
import asyncio
from MedlarTV.avatar.bridge import auth


ROOT = Path(__file__).resolve().parents[1]


def definition(relative, name, namespace):
    tree = ast.parse((ROOT / relative).read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), relative, 'exec'), namespace)
    return namespace[name]


def finish(coroutine):
    """Only mocked, immediately completing awaits are permitted; no event loop."""
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coroutine.close()
    raise AssertionError('Unexpected blocking await in offline test')


COLORS = types.SimpleNamespace(green='', yellow='', reset='')


class PortSelectionTests(unittest.TestCase):
    def select(self, free):
        probes = []
        def factory(*args):
            probe = Mock()
            def bind(address):
                if address[1] not in free:
                    raise OSError('synthetic occupied port')
            probe.bind.side_effect = bind
            probes.append(probe)
            return probe
        sockets = types.SimpleNamespace(socket=factory, AF_INET=2, SOCK_STREAM=1, SOL_SOCKET=1, SO_REUSEADDR=4)
        fn = definition('launcher.py', '_find_free_bridge_port', {'socket': sockets})
        return fn('0.0.0.0'), probes

    def test_first_free_port(self):
        port, probes = self.select({8765, 8768})
        self.assertEqual(port, 8765)
        self.assertEqual(len(probes), 1)

    def test_skips_occupied_ports(self):
        port, probes = self.select({8768})
        self.assertEqual(port, 8768)
        self.assertEqual([p.bind.call_args.args[0] for p in probes], [('0.0.0.0', p) for p in range(8765, 8769)])

    def test_last_port_is_included(self):
        port, probes = self.select({8775})
        self.assertEqual((port, len(probes)), (8775, 11))

    def test_exhaustion_returns_none(self):
        port, probes = self.select(set())
        self.assertIsNone(port)
        self.assertEqual(len(probes), 11)
        for probe in probes:
            probe.close.assert_called_once()

    def test_outside_range_is_not_used(self):
        port, probes = self.select({8776})
        self.assertIsNone(port)
        self.assertEqual(len(probes), 11)

    def test_probe_closes_and_keeps_socket_settings(self):
        _, probes = self.select({8765})
        probes[0].setsockopt.assert_called_once_with(1, 4, 1)
        probes[0].close.assert_called_once()


class BridgeStartupTests(unittest.TestCase):
    def startup(self, port, started=True):
        env = {'BRIDGE_URL': 'ws://stale:1', 'BRIDGE_PORT': '1'}
        finder, spawn = Mock(return_value=port), Mock(return_value=started)
        fn = definition('launcher.py', 'start_avatar_bridge', {
            'os': types.SimpleNamespace(environ=env, getenv=env.get), 'Colors': COLORS, 'DEBUG': False,
            'VENV_PYTHON': Path('isolated-python'), '_find_free_bridge_port': finder,
            'start_process': spawn,
        })
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = fn()
        return result, env, finder, spawn, output.getvalue()

    def test_selected_url_and_port(self):
        for port in (8765, 8768, 8775):
            with self.subTest(port=port):
                result, env, finder, _, _ = self.startup(port)
                self.assertTrue(result)
                self.assertEqual(env['BRIDGE_URL'], f'ws://127.0.0.1:{port}')
                self.assertEqual(env['BRIDGE_PORT'], str(port))
                self.assertEqual(env['BRIDGE_HOST'], '127.0.0.1')
                finder.assert_called_once_with('127.0.0.1')

    def test_owned_launch_contract_is_preserved(self):
        _, _, _, spawn, _ = self.startup(8768)
        spawn.assert_called_once_with('Avatar Bridge Server', ['isolated-python', 'MedlarTV/avatar/bridge/server.py'],
                                      use_venv=True, wait=2, required=False)

    def test_exhausted_range_never_spawns(self):
        result, env, _, spawn, output = self.startup(None)
        self.assertFalse(result)
        spawn.assert_not_called()
        self.assertEqual((env['BRIDGE_PORT'], env['BRIDGE_URL']), ('', ''))
        self.assertIn('no free port in 8765-8775', output)

    def test_failed_child_disables_endpoint(self):
        result, env, _, spawn, output = self.startup(8768, False)
        self.assertFalse(result)
        spawn.assert_called_once()
        self.assertEqual((env['BRIDGE_PORT'], env['BRIDGE_URL']), ('', ''))
        self.assertIn('failed to start', output)


class LauncherFlowTests(unittest.TestCase):
    def flow(self, bridge=True, console=True, mode='twitch', stop_after_bridge=False, core=True):
        env = {'ENABLE_AVATAR_CONSOLE': '1' if console else '0'}
        stops, calls = {'stop': False}, []
        def start_bridge():
            env['BRIDGE_URL'] = 'ws://127.0.0.1:8768' if bridge else ''
            stops['stop'] = stop_after_bridge
            return bridge
        def spawn(name, command, **kwargs):
            calls.append((name, dict(env), kwargs))
            return core if name == 'Medlar Core' else True
        namespace = {
            'os': types.SimpleNamespace(environ=env, getenv=env.get), 'Colors': COLORS,
            'StartupError': RuntimeError, 'VENV_PYTHON': types.SimpleNamespace(exists=lambda: True),
            '_stop_requested': lambda: stops['stop'], 'clear_logs': Mock(), '_load_env_file': Mock(),
            '_load_saved_connections': Mock(), 'clear_old_sessions': Mock(), 'banner': Mock(),
            'ollama_api_ready': Mock(return_value=True), 'start_ollama': Mock(return_value=True),
            '_validate_owned_ollama_endpoint': Mock(), '_monitor_processes': Mock(),
            'libretranslate_home': Mock(return_value='synthetic-translator'), 'start_libretranslate': Mock(),
            'start_avatar_bridge': Mock(side_effect=start_bridge), 'start_process': Mock(side_effect=spawn),
        }
        fn = definition('launcher.py', '_run_services', namespace)
        with contextlib.redirect_stdout(io.StringIO()):
            fn(mode)
        return calls, namespace

    def test_bridge_failure_preserves_core_and_skips_console(self):
        calls, namespace = self.flow(bridge=False)
        self.assertEqual([c[0] for c in calls], ['Medlar Core'])
        self.assertEqual(calls[0][1]['BRIDGE_URL'], '')
        self.assertTrue(calls[0][2]['required'])
        namespace['_monitor_processes'].assert_called_once()

    def test_bridge_url_reaches_core_and_console(self):
        calls, _ = self.flow()
        self.assertEqual([c[0] for c in calls], ['Medlar Core', 'Avatar Console Client'])
        self.assertTrue(all(c[1]['BRIDGE_URL'] == 'ws://127.0.0.1:8768' for c in calls))

    def test_console_is_still_opt_in(self):
        calls, _ = self.flow(console=False)
        self.assertEqual([c[0] for c in calls], ['Medlar Core'])

    def test_all_mode_preserves_optional_failure(self):
        calls, namespace = self.flow(bridge=False, mode='all')
        self.assertEqual([c[0] for c in calls], ['Medlar Core'])
        namespace['start_ollama'].assert_called_once()

    def test_ollama_mode_never_starts_avatar(self):
        calls, namespace = self.flow(mode='ollama')
        self.assertEqual(calls, [])
        namespace['start_avatar_bridge'].assert_not_called()

    def test_stop_after_bridge_prevents_core_launch(self):
        calls, namespace = self.flow(stop_after_bridge=True)
        self.assertEqual(calls, [])
        namespace['_monitor_processes'].assert_not_called()

    def test_core_failure_remains_required(self):
        with self.assertRaisesRegex(RuntimeError, 'Medlar Core failed to start'):
            self.flow(core=False)


class ConsoleEndpointTests(unittest.TestCase):
    def listen(self, url):
        bootstrap = auth.parse_bootstrap({'version': 1, 'role': 'subscriber', 'channel': '#example',
                                         'url': url, 'key': 'b' * 64}, 'subscriber')
        peer = types.SimpleNamespace(send=AsyncMock())
        class Context:
            async def __aenter__(self): return peer
            async def __aexit__(self, *args): return False
        async def wait_for(coro, timeout): return await coro
        connect = Mock(return_value=Context())
        fn = definition('MedlarTV/avatar_client/console_client.py', 'listen', {
            'read_bootstrap': Mock(return_value=bootstrap), 'BootstrapError': auth.BootstrapError,
            'transport_logger': auth.transport_logger, 'IO_TIMEOUT': 2.0,
            'authenticate_client': AsyncMock(side_effect=asyncio.CancelledError()),
            'asyncio': asyncio, 'json': json,
        })
        with patch.object(asyncio, 'wait_for', wait_for), self.assertRaises(asyncio.CancelledError):
            finish(fn(connector=connect))
        return connect, peer

    def test_selected_launcher_url(self):
        connect, peer = self.listen('ws://127.0.0.1:8768')
        self.assertEqual(connect.call_args.args, ('ws://127.0.0.1:8768',))
        self.assertIsNone(connect.call_args.kwargs['proxy'])
        peer.send.assert_not_awaited()

    def test_default_selected_loopback_port(self):
        connect, _ = self.listen('ws://127.0.0.1:8765')
        self.assertEqual(connect.call_args.args, ('ws://127.0.0.1:8765',))

    def test_remote_bootstrap_fails_closed(self):
        with self.assertRaises(auth.BootstrapError): self.listen('ws://example.invalid:9000')

    def test_unmanaged_client_requires_pipe_bootstrap(self):
        read = Mock(side_effect=auth.BootstrapError('Authenticated Avatar startup required'))
        fn = definition('MedlarTV/avatar_client/console_client.py', 'listen', {'read_bootstrap': read})
        connector = Mock()
        with self.assertRaises(auth.BootstrapError): finish(fn(connector=connector))
        connector.assert_not_called()
        read.assert_called_once_with('subscriber', required=True)


class StaticAuditTests(unittest.TestCase):
    def test_audit_recognizes_environment_selected_console_url(self):
        namespace = {
            'Path': Path, 'ast': ast, 'REPO_ROOT': ROOT, 'READY': 'READY', 'UNAVAILABLE': 'UNAVAILABLE',
            'SKIPPED': 'SKIPPED', 'ERROR': 'ERROR',
            'Check': lambda category, name, status, detail: types.SimpleNamespace(name=name, status=status),
        }
        definition('tests/medlar_diagnostics.py', '_source_definitions', namespace)
        check = definition('tests/medlar_diagnostics.py', 'check_component_contracts', namespace)
        rows = {row.name: row.status for row in check()}
        self.assertEqual(rows['Avatar console dynamic-port compatibility'], 'READY')


if __name__ == '__main__':
    unittest.main()
