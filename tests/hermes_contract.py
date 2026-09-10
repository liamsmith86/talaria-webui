"""Run with Hermes's own Python, against temporary storage only; never sends a model request."""

import asyncio
import json
import sys
import time
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from agent.api_request_hooks import ApiRequestHooksMixin
from agent.context_compressor import _SUMMARY_END_MARKER, HISTORICAL_TASK_HEADING, SUMMARY_PREFIX
from hermes_cli import inventory
from hermes_cli import models_reasoning_caps as native_caps
from hermes_state import SessionDB
from providers import get_provider_profile

from talaria.hermes_plugin.bridge import rewind, rewind_preview, wire
from talaria.hermes_plugin.models import model_options
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

# An interrupted final request may have no post hook. Keep its known settings,
# never the usage of the earlier tool round, and attach only to a new saved row.
interrupt_start = time.time() + 10
observations.before(task_id="contract", turn_id="interrupted", api_request_id="tool",
                    request={}, started_at=interrupt_start, model="tool-model", provider="test")
observations.after(task_id="contract", turn_id="interrupted", api_request_id="tool",
                   usage={"prompt_tokens": 900, "output_tokens": 100}, api_duration=2)
observations.before(task_id="contract", turn_id="interrupted", api_request_id="waiting",
                    request={"reasoning_effort": "high"}, started_at=interrupt_start + 3,
                    model="interrupted-model", provider="test")
interrupted_reply = db.append_message("contract", "assistant", "Operation interrupted.",
                                      timestamp=interrupt_start + 4)
observations.end(task_id="contract", turn_id="interrupted", session_id="contract", interrupted=True)
partial = observations.read("contract", interrupted_reply)
assert partial["status"] == "interrupted" and partial["model"] == "interrupted-model"
assert partial["reasoning"] == "high" and "usage" not in partial
assert "duration_seconds" not in partial and "api_request_id" not in partial
observations.before(task_id="contract", turn_id="unsaved", request={},
                    started_at=interrupt_start + 5, model="unsaved-model")
observations.end(task_id="contract", turn_id="unsaved", session_id="contract", interrupted=True)
assert observations.read("contract", interrupted_reply) == partial


# The native inventory intentionally omits levels, while its transport still clamps
# to them. Use that same cached catalog; do not maintain our own model rules.
native_catalog = {
    "providers": [{"slug": "openrouter", "capabilities": {"contract-model": {"reasoning": True}}}]
}
with (
    patch.object(inventory, "load_picker_context", return_value=None),
    patch.object(
        inventory,
        "build_model_options_payload",
        side_effect=lambda *a, **k: deepcopy(native_catalog),
    ),
    patch.object(native_caps, "warm_openrouter_reasoning_caps_async"),
    patch.object(
        native_caps,
        "openrouter_model_reasoning_capabilities",
        return_value={
            "supports_reasoning": True,
            "supported_efforts": ["xhigh", "high"],
            "mandatory": False,
        },
    ),
):
    limits = model_options()["providers"][0]["capabilities"]["contract-model"]["supported_efforts"]
    assert limits == ["high", "xhigh"]
    profile = get_provider_profile("openrouter")
    for requested in ["low", *limits]:
        extra, _ = profile.build_api_kwargs_extras(
            model="contract-model",
            supports_reasoning=True,
            reasoning_config={"enabled": True, "effort": requested},
        )
        assert extra["reasoning"]["effort"] == ("high" if requested == "low" else requested)
    with patch.object(inventory, "_reasoning_catalog_reader", side_effect=AttributeError):
        assert model_options() == native_catalog


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
        assert (await client.get("/talaria/v1/models")).status == 401
        headers = {"Authorization": "Bearer contract-test-secret"}
        caps = await (await client.get("/talaria/v1/capabilities", headers=headers)).json()
        assert caps["rewind"] is True and caps["agent"]["name"] == "Contract agent"
        assert "Private instructions" not in json.dumps(caps)
        # Malformed opt-in never grants access or raises outside the route's error boundary.
        for config in (
            None,
            [],
            {"plugins": ["talaria"]},
            {"plugins": {"enabled": "not-talaria"}},
            {"plugins": {"enabled": ["talaria"], "disabled": "unknown"}},
            *(
                {"plugins": {"enabled": ["talaria"], "disabled": malformed}}
                for malformed in ({}, "", 0, False)
            ),
        ):
            with patch("hermes_cli.config.load_config", return_value=config):
                assert (await client.get("/talaria/v1/capabilities", headers=headers)).status == 404
        # Optional rewind signature drift must not break identity or response details.
        with patch.object(
            db, "rewind_to_message", lambda sid, target, expected_active_ids=None: None
        ):
            response = await client.get("/talaria/v1/capabilities", headers=headers)
            assert response.status == 200 and (await response.json())["rewind"] is False
        for invalid in (-1, 0, 2**63):
            assert (
                await client.get(
                    f"/talaria/v1/sessions/contract/response?message_id={invalid}", headers=headers
                )
            ).status == 400
            assert (
                await client.post(
                    "/talaria/v1/sessions/contract/rewind",
                    headers=headers,
                    json={"message_id": invalid, "preview": True},
                )
            ).status == 400
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
        observations.save("contract", saved_reply, {"model": "older-observation"})
        response = await client.get("/talaria/v1/sessions/contract/context", headers=headers)
        assert response.status == 200 and (await response.json())["context"]["used"] is None


asyncio.run(http_contract())
db.close()
print("Native Hermes contracts passed")
