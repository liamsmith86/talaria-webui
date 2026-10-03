"""Branch completed responses through the installed plugin's fork and rewind APIs."""

import asyncio
import contextlib
import hashlib
import json

from .extensions import PREFIX
from .hermes import APIError, identifier, object_result
from .transcripts import message_page

MAX_MESSAGES = 20_000  # The installed plugin's rewind limit.
UNAVAILABLE = "This response is no longer available as a branch point. Refresh the conversation."
CHANGED = "The conversation changed. Refresh it before creating a branch."


async def active_messages(client, sid, *, earliest=None):
    """Read native active history; keep bounded identities instead of message bodies."""
    messages, offset = [], 0
    while True:
        page = await message_page(client, sid, offset, limit=500, include_compacted=False)
        if page.get("session_id", sid) != sid:
            raise APIError(CHANGED, 409)
        chunk = []
        for row in page["data"]:
            mid = row.get("id")
            if type(mid) is not int or not 0 < mid < 2**63:
                raise APIError(UNAVAILABLE, 409)
            content = {
                key: row.get(key)
                for key in (
                    "role",
                    "content",
                    "tool_calls",
                    "tool_call_id",
                    "tool_name",
                    "reasoning",
                    "reasoning_content",
                    "display_kind",
                    "finish_reason",
                )
            }
            chunk.append(
                {
                    "id": mid,
                    "role": row.get("role"),
                    "owned": row.get("session_id", sid) == sid,
                    "tools": bool(row.get("tool_calls")),
                    "hidden": row.get("display_kind") == "hidden",
                    "digest": hashlib.sha256(json.dumps(content, sort_keys=True).encode()).digest(),
                }
            )
        messages = chunk + messages
        if len(messages) > MAX_MESSAGES:
            raise APIError("This conversation is too large to branch here.", 409)
        if not page["has_more"] or (
            earliest is not None
            and any(row["id"] <= earliest and row["role"] == "user" for row in chunk)
        ):
            break
        offset = page["next_offset"]
    if len({row["id"] for row in messages}) != len(messages):
        raise APIError(CHANGED, 409)
    return messages


def branch_points(messages):
    if any(not row["owned"] for row in messages):
        return set()  # Native fork cannot copy a different session's active ancestors.
    points, user = set(), False
    for index, row in enumerate(messages):
        if row["role"] == "user":
            user = not row["hidden"]
            if row["hidden"]:
                points.clear()  # A later compression handoff can contain future context.
        elif (
            user
            and row["role"] == "assistant"
            and not row["tools"]
            and not row["hidden"]
            and (index + 1 == len(messages) or messages[index + 1]["role"] == "user")
        ):
            points.add(row["id"])
    return points


async def mark_branch_points(client, sid, rows):
    candidates = [
        row["id"] for row in rows if row.get("role") == "assistant" and type(row.get("id")) is int
    ]
    points = set()
    if candidates:
        # Optional controls must never make the transcript unavailable.
        with contextlib.suppress(APIError):
            points = branch_points(await active_messages(client, sid, earliest=min(candidates)))
    return [{**row, "branchable": row.get("id") in points} for row in rows]


async def preview(client, sid, mid):
    return object_result(
        await client.request(
            "POST",
            f"{PREFIX}/sessions/{sid}/rewind",
            json={"message_id": mid, "preview": True},
        )
    )


def digests(messages):
    return [row["digest"] for row in messages]


async def branch_from_response(client, sid, mid, payload):
    original = await active_messages(client, sid)
    if mid not in branch_points(original):
        raise APIError(UNAVAILABLE, 409)
    # Preview validates that the original turn is still in editable API history.
    before = await preview(client, sid, mid)
    index = next(i for i, row in enumerate(original) if row["id"] == mid)
    result = object_result(
        await client.request(
            "POST",
            f"{PREFIX}/sessions/{sid}/fork",
            json=payload,
        )
    )
    child = object_result(result.get("session", result))
    child_id = identifier(child.get("id"))
    if child_id == sid or child.get("parent_session_id") != sid:
        raise APIError("Hermes did not return an independent branch.", 502)
    copied = []
    try:
        copied = await active_messages(client, child_id)
        if digests(copied) != digests(original) or await preview(client, sid, mid) != before:
            raise APIError(CHANGED, 409)
        if index + 1 < len(copied):
            target = copied[index + 1]["id"]
            check = await preview(client, child_id, target)
            if (
                check.get("target_id") != target
                or check.get("message_count") != len(copied) - index - 1
            ):
                raise APIError(UNAVAILABLE, 409)
            await client.request(
                "POST",
                f"{PREFIX}/sessions/{child_id}/rewind",
                json={"message_id": target, "revision": check["revision"]},
            )
        if digests(await active_messages(client, child_id)) != digests(original[: index + 1]):
            raise APIError(UNAVAILABLE, 409)
        current = object_result(await client.request("GET", f"/api/sessions/{child_id}"))
        return object_result(current.get("session", current))
    except BaseException:
        try:
            remaining = await asyncio.shield(active_messages(client, child_id))
            expected = copied[: index + 1]
            # Rewind may retain a hidden compression handoff. Do not delete visible
            # work that another client added to the otherwise disposable copy.
            handoff = (
                len(remaining) == len(expected) + 1
                and remaining[-1]["hidden"]
                and remaining[-1]["role"] == "user"
                and remaining[:-1] == expected
            )
            if digests(copied) != digests(original) or (
                remaining not in (copied, expected) and not handoff
            ):
                raise APIError(CHANGED, 409)
            await asyncio.shield(client.request("DELETE", f"/api/sessions/{child_id}"))
        except APIError as exc:
            raise APIError(
                "Branching failed and the unfinished copy could not be removed. "
                "It remains in your session list.",
                502,
            ) from exc
        raise
