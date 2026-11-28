"""Versioned routes through Hermes's native plugin seam; no patched core or second runtime."""

import asyncio
import hashlib
import json
import logging

from .observations import Observations
from .release import LOADED_REVISION, PLUGIN_VERSION
from .store import compatible, open_store, user_turn_parts

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
            and "talaria-webui" in allowed
            and "talaria-webui" not in (denied or [])
        )
    except Exception:
        return False


def message_identifier(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("Choose a saved message.")
    return value


def revision(ids, content):
    return hashlib.sha256(json.dumps([ids, content], separators=(",", ":")).encode()).hexdigest()


def user_turn(row):
    handoff, user = user_turn_parts(row)
    if handoff is None and user is None and row.get("display_kind") == "internal_notification":
        # Hermes 0.22.1+ stores each API turn's own message this way; its rewind accepts them.
        return user_turn_parts({**row, "display_kind": None})
    return handoff, user


def rewind_preview(store, sid, message_id):
    from agent.memory_manager import sanitize_context

    if (store.session(sid) or {}).get("source") != "api_server":
        raise ValueError("Only API sessions can be changed here.")
    if store.pending_turn(sid):
        raise RuntimeError("The session has a pending turn.")

    ids = store.active_ids(sid)
    if len(ids) > MAX_MESSAGES:
        raise ValueError("This session is too large to rewind here.")
    rows = store.active(sid, MAX_MESSAGES + 1)
    # Active-id CAS detects messages arriving during the read as well as during confirmation.
    if [row["id"] for row in rows] != ids:
        raise RuntimeError("The session changed. Reopen the action and try again.")
    index = next((i for i, row in enumerate(rows) if row["id"] == message_id), None)
    if index is None or rows[index].get("role") not in {"user", "assistant"}:
        raise ValueError("This message is no longer available to change.")
    target = next(
        (i for i in range(index, -1, -1) if user_turn(rows[i])[1] is not None),
        None,
    )
    if target is None:
        raise ValueError(
            "The original message is in older, compacted history and cannot be changed here."
        )
    handoff, user = user_turn(rows[target])
    content = user.get("content")
    if isinstance(content, str):
        content = sanitize_context(content).strip()
    preview = {
        "revision": revision(ids, content),
        "target_id": rows[target]["id"],
        "message_count": len(rows) - target,
        "turn_count": sum(user_turn(row)[1] is not None for row in rows[target:]),
        "user": {"id": rows[target]["id"], "role": "user", "content": content},
    }
    # Hermes's content check refuses those API-turn rows; the active IDs still pin the turn.
    expected = None if rows[target].get("display_kind") == "internal_notification" else content
    return preview, (rows, target, handoff, expected)


def rewind(store, sid, data):
    from hermes_constants import get_hermes_home

    preview, change = rewind_preview(store, sid, data["message_id"])
    if data.get("preview") is True:
        return preview
    if data.get("revision") != preview["revision"]:
        raise RuntimeError("The session changed. Reopen the action and try again.")
    removed = store.rewind(sid, *change)
    # A size measured after an earlier compaction no longer describes this transcript.
    Observations(get_hermes_home()).forget_compaction(sid)
    return {"ok": True, "removed": removed}


def wire(app, adapter, **kwargs):
    if app.get("talaria.webui.registered"):
        return
    if app.frozen or app.router.frozen or app.on_cleanup.frozen:
        log.warning(
            "Talaria API registration requires a gateway restart: the HTTP server is already "
            "running. Restart the gateway process after active work finishes to activate it."
        )
        return
    if not callable(getattr(adapter, "_check_auth", None)) or not compatible(adapter):
        log.warning("Talaria routes unavailable: incompatible Hermes API adapter")
        return

    from .commands import CommandJobs

    commands = CommandJobs()
    app.on_cleanup.append(commands.close)

    async def dispatch_request(request):
        return await dispatch(request, adapter, commands)

    # Keep the native `profile` parameter for Hermes's authentication and request scope.
    # Its earlier /p/{profile}/* catch-all shadows plugin routes in that namespace.
    for prefix in (PREFIX, f"{PREFIX}/profiles/{{profile}}", f"/p/{{profile}}{PREFIX}"):
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
    app["talaria.webui.registered"] = True


def register(ctx):
    from hermes_constants import get_hermes_home

    observations = Observations(get_hermes_home())
    ctx.register_platform_handler("api_server", wire)
    ctx.register_hook("pre_api_request", observations.before)
    ctx.register_hook("post_api_request", observations.after)
    ctx.register_hook("on_session_end", observations.end)
    ctx.register_hook("post_compaction", observations.compacted)
    ctx.register_hook("on_session_delete", observations.deleted)


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


async def capabilities(adapter, store):
    from aiohttp import web
    from hermes_constants import get_hermes_home

    from .activity import supported as activity_supported
    from .commands import available
    from .context import load_context, supports_context_runs
    from .identity import read_name
    from .live import supported as live_supported
    from .search import supported as search_supported

    name = await asyncio.to_thread(read_name, get_hermes_home())
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
            "rewind": store.rewindable(),
            # Branching a CLI, messaging or scheduled session leaves that session open.
            "external_session_copies": True,
            "history_search": search_supported(store, adapter),
            "session_activity": activity_supported(adapter),
            "agent": {"name": name},
        }
    )


