"""Native API + agent + persistence against a loopback-only model simulator."""

import asyncio
import json
import os
import socket
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from agent.secret_scope import set_multiplex_active
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from gateway.config import PlatformConfig
from gateway.platforms import api_server_runs
from gateway.platforms.api_server import APIServerAdapter
from gateway.run import _profile_runtime_scope
from hermes_state import SessionDB
from run_agent import AIAgent

from talaria.hermes_plugin.bridge import wire
from talaria.hermes_plugin.context import MAX_PREFILL, load_context, supports_context_runs

home = Path(sys.argv[1])
assert home.is_dir() and str(home).startswith("/tmp/")
provider_name = sys.argv[2] if len(sys.argv) > 2 else "openai"
model_name = "z-ai/glm-5.2" if provider_name == "openrouter" else "gpt-4.1-mini"
prefill = [
    {"role": "user", "content": "PREFILL_EXAMPLE_QUESTION"},
    {"role": "assistant", "content": "PREFILL_EXAMPLE_ANSWER"},
]


def configure(prompt="PROFILE_INSTRUCTION", **extras):
    # JSON is valid YAML; avoids any quoting interpolation in fixture prompts.
    (home / "config.yaml").write_text(
        json.dumps(
            {
                "plugins": {"enabled": ["talaria"]},
                "model": {
                    "default": model_name,
                    "provider": provider_name,
                    "context_length": 128000,
                },
                "agent": {"system_prompt": prompt},
                "prefill_messages_file": "prefill.json",
                **extras,
            }
        )
    )


configure()
(home / "prefill.json").write_text(json.dumps(prefill))
loaded = load_context()
assert loaded.instructions == "PROFILE_INSTRUCTION" and loaded.prefill == prefill
assert "PROFILE_INSTRUCTION" not in json.dumps(loaded.public())
assert "prefill.json" not in json.dumps(loaded.public())

# The native personality resolver retains its own precedence; reloads are per turn.
configure(display={"personality": "concise"})
assert (
    "concise" in load_context().instructions
    and "PROFILE_INSTRUCTION" not in load_context().instructions
)
with patch.dict(os.environ, {"HERMES_EPHEMERAL_SYSTEM_PROMPT": "ENV_INSTRUCTION"}):
    assert load_context().instructions == "ENV_INSTRUCTION"
configure()
for invalid in (
    "not json",
    json.dumps([{"role": "tool", "content": "bad"}]),
    "x" * (MAX_PREFILL + 1),
):
    (home / "prefill.json").write_text(invalid)
    context = load_context()
    assert context.instructions == "PROFILE_INSTRUCTION" and context.prefill == []
    assert context.prefill_status == "unavailable"
(home / "prefill.json").unlink()
os.mkfifo(home / "prefill.json")
assert load_context().prefill_status == "unavailable"
(home / "prefill.json").unlink()
(home / "prefill.json").write_text(json.dumps(prefill))

# Multiplexed profiles resolve their own relative paths and environment overrides.
# A profile without an override must never inherit the launch profile's process env.
other = home / "another-profile"
other.mkdir()
(other / "config.yaml").write_text(
    json.dumps(
        {"agent": {"system_prompt": "OTHER_PROFILE", "prefill_messages_file": "prefill.json"}}
    )
)
(other / "prefill.json").write_text(json.dumps([{"role": "assistant", "content": "OTHER_PREFILL"}]))
set_multiplex_active(True)
try:
    with patch.dict(os.environ, {"HERMES_EPHEMERAL_SYSTEM_PROMPT": "LAUNCH_PROFILE_ONLY"}):
        with _profile_runtime_scope(other, prepared_secret_scope={}):
            assert load_context().instructions == "OTHER_PROFILE"
            assert load_context().prefill[0]["content"] == "OTHER_PREFILL"
        with _profile_runtime_scope(
            home, prepared_secret_scope={"HERMES_EPHEMERAL_SYSTEM_PROMPT": "SCOPED_ENV"}
        ):
            assert load_context().instructions == "SCOPED_ENV"
            assert load_context().prefill == prefill
