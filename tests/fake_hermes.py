"""Deterministic public-protocol peer for functional and visual tests."""

import asyncio
import copy
import json
import re
import time
import uuid

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from .fake_session_views import respond as session_view

KEY = "test-hermes-key-do-not-use"
STEER_OPEN = (
    "[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this "
    "position; not tool output and not a new delivery when replayed from conversation history]"
)


class FakeHermes:
    def __init__(self, api_key=KEY):
        self.api_key = api_key
        self.sessions = {}
        self.messages = {}
        self.runs = {}
        self.requests = {}
        self.stops = 0
        self.approvals = 0
        self.approval_choices = ["once", "session", "deny"]
        # Hermes 0.22.1+ session authority: prompts settle by prompt ID and generation.
        self.native_controls = False
        # ...which also refuses API turns in sessions another surface owns.
        self.session_authority = False
        self.default_model = "hermes-test"
        self.default_provider = "test"
        self.persist_image_originals = True
        self.discovery_overrides = {}
        self.extension = None
        self.rewinds = 0
        self.calls = []
        self.app = Starlette(
            routes=[
                Route(
                    "/{path:path}", self.handle, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
                )
            ]
        )

    async def handle(self, request: Request):
        if request.headers.get("authorization") != f"Bearer {self.api_key}":
            return JSONResponse({"error": "Unauthorized"}, 401)
        path = request.url.path
        self.calls.append((request.method, path, dict(request.query_params)))
        body = await request.json() if request.method in {"POST", "PATCH", "PUT"} else {}
        if path in self.discovery_overrides:
            return JSONResponse(*self.discovery_overrides[path])
        plugin_fork = path.startswith("/talaria/v1/sessions/") and path.endswith("/fork")
        if plugin_fork:
            if self.extension is None or not self.extension.get("session_fork", True):
                return JSONResponse({"error": "Not installed"}, 404)
            path = path.replace("/talaria/v1/sessions/", "/api/sessions/", 1)
        if path == "/talaria/v1/runs":
            if not (self.extension or {}).get("context_runs"):
                return JSONResponse({"error": "Not installed"}, 404)
            path = "/v1/runs"  # Both entry points share Hermes's run/event lifecycle.
        if path.startswith("/talaria/v1/runs/") and path.endswith("/clarification"):
            run = self.runs.get(path.split("/")[4], {})
            if not run.get("clarification") or run["clarification"].get("request_id") != body.get(
                "request_id"
            ):
                return JSONResponse({"error": "Question expired"}, 409)
            run.setdefault("answers", []).append(body["answer"])
            run.update(clarification=None, status="running")
            return JSONResponse({"ok": True})
        if path.startswith("/talaria/v1/"):
            if self.extension is None:
                return JSONResponse({"error": "Not installed"}, 404)
            if path == "/talaria/v1/capabilities":
                return JSONResponse(
                    {
                        "version": 1,
                        "response_details": True,
                        "context_usage": True,
                        "rewind": True,
                        "external_session_copies": True,
                        **self.extension,
                    }
                )
            view = session_view(self, request)
            if view is not None:
                return view
            sid, action = path.split("/")[4:6]
            if sid not in self.sessions:
                return JSONResponse({"error": "Not found"}, 404)
            rows = self.messages[sid]
            if action == "context":
                return JSONResponse(
                    {
                        "context": {
                            "used": 12000,
                            "maximum": 128000,
                            "model": "actual-response-model",
                        }
                        if rows
                        else None
                    }
                )
            if action == "response":
                return JSONResponse(
                    {
                        "model": "actual-response-model",
                        "provider": "test",
                        "reasoning": "high",
                        "duration_seconds": 2.4,
                        "usage": {"input_tokens": 12000, "output_tokens": 400},
                        "finish_reason": "stop",
                    }
                )
            index = next((i for i, row in enumerate(rows) if row["id"] == body["message_id"]), None)
            if index is None:
                return JSONResponse({"error": "Not found"}, 409)
            if rows[index]["role"] not in {"user", "assistant"}:
                # The plugin refuses metadata and tool rows as rewind targets.
                return JSONResponse({"error": "This message is no longer available."}, 400)
            index = next(i for i in range(index, -1, -1) if rows[i]["role"] == "user")
            revision = str([row["id"] for row in rows])
            if body.get("preview"):
                return JSONResponse(
                    {
                        "revision": revision,
                        "user": rows[index],
                        "target_id": rows[index]["id"],
                        "message_count": len(rows) - index,
                        "turn_count": sum(row["role"] == "user" for row in rows[index:]),
                    }
                )
            if body.get("revision") != revision:
                return JSONResponse({"error": "Changed"}, 409)
            self.messages[sid] = rows[:index]
            self.rewinds += 1
            return JSONResponse({"ok": True})
        if path == "/v1/capabilities":
            return JSONResponse(
                {
                    "object": "hermes.api_server.capabilities",
                    "platform": "hermes-agent",
                    "features": {
                        "runs_idempotency": {
                            "supported": True,
                            "durable": True,
                            "retention_seconds": 86400,
                        },
                        **dict.fromkeys(
                            [
                                "run_submission",
                                "run_stop",
                                "run_steer",
                                "run_status",
                                "run_events_sse",
                                "run_approval_response",
                                "session_resources",
                                "session_fork",
                                "model_options",
                                "skills_api",
                            ],
                            True,
                        ),
                        **({"run_unknown_resolution": True} if self.session_authority else {}),
                    },
                }
            )
        if path == "/api/model/options":
            return JSONResponse(
                {
                    "model": self.default_model,
                    "provider": self.default_provider,
                    "providers": [
                        {
                            "slug": "test",
                            "is_current": True,
                            "authenticated": True,
                            "label": "Test provider",
                            "models": [
                                {"id": "hermes-test", "name": "Hermes Test"},
                                {"id": "hermes-fast", "name": "Hermes Fast"},
                            ],
                            "capabilities": {
                                "hermes-test": {"reasoning": True, "can_disable_reasoning": True},
                                "hermes-fast": {"reasoning": True},
                            },
                        }
                    ],
                }
            )
        if path == "/health/detailed":
            return JSONResponse(
                {
                    "status": "ok",
                    "version": "1.2.3",
                    "gateway_state": "running",
                    "active_agents": 0,
                    "readiness": {"status": "ok", "checks": {}},
                    "platforms": {"api_server": {"state": "connected"}},
                    "pid": 12345,
                    "private_future_field": KEY,
                }
            )
        if path == "/api/sessions":
            if request.method == "GET":
                offset = int(request.query_params.get("offset", 0))
                limit = int(request.query_params.get("limit", 100))
                values = list(self.sessions.values())[::-1]
                if request.query_params.get("include_children") == "false":
                    values = [s for s in values if s.get("source") != "subagent"]
                pins = [s for s in values if s.get("pinned")]
                values = [s for s in values if not s.get("pinned")]
                return JSONResponse(
                    {
                        "data": pins + values[offset : offset + limit],
                        "has_more": len(values) > offset + limit,
                        "limit": limit,
                    }
                )
            sid = uuid.uuid4().hex
            if body.get("title") and any(
                s.get("title") == body["title"] for s in self.sessions.values()
            ):
                return JSONResponse(
                    {"error": {"code": "invalid_title", "message": "Duplicate title"}}, 400
                )
            record = {
                "id": sid,
                "title": body.get("title", "Untitled"),
                "started_at": time.time(),
                "source": body.get("source", "api_server"),
                "pinned": False,
            }
            self.sessions[sid], self.messages[sid] = record, []
            return JSONResponse({"object": "hermes.session", "session": record}, 201)
        parts = path.strip("/").split("/")
        if path.startswith("/api/sessions/"):
            sid = parts[2]
            if sid not in self.sessions:
                return JSONResponse({}, 404)
            if path.endswith("/messages"):
                # Native API forks lacking the CLI's marker are incorrectly
                # treated as continuations by Hermes's resume resolver.
                children = [
                    s
                    for s in self.sessions.values()
                    if s.get("parent_session_id") == sid
                    and not any(
                        (s.get("model_config") or {}).get(key)
                        for key in ("_branched_from", "_delegate_from", "_reset_from")
                    )
                    and s.get("source") != "tool"
                ]
                if children:
                    sid = children[-1]["id"]
                offset = int(request.query_params.get("offset", "0"))
                limit = int(request.query_params.get("limit", "100"))
                all_messages = self.messages[sid]
                if request.query_params.get("include_compacted") == "false":
                    all_messages = [row for row in all_messages if row.get("active", True)]
                end = max(0, len(all_messages) - offset)
                return JSONResponse(
                    {
                        "session_id": sid,
                        "data": all_messages[offset : offset + limit]
                        if request.query_params.get("order") == "oldest"
                        else all_messages[max(0, end - limit) : end],
                    }
                )
            if path.endswith("/fork"):
                title = body.get("title")
                titles = {s.get("title") for s in self.sessions.values()}
                if title is None:
                    base = re.sub(r" #\d+$", "", self.sessions[sid].get("title") or "fork")
                    title, number = base, 1
                    while title in titles:
                        number += 1
                        title = f"{base} #{number}"
                elif title in titles and plugin_fork:
                    return JSONResponse({"error": {"code": "invalid_title"}}, 400)
                new_id = uuid.uuid4().hex
                # The plugin copies another app's session instead of ending it.
                if not plugin_fork or self.sessions[sid].get("source") == "api_server":
                    self.sessions[sid]["end_reason"] = "branched"
                self.sessions[new_id] = {
                    "id": new_id,
                    "title": title,
                    "source": "api_server",
                    "parent_session_id": sid,
                    "model_config": {"_branched_from": sid} if plugin_fork else {},
                }
                copied = copy.deepcopy(
                    [row for row in self.messages[sid] if row.get("active", True)]
                )
                start = (
                    max((row["id"] for rows in self.messages.values() for row in rows), default=0)
                    + 1
                )
                self.messages[new_id] = [
                    {**row, "id": start + index, "session_id": new_id}
                    for index, row in enumerate(copied)
                ]
                if title in titles:
                    self.sessions[new_id]["title"] = None
                    return JSONResponse({"error": {"code": "invalid_title"}}, 400)
                return JSONResponse(
                    {"object": "hermes.session", "session": self.sessions[new_id]}, 201
                )
            if request.method == "PATCH":
                self.sessions[sid].update(body)
            if request.method == "DELETE":
                del self.sessions[sid]
                del self.messages[sid]
                return JSONResponse({"ok": True})
            return JSONResponse(self.sessions[sid])
        if path == "/v1/runs":
            owner = self.sessions.get(body.get("session_id"), {}).get("source", "api_server")
            if self.session_authority and owner not in {"api_server", "bot_room"}:
                error = {"code": "permission_denied", "message": "permission_denied"}
                return JSONResponse({"error": error}, 409)
            key = request.headers.get("idempotency-key")
            if key in self.requests:
                return JSONResponse({"run_id": self.requests[key], "status": "started"}, 202)
            rid = "run_" + uuid.uuid4().hex
            self.requests[key] = rid
            content = (
                body["input"] if isinstance(body["input"], str) else body["input"][-1]["content"]
            )
            prompt = (
                content
                if isinstance(content, str)
                else "\n".join(p.get("text", "") for p in content)
            )
            self.runs[rid] = {
                "status": "running",
                "session_id": body["session_id"],
                "input": prompt,
                "raw_input": body["input"],
                "model_options": body.get("model_options"),
                "run_id": rid,
                "model": body.get("model"),
                "provider": body.get("provider"),
                "approved": False,
                "created_at": time.time(),
            }
            messages = self.messages[body["session_id"]]
            stored = content
            if isinstance(content, list) and not self.persist_image_originals:
                stored = "\n".join(
                    p.get("text", "") if p.get("type") == "text" else "[screenshot]"
                    for p in content
                )
            messages.append(
                {
                    "id": max((m.get("id", 0) for m in messages), default=0) + 1,
                    "role": "user",
                    "content": stored,
                    "timestamp": time.time(),
                }
            )
            queue = self.runs[rid]["_queue"] = asyncio.Queue()
            self.runs[rid]["_task"] = asyncio.create_task(self.produce(self.runs[rid], queue))
            return JSONResponse({"run_id": rid, "status": "started"}, 202)
        if path.startswith("/v1/runs/"):
            rid = parts[2]
            if rid not in self.runs:
                return JSONResponse({}, 404)
            run = self.runs[rid]
            if path.endswith("/events"):
                if run.get("_queue") is None:
                    return JSONResponse({}, 404)
                return StreamingResponse(self.subscribe(run), media_type="text/event-stream")
            if path.endswith("/resolve-unknown"):
                lost = {k: run.get(k) for k in ("admission_id", "execution_generation")}
                if run["status"] != "unknown":
                    return JSONResponse({"error": {"code": "stale_generation"}}, 409)
                if body != lost:
                    return JSONResponse({"error": {"code": "invalid_params"}}, 400)
                run["status"] = "cancelled"
                messages = self.messages[run["session_id"]]
                note = "This turn did not complete. Some actions may already have run."
                messages.append(
                    {
                        "id": max((m.get("id", 0) for m in messages), default=0) + 1,
                        "role": "assistant",
                        "content": note,
                        "display_kind": "failed_turn",
                    }
                )
                return JSONResponse({"status": "terminal", "outcome": "interrupted", **lost})
            if path.endswith("/stop"):
                self.stops += 1
                run["status"] = "cancelled"
                return JSONResponse({"status": "stopping"})
            if path.endswith("/clarify") or (self.native_controls and path.endswith("/approval")):
                field = "choice" if path.endswith("/approval") else "answer"
                prompt = next(
                    (
                        p
                        for p in run.get("pending_controls") or []
                        if p["prompt_id"] == body.get("request_id")
                    ),
                    None,
                )
                if (
                    set(body) != {"request_id", "execution_generation", field}
                    or prompt is None
                    or body["execution_generation"] != prompt["execution_generation"]
                ):
                    return JSONResponse({"error": {"code": "invalid_params"}}, 400)
                run["pending_controls"] = []
                if field == "answer":
                    run.setdefault("answers", []).append(body["answer"])
                    return JSONResponse({"status": "resolved", "prompt_id": prompt["prompt_id"]})
            if path.endswith("/approval"):
                self.approvals += 1
                run["approval_choice"] = body["choice"]
                run["approved"] = True
                run["status"] = "running"
                return JSONResponse({"ok": True})
            if path.endswith("/steer"):
                if run["status"] != "running":
                    # Hermes takes guidance only while the run is working.
                    return JSONResponse({"error": {"code": "run_not_accepting_steer"}}, 409)
                run["steer"] = body["input"]
                return JSONResponse({"accepted": True})
            return JSONResponse({k: v for k, v in run.items() if not k.startswith("_")})
        return JSONResponse({}, 404)

    async def subscribe(self, run):
        queue = run["_queue"]
        try:
            while (event := await queue.get()) is not None:
                yield event
        finally:
            run["_queue"] = None

    async def produce(self, run, queue):
        async for event in self.events(run):
            await queue.put(event)
        await queue.put(None)

    async def events(self, run):
        def event(name, **fields):
            return (
                "data: " + json.dumps({"event": name, "run_id": run["run_id"], **fields}) + "\n\n"
            )

        approval = "approval" in run["input"].lower()
        slow = "slow" in run["input"].lower()
        yield event("tool.started", tool="read_file", preview="Reading the project notes")
        await asyncio.sleep(0.15)
        yield event("tool.completed", tool="read_file", duration=0.15)
        if "saved steps" in run["input"].lower():
            # Hermes saves each step as it finishes, before the reply is done.
            messages = self.messages[run["session_id"]]
            first = max((m.get("id", 0) for m in messages), default=0) + 1
            call = {"id": f"read-{first}", "function": {"name": "read_file", "arguments": "{}"}}
            messages += [
                {"id": first, "role": "assistant", "content": "", "tool_calls": [call]},
                {"id": first + 1, "role": "tool", "tool_call_id": call["id"], "content": "Notes"},
            ]
        if "lost in a restart" in run["input"].lower():
            # The gateway died mid-turn: the stream ends and the next owner reports it unknown.
            run.update(status="unknown", admission_id="lost-admission", execution_generation=2)
            return
        if approval:
            prompt = {"command": "python -m pytest", "choices": self.approval_choices}
            if self.native_controls:
                prompt.update(kind="approval", prompt_id="test-approval", execution_generation=3)
                run["pending_controls"] = [prompt]
            else:
                run["approval"] = prompt = {"request_id": "test-approval", **prompt}
            run["status"] = "waiting_for_approval"
            yield event("approval.request", **prompt)
            for _ in range(300):
                if run["approved"] or run["status"] == "cancelled":
                    break
                await asyncio.sleep(0.1)
            if self.native_controls and run["approved"]:
                yield event("approval.settled", prompt_id="test-approval", execution_generation=3)
        if "delegate" in run["input"].lower():
            child = "child-" + run["run_id"]
            self.sessions[child] = {
                "id": child,
                "title": "Review the project notes",
                "source": "subagent",
                "parent_session_id": run["session_id"],
                "model_config": {"_delegate_from": run["session_id"]},
            }
            self.messages[child] = [
                {"role": "user", "content": "Review the notes"},
                {"role": "assistant", "content": "The child transcript is ready."},
            ]
            yield event(
                "subagent.start",
                task_index=0,
                goal="Review the project notes",
                child_session_id=child,
                model="hermes-fast",
            )
            await asyncio.sleep(0.2)
            yield event(
                "subagent.complete",
                task_index=0,
                summary="The notes are ready.",
                child_session_id=child,
                duration_seconds=12.5,
                cost_usd=0.0024,
            )
        output = (
            "## A thoughtful place to start\n\n"
            "I’ve looked through the notes. Here’s a simple plan:\n\n"
            "1. Keep the interface **clear and calm**.\n2. Let Hermes do the work.\n"
            "3. Make every small interaction feel considered.\n\n"
            "```python\ndef hello(name):\n    return f'Hello, {name}'\n```\n\n"
            "What would you like to explore next?"
        )
        for i in range(0, len(output), 15):
            if run["status"] == "cancelled":
                yield event("run.cancelled")
                return
            yield event("message.delta", delta=output[i : i + 15])
            await asyncio.sleep(0.25 if slow else 0.015)
        run["status"], run["output"] = "completed", output
        messages = self.messages[run["session_id"]]
        if run.get("steer"):
            # Hermes saves delivered guidance as a typed user row inside the turn.
            messages.append(
                {
                    "id": max((m.get("id", 0) for m in messages), default=0) + 1,
                    "role": "user",
                    "display_kind": "steer",
                    "content": f"{STEER_OPEN}\n{run['steer']}\n[/OUT-OF-BAND USER MESSAGE]",
                }
            )
        messages.append(
            {
                "id": max((m.get("id", 0) for m in messages), default=0) + 1,
                "role": "assistant",
                "content": output,
            }
        )
        self.sessions[run["session_id"]].update(
            input_tokens=345,
            output_tokens=123,
            cache_read_tokens=200,
            cache_write_tokens=0,
            reasoning_tokens=42,
            api_call_count=1,
            estimated_cost_usd=0.0042,
        )
        yield event(
            "run.completed", output=output, usage={"input_tokens": 345, "output_tokens": 123}
        )
