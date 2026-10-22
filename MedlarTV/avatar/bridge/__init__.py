"""
MedlarTV Bridge Module
WebSocket communication bridge between components
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import start_bridge_loop as start_bridge_loop, ws_send as ws_send, register_channel as register_channel, send_mood_update as send_mood_update
    from .server import run_server as run_server, broadcast as broadcast
    from .common import BridgeEvent as BridgeEvent, MESSAGE_TYPES as MESSAGE_TYPES

def __getattr__(name: str) -> object:
    if name in {"run_server", "broadcast"}:
        from . import server
        return getattr(server, name)
    if name in {"BridgeEvent", "MESSAGE_TYPES"}:
        from . import common
        return getattr(common, name)
    if name in __all__:
        from . import client
        return getattr(client, name)
    raise AttributeError(name)

__all__ = [
    'run_server',
    'broadcast',
    'start_bridge_loop',
    'ws_send',
    'register_channel',
    'send_mood_update',
    'BridgeEvent',
    'MESSAGE_TYPES',
]
