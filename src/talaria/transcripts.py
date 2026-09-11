"""Canonical message pagination and disposable, complete transcript downloads."""

import asyncio
import json
import re
from datetime import UTC, datetime
from tempfile import SpooledTemporaryFile
from urllib.parse import quote

from starlette.responses import StreamingResponse

from .content import content_text
from .hermes import APIError, identifier

MAX_EXPORT = 128 * 1024 * 1024


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
    result = await client.request("GET", f"/api/sessions/{sid}")
    if not isinstance(result, dict):
        raise APIError("Hermes did not return readable session details.")
    session = result.get("session", result)
    if not isinstance(session, dict):
        raise APIError("Hermes did not return readable session details.")
    title = str(session.get("title") or "Session")
    # Finish fetching before sending headers: a failed page must never look like a complete export.
    spool = SpooledTemporaryFile(max_size=2 * 1024 * 1024, mode="w+b")

    def write(text):
        data = text.encode("utf-8")
        if spool.tell() + len(data) > MAX_EXPORT:
            raise APIError("This transcript exceeds the 128 MB download limit.", 413, "too_large")
        spool.write(data)

    try:
        async with asyncio.timeout(180):
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
            offset, first = 0, True
            while True:
                if await request.is_disconnected():
                    raise asyncio.CancelledError
                page = await message_page(client, sid, offset, order="oldest")
                canonical = page.get("session_id") or sid
                if canonical != sid and offset:
                    raise APIError(
                        "The session changed during download. Please try again.", 409
                    )
                sid = identifier(canonical)
                for message in page["data"]:
                    if format_ == "json":
                        write(
                            ("" if first else ",\n")
                            + json.dumps(message, ensure_ascii=False, indent=2)
                        )
                    else:
                        write(markdown_message(message))
                    first = False
                    # Formatting a large page must not monopolize the server
                    # while other tabs are submitting messages or streaming.
                    await asyncio.sleep(0)
                offset = page["next_offset"]
                if not page["has_more"]:
                    break
            if format_ == "json":
                write("\n  ]\n}\n")
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
