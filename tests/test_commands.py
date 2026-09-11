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
    expect(page.locator(".conversation-content .command-card")).to_be_visible()
    expect(page.locator(".composer .command-card")).to_have_count(0)
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
    expect(page.locator(".command-card")).to_contain_text("Compressing context…")
    page.reload()
    expect(page.get_by_role("textbox", name="Message Hermes")).to_have_value("")
    expect(page.locator(".command-card")).to_contain_text("Compressing context…")
    done = True
    expect(page.locator(".command-card")).to_contain_text("Recovered command result", timeout=10000)
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
    expect(page.locator(".command-card")).to_contain_text("Compressing context…")
    page.evaluate("async sid => (await import('/static/store.js')).openSession(sid)", other)
    done = True
    page.wait_for_function("() => localStorage.getItem('talaria.command.pending') === 'null'")
    assert page.evaluate("async () => (await import('/static/store.js')).state.active") == other
    expect(page.locator(".command-card")).to_have_count(0)
    page.evaluate("async sid => (await import('/static/store.js')).openSession(sid)", sid)
    expect(page.locator(".command-card")).to_contain_text("Done")


def test_command_card_live_states_and_mobile_layout(page, live_app):
    from pathlib import Path

    sid = prepare(page, live_app)
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(reduced_motion="reduce")
    page.evaluate("document.documentElement.dataset.theme = 'dark'")
    phase = "running"
    admissions = []

    page.route("**/api/commands", lambda route: admissions.append(route))

    def progress(route):
        if phase == "offline":
            route.fulfill(status=503, json={"error": "Temporarily unavailable"})
        else:
            route.fulfill(
                json={
                    "status": phase,
                    "session_id": sid,
                    "error": "Summary provider is unavailable.",
                }
            )

    page.route("**/api/commands/*", progress)
    page.get_by_role("textbox", name="Message Hermes").fill("/compress ")
    page.get_by_role("button", name="Send message", exact=True).click()
    card = page.locator(".conversation-content .command-card")
    expect(card).to_contain_text("Compressing context…")
    node = card.element_handle()
    # Visible immediately, even while admission itself is still waiting on the network.
    assert admissions
    admissions[0].fulfill(json={"status": "running"})
    expect(card.locator(".command-elapsed")).not_to_have_text("0s")
    assert card.locator(".spinner").evaluate("el => getComputedStyle(el).animationName") == "none"
    assert card.evaluate("el => el.scrollWidth <= el.clientWidth")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    Path("test-results").mkdir(exist_ok=True)
    page.screenshot(path="test-results/command-mobile-running.png", animations="disabled")
    phase = "offline"
    expect(card).to_contain_text("Reconnecting to Hermes…", timeout=10000)
    assert node.evaluate("el => el.isConnected && el === document.querySelector('.command-card')")
    phase = "failed"
    expect(card).to_contain_text("Command failed", timeout=10000)
    expect(card).to_contain_text("Summary provider is unavailable.")
    expect(card.locator(".spinner")).to_have_count(0)
    assert node.evaluate("el => el.isConnected && el === document.querySelector('.command-card')")
    page.screenshot(path="test-results/command-mobile-failed.png", animations="disabled")
    page.get_by_role("button", name="Dismiss command result").click()
    expect(card).to_have_count(0)
    assert len(admissions) == 1 and not live_app[1].runs


