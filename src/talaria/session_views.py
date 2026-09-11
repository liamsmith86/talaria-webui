"""Read-only, profile-scoped plugin search and activity transport."""

from starlette.responses import JSONResponse

from .extensions import PREFIX
from .hermes import APIError, identifier, object_result


def number(value, maximum):
    try:
        result = int(value)
        if not 0 <= result <= maximum:
            raise ValueError
        return result
    except ValueError as exc:
        raise APIError("Invalid search position.", 400) from exc


def valid_rows(action, rows):
    if (
        not isinstance(rows, list)
        or len(rows) > {"search": 20, "around": 11, "activity": 256}[action]
    ):
        return False
    fields = {
        "search": {"id": int, "session_id": str, "title": str, "snippet": str, "role": str},
        "around": {"id": int, "role": str},
        "activity": {"session_id": str, "status": str},
    }[action]
    return all(
        isinstance(row, dict) and all(type(row.get(key)) is kind for key, kind in fields.items())
        for row in rows
    )


async def view(request):
    action = request.url.path.rsplit("/", 1)[-1]
    params = {}
    path = f"{PREFIX}/{action}"
    if action == "search":
        query = request.query_params.get("q", "").strip()
        if not query or len(query) > 200:
            raise APIError("Enter a search of 1-200 characters.", 400)
        params = {"q": query, "offset": number(request.query_params.get("offset", "0"), 10000)}
    elif action == "around":
        sid = identifier(request.path_params["session_id"])
        mid = number(request.query_params.get("message_id", "0"), 2**63 - 1)
        if not mid:
            raise APIError("Choose a saved message.", 400)
        path = f"{PREFIX}/sessions/{sid}/around"
        params = {"message_id": mid}
    result = object_result(await request.app.state.hermes.request("GET", path, params=params))
    if not valid_rows(action, result.get("data")):
        raise APIError("Hermes returned an unreadable session view.")
    if action == "around" and (
        result.get("session_id") != sid or not any(row["id"] == mid for row in result["data"])
    ):
        raise APIError("Hermes could not locate the requested message.")
    return JSONResponse(result)
