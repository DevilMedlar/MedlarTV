"""Authenticated Avatar bootstrap, publisher and console with in-memory peers."""
from __future__ import annotations
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from typing import TYPE_CHECKING, Protocol, TypeVar, TypedDict, cast
if TYPE_CHECKING:
    from avatar_fixture_types import AvatarServerNamespace

_T = TypeVar("_T")
_Namespace = TypeVar("_Namespace", bound=dict[str, object])

import ast
import asyncio
import contextlib
import importlib
import importlib.util
from importlib.machinery import ModuleSpec
from importlib.abc import Loader
import io
import json
import logging
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch, mock_open
from MedlarTV.avatar.bridge import auth, runtime


if TYPE_CHECKING:
    class ObserverNamespace(TypedDict):
        _mood_observer: Mock
        log: Mock
        _notify_mood: Callable[[str, str], None]
        set_mood_observer: Callable[[object], None]

    class LauncherNamespace(TypedDict):
        _avatar_session: auth.Bootstrap
        StartupError: type[RuntimeError]
        _avatar_bootstrap_for: Callable[[str], dict[str, str | int] | None]
        _write_avatar_bootstrap: Callable[[object, dict[str, str | int]], None]

    class EmotionalModule(Protocol):
        __name__: str
        get_emotional_system: Mock
        get_emotion_state: Mock

    class BrainModule(Protocol):
        __name__: str
        set_mood_observer: Mock

    class CoreNamespace(TypedDict):
        initialize_systems: Callable[[], None]
        shutdown: Callable[[], None]
        load_memory: Mock
        log: Mock
        sys: types.SimpleNamespace

    class _Generate(Protocol):
        def __call__(self, message: str, username: str, *, context: str) -> object: ...

    class GenerationNamespace(TypedDict):
        generate_response: _Generate

    class _Listen(Protocol):
        def __call__(self, bootstrap: auth.Bootstrap, *, connector: Mock) -> Coroutine[object, object, None]: ...

    class ConsoleNamespace(TypedDict):
        listen: _Listen
        render_mood: Mock

    class RendererNamespace(TypedDict):
        render_mood: Callable[[object], None]

ROOT = Path(__file__).resolve().parents[1]
PUB, SUB = 'a' * 64, 'b' * 64  # Fixed fake capabilities; never real generated keys.


def session(role: str = 'server', channel: str = '#example') -> auth.Bootstrap:
    data = {'version': 1, 'role': 'server', 'channel': channel, 'url': 'ws://127.0.0.1:8768',
            'publisher_key': PUB, 'subscriber_key': SUB}
    server = auth.parse_bootstrap(data, 'server')
    return server if role == 'server' else auth.parse_bootstrap(auth.child_payload(server, role), role)


def finish(coroutine: Coroutine[object, object, _T]) -> _T:
    try:
        coroutine.send(None)
    except StopIteration as result:
        return cast(_T, result.value)
    finally:
        coroutine.close()
    raise AssertionError('Unexpected blocking await')


def definitions(relative: str, namespace: _Namespace, names: set[str] | None = None) -> _Namespace:
    tree = ast.parse((ROOT / relative).read_text(encoding='utf-8-sig'))
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and
             (names is None or n.name in names)]
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[])), relative, 'exec'), namespace)
    return namespace


class FakeAsyncio:
    CancelledError = asyncio.CancelledError
    def __init__(self) -> None:
        self.waits: list[float] = []
        self.sleeps: list[float] = []
        self.on_sleep: Callable[[float], object] = lambda delay: None
        self.failure: BaseException | None = None
    async def wait_for(self, coroutine: Coroutine[object, object, _T], timeout: float) -> _T:
        self.waits.append(timeout)
        if self.failure:
            coroutine.close()
            raise self.failure
        return await coroutine
    async def gather(self, *coroutines: Awaitable[object], return_exceptions: bool = False) -> list[object]:
        result: list[object] = []
        for coroutine in coroutines:
            try: result.append(await coroutine)
            except Exception as exc:
                if not return_exceptions: raise
                result.append(exc)
        return result
    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.on_sleep(delay)


def load_server() -> tuple[AvatarServerNamespace[Peer], FakeAsyncio]:
    import unicodedata
    fake = FakeAsyncio()
    globals_namespace: dict[str, object] = {'asyncio': fake, 'json': json, 'unicodedata': unicodedata, 'DEBUG': True,
          'ACTIVE_CONNECTIONS': set(), 'CHANNEL_REGISTRY': {}, 'CONNECTION_ROLES': {},
          'SERVER_SESSION': session(), 'LATEST_MOOD': None, 'MAX_CONNECTIONS': 64,
          'MAX_SUBSCRIPTIONS': 32, 'MAX_CHANNEL_LENGTH': 100, 'MAX_MOOD_LENGTH': 64,
          'MAX_MESSAGE_SIZE': 16384, 'MAX_QUEUE': 16, 'SEND_TIMEOUT': 2.0,
          'AUTH_TIMEOUT': 3.0, 'credential_role': auth.credential_role,
          'BootstrapError': auth.BootstrapError, 'transport_logger': auth.transport_logger}
    definitions('MedlarTV/avatar/bridge/server.py', globals_namespace)
    # Extracted real server definitions share the existing initialized state.
    return cast("AvatarServerNamespace[Peer]", globals_namespace), fake


