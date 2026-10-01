"""Native indexed search, archived windows and per-owner activity isolation."""

import inspect
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB

from talaria.hermes import Hermes
from talaria.hermes_plugin import activity, search
from talaria.transcripts import message_page


async def verify_titles(run, db, configure, client, headers):
    from hermes_live_contract import wait_for

    configure(auxiliary={"title_generation": {"enabled": True}})
    response = await client.post("/api/sessions", headers=headers, json={"source": "api_server"})
    assert response.status == 201, await response.text()
    sid = (await response.json())["session"]["id"]
    assert not db.get_session(sid).get("title")
    with patch("agent.title_generator.generate_title", return_value="Hermes generated title"):
        await run("Please fix the profile configuration", "native-title", session_id=sid)
        await wait_for(lambda: db.get_session_title(sid) == "Hermes generated title")
    assert db.get_session_title_source(sid) == "llm"
    db.set_session_title(sid, "Owner title")
    await run("Keep my chosen title", "owner-title", session_id=sid)
    assert db.get_session_title(sid) == "Owner title"
    assert db.get_session_title_source(sid) == "user"
    configure(debug_requests=False)


def verify_views(home):
    db = SessionDB(home / "views.db")
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "fixture-key"}))
    try:
        assert search.supported(db, adapter)
        db.create_session("visible", "api_server")
        target = db.append_message("visible", "user", "uncommonneedle 故事")
        db.append_message("visible", "assistant", "An answer")
        removed = db.append_message("visible", "user", "uncommonneedle removed")
        db.rewind_to_message("visible", removed)
        db.create_session("hidden", "api_server")
        db.append_message("hidden", "assistant", "uncommonneedle hidden")
        db.set_session_hidden("hidden", True)
        hits = search.search(db, "uncommonneedle", 0)["data"]
        assert [(hit["session_id"], hit["id"]) for hit in hits] == [("visible", target)]
        assert "context" not in hits[0] and "content" not in hits[0]
        window = search.around(db, adapter, "visible", target)
        assert len(window["data"]) == 2
        assert window["data"][0]["id"] == target
        assert search.around(db, adapter, "visible", removed) is None
        assert search.around(db, adapter, "hidden", target) is None
        assert search.around(db, adapter, "visible", 999999) is None
        assert search.search(db, "故事", 0)["data"][0]["id"] == target
        db.archive_and_compact("visible", [{"role": "user", "content": "A compact summary"}])
        assert search.search(db, "uncommonneedle", 0)["data"][0]["id"] == target
        assert search.around(db, adapter, "visible", target)["message_id"] == target
        verify_activity(adapter)
    finally:
        db.close()
        from gateway.platforms.api_server_runs import _close_run_state

        _close_run_state(adapter)


async def verify_history(home):
    db = SessionDB(home / "history.db")
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "fixture-key"}))
    adapter._session_db = db
    app = web.Application()
    app.router.add_get("/api/sessions/{session_id}/messages", adapter._handle_session_messages)
    try:
        db.create_session("history", "api_server")
        db.append_message("history", "user", "Earlier question")
        db.append_message("history", "assistant", "Earlier answer")
        db.archive_and_compact("history", [{"role": "user", "content": "Synthetic summary"}])
        db.append_message("history", "user", "Current question")
        db.append_message("history", "assistant", "Current answer")
        removed = db.append_message("history", "user", "Rewound question")
        db.append_message("history", "assistant", "Rewound answer")
        db.rewind_to_message("history", removed)
        async with TestClient(TestServer(app)) as client:
            transport = Hermes(str(client.make_url("/")), "fixture-key")
            try:
                pages = [
                    await message_page(transport, "history", offset, order="oldest", limit=2)
                    for offset in (0, 2, 4)
                ]
                rows = [row for page in pages for row in page["data"]]
                assert len({row["id"] for row in rows}) == len(rows)
                content = [row["content"] for row in rows]
                assert content.count("Current question") == content.count("Current answer") == 1
                assert not any("Rewound" in text for text in content)
                # Older API servers ignore include_compacted; current Hermes must
                # return the archived display history through its native projection.
                if "include_ancestors" in inspect.signature(db.get_messages).parameters:
                    assert content[:2] == ["Earlier question", "Earlier answer"], content
                latest = await message_page(transport, "history", order="latest", limit=1)
                assert latest["data"][0]["content"] == "Current answer"
            finally:
                await transport.close()
    finally:
        await adapter.disconnect()
        db.close()


def verify_activity(adapter):
    own = make_mocked_request(
        "GET", "/talaria/v1/activity", headers={"Authorization": "Bearer own"}
    )
    from gateway.platforms.api_server import _api_request_profile

    adapter._run_owners["mine"] = adapter._run_idempotency_scope(own)
    adapter._set_run_status("mine", "waiting_for_input", session_id="mine", output="PRIVATE")
    token = _api_request_profile.set("another-profile")
    try:
        adapter._run_owners["other"] = adapter._run_idempotency_scope(own)
        adapter._set_run_status("other", "running", session_id="other", output="PRIVATE")
    finally:
        _api_request_profile.reset(token)
    result = activity.snapshot(adapter, own)["data"]
    assert len(result) == 1 and result[0]["session_id"] == "mine"
    assert result[0]["status"] == "waiting_for_input"
    assert "PRIVATE" not in str(result)
    adapter._set_run_status("mine", "completed")
    assert activity.snapshot(adapter, own)["data"][0]["status"] == "completed"
    adapter._run_owners["newer"] = adapter._run_idempotency_scope(own)
    adapter._set_run_status("newer", "running", session_id="mine", created_at=10**10)
    adapter._run_statuses["mine"] = adapter._run_statuses.pop("mine")
    assert activity.snapshot(adapter, own)["data"][0]["run_id"] == "newer"
    # Same bearer in another native profile must not inherit this run.
    token = _api_request_profile.set("another-profile")
    try:
        assert [row["session_id"] for row in activity.snapshot(adapter, own)["data"]] == ["other"]
    finally:
        _api_request_profile.reset(token)
