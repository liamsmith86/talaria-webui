"""Small, owner-filtered projections of the native API run registry."""

import inspect
import math
from itertools import islice

STATES = {
    "queued",
    "running",
    "waiting_for_approval",
    "waiting_for_input",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
}


def supported(adapter):
    try:
        inspect.signature(adapter._request_owns_run).bind(None, "run")
        return isinstance(adapter._run_statuses, dict)
    except (AttributeError, TypeError, ValueError):
        return False


def snapshot(adapter, request):
    sessions = {}
    # Ownership remains native and profile/key-scoped. A restored old run can
    # be inserted after a newer one, so use native creation times within this
    # bounded window. Never expose prompts, controls or errors.
    for rid in islice(reversed(adapter._run_statuses), 256):
        run = adapter._run_statuses[rid]
        sid, status = run.get("session_id"), run.get("status")
        created = run.get("created_at")
        if not isinstance(sid, str) or status not in STATES:
            continue
        if type(created) not in (int, float) or not math.isfinite(created):
            continue
        if sid in sessions and sessions[sid]["created_at"] >= created:
            continue
        if adapter._request_owns_run(request, rid):
            sessions[sid] = {
                "session_id": sid,
                "run_id": rid,
                "status": status,
                "created_at": created,
            }
    return {"data": list(sessions.values()), "scope": "api_runs"}


async def dispatch(request, adapter):
    from aiohttp import web

    if not supported(adapter):
        return web.json_response({"error": "Session activity unavailable."}, status=404)
    return web.json_response(snapshot(adapter, request))
