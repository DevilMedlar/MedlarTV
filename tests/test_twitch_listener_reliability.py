"""Deterministic offline regression tests for Twitch IRC lifecycle reliability.

The suite's own ``TwitchIRC`` fixtures opt into guarded project imports while
the shared harness default preserves production imports for dependent framing
and authority suites. No credentials are read and no real network is contacted.
"""

from __future__ import annotations

import ast
import builtins
import logging
import queue
import socket as socket_constants
import ssl
import threading
import time
import unittest
from chat_pacing_fixture import install_fake_chat_clock
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


LISTENER = Path(__file__).resolve().parents[1] / "MedlarTV" / "tools" / "twitch_listener.py"
COMMAND_CENTER = Path(__file__).resolve().parents[1] / "MedlarTV" / "app" / "medlar_app.py"
IRC_FRAMING = Path(__file__).resolve().parents[1] / "MedlarTV" / "core" / "irc_framing.py"


def isolated_project_builtins():
    """Provide only the Medlar imports used by transport-only connect tests."""
    tree = ast.parse(IRC_FRAMING.read_text(encoding="utf-8"), filename=str(IRC_FRAMING))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.ClassDef) and item.name == "IRCLineBuffer"
    )
    framing_namespace = {}
    exec(
        compile(ast.Module(body=[node], type_ignores=[]), str(IRC_FRAMING), "exec"),
        framing_namespace,
    )

    def no_auxiliary_channels(_primary, copilots):
        if list(copilots):
            raise AssertionError("Transport fixture does not model co-pilot authority")
        return []

    framing_module = SimpleNamespace(IRCLineBuffer=framing_namespace["IRCLineBuffer"])
    authority_module = SimpleNamespace(copilot_channels=no_auxiliary_channels)
    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "MedlarTV.core.irc_framing":
            return framing_module
        if name == "MedlarTV.command_authority":
            return authority_module
        if name.startswith("MedlarTV"):
            raise AssertionError(f"Unexpected Medlar import in transport fixture: {name}")
        return real_import(name, globals, locals, fromlist, level)

    namespace = dict(vars(builtins))
    namespace["__import__"] = guarded_import
    return namespace


def load_client_class(namespace, *, isolate_project_imports=False):
    """Compile the production client without importing the listener module."""
    if isolate_project_imports:
        namespace["__builtins__"] = isolated_project_builtins()
    tree = ast.parse(LISTENER.read_text(encoding="utf-8"), filename=str(LISTENER))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.ClassDef) and item.name == "TwitchIRC"
    )
    # Both nodes come from the parser with complete locations. Rewalking the
    # large class with fix_missing_locations needlessly enters the intermittent
    # runtime traversal failure reproduced by the bounded offline control.
    # Compile fresh code into this fixture's globals; never import startup code.
    future = ast.parse("from __future__ import annotations").body[0]
    selected = ast.Module(body=[future, node], type_ignores=[])
    exec(compile(selected, str(LISTENER), "exec"), namespace)
    return namespace["TwitchIRC"]


class ScriptedSocket:
    """Socket double with scripted receives and observable complete writes."""

    def __init__(self, actions=()):
        self.actions = deque(actions)
        self.sent = []
        self.send_threads = []
        self.connected_to = None
        self.timeouts = []
        self.shutdown_calls = []
        self.close_count = 0
        self._condition = threading.Condition()

    def connect(self, endpoint):
        self.connected_to = endpoint

    def settimeout(self, value):
        self.timeouts.append(value)

    def recv(self, _size):
        with self._condition:
            if not self.actions:
                raise TimeoutError("script exhausted")
            action = self.actions.popleft()
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return action(self)
        return action

    def sendall(self, payload):
        with self._condition:
            self.sent.append(payload)
            self.send_threads.append(threading.current_thread().name)
            self._condition.notify_all()

    def shutdown(self, how):
        self.shutdown_calls.append(how)

    def close(self):
        self.close_count += 1

    def wait_for_sent(self, count, timeout=1.0):
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(self.sent) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


class StepClock:
    """Monotonic clock that advances without sleeping."""

    def __init__(self, step=1.0):
        self.value = 0.0
        self.step = step

    def monotonic(self):
        current = self.value
        self.value += self.step
        return current

    def time(self):
        return self.value

    @staticmethod
    def sleep(_seconds):
        raise AssertionError("reliability tests must not sleep")


