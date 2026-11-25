"""Server-Sent Events broadcaster (spec section 12.3)."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable

QUEUE_MAX = 1000
HEARTBEAT_S = 15.0


def format_event(event: str, data: dict, event_id: int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"event: {event}\n{head}data: {json.dumps(data)}\n\n"


class Broadcaster:
    """Fans events out to connected clients; a slow client loses the oldest."""

    def __init__(self) -> None:
        self._clients: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def start(self) -> None:
        """Remember the running event loop; call from inside it."""
        self._loop = asyncio.get_running_loop()

    def publish_readings(self, readings: list[dict]) -> None:
        """Send readings to every client. Safe to call from any thread."""
        if readings and self._loop is not None:
            self._loop.call_soon_threadsafe(self._put_all, readings)

    def publish_event(self, event: str, data: dict) -> None:
        """Send one event without an id. Safe to call from any thread."""
        if self._loop is not None:
            message = {"event": event, "data": data}
            self._loop.call_soon_threadsafe(self._put_all, [message])

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(QUEUE_MAX)
        self._clients.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._clients.discard(queue)

    def _put_all(self, messages: list[dict]) -> None:
        for queue in self._clients:
            for message in messages:
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(message)

    async def events(
        self, last_id: int | None, missed_since: Callable[[int], list[dict]]
    ) -> AsyncIterator[str]:
        """Yield SSE text: missed readings first, then live ones."""
        # Subscribe before reading the database so that nothing falls between.
        queue = self.subscribe()
        replayed = 0
        try:
            yield format_event("heartbeat", {})
            if last_id is not None:
                for reading in await asyncio.to_thread(missed_since, last_id):
                    replayed = reading["id"]
                    yield format_event("reading", reading, replayed)
            while True:
                try:
                    reading = await asyncio.wait_for(queue.get(), HEARTBEAT_S)
                except TimeoutError:
                    yield format_event("heartbeat", {})
                    continue
                if "event" in reading:
                    yield format_event(reading["event"], reading["data"])
                elif reading["id"] > replayed:
                    yield format_event("reading", reading, reading["id"])
        finally:
            self.unsubscribe(queue)
