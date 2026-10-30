"""Run-status recovery must mirror native control-plane events."""

import json
from types import SimpleNamespace

import pytest

from talaria import relay


async def test_native_replay_gap_requests_history_before_retained_events():
    class Upstream:
        async def events(self, run_id):
            yield {"event": "replay.truncated", "oldest_retained_seq": 100}
            yield {"event": "message.delta", "delta": "Retained tail", "seq": 100}
            yield {"event": "run.completed", "output": "Final answer", "seq": 101}

    channel = relay.Channel("native-replay")
    await relay.Relay(Upstream()).observe(channel)
    events = [json.loads(entry.encoded) for entry in channel.events]
    assert events == [
        # The replay restarts part way through, so clients rebuild rather than append.
        {"event": "talaria.reconcile", "replay": True},
        {"event": "message.delta", "delta": "Retained tail", "seq": 100},
        {"event": "run.completed", "output": "Final answer", "seq": 101},
    ]
    assert channel.finished


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
    events = [json.loads(entry.encoded) for entry in channel.events]
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


async def test_polling_replays_and_settles_session_authority_prompts(monkeypatch):
    question = {"kind": "clarify", "prompt_id": "question", "execution_generation": 2}
    approval = {"kind": "approval", "prompt_id": "approval", "execution_generation": 2}
    statuses = [
        {"status": "running", "pending_controls": [question]},
        {"status": "running", "pending_controls": [question]},
        {"status": "waiting_for_approval", "pending_controls": [approval]},
        {"status": "running", "pending_controls": []},
        {"status": "completed"},
    ]

    class Upstream:
        async def request(self, *args, **kwargs):
            return statuses.pop(0)

    async def no_delay(seconds):
        pass

    monkeypatch.setattr(relay, "asyncio", SimpleNamespace(sleep=no_delay))
    channel = relay.Channel("authority-prompts")
    await relay.Relay(Upstream()).poll(channel)
    events = [json.loads(entry.encoded) for entry in channel.events]
    assert [(event["event"], event.get("prompt_id")) for event in events] == [
        ("clarify.request", "question"),
        ("approval.request", "approval"),
        ("clarify.settled", "question"),
        ("approval.settled", "approval"),
        ("run.completed", None),
    ]
    assert events[0] == {**question, "event": "clarify.request"}
    assert events[3] == {
        "event": "approval.settled",
        "prompt_id": "approval",
        "execution_generation": 2,
    }


async def test_turns_lost_in_a_restart_wait_for_a_decision_without_false_failures(monkeypatch):
    lost = {"status": "unknown", "admission_id": "lost", "execution_generation": 2}
    statuses = {
        "lost": [lost],
        "queued": [
            {"status": "queued"},
            {"status": "queued"},
            {"status": "running"},
            {"status": "completed"},
        ],
    }

    class Upstream:
        async def events(self, run_id):
            if run_id == "queued":
                # Hermes queues this turn behind the lost one but fails its stream.
                yield {"event": "run.failed", "error": "unknown_execution"}

        async def request(self, method, path):
            return statuses[path.split("/")[-1]].pop(0)

    async def no_delay(seconds):
        pass

    monkeypatch.setattr(relay, "asyncio", SimpleNamespace(sleep=no_delay))
    for run_id, expected in (
        ("lost", [{**lost, "event": "run.unknown"}]),
        (
            "queued",
            [
                {"event": "run.queued"},
                {"event": "talaria.status", "text": ""},
                {"status": "completed", "event": "run.completed"},
            ],
        ),
    ):
        channel = relay.Channel(run_id)
        await relay.Relay(Upstream()).observe(channel)
        assert [json.loads(entry.encoded) for entry in channel.events] == expected
        assert channel.finished and not statuses[run_id]


async def test_many_reconnects_share_one_run_and_oversized_replay_keeps_terminal():
    import asyncio

    class Upstream:
        started = 0

        async def events(self, run_id):
            self.started += 1
            yield {"event": "tool.started", "tool_call_id": "fixture", "name": "terminal"}
            for _ in range(1600):
                yield {"event": "message.delta", "delta": "emoji 🐈" * 100}
            # Simulate an upstream connection closing without a terminal SSE frame.

        async def request(self, method, path):
            return {"status": "cancelled", "output": "Native final output"}

    peer = Upstream()
    manager = relay.Relay(peer)
    channel = manager.attach("same-run")
    assert all(manager.attach("same-run") is channel for _ in range(100))
    await asyncio.wait_for(channel.task, 5)
    assert peer.started == 1 and channel.finished
    assert len(channel.events) <= 1500 and channel.size <= 2 * 1024 * 1024
    frames = [frame async for frame in manager.stream(channel, 1)]
    assert '"event":"talaria.reconcile"' in frames[1]
    assert '"event":"run.cancelled"' in frames[-1]

    async def reconnect():
        reconciled = False
        last = ""
        async for frame in manager.stream(channel, 1):
            reconciled |= '"event":"talaria.reconcile"' in frame
            last = frame
        return reconciled and '"event":"run.cancelled"' in last

    assert all(await asyncio.gather(*(reconnect() for _ in range(100))))
    cursor = channel.sequence - 1
    frames = [frame async for frame in manager.stream(channel, cursor)]
    assert len(frames) == 2 and "Native final output" in frames[-1]
    await manager.close()
