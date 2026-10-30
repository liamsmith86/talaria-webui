"""Optional, versioned Hermes plugin transport. Talaria never imports Hermes internals."""

import asyncio
import math
import time

from starlette.requests import Request
from starlette.responses import JSONResponse

from .content import MAX_CHAT_BODY, image_inputs
from .hermes import APIError, identifier, object_result
from .plugin_status import release_status

PREFIX = "/talaria/v1"
# A failed check is retried at most this often by requests that need the features.
RECHECK_SECONDS = 30


def numeric(value):
    try:
        return (
            value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
        )
    except OverflowError:
        return None


def project_details(action, result):
    from .metadata import text

    result = object_result(result)
    if action == "context":
        context = result.get("context")
        return {
            "context": {
                "used": numeric(context.get("used")),
                "maximum": numeric(context.get("maximum")),
                "model": text(context.get("model"), 256),
            }
            if isinstance(context, dict)
            else None
        }
    if action == "response":
        usage = result.get("usage")
        return {
            **{
                key: text(result.get(key), 256)
                for key in ("model", "provider", "reasoning", "finish_reason")
            },
            "duration_seconds": numeric(result.get("duration_seconds")),
            "has_reasoning": result.get("has_reasoning") is True,
            "usage": {
                key: numeric(usage.get(key))
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                    "cache_read_tokens",
                )
            }
            if isinstance(usage, dict)
            else None,
        }
    return result


async def discover(client):
    """The plugin's features: {} without a usable plugin, or None when the check failed."""
    try:
        result = await client.request("GET", f"{PREFIX}/capabilities", timeout=3)
    except APIError as exc:
        # Only Hermes answering that the route does not exist means no plugin.
        # A slow or failed check says nothing about it.
        return {} if exc.status == 404 else None
    if isinstance(result, dict) and type(result.get("version")) is int and result["version"] == 1:
        agent = result.get("agent")
        name = agent.get("name") if isinstance(agent, dict) else None
        context = result.get("profile_context")
        context = context if isinstance(context, dict) else {}
        return {
            "version": 1,
            "release": release_status(result),
            "agent": {"name": name[:80] if isinstance(name, str) else ""},
            "profile_context": {
                key: context.get(key)
                if isinstance(context.get(key), str)
                and context[key] in {"ready", "not_configured", "unavailable"}
                else "unavailable"
                for key in ("instructions", "prefill")
            },
            **{
                key: result.get(key) is True
                for key in (
                    "commands",
                    "response_details",
                    "context_usage",
                    "model_details",
                    "context_runs",
                    "live_interactions",
                    "rewind",
                    "history_search",
                    "session_activity",
                    "external_session_copies",
                )
            },
        }
    return {}


class Extensions:
    """What the plugin offers one Hermes connection.

    Unknown until a check answers: the first request that needs a feature asks,
    and a failed check never hides features from every tab.
    """

    def __init__(self):
        self.features = {}
        self.known = False
        self.retry = 0.0
        self.lock = asyncio.Lock()

    def adopt(self, found):
        """Keep what a check found. A failed check (None) changes nothing."""
        if found is not None:
            self.features, self.known = found, True
        return self.features

    async def check(self, hermes):
        """The features, asking Hermes when no check has answered yet."""
        if not self.known and time.monotonic() >= self.retry:
            async with self.lock:
                if not self.known and time.monotonic() >= self.retry:
                    self.adopt(await discover(hermes))
                    if not self.known:
                        self.retry = time.monotonic() + RECHECK_SECONDS
        return self.features


def plugin_refusal(exc):
    """The plugin's own explanation of why it refused, written for people."""
    return APIError(exc.upstream_message or exc.message, exc.status, exc.code)


async def rewind_request(request: Request):
    from .routes import body, text_field

    data = await body(request, MAX_CHAT_BODY)
    mid = data.get("message_id")
    if type(mid) is not int or not 1 <= mid <= 2**63 - 1:
        raise APIError("Choose a saved message.", 400)
    if "replacement" in data:
        replacement = data["replacement"]
        if not isinstance(replacement, dict):
            raise APIError("Invalid replacement message.", 400)
        prompt = text_field(replacement, "input", 50000).strip()
        images = image_inputs(replacement.get("images", []))
        if not prompt and not images:
            raise APIError("Write a message first.", 400)
    return {
        "message_id": mid,
        "preview": data.get("preview") is True,
        "revision": text_field(data, "revision", 64),
    }


async def session_extension(request: Request):
    sid = identifier(request.path_params["session_id"])
    action = request.url.path.rsplit("/", 1)[-1]
    payload = {}
    if request.method == "POST":
        payload["json"] = await rewind_request(request)
    elif action == "response":
        try:
            mid = int(request.query_params.get("message_id", ""))
            if not 1 <= mid <= 2**63 - 1:
                raise ValueError
        except ValueError as exc:
            raise APIError("Choose a saved response.", 400) from exc
        payload["params"] = {"message_id": str(mid)}
    try:
        result = await request.app.state.hermes.request(
            "GET" if request.method == "HEAD" else request.method,
            f"{PREFIX}/sessions/{sid}/{action}",
            **payload,
        )
    except APIError as exc:
        # The plugin explains why a turn cannot change, in words written for people.
        if action == "rewind" and exc.status == 400 and exc.upstream_message:
            raise plugin_refusal(exc) from exc
        raise
    projected = project_details(action, result)
    if action == "context" and projected["context"] is None:
        projected["context"] = await reply_context(request, sid)
    return JSONResponse(projected)


async def reply_context(request, sid):
    """The usage of the reply that now ends the conversation.

    The plugin reports only its newest observation and drops it once a rewind
    removes that reply, although the earlier reply now ending the conversation
    keeps its own. Kept rows that compaction re-saved have no observation, so
    this never reports usage from before a compaction.
    """
    from .metadata import text

    try:
        mid = int(request.query_params.get("reply", ""))
    except ValueError:
        return None
    if not 1 <= mid <= 2**63 - 1:
        return None
    try:
        detail = object_result(
            await request.app.state.hermes.request(
                "GET",
                f"{PREFIX}/sessions/{sid}/response",
                params={"message_id": str(mid)},
            )
        )
    except APIError:
        return None
    usage = detail.get("usage")
    used = numeric(usage.get("input_tokens")) if isinstance(usage, dict) else None
    if used is None:
        return None
    return {
        "used": used,
        "maximum": numeric(detail.get("context_max")),
        "model": text(detail.get("model"), 256),
    }
