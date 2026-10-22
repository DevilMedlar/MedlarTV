"""Authenticated text-only Avatar consumer with bounded reconnect."""
from __future__ import annotations
from typing import TYPE_CHECKING, Protocol

import asyncio
import json
import time
from colorama import init, Fore, Style
from MedlarTV.avatar.bridge.auth import (
    Bootstrap, BootstrapError, IO_TIMEOUT, authenticate_client, read_bootstrap, transport_logger,
)
from MedlarTV.avatar.bridge.runtime import valid_mood

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from contextlib import AbstractAsyncContextManager
    import logging

    class _ConsoleSocket(Protocol):
        async def send(self, message: str) -> None: ...
        async def recv(self) -> str | bytes: ...
        def __aiter__(self) -> AsyncIterator[str | bytes]: ...

    class _ConsoleConnector(Protocol):
        def __call__(self, uri: str, *, proxy: None, open_timeout: float,
                     close_timeout: float, ping_interval: None,
                     logger: logging.Logger) -> AbstractAsyncContextManager[_ConsoleSocket]: ...


MOOD_COLORS = {
    "hype": Fore.RED + Style.BRIGHT,
    "chill": Fore.CYAN,
    "snarky": Fore.MAGENTA,
    "supportive": Fore.YELLOW + Style.BRIGHT,
}
MOOD_ANIMATIONS = {
    "hype": "*pumps fist in the air* LET'S GOOOO!!!",
    "chill": "*leans back, calm and steady* just vibin'...",
    "snarky": "*rolls eyes, smirking slightly* lol sure",
    "supportive": "*offers a warm nod and a smile* You got this!",
}


def render_mood(mood: object) -> None:
    if not valid_mood(mood):
        return
    color = MOOD_COLORS.get(mood, Fore.WHITE)
    print(color + f"[{time.strftime('%H:%M:%S')}] MedlarTV MODE SHIFT -> {mood.upper()}")
    animation = MOOD_ANIMATIONS.get(mood, "")
    if animation:
        print(color + animation)


async def listen(bootstrap: Bootstrap | None = None, *, connector: _ConsoleConnector | None = None) -> None:
    from typing import cast
    bootstrap = bootstrap or read_bootstrap("subscriber", required=True)
    if bootstrap.role != "subscriber":
        raise BootstrapError("Subscriber capability required")
    if connector is None:
        from websockets.asyncio.client import connect
        connector = connect
    delay = 1.0
    while True:
        try:
            async with connector(bootstrap.url, proxy=None, open_timeout=IO_TIMEOUT, close_timeout=1,
                                 ping_interval=None, logger=transport_logger()) as websocket:
                await authenticate_client(websocket, bootstrap)
                await asyncio.wait_for(websocket.send(json.dumps({
                    "event": "register", "channel": bootstrap.channel,
                })), IO_TIMEOUT)
                delay = 1.0
                print("[Avatar] Authenticated console connected")
                async for raw in websocket:
                    try:
                        data: object = json.loads(raw)
                    except (ValueError, UnicodeError, RecursionError):
                        continue
                    if isinstance(data, dict) and cast(dict[object, object], data).get("event") == "mood_update":
                        render_mood(cast(dict[object, object], data).get("mood"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[Avatar] Console connection unavailable: {type(exc).__name__}")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 15.0)


if __name__ == "__main__":
    init(autoreset=True)
    try:
        asyncio.run(listen())
    except KeyboardInterrupt:
        print("[Avatar] Console stopped")
    except BootstrapError:
        print("[Avatar] Authenticated console startup required")
        raise SystemExit(2) from None