class Peer:
    def __init__(self, frames: Iterable[object] = (), *, key: object = PUB, send_error: BaseException | None = None, receive_error: BaseException | None = None) -> None:
        self.frames = iter(frames)
        self.send = AsyncMock(side_effect=send_error)
        self.close = AsyncMock()
        self.recv = AsyncMock(side_effect=receive_error, return_value=json.dumps({'event': 'authenticate', 'token': key}))
        self.close_code: int | None = None
    def __aiter__(self) -> Peer: return self
    async def __anext__(self) -> str | bytes:
        try: frame = next(self.frames)
        except StopIteration: raise StopAsyncIteration
        if isinstance(frame, BaseException): raise frame
        return frame if isinstance(frame, (str, bytes)) else json.dumps(frame)


class Context:
    def __init__(self, peer: Peer) -> None: self.peer = peer
    async def __aenter__(self) -> Peer: return self.peer
    async def __aexit__(self, *args: object) -> bool: return False


class BootstrapTests(unittest.TestCase):
    def test_fake_session_rotation_and_role_distribution(self) -> None:
        with patch.object(auth.secrets, 'token_hex', side_effect=[PUB, SUB, 'c' * 64, 'd' * 64]):
            first = auth.create_session('Example', 'ws://127.0.0.1:8768')
            second = auth.create_session('Example', 'ws://127.0.0.1:8768')
        self.assertNotEqual(first.publisher_key, second.publisher_key)
        self.assertIsNone(auth.credential_role(second, PUB))
        self.assertEqual(auth.credential_role(first, SUB), 'subscriber')
        for role, key in (('publisher', PUB), ('subscriber', SUB)):
            payload = auth.child_payload(first, role)
            self.assertEqual(payload['key'], key)
            self.assertNotIn('publisher_key', payload)
            self.assertNotIn('subscriber_key', payload)
        self.assertNotIn(PUB, repr(first))
        self.assertNotIn(SUB, repr(first))

    def test_pipe_read_and_closed_or_missing_startup(self) -> None:
        payload = auth.encode_bootstrap(auth.child_payload(session(), 'publisher'))
        value = auth.read_bootstrap('publisher', stream=io.BytesIO(payload), enabled=True)
        self.assertEqual(cast(auth.Bootstrap, value).key, PUB)
        self.assertIsNone(auth.read_bootstrap('publisher', enabled=False))
        for raw in (b'', b'[]\n', b'{\n', b'x' * 2049, payload.rstrip(b'\n')):
            with self.subTest(length=len(raw)), self.assertRaises(auth.BootstrapError):
                auth.read_bootstrap('publisher', stream=io.BytesIO(raw), enabled=True)
        with self.assertRaises(auth.BootstrapError): auth.read_bootstrap('subscriber', enabled=False, required=True)

    def test_invalid_roles_keys_and_remote_urls_fail_closed(self) -> None:
        base = auth.child_payload(session(), 'publisher')
        for change in ({'role': 'subscriber'}, {'key': 'bad'}, {'url': 'ws://example.invalid:8768'},
                       {'url': 'ws://127.0.0.1:8768/?token=' + PUB}, {'url': 'wss://127.0.0.1:8768'}):
            with self.subTest(fields=list(change)), self.assertRaises(auth.BootstrapError):
                auth.parse_bootstrap(dict(base, **change), 'publisher')

    def test_disabled_transport_logs_even_under_debug(self) -> None:
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            logger = auth.transport_logger()
            logger.debug(PUB)
            logger.error(SUB)
            self.assertEqual(output.getvalue(), '')
        finally: root.removeHandler(handler)

    def test_client_checks_authenticated_role_and_channel(self) -> None:
        bootstrap = session('publisher')
        for response in ({'event': 'handshake', 'msg': 'connected', 'role': 'publisher', 'channel': '#example'},
                         {'event': 'handshake', 'msg': 'connected', 'role': 'subscriber', 'channel': '#example'}, {}):
            peer = Peer()
            peer.recv = AsyncMock(return_value=json.dumps(response))
            fake = FakeAsyncio()
            with patch.object(asyncio, 'wait_for', fake.wait_for):
                if response.get('role') == 'publisher': finish(auth.authenticate_client(peer, bootstrap))
                else:
                    with self.assertRaises(auth.BootstrapError): finish(auth.authenticate_client(peer, bootstrap))
            self.assertEqual(fake.waits, [2.0, 3.0])


