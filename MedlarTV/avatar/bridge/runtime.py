"""A bounded Core-owned mood publisher; producer calls never do socket I/O."""
from __future__ import annotations
from typing import TYPE_CHECKING, Protocol, TypeGuard, overload
from contextlib import AbstractAsyncContextManager
from .auth import Bootstrap

import asyncio
import json
import logging
import threading
import unicodedata
from .auth import authenticate_client, transport_logger, IO_TIMEOUT


if TYPE_CHECKING:
    class _PublisherSocket(Protocol):
        @property
        def close_code(self) -> int | None: ...
        async def send(self, message: str) -> None: ...
        async def recv(self) -> str | bytes: ...

    class _PublisherConnector(Protocol):
        def __call__(self, uri: str, *, proxy: None, open_timeout: float,
                     close_timeout: float, ping_interval: None,
                     logger: logging.Logger) -> AbstractAsyncContextManager[_PublisherSocket]: ...

log = logging.getLogger("avatar.runtime")
_publisher = None


def valid_mood(value: object) -> TypeGuard[str]:
    return (isinstance(value, str) and 1 <= len(value) <= 64
            and all(unicodedata.category(c) not in ("Cc", "Cs") for c in value))


class MoodPublisher:
    def __init__(self, bootstrap: Bootstrap, *, connector: _PublisherConnector | None = None) -> None:
        if bootstrap.role != "publisher":
            raise ValueError("Publisher capability required")
        self.bootstrap = bootstrap
        self._connector = connector
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._latest: str | None = None
        self._ready = False
        self._thread = None
        self._loop = None
        self._task = None

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._ready and not self._stop.is_set()

    def offer(self, label: object) -> bool:
        if not valid_mood(label) or self._stop.is_set():
            return False
        with self._lock:
            if self._stop.is_set():
                return False
            self._latest = label
        return True

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._thread_main, name="Avatar-mood-publisher", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 3.0) -> bool:
        self._stop.set()
        with self._lock:
            loop, task = self._loop, self._task
            self._ready = False
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(0.0, min(timeout, 3.0)))
        return self._thread is None or not self._thread.is_alive()

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        task = loop.create_task(self._run())
        with self._lock:
            self._loop, self._task = loop, task
        if self._stop.is_set():
            task.cancel()
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning("[Avatar] Worker ended: %s", type(exc).__name__)
        finally:
            with self._lock:
                self._loop = self._task = None
                self._ready = False
            loop.close()

    async def _run(self) -> None:
        connector = self._connector
        if connector is None:
            from websockets.asyncio.client import connect
            connector = connect
        delay = 1.0
        while not self._stop.is_set():
            try:
                async with connector(self.bootstrap.url, proxy=None, open_timeout=IO_TIMEOUT,
                                     close_timeout=1, ping_interval=None, logger=transport_logger()) as websocket:
                    await authenticate_client(websocket, self.bootstrap)
                    with self._lock:
                        self._ready = True
                    delay, sent = 1.0, None
                    while not self._stop.is_set():
                        if websocket.close_code is not None:
                            raise ConnectionError("Avatar connection ended")
                        with self._lock:
                            latest = self._latest
                        if latest is not None and latest != sent:
                            await asyncio.wait_for(websocket.send(json.dumps({
                                "event": "mood_update", "mood": latest, "channel": self.bootstrap.channel,
                            })), IO_TIMEOUT)
                            sent = latest
                        await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("[Avatar] Connection unavailable: %s", type(exc).__name__)
            finally:
                with self._lock:
                    self._ready = False
            if not self._stop.is_set():
                await asyncio.sleep(delay)
                delay = min(delay * 2, 15.0)


@overload
def start_publisher(bootstrap: None, initial_label: object) -> None: ...

@overload
def start_publisher(bootstrap: Bootstrap, initial_label: object) -> MoodPublisher: ...

def start_publisher(bootstrap: Bootstrap | None, initial_label: object) -> MoodPublisher | None:
    global _publisher
    if bootstrap is None:
        return None
    if _publisher is not None:
        raise RuntimeError("Avatar publisher already started")
    publisher = MoodPublisher(bootstrap)
    publisher.offer(initial_label)
    publisher.start()
    _publisher = publisher
    return publisher


def stop_publisher() -> bool:
    global _publisher
    publisher, _publisher = _publisher, None
    return publisher is None or publisher.stop()


def current_publisher() -> MoodPublisher | None:
    return _publisher
