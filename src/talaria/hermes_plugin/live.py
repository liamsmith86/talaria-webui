"""Bridge native reasoning, status and clarify callbacks onto the existing run stream."""

import asyncio
import hashlib
import inspect
import threading
import uuid
from contextlib import suppress
from functools import wraps


def supported(adapter):
    try:
        from gateway.platforms import api_server_runs
        from tools import clarify_gateway

        inspect.signature(api_server_runs._load_owned_run).bind(
            adapter, None, _api_server=None, permission="dispatch", active_fallback=False
        )
        return all(
            callable(getattr(adapter, name, None))
            for name in (
                "_make_run_event_callback",
                "_set_run_status",
                "_request_owns_run",
            )
        ) and callable(clarify_gateway.wait_for_response)
    except (ImportError, AttributeError, TypeError, ValueError):
        return False


class LiveRun:
    def __init__(self, adapter, run_id, loop):
        self.adapter, self.run_id, self.loop = adapter, run_id, loop
        self.reasoning_id = None
        self.question_lock = threading.Lock()

    def emit(self, event, *, status=None, **fields):
        def publish():
            current = self.adapter._run_statuses.get(self.run_id, {})
            if current.get("status") in {"completed", "failed", "cancelled", "interrupted"}:
                return
            updates = {"last_event": event}
            if event == "talaria.clarification.request":
                updates["clarification"] = fields
            elif event == "talaria.clarification.resolved":
                updates["clarification"] = None
            self.adapter._set_run_status(
                self.run_id, status or current.get("status", "running"), **updates
            )
            queue = self.adapter._run_streams.get(self.run_id)
            if queue is not None:
                queue.put_nowait({"event": event, "run_id": self.run_id, **fields})

        with suppress(RuntimeError):
            self.loop.call_soon_threadsafe(publish)

    def reasoning(self, delta):
        if not isinstance(delta, str) or not delta:
            return
        if self.reasoning_id is None:
            self.reasoning_id = uuid.uuid4().hex
        self.emit("talaria.reasoning.delta", block_id=self.reasoning_id, delta=delta)

    def status(self, kind, message, **kwargs):
        if isinstance(message, str) and message:
            self.emit("talaria.status", kind=str(kind), text=message[:4000])

    def progress(self, original):
        @wraps(original)
        def report(event, *args, **kwargs):
            if event == "reasoning.available":
                return  # Native runs use this for a completed prose preview, not reasoning.
            if event in {"tool.started", "tool.completed"}:
                self.reasoning_id = None
            return original(event, *args, **kwargs)

        return report

    def bind(self, agent):
        if not getattr(agent, "reasoning_callback", None):
            agent.reasoning_callback = self.reasoning
        if not getattr(agent, "status_callback", None):
            agent.status_callback = self.status
        if not getattr(agent, "clarify_callback", None):
            agent.clarify_callback = lambda question, choices, multi_select=False: self.clarify(
                agent, question, choices, multi_select
            )
        stream = agent.stream_delta_callback

        @wraps(stream)
        def text(delta):
            if delta:
                self.reasoning_id = None
            return stream(delta)

        agent.stream_delta_callback = text

    async def cancel_when_stopped(self, agent, request_id):
        from tools import clarify_gateway

        try:
            while not getattr(agent, "_interrupt_requested", False):
                if self.run_id in self.adapter._stopping_run_ids:
                    break
                await asyncio.sleep(0.1)
        finally:
            clarify_gateway.resolve_gateway_clarify(request_id, "")

    def clarify(self, agent, question, choices, multi_select):
        with self.question_lock:
            if getattr(agent, "_interrupt_requested", False):
                return ""
            return self.ask(agent, question, choices, multi_select)

    def ask(self, agent, question, choices, multi_select):
        from tools import clarify_gateway
        from tools.clarify_tool import TIMEOUT_RESPONSE

        if len(question) > 16000 or any(len(choice) > 4000 for choice in choices or []):
            raise ValueError("Use a shorter clarification question or choices.")
        request_id = uuid.uuid4().hex
        clarify_gateway.register(request_id, self.run_id, question, choices, multi_select)
        self.emit(
            "talaria.clarification.request",
            status="waiting_for_input",
            request_id=request_id,
            question=question,
            choices=choices,
            multi_select=bool(multi_select),
        )
        monitor = asyncio.run_coroutine_threadsafe(
            self.cancel_when_stopped(agent, request_id), self.loop
        )
        try:
            response = clarify_gateway.wait_for_response(
                request_id, timeout=clarify_gateway.get_clarify_timeout()
            )
            return TIMEOUT_RESPONSE if response is None else response
        finally:
            monitor.cancel()
            self.reasoning_id = None
            self.emit("talaria.clarification.resolved", status="running", request_id=request_id)


async def answer(request, adapter):
    from aiohttp import web

    if not supported(adapter):
        return web.json_response({"error": "Interactive questions unavailable."}, status=404)
    from gateway.platforms import api_server, api_server_runs
    from tools import clarify_gateway

    run_id, status, _, _, error = api_server_runs._load_owned_run(
        adapter, request, _api_server=api_server, permission="dispatch", active_fallback=False
    )
    if error is not None:
        return error
    data = await request.json()
    request_id = data.get("request_id") if isinstance(data, dict) else None
    response = data.get("answer") if isinstance(data, dict) else None
    if (
        not isinstance(request_id, str)
        or len(request_id) > 256
        or not isinstance(response, str)
        or len(response) > 16000
    ):
        return web.json_response({"error": "Invalid answer."}, status=400)
    digest = hashlib.sha256(response.encode()).hexdigest()
    receipt = {"request_id": request_id, "digest": digest}
    if status.get("clarification_answer") == receipt:
        return web.json_response({"ok": True})
    pending = status.get("clarification") or {}
    if (
        status.get("status") != "waiting_for_input"
        or pending.get("request_id") != request_id
        or run_id in adapter._stopping_run_ids
    ):
        return web.json_response(
            {"error": "This question is no longer waiting for an answer."}, status=409
        )
    if not clarify_gateway.resolve_gateway_clarify(request_id, response):
        return web.json_response({"error": "This question has already settled."}, status=409)
    adapter._set_run_status(run_id, "running", clarification=None, clarification_answer=receipt)
    return web.json_response({"ok": True})