class ListenerHarness:
    def client_with_socket(self, tls_socket=None, *, raw_socket=None, clock=None,
                           isolate_project_imports=False):
        raw_socket = raw_socket or ScriptedSocket()
        tls_socket = tls_socket or ScriptedSocket()
        socket_factory = Mock(return_value=raw_socket)
        socket_module = SimpleNamespace(
            socket=socket_factory,
            SHUT_RDWR=socket_constants.SHUT_RDWR,
        )
        context = SimpleNamespace(
            check_hostname=True,
            verify_mode=ssl.CERT_REQUIRED,
            wrap_socket=Mock(return_value=tls_socket),
        )
        ssl_module = SimpleNamespace(
            create_default_context=Mock(return_value=context),
        )
        fake_os = SimpleNamespace(getenv=lambda key, default="": (
            "false" if key in {
                "ENABLE_ONLINE_OFFLINE_MSGS", "ENABLE_EMOTE_RESPONSES",
            } else default
        ))
        namespace = {
            "socket": socket_module,
            "ssl": ssl_module,
            "threading": threading,
            "time": clock or time,
            "os": fake_os,
            "log": logging.getLogger("test_twitch_listener_reliability"),
            "TWITCH_SERVER": "irc.chat.twitch.tv",
            "TWITCH_PORT": 6697,
            "IRC_TOKEN": "oauth:synthetic-test-token",
            "BOT_NICK": "TestBot",
            "CHANNEL": "#testchannel",
            "_get_active_copilots": lambda: [],
            "_load_timers_yaml": lambda: [],
            "_prepare_msg": lambda message: message,
            "is_enabled": lambda _key: False,
            "_arrived_users": set(),
            "_apply_event_rules": Mock(),
        }
        client_class = load_client_class(
            namespace, isolate_project_imports=isolate_project_imports,
        )
        client = client_class()
        install_fake_chat_clock(client)
        # This suite isolates transport with an explicitly disabled content policy.
        client._load_output_policy = lambda: SimpleNamespace(
            enabled=False, llm_enabled=False, permits=lambda _text: True,
            format_body=lambda text: text,
        )
        client._init_emotes = Mock()
        client._init_timers = Mock()
        return SimpleNamespace(
            client=client,
            raw=raw_socket,
            tls=tls_socket,
            context=context,
            socket_module=socket_module,
            ssl_module=ssl_module,
        )

    @staticmethod
    def activate(client, transport, generation=1):
        with client._state_lock:
            client._generation = generation
            client.sock = transport
            client.connected = True
            client.stop_flag = False
            client._stopping = False
            client._stop_event.clear()


class IsolatedListenerHarness(ListenerHarness):
    """Opt-in project import isolation for this module's transport contracts."""

    def client_with_socket(self, tls_socket=None, *, raw_socket=None, clock=None,
                           isolate_project_imports=True):
        return super().client_with_socket(
            tls_socket,
            raw_socket=raw_socket,
            clock=clock,
            isolate_project_imports=isolate_project_imports,
        )


