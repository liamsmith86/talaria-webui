"""Tab navigation and refresh through the real UI and a synthetic Hermes server."""

import pytest
from playwright.sync_api import expect

from .conftest import wait_for_store
from .test_conversation_features import seed


def open_session(page, sid):
    page.evaluate("async id => (await import('/static/store.js')).openSession(id)", sid)


def refresh(page, sid):
    page.evaluate("async id => (await import('/static/store.js')).refreshHistory(id)", sid)


def history_ids(page):
    return page.evaluate("import('/static/store.js').then(s => s.state.history.map(m => m.id))")


def test_tabs_keep_their_session_draft_and_browser_history(page, live_app):
    a = seed(live_app[1], "tab-a", count=2)
    b = seed(live_app[1], "tab-b", count=2)
    open_session(page, a)
    page.get_by_label("Message Hermes").fill("Draft in A")
    other = page.context.new_page()
    try:
        other.goto(live_app[0] + "?session=" + b)
        expect(other.get_by_label("Message Hermes")).to_be_visible()
        other.get_by_label("Message Hermes").fill("Draft in B")
        page.reload()
        expect(page.get_by_label("Message Hermes")).to_have_value("Draft in A")
        wait_for_store(page, "s => s.active === 'tab-a'")
        open_session(page, b)
        expect(page.get_by_label("Message Hermes")).to_have_value("Draft in B")
        page.go_back()
        expect(page.get_by_label("Message Hermes")).to_have_value("Draft in A")
        page.go_forward()
        expect(page.get_by_label("Message Hermes")).to_have_value("Draft in B")
        page.locator(".new-chat").click()
        page.get_by_label("Message Hermes").fill("Unsent new session")
        open_session(other, a)
        page.reload()
        expect(page.get_by_label("Message Hermes")).to_have_value("Unsent new session")
        wait_for_store(page, "s => s.active === null")
    finally:
        other.close()


def test_session_link_opens_independent_tab(page, live_app):
    seed(live_app[1], "linked", count=2)
    page.evaluate("import('/static/store.js').then(s => s.refreshSessions())")
    link = page.get_by_role("link", name="Design notes", exact=True)
    expect(link).to_have_attribute("href", "/?session=linked")
    with page.context.expect_page() as opened:
        link.click(button="middle")
    other = opened.value
    try:
        expect(other.get_by_label("Message Hermes")).to_be_visible()
        wait_for_store(other, "s => s.active === 'linked'")
        wait_for_store(page, "s => s.active === null")
    finally:
        other.close()


@pytest.mark.parametrize("count", [150, 610])
def test_refresh_preserves_loaded_range_and_removes_deleted_messages(page, live_app, count):
    sid = seed(live_app[1], "retained", count=count)
    open_session(page, sid)
    page.evaluate("""async () => {
        const s = await import('/static/store.js');
        while (s.state.historyHasMore) await s.loadOlderMessages();
    }""")
    live_app[1].messages[sid].append({"id": count + 1, "role": "assistant", "content": "New reply"})
    refresh(page, sid)
    assert history_ids(page) == list(range(1, count + 2))
    # Hermes owns deletions, including older loaded rows outside the latest page.
    live_app[1].messages[sid] = live_app[1].messages[sid][2:-2]
    refresh(page, sid)
    assert history_ids(page) == list(range(3, count))


def test_focus_catches_external_changes_without_erasing_draft_or_selection(page, live_app):
    sid = seed(live_app[1], "external", count=130)
    open_session(page, sid)
    page.evaluate("import('/static/store.js').then(s => s.loadOlderMessages())")
    composer = page.get_by_label("Message Hermes")
    composer.fill("My unsent draft")
    page.get_by_text("Message 041", exact=True).scroll_into_view_if_needed()
    page.evaluate("""() => {
        const text = [...document.querySelectorAll('.message-text p')]
            .find(e => e.textContent === 'Message 041');
        const range = document.createRange(); range.selectNodeContents(text);
        const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
        window.anchorBefore = text.getBoundingClientRect().top;
    }""")
    live_app[1].messages[sid].append(
        {"id": 131, "role": "assistant", "content": "Externally added"}
    )
    live_app[1].sessions[sid]["title"] = "Renamed elsewhere"
    del live_app[1].messages[sid][:2]
    calls = []
    page.on("request", lambda req: calls.append(req.url))
    page.evaluate("""() => {
        for (let i = 0; i < 20; i++) {
            window.dispatchEvent(new Event('focus'));
            window.dispatchEvent(new Event('online'));
            document.dispatchEvent(new Event('visibilitychange'));
        }
    }""")
    expect(page.get_by_text("Externally added", exact=True)).to_be_attached()
    expect(page.get_by_role("link", name="Renamed elsewhere", exact=True)).to_be_visible()
    expect(composer).to_have_value("My unsent draft")
    assert page.evaluate("getSelection().toString()") == "Message 041"
    assert (
        page.evaluate("""Math.abs([...document.querySelectorAll('.message-text p')]
        .find(e => e.textContent === 'Message 041').getBoundingClientRect().top
        - window.anchorBefore)""")
        < 3
    )
    assert len([url for url in calls if "/external/messages" in url]) == 1
    assert history_ids(page) == list(range(3, 132))


def test_refresh_compaction_uses_canonical_session_and_carries_draft(page, live_app):
    sid = seed(live_app[1], "before-compaction", count=2)
    open_session(page, sid)
    page.get_by_label("Message Hermes").fill("Draft survives external compaction")
    canonical = seed(live_app[1], "after-compaction", count=3)
    live_app[1].sessions[canonical]["parent_session_id"] = sid
    refresh(page, sid)
    wait_for_store(page, "s => s.active === 'after-compaction'")
    assert "session=after-compaction" in page.url
    expect(page.get_by_label("Message Hermes")).to_have_value("Draft survives external compaction")
    assert history_ids(page) == [1, 2, 3]


