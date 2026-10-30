"""Bounded presentation replay; execution and transcripts remain in Hermes."""

import asyncio
import contextlib
import json
import time
from array import array
from collections import deque
from dataclasses import dataclass, field
from itertools import chain

from .hermes import MAX_STREAMS, APIError, Hermes, object_result

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
TERMINAL_EVENTS = {f"run.{status}" for status in TERMINAL}
MAX_EVENTS = 1500
MAX_BYTES = 2 * 1024 * 1024
# Streamed text arrives one small chunk per event. Older chunks are folded
# together rather than dropped, so a replay from the start stays complete.
FOLDABLE = {"message.delta", "assistant.delta", "talaria.reasoning.delta"}
# Precedes a replay that restarts the response: clients rebuild, never append.
REPLAY = '{"event":"talaria.reconcile","replay":true}'
# Precedes a replay missing events a client never saw: it keeps what it has
# and reconciles with the saved transcript.
RECONCILE = '{"event":"talaria.reconcile"}'


@dataclass(slots=True)
class Published:
    """One event, replayed exactly as it was published."""

    sequence: int
    encoded: str

    @property
    def first(self):
        return self.sequence

    @property
    def last(self):
        return self.sequence

    @property
    def size(self):
        return len(self.encoded)

    def payload(self, after=None):
        return self.encoded


@dataclass
class Folded:
    """Consecutive text chunks of one kind, kept as one replayable event.

    Long responses fold hundreds of thousands of chunks, so the text grows in
    place as UTF-8 and each chunk costs only the eight bytes recording where
    it ends. The size limit then bounds what the relay actually holds.
    """

    first: int
    last: int
    event: dict
    text: bytearray = field(default_factory=bytearray)
    # Where each folded chunk's text ends, in sequence order, so a subscriber
    # part way through receives only what it has not seen.
    ends: array = field(default_factory=lambda: array("Q"))

    @staticmethod
    def accepts(event):
        return event.get("event") in FOLDABLE and isinstance(event.get("delta"), str)

    def joins(self, sequence, event):
        return (
            self.last == sequence - 1
            and event.get("event") == self.event.get("event")
            and event.get("block_id") == self.event.get("block_id")
        )

    def add(self, sequence, event):
        self.text += event["delta"].encode("utf-8", "surrogatepass")
        self.last = sequence
        self.ends.append(len(self.text))

    def payload(self, after=None):
        start = 0
        if after is not None and self.first <= after < self.last:
            start = self.ends[after - self.first]
        text = self.text[start:].decode("utf-8", "surrogatepass")
        return json.dumps({**self.event, "delta": text}, separators=(",", ":"))

    @property
    def size(self):
        return len(self.text) + self.ends.itemsize * len(self.ends) + 256


@dataclass
class Channel:
    run_id: str
    # Recent events as published, and older ones folded into fewer entries.
    events: deque = field(default_factory=deque)
    folded: deque = field(default_factory=deque)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    sequence: int = 0
    size: int = 0
    touched: float = field(default_factory=time.monotonic)
    finished: bool = False
    task: asyncio.Task | None = None

    async def publish(self, event: dict):
        encoded = json.dumps(event, separators=(",", ":"))
        if len(encoded) > MAX_BYTES:
            kind = event.get("event")
            if not isinstance(kind, str) or kind not in TERMINAL_EVENTS:
                kind = "talaria.reconcile"
            encoded = json.dumps({"event": kind, "needs_history": True})
        async with self.condition:
            self.sequence += 1
            entry = Published(self.sequence, encoded)
            self.events.append(entry)
            self.size += entry.size
            while len(self.events) > MAX_EVENTS:
                self.fold(self.events.popleft())
            # Beyond the size limit the oldest events are lost; a subscriber
            # that needs them is told to reconcile with the saved transcript.
            while self.size > MAX_BYTES and (self.folded or len(self.events) > 1):
                self.size -= (self.folded or self.events).popleft().size
            self.touched = time.monotonic()
            self.condition.notify_all()

    def fold(self, entry):
        event = json.loads(entry.encoded)
        tail = self.folded[-1] if self.folded else None
        self.size -= entry.size
        if not Folded.accepts(event):
            self.folded.append(entry)
        elif isinstance(tail, Folded) and tail.joins(entry.sequence, event):
            self.size -= tail.size
            tail.add(entry.sequence, event)
            entry = tail
        else:
            entry = Folded(entry.sequence, entry.sequence, {**event, "delta": ""})
            entry.add(entry.first, event)
            self.folded.append(entry)
        self.size += entry.size
        if len(self.folded) > MAX_EVENTS:
            self.size -= self.folded.popleft().size

    def oldest(self):
        """The first sequence a replay can still deliver."""
        entries = self.folded or self.events
        return entries[0].first if entries else self.sequence + 1


