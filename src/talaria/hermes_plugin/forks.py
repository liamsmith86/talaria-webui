"""Keep native branch creation isolated even when its later title write fails.

Native forks end the original as "branched", which is right for the API's own
sessions. A session owned by another surface (a CLI, messaging or scheduled
session) is copied instead: ending it would make that surface start over.
"""

import asyncio

# A branch ends only the API's own sessions, as native forks do.
API_SOURCES = {"api_server"}


class BranchStore:
    def __init__(self, db, parent, *, owns_id, ends_parent=True):
        self.db, self.parent, self.owns_id = db, parent, owns_id
        self.ends_parent = ends_parent
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
        elif self.created and self.owns_id:
            # Never remove a caller-selected ID: native creation is an upsert.
            # A server-generated child belongs exclusively to this failed action.
            self.db.delete_session(self.created, expected_delete_ids=[self.created])


class BranchAdapter:
    def __init__(self, adapter, store):
        self.adapter, self.store = adapter, store

    def __getattr__(self, name):
        return getattr(self.adapter, name)

    async def _ensure_session_db_async(self):
        return self.store


async def fork_session(adapter, request, db, sid):
    from aiohttp import web

    handler = getattr(adapter._handle_fork_session, "__func__", None)
    if not callable(handler):
        return web.json_response({"error": "Branch metadata unavailable."}, status=404)
    data = await request.json()
    if not isinstance(data, dict):
        raise ValueError("Invalid branch request")
    source = await asyncio.to_thread(db.get_session, sid) or {}  # Native answers a missing ID.
    try:
        title = data.get("title")
        if title is None:
            title = await asyncio.to_thread(
                db.get_next_title_in_lineage, source.get("title") or "fork"
            )
        title = db.sanitize_title(str(title))
        if title and await asyncio.to_thread(db.get_session_by_title, title):
            raise ValueError("That session name is already in use.")
    except ValueError as exc:
        return web.json_response(
            {"error": {"code": "invalid_title", "message": str(exc)}}, status=400
        )
    store = BranchStore(
        db,
        sid,
        owns_id=not (data.get("id") or data.get("session_id")),
        ends_parent=source.get("source") in API_SOURCES,
    )
    succeeded = False
    try:
        response = await handler(BranchAdapter(adapter, store), request)
        succeeded = response.status == 201
        return response
    finally:
        await asyncio.shield(asyncio.to_thread(store.finish, succeeded))
