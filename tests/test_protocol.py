import asyncio
from collections import deque

import httpx
import pytest

from talaria.hermes import APIError, Hermes
from talaria.relay import Channel, Relay


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read += 1
            yield chunk


@pytest.mark.asyncio
async def test_split_utf8_streams_without_waiting_for_completion():
    raw = 'data: {"event":"message.delta","delta":"café"}\r\n\r\n'.encode()
    split = raw.index(b"\xc3") + 1
    stream = Chunks([raw[:split], raw[split:], b'data: {"event":"run.completed"}\n\n'])
    client = Hermes(
        "http://hermes.test",
        "",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
    )
    try:
        events = client.events("test-run")
        assert (await anext(events))["delta"] == "café"
        assert stream.read == 2
        assert (await anext(events))["event"] == "run.completed"
        await events.aclose()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unterminated_event_is_bounded():
    stream = Chunks([b"data: "] + [b"x" * 65536] * 50)
    client = Hermes(
        "http://hermes.test",
        "",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
    )
    try:
        with pytest.raises(APIError, match="exceeded the display limit"):
            await anext(client.events("test-run"))
        assert stream.read < 36
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_large_terminal_event_keeps_completion_and_ends_stream():
    channel = Channel("large-run")
    await channel.publish({"event": "run.completed", "output": "x" * 3_000_000})
    channel.finished = True
    async with asyncio.timeout(1):
        frames = [frame async for frame in Relay(None).stream(channel, 0)]
    assert "run.completed" in frames[-1] and "needs_history" in frames[-1]
    assert channel.size < 1000


async def test_subscriber_only_visits_unseen_replay_entries():
    class Counted(deque):
        visited = 0

        def __iter__(self):
            for item in super().__iter__():
                self.visited += 1
                yield item

        def __reversed__(self):
            for item in super().__reversed__():
                self.visited += 1
                yield item

    channel = Channel("incremental", events=Counted())
    for i in range(1500):
        await channel.publish({"event": "message.delta", "delta": str(i)})
    subscriber = Relay(None).stream(channel, channel.sequence)
    await anext(subscriber)
    try:
        for i in range(10):
            await channel.publish({"event": "message.delta", "delta": str(i)})
            assert (await anext(subscriber)).startswith(f"id: {1501 + i}\n")
        assert channel.events.visited <= 20
    finally:
        await subscriber.aclose()
    channel.finished = True
    replay = [frame async for frame in Relay(None).stream(channel, 1505)]
    assert [frame.splitlines()[0] for frame in replay[1:]] == [
        f"id: {i}" for i in range(1506, 1511)
    ]


async def test_fragmented_event_scans_each_byte_once(monkeypatch):
    from talaria import hermes

    class Counted(bytearray):
        scanned = 0

        def find(self, value, start=0):
            Counted.scanned += len(self) - start
            return super().find(value, start)

    monkeypatch.setattr(hermes, "bytearray", Counted, raising=False)
    raw = b'data: {"event":"message.delta","delta":"' + b"x" * 262144 + b'"}\r\n\r\n'
    chunks = Chunks([raw[i : i + 127] for i in range(0, len(raw), 127)])
    client = Hermes(
        "http://hermes.test", "",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=chunks)),
    )
    try:
        events = [event async for event in client.events("fragmented")]
        assert events == [{"event": "message.delta", "delta": "x" * 262144}]
        assert Counted.scanned <= len(raw) * 2
    finally:
        await client.close()


@pytest.mark.parametrize("width", [1, 2, 7, 4096])
async def test_event_boundaries_preserve_multiline_json_and_utf8(width):
    raw = ('data: {"event":"message.delta",\r\n'
           'data: "delta":"café 👩🏽‍💻"}\r\n\r\n'
           'data: {"event":"run.completed"}\n\n').encode()
    chunks = Chunks([raw[i : i + width] for i in range(0, len(raw), width)])
    client = Hermes(
        "http://hermes.test", "",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=chunks)),
    )
    try:
        assert [event async for event in client.events("boundaries")] == [
            {"event": "message.delta", "delta": "café 👩🏽‍💻"},
            {"event": "run.completed"},
        ]
    finally:
        await client.close()
