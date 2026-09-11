"""Run-status recovery must mirror native control-plane events."""

import json
from types import SimpleNamespace

import pytest

from talaria import relay


@pytest.mark.parametrize("replacement", [False, True])
async def test_polling_clears_only_the_last_pending_approval(monkeypatch, replacement):
    first = {"request_id": "question-one", "command": "First command"}
    second = {"request_id": "question-two", "command": "Next command"}
    pending = second if replacement else first
    statuses = [
        {"status": "waiting_for_approval", "approval": first},
        {"status": "waiting_for_approval", "approval": first},
    ]
    if replacement:
        statuses.extend(
            [
                {"status": "waiting_for_approval", "approval": second},
                {"status": "waiting_for_approval", "approval": second},
            ]
        )
    statuses.extend([{"status": "running"}, {"status": "running"}, {"status": "completed"}])

    class Upstream:
        async def request(self, *args, **kwargs):
            return statuses.pop(0)

    async def no_delay(seconds):
        pass

    monkeypatch.setattr(relay, "asyncio", SimpleNamespace(sleep=no_delay))
    channel = relay.Channel("isolated-approval")
    await relay.Relay(Upstream()).poll(channel)
    events = [json.loads(event) for _, event in channel.events]
    requested = [event for event in events if event["event"] == "approval.request"]
    assert requested == [
        {**question, "event": "approval.request"}
        for question in ([first, second] if replacement else [first])
    ]
    resolved = [event for event in events if event["event"] == "approval.responded"]
    assert len(resolved) == 1
    assert resolved[0]["request_id"] == pending["request_id"]
    assert events.index(resolved[0]) < len(events) - 1
    assert events[-1]["event"] == "run.completed"
