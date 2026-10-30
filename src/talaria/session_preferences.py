"""Each session's model and reasoning choice, the same on every device.

Hermes saves a session's model only after a turn uses it and never saves the
reasoning level, so a choice made before sending lives here. Hermes's own
session model still applies to a session nobody has chosen for in Talaria.
"""

import asyncio

from starlette.requests import Request
from starlette.responses import JSONResponse

from .content import REASONING
from .hermes import IDENTIFIER, APIError, identifier
from .session_state import StateUnavailable

MAX_IMPORT = 400


def model_choice(value):
    if value is None:
        return None
    if (
        isinstance(value, dict)
        and isinstance(value.get("id"), str)
        and isinstance(value.get("provider"), str)
        and 0 < len(value["id"]) <= 256
        and len(value["provider"]) <= 256
    ):
        return {"id": value["id"], "provider": value["provider"]}
    raise APIError("Choose a model from the list.", 400, "invalid_model")


def reasoning_choice(value):
    if value == "auto" or (isinstance(value, str) and value in REASONING):
        return value
    raise APIError("Choose a supported reasoning level.", 400, "invalid_reasoning")


def changes(data):
    if not isinstance(data, dict) or not data or set(data) - {"model", "reasoning"}:
        raise APIError("Choose a model or reasoning level.", 400)
    result = {}
    if "model" in data:
        result["model"] = model_choice(data["model"])
    if "reasoning" in data:
        result["reasoning"] = reasoning_choice(data["reasoning"])
    return result


async def saved_choice(state, sid, parent):
    """The choice that goes with Hermes's session details, or None if unreadable.

    Viewing a session never depends on Talaria's own records. Without the
    choice, each browser keeps whatever it already shows.
    """
    parent = parent if isinstance(parent, str) and IDENTIFIER.fullmatch(parent) else None
    try:
        return await asyncio.to_thread(
            state.session_state.preferences, state.profile_id, sid, parent
        )
    except StateUnavailable:
        return None


async def update(request: Request):
    from .routes import body

    sid = identifier(request.path_params["session_id"])
    choice = changes(await body(request, 4096))
    state = request.app.state
    await asyncio.to_thread(state.session_state.set_preferences, state.profile_id, sid, choice)
    return JSONResponse({"ok": True})


async def import_choices(request: Request):
    """Adopt choices Talaria 0.3.20 and earlier kept in one browser.

    A choice another device already saved is never replaced.
    """
    from .routes import body

    data = await body(request, 256 * 1024)
    entries = data.get("sessions")
    if not isinstance(entries, dict) or len(entries) > MAX_IMPORT:
        raise APIError("Invalid saved choices.", 400)
    accepted = {}
    for sid, choice in entries.items():
        if not isinstance(sid, str) or not IDENTIFIER.fullmatch(sid):
            continue
        try:
            accepted[sid] = changes(choice)
        except APIError:
            continue
    state = request.app.state
    await asyncio.to_thread(state.session_state.import_preferences, state.profile_id, accepted)
    return JSONResponse({"imported": len(accepted)})