class AuthorizationTests(unittest.TestCase):
    def setUp(self) -> None: self.ns, self.fake = load_server()
    def run_peer(self, peer: Peer) -> None:
        with contextlib.redirect_stdout(io.StringIO()): finish(self.ns['handler'](peer))
        self.assertNotIn(peer, self.ns['ACTIVE_CONNECTIONS'])
        self.assertNotIn(peer, self.ns['CONNECTION_ROLES'])
        self.assertFalse(any(peer in peers for peers in self.ns['CHANNEL_REGISTRY'].values()))

    def test_invalid_missing_and_old_keys_never_forward(self) -> None:
        invalid_keys: tuple[object, ...] = ('', 'c' * 64, None, {}, PUB.upper())
        for key in invalid_keys:
            with self.subTest(kind=type(key).__name__):
                peer = Peer([{'event': 'mood_update', 'mood': 'Neutral', 'channel': '#example'}], key=key)
                self.run_peer(peer)
                peer.close.assert_awaited_once_with(code=1008, reason='Avatar authentication required')
                self.assertIsNone(self.ns['LATEST_MOOD'])

    def test_role_is_derived_from_key_not_claim(self) -> None:
        peer = Peer([{'event': 'mood_update', 'mood': 'Neutral', 'channel': '#example'}], key=SUB)
        peer.recv.return_value = json.dumps({'event': 'authenticate', 'token': SUB, 'role': 'publisher'})
        self.run_peer(peer)
        self.assertEqual(json.loads(peer.send.await_args_list[0].args[0])['role'], 'subscriber')
        peer.close.assert_awaited_once_with(code=1008, reason='Avatar role not permitted')

    def test_publisher_cannot_subscribe_or_cross_channel(self) -> None:
        for event in ({'event': 'register', 'channel': '#example'},
                      {'event': 'mood_update', 'channel': '#other', 'mood': 'Neutral'}):
            peer = Peer([event])
            self.run_peer(peer)
            peer.close.assert_awaited_once()
        self.assertIsNone(self.ns['LATEST_MOOD'])

    def test_subscriber_cannot_cross_channel(self) -> None:
        peer = Peer([{'event': 'register', 'channel': '#other'}], key=SUB)
        self.run_peer(peer)
        peer.close.assert_awaited_once_with(code=1008, reason='Avatar channel not permitted')

    def test_auth_timeout_and_cancel_release_pending_slot(self) -> None:
        peer = Peer(receive_error=TimeoutError())
        self.run_peer(peer)
        self.assertEqual(self.fake.waits, [3.0])
        cancelled = Peer(receive_error=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError): finish(self.ns['handler'](cancelled))
        self.assertFalse(self.ns['ACTIVE_CONNECTIONS'])

    def test_greeting_failure_removes_all_stale_memberships(self) -> None:
        peer = Peer(send_error=RuntimeError('synthetic greeting failure'))
        self.ns['CHANNEL_REGISTRY']['#example'] = [peer, peer]
        with self.assertRaises(RuntimeError): finish(self.ns['handler'](peer))
        self.assertFalse(self.ns['ACTIVE_CONNECTIONS'])
        self.assertFalse(self.ns['CONNECTION_ROLES'])
        self.assertFalse(self.ns['CHANNEL_REGISTRY'])

    def test_current_snapshot_and_stable_change_deduplication(self) -> None:
        forward = AsyncMock()
        self.ns['broadcast'] = forward
        self.run_peer(Peer([{'event': 'mood_update', 'mood': 'Hyped & Excited', 'channel': '#example'}] * 2))
        forward.assert_awaited_once_with({'event': 'mood_update', 'mood': 'Hyped & Excited'}, '#example')
        subscriber = Peer([{'event': 'register', 'channel': '#EXAMPLE'}], key=SUB)
        self.run_peer(subscriber)
        self.assertEqual(json.loads(subscriber.send.await_args_list[-1].args[0]), self.ns['LATEST_MOOD'])

    def test_pending_and_publisher_connections_receive_no_mood(self) -> None:
        subscriber, publisher, pending = Peer(key=SUB), Peer(), Peer(key='')
        self.ns['ACTIVE_CONNECTIONS'].update((subscriber, publisher, pending))
        self.ns['CONNECTION_ROLES'].update({subscriber: 'subscriber', publisher: 'publisher'})
        finish(self.ns['broadcast']({'event': 'mood_update', 'mood': 'Neutral'}))
        subscriber.send.assert_awaited_once()
        publisher.send.assert_not_awaited()
        pending.send.assert_not_awaited()

    def test_debug_capture_never_contains_auth_frame_or_key(self) -> None:
        output = io.StringIO()
        peer = Peer([{'event': 'unknown', 'token': PUB, 'private': 'fake-private-chat'}])
        with contextlib.redirect_stdout(output): finish(self.ns['handler'](peer))
        for canary in (PUB, SUB, 'fake-private-chat'): self.assertNotIn(canary, output.getvalue())