finally:
    set_multiplex_active(False)

original_connect = socket.socket.connect


def local_connect(sock, address):
    if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
        raise OSError("Non-loopback connections disabled in this test")
    return original_connect(sock, address)


async def main():
    seen = []
    agents = []

    async def complete(request):
        payload = await request.json()
        seen.append(payload)
        response = {
            "id": "chatcmpl-fixture",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Fixture reply."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 123, "completion_tokens": 3, "total_tokens": 126},
        }
        if payload.get("stream"):
            response["object"] = "chat.completion.chunk"
            response["choices"][0]["delta"] = response["choices"][0].pop("message")
            return web.Response(
                text="data: " + json.dumps(response) + "\n\ndata: [DONE]\n\n",
                content_type="text/event-stream",
            )
        return web.json_response(response)

    provider = web.Application()
    provider.router.add_post("/v1/chat/completions", complete)
    async with TestServer(provider) as server:
        db = SessionDB(home / "state.db")
        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "fixture-key"}))
        adapter._session_db = db
        assert supports_context_runs(adapter)
        with patch.object(api_server_runs, "_handle_runs", lambda adapter, request: None):
            assert not supports_context_runs(adapter)
        app = web.Application(middlewares=[adapter._make_profile_prefix_middleware()])
        app.router.add_post("/v1/runs", adapter._handle_runs)
        app.router.add_get("/v1/runs/{run_id}/events", adapter._handle_run_events)
        wire(app, adapter)

        class ContractAgent(AIAgent):
            def __init__(self, **kwargs):
                kwargs.update(
                    skip_memory=True, skip_background_review=True, skip_context_files=True
                )
                super().__init__(**kwargs)
                agents.append(self)

        with (
            patch(
                "gateway.run._resolve_runtime_agent_kwargs",
                return_value={
                    "provider": provider_name,
                    "base_url": str(server.make_url("/v1")),
                    "api_key": "fixture-only",
                    "api_mode": "chat_completions",
                },
            ),
            patch("hermes_cli.tools_config._get_platform_tools", return_value=set()),
            patch("agent.model_metadata.get_model_context_length", return_value=128000),
            patch("agent.context_compressor.get_model_context_length", return_value=128000),
            patch("run_agent.AIAgent", ContractAgent),
        ):
            async with TestClient(TestServer(app)) as client:
                assert (
                    await client.post("/talaria/v1/runs", json={"input": "unauthorized"})
                ).status == 401
                headers = {"Authorization": "Bearer fixture-key", "Idempotency-Key": "first"}
                caps = await (await client.get("/talaria/v1/capabilities", headers=headers)).json()
                assert caps["context_runs"] and caps["profile_context"]["instructions"] == "ready"
                db.create_session("context-test", "api_server")

                async def run(text, key, **extras):
                    response = await client.post(
                        "/talaria/v1/runs",
                        json={
                            "session_id": "context-test",
                            "input": text,
                            **extras,
                        },
                        headers={**headers, "Idempotency-Key": key},
                    )
                    data = await response.json()
                    assert response.status == 202, data
                    task = adapter._active_run_tasks.get(data["run_id"])
                    if key == "first" and not (home / "stream.json").exists():
                        stream = await client.get(
                            f"/v1/runs/{data['run_id']}/events",
                            headers=headers,
                        )
                        assert stream.status == 200
                        frames = [
                            json.loads(line[6:])
                            for line in (await stream.text()).splitlines()
                            if line.startswith("data: ")
                        ]
                        # Only synthetic fixture content is recorded. Exclude
                        # variable identifiers, timing, usage and provider metadata.
                        fields = {
                            "event",
                            "delta",
                            "text",
                            "output",
                            "tool",
                            "preview",
                            "tool_call_id",
                        }
                        trace = {
                            "events": [
                                {k: v for k, v in event.items() if k in fields} for event in frames
                            ],
                            "messages": [
                                {k: row[k] for k in ("role", "content")}
                                for row in db.get_messages("context-test")
                            ],
                        }
                        (home / "stream.json").write_text(json.dumps(trace, indent=2) + "\n")
                    if task:
                        await asyncio.wait_for(task, 30)
                    status = adapter._run_statuses[data["run_id"]]
                    assert status["status"] == "completed", status
                    assert adapter._pending_agent_requests == 0
                    return data["run_id"]

                first = await run("First real user turn", "first")
                assert len(seen) == 1
                before = deepcopy(seen[0])
                configure("UPDATED_PROFILE_INSTRUCTION")
                assert load_context().instructions == "UPDATED_PROFILE_INSTRUCTION"
                assert await run("First real user turn", "first") == first
                assert (
                    len(seen) == 1
                )  # Config changes cannot turn a lost acceptance into a second run.
                await run("Second real user turn", "second")
                assert len(seen) == 2
                assert "UPDATED_PROFILE_INSTRUCTION" in json.dumps(seen[1]), {
                    "agents": [a.ephemeral_system_prompt for a in agents],
                    "payload": json.dumps(seen[1])[:700],
                    "messages": [
                        (m.get("role"), str(m.get("content"))[-160:])
                        for m in seen[1].get("messages", [])
                    ],
                }
                assert before == seen[0]
                for request in seen:
                    serialized = json.dumps(request)
                    assert serialized.count("PREFILL_EXAMPLE_QUESTION") == 1
                    assert serialized.count("PREFILL_EXAMPLE_ANSWER") == 1
                rows = db.get_messages("context-test", include_inactive=True)
                assert [r["content"] for r in rows] == [
                    "First real user turn",
                    "Fixture reply.",
                    "Second real user turn",
                    "Fixture reply.",
                ]
                saved = json.dumps({"session": db.get_session("context-test"), "messages": rows})
                assert "PROFILE_INSTRUCTION" not in saved and "PREFILL_EXAMPLE" not in saved
                assert len(agents) == 2 and all(a.prefill_messages == prefill for a in agents)
                # Preserve native drain bookkeeping while context loads off the loop.
                entered, release = threading.Event(), threading.Event()

                def blocked_context():
                    entered.set()
                    assert release.wait(5)
                    return load_context()

                with patch("talaria.hermes_plugin.context.load_context", blocked_context):
                    pending = asyncio.create_task(
                        run("Third real user turn", "third", instructions="EXPLICIT_INSTRUCTION")
                    )
                    try:
                        for _ in range(100):
                            if entered.is_set():
                                break
                            await asyncio.sleep(0.01)
                        assert entered.is_set()
                        assert adapter._pending_agent_requests == 1
                        assert adapter.active_agent_work_count() == 1
                    finally:
                        release.set()
                    await pending
                assert "EXPLICIT_INSTRUCTION" in json.dumps(seen[-1])
                assert "UPDATED_PROFILE_INSTRUCTION" not in json.dumps(seen[-1])
                assert adapter.active_agent_work_count() == 0
                from hermes_commands_contract import verify_commands

                # Session-persisted models resolve provider credentials separately from
                # the global defaults. Keep that credential lookup on the simulator too.
                with patch.object(
                    adapter,
                    "_resolve_provider_runtime",
                    return_value={
                        "provider": provider_name,
                        "base_url": str(server.make_url("/v1")),
                        "api_key": "fixture-only",
                        "api_mode": "chat_completions",
                    },
                ):
                    await verify_commands(client, db, model_name)
        await adapter.disconnect()
        db.close()
    print("Native ephemeral context, clean persistence, resumed turns, replay and admission passed")


with patch.object(socket.socket, "connect", local_connect):
    asyncio.run(main())
