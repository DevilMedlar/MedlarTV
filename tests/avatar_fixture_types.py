"""Test-only types for existing AST-extracted Avatar server globals."""
from __future__ import annotations
from collections.abc import Callable, Coroutine
from typing import Generic, NotRequired, Protocol, TypeVar, TypedDict
from MedlarTV.avatar.bridge.auth import Bootstrap

_Peer = TypeVar('_Peer')

class _Broadcast(Protocol):
    def __call__(self, payload: dict[str, object], channel: str | None = None) -> Coroutine[object, object, None]: ...

class _RunServer(Protocol):
    def __call__(self, host: str = '127.0.0.1', port: int = 8765) -> Coroutine[object, object, None]: ...

class AvatarServerNamespace(TypedDict, Generic[_Peer]):
    ACTIVE_CONNECTIONS: set[_Peer]
    CHANNEL_REGISTRY: dict[str, list[_Peer]]
    CONNECTION_ROLES: dict[_Peer, str]
    LATEST_MOOD: dict[str, object] | None
    SERVER_SESSION: Bootstrap | None
    handler: Callable[[_Peer], Coroutine[object, object, None]]
    register: Callable[[_Peer], Coroutine[object, object, bool]]
    unregister: Callable[[_Peer], Coroutine[object, object, None]]
    _send_target: Callable[[_Peer, str], Coroutine[object, object, bool]]
    _subscribe: Callable[[_Peer, str], bool]
    _remove_subscription: Callable[[_Peer, str], None]
    broadcast: _Broadcast
    run_server: _RunServer
    _install_cooperative_stop: object
    websockets: NotRequired[object]
