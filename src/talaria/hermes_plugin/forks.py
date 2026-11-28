"""Keep branch creation isolated even when its later title write fails.

Native forks end the original as "branched", which is right for the API's own
sessions. A session owned by another surface (a CLI, messaging or scheduled
session) is copied instead: ending it would make that surface start over.
"""

import asyncio
import time
import uuid

# A branch ends only the API's own sessions, as native forks do.
API_SOURCES = {"api_server"}


class TitleTaken(ValueError):
    """Another session took the branch's title after it was checked."""


def branch(store, source, title, ends_parent):
    """Copy the session as the native fork does, but end only an API session, and only once the
    copy is complete. A failed copy is removed."""
    from gateway.session_ingress_media import native_inputs_root
    from persistence.conversation.messages import NewMessage, ReplaceMode
    from persistence.conversation.sessions import NewSession, SessionLineageKind

    sessions, messages = store.handle.sessions, store.handle.messages
    sid, child = source["id"], f"api_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    sessions.create(
        NewSession(
            session_id=child,
            source="api_server",
            model=source.get("model"),
            system_prompt=source.get("system_prompt"),
            parent_session_id=sid,
            lineage_kind=SessionLineageKind.BRANCH,
        )
    )
    try:
        rows = [NewMessage.from_live(row.as_wire_row()) for row in messages.read_transcript(sid)]
        messages.replace_transcript(child, rows, ReplaceMode.DESTRUCTIVE)
        try:
            sessions.set_title(child, title)
        except ValueError as exc:
            raise TitleTaken(str(exc)) from exc
        if ends_parent:
            sessions.end(sid, "branched")
    except Exception:
        sessions.delete(child, expected_ids=[child], media_root=str(native_inputs_root()))
        raise
    return sessions.get(child).as_session_row()


def title_refused(exc):
    from aiohttp import web

    return web.json_response({"error": {"code": "invalid_title", "message": str(exc)}}, status=400)


async def fork_session(adapter, request, store, source):
    from aiohttp import web

    if not callable(getattr(adapter, "_session_response", None)):
        return web.json_response({"error": "Branch metadata unavailable."}, status=404)
    data = await request.json()
    if not isinstance(data, dict) or data.get("id") or data.get("session_id"):
        raise ValueError("Invalid branch request")
    sid, ends_parent = source["id"], source.get("source") in API_SOURCES
    try:
        title = await asyncio.to_thread(store.branch_title, source, data.get("title"))
    except ValueError as exc:
        return title_refused(exc)
    if ends_parent and await asyncio.to_thread(store.pending_turn, sid):
        return web.json_response(
            {"error": "Wait for this session's pending turn to finish, then branch it."},
            status=409,
        )
    try:
        row = await asyncio.to_thread(branch, store, source, title, ends_parent)
    except TitleTaken as exc:
        return title_refused(exc)
    session = adapter._session_response(row)
    return web.json_response({"object": "hermes.session", "session": session}, status=201)
