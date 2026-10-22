"""
MedlarTV Avatar Module
WebSocket bridge for real-time communication between components.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge.client import start_bridge_loop as start_bridge_loop, ws_send as ws_send, register_channel as register_channel, send_mood_update as send_mood_update

def __getattr__(name: str) -> object:
    if name in __all__:
        from .bridge import client
        return getattr(client, name)
    raise AttributeError(name)

__all__ = [
    'start_bridge_loop',
    'ws_send',
    'register_channel',
    'send_mood_update',
]