def test_command_updates_respect_scroll_and_keep_their_place_in_chat(page, live_app):
    from pathlib import Path

    sid = prepare(page, live_app)
    peer = live_app[1]
    peer.messages[sid] = [
        {
            "id": i + 1,
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"Earlier message {i + 1}. " + "A paragraph of session history. " * 10,
        }
        for i in range(24)
    ]
    page.evaluate("async sid => (await import('/static/store.js')).openSession(sid)", sid)
    finished = False
    page.route("**/api/commands", lambda route: route.fulfill(json={"status": "running"}))
    page.route(
        "**/api/commands/*",
        lambda route: route.fulfill(
            json={
                "status": "completed" if finished else "running",
                "session_id": sid,
                "text": (
                    "Context reduced from 42,000 to 12,000 tokens.\nRecent exchanges preserved."
                ),
            }
        ),
    )
    page.get_by_role("textbox", name="Message Hermes").fill("/compress ")
    page.get_by_role("button", name="Send message", exact=True).click()
    card = page.locator(".conversation-content .command-card")
    expect(card).to_contain_text("Compressing context…")
    expect(card).to_be_in_viewport()
    node = card.element_handle()
    viewport = page.locator(".conversation-viewport")
    viewport.evaluate("el => el.scrollTop = 100")
    expect(page.get_by_role("button", name="Jump to latest message")).to_be_visible()
    page.wait_for_function(
        "() => document.querySelector('.conversation-viewport').scrollTop === 100"
    )
    finished = True
    expect(card).to_contain_text("Recent exchanges preserved.")
    assert abs(viewport.evaluate("el => el.scrollTop") - 100) < 2
    assert node.evaluate("el => el.isConnected && el === document.querySelector('.command-card')")
    peer.messages[sid].extend(
        [
            {"id": 25, "role": "user", "content": "A message sent after the command"},
            {"id": 26, "role": "assistant", "content": "A later reply"},
        ]
    )
    page.evaluate("async sid => (await import('/static/store.js')).refreshHistory(sid)", sid)
    expect(page.get_by_text("A later reply", exact=True)).to_be_visible()
    assert card.evaluate(
        "el => !!(el.compareDocumentPosition("
        "[...document.querySelectorAll('.user-content')].at(-1)) "
        "& Node.DOCUMENT_POSITION_FOLLOWING)"
    )
    page.get_by_role("button", name="Jump to latest message").click()
    expect(card).to_be_in_viewport()
    Path("test-results").mkdir(exist_ok=True)
    page.screenshot(path="test-results/command-desktop-complete.png", animations="disabled")


def test_command_continues_while_viewing_a_read_only_child(page, live_app):
    sid = prepare(page, live_app)
    with signed_in(live_app[0]) as client:
        child = client.post("/api/sessions", json={"title": "Child session"}).json()["id"]
    finished = False
    page.route("**/api/commands", lambda route: route.fulfill(json={"status": "running"}))
    page.route(
        "**/api/commands/*",
        lambda route: route.fulfill(
            json={
                "status": "completed" if finished else "running",
                "session_id": sid,
                "text": "Finished in background",
            }
        ),
    )
    page.get_by_role("textbox", name="Message Hermes").fill("/compress ")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.locator(".command-card")).to_contain_text("Compressing context…")
    page.evaluate(
        "async ids => (await import('/static/store.js')).openSession(ids.child, {id: ids.sid})",
        {"child": child, "sid": sid},
    )
    expect(page.locator(".composer")).to_have_count(0)
    finished = True
    page.wait_for_function("() => localStorage.getItem('talaria.command.pending') === 'null'")
    assert page.evaluate("async () => (await import('/static/store.js')).state.active") == child
    page.get_by_role("button", name="Back to parent session").click()
    expect(page.locator(".command-card")).to_contain_text("Finished in background")


