"""Talaria runs through a real gateway's session authority, as Hermes 0.22.1+ serves them."""

import asyncio
import importlib.util
import json
import os
import socket
import sys
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web
from gateway.config import Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter

from talaria.hermes_plugin import bridge

home = Path(sys.argv[1])
assert home.is_dir() and str(home).startswith("/tmp/")
if importlib.util.find_spec("gateway.session_authority") is None:
    # Older Hermes runs every API turn through _create_agent; other contracts cover it.
    print("No session authority in this Hermes version")
    sys.exit(0)

from gateway.run import GatewayRunner  # noqa: E402
from gateway.session_authority import initialize_session_authority  # noqa: E402

KEY = "authority-contract-key"
INSTRUCTION = "AUTHORITY_PROFILE_INSTRUCTION"
PREFILL = "AUTHORITY_PREFILL_EXAMPLE"
original_connect = socket.socket.connect


def local_connect(sock, address):
    if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
        raise OSError("Non-loopback connections disabled in this test")
    return original_connect(sock, address)


socket.socket.connect = local_connect


def tool_call(name, arguments):
    call = {"id": f"{name}-call", "type": "function", "function": {"name": name}}
    call["function"]["arguments"] = json.dumps(arguments)
    return {"role": "assistant", "content": "", "tool_calls": [{"index": 0, **call}]}


