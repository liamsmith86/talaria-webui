"""Exercise Talaria-only message branching with native Hermes and temporary storage."""

import asyncio
import json
import sys
from pathlib import Path

from agent.context_compressor import _SUMMARY_END_MARKER, HISTORICAL_TASK_HEADING, SUMMARY_PREFIX
from aiohttp import web
from aiohttp.test_utils import TestServer
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB

from talaria.branching import branch_from_response, mark_branch_points
from talaria.hermes import APIError, Hermes
from talaria.hermes_plugin.bridge import wire

home = Path(sys.argv[1])
assert home.is_dir() and str(home).startswith("/tmp/")
(home / "config.yaml").write_text(json.dumps({"plugins": {"enabled": ["talaria-webui"]}}))


async def main():
    db = SessionDB(home / "state.db")
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "fixture-key"}))
    adapter._session_db = db
    app = web.Application()
    app.router.add_get("/api/sessions/{session_id}", adapter._handle_get_session)
    app.router.add_delete("/api/sessions/{session_id}", adapter._handle_delete_session)
    app.router.add_get("/api/sessions/{session_id}/messages", adapter._handle_session_messages)
    wire(app, adapter)
    async with TestServer(app) as server:
        client = Hermes(str(server.make_url("/")), "fixture-key")
        try:
            db.create_session("original", "api_server")
            db.append_message("original", "user", "Synthetic question")
            db.append_message(
                "original",
                "assistant",
                "Checking",
                tool_calls=[
                    {
                        "id": "call",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    },
                ],
            )
            db.append_message("original", "tool", "Synthetic result", tool_call_id="call")
            selected = db.append_message("original", "assistant", "Synthetic first answer")
            db.append_message("original", "user", "Synthetic later question")
            latest = db.append_message("original", "assistant", "Synthetic later answer")
            original = db.get_messages("original")
            child = await branch_from_response(client, "original", selected, {})
            assert child["message_count"] == 4
            assert [m["content"] for m in db.get_messages(child["id"])] == [
                m["content"] for m in original[:4]
            ]
            assert db.get_messages(child["id"])[2]["tool_call_id"] == "call"
            assert db.get_messages("original") == original
            assert db.resolve_resume_session_id("original") == "original"
            latest_child = await branch_from_response(client, "original", latest, {})
            assert latest_child["message_count"] == 6

            # Compacted display history is readable, but cannot be a branch point.
            db.archive_and_compact("original", [{"role": "user", "content": "Synthetic summary"}])
            db.append_message("original", "user", "New synthetic ask")
            fresh = db.append_message("original", "assistant", "New synthetic answer")
            displayed = (
                await client.request(
                    "GET", "/api/sessions/original/messages", params={"include_compacted": "true"}
                )
            )["data"]
            marked = await mark_branch_points(client, "original", displayed)
            assert selected not in {row["id"] for row in marked if row["branchable"]}
            assert fresh in {row["id"] for row in marked if row["branchable"]}
            try:
                await branch_from_response(client, "original", selected, {})
                raise AssertionError("Archived response was branchable")
            except APIError as exc:
                assert exc.status == 409

            # A retained handoff for a LATER turn must never leak into an earlier branch.
            db.create_session("handoff", "api_server")
            db.append_message("handoff", "user", "Old ask")
            old = db.append_message("handoff", "assistant", "Old answer")
            carrier = (
                f"{SUMMARY_PREFIX}\n{HISTORICAL_TASK_HEADING}\nfuture context\n\n"
                f"{_SUMMARY_END_MARKER}\n\nNEW ASK"
            )
            db.append_message("handoff", "user", carrier)
            db.append_message("handoff", "assistant", "New answer")
            count = db._read_one("SELECT count(*) AS n FROM sessions")["n"]
            retained = db.get_messages("handoff")
            try:
                await branch_from_response(client, "handoff", old, {})
                raise AssertionError("Future compaction context survived the cut")
            except APIError:
                pass
            assert db._read_one("SELECT count(*) AS n FROM sessions")["n"] == count
            assert db.get_messages("handoff") == retained
        finally:
            await client.close()
    await adapter.disconnect()
    db.close()


asyncio.run(main())
