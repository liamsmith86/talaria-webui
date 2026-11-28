"""Bounded views over Hermes's native index and message windows; no transcript store."""

import asyncio

FIELDS = ("id", "session_id", "role", "snippet", "timestamp", "source")
PAGE_SIZE = 20


def supported(store, adapter):
    return store.searchable(FIELDS) and callable(getattr(adapter, "_message_response", None))


def visible(row):
    return (
        (row.get("active") or row.get("compacted"))
        and row.get("display_kind") != "hidden"
        and row.get("role") in {"user", "assistant", "tool"}
    )


def conversation_ids(store, sid, shown):
    """Row ids in the display of this session's conversation, read once per search."""
    key, read = store.conversation(sid)
    if key not in shown:
        shown[key] = {row["id"] for row in read()}
    return shown[key]


def compacted(row, session):
    """Only a compaction leaves copies, so an untouched session never pays for the display read."""
    return (
        row.get("compacted")
        or session.get("parent_session_id")
        or session.get("end_reason") == "compression"
    )


def search(store, query, offset):
    rows = store.search(query, PAGE_SIZE, offset, FIELDS)
    sessions, shown = {}, {}
    result = []
    for row in rows:
        sid = row["session_id"]
        if sid not in sessions:
            sessions[sid] = store.session(sid)
        session = sessions[sid]
        if not session or session.get("hidden"):
            continue
        # The native index excludes rewound rows. Recheck visibility after search:
        # a concurrent rewind must not return a now-hidden match or its snippet.
        stored = store.stored(sid, row["id"])
        if not stored or not visible(stored):
            continue
        if compacted(stored, session) and row["id"] not in conversation_ids(store, sid, shown):
            continue
        result.append(
            {
                **row,
                "snippet": str(row.get("snippet") or "")[:1200],
                "title": session.get("title") or "Untitled session",
            }
        )
    return {"data": result, "has_more": len(rows) == PAGE_SIZE, "next_offset": offset + len(rows)}


def around(store, adapter, sid, mid):
    session = store.session(sid)
    if not session or session.get("hidden"):
        return None
    # Row IDs do not follow display order across a compaction, so window the display itself.
    rows = [row for row in store.displayed(sid) if visible(row)]
    index = next((i for i, row in enumerate(rows) if row["id"] == mid), None)
    if index is None:
        return None
    rows = rows[max(0, index - 5) : index + 6]
    return {
        "session_id": sid,
        "title": session.get("title") or "Untitled session",
        "message_id": mid,
        "data": [adapter._message_response(row) for row in rows],
    }


async def dispatch(request, adapter, store):
    from aiohttp import web

    if not supported(store, adapter):
        return web.json_response({"error": "Update Hermes to search session history."}, status=404)
    try:
        if request.match_info["action"] == "search":
            query = request.query.get("q", "").strip()
            offset = int(request.query.get("offset", "0"))
            if not query or len(query) > 200 or not 0 <= offset <= 10000:
                raise ValueError
            result = await asyncio.to_thread(search, store, query, offset)
        else:
            mid = int(request.query.get("message_id", "0"))
            if not 0 < mid < 2**63:
                raise ValueError
            result = await asyncio.to_thread(
                around, store, adapter, request.match_info["session_id"], mid
            )
            if result is None:
                return web.json_response(
                    {"error": "This message is no longer available."}, status=404
                )
    except ValueError:
        return web.json_response(
            {"error": "Enter a search of 1-200 characters or a saved message."}, status=400
        )
    return web.json_response(result)