def test_compression_refresh_keeps_chat_visible_until_new_history_arrives(page, live_app):
    sid = prepare(page, live_app)
    peer = live_app[1]
    peer.messages[sid] = [
        {"id": 1, "role": "user", "content": "Original question"},
        {"id": 2, "role": "assistant", "content": "Original reply remains visible"},
    ]
    page.evaluate("async sid => (await import('/static/store.js')).openSession(sid)", sid)
    history_requests = []
    page.route(f"**/api/sessions/{sid}/messages", lambda route: history_requests.append(route))
    page.route("**/api/commands", lambda route: route.fulfill(json={"status": "running"}))
    page.route(
        "**/api/commands/*",
        lambda route: route.fulfill(
            json={
                "status": "completed",
                "session_id": sid,
                "changed": True,
                "text": "Earlier context summarized.",
            }
        ),
    )
    page.get_by_role("textbox", name="Message Hermes").fill("/compress ")
    page.get_by_role("button", name="Send message", exact=True).click()
    card = page.locator(".command-card")
    expect(card).to_contain_text("Compressing context…")
    node = card.element_handle()
    expect(page.get_by_text("Original reply remains visible", exact=True)).to_be_visible()
    expect(page.locator(".history-loading")).to_have_count(0)
    page.wait_for_function("() => document.querySelector('.command-elapsed')?.textContent !== '0s'")
    page.get_by_role("textbox", name="Message Hermes").fill("Draft during history refresh")
    assert len(history_requests) == 1
    history_requests[0].fulfill(
        json={
            "session_id": sid,
            "data": [
                {"id": 3, "role": "user", "content": "Compacted context"},
                {"id": 4, "role": "assistant", "content": "Saved continuation"},
            ],
        }
    )
    expect(card).to_contain_text("Context compressed")
    expect(page.get_by_text("Saved continuation", exact=True)).to_be_visible()
    assert node.evaluate("el => el.isConnected && el === document.querySelector('.command-card')")
    expect(page.get_by_role("textbox", name="Message Hermes")).to_have_value(
        "Draft during history refresh"
    )
    assert not peer.runs


def test_compaction_continuation_preserves_unsent_draft(page, live_app):
    sid = prepare(page, live_app)
    with signed_in(live_app[0]) as client:
        continued = client.post("/api/sessions", json={"title": "Continued session"}).json()["id"]
    finished = False
    page.route(
        "**/api/commands",
        lambda route: route.fulfill(
            json={"commands": CATALOG} if route.request.method == "GET" else {"status": "running"}
        ),
    )
    page.route(
        "**/api/commands/*",
        lambda route: route.fulfill(
            json={
                "status": "completed" if finished else "running",
                "session_id": continued,
                "changed": True,
                "text": "Context compressed",
            }
        ),
    )
    composer = page.get_by_role("textbox", name="Message Hermes")
    composer.fill("/compress ")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(composer).to_have_value("")
    expect(page.locator(".command-card")).to_contain_text("Compressing context")
    draft = "Keep my unsent draft.\nIncluding this second line."
    composer.fill(draft)
    page.evaluate("window.compactionPageMarker = true")
    page.route(
        f"**/api/sessions/{sid}/messages",
        lambda route: route.fulfill(
            json={
                "session_id": continued,
                "data": [],
            }
        ),
    )
    finished = True
    expect(page.locator(".command-card")).to_contain_text("Context compressed")
    expect(composer).to_have_value(draft)
    assert page.evaluate("window.compactionPageMarker") is True
    assert page.evaluate("async () => (await import('/static/store.js')).state.active") == continued
    page.reload()
    expect(composer).to_have_value(draft)
    assert not live_app[1].runs


def test_command_acknowledgement_preserves_draft_after_leaving_child_view(page, live_app):
    sid = prepare(page, live_app)
    with signed_in(live_app[0]) as client:
        child = client.post("/api/sessions", json={"title": "Read-only child"}).json()["id"]
    admissions = []
    page.route(
        "**/api/commands",
        lambda route: (
            route.fulfill(json={"commands": CATALOG})
            if route.request.method == "GET"
            else admissions.append(route)
        ),
    )
    page.route(
        "**/api/commands/*",
        lambda route: route.fulfill(
            json={
                "status": "completed",
                "session_id": sid,
                "text": "Context compressed",
            }
        ),
    )
    composer = page.get_by_role("textbox", name="Message Hermes")
    composer.fill("/compress ")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.locator(".command-card")).to_contain_text("Compressing context")
    page.evaluate(
        "async ids => (await import('/static/store.js')).openSession(ids.child, {id: ids.sid})",
        {"child": child, "sid": sid},
    )
    expect(page.locator(".composer")).to_have_count(0)
    page.get_by_role("button", name="Back to parent session").click()
    composer.fill("New draft written before command acknowledgement")
    assert len(admissions) == 1
    admissions[0].fulfill(json={"status": "running"})
    expect(page.locator(".command-card")).to_contain_text("Context compressed")
    expect(composer).to_have_value("New draft written before command acknowledgement")
    page.reload()
    expect(composer).to_have_value("New draft written before command acknowledgement")
