import json

import pytest
from playwright.sync_api import expect

from .test_app import signed_in


def test_profile_context_runs_use_existing_history_replay_and_events(live_app):
    peer = live_app[1]
    peer.extension = {"context_runs": True}
    # Existing browser tabs may resume against a freshly restarted Talaria before
    # any capabilities request. Context must not depend on cached UI discovery.
    assert live_app[2].state.extensions == {}
    with signed_in(live_app[0]) as client:
        sid = client.post("/api/sessions", json={"title": "Inherited context"}).json()["id"]
        payload = {
            "session_id": sid,
            "input": "My actual message",
            "request_id": "context-run",
            "reasoning": "high",
        }
        result = client.post("/api/runs", json=payload)
        assert result.status_code == 202
        rid = result.json()["run_id"]
        assert client.post("/api/runs", json=payload).json()["run_id"] == rid
        assert "run.completed" in client.get(f"/api/runs/{rid}/events").text
        assert len(peer.runs) == 1
        assert peer.runs[rid]["model_options"]["reasoning"]["effort"] == "high"
        assert peer.messages[sid][0]["content"] == payload["input"]
        assert any(path == "/talaria/v1/runs" for _, path, _ in peer.calls)
        assert not any(path == "/v1/runs" for _, path, _ in peer.calls)


@pytest.mark.parametrize("failure,fallback", [(404, True), (503, False)])
def test_absent_context_route_falls_back_but_ambiguous_dispatch_never_retries(
    live_app, failure, fallback
):
    peer = live_app[1]
    peer.extension = {"context_runs": True}
    peer.discovery_overrides["/talaria/v1/runs"] = ({"error": "Unavailable"}, failure)
    with signed_in(live_app[0]) as client:
        client.get("/api/capabilities")
        sid = client.post("/api/sessions", json={"title": "Fallback"}).json()["id"]
        response = client.post(
            "/api/runs", json={"session_id": sid, "input": "Hello", "request_id": "fallback"}
        )
        assert response.status_code == (202 if fallback else 502)
        assert any(path == "/v1/runs" for _, path, _ in peer.calls) is fallback


def test_context_status_is_private_and_degrades_independently(page, live_app):
    peer = live_app[1]
    peer.extension = {
        "context_runs": True,
        "profile_context": {
            "instructions": "ready",
            "prefill": "unavailable",
            "prompt": "PRIVATE INSTRUCTION",
            "path": "/private/prefill.json",
        },
    }
    page.reload()
    page.get_by_role("button", name="Your space").click()
    page.get_by_role("tab", name="Connection", exact=True).click()
    section = page.locator(".extended-access")
    expect(section).to_contain_text("Some profile context could not be loaded")
    with signed_in(live_app[0]) as client:
        caps = client.get("/api/capabilities").json()
        assert "PRIVATE INSTRUCTION" not in json.dumps(caps)
        assert "/private/prefill.json" not in json.dumps(caps)
        peer.extension["profile_context"] = {"instructions": [], "prefill": {"unexpected": True}}
        context = client.get("/api/capabilities").json()["talaria_extensions"]["profile_context"]
        assert context == {"instructions": "unavailable", "prefill": "unavailable"}
    peer.extension["profile_context"] = {"instructions": "ready", "prefill": "not_configured"}
    page.get_by_role("button", name="Check again").click()
    expect(section).not_to_contain_text("Some profile context could not be loaded")