class TwitchHandshakeReliabilityTests(IsolatedListenerHarness, unittest.TestCase):
    def test_connected_is_false_until_001_and_matching_self_join_arrive(self):
        observed = []
        harness = None

        def authentication(_socket):
            observed.append(harness.client.connected)
            return b":tmi.twitch.tv 001 TestBot :Welcome\r\n"

        def joined(_socket):
            observed.append(harness.client.connected)
            return b":TestBot!testbot@testbot.tmi.twitch.tv JOIN #testchannel\r\n"

        harness = self.client_with_socket(ScriptedSocket([
            authentication,
            b":OtherBot!other@other.tmi.twitch.tv JOIN #testchannel\r\n",
            b":TestBot!testbot@testbot.tmi.twitch.tv JOIN #otherchannel\r\n",
            joined,
        ]))

        generation = harness.client.connect()

        self.assertEqual(observed, [False, False])
        self.assertEqual(generation, 1)
        self.assertTrue(harness.client.connected)
        self.assertIs(harness.client.sock, harness.tls)

    def test_authentication_rejection_closes_socket_without_initializing(self):
        harness = self.client_with_socket(ScriptedSocket([
            b":tmi.twitch.tv NOTICE * :Login authentication failed\r\n",
        ]))

        with self.assertRaisesRegex(
            harness.client.AuthenticationError, "rejected IRC authentication"
        ):
            harness.client.connect()

        self.assertFalse(harness.client.connected)
        self.assertIsNone(harness.client.sock)
        self.assertEqual(harness.tls.close_count, 1)
        harness.client._init_emotes.assert_not_called()
        harness.client._init_timers.assert_not_called()

    def test_missing_join_ack_times_out_without_real_wait(self):
        clock = StepClock()
        harness = self.client_with_socket(
            ScriptedSocket([
                b":tmi.twitch.tv 001 TestBot :Welcome\r\n",
                TimeoutError("poll"),
            ]),
            clock=clock,
        )
        harness.client.HANDSHAKE_TIMEOUT = 3.0

        with self.assertRaisesRegex(
            harness.client.JoinError, "JOIN acknowledgement timed out"
        ):
            harness.client.connect()

        self.assertFalse(harness.client.connected)
        self.assertIsNone(harness.client.sock)
        self.assertEqual(harness.tls.close_count, 1)

    def test_eof_during_handshake_is_failure_not_connected(self):
        harness = self.client_with_socket(ScriptedSocket([
            b":tmi.twitch.tv 001 TestBot :Welcome\r\n",
            b"",
        ]))

        with self.assertRaisesRegex(ConnectionError, "closed during authentication"):
            harness.client.connect()

        self.assertFalse(harness.client.connected)
        self.assertIsNone(harness.client.sock)
        self.assertEqual(harness.tls.close_count, 1)

    def test_stop_signal_during_handshake_cancels_connection(self):
        harness = None

        def cancel(_socket):
            harness.client._stop_event.set()
            return b""

        harness = self.client_with_socket(ScriptedSocket([cancel]))

        with self.assertRaisesRegex(harness.client.Cancelled, "cancelled"):
            harness.client.connect()

        self.assertFalse(harness.client.connected)
        self.assertEqual(harness.tls.close_count, 1)

    def test_stop_closes_and_unblocks_an_in_progress_raw_connect(self):
        entered_connect = threading.Event()
        released_connect = threading.Event()

        class BlockingRawSocket(ScriptedSocket):
            def connect(self, endpoint):
                super().connect(endpoint)
                entered_connect.set()
                released_connect.wait(1.0)
                raise OSError("synthetic raw close interruption")

            def close(self):
                super().close()
                released_connect.set()

        raw = BlockingRawSocket()
        harness = self.client_with_socket(raw_socket=raw)
        result = []

        def connect():
            try:
                harness.client.connect()
            except BaseException as exc:
                result.append(exc)

        connector = threading.Thread(target=connect, daemon=True)
        connector.start()
        self.assertTrue(entered_connect.wait(1.0))

        harness.client.stop()
        connector.join(1.0)

        self.assertFalse(connector.is_alive())
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], harness.client.Cancelled)
        self.assertEqual(raw.close_count, 1)
        harness.context.wrap_socket.assert_not_called()
        self.assertEqual(harness.tls.sent, [])

    def test_stop_closes_and_unblocks_an_in_progress_handshake(self):
        entered_recv = threading.Event()
        released_recv = threading.Event()

        class BlockingSocket(ScriptedSocket):
            def recv(self, _size):
                entered_recv.set()
                released_recv.wait(1.0)
                raise OSError("synthetic close interruption")

            def close(self):
                super().close()
                released_recv.set()

        harness = self.client_with_socket(BlockingSocket())
        result = []

        def connect():
            try:
                harness.client.connect()
            except BaseException as exc:
                result.append(exc)

        connector = threading.Thread(target=connect, daemon=True)
        connector.start()
        self.assertTrue(entered_recv.wait(1.0))

        harness.client.stop()
        connector.join(1.0)

        self.assertFalse(connector.is_alive())
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], harness.client.Cancelled)
        self.assertEqual(harness.tls.close_count, 1)


