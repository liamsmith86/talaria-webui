"""Authenticated browser requests can only submit fixed launcher operations."""

import asyncio

from starlette.responses import JSONResponse

from . import control, installation
from .hermes import APIError
from .routes import body


async def submit(request):
    if request.app.state.development:
        raise APIError("Updates are unavailable in a development checkout.", 409)
    root = installation.managed_root()
    if root is None or not (root / control.SOCKET).exists():
        raise APIError("Start Talaria with its managed launcher to enable updates.", 409)
    if request.path_params["action"] not in {"check", "update"}:
        raise APIError("Unknown update action.", 404)
    data = await body(request)
    operation = {**data, "action": request.path_params["action"]}
    try:
        control.validate(operation)
    except control.ControlError as exc:
        raise APIError(str(exc), 400) from exc
    try:
        result = await asyncio.to_thread(control.request, root, operation)
    except control.ControlError as exc:
        raise APIError(str(exc), 409) from exc
    except (OSError, ValueError) as exc:
        # The request may already have been accepted. Clients reconcile by id;
        # they must not blindly repeat an update after a lost response.
        raise APIError("Launcher connection interrupted. Checking update status…", 503) from exc
    return JSONResponse(result, status_code=202)
