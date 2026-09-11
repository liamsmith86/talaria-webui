"""Public search fixtures. Native FTS/visibility is checked separately against SessionDB."""

from starlette.responses import JSONResponse


def respond(peer, request):
    path = request.url.path
    if path == "/talaria/v1/activity":
        return JSONResponse({"data": [], "scope": "api_runs"})
    if path == "/talaria/v1/search":
        query = request.query_params.get("q", "").lower()
        offset = int(request.query_params.get("offset", "0"))
        rows = [
            {
                "id": row["id"],
                "session_id": sid,
                "title": peer.sessions[sid]["title"],
                "role": row["role"],
                "snippet": str(row.get("content", ""))[:120],
                "source": peer.sessions[sid]["source"],
            }
            for sid, messages in peer.messages.items()
            for row in messages
            if query in str(row.get("content", "")).lower()
        ]
        return JSONResponse(
            {
                "data": rows[offset : offset + 20],
                "has_more": offset + 20 < len(rows),
                "next_offset": offset + 20,
            }
        )
    if path.endswith("/around"):
        sid = path.split("/")[4]
        mid = int(request.query_params["message_id"])
        messages = peer.messages.get(sid, [])
        index = next((i for i, row in enumerate(messages) if row["id"] == mid), None)
        if index is None:
            return JSONResponse({"error": "This message is no longer available."}, 404)
        return JSONResponse(
            {
                "session_id": sid,
                "message_id": mid,
                "title": peer.sessions[sid]["title"],
                "data": messages[max(0, index - 5) : index + 6],
            }
        )
    return None
