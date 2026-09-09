"""Optional, versioned Hermes plugin transport. Talaria never imports Hermes internals."""

import math

from starlette.requests import Request
from starlette.responses import JSONResponse

from .content import MAX_CHAT_BODY, image_inputs
from .hermes import APIError, identifier

PREFIX = "/talaria/v1"


def numeric(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def project_details(action, result):
    from .metadata import text

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
    try:
        result = await client.request("GET", f"{PREFIX}/capabilities", timeout=3)
        if type(result.get("version")) is int and result["version"] == 1:
            agent = result.get("agent")
            name = agent.get("name") if isinstance(agent, dict) else None
            return {
                "version": 1,
                "agent": {"name": name[:80] if isinstance(name, str) else ""},
                **{
                    key: result.get(key) is True
                    for key in ("response_details", "context_usage", "rewind")
                },
            }
    except APIError:
        pass
    return {}


async def session_extension(request: Request):
    from .routes import body, text_field

    sid = identifier(request.path_params["session_id"])
    action = request.url.path.rsplit("/", 1)[-1]
    payload = {}
    if request.method == "POST":
        data = await body(request, MAX_CHAT_BODY)
        mid = data.get("message_id")
        if type(mid) is not int or mid < 1:
            raise APIError("Choose a saved message.", 400)
        if "replacement" in data:
            replacement = data["replacement"]
            if not isinstance(replacement, dict):
                raise APIError("Invalid replacement message.", 400)
            prompt = text_field(replacement, "input", 50000).strip()
            images = image_inputs(replacement.get("images", []))
            if not prompt and not images:
                raise APIError("Write a message first.", 400)
        payload["json"] = {
            "message_id": mid,
            "preview": data.get("preview") is True,
            "revision": text_field(data, "revision", 64),
        }
    elif action == "response":
        try:
            mid = int(request.query_params.get("message_id", ""))
            if mid < 1:
                raise ValueError
        except ValueError as exc:
            raise APIError("Choose a saved response.", 400) from exc
        payload["params"] = {"message_id": str(mid)}
    result = await request.app.state.hermes.request(
        request.method, f"{PREFIX}/sessions/{sid}/{action}", **payload
    )
    return JSONResponse(project_details(action, result))
