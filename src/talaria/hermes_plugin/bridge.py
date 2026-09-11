"""Versioned routes through Hermes's native plugin seam; no patched core or second runtime."""

import asyncio
import hashlib
import inspect
import json
import logging

from .observations import Observations
from .release import LOADED_REVISION, PLUGIN_VERSION
from .request_log import RequestLog

log = logging.getLogger(__name__)
PREFIX = "/talaria/v1"
MAX_MESSAGES = 20000


def enabled():
    """Each profile opts in independently; malformed configuration grants nothing."""
    try:
        from hermes_cli.config import load_config

        config = load_config()
        plugins = config.get("plugins") if isinstance(config, dict) else None
        if not isinstance(plugins, dict):
            return False
        allowed, denied = plugins.get("enabled"), plugins.get("disabled")
        return (
            isinstance(allowed, list)
            and (denied is None or isinstance(denied, list))
            and "talaria" in allowed
            and "talaria" not in (denied or [])
        )
    except Exception:
        return False


def supports_rewind(db):
    try:
        if not callable(getattr(db, "get_active_message_ids", None)):
            return False
        inspect.signature(db.rewind_to_message).bind(
            "session",
            1,
            expected_active_ids=[],
            preserve_compaction_handoff=False,
            expected_target_content="",
        )
        return True
    except (AttributeError, TypeError, ValueError):
        return False


