"""Run with Hermes's own Python, against temporary storage only; never sends a model request."""

import asyncio
import json
import sys
import time
from pathlib import Path

from agent.api_request_hooks import ApiRequestHooksMixin
from agent.context_compressor import _SUMMARY_END_MARKER, HISTORICAL_TASK_HEADING, SUMMARY_PREFIX
from hermes_state import SessionDB

from talaria.hermes_plugin.bridge import rewind, rewind_preview, wire
from talaria.hermes_plugin.observations import Observations

home = Path(sys.argv[1])
assert home.is_dir() and str(home).startswith("/tmp/")
db = SessionDB(home / "state.db")
db.create_session("contract", "api_server")
first = db.append_message("contract", "user", "Keep this")
db.append_message("contract", "assistant", "Keep reply")
target = db.append_message("contract", "user", "Retry this")
db.append_message(
    "contract",
    "assistant",
    "",
    tool_calls=[
        {"id": "c", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
    ],
)
db.append_message("contract", "tool", "File output", tool_call_id="c")
reply = db.append_message("contract", "assistant", "Retry reply", finish_reason="stop")
preview, original_ids, _ = rewind_preview(db, "contract", reply)
assert preview["target_id"] == target and preview["message_count"] == 4
assert db.try_acquire_session_turn_lease("contract", "test-active", ttl_seconds=30)
try:
    rewind(db, "contract", {"message_id": reply, "revision": preview["revision"]})
    raise AssertionError("Active turn was not protected")
except RuntimeError:
    pass
finally:
    db.release_session_turn_lease("contract", "test-active")
assert db.get_active_message_ids("contract") == original_ids
db.append_message("contract", "user", "Newer turn")
try:
    rewind(db, "contract", {"message_id": reply, "revision": preview["revision"]})
    raise AssertionError("Stale confirmation was not rejected")
except RuntimeError:
    pass
preview, _, _ = rewind_preview(db, "contract", reply)
assert preview["turn_count"] == 2
result = rewind(db, "contract", {"message_id": reply, "revision": preview["revision"]})
assert result["removed"] == 5
assert [r["content"] for r in db.get_messages("contract")] == ["Keep this", "Keep reply"]
archived = db.get_messages("contract", include_inactive=True)
assert len(archived) == 7 and any(row["tool_calls"] for row in archived)

# A compaction carrier may contain both a retained summary and the live user ask.
# Hermes must retain its scaffold when that ask is removed.

db.create_session("compacted", "api_server")
carrier = (
    f"{SUMMARY_PREFIX}\n{HISTORICAL_TASK_HEADING}\nold task\n\n{_SUMMARY_END_MARKER}\n\nREAL ASK"
)
carrier_id = db.append_message("compacted", "user", carrier)
db.append_message("compacted", "assistant", "Compacted reply")
preview, _, composite = rewind_preview(db, "compacted", carrier_id)
assert composite and preview["user"]["content"] == "REAL ASK"
rewind(db, "compacted", {"message_id": carrier_id, "revision": preview["revision"]})
scaffold = db.get_messages("compacted")
assert len(scaffold) == 1 and scaffold[0]["display_kind"] == "hidden"
assert "REAL ASK" not in scaffold[0]["content"] and "old task" in scaffold[0]["content"]

# Other transports may own cached histories. Their editing stays with that transport.
db.create_session("terminal", "cli")
terminal_message = db.append_message("terminal", "user", "Owned by a terminal")
try:
    rewind_preview(db, "terminal", terminal_message)
    raise AssertionError("A different transport's history was editable")
except ValueError:
    pass

observations = Observations(home)
started = time.time()
observations.before(
    task_id="contract",
    turn_id="t",
    request=ApiRequestHooksMixin()._api_request_payload_for_hook(
        {"reasoning_effort": "high", "api_key": "never-save"}
    ),
    started_at=started,
)
observations.after(
    task_id="contract",
    turn_id="t",
    model="model",
    response_model="actual-model",
    provider="test",
    usage={"prompt_tokens": 123, "output_tokens": 7},
    api_duration=1.5,
    ended_at=started + 1.5,
)
saved_reply = db.append_message("contract", "assistant", "Recorded reply", timestamp=started + 2)
observations.end(task_id="contract", turn_id="t", session_id="contract", completed=True)
observed = observations.read("contract", saved_reply)
assert observed["model"] == "actual-model" and observed["reasoning"] == "high"
assert "never-save" not in json.dumps(observed)


async def http_contract():

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from gateway.platforms.api_server import APIServerAdapter

    (home / "config.yaml").write_text("plugins:\n  enabled: [talaria]\n")
    (home / "SOUL.md").write_text("Name: Contract agent\nPrivate instructions")

    class Adapter:
        _check_auth = APIServerAdapter._check_auth
        _auth_failed_response = staticmethod(APIServerAdapter._auth_failed_response)
        _api_key = "contract-test-secret"

        def _expected_api_key(self):
            return self._api_key

        def _request_audit_log_suffix(self, request):
            return "test"

        async def _ensure_session_db_async(self):
            return db

    app = web.Application()
    wire(app, Adapter())
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/talaria/v1/capabilities")).status == 401
        headers = {"Authorization": "Bearer contract-test-secret"}
        caps = await (await client.get("/talaria/v1/capabilities", headers=headers)).json()
        assert caps["rewind"] is True and caps["agent"]["name"] == "Contract agent"
        assert "Private instructions" not in json.dumps(caps)
        for path in ("context", f"response?message_id={saved_reply}"):
            assert (
                await client.get(f"/talaria/v1/sessions/contract/{path}", headers=headers)
            ).status == 200
        response = await client.get(
            f"/talaria/v1/sessions/contract/response?message_id={reply}", headers=headers
        )
        assert (
            response.status == 404
        )  # Removed replies do not receive metadata for a different row.
        assert (
            await client.get("/talaria/v1/sessions/missing/context", headers=headers)
        ).status == 404


asyncio.run(http_contract())
db.close()
print("Native Hermes contracts passed")
