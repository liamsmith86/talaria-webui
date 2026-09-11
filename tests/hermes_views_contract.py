"""Native indexed search, archived windows and per-owner activity isolation."""

from aiohttp.test_utils import make_mocked_request
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB

from talaria.hermes_plugin import activity, search


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