class PublisherTests(unittest.TestCase):
    def test_slot_coalesces_and_stop_rejects_new_work(self) -> None:
        publisher = runtime.MoodPublisher(session('publisher'))
        for i in range(1000): self.assertTrue(publisher.offer('label' + str(i)))
        self.assertEqual(publisher._latest, 'label999')  # pyright: ignore[reportPrivateUsage] -- existing white-box publisher lifecycle assertion/setup
        self.assertTrue(publisher.stop())
        self.assertFalse(publisher.offer('Neutral'))
        self.assertFalse(publisher.ready)

    def test_initial_latest_changes_and_reconnect_snapshot(self) -> None:
        publisher = runtime.MoodPublisher(session('publisher'))
        peers = [Peer(), Peer()]
        for peer in peers:
            peer.recv.return_value = json.dumps({'event': 'handshake', 'msg': 'connected', 'role': 'publisher', 'channel': '#example'})
        connector = Mock(side_effect=[Context(peers[0]), Context(peers[1])])
        publisher._connector = connector  # pyright: ignore[reportPrivateUsage] -- existing white-box publisher lifecycle assertion/setup
        publisher.offer('Neutral')
        fake = FakeAsyncio()
        calls = {'poll': 0}
        def sleep(delay: float) -> None:
            if delay != 0.1: return
            calls['poll'] += 1
            if calls['poll'] == 1:
                publisher.offer('intermediate')
                publisher.offer('Hyped & Excited')
            elif calls['poll'] == 2: peers[0].close_code = 1006
            elif calls['poll'] == 3: publisher._stop.set()  # pyright: ignore[reportPrivateUsage] -- existing white-box publisher lifecycle assertion/setup
        fake.on_sleep = sleep
        with patch.object(runtime, 'asyncio', fake), patch.object(asyncio, 'wait_for', fake.wait_for):
            finish(publisher._run())  # pyright: ignore[reportPrivateUsage] -- existing white-box publisher lifecycle assertion/setup
        first = [json.loads(c.args[0]) for c in peers[0].send.await_args_list]
        second = [json.loads(c.args[0]) for c in peers[1].send.await_args_list]
        self.assertEqual([d['mood'] for d in first if d['event'] == 'mood_update'], ['Neutral', 'Hyped & Excited'])
        self.assertEqual([d['mood'] for d in second if d['event'] == 'mood_update'], ['Hyped & Excited'])
        for record in first + second:
            if record['event'] == 'mood_update': self.assertEqual(set(record), {'event', 'mood', 'channel'})
        self.assertTrue(all(c.kwargs['proxy'] is None for c in connector.call_args_list))

    def test_reconnect_backoff_is_capped_and_secret_free(self) -> None:
        publisher = runtime.MoodPublisher(session('publisher'), connector=Mock(side_effect=RuntimeError(PUB)))
        fake = FakeAsyncio()
        fake.on_sleep = lambda delay: publisher._stop.set() if len(fake.sleeps) == 7 else None  # pyright: ignore[reportPrivateUsage] -- existing white-box publisher lifecycle assertion/setup
        output = io.StringIO()
        logger = logging.Logger('fake-avatar-runtime', logging.DEBUG)
        logger.addHandler(logging.StreamHandler(output))
        with patch.object(runtime, 'asyncio', fake), patch.object(runtime, 'log', logger): finish(publisher._run())  # pyright: ignore[reportPrivateUsage] -- existing white-box publisher lifecycle assertion/setup
        self.assertEqual(fake.sleeps, [1, 2, 4, 8, 15, 15, 15])
        self.assertNotIn(PUB, output.getvalue())

    def test_stop_cancels_owner_loop_and_bounds_join(self) -> None:
        publisher = runtime.MoodPublisher(session('publisher'))
        publisher._loop, publisher._task, publisher._thread = Mock(), Mock(), Mock()  # pyright: ignore[reportPrivateUsage] -- existing white-box publisher lifecycle assertion/setup
        publisher._thread.is_alive.return_value = False  # pyright: ignore[reportPrivateUsage] -- existing white-box publisher lifecycle assertion/setup
        self.assertTrue(publisher.stop(timeout=100))
        publisher._loop.call_soon_threadsafe.assert_called_once_with(publisher._task.cancel)  # pyright: ignore[reportPrivateUsage] -- existing white-box publisher lifecycle assertion/setup
        publisher._thread.join.assert_called_once_with(timeout=3.0)  # pyright: ignore[reportPrivateUsage] -- existing white-box publisher lifecycle assertion/setup

    def test_start_failure_and_duplicate_start_cannot_orphan_publisher(self) -> None:
        with patch.object(runtime, '_publisher', None), patch.object(runtime.MoodPublisher, 'start', side_effect=RuntimeError(PUB)):
            with self.assertRaises(RuntimeError): runtime.start_publisher(session('publisher'), 'Neutral')
            self.assertIsNone(runtime.current_publisher())
        with patch.object(runtime, '_publisher', None), patch.object(runtime.MoodPublisher, 'start') as start:
            publisher = runtime.start_publisher(session('publisher'), 'Neutral')
            self.assertEqual(publisher._latest, 'Neutral')  # pyright: ignore[reportPrivateUsage] -- existing white-box publisher lifecycle assertion/setup
            with self.assertRaises(RuntimeError): runtime.start_publisher(session('publisher'), 'other')
            start.assert_called_once()
            self.assertTrue(runtime.stop_publisher())
            self.assertIsNone(runtime.current_publisher())

    def test_owner_worker_unwinds_and_clears_state_without_event_loop(self) -> None:
        publisher = runtime.MoodPublisher(session('publisher'))
        loop, task = Mock(), Mock()
        create_task: Callable[[Coroutine[object, object, None]], Mock] = lambda coro: (coro.close(), task)[1]
        loop.create_task.side_effect = create_task
        loop.run_until_complete.side_effect = asyncio.CancelledError()
        fake = types.SimpleNamespace(new_event_loop=Mock(return_value=loop), CancelledError=asyncio.CancelledError)
        publisher._stop.set()  # pyright: ignore[reportPrivateUsage] -- existing white-box publisher lifecycle assertion/setup
        with patch.object(runtime, 'asyncio', fake): publisher._thread_main()  # pyright: ignore[reportPrivateUsage] -- existing white-box publisher lifecycle assertion/setup
        task.cancel.assert_called_once()
        loop.close.assert_called_once()
        self.assertIsNone(publisher._loop)  # pyright: ignore[reportPrivateUsage] -- existing white-box publisher lifecycle assertion/setup
        self.assertIsNone(publisher._task)  # pyright: ignore[reportPrivateUsage] -- existing white-box publisher lifecycle assertion/setup
        self.assertFalse(publisher.ready)

    def test_surviving_worker_is_reported_incomplete_with_three_second_budget(self) -> None:
        publisher = runtime.MoodPublisher(session('publisher'))
        publisher._thread = Mock()  # pyright: ignore[reportPrivateUsage] -- existing white-box publisher lifecycle assertion/setup
        publisher._thread.is_alive.return_value = True  # pyright: ignore[reportPrivateUsage] -- existing white-box publisher lifecycle assertion/setup
        self.assertFalse(publisher.stop())
        publisher._thread.join.assert_called_once_with(timeout=3.0)  # pyright: ignore[reportPrivateUsage] -- existing white-box publisher lifecycle assertion/setup

    def test_observer_excludes_desktop_and_isolates_failure(self) -> None:
        observer = Mock()
        globals_namespace: dict[str, object] = {'_mood_observer': observer, 'log': Mock()}
        definitions('MedlarTV/core/llm_brain.py', globals_namespace, {'_notify_mood', 'set_mood_observer'})
        ns = cast('ObserverNamespace', globals_namespace)
        ns['_notify_mood']('Neutral', 'desktop')
        observer.assert_not_called()
        ns['_notify_mood']('Hyped & Excited', 'twitch')
        observer.assert_called_once_with('Hyped & Excited')
        observer.side_effect = RuntimeError(PUB)
        ns['_notify_mood']('Neutral', 'twitch')
        self.assertNotIn(PUB, str(ns['log'].warning.call_args))
        ns['set_mood_observer'](None)
        ns['_notify_mood']('Neutral', 'twitch')