def still_in_transcript(store, sid, detail):
    """A rewind removes the reply a context reading came from; compaction only renumbers it."""
    if store.message(sid, detail["message_id"]):
        return True
    uid = detail.get("uid")
    return bool(uid) and any(row.get("message_uid") == uid for row in store.latest(sid, 200))


def context_usage(store, sid, observations):
    detail = observations.read(sid)
    if detail and not still_in_transcript(store, sid, detail):
        detail = None
    compaction = observations.read_compaction(sid)
    if compaction and (compaction.get("observed_at") or 0) > (
        (detail or {}).get("observed_at") or 0
    ):
        return {
            "used": compaction.get("tokens"),
            "maximum": compaction.get("maximum") or (detail or {}).get("context_max"),
            "model": compaction.get("model") or (detail or {}).get("model"),
            "observed_at": compaction.get("observed_at"),
            "estimated": True,
        }
    if not detail:
        return None
    usage = detail.get("usage")
    return {
        "used": usage.get("input_tokens") if isinstance(usage, dict) else None,
        "maximum": detail.get("context_max"),
        "model": detail.get("model"),
        "observed_at": detail.get("observed_at"),
    }


async def session_details(request, store, sid, action):
    from aiohttp import web
    from hermes_constants import get_hermes_home

    observations = Observations(get_hermes_home())
    if action == "rewind":
        if not store.rewindable():
            return web.json_response(
                {"error": "Update Hermes to enable session changes."}, status=501
            )
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("Choose a saved message.")
        message_identifier(data.get("message_id"))
        return web.json_response(await asyncio.to_thread(rewind, store, sid, data))
    message_id = request.query.get("message_id")
    if action == "response":
        message_id = message_identifier(int(message_id))
        row = await asyncio.to_thread(store.message, sid, message_id)
        if row is None:
            return web.json_response({"error": "Message not found."}, status=404)
        detail = await asyncio.to_thread(observations.read, sid, message_id, row.get("message_uid"))
        return web.json_response(
            {
                "timestamp": row.get("timestamp"),
                "finish_reason": row.get("finish_reason"),
                # Hermes can save a blank value when a model's API requires the field.
                "has_reasoning": any(
                    isinstance(value, str) and value.strip()
                    for value in (row.get("reasoning"), row.get("reasoning_content"))
                ),
                **(detail or {}),
                "message_id": message_id,
            }
        )
    return web.json_response(
        {"context": await asyncio.to_thread(context_usage, store, sid, observations)}
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
    store = await open_store(adapter)
    if store is None:
        return web.json_response({"error": "Hermes session storage is unavailable."}, status=503)
    if action in {"search", "around"}:
        from .search import dispatch as search_dispatch

        return await search_dispatch(request, adapter, store)
    if action == "commands" and commands is not None:
        return await commands.dispatch(request, adapter, store)
    if action == "capabilities":
        return await capabilities(adapter, store)
    return await dispatch_session(request, adapter, store, action)


async def dispatch_session(request, adapter, store, action):
    from aiohttp import web

    sid = request.match_info["session_id"]
    session = await asyncio.to_thread(store.session, sid)
    if session is None:
        return web.json_response({"error": "Session not found."}, status=404)
    if action == "fork":
        from .forks import fork_session

        return await fork_session(adapter, request, store, session)
    return await session_details(request, store, sid, action)
