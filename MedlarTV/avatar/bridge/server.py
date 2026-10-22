"""Authenticated, bounded, local Avatar relay."""
from __future__ import annotations
from typing import TYPE_CHECKING, Protocol, TypeGuard

import asyncio
import json
import os
import signal
import unicodedata
import websockets
from MedlarTV.avatar.bridge.auth import (
    AUTH_TIMEOUT, Bootstrap, BootstrapError, credential_role, read_bootstrap, transport_logger,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping
    from types import FrameType

    class _RelaySocket(Protocol):
        async def send(self, message: str) -> None: ...
        async def recv(self) -> str | bytes: ...
        async def close(self, code: int = 1000, reason: str = "") -> None: ...
        def __aiter__(self) -> AsyncIterator[str | bytes]: ...


DEBUG = os.getenv("MEDLARTV_DEBUG", "false").lower() == "true"
ACTIVE_CONNECTIONS: set[_RelaySocket] = set()
CHANNEL_REGISTRY: dict[str, list[_RelaySocket]] = {}
CONNECTION_ROLES: dict[_RelaySocket, str] = {}
SERVER_SESSION: Bootstrap | None = None
LATEST_MOOD: dict[str, str] | None = None
MAX_CONNECTIONS = 64
MAX_SUBSCRIPTIONS = 32
MAX_CHANNEL_LENGTH = 100
MAX_MOOD_LENGTH = 64
MAX_MESSAGE_SIZE = 16 * 1024
MAX_QUEUE = 16
SEND_TIMEOUT = 2.0


def configure_session(session: Bootstrap) -> None:
    global SERVER_SESSION, LATEST_MOOD
    if session.role != "server" or ACTIVE_CONNECTIONS:
        raise BootstrapError("Invalid Avatar server startup")
    SERVER_SESSION, LATEST_MOOD = session, None


def _valid_text(value: object, minimum: int, maximum: int) -> TypeGuard[str]:
    return (isinstance(value, str) and minimum <= len(value) <= maximum
            and all(unicodedata.category(c) not in ("Cc", "Cs") for c in value))


def _remove_subscription(websocket: _RelaySocket, channel: str) -> None:
    peers = CHANNEL_REGISTRY.get(channel)
    if peers is None:
        return
    remaining = [peer for peer in peers if peer is not websocket]
    if remaining:
        CHANNEL_REGISTRY[channel] = remaining
    else:
        del CHANNEL_REGISTRY[channel]


def _subscribe(websocket: _RelaySocket, channel: str) -> bool:
    if websocket in CHANNEL_REGISTRY.get(channel, []):
        return True
    if sum(websocket in peers for peers in CHANNEL_REGISTRY.values()) >= MAX_SUBSCRIPTIONS:
        return False
    CHANNEL_REGISTRY.setdefault(channel, []).append(websocket)
    return True


async def register(websocket: _RelaySocket) -> bool:
    # Pending authentication consumes a slot too.
    if websocket not in ACTIVE_CONNECTIONS and len(ACTIVE_CONNECTIONS) >= MAX_CONNECTIONS:
        await websocket.close(code=1013, reason="Avatar Bridge connection limit reached")
        return False
    ACTIVE_CONNECTIONS.add(websocket)
    return True


async def unregister(websocket: _RelaySocket) -> None:
    ACTIVE_CONNECTIONS.discard(websocket)
    CONNECTION_ROLES.pop(websocket, None)
    for channel in list(CHANNEL_REGISTRY):
        _remove_subscription(websocket, channel)


async def _authenticate(websocket: _RelaySocket) -> str | None:
    from typing import cast
    try:
        raw = await asyncio.wait_for(websocket.recv(), AUTH_TIMEOUT)
        data: object = json.loads(raw)
        role = credential_role(SERVER_SESSION, cast(dict[object, object], data).get("token")) if (
            isinstance(data, dict) and cast(dict[object, object], data).get("event") == "authenticate") else None
    except Exception:
        role = None
    if role is None:
        await websocket.close(code=1008, reason="Avatar authentication required")
        return None
    CONNECTION_ROLES[websocket] = role
    await asyncio.wait_for(websocket.send(json.dumps({
        "event": "handshake", "msg": "connected", "role": role, "channel": cast("Bootstrap", SERVER_SESSION).channel,
    })), SEND_TIMEOUT)
    return role


async def _send_target(websocket: _RelaySocket, message: str) -> bool:
    try:
        await asyncio.wait_for(websocket.send(message), timeout=SEND_TIMEOUT)
        return True
    except Exception as exc:
        if DEBUG:
            print(f"[Avatar] Recipient send failed: {type(exc).__name__}")
        return False


async def broadcast(payload: Mapping[str, object], channel: str | None = None) -> None:
    # Publishers and pending/unauthenticated peers never receive mood data.
    channel = channel.lower() if channel else channel
    targets: set[_RelaySocket] | list[_RelaySocket] = set(CHANNEL_REGISTRY.get(channel, [])) if channel else set(ACTIVE_CONNECTIONS)
    targets = [peer for peer in targets if CONNECTION_ROLES.get(peer) == "subscriber"]
    if targets:
        await asyncio.gather(*(_send_target(peer, json.dumps(payload)) for peer in targets),
                             return_exceptions=True)


async def handler(websocket: _RelaySocket) -> None:
    from typing import cast
    global LATEST_MOOD
    try:
        if SERVER_SESSION is None:
            await websocket.close(code=1008, reason="Avatar authentication unavailable")
            return
        if not await register(websocket):
            return
        role = await _authenticate(websocket)
        if role is None:
            return
        async for message in websocket:
            try:
                data: object = json.loads(message)
            except (ValueError, UnicodeError, RecursionError):
                continue
            if not isinstance(data, dict):
                continue
            event = cast(dict[object, object], data).get("event")
            # No frame contents, credentials, headers, or exception bodies are logged.
            if event in ("register", "unregister"):
                if role != "subscriber":
                    await websocket.close(code=1008, reason="Avatar role not permitted")
                    return
                channel = cast(dict[object, object], data).get("channel")
                if not _valid_text(channel, 1, MAX_CHANNEL_LENGTH):
                    continue
                channel = channel.lower()
                if channel != SERVER_SESSION.channel:
                    await websocket.close(code=1008, reason="Avatar channel not permitted")
                    return
                if event == "unregister":
                    _remove_subscription(websocket, channel)
                elif _subscribe(websocket, channel) and LATEST_MOOD is not None:
                    await _send_target(websocket, json.dumps(LATEST_MOOD))
            elif event == "mood_update":
                if role != "publisher":
                    await websocket.close(code=1008, reason="Avatar role not permitted")
                    return
                mood, channel = cast(dict[object, object], data).get("mood"), cast(dict[object, object], data).get("channel")
                if not _valid_text(mood, 1, MAX_MOOD_LENGTH):
                    continue
                if not _valid_text(channel, 1, MAX_CHANNEL_LENGTH):
                    continue
                if channel.lower() != SERVER_SESSION.channel:
                    await websocket.close(code=1008, reason="Avatar channel not permitted")
                    return
                payload = {"event": "mood_update", "mood": mood}
                if payload != LATEST_MOOD:
                    LATEST_MOOD = payload
                    await broadcast(payload, SERVER_SESSION.channel)
    finally:
        await unregister(websocket)


def _install_cooperative_stop(stop: asyncio.Future[None]) -> Callable[[], None]:
    """Complete the server wait on its loop rather than interrupt asyncio internals."""
    event = getattr(signal, "SIGBREAK", None)
    if event is None:
        return lambda: None
    loop = asyncio.get_running_loop()
    requested = False

    def finish_stop() -> None:
        if not stop.done():
            stop.set_result(None)

    def request_stop(signum: int, frame: FrameType | None) -> None:
        nonlocal requested
        if requested:
            return
        requested = True
        print("[Avatar] Cooperative stop requested", flush=True)
        loop.call_soon_threadsafe(finish_stop)

    try:
        previous = signal.getsignal(event)
        signal.signal(event, request_stop)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"[Avatar] Cooperative handler unavailable: {type(exc).__name__}", flush=True)
        return lambda: None

    def restore() -> None:
        try:
            signal.signal(event, previous)
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"[Avatar] Handler restore unavailable: {type(exc).__name__}", flush=True)
    return restore


async def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    if SERVER_SESSION is None:
        raise BootstrapError("Authenticated Avatar startup required")
    stop: asyncio.Future[None] = asyncio.Future()
    restore = _install_cooperative_stop(stop)
    try:
        async with websockets.serve(handler, host, port, max_size=MAX_MESSAGE_SIZE, max_queue=MAX_QUEUE,
                                    logger=transport_logger(), origins=[None], close_timeout=1):
            await stop
    finally:
        restore()


if __name__ == "__main__":
    try:
        configure_session(read_bootstrap("server", required=True))
        host = os.getenv("BRIDGE_HOST", "127.0.0.1")
        port = int(os.getenv("BRIDGE_PORT", "8765"))
        asyncio.run(run_server(host, port))
    except (BootstrapError, ValueError):
        print("[Avatar] Authenticated startup unavailable")
        raise SystemExit(2) from None
