"""Compatibility helpers backed by the Core-owned authenticated publisher."""
from .runtime import current_publisher


def is_bridge_available() -> bool:
    publisher = current_publisher()
    return publisher is not None and publisher.ready


def send_mood_update(mood: object) -> bool:
    publisher = current_publisher()
    return publisher is not None and publisher.offer(mood)


def ws_send(payload: object) -> bool:
    from typing import cast
    if not isinstance(payload, dict) or cast(dict[object, object], payload).get("event") != "mood_update":
        return False
    publisher = current_publisher()
    return (publisher is not None and cast(dict[object, object], payload).get("channel", publisher.bootstrap.channel) == publisher.bootstrap.channel
            and publisher.offer(cast(dict[object, object], payload).get("mood")))


def register_channel(channel: object, platform: str = "twitch") -> bool:
    # Membership belongs to the persistent authenticated consumer connection.
    return False


def start_bridge_loop() -> None:
    import asyncio
    from MedlarTV.avatar_client.console_client import listen
    asyncio.run(listen())