class LauncherPipeTests(unittest.TestCase):
    def test_per_child_role_and_pipe_failure_errors_are_secret_free(self) -> None:
        globals_namespace: dict[str, object] = {'_avatar_session': session()}
        definitions('launcher.py', globals_namespace, {'_avatar_bootstrap_for', '_write_avatar_bootstrap'})
        ns = cast('LauncherNamespace', globals_namespace)
        for name, role in (('Avatar Bridge Server', 'server'), ('Medlar Core', 'publisher'), ('Avatar Console Client', 'subscriber')):
            self.assertEqual(cast(dict[str, str | int], ns['_avatar_bootstrap_for'](name))['role'], role)
        self.assertIsNone(ns['_avatar_bootstrap_for']('Ollama Server'))
        self.assertIsNone(ns['_avatar_bootstrap_for']('LibreTranslate Server'))
        ns['StartupError'] = RuntimeError
        pipe = Mock()
        child = types.SimpleNamespace(stdin=pipe)
        ns['_write_avatar_bootstrap'](child, auth.child_payload(session(), 'publisher'))
        pipe.close.assert_called_once()
        self.assertIn(PUB.encode(), pipe.write.call_args.args[0])
        for operation in ('write', 'flush', 'close'):
            pipe = Mock()
            getattr(pipe, operation).side_effect = RuntimeError(PUB)
            with self.subTest(operation=operation), self.assertRaises(RuntimeError) as caught:
                ns['_write_avatar_bootstrap'](types.SimpleNamespace(stdin=pipe), auth.child_payload(session(), 'publisher'))
            self.assertNotIn(PUB, str(caught.exception))

    def test_owned_pipe_launch_exposes_no_key_in_environment_or_arguments(self) -> None:
        import launcher
        for name, script, role in (
            ('Avatar Bridge Server', 'MedlarTV/avatar/bridge/server.py', 'server'),
            ('Medlar Core', 'MedlarTV/core/main.py', 'publisher'),
            ('Avatar Console Client', 'MedlarTV/avatar_client/console_client.py', 'subscriber')):
            owned: list[launcher.ProcessEntry] = []
            child = Mock(pid=43277)
            child.poll.return_value = None
            def check_write(raw: bytes) -> int:
                self.assertIs(owned[0]['proc'], child)  # Ownership precedes any pipe write.
                self.assertEqual(json.loads(raw)['role'], role)
                self.assertLessEqual(len(raw), 2048)
                return len(raw)
            child.stdin.write.side_effect = check_write
            output = io.StringIO()
            with patch.object(launcher, '_avatar_session', session()), patch.object(launcher, 'processes', owned), \
                 patch.object(launcher, '_child_environment', return_value={}), patch.object(launcher, 'DEBUG', True), \
                 patch.object(launcher, '_windows_console_available', return_value=True), \
                 patch.object(launcher.platform, 'system', return_value='Windows'), \
                 patch.object(launcher.subprocess, 'Popen', return_value=child) as popen, \
                 patch.object(launcher.time, 'sleep'), patch('builtins.open', mock_open()) as log_file, \
                 contextlib.redirect_stdout(output):
                self.assertTrue(launcher.start_process(name, ['fake-python', script], wait=0, use_venv=True))
            self.assertEqual(popen.call_args.kwargs['stdin'], launcher.subprocess.PIPE)
            self.assertEqual(popen.call_args.kwargs['env'][auth.BOOTSTRAP_FLAG], '1')
            self.assertFalse(popen.call_args.kwargs['shell'])
            child.stdin.close.assert_called_once()
            for key in (PUB, SUB):
                self.assertNotIn(key, str(popen.call_args))
                self.assertNotIn(key, output.getvalue())
                self.assertNotIn(key, str(log_file().write.call_args_list))

    def test_unrelated_service_never_inherits_auth_pipe(self) -> None:
        import launcher
        child = Mock(pid=43278)
        child.poll.return_value = None
        with patch.object(launcher, '_avatar_session', session()), patch.object(launcher, 'processes', []), \
             patch.object(launcher, '_child_environment', return_value={}), \
             patch.object(launcher.platform, 'system', return_value='Linux'), \
             patch.object(launcher.subprocess, 'Popen', return_value=child) as popen, \
             patch.object(launcher.time, 'sleep'), patch('builtins.open', mock_open()), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(launcher.start_process('Ollama Server', ['fake-ollama', 'serve'], wait=0))
        self.assertEqual(popen.call_args.kwargs['stdin'], launcher.subprocess.DEVNULL)
        self.assertNotIn(auth.BOOTSTRAP_FLAG, popen.call_args.kwargs['env'])
        child.stdin.write.assert_not_called()

    def test_pipe_failure_retains_owned_child_for_launcher_cleanup(self) -> None:
        import launcher
        owned: list[launcher.ProcessEntry] = []
        child = Mock(pid=43279)
        child.stdin.write.side_effect = OSError(PUB)
        output = io.StringIO()
        with patch.object(launcher, '_avatar_session', session()), patch.object(launcher, 'processes', owned), \
             patch.object(launcher, '_child_environment', return_value={}), \
             patch.object(launcher.platform, 'system', return_value='Linux'), \
             patch.object(launcher.subprocess, 'Popen', return_value=child), \
             patch.object(launcher.time, 'sleep') as sleep, patch('builtins.open', mock_open()), \
             contextlib.redirect_stdout(output), self.assertRaises(launcher.StartupError):
            launcher.start_process('Avatar Bridge Server', ['fake-python', 'MedlarTV/avatar/bridge/server.py'])
        self.assertIs(owned[0]['proc'], child)
        child.stdin.close.assert_called_once()
        sleep.assert_not_called()
        self.assertNotIn(PUB, output.getvalue())


