import base64
import json
from pathlib import Path

import pytest
from playwright.sync_api import expect
from starlette.requests import Request
from starlette.routing import Route

from talaria.app import create_app
from talaria.profiles import connection_url, split_url
from talaria.relay import Channel

from .fake_hermes import KEY, FakeHermes
from .test_app import signed_in
from .test_conversation_features import png

RESEARCH_KEY = "separate-research-key"


@pytest.fixture
def research(live_app):
    second = FakeHermes(RESEARCH_KEY)

    async def routed(request):
        scope = {**request.scope, "path": request.scope["path"].removeprefix("/p/research")}
        return await second.handle(Request(scope, request.receive))

    live_app[1].app.router.routes.insert(
        0,
        Route(
            "/p/research/{path:path}",
            routed,
            methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        ),
    )
    with signed_in(live_app[0]) as client:
        response = client.post(
            "/api/profiles",
            json={
                "label": "Research",
                "url": live_app[2].state.settings.hermes_url,
                "profile": "research",
                "api_key": RESEARCH_KEY,
            },
        )
        assert response.status_code == 201, response.text
        profile_id = response.json()["id"]
    return profile_id, second


def scoped(path, profile_id):
    return path + ("&" if "?" in path else "?") + "talaria_profile=" + profile_id


def test_native_profile_urls_preserve_reverse_proxy_prefixes():
    assert (
        connection_url({"url": "https://example.test/hermes/v1", "profile": "research"})
        == "https://example.test/hermes/p/research"
    )
    assert split_url("https://example.test/hermes/p/research") == (
        "https://example.test/hermes",
        "research",
    )
    assert (
        connection_url({"url": "https://example.test/hermes/p/research", "profile": "default"})
        == "https://example.test/hermes"
    )
    for invalid in ("../default", "a/b", "", 42):
        with pytest.raises(ValueError):
            connection_url({"url": "https://example.test", "profile": invalid})


def test_profiles_own_sessions_exports_runs_controls_and_credentials(live_app, research):
    profile_id, second = research
    first = live_app[1]
    with signed_in(live_app[0]) as client:
        prod = client.post("/api/sessions", json={"title": "Production only"}).json()["id"]
        own = client.post(
            scoped("/api/sessions", profile_id), json={"title": "Research only"}
        ).json()["id"]
        assert prod in first.sessions and prod not in second.sessions
        assert own in second.sessions and own not in first.sessions
        assert {
            s["id"] for s in client.get(scoped("/api/sessions", profile_id)).json()["data"]
        } == {own}
        for method, path, body in (
            ("GET", f"/api/sessions/{prod}", None),
            ("GET", f"/api/sessions/{prod}/messages", None),
            ("GET", f"/api/sessions/{prod}/export?format=json", None),
            ("PATCH", f"/api/sessions/{prod}", {"title": "Wrong"}),
            ("DELETE", f"/api/sessions/{prod}", {}),
            ("POST", f"/api/sessions/{prod}/fork", {"title": "Wrong branch"}),
        ):
            assert client.request(method, scoped(path, profile_id), json=body).status_code == 404
        rid = client.post("/api/runs", json={"session_id": prod, "input": "slow response"}).json()[
            "run_id"
        ]
        for method, path, body in (
            ("GET", f"/api/runs/{rid}", None),
            ("POST", f"/api/runs/{rid}/stop", {}),
            ("POST", f"/api/runs/{rid}/steer", {"input": "Wrong"}),
            ("POST", f"/api/runs/{rid}/approval", {"choice": "once"}),
        ):
            assert client.request(method, scoped(path, profile_id), json=body).status_code == 404
        assert first.stops == 0 and first.approvals == 0
        # A stream cannot return another profile's events either.
        events = client.get(scoped(f"/api/runs/{rid}/events", profile_id)).text
        assert "message.delta" not in events and "run.completed" not in events
        client.post(f"/api/runs/{rid}/stop", json={})
        boot = client.get(scoped("/api/bootstrap", profile_id)).json()
        assert boot["profile"]["id"] == profile_id
        for path in ("/api/bootstrap", "/api/profiles", "/api/connection"):
            response = client.get(scoped(path, profile_id))
            assert KEY not in response.text and RESEARCH_KEY not in response.text
        stored = live_app[2].state.config_path.with_name("profiles.json")
        assert stored.stat().st_mode & 0o077 == 0
        assert RESEARCH_KEY in stored.read_text()


def test_unknown_profile_never_falls_back_and_saved_connection_is_immutable(live_app, research):
    with signed_in(live_app[0]) as client:
        for path in ("/api/bootstrap", "/api/sessions", "/api/connection"):
            response = client.get(scoped(path, "missing"))
            assert response.status_code == 404 and response.json()["code"] == "profile_missing"
        before = live_app[2].state.settings.hermes_url
        changed = client.put(
            "/api/connection",
            json={
                "url": before,
                "profile": "research",
                "api_key": RESEARCH_KEY,
            },
        )
        assert changed.status_code == 409
        assert live_app[2].state.settings.hermes_url == before
        duplicate = client.post(
            "/api/profiles",
            json={
                "url": before,
                "profile": "research",
                "api_key": RESEARCH_KEY,
                "label": "Duplicate",
            },
        )
        assert duplicate.status_code == 409


