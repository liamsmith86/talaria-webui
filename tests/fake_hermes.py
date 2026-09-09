"""Deterministic public-protocol peer for functional and visual tests."""

import asyncio
import json
import time
import uuid

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

KEY = "test-hermes-key-do-not-use"


class FakeHermes:
    def __init__(self):
        self.sessions = {}
        self.messages = {}
        self.runs = {}
        self.requests = {}
        self.stops = 0
        self.approvals = 0
        self.approval_choices = ["once", "session", "deny"]
        self.default_model = "hermes-test"
        self.default_provider = "test"
        self.discovery_overrides = {}
        self.app = Starlette(
            routes=[
                Route(
                    "/{path:path}", self.handle, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
                )
            ]
        )

    async def handle(self, request: Request):
        if request.headers.get("authorization") != f"Bearer {KEY}":
            return JSONResponse({"error": "Unauthorized"}, 401)
        path = request.url.path
        body = await request.json() if request.method in {"POST", "PATCH", "PUT"} else {}
        if path in self.discovery_overrides:
            return JSONResponse(*self.discovery_overrides[path])
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
                        **{
                            key: True
                            for key in [
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
                            ]
                        },
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
                    "platforms": {"api_server": {"state": "connected"}},
                    "pid": 12345,
                    "private_future_field": KEY,
                }
            )
        if path == "/v1/skills":
            return JSONResponse(
                {
                    "data": [
                        {
                            "name": "project-notes",
                            "description": "Read project notes",
                            "secret": KEY,
                        }
                    ]
                }
            )
        if path == "/v1/toolsets":
            return JSONResponse(
                {
                    "data": [
                        {
                            "name": "files",
                            "label": "File tools",
                            "enabled": True,
                            "configured": True,
                            "tools": ["read_file", "write_file"],
                            "secret": KEY,
                        }
                    ]
                }
            )
        if path == "/api/sessions":
            if request.method == "GET":
                offset = int(request.query_params.get("offset", 0))
                values = list(self.sessions.values())[::-1]
                return JSONResponse(
                    {"data": values[offset : offset + 100], "has_more": len(values) > offset + 100}
                )
            sid = uuid.uuid4().hex
            if body.get("title") and any(
                s.get("title") == body["title"] for s in self.sessions.values()
            ):
                return JSONResponse(
                    {"error": {"code": "invalid_title", "message": "Duplicate title"}}, 400
                )
            record = {"id": sid, "title": body.get("title", "Untitled"), "started_at": time.time()}
            self.sessions[sid], self.messages[sid] = record, []
            return JSONResponse({"object": "hermes.session", "session": record}, 201)
        parts = path.strip("/").split("/")
        if path.startswith("/api/sessions/"):
            sid = parts[2]
            if sid not in self.sessions:
                return JSONResponse({}, 404)
            if path.endswith("/messages"):
                offset = int(request.query_params.get("offset", "0"))
                limit = int(request.query_params.get("limit", "100"))
                all_messages = self.messages[sid]
                end = max(0, len(all_messages) - offset)
                return JSONResponse(
                    {"session_id": sid, "data": all_messages[max(0, end - limit) : end]}
                )
            if path.endswith("/fork"):
                new_id = uuid.uuid4().hex
                self.sessions[new_id] = {**self.sessions[sid], "id": new_id, "title": body["title"]}
                self.messages[new_id] = list(self.messages[sid])
                return JSONResponse(
                    {"object": "hermes.session", "session": self.sessions[new_id]}, 201
                )
            if request.method == "PATCH":
                self.sessions[sid]["title"] = body["title"]
            if request.method == "DELETE":
                del self.sessions[sid]
                del self.messages[sid]
                return JSONResponse({"ok": True})
            return JSONResponse(self.sessions[sid])
        if path == "/v1/runs":
            key = request.headers.get("idempotency-key")
            if key in self.requests:
                return JSONResponse({"run_id": self.requests[key], "status": "started"}, 202)
            rid = "run_" + uuid.uuid4().hex
            self.requests[key] = rid
            self.runs[rid] = {
                "status": "running",
                "session_id": body["session_id"],
                "input": body["input"],
                "run_id": rid,
                "model": body.get("model"),
                "provider": body.get("provider"),
                "approved": False,
            }
            self.messages[body["session_id"]].append({"role": "user", "content": body["input"]})
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
            if path.endswith("/stop"):
                self.stops += 1
                run["status"] = "cancelled"
                return JSONResponse({"status": "stopping"})
            if path.endswith("/approval"):
                self.approvals += 1
                run["approval_choice"] = body["choice"]
                run["approved"] = True
                run["status"] = "running"
                return JSONResponse({"ok": True})
            if path.endswith("/steer"):
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
        if approval:
            run["approval"] = {
                "request_id": "test-approval",
                "command": "python -m pytest",
                "choices": self.approval_choices,
            }
            run["status"] = "waiting_for_approval"
            yield event("approval.request", **run["approval"])
            for _ in range(300):
                if run["approved"] or run["status"] == "cancelled":
                    break
                await asyncio.sleep(0.1)
        if "delegate" in run["input"].lower():
            yield event("subagent.start", task_index=0, goal="Review the project notes")
            await asyncio.sleep(0.2)
            yield event("subagent.complete", task_index=0, summary="The notes are ready.")
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
        self.messages[run["session_id"]].append({"role": "assistant", "content": output})
        yield event(
            "run.completed", output=output, usage={"input_tokens": 345, "output_tokens": 123}
        )
