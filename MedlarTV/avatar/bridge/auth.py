"""Per-launch Avatar capabilities. Secrets exist only in pipes and memory."""
from __future__ import annotations
from typing import TYPE_CHECKING, Literal, Protocol, TypeGuard, cast, overload
from dataclasses import dataclass, field
import hmac
import json
import logging
import os
import re
import secrets
import sys
from urllib.parse import urlsplit

if TYPE_CHECKING:
    class _BootstrapStream(Protocol):
        def readline(self, limit: int = -1, /) -> bytes: ...

    class _AuthWebsocket(Protocol):
        async def send(self, message: str) -> None: ...
        async def recv(self) -> str | bytes: ...


BOOTSTRAP_FLAG = "MEDLARTV_AVATAR_BOOTSTRAP"
BOOTSTRAP_LIMIT = 2048
AUTH_TIMEOUT = 3.0
IO_TIMEOUT = 2.0


class BootstrapError(ValueError):
    """A fixed, secret-free startup error."""


@dataclass(frozen=True)
class Bootstrap:
    role: str
    channel: str
    url: str
    key: str = field(default="", repr=False)
    publisher_key: str = field(default="", repr=False)
    subscriber_key: str = field(default="", repr=False)


def _key_valid(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def normalize_channel(value: object) -> str:
    if not isinstance(value, str):
        raise BootstrapError("Invalid Avatar channel")
    value = value.strip().lower()
    if not value.startswith("#"):
        value = "#" + value
    if not re.fullmatch(r"#[a-z0-9_]{1,99}", value):
        raise BootstrapError("Invalid Avatar channel")
    return value


def parse_bootstrap(data: object, expected_role: str) -> Bootstrap:
    if not isinstance(data, dict) or cast(dict[object, object], data).get("version") != 1 or cast(dict[object, object], data).get("role") != expected_role:
        raise BootstrapError("Invalid Avatar bootstrap")
    mapping = cast(dict[object, object], data)
    channel = normalize_channel(mapping.get("channel"))
    url = mapping.get("url")
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
        valid_url = (parsed is not None and parsed.scheme == "ws" and parsed.hostname == "127.0.0.1"
                     and parsed.port is not None and 1 <= parsed.port <= 65535
                     and not parsed.username and not parsed.password and not parsed.query
                     and not parsed.fragment and parsed.path in ("", "/"))
    except ValueError:
        valid_url = False
    if not valid_url:
        raise BootstrapError("Invalid Avatar endpoint")
    # The endpoint checks establish text; keep its original value unchanged.
    url = cast(str, url)
    if expected_role == "server":
        pub, sub = mapping.get("publisher_key"), mapping.get("subscriber_key")
        if not _key_valid(pub) or not _key_valid(sub) or hmac.compare_digest(pub, sub):
            raise BootstrapError("Invalid Avatar capabilities")
        return Bootstrap(expected_role, channel, url, publisher_key=pub, subscriber_key=sub)
    if expected_role not in ("publisher", "subscriber") or not _key_valid(mapping.get("key")):
        raise BootstrapError("Invalid Avatar capability")
    return Bootstrap(expected_role, channel, url, key=cast(str, mapping["key"]))


def create_session(channel: str, url: str) -> Bootstrap:
    """Called only by an explicitly started launcher, never at import time."""
    return parse_bootstrap({"version": 1, "role": "server", "channel": channel, "url": url,
                            "publisher_key": secrets.token_hex(32), "subscriber_key": secrets.token_hex(32)}, "server")


def child_payload(session: Bootstrap, role: str) -> dict[str, str | int]:
    data: dict[str, str | int] = {"version": 1, "role": role, "channel": session.channel, "url": session.url}
    if role == "server":
        data.update(publisher_key=session.publisher_key, subscriber_key=session.subscriber_key)
    elif role == "publisher":
        data["key"] = session.publisher_key
    elif role == "subscriber":
        data["key"] = session.subscriber_key
    else:
        raise BootstrapError("Invalid Avatar role")
    return data


def encode_bootstrap(data: dict[str, str | int]) -> bytes:
    encoded = (json.dumps(data, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > BOOTSTRAP_LIMIT:
        raise BootstrapError("Avatar bootstrap too large")
    return encoded


@overload
def read_bootstrap(role: str, *, required: Literal[True], stream: _BootstrapStream | None = None, enabled: bool | None = None) -> Bootstrap: ...

@overload
def read_bootstrap(role: str, *, required: Literal[False] = False, stream: _BootstrapStream | None = None, enabled: bool | None = None) -> Bootstrap | None: ...

@overload
def read_bootstrap(role: str, *, required: bool, stream: _BootstrapStream | None = None, enabled: bool | None = None) -> Bootstrap | None: ...


def read_bootstrap(role: str, *, required: bool = False, stream: _BootstrapStream | None = None, enabled: bool | None = None) -> Bootstrap | None:
    if enabled is None:
        enabled = os.getenv(BOOTSTRAP_FLAG, "") == "1"
    if not enabled:
        if required:
            raise BootstrapError("Authenticated Avatar startup required")
        return None
    reader: _BootstrapStream = stream if stream is not None else sys.stdin.buffer
    try:
        raw = reader.readline(BOOTSTRAP_LIMIT + 1)
        if not raw or len(raw) > BOOTSTRAP_LIMIT or not raw.endswith(b"\n"):
            raise BootstrapError("Invalid Avatar bootstrap")
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise BootstrapError("Invalid Avatar bootstrap") from None
    return parse_bootstrap(data, role)


def credential_role(session: Bootstrap | None, token: object) -> Literal["publisher", "subscriber"] | None:
    if session is None or session.role != "server" or not _key_valid(token):
        return None
    # Evaluate both comparisons; the requested role isn't an authority.
    publisher = hmac.compare_digest(token, session.publisher_key)
    subscriber = hmac.compare_digest(token, session.subscriber_key)
    return "publisher" if publisher else "subscriber" if subscriber else None


def transport_logger() -> logging.Logger:
    # A dedicated logger prevents WebSocket frame/header traces, even with root DEBUG.
    logger = logging.getLogger("medlar.avatar.transport")
    logger.disabled = True
    return logger


async def authenticate_client(websocket: _AuthWebsocket, bootstrap: Bootstrap) -> None:
    import asyncio
    await asyncio.wait_for(websocket.send(json.dumps({"event": "authenticate", "token": bootstrap.key})), IO_TIMEOUT)
    raw = await asyncio.wait_for(websocket.recv(), AUTH_TIMEOUT)
    try:
        response: object = json.loads(raw)
        valid = (isinstance(response, dict) and cast(dict[object, object], response).get("event") == "handshake"
                 and cast(dict[object, object], response).get("msg") == "connected" and cast(dict[object, object], response).get("role") == bootstrap.role
                 and cast(dict[object, object], response).get("channel") == bootstrap.channel)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        valid = False
    if not valid:
        raise BootstrapError("Avatar authentication failed")
