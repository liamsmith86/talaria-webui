"""Optional, versioned Hermes plugin transport. Talaria never imports Hermes internals."""

import math

from starlette.requests import Request
from starlette.responses import JSONResponse

from .content import MAX_CHAT_BODY, image_inputs
from .hermes import APIError, identifier, object_result
from .plugin_status import release_status

PREFIX = "/talaria/v1"


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
    try:
        result = await client.request("GET", f"{PREFIX}/capabilities", timeout=3)
        if not isinstance(result, dict):
            return {}
        if type(result.get("version")) is int and result["version"] == 1:
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
                    )
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
        payload["json"] = {
            "message_id": mid,
            "preview": data.get("preview") is True,
            "revision": text_field(data, "revision", 64),
        }
    elif action == "response":
        try:
            mid = int(request.query_params.get("message_id", ""))
            if not 1 <= mid <= 2**63 - 1:
                raise ValueError
        except ValueError as exc:
            raise APIError("Choose a saved response.", 400) from exc
        payload["params"] = {"message_id": str(mid)}
    result = await request.app.state.hermes.request(
        request.method, f"{PREFIX}/sessions/{sid}/{action}", **payload
    )
    return JSONResponse(project_details(action, result))


async def commands(request: Request):
    from .routes import body, text_field

    path = f"{PREFIX}/commands"
    if command_id := request.path_params.get("command_id"):
        path += f"/{identifier(command_id)}"
    options = {}
    if request.method == "POST":
        data = await body(request, 12000)
        options["json"] = {
            key: text_field(data, key, limit)
            for key, limit in (
                ("command", 80),
                ("args", 2000),
                ("session_id", 256),
                ("request_id", 128),
            )
        }
    return JSONResponse(await request.app.state.hermes.request(request.method, path, **options))
