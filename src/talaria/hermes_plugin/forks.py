"""Keep branch creation isolated even when its later title write fails.

Native forks end the original as "branched", which is right for the API's own
sessions. A session owned by another surface (a CLI, messaging or scheduled
session) is copied instead: ending it would make that surface start over.
"""

import asyncio
import time
import uuid

from .store import Legacy

# A branch ends only the API's own sessions, as native forks do.
API_SOURCES = {"api_server"}


class BranchStore:
    """Before 0.23.3 the native handler branches through this wrapper around its ``SessionDB``."""

    def __init__(self, db, parent, *, ends_parent):
        self.db, self.parent, self.ends_parent = db, parent, ends_parent
        self.created = None
        self.end = None

    def __getattr__(self, name):
        return getattr(self.db, name)

    def end_session(self, session_id, reason):
        if session_id == self.parent and reason == "branched":
            self.end = (session_id, reason)
        else:
            self.db.end_session(session_id, reason)

    def create_session(self, session_id, source, **kwargs):
        if kwargs.get("parent_session_id") != self.parent or session_id == self.parent:
            raise ValueError("Unexpected branch identity")
        result = self.db.create_session(session_id, source, **kwargs)
        self.created = session_id
        return result

    def finish(self, succeeded):
        if succeeded:
            if self.end and self.ends_parent:
                self.db.end_session(*self.end)
        elif self.created:
            # The handler generated this ID, so the child belongs to this failed action alone.
            self.db.delete_session(self.created, expected_delete_ids=[self.created])


class BranchAdapter:
    def __init__(self, adapter, store):
        self.adapter, self.store = adapter, store

    def __getattr__(self, name):
        return getattr(self.adapter, name)

    async def _ensure_session_db_async(self):
        return self.store


def branch(store, source, title, ends_parent):
    """Hermes 0.23.3+: copy the session as the native fork does, but end only an API session, and
    only once the copy is complete. A failed copy is removed."""
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
        sessions.set_title(child, title)
        if ends_parent:
            sessions.end(sid, "branched")
    except Exception:
        sessions.delete(child, expected_ids=[child], media_root=str(native_inputs_root()))
        raise
    return sessions.get(child).as_session_row()


async def native_branch(handler, adapter, request, store, sid, ends_parent):
    """Before 0.23.3: run the native handler on a store that defers ending the parent."""
    branch_store = BranchStore(store.db, sid, ends_parent=ends_parent)
    succeeded = False
    try:
        response = await handler(BranchAdapter(adapter, branch_store), request)
        succeeded = response.status == 201
        return response
    finally:
        await asyncio.shield(asyncio.to_thread(branch_store.finish, succeeded))


async def fork_session(adapter, request, store, source):
    from aiohttp import web

    legacy = isinstance(store, Legacy)
    handler = getattr(getattr(adapter, "_handle_fork_session", None), "__func__", None)
    if not callable(handler if legacy else getattr(adapter, "_session_response", None)):
        return web.json_response({"error": "Branch metadata unavailable."}, status=404)
    data = await request.json()
    if not isinstance(data, dict) or data.get("id") or data.get("session_id"):
        raise ValueError("Invalid branch request")
    sid, ends_parent = source["id"], source.get("source") in API_SOURCES
    try:
        # Before 0.23.3 this only checks the title: the native handler names the branch itself.
        title = await asyncio.to_thread(store.branch_title, source, data.get("title"))
        if ends_parent and await asyncio.to_thread(store.pending_turn, sid):
            return web.json_response(
                {"error": "Wait for this session's pending turn to finish, then branch it."},
                status=409,
            )
        if legacy:
            return await native_branch(handler, adapter, request, store, sid, ends_parent)
        row = await asyncio.to_thread(branch, store, source, title, ends_parent)
    except ValueError as exc:
        return web.json_response(
            {"error": {"code": "invalid_title", "message": str(exc)}}, status=400
        )
    session = adapter._session_response(row)
    return web.json_response({"object": "hermes.session", "session": session}, status=201)