def test_profile_config_survives_restart_and_corruption_keeps_default(live_app, research):
    profile_id, _ = research
    app = create_app(live_app[2].state.settings, live_app[2].state.config_path)
    assert app.state.profiles.describe(profile_id)["profile"] == "research"
    # No child connection is opened until the profile is selected.
    assert list(app.state.profiles.apps) == ["default"]
    app.state.config_path.with_name("profiles.json").write_text("bad json")
    recovered = create_app(app.state.settings, app.state.config_path)
    assert recovered.state.profiles.public()["error"]
    assert recovered.state.profiles.public()["profiles"][0]["id"] == "default"


def test_profiles_share_the_original_replay_memory_budget(live_app, research):
    profile_id, _ = research
    profiles = live_app[2].state.profiles
    first = live_app[2].state.relay
    for index in range(32):
        rid = f"finished-{index}"
        first.channels[rid] = Channel(rid, finished=True, touched=index)
    with signed_in(live_app[0]) as client:
        response = client.get(scoped("/api/runs/not-a-real-run/events", profile_id))
        assert response.status_code == 200
    assert len(first.channels) == 31 and "finished-0" not in first.channels
    assert sum(len(app.state.relay.channels) for app in profiles.apps.values()) == 32


def test_removal_only_forgets_the_connection(live_app, research):
    profile_id, second = research
    with signed_in(live_app[0]) as client:
        own = client.post(scoped("/api/sessions", profile_id), json={"title": "Keep me"}).json()[
            "id"
        ]
        assert client.delete(scoped(f"/api/profiles/{profile_id}", profile_id)).status_code == 409
        assert client.delete(f"/api/profiles/{profile_id}").status_code == 200
        assert own in second.sessions
        assert client.get(scoped("/api/sessions", profile_id)).status_code == 404
        assert client.delete("/api/profiles/default").status_code == 400


def test_named_settings_and_readiness_stay_with_their_profile(live_app, research, tmp_path):
    profile_id, second = research
    second.extension = {"agent": {"name": "Research assistant"}}
    second.default_model = "research-model"
    second.discovery_overrides["/health/detailed"] = ({"status": "degraded"}, 200)
    original = live_app[2].state.settings
    with signed_in(live_app[0]) as client:
        second.api_key = "rotated-research-key"
        assert (
            client.put(
                scoped("/api/connection", profile_id),
                json={"url": original.hermes_url, "profile": "research", "api_key": second.api_key},
            ).status_code
            == 200
        )
        client.get(scoped("/api/capabilities", profile_id))
        assert client.get(scoped("/api/bootstrap", profile_id)).json()["agent"]["name"] == (
            "Research assistant"
        )
        assert client.get(scoped("/api/models", profile_id)).json()["model"] == "research-model"
        assert client.get("/api/models").json()["model"] == "hermes-test"
        assert client.get(scoped("/api/readiness", profile_id)).json()["status"] == "degraded"
        assert client.get("/api/readiness").json()["status"] == "ok"
        assert live_app[2].state.settings == original
        record = live_app[2].state.profiles.records[profile_id]
        assert record["api_key"] == second.api_key and "hermes_home" not in record