def test_delayed_focus_response_cannot_replace_new_navigation(page, live_app):
    a = seed(live_app[1], "slow-old", count=2)
    b = seed(live_app[1], "fast-new", count=3)
    open_session(page, a)
    routes = []
    page.route("**/api/sessions/slow-old/messages?*", lambda route: routes.append(route))
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(page.get_by_label("Message Hermes")).to_be_visible()
    page.wait_for_timeout(200)
    assert len(routes) == 1
    open_session(page, b)
    for route in routes:
        route.continue_()
    page.unroute("**/api/sessions/slow-old/messages?*")
    page.wait_for_timeout(200)
    wait_for_store(page, "s => s.active === 'fast-new'")
    assert history_ids(page) == [1, 2, 3]
    assert "session=fast-new" in page.url


def test_refresh_retries_shifted_offsets_without_duplicates(page):
    result = page.evaluate("""async () => {
        const {historyWindow} = await import('/static/history-page.js');
        const original = window.fetch;
        const rows = Array.from({length:610}, (_, i) =>
            ({id:i+1, role:'user', content:'Same text'}));
        const previous = rows.slice();
        let calls = 0;
        window.fetch = async url => {
            const params = new URL(url, location.href).searchParams;
            const offset = Number(params.get('offset')), limit = Number(params.get('limit'));
            const end = Math.max(0, rows.length - offset);
            const data = rows.slice(Math.max(0, end - limit), end);
            const page = {data, session_id:'moving', has_more:data.length === limit,
                next_offset:offset+data.length};
            if (++calls === 1)
                rows.push({id:611, role:'assistant', content:'Arrived between pages'});
            return Response.json(page);
        };
        try {
            const page = await historyWindow('moving', previous);
            return {ids:page.data.map(m => m.id), offset:page.next_offset, calls};
        } finally { window.fetch = original; }
    }""")
    assert result["ids"] == list(range(1, 612))
    assert result["offset"] == 611
    assert result["calls"] == 6


def test_return_during_cooldown_gets_one_delayed_refresh(page, live_app):
    sid = seed(live_app[1], "cooldown", count=2)
    open_session(page, sid)
    page.clock.install()
    calls = []
    page.on("request", lambda req: calls.append(req.url))
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    page.clock.run_for(150)
    wait_for_store(page, "s => s.sessionDetails?.id === 'cooldown'")
    # Wait for the complete batch, then return again inside the cooldown.
    page.wait_for_timeout(100)
    live_app[1].messages[sid].append({"id": 3, "role": "assistant", "content": "During cooldown"})
    page.evaluate("""() => {
        window.dispatchEvent(new Event('focus'));
        window.dispatchEvent(new Event('online'));
    }""")
    page.clock.run_for(500)
    assert len([url for url in calls if "/cooldown/messages" in url]) == 1
    page.clock.run_for(5000)
    expect(page.get_by_text("During cooldown", exact=True)).to_be_attached()
    assert len([url for url in calls if "/cooldown/messages" in url]) == 2


def test_failed_return_preserves_work_and_later_return_recovers(page, live_app):
    sid = seed(live_app[1], "unavailable", count=2)
    open_session(page, sid)
    composer = page.get_by_label("Message Hermes")
    composer.fill("Keep my work")
    page.route(
        "**/api/sessions/unavailable/messages?*",
        lambda route: route.fulfill(
            status=503, json={"error": "Hermes is temporarily unavailable"}
        ),
    )
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(page.get_by_text("Hermes is temporarily unavailable", exact=True)).to_be_visible()
    assert history_ids(page) == [1, 2]
    expect(composer).to_have_value("Keep my work")
    page.unroute("**/api/sessions/unavailable/messages?*")
    live_app[1].messages[sid].append({"id": 3, "role": "assistant", "content": "Recovered"})
    page.evaluate("window.dispatchEvent(new Event('online'))")
    expect(page.get_by_text("Recovered", exact=True)).to_be_attached(timeout=8000)
    expect(page.get_by_text("Hermes is temporarily unavailable", exact=True)).to_have_count(0)
    expect(composer).to_have_value("Keep my work")


def test_large_external_append_preserves_the_original_loaded_boundary(page, live_app):
    sid = seed(live_app[1], "large-append", count=180)
    open_session(page, sid)
    # More than one default page arrives while this client is away.
    live_app[1].messages[sid].extend(
        {"id": i, "role": "user", "content": "Repeated legitimate message"} for i in range(181, 431)
    )
    refresh(page, sid)
    assert history_ids(page) == list(range(81, 431))
    page.evaluate("import('/static/store.js').then(s => s.loadOlderMessages())")
    assert history_ids(page) == list(range(1, 431))


def test_focus_preserves_loaded_sidebar_pages(page, live_app):
    for i in range(115):
        seed(live_app[1], f"listed-{i}")
    page.evaluate("""async () => {
        const s = await import('/static/store.js');
        await s.refreshSessions(); await s.refreshSessions(true);
    }""")
    expect(page.locator(".session-select")).to_have_count(115)
    live_app[1].sessions["listed-0"]["title"] = "Updated in another client"
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(page.get_by_role("link", name="Updated in another client", exact=True)).to_be_attached()
    expect(page.locator(".session-select")).to_have_count(115)