class TwitchReconnectReliabilityTests(IsolatedListenerHarness, unittest.TestCase):
    def test_established_eof_is_reported_to_reconnect_manager(self):
        handshake = (
            b":tmi.twitch.tv 001 TestBot :Welcome\r\n"
            b":TestBot!testbot@testbot.tmi.twitch.tv JOIN #testchannel\r\n"
        )
        harness = self.client_with_socket(ScriptedSocket([handshake, b""]))
        generation = harness.client.connect()

        with self.assertRaisesRegex(EOFError, "closed by peer"):
            harness.client._receive_loop(generation)

        self.assertTrue(
            harness.client._disconnect_generation(generation, "test_eof")
        )
        self.assertFalse(harness.client.connected)
        self.assertIsNone(harness.client.sock)

    def test_reconnect_uses_interruptible_exponential_backoff(self):
        harness = self.client_with_socket()
        client = harness.client
        client._ensure_background_workers = Mock()
        client._shutdown_background_workers = Mock()
        client._stop_timers = Mock()
        client.connect = Mock(side_effect=[
            OSError("synthetic connect failure"),
            OSError("synthetic reconnect failure"),
        ])
        waits = []

        def wait(delay):
            waits.append(delay)
            return len(waits) == 2

        client._wait_for_reconnect = wait

        client.listen()

        self.assertEqual(client.connect.call_count, 2)
        self.assertEqual(waits, [1.0, 2.0])
        client._shutdown_background_workers.assert_called_once_with()


