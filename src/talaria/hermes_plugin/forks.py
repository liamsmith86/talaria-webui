"""Keep native branch creation isolated even when its later title write fails."""

import asyncio


class BranchStore:
    def __init__(self, db, parent, *, owns_id):
        self.db, self.parent, self.owns_id = db, parent, owns_id
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
        # Include Hermes's native lineage marker in the original INSERT, so even
        # concurrent readers and failed copies never see a compression descendant.
        kwargs["model_config"] = {
            **(kwargs.get("model_config") or {}),
            "_branched_from": self.parent,
        }
        result = self.db.create_session(session_id, source, **kwargs)
        self.created = session_id
        return result

    def finish(self, succeeded):
        if succeeded:
            if self.end:
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
    try:
        title = data.get("title")
        if title is None:
            source = await asyncio.to_thread(db.get_session, sid)
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
    store = BranchStore(db, sid, owns_id=not (data.get("id") or data.get("session_id")))
    succeeded = False
    try:
        response = await handler(BranchAdapter(adapter, store), request)
        succeeded = response.status == 201
        return response
    finally:
        await asyncio.shield(asyncio.to_thread(store.finish, succeeded))