class CorePublishingIntegrationTests(unittest.TestCase):
    def fake_core_modules(self) -> tuple[EmotionalModule, BrainModule]:
        emotional = cast('EmotionalModule', types.ModuleType('MedlarTV.core.emotional_system'))
        emotional.get_emotional_system = Mock()
        emotional.get_emotion_state = Mock(return_value={'joy': 0.8})
        brain = cast('BrainModule', types.ModuleType('MedlarTV.core.llm_brain'))
        brain.set_mood_observer = Mock()
        return emotional, brain

    def test_initialize_reads_publisher_pipe_and_offers_current_canonical_mood(self) -> None:
        emotional, brain = self.fake_core_modules()
        publisher = Mock()
        getenv: Callable[..., str] = lambda *args: 'false'
        globals_namespace: dict[str, object] = {'os': types.SimpleNamespace(getenv=getenv), 'log': Mock(),
              'compute_mood': Mock(return_value={'label': 'Hyped & Excited'}),
              'load_memory': Mock(), 'verify_twitch_tokens': Mock(return_value=False)}
        definitions('MedlarTV/core/main.py', globals_namespace, {'initialize_systems'})
        ns = cast('CoreNamespace', globals_namespace)
        with patch.dict(sys.modules, {emotional.__name__: emotional, brain.__name__: brain}), \
             patch.object(auth, 'read_bootstrap', return_value=session('publisher')) as read, \
             patch.object(runtime, 'start_publisher', return_value=publisher) as start:
            ns['initialize_systems']()
        read.assert_called_once_with('publisher')
        start.assert_called_once_with(session('publisher'), 'Hyped & Excited')
        brain.set_mood_observer.assert_called_once_with(publisher.offer)
        ns['load_memory'].assert_called_once()

    def test_no_bootstrap_leaves_standalone_core_without_publisher(self) -> None:
        emotional, brain = self.fake_core_modules()
        getenv: Callable[..., str] = lambda *args: 'false'
        globals_namespace: dict[str, object] = {'os': types.SimpleNamespace(getenv=getenv), 'log': Mock(),
              'load_memory': Mock(), 'verify_twitch_tokens': Mock(return_value=False)}
        definitions('MedlarTV/core/main.py', globals_namespace, {'initialize_systems'})
        ns = cast('CoreNamespace', globals_namespace)
        with patch.dict(sys.modules, {emotional.__name__: emotional, brain.__name__: brain}), \
             patch.object(auth, 'read_bootstrap', return_value=None), patch.object(runtime, 'start_publisher') as start:
            ns['initialize_systems']()
        start.assert_not_called()
        brain.set_mood_observer.assert_not_called()

    def test_shutdown_detaches_hook_and_preserves_combined_cleanup_status(self) -> None:
        _, brain = self.fake_core_modules()
        for publisher_stopped, listener_stopped, expected in ((True, True, 0), (False, True, 1), (True, False, 1)):
            order: list[str] = []
            detach: Callable[[object], None] = lambda value: order.append('detach')
            brain.set_mood_observer.side_effect = detach
            def stop() -> bool: order.append('publisher'); return publisher_stopped
            def listener() -> bool: order.append('listener'); return listener_stopped
            getenv: Callable[..., str] = lambda *args: 'false'
            globals_namespace: dict[str, object] = {'os': types.SimpleNamespace(getenv=getenv), 'log': Mock(),
                  'stop_listener': listener, 'sys': types.SimpleNamespace(exit=Mock())}
            definitions('MedlarTV/core/main.py', globals_namespace, {'shutdown'})
            ns = cast('CoreNamespace', globals_namespace)
            with patch.dict(sys.modules, {brain.__name__: brain}), patch.object(runtime, 'stop_publisher', side_effect=stop):
                ns['shutdown']()
            self.assertEqual(order, ['detach', 'publisher', 'listener'])
            ns['sys'].exit.assert_called_once_with(expected)

    def test_actual_generation_prefix_publishes_after_emotion_update_and_excludes_desktop(self) -> None:
        # Execute the actual generation path up to model-context construction;
        # no prompt/model/history/network work is part of this test.
        tree = ast.parse((ROOT / 'MedlarTV/core/llm_brain.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'generate_response')
        boundary = next(i for i, n in enumerate(node.body) if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == 'emotional_context' for t in n.targets))
        node.body = node.body[:boundary]
        for context in ('twitch', 'desktop'):
            order: list[str] = []
            offered: list[str] = []
            emotional = Mock()
            process_message: Callable[[object], None] = lambda message: order.append('emotion')
            emotional.process_message.side_effect = process_message
            def compute(state: object) -> dict[str, str]: order.append('mood'); return {'label': 'Hyped & Excited'}
            def observer(label: str) -> None: order.append('offer'); offered.append(label)
            globals_namespace: dict[str, object] = {'DEBUG': False, 'log': Mock(), '_mood_observer': observer,
                  '_load_personality': Mock(return_value={}), '_build_system_prompt': Mock(return_value=''),
                  'analyze_sentiment_advanced': Mock(return_value=(0, {})), '_estimate_severity': Mock(return_value=0),
                  'get_emotional_system': Mock(return_value=emotional), 'get_emotion_state': Mock(return_value={}),
                  'compute_mood': compute}
            definitions('MedlarTV/core/llm_brain.py', globals_namespace, {'_notify_mood'})
            future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
            exec(compile(ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[])), '<generation prefix>', 'exec'), globals_namespace)
            ns = cast('GenerationNamespace', globals_namespace)
            ns['generate_response']('fake synthetic emotional input', 'fixture', context=context)
            self.assertEqual(order, ['emotion', 'mood', 'offer'] if context == 'twitch' else ['emotion', 'mood'])
            self.assertEqual(offered, ['Hyped & Excited'] if context == 'twitch' else [])


