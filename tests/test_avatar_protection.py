"""Approved Avatar limits and cleanup, without network, processes or event loops."""
from __future__ import annotations
from collections.abc import Awaitable, Callable, Coroutine, Generator, Iterable
from typing import TYPE_CHECKING, TypeVar, cast
if TYPE_CHECKING:
    from avatar_fixture_types import AvatarServerNamespace

_T = TypeVar("_T")

import ast
import asyncio
import json
from pathlib import Path
import types
import unicodedata
import unittest
from unittest.mock import AsyncMock, Mock
from MedlarTV.avatar.bridge import auth


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'MedlarTV/avatar/bridge/server.py'


def finish(coroutine: Coroutine[object, object, _T]) -> _T:
    try:
        coroutine.send(None)
    except StopIteration as result:
        return cast(_T, result.value)
    finally:
        coroutine.close()
    raise AssertionError('Unexpected blocking await in an offline mock')


class FakeAsyncio:
    Future: Callable[[], Awaitable[object]]
    def __init__(self) -> None:
        self.waits: list[float] = []
        self.sleeps: list[float] = []
        self.gathers: list[tuple[int, bool]] = []
        self.failure: BaseException | None = None
        self.on_sleep: Callable[[], object] = lambda: None

    async def wait_for(self, coroutine: Coroutine[object, object, _T], timeout: float) -> _T:
        self.waits.append(timeout)
        if self.failure is not None:
            coroutine.close()
            raise self.failure
        return await coroutine

    async def gather(self, *coroutines: Awaitable[object], return_exceptions: bool = False) -> list[object]:
        self.gathers.append((len(coroutines), return_exceptions))
        results: list[object] = []
        for coroutine in coroutines:
            try:
                results.append(await coroutine)
            except Exception as exc:
                if not return_exceptions:
                    raise
                results.append(exc)
        return results

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.on_sleep()


def load_server() -> tuple[AvatarServerNamespace[Peer], FakeAsyncio]:
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    nodes: list[ast.stmt] = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and
                 (t.id.startswith('MAX_') or t.id == 'SEND_TIMEOUT') for t in n.targets))]
    fake = FakeAsyncio()
    namespace: dict[str, object] = {'DEBUG': False, 'ACTIVE_CONNECTIONS': set(), 'CHANNEL_REGISTRY': {},
                 'json': json, 'unicodedata': unicodedata, 'asyncio': fake,
                 'CONNECTION_ROLES': {}, 'LATEST_MOOD': None,
                 'SERVER_SESSION': auth.parse_bootstrap({'version': 1, 'role': 'server',
                     'channel': '#example', 'url': 'ws://127.0.0.1:8768',
                     'publisher_key': 'a' * 64, 'subscriber_key': 'b' * 64}, 'server'),
                 'AUTH_TIMEOUT': 3.0, 'credential_role': auth.credential_role,
                 'BootstrapError': auth.BootstrapError, 'transport_logger': auth.transport_logger}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(SOURCE), 'exec'), namespace)
    # AST selection loads these real functions; initial globals supply their state.
    return cast("AvatarServerNamespace[Peer]", namespace), fake