class Relay:
    def __init__(self, hermes: Hermes):
        self.hermes = hermes
        self.channels: dict[str, Channel] = {}

    def attach(self, run_id: str) -> Channel:
        now = time.monotonic()
        for key, channel in list(self.channels.items()):
            if channel.finished and now - channel.touched > 600:
                self.channels.pop(key)
        if run_id in self.channels:
            return self.channels[run_id]
        while len(self.channels) >= MAX_STREAMS:
            finished = next((key for key, item in self.channels.items() if item.finished), None)
            if finished is None:
                break
            self.channels.pop(finished)
        if len(self.channels) >= MAX_STREAMS:
            raise APIError("Too many live sessions. Close an active run and try again.", 429)
        channel = self.channels[run_id] = Channel(run_id)
        channel.task = asyncio.create_task(self.observe(channel))
        return channel

    async def observe(self, channel: Channel):
        try:
            try:
                async for event in self.hermes.events(channel.run_id):
                    kind = object_result(event).get("event")
                    if not isinstance(kind, str):
                        raise APIError("Hermes returned an unreadable live update.")
                    if kind == "replay.truncated":
                        # Hermes replays the run from a later point.
                        event = {"event": "talaria.reconcile", "replay": True}
                    if kind == "run.failed" and event.get("error") == "unknown_execution":
                        # Hermes keeps this turn queued behind a turn lost in a restart.
                        continue
                    await channel.publish(event)
                    if kind in TERMINAL_EVENTS:
                        return
            except APIError:
                await channel.publish({"event": "talaria.reconcile"})
            # A lost stream is not a stopped run. Poll until Hermes confirms its final state.
            await self.poll(channel)
        finally:
            async with channel.condition:
                channel.finished = True
                channel.condition.notify_all()

    async def poll(self, channel: Channel):
        last = None
        for _ in range(1800):
            try:
                status = object_result(
                    await self.hermes.request("GET", f"/v1/runs/{channel.run_id}")
                )
                state = status.get("status")
                if not isinstance(state, str):
                    raise APIError("Hermes returned an unreadable run status.")
                if state in TERMINAL:
                    await channel.publish({**status, "event": f"run.{state}"})
                    return
                if state == "unknown":
                    # A restart lost this turn. It stays unknown until someone discards it.
                    await channel.publish({**status, "event": "run.unknown"})
                    return
                waited = (last or {}).get("status") == "queued"
                if state == "queued" and not waited:
                    await channel.publish({"event": "run.queued"})
                elif waited and state != "queued":
                    await channel.publish({"event": "talaria.status", "text": ""})  # It started.
                await publish_pending(channel, status, last)
                last = status
            except APIError as exc:
                if exc.status in {401, 403, 404}:
                    event = {"event": "talaria.unavailable", "message": exc.message}
                    # Hermes forgets a run some time after it ends.
                    await channel.publish({**event, "forgotten": exc.status == 404})
                    return
            await asyncio.sleep(2)
        await channel.publish(
            {
                "event": "talaria.unavailable",
                "message": "Live updates expired. Reopen this session to check it.",
            }
        )

    async def stream(self, channel: Channel, cursor: int):
        yield ": connected\n\n"
        while True:
            restarted = missing = False
            async with channel.condition:
                if cursor > channel.sequence:
                    # A cursor from before Talaria restarted: the replay restarts the run.
                    cursor = 0
                    restarted = True
                if cursor < channel.oldest() - 1:
                    missing = True
                    cursor = channel.oldest() - 1
                events = events_after(channel, cursor)
                if not events and not channel.finished:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(channel.condition.wait(), 15)
            if restarted or missing:
                yield f"data: {REPLAY if restarted else RECONCILE}\n\n"
            for sequence, payload in events:
                yield f"id: {sequence}\ndata: {payload}\n\n"
                cursor = sequence
            if channel.finished and cursor >= channel.sequence:
                return
            if not events:
                yield ": keepalive\n\n"

    async def close(self):
        tasks = [c.task for c in self.channels.values() if c.task and not c.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def publish_pending(channel, status, previous):
    for key, waiting, requested, resolved in (
        ("approval", "waiting_for_approval", "approval.request", "approval.responded"),
        (
            "clarification",
            "waiting_for_input",
            "talaria.clarification.request",
            "talaria.clarification.resolved",
        ),
    ):
        current = status.get(key)
        prior = (previous or {}).get(key)
        if (
            status.get("status") == waiting
            and isinstance(current, dict)
            and current
            and status != previous
        ):
            await channel.publish({**current, "event": requested})
        elif isinstance(prior, dict) and prior and not current:
            await channel.publish({"event": resolved, "request_id": prior.get("request_id")})
    # Hermes's session authority lists open prompts instead, settled by prompt ID.
    current, prior = pending_controls(status), pending_controls(previous)
    for prompt_id, prompt in current.items():
        if prompt != prior.get(prompt_id):
            await channel.publish({**prompt, "event": f"{prompt['kind']}.request"})
    for prompt_id, prompt in prior.items():
        if prompt_id not in current:
            generation = prompt.get("execution_generation")
            await channel.publish(
                {
                    "event": f"{prompt['kind']}.settled",
                    "prompt_id": prompt_id,
                    "execution_generation": generation,
                }
            )


def pending_controls(status):
    controls = (status or {}).get("pending_controls")
    if not isinstance(controls, list):
        return {}
    return {
        prompt["prompt_id"]: prompt
        for prompt in controls
        if isinstance(prompt, dict)
        and prompt.get("kind") in {"approval", "clarify"}
        and isinstance(prompt.get("prompt_id"), str)
    }


def events_after(channel, cursor):
    # Connected subscribers usually need just the newest event.
    # Walk back only through unseen entries, including full replay
    # when reconnecting, rather than rescanning the retained ring.
    events = []
    for entry in chain(reversed(channel.events), reversed(channel.folded)):
        if entry.last <= cursor:
            break
        events.append((entry.last, entry.payload(cursor)))
    return events[::-1]
