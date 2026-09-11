"""Bounded presentation replay; execution and transcripts remain in Hermes."""

import asyncio
import contextlib
import json
import time
from collections import deque
from dataclasses import dataclass, field

from .hermes import APIError, Hermes, object_result

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
TERMINAL_EVENTS = {f"run.{status}" for status in TERMINAL}


@dataclass
class Channel:
    run_id: str
    events: deque = field(default_factory=deque)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    sequence: int = 0
    size: int = 0
    touched: float = field(default_factory=time.monotonic)
    finished: bool = False
    task: asyncio.Task | None = None

    async def publish(self, event: dict):
        encoded = json.dumps(event, separators=(",", ":"))
        if len(encoded) > 2 * 1024 * 1024:
            kind = event.get("event")
            if not isinstance(kind, str) or kind not in TERMINAL_EVENTS:
                kind = "talaria.reconcile"
            encoded = json.dumps({"event": kind, "needs_history": True})
        async with self.condition:
            self.sequence += 1
            self.events.append((self.sequence, encoded))
            self.size += len(encoded)
            while len(self.events) > 1500 or self.size > 2 * 1024 * 1024:
                self.size -= len(self.events.popleft()[1])
            self.touched = time.monotonic()
            self.condition.notify_all()


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
        while len(self.channels) >= 32:
            finished = next((key for key, item in self.channels.items() if item.finished), None)
            if finished is None:
                break
            self.channels.pop(finished)
        if len(self.channels) >= 32:
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
                approval = status.get("approval")
                if (
                    state == "waiting_for_approval"
                    and isinstance(approval, dict)
                    and approval
                    and status != last
                ):
                    await channel.publish({**approval, "event": "approval.request"})
                clarification = status.get("clarification")
                if (
                    state == "waiting_for_input"
                    and isinstance(clarification, dict)
                    and clarification
                    and status != last
                ):
                    await channel.publish(
                        {**clarification, "event": "talaria.clarification.request"}
                    )
                elif last and last.get("clarification") and not clarification:
                    await channel.publish(
                        {
                            "event": "talaria.clarification.resolved",
                            "request_id": last["clarification"].get("request_id"),
                        }
                    )
                last = status
            except APIError as exc:
                if exc.status in {401, 403, 404}:
                    await channel.publish({"event": "talaria.unavailable", "message": exc.message})
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
            reconcile = False
            async with channel.condition:
                if cursor > channel.sequence:
                    cursor = 0
                    reconcile = True
                if channel.events and cursor < channel.events[0][0] - 1:
                    reconcile = True
                    cursor = channel.events[0][0] - 1
                events = events_after(channel, cursor)
                if not events and not channel.finished:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(channel.condition.wait(), 15)
            if reconcile:
                yield 'data: {"event":"talaria.reconcile"}\n\n'
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


def events_after(channel, cursor):
    # Connected subscribers usually need just the newest event.
    # Walk back only through unseen entries, including full replay
    # when reconnecting, rather than rescanning the retained ring.
    events = []
    for entry in reversed(channel.events):
        if entry[0] <= cursor:
            break
        events.append(entry)
    events.reverse()
    return events
