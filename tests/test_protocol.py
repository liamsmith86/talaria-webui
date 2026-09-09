import asyncio

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