class ConsoleIntegrationTests(unittest.TestCase):
    def namespace(self, fake: FakeAsyncio) -> ConsoleNamespace:
        globals_namespace: dict[str, object] = {'asyncio': fake, 'json': json, 'IO_TIMEOUT': 2.0, 'BootstrapError': auth.BootstrapError,
              'authenticate_client': auth.authenticate_client, 'transport_logger': auth.transport_logger,
              'render_mood': Mock()}
        return cast('ConsoleNamespace', definitions('MedlarTV/avatar_client/console_client.py', globals_namespace, {'listen'}))

    def test_console_authenticates_subscribes_and_renders_only_valid_mood_event(self) -> None:
        peer = Peer(['[]', '{', {'event': 'handshake'}, {'event': 'mood_update', 'mood': 'Hyped & Excited'},
                     asyncio.CancelledError()])
        peer.recv.return_value = json.dumps({'event': 'handshake', 'msg': 'connected', 'role': 'subscriber', 'channel': '#example'})
        fake = FakeAsyncio()
        globals_namespace = self.namespace(fake)
        connector = Mock(return_value=Context(peer))
        with patch.object(asyncio, 'wait_for', fake.wait_for), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(asyncio.CancelledError):
            finish(globals_namespace['listen'](session('subscriber'), connector=connector))
        records = [json.loads(c.args[0]) for c in peer.send.await_args_list]
        self.assertEqual([r['event'] for r in records], ['authenticate', 'register'])
        self.assertEqual(records[-1], {'event': 'register', 'channel': '#example'})
        globals_namespace['render_mood'].assert_called_once_with('Hyped & Excited')

    def test_invalid_ack_never_subscribes_and_reconnects_without_secrets(self) -> None:
        peer = Peer()
        peer.recv.return_value = json.dumps({'event': 'handshake', 'msg': 'connected', 'role': 'publisher', 'channel': '#example'})
        fake = FakeAsyncio()
        def cancel(delay: float) -> None: raise asyncio.CancelledError()
        fake.on_sleep = cancel
        globals_namespace = self.namespace(fake)
        output = io.StringIO()
        with patch.object(asyncio, 'wait_for', fake.wait_for), contextlib.redirect_stdout(output), self.assertRaises(asyncio.CancelledError):
            finish(globals_namespace['listen'](session('subscriber'), connector=Mock(return_value=Context(peer))))
        self.assertEqual(peer.send.await_count, 1)
        globals_namespace['render_mood'].assert_not_called()
        self.assertEqual(fake.sleeps, [1])
        self.assertNotIn(SUB, output.getvalue())

    def test_console_backoff_caps_and_cancellation_unwinds(self) -> None:
        fake = FakeAsyncio()
        def cancel(delay: float) -> None:
            if len(fake.sleeps) == 7: raise asyncio.CancelledError()
        fake.on_sleep = cancel
        globals_namespace = self.namespace(fake)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(asyncio.CancelledError):
            finish(globals_namespace['listen'](session('subscriber'), connector=Mock(side_effect=RuntimeError(SUB))))
        self.assertEqual(fake.sleeps, [1, 2, 4, 8, 15, 15, 15])
        self.assertNotIn(SUB, output.getvalue())

    def test_renderer_rejects_terminal_controls_and_preserves_legacy_gestures(self) -> None:
        strftime: Callable[..., str] = lambda *args: '00:00:00'
        globals_namespace: dict[str, object] = {'valid_mood': runtime.valid_mood, 'Fore': types.SimpleNamespace(WHITE=''),
              'time': types.SimpleNamespace(strftime=strftime),
              'MOOD_COLORS': {}, 'MOOD_ANIMATIONS': {'hype': 'legacy gesture fixture'}}
        definitions('MedlarTV/avatar_client/console_client.py', globals_namespace, {'render_mood'})
        ns = cast('RendererNamespace', globals_namespace)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            for invalid in ('', '\x1b[31m', 'bad\n', 'x' * 65, None): ns['render_mood'](invalid)
            ns['render_mood']('Hyped & Excited')
            ns['render_mood']('hype')
        self.assertEqual(output.getvalue().count('MODE SHIFT'), 2)
        self.assertIn('HYPED & EXCITED', output.getvalue())
        self.assertIn('legacy gesture fixture', output.getvalue())


