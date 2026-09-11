"""Bounded views over Hermes's native index and message windows; no transcript store."""

import asyncio
import inspect

FIELDS = ("id", "session_id", "role", "snippet", "timestamp", "source")
PAGE_SIZE = 20


def supported(db, adapter):
    try:
        inspect.signature(db.search_messages).bind("query", limit=1, offset=0, fields=FIELDS)
        inspect.signature(db.get_messages_around).bind("session", 1, window=5)
        return callable(adapter._message_response)
    except (AttributeError, TypeError, ValueError):
        return False


def visible(row):
    return (
        (row.get("active") or row.get("compacted"))
        and row.get("display_kind") != "hidden"
        and row.get("role") in {"user", "assistant", "tool"}
    )


def search(db, query, offset):
    rows = db.search_messages(query, limit=PAGE_SIZE, offset=offset, fields=FIELDS)
    sessions = {}
    result = []
    for row in rows:
        sid = row["session_id"]
        if sid not in sessions:
            sessions[sid] = db.get_session(sid)
        session = sessions[sid]
        if not session or session.get("hidden"):
            continue
        # The native index excludes rewound rows. Recheck visibility after search:
        # a concurrent rewind must not return a now-hidden match or its snippet.
        window = db.get_messages_around(sid, row["id"], window=0)["window"]
        if window and visible(window[0]):
            result.append(
                {
                    **row,
                    "snippet": str(row.get("snippet") or "")[:1200],
                    "title": session.get("title") or "Untitled session",
                }
            )
    return {"data": result, "has_more": len(rows) == PAGE_SIZE, "next_offset": offset + len(rows)}


def around(db, adapter, sid, mid):
    session = db.get_session(sid)
    if not session or session.get("hidden"):
        return None
    rows = db.get_messages_around(sid, mid, window=5)["window"]
    rows = [row for row in rows if visible(row)]
    if not any(row["id"] == mid for row in rows):
        return None
    return {
        "session_id": sid,
        "title": session.get("title") or "Untitled session",
        "message_id": mid,
        "data": [adapter._message_response(row) for row in rows],
    }


async def dispatch(request, adapter, db):
    from aiohttp import web

    if not supported(db, adapter):
        return web.json_response({"error": "Update Hermes to search session history."}, status=404)
    try:
        if request.match_info["action"] == "search":
            query = request.query.get("q", "").strip()
            offset = int(request.query.get("offset", "0"))
            if not query or len(query) > 200 or not 0 <= offset <= 10000:
                raise ValueError
            result = await asyncio.to_thread(search, db, query, offset)
        else:
            mid = int(request.query.get("message_id", "0"))
            if not 0 < mid < 2**63:
                raise ValueError
            result = await asyncio.to_thread(
                around, db, adapter, request.match_info["session_id"], mid
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