class TwitchQueueReliabilityTests(IsolatedListenerHarness, unittest.TestCase):
    def test_ping_remains_responsive_while_message_worker_is_blocked(self):
        harness = self.client_with_socket()
        client = harness.client
        self.activate(client, harness.tls)
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow_process(_line):
            started.set()
            release.wait(1.0)
            finished.set()

        client._process_raw_message = slow_process
        client._ensure_background_workers()
        self.addCleanup(client.stop)
        self.addCleanup(release.set)

        client._handle_raw_message(
            ":viewer!viewer@local PRIVMSG #testchannel :slow request", 1
        )
        self.assertTrue(started.wait(1.0), "worker did not start synthetic slow work")

        client._handle_raw_message("PING :tmi.twitch.tv", 1)

        self.assertTrue(harness.tls.wait_for_sent(1))
        self.assertEqual(harness.tls.sent, [b"PONG :tmi.twitch.tv\r\n"])
        self.assertFalse(finished.is_set(), "PING waited for slow message processing")
        release.set()
        self.assertTrue(finished.wait(1.0))
        client.stop()

    def test_work_and_outbound_queues_reject_overflow_without_blocking(self):
        harness = self.client_with_socket()
        client = harness.client
        self.activate(client, harness.tls)

        client._work_queue = queue.Queue(maxsize=2)
        self.assertTrue(client._enqueue_work("first", 1))
        self.assertTrue(client._enqueue_work("second", 1))
        self.assertFalse(client._enqueue_work("dropped", 1))
        self.assertEqual(client._work_queue.qsize(), 2)
        self.assertEqual(
            [client._work_queue.get_nowait(), client._work_queue.get_nowait()],
            [(1, "first"), (1, "second")],
        )

        class AliveSender:
            @staticmethod
            def is_alive():
                return True

        client._sender_thread = AliveSender()
        client._outbound_queue = queue.Queue(maxsize=2)
        self.assertTrue(client._queue_outbound("PRIVMSG #testchannel :one", 1))
        self.assertTrue(client._queue_outbound("PRIVMSG #testchannel :two", 1))
        self.assertFalse(client._queue_outbound("PRIVMSG #testchannel :dropped", 1))
        self.assertEqual(client._outbound_queue.qsize(), 2)
        queued = [
            client._outbound_queue.get_nowait(),
            client._outbound_queue.get_nowait(),
        ]
        self.assertEqual([item[1] for item in queued], [
            "PRIVMSG #testchannel :one", "PRIVMSG #testchannel :two",
        ])
        client._disconnect_generation(1, "test_complete")

    def test_outbound_sender_preserves_fifo_order_and_primary_destination(self):
        harness = self.client_with_socket()
        client = harness.client
        self.activate(client, harness.tls)
        client._ensure_background_workers()
        self.addCleanup(client.stop)
        completions = [threading.Event() for _ in range(3)]
        lines = [
            "PRIVMSG #testchannel :first",
            "PRIVMSG #testchannel :second",
            "PRIVMSG #testchannel :third",
        ]

        for line, completion in zip(lines, completions):
            self.assertTrue(client._queue_outbound(
                line, 1, completion=completion
            ))
        for completion in completions:
            self.assertTrue(completion.wait(1.0))

        self.assertEqual(
            harness.tls.sent,
            [(line + "\r\n").encode("utf-8") for line in lines],
        )
        self.assertEqual(set(harness.tls.send_threads), {"TwitchIRC-send"})
        client.stop()

    def test_stale_generation_cannot_send_and_stop_joins_workers(self):
        harness = self.client_with_socket()
        client = harness.client
        old_transport = harness.tls
        new_transport = ScriptedSocket()
        self.activate(client, old_transport, generation=1)
        old_started = threading.Event()
        release_old = threading.Event()
        old_finished = threading.Event()
        fresh_processed = threading.Event()

        def process(line):
            if line == "old work":
                old_started.set()
                release_old.wait(1.0)
                client.send_message("stale")
                old_finished.set()
            elif line == "fresh work":
                client.send_message("fresh")
                fresh_processed.set()

        client._process_raw_message = process
        client._ensure_background_workers()
        self.addCleanup(client.stop)
        self.addCleanup(release_old.set)

        client._handle_raw_message("old work", 1)
        self.assertTrue(old_started.wait(1.0))
        self.assertTrue(client._disconnect_generation(1, "synthetic_reconnect"))
        self.activate(client, new_transport, generation=2)

        release_old.set()
        self.assertTrue(old_finished.wait(1.0))
        self.assertEqual(new_transport.sent, [])

        client._handle_raw_message("fresh work", 2)
        self.assertTrue(fresh_processed.wait(1.0))
        self.assertTrue(new_transport.wait_for_sent(1))
        self.assertEqual(
            new_transport.sent,
            [b"PRIVMSG #testchannel :fresh\r\n"],
        )

        client.stop()
        sent_at_stop = list(new_transport.sent)
        client.send_message("after stop")
        self.assertEqual(new_transport.sent, sent_at_stop)
        self.assertFalse(client._has_live_background_workers())

    def test_background_worker_start_is_idempotent(self):
        harness = self.client_with_socket()
        client = harness.client
        self.activate(client, harness.tls)

        client._ensure_background_workers()
        first_workers = list(client._worker_threads)
        first_sender = client._sender_thread
        client._ensure_background_workers()

        self.assertEqual(client._worker_threads, first_workers)
        self.assertIs(client._sender_thread, first_sender)
        self.assertEqual(len(client._worker_threads), client.WORKER_COUNT)
        client.stop()
        self.assertFalse(client._has_live_background_workers())

    def test_timer_reinitialization_keeps_only_one_generation_worker(self):
        harness = self.client_with_socket()
        client = harness.client
        self.activate(client, harness.tls)
        client._timers = [{
            "id": "time-message",
            "type": "time",
            "enabled": True,
            "interval_minutes": 10,
            "message": "synthetic timer",
        }]

        client._init_timers = type(client)._init_timers.__get__(client, type(client))
        client._init_timers(1)
        first = client._timer_thread
        self.assertIsNotNone(first)
        self.assertTrue(first.is_alive())

        client._init_timers(1)
        second = client._timer_thread
        self.assertIsNotNone(second)
        self.assertIsNot(first, second)
        self.assertFalse(first.is_alive())
        self.assertTrue(second.is_alive())

        client.stop()
        self.assertFalse(second.is_alive())


