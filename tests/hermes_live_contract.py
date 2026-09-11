"""Provider reasoning and clarify tool calls through real Hermes run endpoints."""

import asyncio
import json
from unittest.mock import patch

from tools import clarify_gateway


async def wait_for(check):
    for _ in range(1500):
        result = check()
        if result:
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("Native run did not reach the expected state")


async def verify_live(client, adapter, db, planned, seen):
    for mode in ("answer", "batch", "skip", "stop", "timeout"):
        with patch.object(
            clarify_gateway, "get_clarify_timeout", return_value=1 if mode == "timeout" else 30
        ):
            await verify_mode(client, adapter, db, planned, seen, mode)


async def verify_mode(client, adapter, db, planned, seen, mode):
    sid = f"clarify-{mode}"
    db.create_session(sid, "api_server")
    questions = [{"question": "Which environment?", "choices": ["Development", "Production"]}]
    if mode == "batch":
        questions.append(
            {"question": "Which checks?", "choices": ["Lint", "Browser"], "multi_select": True}
        )
    planned.extend(
        [
            {
                "message": {
                    "role": "assistant",
                    "content": "Before the question.",
                    "reasoning_content": "ACTUAL_PROVIDER_REASONING_ONE",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "clarify-fixture",
                            "type": "function",
                            "function": {
                                "name": "clarify",
                                "arguments": json.dumps({"questions": questions}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            },
            {
                "message": {
                    "role": "assistant",
                    "content": "After the answer.",
                    "reasoning_content": "ACTUAL_PROVIDER_REASONING_TWO",
                },
                "finish_reason": "stop",
            },
        ]
    )
    headers = {"Authorization": "Bearer fixture-key", "Idempotency-Key": sid}
    response = await client.post(
        "/talaria/v1/runs",
        headers=headers,
        json={"session_id": sid, "input": "Ask me a question", "live_interactions": True},
    )
    assert response.status == 202, await response.text()
    rid = (await response.json())["run_id"]
    task = adapter._active_run_tasks[rid]
    stream = await client.get(f"/v1/runs/{rid}/events", headers=headers)
    events = []

    async def consume():
        async for line in stream.content:
            if line.startswith(b"data: "):
                events.append(json.loads(line[6:]))

    consumer = asyncio.create_task(consume())
    prompt = await wait_for(lambda: adapter._run_statuses[rid].get("clarification"))
    await wait_for(lambda: any(e["event"] == "talaria.reasoning.delta" for e in events))
    assert any(e.get("delta") == "ACTUAL_PROVIDER_REASONING_ONE" for e in events)
    assert not any(e["event"] == "reasoning.available" for e in events)
    assert prompt["question"] == "Which environment?"
    assert prompt["choices"][0] == "Development (Recommended)"
    status = await (await client.get(f"/v1/runs/{rid}", headers=headers)).json()
    assert status["status"] == "waiting_for_input" and status["clarification"] == prompt
    if mode in {"answer", "batch"}:
        await verify_answer(client, adapter, rid, headers, prompt)
        if mode == "batch":
            second = await wait_for(
                lambda: (
                    pending
                    if (pending := adapter._run_statuses[rid].get("clarification"))
                    and pending["request_id"] != prompt["request_id"]
                    else None
                )
            )
            assert second["question"] == "Which checks?" and second["multi_select"]
            response = await client.post(
                f"/talaria/v1/runs/{rid}/clarification",
                headers=headers,
                json={"request_id": second["request_id"], "answer": json.dumps(second["choices"])},
            )
            assert response.status == 200
    elif mode == "skip":
        response = await client.post(
            f"/talaria/v1/runs/{rid}/clarification",
            headers=headers,
            json={"request_id": prompt["request_id"], "answer": ""},
        )
        assert response.status == 200
    elif mode == "stop":
        response = await client.post(f"/v1/runs/{rid}/stop", headers=headers, json={})
        assert response.status == 200
    await asyncio.wait_for(task, 30)
    await asyncio.wait_for(consumer, 5)
    assert not clarify_gateway.has_pending(rid)
    if mode == "stop":
        assert adapter._run_statuses[rid]["status"] == "cancelled"
    else:
        assert adapter._run_statuses[rid]["status"] == "completed"
        assert any(e.get("delta") == "ACTUAL_PROVIDER_REASONING_TWO" for e in events)
        assert any(e["event"] == "talaria.clarification.resolved" for e in events)
        tool_rows = [m for m in seen[-1]["messages"] if m["role"] == "tool"]
        assert tool_rows
        result = json.loads(tool_rows[-1]["content"])
        if mode in {"answer", "batch"}:
            assert result["responses"][0]["user_response"] == "Development"
            if mode == "batch":
                assert result["responses"][1]["user_response"] == ["Lint", "Browser"]
        elif mode == "skip":
            assert result["responses"][0]["user_response"] == ""
        else:
            assert result["timed_out"] is True
    planned.clear()


async def verify_answer(client, adapter, rid, headers, prompt):
    endpoint = f"/talaria/v1/runs/{rid}/clarification"
    payload = {"request_id": prompt["request_id"], "answer": "Development"}
    assert (await client.post(endpoint, json=payload)).status == 401
    owner = adapter._run_owners[rid]
    adapter._run_owners[rid] = "a-different-profile"
    try:
        assert (await client.post(endpoint, headers=headers, json=payload)).status == 404
    finally:
        adapter._run_owners[rid] = owner
    assert (
        await client.post(endpoint, headers=headers, json={**payload, "request_id": "stale"})
    ).status == 409
    assert (await client.post(endpoint, headers=headers, json=payload)).status == 200
    # A lost answer acknowledgement can be retried without advancing the tool twice.
    assert (await client.post(endpoint, headers=headers, json=payload)).status == 200
    assert (
        await client.post(endpoint, headers=headers, json={**payload, "answer": "Production"})
    ).status == 409
