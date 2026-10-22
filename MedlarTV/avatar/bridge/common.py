"""
Bridge Common - Shared constants and types
"""

print("[DEBUG bridge_common] Module import started")

from enum import Enum
from typing import TypedDict, Literal

print("[DEBUG bridge_common] Imported Enum, TypedDict, Literal")


class BridgeEvent(str, Enum):
    """Bridge event types"""
    print("[DEBUG bridge_common] Defining Enum class BridgeEvent")

    HANDSHAKE = "handshake"
    REGISTER = "register"
    UNREGISTER = "unregister"
    MOOD_UPDATE = "mood_update"
    CHAT_MESSAGE = "chat_message"
    SYSTEM_EVENT = "system_event"

print("[DEBUG bridge_common] Enum BridgeEvent created with members:",
      list(BridgeEvent._member_names_))


MESSAGE_TYPES = Literal[
    "handshake",
    "register",
    "unregister",
    "mood_update",
    "chat_message",
    "system_event"
]

print("[DEBUG bridge_common] MESSAGE_TYPES Literal defined")


class BridgeMessage(TypedDict, total=False):
    """Bridge message structure"""
    print("[DEBUG bridge_common] Defining TypedDict BridgeMessage")
    event: str
    channel: str
    mood: str
    msg: str
    platform: str
    data: dict

print("[DEBUG bridge_common] TypedDict BridgeMessage created")


print("[DEBUG bridge_common] Module import complete")
