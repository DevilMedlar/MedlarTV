"""Offline TLS and Core startup checks; no project imports or live connections."""

from __future__ import annotations

import ast
import builtins
import logging
import ssl
import threading
import time
import unittest
from pathlib import Path
from types import FunctionType, SimpleNamespace
from collections.abc import Callable
from typing import Literal, Protocol, cast, overload, runtime_checkable
from unittest.mock import Mock, call


ROOT = Path(__file__).resolve().parents[1]


class _Client(Protocol):
    CONNECT_TIMEOUT: float
    SOCKET_POLL_TIMEOUT: float
    connected: bool
    sock: object | None
    _init_emotes: Callable[[], None]
    _init_timers: Callable[[], None]

    def connect(self) -> int: ...


@runtime_checkable
class _ClientFactory(Protocol):
    def __call__(self) -> _Client: ...


def twitch_getenv(key: str, default: str = "") -> str:
    return "false" if key == "ENABLE_ONLINE_OFFLINE_MSGS" else default


def default_getenv(_key: str, default: str = "") -> str:
    return default


def active_copilots() -> list[str]:
    return ["@Copilot"]


def empty_timers() -> list[object]:
    return []


def tls_copilot_channels(_primary: object, copilots: object) -> list[str]:
    """Small transport-only stand-in; authority behavior has its own real tests."""
    return ["#copilot"] if list(copilots) else []


def isolated_builtins() -> dict[str, object]:
    """Permit only the two intentional Medlar imports performed by connect()."""
    source = ROOT / "MedlarTV/core/irc_framing.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.ClassDef) and item.name == "IRCLineBuffer"
    )
    framing_namespace: dict[str, object] = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), framing_namespace)
    framing_module = SimpleNamespace(IRCLineBuffer=framing_namespace["IRCLineBuffer"])
    authority_module = SimpleNamespace(copilot_channels=tls_copilot_channels)
    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "MedlarTV.core.irc_framing":
            return framing_module
        if name == "MedlarTV.command_authority":
            return authority_module
        if name.startswith("MedlarTV"):
            raise AssertionError(f"Unexpected Medlar import in TLS fixture: {name}")
        return real_import(name, globals, locals, fromlist, level)

    namespace = dict(vars(builtins))
    namespace["__import__"] = guarded_import
    return namespace


@overload
def load_definition(path: str, name: Literal["TwitchIRC"], namespace: dict[str, object]) -> _ClientFactory: ...


@overload
def load_definition(path: str, name: Literal["main"], namespace: dict[str, object]) -> Callable[[], None]: ...


def load_definition(path: str, name: Literal["TwitchIRC", "main"], namespace: dict[str, object]) -> _ClientFactory | Callable[[], None]:
    """Compile the real definition without importing modules with startup effects."""
    source = ROOT / path
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    node = next(
        item for item in tree.body
        if isinstance(item, (ast.ClassDef, ast.FunctionDef)) and item.name == name
    )
    # Parsed nodes already have full locations; compile inherits future annotations.
    selected = ast.Module(body=[node], type_ignores=[])
    exec(compile(selected, str(source), "exec"), namespace)
    definition = namespace[name]
    if name == "TwitchIRC":
        assert isinstance(node, ast.ClassDef), "Expected extracted client class"
        assert isinstance(definition, _ClientFactory), "Expected callable client factory"
        return definition
    assert isinstance(node, ast.FunctionDef), "Expected extracted main function"
    assert isinstance(definition, FunctionType), "Expected compiled main function"
    return cast(Callable[[], None], definition)


class TwitchTLSConnectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw_socket = Mock(name="raw_socket")
        self.tls_socket = Mock(name="tls_socket")
        self.tls_socket.recv.side_effect = [
            b":tmi.twitch.tv 001 TestBot :Welcome, GLHF!\r\n",
            b":TestBot!testbot@testbot.tmi.twitch.tv JOIN #testchannel\r\n",
        ]
        self.context = SimpleNamespace(
            check_hostname=True,
            verify_mode=ssl.CERT_REQUIRED,
            wrap_socket=Mock(name="wrap_socket", return_value=self.tls_socket),
        )
        self.socket_module = SimpleNamespace(
            socket=Mock(return_value=self.raw_socket),
            SHUT_RDWR=2,
        )
        self.ssl_module = SimpleNamespace(create_default_context=Mock(return_value=self.context))
        self.flow = Mock(name="connection_flow")
        self.flow.attach_mock(self.raw_socket.settimeout, "raw_settimeout")
        self.flow.attach_mock(self.raw_socket.connect, "raw_connect")
        self.flow.attach_mock(self.context.wrap_socket, "wrap_socket")
        self.flow.attach_mock(self.tls_socket.settimeout, "tls_settimeout")
        self.flow.attach_mock(self.tls_socket.sendall, "tls_sendall")
        namespace: dict[str, object] = {
            "__builtins__": isolated_builtins(),
            "socket": self.socket_module,
            "ssl": self.ssl_module,
            "threading": threading,
            "time": time,
            "os": SimpleNamespace(getenv=twitch_getenv),
            "log": logging.getLogger("test_twitch_tls"),
            "TWITCH_SERVER": "irc.chat.twitch.tv",
            "TWITCH_PORT": 6697,
            "IRC_TOKEN": "oauth:test-only",
            "BOT_NICK": "TestBot",
            "CHANNEL": "#testchannel",
            "_get_active_copilots": active_copilots,
            "_load_timers_yaml": empty_timers,
        }
        twitch_irc = load_definition("MedlarTV/tools/twitch_listener.py", "TwitchIRC", namespace)
        self.client = twitch_irc()
        self.client._init_emotes = Mock()  # pyright: ignore[reportPrivateUsage] - Replace emote startup with inert mock.
        self.client._init_timers = Mock()  # pyright: ignore[reportPrivateUsage] - Replace timer startup with inert mock.

    def test_verified_context_and_twitch_login_order(self) -> None:
        self.client.connect()

        self.ssl_module.create_default_context.assert_called_once_with()
        self.assertTrue(self.context.check_hostname)
        self.assertEqual(self.context.verify_mode, ssl.CERT_REQUIRED)
        self.context.wrap_socket.assert_called_once_with(
            self.raw_socket, server_hostname="irc.chat.twitch.tv"
        )
        self.raw_socket.settimeout.assert_called_once_with(self.client.CONNECT_TIMEOUT)
        self.raw_socket.connect.assert_called_once_with(("irc.chat.twitch.tv", 6697))
        self.tls_socket.settimeout.assert_called_once_with(self.client.SOCKET_POLL_TIMEOUT)
        self.tls_socket.connect.assert_not_called()
        self.raw_socket.sendall.assert_not_called()
        self.assertEqual(self.flow.mock_calls[:4], [
            call.raw_settimeout(self.client.CONNECT_TIMEOUT),
            call.raw_connect(("irc.chat.twitch.tv", 6697)),
            call.wrap_socket(self.raw_socket, server_hostname="irc.chat.twitch.tv"),
            call.tls_settimeout(self.client.SOCKET_POLL_TIMEOUT),
        ])
        self.assertEqual(self.tls_socket.sendall.call_args_list, [
            call(b"PASS oauth:test-only\r\n"),
            call(b"NICK TestBot\r\n"),
            call(b"CAP REQ :twitch.tv/commands twitch.tv/tags twitch.tv/membership\r\n"),
            call(b"JOIN #testchannel\r\n"),
            call(b"JOIN #copilot\r\n"),
        ])
        self.assertTrue(self.client.connected)
        self.assertIs(self.client.sock, self.tls_socket)

    def test_raw_timeout_failure_closes_unpublished_socket(self) -> None:
        self.raw_socket.settimeout.side_effect = OSError("mock raw timeout failure")

        with self.assertRaisesRegex(OSError, "mock raw timeout failure"):
            self.client.connect()

        self.raw_socket.close.assert_called_once_with()
        self.raw_socket.connect.assert_not_called()
        self.context.wrap_socket.assert_not_called()
        self.raw_socket.sendall.assert_not_called()
        self.tls_socket.sendall.assert_not_called()
        self.assertFalse(self.client.connected)
        self.assertIsNone(self.client.sock)

    def test_raw_connect_failure_closes_published_socket(self) -> None:
        self.raw_socket.connect.side_effect = OSError("mock raw connect failure")

        with self.assertRaisesRegex(OSError, "mock raw connect failure"):
            self.client.connect()

        self.raw_socket.close.assert_called_once_with()
        self.context.wrap_socket.assert_not_called()
        self.raw_socket.sendall.assert_not_called()
        self.tls_socket.sendall.assert_not_called()
        self.assertFalse(self.client.connected)
        self.assertIsNone(self.client.sock)

    def test_wrap_failure_closes_connected_raw_socket(self) -> None:
        self.context.wrap_socket.side_effect = OSError("mock TLS failure")

        with self.assertRaisesRegex(OSError, "mock TLS failure"):
            self.client.connect()

        self.raw_socket.close.assert_called_once_with()
        self.raw_socket.connect.assert_called_once_with(("irc.chat.twitch.tv", 6697))
        self.raw_socket.sendall.assert_not_called()
        self.tls_socket.sendall.assert_not_called()
        self.assertFalse(self.client.connected)
        self.assertIsNone(self.client.sock)

    def test_tls_poll_timeout_failure_closes_wrapped_socket(self) -> None:
        self.tls_socket.settimeout.side_effect = OSError("mock poll timeout failure")

        with self.assertRaisesRegex(OSError, "mock poll timeout failure"):
            self.client.connect()

        self.tls_socket.close.assert_called_once_with()
        self.raw_socket.sendall.assert_not_called()
        self.tls_socket.sendall.assert_not_called()
        self.assertFalse(self.client.connected)
        self.assertIsNone(self.client.sock)

    def test_login_failure_closes_tls_socket(self) -> None:
        self.tls_socket.sendall.side_effect = OSError("mock send failure")

        with self.assertRaisesRegex(OSError, "mock send failure"):
            self.client.connect()

        self.tls_socket.close.assert_called_once_with()
        self.assertEqual(
            self.tls_socket.sendall.call_args_list,
            [call(b"PASS oauth:test-only\r\n")],
        )
        self.assertFalse(self.client.connected)
        self.assertIsNone(self.client.sock)

    def test_wrap_failure_then_fresh_second_attempt_connects(self) -> None:
        first_raw = Mock(name="first_raw_socket")
        second_raw = Mock(name="second_raw_socket")
        first_context = SimpleNamespace(
            check_hostname=True,
            verify_mode=ssl.CERT_REQUIRED,
            wrap_socket=Mock(side_effect=OSError("first wrap failure")),
        )
        self.socket_module.socket.side_effect = [first_raw, second_raw]
        self.ssl_module.create_default_context.side_effect = [first_context, self.context]

        with self.assertRaisesRegex(OSError, "first wrap failure"):
            self.client.connect()
        generation = self.client.connect()

        self.assertEqual(self.socket_module.socket.call_count, 2)
        self.assertEqual(self.ssl_module.create_default_context.call_count, 2)
        first_context.wrap_socket.assert_called_once_with(
            first_raw, server_hostname="irc.chat.twitch.tv",
        )
        self.context.wrap_socket.assert_called_once_with(
            second_raw, server_hostname="irc.chat.twitch.tv",
        )
        first_raw.close.assert_called_once_with()
        first_raw.sendall.assert_not_called()
        second_raw.close.assert_not_called()
        second_raw.sendall.assert_not_called()
        self.assertEqual(generation, 3)
        self.assertTrue(self.client.connected)
        self.assertIs(self.client.sock, self.tls_socket)

    def test_cancellation_during_wrap_closes_returned_tls_without_login(self) -> None:
        def cancel_then_return(_socket, **_kwargs):
            self.client.stop()
            return self.tls_socket

        self.context.wrap_socket.side_effect = cancel_then_return

        with self.assertRaisesRegex(self.client.Cancelled, "cancelled"):
            self.client.connect()

        self.raw_socket.close.assert_called_once_with()
        self.tls_socket.close.assert_called_once_with()
        self.raw_socket.sendall.assert_not_called()
        self.tls_socket.sendall.assert_not_called()
        self.assertFalse(self.client.connected)
        self.assertIsNone(self.client.sock)


class CoreEntrypointSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[str] = []
        self.configure = Mock(side_effect=lambda: self.calls.append("configure"))
        self.initialize = Mock(side_effect=lambda: self.calls.append("initialize"))
        self.listener = Mock(side_effect=lambda: self.calls.append("listener"))
        self.sleep = Mock(side_effect=KeyboardInterrupt())
        self.shutdown = Mock(side_effect=SystemExit(0))
        namespace: dict[str, object] = {
            "os": SimpleNamespace(getenv=default_getenv),
            "log": logging.getLogger("test_core_entrypoint"),
            "configure_logging": self.configure,
            "initialize_systems": self.initialize,
            "start_listener": self.listener,
            "time": SimpleNamespace(sleep=self.sleep),
            "shutdown": self.shutdown,
            "_stop_requested": lambda: False,
        }
        self.main = load_definition("MedlarTV/core/main.py", "main", namespace)

    def test_starts_listener_after_initialization_then_handles_ctrl_c(self) -> None:
        with self.assertRaises(SystemExit) as result:
            self.main()

        self.assertEqual(result.exception.code, 0)
        self.assertEqual(self.calls, ["configure", "initialize", "listener"])
        self.sleep.assert_called_once_with(1)
        self.shutdown.assert_called_once_with()

    def test_listener_failure_propagates_without_reporting_operational(self) -> None:
        self.listener.side_effect = RuntimeError("mock listener failure")

        with self.assertRaisesRegex(RuntimeError, "mock listener failure"):
            self.main()

        self.assertEqual(self.calls, ["configure", "initialize"])
        self.sleep.assert_not_called()
        self.shutdown.assert_not_called()


if __name__ == "__main__":
    unittest.main()
