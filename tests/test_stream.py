"""Tests for server.stream."""

import asyncio
import json

from server import stream
from server.stream import Broadcaster


def ids(chunks):
    return [
        json.loads(c.split("data: ")[1])["id"] for c in chunks if "event: reading" in c
    ]


async def take(events, count):
    return [await asyncio.wait_for(anext(events), 5) for _ in range(count)]


def test_live_readings_have_ids():
    async def run():
        b = Broadcaster()
        b.start()
        events = b.events(None, lambda _: [])
        await take(events, 1)
        b.publish_readings([{"id": 7}])
        (chunk,) = await take(events, 1)
        await events.aclose()
        assert chunk == 'event: reading\nid: 7\ndata: {"id": 7}\n\n'
        assert not b._clients

    asyncio.run(run())


def test_publishing_from_another_thread():
    async def run():
        b = Broadcaster()
        b.start()
        events = b.events(None, lambda _: [])
        await take(events, 1)
        await asyncio.to_thread(b.publish_readings, [{"id": 1}, {"id": 2}])
        assert ids(await take(events, 2)) == [1, 2]
        await events.aclose()

    asyncio.run(run())


def test_reconnect_replays_without_duplicates():
    async def run():
        b = Broadcaster()
        b.start()

        def missed(last_id):
            assert last_id == 4
            b._put_all([{"id": 6}, {"id": 7}])  # 6 is also in the replay
            return [{"id": 5}, {"id": 6}]

        events = b.events(4, missed)
        assert ids(await take(events, 4)) == [5, 6, 7]
        await events.aclose()

    asyncio.run(run())


def test_slow_client_loses_the_oldest(monkeypatch):
    monkeypatch.setattr(stream, "QUEUE_MAX", 3)

    async def run():
        b = Broadcaster()
        b.start()
        queue = b.subscribe()
        b._put_all([{"id": i} for i in range(5)])
        assert [queue.get_nowait()["id"] for _ in range(3)] == [2, 3, 4]

    asyncio.run(run())


def test_heartbeat(monkeypatch):
    monkeypatch.setattr(stream, "HEARTBEAT_S", 0.01)

    async def run():
        b = Broadcaster()
        b.start()
        events = b.events(None, lambda _: [])
        chunks = await take(events, 3)
        await events.aclose()
        assert all(c.startswith("event: heartbeat") for c in chunks)

    asyncio.run(run())
