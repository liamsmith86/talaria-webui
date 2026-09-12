"""Canonical message pagination and disposable, complete transcript downloads."""

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from tempfile import SpooledTemporaryFile
from urllib.parse import quote

from starlette.responses import StreamingResponse

from .content import content_text
from .hermes import APIError, identifier

MAX_EXPORT = 128 * 1024 * 1024
HISTORY_CHANGED = "The session changed during download. Please try again."


class TranscriptResponse(StreamingResponse):
    def __init__(self, spool, **kwargs):
        self.spool = spool
        super().__init__(self.chunks(), **kwargs)

    async def chunks(self):
        while chunk := await asyncio.to_thread(self.spool.read, 65536):
            yield chunk

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # A disconnect can happen while sending headers, before chunks starts,
            # or while its generator is suspended at yield.
            self.spool.close()


async def message_page(client, sid, offset=0, *, order="latest", limit=100):
    while True:
        try:
            result = await client.request(
                "GET",
                f"/api/sessions/{sid}/messages",
                params={"offset": offset, "limit": limit, "order": order},
            )
            break
        except APIError as exc:
            if exc.code != "response_too_large" or limit <= 1:
                raise
            limit = max(1, limit // 2)
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise APIError("Hermes did not return readable session history.")
    data = result["data"]
    if any(not isinstance(row, dict) for row in data):
        raise APIError("Hermes did not return readable session history.")
    if "session_id" in result:
        try:
            identifier(result["session_id"])
        except APIError as exc:
            raise APIError("Hermes did not return readable session history.") from exc
    return {
        **result,
        "limit": limit,
        "has_more": len(data) >= limit,
        "next_offset": offset + len(data),
    }


def markdown_message(message):
    role = message.get("role", "message")
    role = role if isinstance(role, str) else "message"
    label = {"user": "You", "assistant": "Hermes", "tool": "Tool", "system": "System"}.get(
        role, str(role).capitalize()
    )
    sections = [f"## {label}", content_text(message.get("content"))]
    reasoning = message.get("reasoning") or message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        sections.append("### Reasoning\n\n" + reasoning)
    tools = message.get("tool_calls")
    for tool in tools if isinstance(tools, list) else []:
        sections.append("### Tool call\n\n" + json.dumps(tool, ensure_ascii=False, indent=2))
    return "\n\n".join(p for p in sections if p) + "\n\n"


async def download(request):
    sid = identifier(request.path_params["session_id"])
    format_ = request.query_params.get("format", "markdown")
    if format_ not in {"markdown", "json"}:
        raise APIError("Choose Markdown or JSON.", 400)
    client = request.app.state.hermes
    # Finish fetching before sending headers: a failed page must never look like a complete export.
    # Ownership transfers to TranscriptResponse; its ASGI finally closes the spool.
    spool = SpooledTemporaryFile(max_size=2 * 1024 * 1024, mode="w+b")  # noqa: SIM115

    def write(text):
        data = text.encode("utf-8")
        if spool.tell() + len(data) > MAX_EXPORT:
            raise APIError("This transcript exceeds the 128 MB download limit.", 413, "too_large")
        spool.write(data)

    try:
        async with asyncio.timeout(180):
            title = await write_transcript(request, client, sid, format_, write)
    except BaseException as exc:
        spool.close()
        if isinstance(exc, TimeoutError):
            raise APIError("The download took too long. Please try again.", 504) from exc
        raise
    size = spool.tell()
    spool.seek(0)

    extension = "json" if format_ == "json" else "md"
    filename = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", title).strip(" .")[:100] or "Session"
    return TranscriptResponse(
        spool,
        media_type="application/json" if format_ == "json" else "text/markdown",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}.{extension}",
            "Content-Length": str(size),
        },
    )


async def session_details(client, sid):
    result = await client.request("GET", f"/api/sessions/{sid}")
    if not isinstance(result, dict):
        raise APIError("Hermes did not return readable session details.")
    session = result.get("session", result)
    if not isinstance(session, dict):
        raise APIError("Hermes did not return readable session details.")
    if session.get("id", sid) != sid:
        raise APIError(HISTORY_CHANGED, 409)
    return session


def page_signature(page):
    # Retain bounded comparison facts, not another copy of the transcript.
    digest = hashlib.sha256()
    for message in page["data"]:
        digest.update(json.dumps(message, ensure_ascii=False, sort_keys=True).encode())
        digest.update(b"\n")
    return digest.digest()


async def same_session_page(client, sid, **kwargs):
    page = await message_page(client, sid, **kwargs)
    if page.get("session_id", sid) != sid:
        raise APIError(HISTORY_CHANGED, 409)
    return page


async def verify_history(client, sid, session, boundaries):
    # Native Hermes exposes no snapshot token. Recheck the head, tail, and
    # available counts/activity to reject detectable rewinds or appends.
    # This cannot prove that arbitrary edits within unchanged boundaries did
    # not occur, or make these separate HTTP reads an atomic snapshot.
    for order, limit, signature in boundaries:
        page = await same_session_page(client, sid, order=order, limit=limit)
        if page_signature(page) != signature:
            raise APIError(HISTORY_CHANGED, 409)
    current = await session_details(client, sid)
    if any(session.get(key) != current.get(key) for key in ("message_count", "last_active")):
        raise APIError(HISTORY_CHANGED, 409)


async def write_transcript(request, client, sid, format_, write):
    page = await message_page(client, sid, order="oldest")
    sid = identifier(page.get("session_id") or sid)
    session = await session_details(client, sid)
    title = str(session.get("title") or "Session")
    boundaries = [("oldest", page["limit"], page_signature(page))]
    if page["has_more"]:
        tail = await same_session_page(client, sid, order="latest", limit=1)
        boundaries.append(("latest", tail["limit"], page_signature(tail)))
        del tail
    stamp = datetime.now(UTC).isoformat()
    if format_ == "json":
        meta = {
            "format": "talaria-transcript",
            "version": 1,
            "exported_at": stamp,
            "session": session,
        }
        write(json.dumps(meta, ensure_ascii=False, indent=2)[:-2] + ',\n  "messages": [\n')
    else:
        write(f"# {title.replace(chr(10), ' ')}\n\nExported {stamp}\n\n")
    first = True
    while True:
        if await request.is_disconnected():
            raise asyncio.CancelledError
        for message in page["data"]:
            if format_ == "json":
                write(("" if first else ",\n") + json.dumps(message, ensure_ascii=False, indent=2))
            else:
                write(markdown_message(message))
            first = False
            # Formatting a large page must not monopolize the server
            # while other tabs are submitting messages or streaming.
            await asyncio.sleep(0)
        if not page["has_more"]:
            break
        page = await same_session_page(client, sid, offset=page["next_offset"], order="oldest")
    await verify_history(client, sid, session, boundaries)
    if format_ == "json":
        write("\n  ]\n}\n")
    return title