async def main():
    seen, planned = [], []

    async def complete(request):
        payload = await request.json()
        seen.append(payload)
        message, finish = {"role": "assistant", "content": "Fixture reply."}, "stop"
        if planned:
            message, finish = planned.pop(0), "tool_calls"
        choice = {"index": 0, "message": message, "finish_reason": finish}
        body = {"id": "authority", "object": "chat.completion", "created": 1, "choices": [choice]}
        body.update(model=payload["model"], usage={"prompt_tokens": 9, "completion_tokens": 2})
        if payload.get("stream"):
            body["object"] = "chat.completion.chunk"
            choice["delta"] = choice.pop("message")
            text = f"data: {json.dumps(body)}\n\ndata: [DONE]\n\n"
            return web.Response(text=text, content_type="text/event-stream")
        return web.json_response(body)

    provider = web.Application()
    provider.router.add_post("/v1/chat/completions", complete)
    provider_runner = web.AppRunner(provider)
    await provider_runner.setup()
    site = web.TCPSite(provider_runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1"
    os.environ.update(OPENAI_API_KEY="loopback-only", OPENAI_BASE_URL=base, HERMES_EXEC_ASK="1")
    (home / "prefill.json").write_text(
        json.dumps([{"role": "user", "content": PREFILL}, {"role": "assistant", "content": "OK"}])
    )
    (home / "config.yaml").write_text(
        json.dumps(
            {
                "model": {"default": "authority-default", "provider": "custom", "base_url": base},
                "agent": {"system_prompt": INSTRUCTION},
                "prefill_messages_file": "prefill.json",
                "plugins": {"enabled": ["talaria-webui"]},
                "platform_toolsets": {"api_server": ["hermes-api-server", "clarify"]},
                "approvals": {"mode": "manual"},
                "auxiliary": {"title_generation": {"enabled": False}},
            }
        )
    )
    original_wire = APIServerAdapter._wire_plugin_handlers

    def wire_plugins(self, native=None):
        # The plugin manager is not loaded in this process; wire Talaria as it would be.
        original_wire(self, native)
        if native is not None:
            bridge.wire(native, self)

    APIServerAdapter._wire_plugin_handlers = wire_plugins
    runner = GatewayRunner()
    await initialize_session_authority(runner, profile_id="default", instance_id="talaria")
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": KEY, "port": 0}))
    adapter.gateway_runner = runner
    adapter.set_message_handler(runner._handle_message)
    runner.adapters[Platform.API_SERVER] = adapter
    assert await adapter.connect()
    url = f"http://127.0.0.1:{adapter._site._server.sockets[0].getsockname()[1]}"
    auth = {"Authorization": f"Bearer {KEY}"}
    try:
        async with ClientSession(timeout=ClientTimeout(total=60)) as client:

            async def start(body, key):
                headers = {**auth, "Idempotency-Key": key}
                async with client.post(url + "/talaria/v1/runs", json=body, headers=headers) as r:
                    assert r.status == 202, await r.text()
                    return await r.json()

            async def follow(run_id, answer=None):
                events = []
                async with client.get(f"{url}/v1/runs/{run_id}/events", headers=auth) as r:
                    async for raw in r.content:
                        line = raw.decode().strip()
                        if not line.startswith("data: "):
                            continue
                        event = json.loads(line[6:])
                        events.append(event)
                        if answer and event["event"].endswith(".request"):
                            await answer(run_id, event)
                        if event["event"].startswith("run."):
                            return events
                raise AssertionError(events)

            async def run(body, key, answer=None):
                first = len(seen)
                accepted = await start(body, key)
                events = await follow(accepted["run_id"], answer)
                assert events[-1]["event"] == "run.completed", events
                return accepted, events, seen[first:]

            def marks(request, text):
                return json.dumps(request["messages"]).count(text)

            async with client.get(url + "/talaria/v1/capabilities", headers=auth) as r:
                assert (await r.json())["profile_context"]["instructions"] == "ready"
            sid = "authority-session"
            async with client.post(url + "/api/sessions", json={"id": sid}, headers=auth) as r:
                assert r.status == 201, await r.text()

            # Profile instructions and saved models arrive with the run; prefill is native.
            body = {"session_id": sid, "input": "First", "model": "authority-saved"}
            _, _, requests = await run(body, "first")
            assert [r["model"] for r in requests] == ["authority-saved"]
            assert marks(requests[0], INSTRUCTION) == marks(requests[0], PREFILL) == 1
            second = {"session_id": sid, "input": "Second", "live_interactions": True}
            accepted, _, requests = await run(second, "second")
            assert [r["model"] for r in requests] == ["authority-saved"]
            assert marks(requests[0], INSTRUCTION) == marks(requests[0], PREFILL) == 1
            replay = await start(second, "second")
            assert replay["run_id"] == accepted["run_id"] and replay.get("replayed") is True
            body = {"session_id": sid, "input": "Third", "use_default_model": True}
            _, _, requests = await run(body, "third")
            assert [r["model"] for r in requests] == ["authority-default"]
            # Another client's confirmed lock is honored even though /v1/runs ignores it.
            locked = {
                "id": "authority-locked",
                "model": "authority-lock",
                "require_model_lock": True,
            }
            async with client.post(url + "/api/sessions", json=locked, headers=auth) as r:
                assert r.status == 201, await r.text()
            body = {"session_id": locked["id"], "input": "Locked"}
            _, _, requests = await run(body, "locked")
            assert [r["model"] for r in requests] == ["authority-lock"]

            # API turns are stored as internal notifications; they stay editable, and the
            # next turn sends only what remains.
            async with client.get(f"{url}/api/sessions/{sid}/messages", headers=auth) as r:
                rows = (await r.json())["data"]
            third = next(m for m in rows if m["role"] == "user" and m["content"] == "Third")
            path = f"{url}/talaria/v1/sessions/{sid}/rewind"
            async with client.post(path, json={"message_id": third["id"], "preview": True}) as r:
                assert r.status == 401
            async with client.post(
                path, json={"message_id": third["id"], "preview": True}, headers=auth
            ) as r:
                assert r.status == 200, await r.text()
                preview = await r.json()
            assert preview["user"]["content"] == "Third", preview
            confirm = {"message_id": third["id"], "revision": preview["revision"]}
            async with client.post(path, json=confirm, headers=auth) as r:
                assert r.status == 200, await r.text()
            _, _, requests = await run({"session_id": sid, "input": "Rewritten"}, "rewritten")
            sent = [m["content"] for m in requests[0]["messages"] if m["role"] == "user"]
            assert "Third" not in sent and sent[-1] == "Rewritten", sent

            # Native prompts carry the identifiers Talaria's answers echo back.
            question = {"question": "Which one?", "choices": ["alpha", "beta"]}
            planned.append(tool_call("clarify", {"questions": [question]}))
            pending = []

            async def answer_question(run_id, event):
                assert event["event"] == "clarify.request", event
                assert type(event["execution_generation"]) is int, event
                assert event["choices"] == ["alpha (Recommended)", "beta"], event
                async with client.get(f"{url}/v1/runs/{run_id}", headers=auth) as r:
                    pending.extend((await r.json())["pending_controls"])
                async with client.post(
                    f"{url}/talaria/v1/runs/{run_id}/clarification",
                    json={"request_id": event["prompt_id"], "answer": "alpha (Recommended)"},
                    headers=auth,
                ) as r:
                    assert r.status == 409  # Talaria's own bridge never sees these questions.
                answer = {
                    "request_id": event["prompt_id"],
                    "execution_generation": event["execution_generation"],
                    "answer": "alpha (Recommended)",
                }
                async with client.post(
                    f"{url}/v1/runs/{run_id}/clarify", json=answer, headers=auth
                ) as r:
                    assert r.status == 200, await r.text()

            body = {"session_id": sid, "input": "Ask me", "live_interactions": True}
            _, events, requests = await run(body, "question", answer_question)
            request = next(e for e in events if e["event"] == "clarify.request")
            # Reloads and lost streams recover the same prompt from the run status.
            assert pending == [
                {
                    k: v
                    for k, v in request.items()
                    if k not in {"event", "run_id", "seq", "timestamp"}
                }
            ], (pending, request)
            settled = next(e for e in events if e["event"] == "clarify.settled")
            assert settled["prompt_id"] == request["prompt_id"]
            assert settled["execution_generation"] == request["execution_generation"]
            result = json.loads(requests[-1]["messages"][-1]["content"])
            assert result["responses"][0]["user_response"] == "alpha", result

            target = home / "approval-target"
            planned.append(tool_call("terminal", {"command": f"rm -rf {target}"}))

            async def answer_approval(run_id, event):
                assert event["event"] == "approval.request", event
                assert "deny" in event["choices"] and "request_id" not in event, event
                choice = {"choice": "deny"}
                async with client.post(
                    f"{url}/v1/runs/{run_id}/approval", json=choice, headers=auth
                ) as r:
                    assert r.status == 400  # The pre-authority shape is refused.
                answer = {
                    "request_id": event["prompt_id"],
                    "execution_generation": event["execution_generation"],
                    **choice,
                }
                async with client.post(
                    f"{url}/v1/runs/{run_id}/approval", json=answer, headers=auth
                ) as r:
                    assert r.status == 200, await r.text()

            body = {"session_id": sid, "input": "Delete it", "live_interactions": True}
            _, events, _ = await run(body, "approval", answer_approval)
            settled = next(e for e in events if e["event"] == "approval.settled")
            assert settled["prompt_id"] == events[1]["prompt_id"], events

            # Another app's session takes no API turns; a branch continues it here.
            db = adapter._ensure_session_db()
            await asyncio.to_thread(db.create_session, "cli-session", "cli")
            await asyncio.to_thread(db.append_message, "cli-session", "user", "From the CLI")
            async with client.get(url + "/v1/capabilities", headers=auth) as r:
                assert (await r.json())["features"]["run_unknown_resolution"] is True
            async with client.get(url + "/talaria/v1/capabilities", headers=auth) as r:
                assert (await r.json())["external_session_copies"] is True
            refused = {"session_id": "cli-session", "input": "From Talaria"}
            async with client.post(
                url + "/talaria/v1/runs", json=refused, headers={**auth, "Idempotency-Key": "cli"}
            ) as r:
                assert r.status == 409, await r.text()
                assert (await r.json())["error"]["code"] == "permission_denied"
            async with client.post(
                url + "/talaria/v1/sessions/cli-session/fork", json={}, headers=auth
            ) as r:
                assert r.status == 201, await r.text()
                copy = (await r.json())["session"]
            assert copy["source"] == "api_server"
            assert (await asyncio.to_thread(db.get_session, "cli-session"))["end_reason"] is None
            await run({"session_id": copy["id"], "input": "From Talaria"}, "copy")
    finally:
        await adapter.disconnect()
        await provider_runner.cleanup()


asyncio.run(asyncio.wait_for(main(), 80))
print("Authority runs carry Talaria context and native prompts")