class Peer:
    def __init__(self, namespace: AvatarServerNamespace[Peer], frames: Iterable[object] = (), send_error: BaseException | None = None, close_error: BaseException | None = None, key: str = 'b' * 64) -> None:
        self.namespace, self.frames = namespace, iter(frames)
        self.send = AsyncMock(side_effect=send_error)
        self.close = AsyncMock(side_effect=close_error)
        self.snapshots: list[dict[str, list[Peer]]] = []
        self.recv = AsyncMock(return_value=json.dumps({'event': 'authenticate', 'token': key}))

    def __aiter__(self) -> Peer:
        return self

    async def __anext__(self) -> str | bytes:
        self.snapshots.append({key: list(peers) for key, peers in self.namespace['CHANNEL_REGISTRY'].items()})
        try:
            value = next(self.frames)
        except StopIteration:
            raise StopAsyncIteration
        if isinstance(value, BaseException):
            raise value
        return value if isinstance(value, (str, bytes)) else json.dumps(value)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.ns, self.fake = load_server()
        self.forward = AsyncMock()
        self.ns['broadcast'] = self.forward

    def handle(self, frames: Iterable[object]) -> Peer:
        # Fixture JSON keys are text; values deliberately include malformed cases.
        normalized_frames: list[object] = [dict(cast(dict[str, object], frame), channel='#example') if isinstance(frame, dict) and cast(dict[object, object], frame).get('event') == 'mood_update' and 'channel' not in frame else frame for frame in frames]
        peer = Peer(self.ns, normalized_frames, key='a' * 64)
        finish(self.ns['handler'](peer))
        self.assertFalse(self.ns['ACTIVE_CONNECTIONS'])
        self.assertFalse(self.ns['CHANNEL_REGISTRY'])
        return peer

    def test_non_object_json_is_ignored_and_next_message_survives(self):
        self.handle(['[]', 'null', '1', 'true', '"text"', {'event': 'mood_update', 'mood': 'chill'}])
        self.forward.assert_awaited_once_with({'event': 'mood_update', 'mood': 'chill'}, '#example')

    def test_invalid_json_binary_and_deep_json_are_ignored(self):
        self.handle(['{', b'\xff', '[' * 1100 + ']' * 1100, {'event': 'mood_update', 'mood': 'hype'}])
        self.forward.assert_awaited_once()

    def test_mood_length_boundaries(self):
        self.handle([{'event': 'mood_update', 'mood': m} for m in ('', 'a', 'a' * 64, 'a' * 65)])
        self.assertEqual([call.args[0]['mood'] for call in self.forward.await_args_list], ['a', 'a' * 64])

    def test_invalid_mood_types_and_controls_are_ignored(self):
        moods: tuple[object, ...] = (None, 0, True, [], {}, 'a\n', '\x1b[31m', '\x7f', '\u0085', '\ud800')
        self.handle([{'event': 'mood_update', 'mood': mood} for mood in moods])
        self.forward.assert_not_awaited()

    def test_printable_unicode_and_joined_emoji_are_preserved(self):
        mood = 'calm café 👩\u200d💻'
        self.handle([{'event': 'mood_update', 'mood': mood}])
        self.forward.assert_awaited_once_with({'event': 'mood_update', 'mood': mood}, '#example')

    def test_missing_mood_channel_cannot_broadcast_globally(self):
        self.handle([{'event': 'mood_update', 'mood': 'chill', 'channel': channel} for channel in (None, '')])
        self.forward.assert_not_awaited()

    def test_mood_channel_boundaries_and_types(self):
        channels: tuple[object, ...] = ('x' * 101, [], {}, 2, False, 'a\r', '\x1b')
        self.handle([{'event': 'mood_update', 'mood': 'chill', 'channel': c} for c in channels])
        self.forward.assert_not_awaited()
        self.handle([{'event': 'mood_update', 'mood': 'chill', 'channel': '#EXAMPLE'}])
        self.forward.assert_awaited_once_with({'event': 'mood_update', 'mood': 'chill'}, '#example')

    def test_unknown_events_and_event_types_are_ignored(self):
        self.handle([{}, {'event': 'unknown'}, {'event': []}, {'event': {}}])
        self.forward.assert_not_awaited()

    def test_register_channel_boundaries_and_types(self):
        values: tuple[object, ...] = ('', 'a' * 101, None, [], {}, False, 'x\n', '\x7f', '\ud800', '#EXAMPLE')
        peer = Peer(self.ns, [{'event': 'register', 'channel': c} for c in values])
        finish(self.ns['handler'](peer))
        self.assertEqual(list(peer.snapshots[-1]), ['#example'])
        self.assertFalse(self.ns['CHANNEL_REGISTRY'])

    def test_invalid_unregister_cannot_remove_valid_membership(self):
        peer = Peer(self.ns, [{'event': 'register', 'channel': '#example'},
                            {'event': 'unregister', 'channel': []},
                            {'event': 'unregister', 'channel': '#example\n'}])
        finish(self.ns['handler'](peer))
        self.assertIn('#example', peer.snapshots[-1])


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.ns, self.fake = load_server()

    def test_duplicate_registration_is_case_normalized_and_repeat_safe(self):
        peer = Peer(self.ns, [{'event': 'register', 'channel': c} for c in ('#Example', '#EXAMPLE', '#example')])
        finish(self.ns['handler'](peer))
        self.assertEqual(peer.snapshots[-1], {'#example': [peer]})
        self.assertFalse(self.ns['CHANNEL_REGISTRY'])

    def test_32_subscription_defense_remains_33rd_rejected(self):
        # Authenticated handlers allow only the primary channel; retain helper defense.
        peer = Peer(self.ns)
        for i in range(32): self.assertTrue(self.ns['_subscribe'](peer, f'channel{i}'))
        self.assertFalse(self.ns['_subscribe'](peer, 'channel32'))
        self.assertEqual(len(self.ns['CHANNEL_REGISTRY']), 32)

    def test_repeat_at_limit_does_not_use_a_slot(self):
        peer = Peer(self.ns)
        for i in range(32): self.ns['_subscribe'](peer, f'channel{i}')
        self.assertTrue(self.ns['_subscribe'](peer, 'channel0'))
        self.assertEqual(len(self.ns['CHANNEL_REGISTRY']), 32)
        self.assertEqual(self.ns['CHANNEL_REGISTRY']['channel0'], [peer])

    def test_unsubscribe_releases_capacity(self):
        peer = Peer(self.ns)
        for i in range(32): self.ns['_subscribe'](peer, f'channel{i}')
        self.ns['_remove_subscription'](peer, 'channel0')
        self.assertTrue(self.ns['_subscribe'](peer, 'replacement'))
        self.assertEqual(len(self.ns['CHANNEL_REGISTRY']), 32)

    def test_cleanup_removes_all_stale_duplicates_even_when_not_active(self):
        peer, other = Peer(self.ns), Peer(self.ns)
        self.ns['ACTIVE_CONNECTIONS'].add(other)
        self.ns['CHANNEL_REGISTRY'].update({'one': [peer, peer, other], 'two': [peer, peer]})
        finish(self.ns['unregister'](peer))
        finish(self.ns['unregister'](peer))
        self.assertEqual(self.ns['CHANNEL_REGISTRY'], {'one': [other]})
        self.assertEqual(self.ns['ACTIVE_CONNECTIONS'], {other})

    def test_handshake_failure_cleans_connection_and_memberships(self):
        peer = Peer(self.ns, send_error=RuntimeError('synthetic handshake failure'))
        self.ns['CHANNEL_REGISTRY']['stale'] = [peer, peer]
        with self.assertRaises(RuntimeError):
            finish(self.ns['handler'](peer))
        self.assertFalse(self.ns['ACTIVE_CONNECTIONS'])
        self.assertFalse(self.ns['CHANNEL_REGISTRY'])

    def test_cancelled_handshake_cleans_up(self):
        peer = Peer(self.ns, send_error=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            finish(self.ns['handler'](peer))
        self.assertFalse(self.ns['ACTIVE_CONNECTIONS'])

    def test_receive_failure_cleans_memberships(self):
        peer = Peer(self.ns, [{'event': 'register', 'channel': '#example'}, RuntimeError('synthetic disconnect')])
        with self.assertRaises(RuntimeError):
            finish(self.ns['handler'](peer))
        self.assertFalse(self.ns['ACTIVE_CONNECTIONS'])
        self.assertFalse(self.ns['CHANNEL_REGISTRY'])

    def test_64_connections_accepted_and_65th_rejected(self):
        peers = [Peer(self.ns) for _ in range(65)]
        for peer in peers[:64]:
            self.assertTrue(finish(self.ns['register'](peer)))
        self.assertFalse(finish(self.ns['register'](peers[64])))
        self.assertEqual(len(self.ns['ACTIVE_CONNECTIONS']), 64)
        peers[64].send.assert_not_awaited()
        peers[64].close.assert_awaited_once_with(code=1013, reason='Avatar Bridge connection limit reached')

    def test_existing_connection_at_limit_does_not_consume_another_slot(self):
        peers = [Peer(self.ns) for _ in range(64)]
        self.ns['ACTIVE_CONNECTIONS'].update(peers)
        self.assertTrue(finish(self.ns['register'](peers[0])))
        self.assertEqual(len(self.ns['ACTIVE_CONNECTIONS']), 64)
        peers[0].close.assert_not_awaited()

    def test_disconnected_connection_releases_slot(self):
        peers = [Peer(self.ns) for _ in range(64)]
        self.ns['ACTIVE_CONNECTIONS'].update(peers)
        finish(self.ns['unregister'](peers[0]))
        replacement = Peer(self.ns)
        self.assertTrue(finish(self.ns['register'](replacement)))
        self.assertEqual(len(self.ns['ACTIVE_CONNECTIONS']), 64)

    def test_rejected_connection_close_failure_preserves_existing_connections(self):
        peers = [Peer(self.ns) for _ in range(64)]
        self.ns['ACTIVE_CONNECTIONS'].update(peers)
        rejected = Peer(self.ns, close_error=RuntimeError('synthetic close failure'))
        with self.assertRaises(RuntimeError):
            finish(self.ns['handler'](rejected))
        self.assertEqual(self.ns['ACTIVE_CONNECTIONS'], set(peers))


class BroadcastTests(unittest.TestCase):
    def setUp(self):
        self.ns, self.fake = load_server()

    def test_each_recipient_has_two_second_deadline(self):
        peers = [Peer(self.ns) for _ in range(3)]
        self.ns['ACTIVE_CONNECTIONS'].update(peers)
        self.ns['CONNECTION_ROLES'].update({p: 'subscriber' for p in peers})
        finish(self.ns['broadcast']({'event': 'mood_update', 'mood': 'chill'}))
        self.assertEqual(self.fake.waits, [2.0] * 3)
        self.assertEqual(self.fake.gathers, [(3, True)])
        for peer in peers:
            self.assertEqual(json.loads(peer.send.await_args_list[-1].args[0])['mood'], 'chill')

    def test_failed_recipient_does_not_prevent_other_deliveries(self):
        broken, healthy = Peer(self.ns, send_error=RuntimeError('synthetic failure')), Peer(self.ns)
        self.ns['ACTIVE_CONNECTIONS'].update((broken, healthy))
        self.ns['CONNECTION_ROLES'].update({broken: 'subscriber', healthy: 'subscriber'})
        finish(self.ns['broadcast']({'event': 'mood_update', 'mood': 'hype'}))
        broken.send.assert_awaited_once()
        healthy.send.assert_awaited_once()

    def test_timeout_is_contained(self):
        peer = Peer(self.ns)
        self.fake.failure = TimeoutError('synthetic deadline')
        self.assertFalse(finish(self.ns['_send_target'](peer, '{}')))
        self.assertEqual(self.fake.waits, [2.0])

    def test_shutdown_cancellation_is_not_swallowed(self):
        self.fake.failure = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            finish(self.ns['_send_target'](Peer(self.ns), '{}'))

    def test_channel_broadcast_deduplicates_and_selects_only_that_channel(self):
        target, other = Peer(self.ns), Peer(self.ns)
        self.ns['ACTIVE_CONNECTIONS'].update((target, other))
        self.ns['CONNECTION_ROLES'].update({target: 'subscriber', other: 'subscriber'})
        self.ns['CHANNEL_REGISTRY']['example'] = [target, target]
        finish(self.ns['broadcast']({'event': 'mood_update', 'mood': 'chill'}, 'EXAMPLE'))
        target.send.assert_awaited_once()
        other.send.assert_not_awaited()

    def test_no_recipient_broadcast_returns_immediately(self):
        finish(self.ns['broadcast']({'event': 'mood_update', 'mood': 'chill'}))
        self.assertFalse(self.fake.sleeps)
        self.assertFalse(self.fake.waits)

    def test_late_subscriber_gets_cached_current_mood(self):
        self.ns['LATEST_MOOD'] = {'event': 'mood_update', 'mood': 'chill'}
        peer = Peer(self.ns, [{'event': 'register', 'channel': '#example'}])
        finish(self.ns['handler'](peer))
        self.assertEqual(json.loads(peer.send.await_args_list[-1].args[0]), self.ns['LATEST_MOOD'])


class ServerConfigurationTests(unittest.TestCase):
    def test_run_server_defaults_and_explicit_override(self):
        for host in (None, '0.0.0.0', '192.0.2.10'):
            with self.subTest(host=host):
                ns, fake = load_server()
                exits: list[type[BaseException] | None] = []
                class Context:
                    async def __aenter__(self) -> None: pass
                    async def __aexit__(self, kind: type[BaseException] | None, value: BaseException | None, traceback: types.TracebackType | None) -> None: exits.append(kind)
                class Pending:
                    def __await__(self) -> Generator[str, None, None]: yield 'pending'
                serve = Mock(return_value=Context())
                ns['websockets'] = types.SimpleNamespace(serve=serve)
                fake.Future = Pending
                install_stop: Callable[[object], Callable[[], None]] = lambda stop: lambda: None
                ns['_install_cooperative_stop'] = install_stop
                coroutine = ns['run_server']() if host is None else ns['run_server'](host, 8770)
                try:
                    self.assertEqual(coroutine.send(None), 'pending')
                    with self.assertRaises(KeyboardInterrupt):
                        coroutine.throw(KeyboardInterrupt())
                finally:
                    coroutine.close()
                serve.assert_called_once_with(ns['handler'], host or '127.0.0.1', 8765 if host is None else 8770,
                                              max_size=16 * 1024, max_queue=16,
                                              logger=auth.transport_logger(), origins=[None], close_timeout=1)
                self.assertEqual(exits, [KeyboardInterrupt])

    def test_direct_entry_host_environment_override_is_preserved(self):
        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        assignment = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == 'host' for t in n.targets))
        for env, expected in (({}, '127.0.0.1'), ({'BRIDGE_HOST': '0.0.0.0'}, '0.0.0.0'),
                              ({'BRIDGE_HOST': '192.0.2.10'}, '192.0.2.10')):
            namespace = {'os': types.SimpleNamespace(getenv=env.get)}
            exec(compile(ast.Module(body=[assignment], type_ignores=[]), '<direct host selection>', 'exec'), namespace)
            self.assertEqual(namespace['host'], expected)


if __name__ == '__main__':
    unittest.main()