class TwitchGlobalLifecycleTests(unittest.TestCase):
    def test_start_stop_restart_never_creates_duplicate_manager(self):
        tree = ast.parse(LISTENER.read_text(encoding="utf-8"), filename=str(LISTENER))
        functions = [
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name in {"start_listener", "stop_listener"}
        ]
        future = ast.ImportFrom(
            module="__future__", names=[ast.alias(name="annotations")], level=0
        )

        class FakeThread:
            created = []

            def __init__(self, *, target, name, daemon):
                self.target = target
                self.name = name
                self.daemon = daemon
                self.alive = False
                self.created.append(self)

            def start(self):
                self.alive = True

            def is_alive(self):
                return self.alive

            def join(self, _timeout=None):
                self.alive = False

        class FakeClient:
            created = []

            def __init__(self):
                self.stop_calls = 0
                self.created.append(self)

            @staticmethod
            def listen():
                return None

            def stop(self):
                self.stop_calls += 1

            @staticmethod
            def _has_live_background_workers():
                return False

        namespace = {
            "threading": SimpleNamespace(
                Thread=FakeThread,
                current_thread=threading.current_thread,
            ),
            "TwitchIRC": FakeClient,
            "time": time,
            "log": logging.getLogger("test_twitch_global_lifecycle"),
            "_listener_instance": None,
            "_listener_thread": None,
            "_listener_lock": threading.RLock(),
            "_arrived_users": set(),
            "_last_audio_play": {},
        }
        selected = ast.fix_missing_locations(ast.Module(
            body=[future, *functions], type_ignores=[]
        ))
        exec(compile(selected, str(LISTENER), "exec"), namespace)

        namespace["start_listener"]()
        namespace["start_listener"]()
        self.assertEqual(len(FakeClient.created), 1)
        self.assertEqual(len(FakeThread.created), 1)

        first = FakeClient.created[0]
        namespace["stop_listener"]()
        self.assertEqual(first.stop_calls, 1)
        self.assertIsNone(namespace["_listener_instance"])
        self.assertIsNone(namespace["_listener_thread"])

        namespace["start_listener"]()
        self.assertEqual(len(FakeClient.created), 2)
        self.assertEqual(len(FakeThread.created), 2)
        namespace["stop_listener"]()


class CommandCenterIrcStatusTests(unittest.TestCase):
    def test_latest_current_run_state_controls_connection_indicator(self):
        tree = ast.parse(
            COMMAND_CENTER.read_text(encoding="utf-8"), filename=str(COMMAND_CENTER)
        )
        helper = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_irc_connected_after_log_chunk"
        )
        namespace = {}
        selected = ast.fix_missing_locations(ast.Module(
            body=[helper], type_ignores=[]
        ))
        exec(compile(selected, str(COMMAND_CENTER), "exec"), namespace)
        update = namespace["_irc_connected_after_log_chunk"]

        self.assertTrue(update(b"[IRC] State: CONNECTED channel=#test\n"))
        self.assertTrue(update(b"ordinary log output\n", previous=True))
        self.assertFalse(update(
            b"[IRC] State: CONNECTED channel=#test\n"
            b"[IRC] State: RETRYING delay=1.0s\n"
        ))
        self.assertFalse(update(b"[IRC] State: CONNECTING\n", previous=True))
        self.assertTrue(update(
            b"[IRC] State: DISCONNECTED reason=eof\n"
            b"[IRC] State: CONNECTED channel=#test\n"
        ))


if __name__ == "__main__":
    unittest.main()


class ListenerFixtureIsolationTests(unittest.TestCase):
    """Fixture construction is isolated and does not repair parsed locations."""

    def test_parsed_source_never_requires_recursive_location_repair(self):
        from unittest.mock import patch
        with patch.object(ast, "fix_missing_locations", side_effect=AssertionError("recursive repair forbidden")):
            harness = ListenerHarness().client_with_socket()
        self.assertFalse(harness.client.connected)
        self.assertEqual(harness.tls.sent, [])

    def test_separate_fixture_globals_and_class_state_remain_isolated(self):
        first = ListenerHarness().client_with_socket()
        second = ListenerHarness().client_with_socket()
        a, b = type(first.client), type(second.client)
        self.assertIsNot(a, b)
        self.assertIsNot(a.__init__.__globals__, b.__init__.__globals__)
        a.__init__.__globals__["BOT_NICK"] = "ChangedFixture"
        self.assertEqual(b.__init__.__globals__["BOT_NICK"], "TestBot")
        original = b.WORK_QUEUE_SIZE
        a.WORK_QUEUE_SIZE = original + 1
        self.assertEqual(b.WORK_QUEUE_SIZE, original)
        self.assertIsNot(first.socket_module, second.socket_module)

    def test_shared_harness_keeps_real_imports_unless_explicitly_isolated(self):
        shared = ListenerHarness().client_with_socket()
        isolated = IsolatedListenerHarness().client_with_socket()
        shared_builtins = type(shared.client).__init__.__globals__["__builtins__"]
        isolated_builtins = type(isolated.client).__init__.__globals__["__builtins__"]

        self.assertIs(shared_builtins["__import__"], builtins.__import__)
        self.assertIsNot(isolated_builtins["__import__"], builtins.__import__)