def test_same_session_ids_and_images_are_isolated_and_responses_survive_switching(
    page, live_app, research
):
    profile_id, second = research
    sid = "same-session-id"
    for peer, name in ((live_app[1], "Production"), (second, "Research")):
        peer.sessions[sid] = {"id": sid, "title": name, "source": "api_server"}
        peer.messages[sid] = [{"id": 1, "role": "user", "content": f"{name} history\n[screenshot]"}]
    page.reload()
    page.get_by_role("button", name="Production", exact=True).click()
    page.evaluate(
        """async ([sid, url]) => {
      const files = await import('/static/attachments.js');
      await files.cacheMessageImages(sid, 1, [{name:'production.png', url}]);
      await (await import('/static/store.js')).refreshHistory(sid);
    }""",
        [sid, "data:image/png;base64," + base64.b64encode(png()).decode()],
    )
    expect(page.get_by_role("button", name="Open production.png", exact=True)).to_be_visible()
    page.get_by_role("button", name="Switch profile").click()
    page.get_by_role("link", name="Research research", exact=False).click()
    page.get_by_role("button", name="Research", exact=True).click()
    expect(page.get_by_text("Research history", exact=False)).to_be_visible()
    expect(page.get_by_text("Production history", exact=False)).to_have_count(0)
    expect(page.get_by_role("button", name="Open production.png", exact=True)).to_have_count(0)
    page.get_by_label("Message Hermes").fill("Wait for approval while I switch profiles")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_role("button", name="Allow once")).to_be_visible()
    with signed_in(live_app[0]) as client:
        assert client.delete(f"/api/profiles/{profile_id}").status_code == 409
    page.get_by_role("button", name="Switch profile").click()
    page.get_by_role("link", name="Default profile default", exact=False).click()
    expect(page.get_by_role("button", name="Open production.png", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Allow once")).to_have_count(0)
    page.get_by_role("button", name="Switch profile").click()
    page.get_by_role("link", name="Research research", exact=False).click()
    expect(page.get_by_role("button", name="Allow once")).to_be_visible()
    page.get_by_role("button", name="Allow once").click()
    expect(page.get_by_text("What would you like to explore next?", exact=True)).to_be_visible()
    assert second.approvals == 1 and live_app[1].approvals == 0
    assert len(live_app[1].messages[sid]) == 1 and len(second.messages[sid]) == 3


def test_browser_switching_separates_drafts_images_preferences_and_live_requests(
    page, live_app, research
):
    profile_id, second = research
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.get_by_label("Message Hermes").fill("Production draft")
    page.get_by_label("File attachment").set_input_files(
        {"name": "blue.png", "mimeType": "image/png", "buffer": png()}
    )
    expect(page.locator(".attachment-chip")).to_have_count(1)
    page.get_by_role("button", name="Switch profile").click()
    page.get_by_role("link", name="Research research", exact=False).click()
    expect(page.locator(".agent-switcher")).to_contain_text("Research")
    expect(page.get_by_label("Message Hermes")).to_have_value("")
    expect(page.locator(".attachment-chip")).to_have_count(0)
    page.get_by_label("Message Hermes").fill("Research draft")
    page.evaluate(
        "async () => (await import('/static/lib.js'))"
        ".writeStorage('model-choices', 'research-only')"
    )
    # A second tab can stay on production while the first one uses Research.
    other = page.context.new_page()
    try:
        other.goto(live_app[0])
        expect(other.get_by_label("Message Hermes")).to_have_value("Production draft")
        expect(other.locator(".attachment-chip")).to_have_count(1)
        page.get_by_label("Message Hermes").fill("A scoped streaming response")
        page.get_by_role("button", name="Send message", exact=True).click()
        expect(page.get_by_text("What would you like to explore next?", exact=True)).to_be_visible()
        assert len(second.runs) == 1 and not live_app[1].runs
        expect(other.get_by_label("Message Hermes")).to_have_value("Production draft")
        assert (
            other.evaluate(
                "async () => (await import('/static/lib.js')).readStorage('model-choices')"
            )
            == ""
        )
        # Download uses the selected profile too.
        page.get_by_role("button", name="Session options", exact=True).click()
        page.get_by_role("button", name="Download transcript", exact=True).click()
        with page.expect_download() as download:
            page.get_by_role(
                "button", name="JSON Messages, tools, and image data.", exact=False
            ).click()
        data = json.loads(Path(download.value.path()).read_text())
        assert any(
            "scoped streaming" in str(message.get("content")) for message in data["messages"]
        )
    finally:
        other.close()
    assert not errors


def test_missing_profile_shows_a_chooser_without_loading_default_sessions(page, live_app):
    page.goto(live_app[0] + "/?profile=" + "f" * 32)
    expect(page.get_by_role("dialog", name="Choose a profile")).to_be_visible()
    expect(page.get_by_text("This profile is no longer available. Choose another.")).to_be_visible()
    expect(page.get_by_role("button", name="Close dialog")).to_have_count(0)
    page.get_by_role("link", name="Default profile default", exact=False).click()
    expect(page.locator(".topbar-title")).to_be_visible()


def test_missing_profile_can_test_and_add_an_existing_connection(page, live_app, research):
    profile_id, _ = research
    with signed_in(live_app[0]) as client:
        assert client.delete(f"/api/profiles/{profile_id}").status_code == 200
    page.goto(live_app[0] + "/?profile=" + profile_id)
    page.get_by_role("button", name="Manage profiles", exact=True).click()
    page.get_by_role("button", name="Add profile", exact=True).click()
    page.get_by_label("Display name").fill("Restored research")
    page.get_by_label("Hermes profile", exact=True).fill("research")
    page.get_by_label("API key", exact=True).fill(RESEARCH_KEY)
    page.get_by_role("button", name="Test connection", exact=True).click()
    expect(page.get_by_text("Hermes connection verified.", exact=True)).to_be_visible()
    page.get_by_role("button", name="Add profile", exact=True).click()
    expect(page.get_by_text("Restored research", exact=True)).to_be_visible()


def test_add_profile_form_reuses_connection_validation(page, research):
    page.get_by_role("button", name="Settings").click()
    page.get_by_role("tab", name="Connection", exact=True).click()
    page.get_by_role("button", name="Add profile", exact=True).click()
    expect(page.get_by_label("Display name")).to_be_visible()
    page.get_by_label("Display name").fill("Duplicate research")
    page.get_by_label("Hermes profile", exact=True).fill("research")
    page.get_by_label("API key", exact=True).fill(RESEARCH_KEY)
    page.get_by_role("button", name="Test connection", exact=True).click()
    expect(page.get_by_text("Hermes connection verified.", exact=True)).to_be_visible()
    page.get_by_role("button", name="Add profile", exact=True).click()
    expect(
        page.get_by_text("This Hermes profile is already saved. Select it from the profile menu.")
    ).to_be_visible()