class ImportBoundaryTests(unittest.TestCase):
    def test_real_lazy_packages_and_client_import_without_runtime_actions(self) -> None:
        # Temporarily load the actual package initializers instead of synthetic
        # parents, exercising the exported API under the runner's action guards.
        names = ('MedlarTV.avatar', 'MedlarTV.avatar.bridge', 'MedlarTV.avatar.bridge.client')
        saved = {name: sys.modules.get(name) for name in names}
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                for name, relative in (('MedlarTV.avatar', 'MedlarTV/avatar/__init__.py'),
                                       ('MedlarTV.avatar.bridge', 'MedlarTV/avatar/bridge/__init__.py')):
                    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
                    module = importlib.util.module_from_spec(cast(ModuleSpec, spec))
                    sys.modules[name] = module
                    cast(Loader, cast(ModuleSpec, spec).loader).exec_module(module)
                client_name = 'MedlarTV.avatar.bridge.client'
                sys.modules.pop(client_name, None)
                client = importlib.import_module(client_name)
                self.assertIs(sys.modules['MedlarTV.avatar'].send_mood_update, client.send_mood_update)
                self.assertIs(sys.modules['MedlarTV.avatar.bridge'].send_mood_update, client.send_mood_update)
                self.assertFalse(client.is_bridge_available())
                self.assertFalse(client.send_mood_update('Neutral'))
                self.assertFalse(client.register_channel('#example'))
            self.assertEqual(output.getvalue(), '')
        finally:
            for name, module in saved.items():
                if module is None: sys.modules.pop(name, None)
                else: sys.modules[name] = module


if __name__ == '__main__': unittest.main()
