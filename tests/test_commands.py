"""Browser command routing must not create chat turns or repeat uncertain mutations."""

import json

from playwright.sync_api import expect

from .test_app import signed_in

CATALOG = [
    {
        "name": "compress",
        "aliases": ["compact"],
        "description": "Compress session context",
        "mode": "native",
        "reason": "",
    },
    {
        "name": "clear",
        "aliases": [],
        "description": "Clear terminal",
        "mode": "unavailable",
        "reason": "Use this command in Hermes's terminal.",
    },
    {
        "name": "title",
        "aliases": [],
        "description": "Name the session",
        "mode": "web",
        "reason": "",
    },
]


def prepare(page, live_app):
    url, peer, _ = live_app
    with signed_in(url) as client:
        sid = client.post("/api/sessions", json={"title": "Command session"}).json()["id"]
    peer.extension = {"commands": True}
    peer.discovery_overrides["/talaria/v1/commands"] = ({"commands": CATALOG}, 200)
    page.evaluate(
        "async (sid) => { const s = await import('/static/store.js'); "
        "await s.connect(); await s.openSession(sid); }",
        sid,
    )
    return sid


def test_command_picker_alias_and_result_do_not_become_messages(page, live_app):
    sid = prepare(page, live_app)
    posted = []

    def commands(route):
        if route.request.method == "GET":
            route.fulfill(json={"commands": CATALOG})
        else:
            posted.append(route.request.post_data_json)
            route.fulfill(json={"status": "running"})

    page.route("**/api/commands", commands)
    page.route(
        "**/api/commands/*",
        lambda route: route.fulfill(
            json={
                "status": "completed",
                "session_id": sid,
                "text": "Compressed context successfully.",
            }
        ),
    )
    composer = page.get_by_role("textbox", name="Message Hermes")
    composer.fill("/comp")
    expect(page.get_by_role("option")).to_have_count(1)
    composer.press("Tab")
    expect(composer).to_have_value("/compress ")
    composer.fill("/compact --preview")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(
        page.get_by_role("status").filter(has_text="Compressed context successfully.")
    ).to_be_visible()
    assert len(posted) == 1 and posted[0]["command"] == "compress"
    assert posted[0]["args"] == "--preview" and posted[0]["session_id"] == sid
    assert not live_app[1].runs and not live_app[1].messages[sid]
    expect(composer).to_have_value("")
    composer.fill("/clear ")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_text("Use this command in Hermes's terminal.", exact=True)).to_be_visible()
    assert len(posted) == 1


def test_pending_command_recovers_after_reload_without_posting_again(page, live_app):
    sid = prepare(page, live_app)
    posted = []
    done = False

    def admission(route):
        if route.request.method == "GET":
            route.fulfill(json={"commands": CATALOG})
        else:
            posted.append(route.request.post_data_json)
            route.abort("failed")

    page.route("**/api/commands", admission)
    page.route(
        "**/api/commands/*",
        lambda route: route.fulfill(
            json={"status": "completed", "session_id": sid, "text": "Recovered command result"}
            if done
            else {"status": "running"}
        ),
    )
    page.get_by_role("textbox", name="Message Hermes").fill("/compress --preview")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.locator(".command-feedback")).to_contain_text("/compress…")
    page.reload()
    expect(page.get_by_role("textbox", name="Message Hermes")).to_have_value("")
    expect(page.locator(".command-feedback")).to_contain_text("/compress…")
    done = True
    expect(page.locator(".command-feedback")).to_contain_text(
        "Recovered command result", timeout=10000
    )
    assert len(posted) == 1
    assert not live_app[1].runs


def test_command_proxy_validates_body_and_keeps_authentication(live_app):
    url, peer, _ = live_app
    peer.discovery_overrides["/talaria/v1/commands"] = ({"commands": CATALOG}, 200)
    with signed_in(url) as client:
        assert client.get("/api/commands").json() == {"commands": CATALOG}
        assert client.post("/api/commands", json={"args": "x" * 2001}).status_code == 400
        assert client.post("/api/commands", content=json.dumps({"command": []})).status_code == 400


def test_command_completion_does_not_navigate_away_from_another_session(page, live_app):
    sid = prepare(page, live_app)
    with signed_in(live_app[0]) as client:
        other = client.post("/api/sessions", json={"title": "Other session"}).json()["id"]
    done = False
    page.route("**/api/commands", lambda route: route.fulfill(json={"status": "running"}))
    page.route(
        "**/api/commands/*",
        lambda route: route.fulfill(
            json={"status": "completed", "session_id": sid, "text": "Done"}
            if done
            else {"status": "running"}
        ),
    )
    page.get_by_role("textbox", name="Message Hermes").fill("/compress --preview")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.locator(".command-feedback")).to_contain_text("/compress…")
    page.evaluate("async sid => (await import('/static/store.js')).openSession(sid)", other)
    done = True
    page.wait_for_function("() => localStorage.getItem('talaria.command.pending') === 'null'")
    assert page.evaluate("async () => (await import('/static/store.js')).state.active") == other
    expect(page.locator(".command-feedback")).to_have_count(0)
    page.evaluate("async sid => (await import('/static/store.js')).openSession(sid)", sid)
    expect(page.locator(".command-feedback")).to_contain_text("Done")
