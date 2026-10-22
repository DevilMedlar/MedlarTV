"""Virtual monotonic chat clock; no real sleeping or guard changes."""
from __future__ import annotations
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    class _ChatClockTarget(Protocol):
        _chat_clock: Callable[[], float]
        _chat_wait: Callable[[float], bool]
        _pacing_fixture: FakeChatClock


class FakeChatClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []
        self.on_wait: Callable[[], object] | None = None

    def read(self) -> float:
        return self.now

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        self.now += seconds
        if self.on_wait is not None:
            callback, self.on_wait = self.on_wait, None
            callback()
        return False


def install_fake_chat_clock(client: object) -> FakeChatClock:
    # Existing fixture injection writes these three attributes onto the client.
    target = cast("_ChatClockTarget", client)
    clock = FakeChatClock()
    # Intentional white-box clock injection; keep the production hook names.
    target._chat_clock = clock.read  # pyright: ignore[reportPrivateUsage]
    target._chat_wait = clock.wait  # pyright: ignore[reportPrivateUsage]
    target._pacing_fixture = clock  # pyright: ignore[reportPrivateUsage]
    return clock
