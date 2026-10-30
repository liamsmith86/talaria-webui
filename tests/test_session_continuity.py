"""Tab navigation and refresh through the real UI and a synthetic Hermes server."""

import re
from urllib.parse import urljoin

import pytest
from playwright.sync_api import expect

from .conftest import browser_context, wait_for_store, wait_in_browser
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
    # A middle or modified click is the browser's to open in a new tab. Whether a
    # headless browser does so is its own timing, so check that the page leaves
    # these clicks alone, then open the link's address as that tab would.
    page.evaluate("""() => {
        window.prevented = [];
        for (const type of ["click", "auxclick"])
            window.addEventListener(type, (event) => window.prevented.push(event.defaultPrevented));
    }""")
    link.click(button="middle")
    link.click(modifiers=["ControlOrMeta"])
    assert page.evaluate("window.prevented") == [False, False]
    wait_for_store(page, "s => s.active === null")
    other = page.context.new_page()
    try:
        other.goto(urljoin(page.url, link.get_attribute("href")))
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


@pytest.mark.clock
def test_open_session_follows_other_devices_without_focus(page, live_app):
    peer = live_app[1]
    sid = seed(peer, "watched", count=2)
    open_session(page, sid)
    expect(page.get_by_text("Message 001", exact=True)).to_be_visible()
    finished = []
    page.on("requestfinished", lambda request: finished.append(request.url))

    def revalidate():
        # Skip ahead to the next check and let its reads finish. A check
        # schedules the next one only after its own reads, so skip again
        # until one runs.
        del finished[:]
        for _ in range(10):
            page.clock.fast_forward(15001)
            for _ in range(20):
                if any(url.endswith(f"/api/sessions/{sid}") for url in finished) and any(
                    "/api/sessions?" in url for url in finished
                ):
                    return
                page.wait_for_timeout(50)
        raise AssertionError(finished)

    # Nothing changed: the check never reloads the transcript.
    revalidate()
    page.clock.run_for(1000)
    assert not [url for url in finished if "/messages" in url]
    peer.messages[sid].append(
        {"id": 3, "role": "assistant", "content": "Answered on another laptop", "timestamp": 5.0}
    )
    peer.sessions[sid]["title"] = "Renamed on another laptop"
    # An untouched, visible page shows it at its next check.
    revalidate()
    expect(page.get_by_text("Answered on another laptop", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Renamed on another laptop", exact=True)).to_be_visible()


def test_refresh_compaction_uses_canonical_session_and_carries_draft(page, live_app):
    sid = seed(live_app[1], "before-compaction", count=2)
    open_session(page, sid)
    page.get_by_label("Message Hermes").fill("Draft survives external compaction")
    canonical = seed(live_app[1], "after-compaction", count=3)
    live_app[1].sessions[canonical]["parent_session_id"] = sid
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    page.evaluate("""async () => {
        const s = await import('/static/store.js');
        window.loadingTransitions = [];
        window.stopTrackingHistory = s.beforeHistoryUpdate(() =>
            window.loadingTransitions.push(s.state.loading));
    }""")
    refresh(page, sid)
    assert page.evaluate("window.loadingTransitions") == [False]
    page.evaluate("window.stopTrackingHistory()")
    assert len([url for url in requests if "/messages" in url]) == 1
    wait_for_store(page, "s => s.active === 'after-compaction'")
    expect(page).to_have_url(re.compile("session=after-compaction"))
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
    wait_in_browser(page, lambda: routes)
    assert len(routes) == 1
    open_session(page, b)
    for route in routes:
        route.continue_()
    page.unroute("**/api/sessions/slow-old/messages?*")
    page.wait_for_timeout(200)
    wait_for_store(page, "s => s.active === 'fast-new'")
    assert history_ids(page) == [1, 2, 3]
    expect(page).to_have_url(re.compile("session=fast-new"))


def test_refresh_retries_shifted_offsets_without_duplicates(module_page):
    result = module_page.evaluate("""async () => {
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


@pytest.mark.clock
def test_return_during_cooldown_gets_one_delayed_refresh(page, live_app):
    sid = seed(live_app[1], "cooldown", count=2)
    open_session(page, sid)
    calls = []
    page.on("request", lambda req: calls.append(req.url))
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    page.clock.run_for(150)
    wait_for_store(page, "s => s.sessionDetails?.id === 'cooldown'")
    # Wait for the complete batch, then return again inside the cooldown.
    wait_in_browser(page, lambda: any("/cooldown/messages" in url for url in calls))
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


def send_with_saved_steps(page, sid, text="Check the build slowly with saved steps"):
    open_session(page, sid)
    page.get_by_label("Message Hermes").fill(text)
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.locator(".message.assistant .message-text")).to_be_visible()


def expect_one_turn(page):
    expect(page.locator(".message.user")).to_have_count(2)
    expect(page.locator(".message.assistant")).to_have_count(1)
    expect(page.locator(".tool-card")).to_have_count(1)


def test_reloading_mid_reply_shows_its_saved_steps_once(page, live_app):
    send_with_saved_steps(page, seed(live_app[1], "saved-steps"))
    page.reload()
    stop = page.get_by_role("button", name="Stop response", exact=True)
    expect(stop).to_be_visible()
    wait_for_store(page, "s => s.active === 'saved-steps' && !s.loading && s.history.length")
    expect_one_turn(page)
    expect(stop).to_be_hidden(timeout=20000)
    expect_one_turn(page)


def test_returning_to_a_running_session_shows_its_saved_steps_once(page, live_app):
    peer = live_app[1]
    seed(peer, "elsewhere")
    send_with_saved_steps(page, seed(peer, "saved-steps"))
    open_session(page, "elsewhere")
    wait_for_store(page, "s => s.active === 'elsewhere' && !s.loading")
    open_session(page, "saved-steps")
    wait_for_store(page, "s => s.active === 'saved-steps' && !s.loading")
    expect(page.get_by_role("button", name="Stop response", exact=True)).to_be_visible()
    expect_one_turn(page)


def test_another_device_follows_and_approves_a_running_reply(page, live_app, browser):
    peer = live_app[1]
    peer.extension = {"session_activity": True}
    seed(peer, "saved-steps")
    page.reload()
    wait_for_store(page, "s => s.caps.talaria_extensions?.session_activity")
    open_session(page, "saved-steps")
    page.get_by_label("Message Hermes").fill("Check the build slowly with saved steps and approval")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_role("button", name="Allow once")).to_be_visible()
    with browser_context(browser) as context:
        other = context.new_page()
        other.goto(live_app[0])
        other.get_by_label("Password", exact=True).fill("test-password")
        other.get_by_role("button", name="Sign in").click()
        wait_for_store(other, "s => s.readiness.status === 'ok'")
        other.goto(f"{live_app[0]}?session=saved-steps")
        # The other device shows the reply live, once, and can answer its approval.
        expect(other.get_by_role("button", name="Stop response", exact=True)).to_be_visible()
        expect_one_turn(other)
        other.get_by_role("button", name="Allow once").click()
        reply = "What would you like to explore next?"
        expect(other.get_by_text(reply, exact=True)).to_be_visible(timeout=20000)
        expect(other.get_by_role("button", name="Stop response", exact=True)).to_be_hidden()
        expect_one_turn(other)
    expect(page.get_by_text(reply, exact=True)).to_be_visible(timeout=20000)
    expect_one_turn(page)
    assert peer.approvals == 1


def test_another_tab_follows_a_running_reply_when_looked_at(page, live_app):
    peer = live_app[1]
    peer.extension = {"session_activity": True}
    seed(peer, "saved-steps")
    page.reload()
    wait_for_store(page, "s => s.caps.talaria_extensions?.session_activity")
    other = page.context.new_page()
    other.goto(f"{live_app[0]}?session=saved-steps")
    wait_for_store(other, "s => s.active === 'saved-steps' && !s.loading")
    open_session(page, "saved-steps")
    page.get_by_label("Message Hermes").fill("Check the build slowly with saved steps and approval")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_role("button", name="Allow once")).to_be_visible()
    other.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(other.get_by_role("button", name="Stop response", exact=True)).to_be_visible()
    expect_one_turn(other)
    page.get_by_role("button", name="Allow once").click()
    reply = other.get_by_text("What would you like to explore next?", exact=True)
    expect(reply).to_be_visible(timeout=20000)
    expect(other.get_by_role("button", name="Stop response", exact=True)).to_be_hidden()
    expect_one_turn(other)


OPEN_STREAMS = """() => {
  const Native = window.EventSource, open = new Set();
  window.EventSource = class extends Native {
    constructor(url) { super(url); open.add(this); }
    close() { open.delete(this); super.close(); }
  };
  window.openStreams = () => [...open].filter((s) => s.readyState !== 2).map((s) => s.url);
}"""


def test_only_the_session_on_screen_streams_its_reply(page, live_app):
    # A browser allows about six connections per server across all its tabs.
    peer = live_app[1]
    page.evaluate(OPEN_STREAMS)
    for sid in ("parked-first", "parked-second"):
        open_session(page, seed(peer, sid, count=0))
        page.get_by_label("Message Hermes").fill("Check the build slowly")
        page.get_by_role("button", name="Send message", exact=True).click()
        expect(page.locator(".message.assistant .message-text")).to_be_visible()
    runs = page.evaluate("""async () => {
      const {state} = await import('/static/store.js');
      return [state.lives['parked-first'].id, state.lives['parked-second'].id];
    }""")
    streams = page.evaluate("window.openStreams()")
    assert len(streams) == 1 and runs[1] in streams[0]
    # Returning resumes after the events this page already showed.
    open_session(page, "parked-first")
    page.wait_for_function("run => window.openStreams().join().includes(run)", arg=runs[0])
    streams = page.evaluate("window.openStreams()")
    assert len(streams) == 1 and "after=" in streams[0]
    wait_for_store(page, "s => s.lives['parked-first']?.status === 'completed'")
    text = page.evaluate(
        "async () => (await import('/static/store.js')).state.lives['parked-first'].text"
    )
    assert text.count("A thoughtful place to start") == 1
    # The parked reply finishes without being opened.
    finished = page.locator('a.session-select[href*="session=parked-second"] .session-activity')
    expect(finished).to_have_attribute("aria-label", "Finished", timeout=20000)
    page.wait_for_function("() => window.openStreams().length === 0")
    open_session(page, "parked-second")
    expect(page.locator(".message.assistant")).to_have_count(1)
    expect(page.get_by_text("What would you like to explore next?")).to_have_count(1)


def test_a_hidden_page_releases_its_stream_and_resumes_when_shown(page, live_app):
    page.evaluate(OPEN_STREAMS)
    open_session(page, seed(live_app[1], "hidden-page", count=0))
    page.get_by_label("Message Hermes").fill("Check the build slowly")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.locator(".message.assistant .message-text")).to_be_visible()
    shown = """state => {
      Object.defineProperty(document, 'visibilityState', {value: state, configurable: true});
      document.dispatchEvent(new Event('visibilitychange'));
    }"""
    page.evaluate(shown, "hidden")
    page.wait_for_function("() => window.openStreams().length === 0")
    page.evaluate(shown, "visible")
    page.wait_for_function("() => window.openStreams().some((url) => url.includes('after='))")
    expect(page.get_by_role("button", name="Stop response", exact=True)).to_be_hidden(timeout=20000)
    expect(page.get_by_text("What would you like to explore next?")).to_have_count(1)


def test_session_deleted_elsewhere_opens_a_new_session(page, live_app):
    peer = live_app[1]
    sid = seed(peer, "deleted-elsewhere", count=2)
    open_session(page, sid)
    expect(page.get_by_text("Message 001", exact=True)).to_be_visible()
    page.get_by_label("Message Hermes").fill("Unsent words")
    del peer.sessions[sid], peer.messages[sid]
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(page.get_by_text("This session was deleted.", exact=True)).to_be_visible()
    wait_for_store(page, "s => s.active === null")
    expect(page.get_by_text("Message 001", exact=True)).to_have_count(0)
    expect(page.locator(".alert")).to_have_count(0)
    assert "session=deleted-elsewhere" not in page.url
    # Reopening the remembered session never lands on the deleted one either.
    sid = seed(peer, "deleted-while-away", count=2)
    open_session(page, sid)
    expect(page.get_by_text("Message 001", exact=True)).to_be_visible()
    del peer.sessions[sid], peer.messages[sid]
    page.goto(live_app[0])
    expect(page.get_by_text("This session was deleted.", exact=True)).to_be_visible()
    wait_for_store(page, "s => s.active === null && !s.loading")
    expect(page.locator(".alert")).to_have_count(0)