def message_identifier(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("Choose a saved message.")
    return value


def revision(ids, content):
    return hashlib.sha256(json.dumps([ids, content], separators=(",", ":")).encode()).hexdigest()


def rewind_preview(db, sid, message_id):
    from agent.context_compressor import split_user_originated_turn
    from agent.memory_manager import sanitize_context

    if (db.get_session(sid) or {}).get("source") != "api_server":
        raise ValueError("Only API sessions can be changed here.")

    ids = db.get_active_message_ids(sid)
    if len(ids) > MAX_MESSAGES:
        raise ValueError("This session is too large to rewind here.")
    rows = db.get_messages(sid, limit=MAX_MESSAGES + 1)
    # Active-id CAS detects messages arriving during the read as well as during confirmation.
    if [row["id"] for row in rows] != ids:
        raise RuntimeError("The session changed. Reopen the action and try again.")
    index = next((i for i, row in enumerate(rows) if row["id"] == message_id), None)
    if index is None or rows[index].get("role") not in {"user", "assistant"}:
        raise ValueError("This message is no longer available to change.")
    target = next(
        (i for i in range(index, -1, -1) if split_user_originated_turn(rows[i])[1] is not None),
        None,
    )
    if target is None:
        raise ValueError(
            "The original message is in older, compacted history and cannot be changed here."
        )
    handoff, user = split_user_originated_turn(rows[target])
    content = user.get("content")
    if isinstance(content, str):
        content = sanitize_context(content).strip()
    return (
        {
            "revision": revision(ids, content),
            "target_id": rows[target]["id"],
            "message_count": len(rows) - target,
            "turn_count": sum(
                split_user_originated_turn(row)[1] is not None for row in rows[target:]
            ),
            "user": {"id": rows[target]["id"], "role": "user", "content": content},
        },
        ids,
        handoff is not None,
    )


def rewind(db, sid, data):
    preview, ids, composite = rewind_preview(db, sid, data["message_id"])
    if data.get("preview") is True:
        return preview
    if data.get("revision") != preview["revision"]:
        raise RuntimeError("The session changed. Reopen the action and try again.")
    result = db.rewind_to_message(
        sid,
        preview["target_id"],
        expected_active_ids=ids,
        preserve_compaction_handoff=composite,
        expected_target_content=preview["user"]["content"],
    )
    return {"ok": True, "removed": result["rewound_count"]}


def wire(app, adapter, **kwargs):
    if not callable(getattr(adapter, "_check_auth", None)) or not callable(
        getattr(adapter, "_ensure_session_db_async", None)
    ):
        log.warning("Talaria routes unavailable: incompatible Hermes API adapter")
        return

    from .commands import CommandJobs

    commands = CommandJobs()
    app.on_cleanup.append(commands.close)

    async def dispatch_request(request):
        return await dispatch(request, adapter, commands)

    for prefix in (PREFIX, f"/p/{{profile}}{PREFIX}"):
        app.router.add_get(f"{prefix}/capabilities", dispatch_request)
        app.router.add_get(f"{prefix}/{{action:commands}}", dispatch_request)
        app.router.add_post(f"{prefix}/{{action:commands}}", dispatch_request)
        app.router.add_get(f"{prefix}/{{action:commands}}/{{command_id}}", dispatch_request)
        app.router.add_get(f"{prefix}/{{action:models|search|activity}}", dispatch_request)
        app.router.add_post(f"{prefix}/{{action:runs}}", dispatch_request)
        app.router.add_post(f"{prefix}/runs/{{run_id}}/{{action:clarification}}", dispatch_request)
        app.router.add_get(
            f"{prefix}/sessions/{{session_id}}/{{action:context|response|around}}", dispatch_request
        )
        app.router.add_post(
            f"{prefix}/sessions/{{session_id}}/{{action:rewind|fork}}", dispatch_request
        )


def register(ctx):
    from hermes_constants import get_hermes_home

    observations = Observations(get_hermes_home())
    ctx.register_platform_handler("api_server", wire)
    ctx.register_hook("pre_api_request", observations.before)
    ctx.register_hook("pre_api_request", RequestLog(ctx).before)
    ctx.register_hook("post_api_request", observations.after)
    ctx.register_hook("on_session_end", observations.end)


async def dispatch(request, adapter, commands=None):
    from aiohttp import web

    auth_error = adapter._check_auth(request)
    if auth_error is not None:
        return auth_error
    # Named profiles opt in independently, even when the root listener wires the routes.
    if not await asyncio.to_thread(enabled):
        return web.json_response(
            {"error": "Talaria plugin is not enabled for this profile."}, status=404
        )
    try:
        return await dispatch_action(request, adapter, commands)
    except (ValueError, TypeError):
        return web.json_response(
            {"error": "This message cannot be changed safely. Refresh the session and try again."},
            status=400,
        )
    except RuntimeError:
        return web.json_response(
            {
                "error": "The session changed or is busy. "
                "Wait for it to finish, then reopen this action."
            },
            status=409,
        )
    except Exception:
        log.exception("Talaria extension request failed")
        return web.json_response(
            {"error": "Hermes could not complete this action. Your session is still available."},
            status=503,
        )


async def capabilities(adapter, db):
    from aiohttp import web
    from hermes_constants import get_hermes_home

    from .activity import supported as activity_supported
    from .commands import available
    from .context import load_context, supports_context_runs
    from .identity import inspect_home
    from .live import supported as live_supported
    from .search import supported as search_supported

    identity = await asyncio.to_thread(inspect_home, str(get_hermes_home()))
    context_runs = supports_context_runs(adapter)
    context = await asyncio.to_thread(load_context) if context_runs else None
    return web.json_response(
        {
            "version": 1,
            "revision": LOADED_REVISION,
            "plugin_version": PLUGIN_VERSION,
            "commands": available(adapter),
            "response_details": True,
            "context_usage": True,
            "model_details": True,
            "context_runs": context_runs,
            "live_interactions": context_runs and live_supported(adapter),
            "profile_context": context.public() if context else {},
            "rewind": supports_rewind(db),
            "history_search": search_supported(db, adapter),
            "session_activity": activity_supported(adapter),
            "agent": {"name": identity.name},
        }
    )


async def session_details(request, db, sid, action):
    from aiohttp import web
    from hermes_constants import get_hermes_home

    can_rewind = supports_rewind(db)
    observations = Observations(get_hermes_home())
    if action == "rewind":
        if not can_rewind:
            return web.json_response(
                {"error": "Update Hermes to enable session changes."}, status=501
            )
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("Choose a saved message.")
        message_identifier(data.get("message_id"))
        return web.json_response(await asyncio.to_thread(rewind, db, sid, data))
    message_id = request.query.get("message_id")
    if action == "response":
        message_id = message_identifier(int(message_id))
        rows = await asyncio.to_thread(db.get_messages, sid, after_id=message_id - 1, limit=1)
        if not rows or rows[0]["id"] != message_id:
            return web.json_response({"error": "Message not found."}, status=404)
        row = rows[0]
        detail = await asyncio.to_thread(observations.read, sid, message_id)
        return web.json_response(
            {
                "message_id": message_id,
                "timestamp": row.get("timestamp"),
                "finish_reason": row.get("finish_reason"),
                "has_reasoning": bool(row.get("reasoning") or row.get("reasoning_content")),
                **(detail or {}),
            }
        )
    detail = await asyncio.to_thread(observations.read, sid)
    if detail:
        rows = await asyncio.to_thread(
            db.get_messages, sid, after_id=detail["message_id"] - 1, limit=1
        )
        if not rows or rows[0]["id"] != detail["message_id"]:
            detail = None  # A rewind invalidates the removed turn's context observation.
    usage = detail.get("usage") if detail else None
    return web.json_response(
        {
            "context": {
                "used": usage.get("input_tokens") if isinstance(usage, dict) else None,
                "maximum": detail.get("context_max"),
                "model": detail.get("model"),
                "observed_at": detail.get("observed_at"),
            }
            if detail
            else None
        }
    )


async def dispatch_action(request, adapter, commands=None):
    from aiohttp import web

    action = request.match_info.get("action", "capabilities")
    if action == "clarification":
        from .live import answer

        return await answer(request, adapter)
    if action == "runs":
        from .context import start_run, supports_context_runs

        if not supports_context_runs(adapter):
            return web.json_response({"error": "Profile context unavailable."}, status=404)
        return await start_run(adapter, request)
    if action == "models":
        from .models import model_options

        return web.json_response(
            await asyncio.to_thread(model_options, request.query.get("refresh") == "1")
        )
    if action == "activity":
        from .activity import dispatch as activity_dispatch

        return await activity_dispatch(request, adapter)
    db = await adapter._ensure_session_db_async()
    if db is None:
        return web.json_response({"error": "Hermes session storage is unavailable."}, status=503)
    if action in {"search", "around"}:
        from .search import dispatch as search_dispatch

        return await search_dispatch(request, adapter, db)
    if action == "commands" and commands is not None:
        return await commands.dispatch(request, adapter, db)
    if action == "capabilities":
        return await capabilities(adapter, db)
    return await dispatch_session(request, adapter, db, action)


async def dispatch_session(request, adapter, db, action):
    from aiohttp import web

    sid = request.match_info["session_id"]
    session = await asyncio.to_thread(db.get_session, sid)
    if session is None:
        return web.json_response({"error": "Session not found."}, status=404)
    if action == "fork":
        from .forks import fork_session

        return await fork_session(adapter, request, db, sid)
    return await session_details(request, db, sid, action)
