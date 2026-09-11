"""Opt-in model inputs from Hermes's native hook, confined to Talaria sessions."""

import json
import logging
import os
import stat
import threading
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps

log = logging.getLogger(__name__)
MAX_RECORD = 8 * 1024 * 1024
MAX_FILE = 16 * 1024 * 1024
BACKUPS = 3
active_run = ContextVar("talaria_request_log_run", default=False)
PARAMETERS = frozenset(
    {
        "model",
        "temperature",
        "top_p",
        "top_k",
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
        "stop",
        "stream",
        "seed",
        "reasoning",
        "reasoning_effort",
        "thinking",
        "service_tier",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "response_format",
        "frequency_penalty",
        "presence_penalty",
    }
)


def scoped_run(run):
    """Mark only this agent's invocation; Hermes propagates context into hook workers."""

    @wraps(run)
    def invoke(*args, **kwargs):
        token = active_run.set(True)
        try:
            return run(*args, **kwargs)
        finally:
            active_run.reset(token)

    return invoke


def private_append(path, data):
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "ab") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise ValueError("Request log must be a regular file")
        os.fchmod(file.fileno(), 0o600)
        file.write(data)


def append_record(home, record):
    directory = home / "talaria"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "request-debug.jsonl"
    data = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode()
    if len(data) > MAX_RECORD:
        # Never leave a truncated prompt looking like a complete provider input.
        data = (
            json.dumps(
                {
                    "event": "request_omitted",
                    "session_id": record.get("session_id"),
                    "api_request_id": record.get("api_request_id"),
                    "time": record["time"],
                    "reason": "record exceeds 8 MiB",
                    "bytes": len(data),
                }
            )
            + "\n"
        ).encode()
    if path.exists() and path.lstat().st_size + len(data) > MAX_FILE:
        for index in range(BACKUPS, 0, -1):
            source = path if index == 1 else path.with_suffix(f".jsonl.{index - 1}")
            if source.exists():
                source.replace(path.with_suffix(f".jsonl.{index}"))
    private_append(path, data)


class RequestLog:
    def __init__(self, ctx):
        self.ctx = ctx
        self.lock = threading.Lock()

    def before(self, *, platform=None, request_messages=None, system_prompt=None, **kwargs):
        try:
            from hermes_constants import get_hermes_home

            if platform != "api_server" or not active_run.get():
                return
            if self.ctx.get_config("debug_requests", False) is not True:
                return
            record = {
                "event": "model_request",
                "time": datetime.now(UTC).isoformat(),
                **{
                    key: kwargs.get(key)
                    for key in (
                        "session_id",
                        "turn_id",
                        "api_request_id",
                        "model",
                        "provider",
                        "api_mode",
                        "api_call_count",
                        "retry_count",
                        "user_message",
                    )
                },
                "system_prompt": system_prompt,
                "messages": request_messages,
            }
            # This separate hook field is already sanitized/capped by Hermes.
            # Full prompts above come from the native observer passthrough fields.
            body = (kwargs.get("request") or {}).get("body", {})
            record["parameters"] = {key: body[key] for key in PARAMETERS if key in body}
            extra = body.get("extra_body")
            if isinstance(extra, dict):
                record["parameters"]["extra_body"] = {
                    key: extra[key] for key in PARAMETERS | {"provider"} if key in extra
                }
            record["prompt_available"] = isinstance(request_messages, list)
            with self.lock:
                append_record(get_hermes_home(), record)
        except Exception as error:
            # Debugging must not break a run or echo potentially sensitive exception text.
            log.warning("Talaria request logging failed (%s)", type(error).__name__)
