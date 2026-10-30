"""Native commands are recorded by Talaria's server: every device and reload shows the same."""

import json
from contextlib import contextmanager

import pytest
from playwright.sync_api import expect

from talaria.app import STATE_UNAVAILABLE
from talaria.session_state import StateUnavailable

from .conftest import browser_context, wait_for, wait_for_store, wait_in_browser
from .test_app import signed_in
from .test_conversation_features import png
from .test_reply_settlement import start_reply

CATALOG = [
    {
        "name": "compress",
        "aliases": ["compact"],
        "description": "Compress session context",
        "mode": "native",
        "reason": "",
    },
    {
        "name": "version",
        "aliases": [],
        "description": "Hermes version",
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
        "name": "stop",
        "aliases": [],
        "description": "Kill all running background processes",
        "mode": "unavailable",
        "reason": "Use this command in Hermes's terminal.",
    },
    {
        "name": "steer",
        "aliases": ["s"],
        "description": "Inject a message after the next tool call",
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


def turns(count, start=1, words="A paragraph of session history. "):
    return [
        {
            "id": start + i,
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"Message {start + i}. " + words,
            "timestamp": 1000.0 + start + i,
        }
        for i in range(count)
    ]


def prepare(page, live_app, rows=None):
    url, peer, _ = live_app
    with signed_in(url) as client:
        sid = client.post("/api/sessions", json={"title": "Command session"}).json()["id"]
    peer.extension = {"commands": True}
    peer.command_catalog = CATALOG
    if rows is not None:
        peer.messages[sid] = rows
    open_session(page, sid, connect=True)
    return sid


def open_session(page, sid, connect=False):
    page.evaluate(
        "async ([sid, connect]) => { const s = await import('/static/store.js'); "
        "if (connect) await s.connect(); await s.openSession(sid); }",
        [sid, connect],
    )
    wait_for_store(page, f"s => s.active === {json.dumps(sid)} && !s.loading")


@contextmanager
def other_device(browser, live_app, clock=False):
    with browser_context(browser) as context:
        if clock:
            # Time still passes; the test can skip ahead to the next revalidation.
            context.clock.install()
        tab = context.new_page()
        tab.goto(live_app[0])
        tab.get_by_label("Password", exact=True).fill("test-password")
        tab.get_by_role("button", name="Sign in").click()
        tab.locator(".topbar-title").wait_for()
        wait_for_store(tab, "state => !!state.defaultModel && state.readiness.status === 'ok'")
        yield tab


def lose_answer(route):
    """Talaria and Hermes accept a command; a proxy loses only the answer."""
    if route.request.method == "POST":
        route.fetch()
        route.fulfill(status=502, body="Bad gateway")
    else:
        route.continue_()


def run_command(page, text):
    page.get_by_role("textbox", name="Message Hermes").fill(text)
    page.get_by_role("button", name="Send message", exact=True).click()


def layout(page):
    return page.evaluate(
        """() => [...document.querySelector('.conversation-content').children]
          .filter(c => c.matches('.message,.command-card'))
          .map(c => c.matches('.notice') ? 'compacted' : c.matches('.command-card') ? 'card'
            : c.querySelector('.user-content,.message-text')?.innerText.trim().split('.')[0])"""
    )


@pytest.mark.parametrize("mobile", [False, True])
def test_compress_feedback_follows_a_settled_reply_without_saved_history(page, live_app, mobile):
    if mobile:
        page.set_viewport_size({"width": 390, "height": 844})
    prepare(page, live_app)
    peer = live_app[1]
    text = "\n\n".join(f"Work item {i}. " + "Completed sysadmin work. " * 8 for i in range(35))
    start_reply(page, live_app, text)
    # A terminal reply remains in the live cache if its native history cannot
    # yet be reconciled. Commands must appear after that reply, just as they do
    # after a reply already adopted into history.
    peer.messages["settling"] = []
    page.evaluate(
        """output => settlingSource.onmessage({data:JSON.stringify({
          event:'run.failed', output, completed:false, partial:false,
          turn_exit_reason:'max_iterations_reached(500/500)'
        })})""",
        text,
    )
    wait_for_store(page, "s => s.lives.settling.status === 'failed'")
    run_command(page, "/compress --preview")
    card = page.get_by_role("region", name="/compress command")
    expect(card).to_contain_text("Compressing context")
    expect(card).to_be_in_viewport()
    peer.finish_command(session_id="settling", text="Compression preview complete")
    expect(card).to_contain_text("Compression preview complete")
    expect(card).to_be_in_viewport()
    # Once a new run begins, the old command stays above that new turn.
    page.evaluate("() => {window.EventSource = class {close() {}};}")
    run_command(page, "Next work item")
    expect(page.locator(".message.user").last).to_contain_text("Next work item")
    assert card.evaluate(
        """el => {
          const next = Array.from(document.querySelectorAll('.message.user')).at(-1);
          return !!(el.compareDocumentPosition(next) & Node.DOCUMENT_POSITION_FOLLOWING);
        }"""
    )


def test_command_picker_alias_and_result_do_not_become_messages(page, live_app):
    sid = prepare(page, live_app)
    peer = live_app[1]
    composer = page.get_by_role("textbox", name="Message Hermes")
    composer.fill("/comp")
    expect(page.get_by_role("option")).to_have_count(1)
    composer.press("Tab")
    expect(composer).to_have_value("/compress ")
    composer.fill("/compact --preview")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.locator(".command-card")).to_contain_text("Compressing context")
    peer.finish_command(session_id=sid, text="Compressed context successfully.")
    expect(
        page.get_by_role("status").filter(has_text="Compressed context successfully.")
    ).to_be_visible()
    expect(page.locator(".conversation-content .command-card")).to_be_visible()
    expect(page.locator(".composer .command-card")).to_have_count(0)
    [job] = peer.commands.values()
    assert job["payload"]["command"] == "compress"
    assert job["payload"]["args"] == "--preview" and job["payload"]["session_id"] == sid
    assert not peer.runs and not peer.messages[sid]
    expect(composer).to_have_value("")
    composer.fill("/clear ")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_text("Use this command in Hermes's terminal.", exact=True)).to_be_visible()
    assert len(peer.commands) == 1


def test_commands_talaria_cannot_run_stay_out_of_the_picker_and_help(page, live_app):
    prepare(page, live_app)
    composer = page.get_by_role("textbox", name="Message Hermes")
    composer.fill("/")
    options = page.get_by_role("option")
    expect(options.filter(has_text="/compress")).to_have_count(1)
    expect(options.filter(has_text="/steer")).to_contain_text("<prompt>")
    expect(options.filter(has_text="/stop")).to_contain_text("Stop the current response")
    expect(options.filter(has_text="/clear")).to_have_count(0)
    composer.fill("/cl")
    expect(options).to_have_count(0)
    live_app[1].command_catalog = [*CATALOG, {**CATALOG[0], "name": "help", "mode": "web"}]
    page.reload()
    composer.fill("/help")
    page.get_by_role("button", name="Send message", exact=True).click()
    output = page.locator(".command-output")
    expect(output).to_contain_text("/stop — Stop the current response")
    expect(output).not_to_contain_text("/clear")


def test_stop_and_steer_act_on_the_running_response(page, live_app):
    prepare(page, live_app)
    peer = live_app[1]
    composer = page.get_by_role("textbox", name="Message Hermes")

    def run_command(text):
        composer.fill(text)
        page.locator(".send-button").click()

    for text, error in (
        ("/stop", "No response is running."),
        ("/steer Use tabs", "No response is running."),
    ):
        run_command(text)
        expect(page.get_by_text(error, exact=True)).to_be_visible()
    assert not peer.runs

    composer.fill("Start working")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_role("button", name="Stop response", exact=True)).to_be_visible()
    [run] = peer.runs.values()

    run_command("/steer")
    expect(page.get_by_text("Type your guidance after /steer.", exact=True)).to_be_visible()
    run_command("/stop now")
    expect(page.get_by_text("Use /stop without arguments.", exact=True)).to_be_visible()
    assert "steer" not in run and not peer.stops

    run_command("/s Use tabs")
    expect(composer).to_have_value("")
    wait_for(lambda: run.get("steer"), lambda steer: steer == "Use tabs")
    assert len(peer.runs) == 1

    run_command("/stop")
    expect(composer).to_have_value("")
    wait_for(lambda: peer.stops, lambda stops: stops == 1)


def test_lost_admission_response_and_reload_never_post_twice(page, live_app):
    sid = prepare(page, live_app, turns(4))
    peer = live_app[1]

    page.route("**/api/commands", lose_answer)
    with page.expect_response(lambda r: r.url.endswith("/api/commands") and r.status == 502):
        run_command(page, "/compress ")
    expect(page.locator(".command-card")).to_contain_text("Compressing context…")
    page.unroute("**/api/commands")
    page.reload()
    expect(page.get_by_role("textbox", name="Message Hermes")).to_have_value("")
    expect(page.locator(".command-card")).to_contain_text("Compressing context…")
    peer.finish_command(session_id=sid, changed=True, text="Recovered command result")
    notice = page.locator(".message.notice")
    expect(notice).to_contain_text("Context compressed", timeout=10000)
    expect(page.locator(".command-card")).to_have_count(0)
    assert len(peer.commands) == 1
    assert not peer.runs


def test_unconfirmed_command_start_is_followed_and_never_offered_again(page, live_app):
    sid = prepare(page, live_app, turns(4))
    peer = live_app[1]
    checks = []

    def unreachable_once(route):
        if checks:
            route.continue_()
        else:
            checks.append(route.request.url)
            route.abort()

    page.route("**/api/commands", lose_answer)
    page.route("**/api/commands/*", unreachable_once)
    with page.expect_response(lambda r: r.url.endswith("/api/commands") and r.status == 502):
        run_command(page, "/compress ")
    # Whether it started is not yet known. It is followed by ID, and the
    # message box never offers it to run a second time.
    expect(page.get_by_role("textbox", name="Message Hermes")).to_have_value("")
    expect(page.locator(".command-card")).to_have_count(1)
    peer.finish_command(session_id=sid, changed=True, text="Recovered command result")
    expect(page.locator(".message.notice")).to_contain_text("Context compressed", timeout=10000)
    expect(page.locator(".command-card")).to_have_count(0)
    assert len(peer.commands) == 1
    assert checks


def test_compaction_is_shown_on_every_device_after_reloads_and_hours_away(page, live_app, browser):
    """The reported case: compact, walk away, return on another laptop."""
    sid = prepare(page, live_app, turns(4))
    peer = live_app[1]
    with other_device(browser, live_app, clock=True) as laptop:
        open_session(laptop, sid)
        run_command(page, "/compact ")
        wait_for(lambda: peer.commands, bool)
        # The other laptop, open and untouched, sees the compaction running and
        # cannot send meanwhile.
        laptop.clock.fast_forward(16000)
        expect(laptop.locator(".command-card")).to_contain_text("Compressing context…")
        laptop.get_by_role("textbox", name="Message Hermes").fill("Sent too early")
        expect(laptop.get_by_role("button", name="Send message", exact=True)).to_be_disabled()
        # Every browser closes before Hermes finishes.
        page.close()
        peer.finish_command(
            session_id=sid, changed=True, text="Compressed 4 messages.\n~4,000 → ~1,200 tokens."
        )
        notice = laptop.locator(".message.notice")
        expect(notice).to_contain_text("Context compressed", timeout=10000)
        expect(laptop.get_by_role("button", name="Send message", exact=True)).to_be_enabled()
        assert layout(laptop) == ["Message 1", "Message 2", "Message 3", "Message 4", "compacted"]
        notice.locator("summary").click()
        expect(notice).to_contain_text("~4,000 → ~1,200 tokens.")
        laptop.reload()
        expect(laptop.locator(".message.notice")).to_contain_text("Context compressed")
        # A compaction is history, not a result to dismiss.
        expect(laptop.get_by_role("button", name="Dismiss command result")).to_have_count(0)
    with other_device(browser, live_app) as third:
        open_session(third, sid)
        expect(third.locator(".message.notice")).to_contain_text("Context compressed")
        indicator = third.locator(".context-indicator")
        expect(indicator).to_have_attribute(
            "aria-label", "Context compressed · usage updates after the next reply"
        )
        peer.messages[sid].extend(turns(2, start=5))
        third.evaluate("async sid => (await import('/static/store.js')).refreshHistory(sid)", sid)
        expect(indicator).to_have_attribute("aria-label", "9% context used · last request")
        assert layout(third)[4:] == ["compacted", "Message 5", "Message 6"]


def test_command_results_and_dismissal_are_shared(page, live_app, browser):
    sid = prepare(page, live_app, turns(2))
    peer = live_app[1]
    with other_device(browser, live_app) as laptop:
        open_session(laptop, sid)
        run_command(page, "/version")
        peer.finish_command(session_id=sid, text="Hermes Agent 0.22.5")
        expect(page.locator(".command-card")).to_contain_text("Hermes Agent 0.22.5")
        laptop.reload()
        expect(laptop.locator(".command-card")).to_contain_text("Hermes Agent 0.22.5")
        laptop.get_by_role("button", name="Dismiss command result").click()
        expect(laptop.locator(".command-card")).to_have_count(0)
        page.evaluate("dispatchEvent(new Event('focus'))")
        expect(page.locator(".command-card")).to_have_count(0)


@pytest.mark.parametrize("refused_by", ["hermes", "talaria"])
def test_refused_command_explains_itself_and_keeps_the_draft(
    page, live_app, monkeypatch, refused_by
):
    prepare(page, live_app, turns(2))
    _, peer, app = live_app
    if refused_by == "hermes":
        peer.command_refusal = reason = "This session is busy. Wait for the response to finish."
    else:
        reason = STATE_UNAVAILABLE

        def unavailable():
            raise StateUnavailable("disk I/O error")

        monkeypatch.setattr(app.state.session_state, "connect", unavailable)
    composer = page.get_by_role("textbox", name="Message Hermes")
    run_command(page, "/compress ")
    expect(page.get_by_text(reason, exact=True)).to_be_visible()
    expect(composer).to_have_value("/compress ")
    expect(page.locator(".command-card")).to_have_count(0)
    expect(page.get_by_role("button", name="Send message", exact=True)).to_be_enabled()
    page.reload()
    expect(composer).to_have_value("/compress")
    assert not peer.commands


def test_command_that_never_reached_talaria_returns_to_the_message_box(page, live_app):
    prepare(page, live_app, turns(2))
    peer = live_app[1]
    page.route(
        "**/api/commands",
        lambda route: route.abort() if route.request.method == "POST" else route.continue_(),
    )
    run_command(page, "/compress ")
    expect(
        page.get_by_text("This command did not start. It is back in the message box.", exact=True)
    ).to_be_visible(timeout=10000)
    expect(page.get_by_role("textbox", name="Message Hermes")).to_have_value("/compress")
    expect(page.locator(".command-card")).to_have_count(0)
    assert not peer.commands


def test_command_whose_result_hermes_lost_says_so_and_frees_the_session(page, live_app):
    sid = prepare(page, live_app, turns(2))
    peer = live_app[1]
    run_command(page, "/compress ")
    card = page.locator(".command-card")
    expect(card).to_contain_text("Compressing context…")
    wait_for(lambda: peer.commands, bool)
    peer.commands.clear()  # Hermes restarted; the plugin kept the job only in memory.
    expect(card).to_contain_text("Command result unknown", timeout=10000)
    expect(card).to_contain_text("Hermes did not report this command's result.")
    expect(page.locator(".message.notice")).to_have_count(0)
    page.get_by_role("textbox", name="Message Hermes").fill("Carry on")
    expect(page.get_by_role("button", name="Send message", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Dismiss command result").click()
    expect(card).to_have_count(0)
    page.reload()
    wait_for_store(page, f"s => s.commands.session === {json.dumps(sid)}")
    expect(card).to_have_count(0)


@pytest.mark.parametrize("arrives", ["while saving", "after saving"])
def test_a_dismissed_result_stays_dismissed_when_an_older_list_arrives(page, live_app, arrives):
    sid = prepare(page, live_app, turns(2))
    peer = live_app[1]
    run_command(page, "/version")
    peer.finish_command(session_id=sid, text="Hermes Agent 0.22.5")
    card = page.locator(".command-card")
    expect(card).to_contain_text("Hermes Agent 0.22.5")
    lists, dismissals = [], []

    def hold_first_list(route):
        if lists:
            route.continue_()
        else:
            # Read now, before the dismissal; delivered later.
            lists.append((route, route.fetch()))

    def held(routes):
        return wait_in_browser(page, lambda: routes)[0]

    page.route(f"**/api/sessions/{sid}/commands*", hold_first_list)
    page.evaluate("() => { import('/static/store.js').then(s => s.refreshCommands()); }")
    route, response = held(lists)
    dismiss = page.get_by_role("button", name="Dismiss command result")
    if arrives == "while saving":
        page.route("**/api/commands/*", lambda request: dismissals.append(request))
        dismiss.click()
        saving = held(dismissals)
        route.fulfill(response=response)
        page.wait_for_timeout(300)
        shown = card.count()
        saving.continue_()
        assert shown == 0
    else:
        with page.expect_response(lambda r: r.request.method == "DELETE"):
            dismiss.click()
        route.fulfill(response=response)
    page.wait_for_timeout(300)
    expect(card).to_have_count(0)


def test_a_result_from_unloaded_history_waits_for_that_history(page, live_app):
    sid = prepare(page, live_app, turns(2))
    peer = live_app[1]
    run_command(page, "/version")
    peer.finish_command(session_id=sid, text="Hermes Agent 0.22.5")
    expect(page.locator(".command-card")).to_contain_text("Hermes Agent 0.22.5")
    peer.messages[sid].extend(turns(150, start=3))
    page.reload()
    expect(page.get_by_text("Message 152.", exact=False)).to_be_visible()
    # It ran after Message 2, which is not loaded: the top of this page is no place for it.
    expect(page.locator(".command-card")).to_have_count(0)
    page.get_by_role("button", name="Load earlier messages").click()
    expect(page.locator(".command-card")).to_have_count(1)
    assert layout(page)[:3] == ["Message 1", "Message 2", "card"]


def test_command_proxy_validates_body_and_keeps_authentication(live_app):
    url, peer, _ = live_app
    peer.extension = {"commands": True}
    peer.command_catalog = CATALOG
    with signed_in(url) as client:
        assert client.get("/api/commands").json() == {"commands": CATALOG}
        assert client.post("/api/commands", json={"args": "x" * 2001}).status_code == 400
        assert client.post("/api/commands", content=json.dumps({"command": []})).status_code == 400
        short = {"request_id": "short", "command": "version", "session_id": "", "args": ""}
        assert client.post("/api/commands", json=short).status_code == 400
        assert client.get("/api/commands/not/valid").status_code == 404
    assert not peer.commands


def test_command_outside_a_session_shows_its_result(page, live_app):
    peer = live_app[1]
    peer.extension = {"commands": True}
    peer.command_catalog = CATALOG
    page.evaluate("async () => (await import('/static/store.js')).connect()")
    run_command(page, "/version")
    card = page.locator(".command-card")
    expect(card).to_contain_text("Running command…")
    peer.finish_command(session_id="", text="Hermes Agent 0.22.5")
    expect(card).to_contain_text("Hermes Agent 0.22.5")
    page.get_by_role("button", name="Dismiss command result").click()
    expect(card).to_have_count(0)


def test_a_command_listed_before_hermes_answers_shows_one_card(module_page):
    result = module_page.evaluate("""async () => {
        const {html, render} = await import('/static/lib.js');
        const store = await import('/static/store.js');
        const {useCommandActivity} = await import('/static/command-activity.js');
        const fetch = window.fetch;
        let answer, seen;
        window.fetch = (url, options) => {
            const path = new URL(url, location.href).pathname;
            // Hermes is slow to answer this start.
            if (path === '/api/commands' && options?.method === 'POST')
                return new Promise(resolve => { answer = resolve; });
            if (path === '/api/sessions/listed/commands')
                return Promise.resolve(Response.json({data: [], server_time: Date.now() / 1000}));
            return fetch(url, options);
        };
        const host = document.createElement('div');
        document.body.append(host);
        const previous = {active: store.state.active, auth: store.state.auth,
            caps: store.state.caps, commands: store.state.commands};
        function Probe() {
            seen = useCommandActivity(store.state);
            return null;
        }
        const draw = () => new Promise(resolve => {
            render(html`<${Probe} />`, host);
            setTimeout(resolve, 20);
        });
        try {
            store.update({active: 'listed', auth: true,
                commands: {session: 'listed', records: [], clock: 0},
                caps: {...store.state.caps, talaria_extensions: {commands: true}}});
            await draw();
            const started = seen.start('version', '', 'listed', '/version');
            await draw();
            const id = seen.activities[0].id;
            // Talaria lists the command before this tab hears back.
            const record = {id, session_id: 'listed', command: 'version', args: '',
                status: 'running', changed: false, text: '', anchor: null,
                started_at: Date.now() / 1000, finished_at: null};
            store.update({commands: {session: 'listed', records: [record], clock: 0}});
            await draw();
            const shown = seen.activities.map(activity => [activity.id === id, activity.status]);
            answer(Response.json(record, {status: 202}));
            await started.catch(() => {});
            return shown;
        } finally {
            render(null, host);
            host.remove();
            window.fetch = fetch;
            store.update(previous);
        }
    }""")
    assert result == [[True, "running"]]


def test_command_runs_after_its_catalog_failed_to_load(page, live_app):
    sid = prepare(page, live_app, turns(2))
    peer = live_app[1]
    failures = []

    def fail_once(route):
        if route.request.method == "GET" and not failures:
            failures.append(route.request.url)
            route.fulfill(status=502, body="Bad gateway")
        else:
            route.continue_()

    page.route("**/api/commands", fail_once)
    with page.expect_response(lambda r: r.url.endswith("/api/commands") and r.status == 502):
        page.reload()
    page.get_by_role("textbox", name="Message Hermes").wait_for()
    # Typing "/" asks again, and sending never waits on the picker.
    run_command(page, "/version")
    expect(page.locator(".command-card")).to_contain_text("Running command…")
    peer.finish_command(session_id=sid, text="Hermes Agent 0.22.5")
    expect(page.locator(".command-card")).to_contain_text("Hermes Agent 0.22.5")


def test_command_completion_does_not_navigate_away_from_another_session(page, live_app):
    sid = prepare(page, live_app, turns(2))
    peer = live_app[1]
    with signed_in(live_app[0]) as client:
        other = client.post("/api/sessions", json={"title": "Other session"}).json()["id"]
    run_command(page, "/compress --preview")
    expect(page.locator(".command-card")).to_contain_text("Compressing context…")
    open_session(page, other)
    peer.finish_command(session_id=sid, text="Done")
    with signed_in(live_app[0]) as client:
        wait_for(
            lambda: client.get(f"/api/sessions/{sid}/commands").json()["data"],
            lambda records: records[0]["status"] == "completed",
        )
    assert page.evaluate("async () => (await import('/static/store.js')).state.active") == other
    expect(page.locator(".command-card")).to_have_count(0)
    open_session(page, sid)
    expect(page.locator(".command-card")).to_contain_text("Done")


def test_command_card_live_states_and_mobile_layout(page, live_app):
    from pathlib import Path

    sid = prepare(page, live_app, turns(2))
    peer = live_app[1]
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(reduced_motion="reduce")
    page.evaluate("document.documentElement.dataset.theme = 'dark'")
    admissions = []
    page.route(
        "**/api/commands",
        lambda route: (
            admissions.append(route) if route.request.method == "POST" else route.continue_()
        ),
    )
    run_command(page, "/compress ")
    card = page.locator(".conversation-content .command-card")
    expect(card).to_contain_text("Compressing context…")
    node = card.element_handle()
    # Visible immediately, even while admission itself is still waiting on the network.
    assert admissions
    admissions[0].continue_()
    expect(card.locator(".command-elapsed")).not_to_have_text("0s")
    assert card.locator(".spinner").evaluate("el => getComputedStyle(el).animationName") == "none"
    assert card.evaluate("el => el.scrollWidth <= el.clientWidth")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    Path("test-results").mkdir(exist_ok=True)
    page.screenshot(path="test-results/command-mobile-running.png", animations="disabled")
    page.route(
        f"**/api/sessions/{sid}/commands*",
        lambda route: route.fulfill(status=503, json={"error": "Temporarily unavailable"}),
    )
    expect(card).to_contain_text("Reconnecting to Hermes…", timeout=10000)
    assert node.evaluate("el => el.isConnected && el === document.querySelector('.command-card')")
    rid = next(iter(peer.commands))
    peer.commands[rid]["result"] = {"status": "failed", "error": "Summary provider is unavailable."}
    page.unroute(f"**/api/sessions/{sid}/commands*")
    expect(card).to_contain_text("Command failed", timeout=10000)
    expect(card).to_contain_text("Summary provider is unavailable.")
    expect(card.locator(".spinner")).to_have_count(0)
    assert node.evaluate("el => el.isConnected && el === document.querySelector('.command-card')")
    page.screenshot(path="test-results/command-mobile-failed.png", animations="disabled")
    page.get_by_role("button", name="Dismiss command result").click()
    expect(card).to_have_count(0)
    assert len(admissions) == 1 and len(peer.commands) == 1 and not peer.runs


def test_command_updates_respect_scroll_and_keep_their_place_in_chat(page, live_app):
    from pathlib import Path

    rows = turns(24, words="A paragraph of session history. " * 10)
    sid = prepare(page, live_app, rows)
    peer = live_app[1]
    run_command(page, "/compress --preview")
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
    peer.finish_command(
        session_id=sid,
        text="Context would shrink from 42,000 to 12,000 tokens.\nRecent exchanges preserved.",
    )
    expect(card).to_contain_text("Recent exchanges preserved.")
    assert abs(viewport.evaluate("el => el.scrollTop") - 100) < 2
    assert node.evaluate("el => el.isConnected && el === document.querySelector('.command-card')")
    peer.messages[sid].extend(turns(2, start=25))
    page.evaluate("async sid => (await import('/static/store.js')).refreshHistory(sid)", sid)
    expect(page.get_by_text("Message 26.", exact=False)).to_be_visible()
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
    sid = prepare(page, live_app, turns(2))
    peer = live_app[1]
    with signed_in(live_app[0]) as client:
        child = client.post("/api/sessions", json={"title": "Child session"}).json()["id"]
    run_command(page, "/compress --preview")
    expect(page.locator(".command-card")).to_contain_text("Compressing context…")
    page.evaluate(
        "async ids => (await import('/static/store.js')).openSession(ids.child, {id: ids.sid})",
        {"child": child, "sid": sid},
    )
    expect(page.locator(".composer")).to_have_count(0)
    peer.finish_command(session_id=sid, text="Finished in background")
    assert page.evaluate("async () => (await import('/static/store.js')).state.active") == child
    page.get_by_role("button", name="Back to parent session").click()
    expect(page.locator(".command-card")).to_contain_text("Finished in background")


def test_compression_refresh_keeps_chat_visible_until_new_history_arrives(page, live_app):
    rows = [
        {"id": 1, "role": "user", "content": "Original question", "timestamp": 1001.0},
        {
            "id": 2,
            "role": "assistant",
            "content": "Original reply remains visible",
            "timestamp": 1002.0,
        },
    ]
    sid = prepare(page, live_app, rows)
    peer = live_app[1]
    run_command(page, "/compress ")
    expect(page.locator(".command-card")).to_contain_text("Compressing context…")
    page.wait_for_function("() => document.querySelector('.command-elapsed')?.textContent !== '0s'")
    history_requests = []
    page.route(f"**/api/sessions/{sid}/messages*", lambda route: history_requests.append(route))
    page.get_by_role("textbox", name="Message Hermes").fill("Draft during history refresh")
    peer.finish_command(session_id=sid, changed=True, text="Earlier context summarized.")
    page.wait_for_function("() => document.querySelector('.message.notice')")
    expect(page.get_by_text("Original reply remains visible", exact=True)).to_be_visible()
    expect(page.locator(".history-loading")).to_have_count(0)
    wait_in_browser(page, lambda: history_requests)
    history_requests[-1].fulfill(
        json={
            "session_id": sid,
            "data": [
                {"id": 3, "role": "user", "content": "Original question", "timestamp": 1001.0},
                {"id": 4, "role": "assistant", "content": "Saved continuation", "timestamp": 1002},
            ],
        }
    )
    expect(page.locator(".message.notice")).to_contain_text("Context compressed")
    expect(page.get_by_text("Saved continuation", exact=True)).to_be_visible()
    expect(page.get_by_role("textbox", name="Message Hermes")).to_have_value(
        "Draft during history refresh"
    )
    assert not peer.runs


def test_in_place_compaction_keeps_the_result_below_the_latest_turn(page, live_app):
    sid = prepare(page, live_app, turns(4))
    peer = live_app[1]
    run_command(page, "/compress ")
    expect(page.locator(".command-card")).to_contain_text("Compressing context…")
    # Hermes archives every row and re-inserts the ones it keeps with new IDs,
    # but their original timestamps, in their original display places; the
    # hidden summary row closes the list.
    first, second, third, fourth = turns(4)
    peer.messages[sid] = [
        {**first, "id": 5},
        second,
        {**third, "id": 7},
        {**fourth, "id": 8},
        {
            "id": 6,
            "role": "assistant",
            "content": "[CONTEXT COMPACTION]",
            "display_kind": "hidden",
            "timestamp": 1010.0,
        },
    ]
    peer.finish_command(session_id=sid, changed=True, text="Compressed: 4 → 3 messages")
    expect(page.locator(".message.notice")).to_contain_text("Context compressed")
    assert layout(page) == ["Message 1", "Message 2", "Message 3", "Message 4", "compacted"]
    peer.messages[sid].extend(turns(2, start=9))
    page.evaluate("async sid => (await import('/static/store.js')).refreshHistory(sid)", sid)
    expect(page.get_by_text("Message 10.", exact=False)).to_be_visible()
    assert layout(page)[3:] == ["Message 4", "compacted", "Message 9", "Message 10"]


def test_compaction_continuation_preserves_unsent_draft_and_choices(page, live_app):
    sid = prepare(page, live_app, turns(2))
    peer = live_app[1]
    page.evaluate(
        "async sid => (await import('/static/store.js')).chooseReasoning('high', sid)", sid
    )
    with signed_in(live_app[0]) as client:
        continued = client.post("/api/sessions", json={"title": "Continued session"}).json()["id"]
    peer.sessions[continued]["parent_session_id"] = sid
    composer = page.get_by_role("textbox", name="Message Hermes")
    run_command(page, "/compress ")
    expect(composer).to_have_value("")
    expect(page.locator(".command-card")).to_contain_text("Compressing context")
    draft = "Keep my unsent draft.\nIncluding this second line."
    composer.fill(draft)
    page.get_by_label("File attachment").set_input_files(
        {"name": "draft.png", "mimeType": "image/png", "buffer": png()}
    )
    attachment = page.get_by_role("button", name="Remove draft.png", exact=True)
    expect(attachment).to_be_visible()
    page.evaluate("window.compactionPageMarker = true")
    peer.messages[continued] = [{**row, "session_id": sid} for row in turns(2)]
    page.route(
        f"**/api/sessions/{sid}/messages*",
        lambda route: route.fulfill(
            json={"session_id": continued, "data": peer.messages[continued]}
        ),
    )
    peer.finish_command(session_id=continued, changed=True, text="Context compressed")
    expect(page.locator(".message.notice")).to_contain_text("Context compressed")
    wait_for_store(page, f"s => s.active === {json.dumps(continued)}")
    expect(composer).to_have_value(draft)
    expect(attachment).to_be_visible()
    expect(page.get_by_role("button", name="Choose reasoning", exact=True)).to_contain_text("High")
    assert page.evaluate("window.compactionPageMarker") is True
    page.unroute(f"**/api/sessions/{sid}/messages*")
    page.reload()
    expect(composer).to_have_value(draft)
    expect(attachment).to_be_visible()
    expect(page.locator(".message.notice")).to_contain_text("Context compressed")
    expect(page.get_by_role("button", name="Choose reasoning", exact=True)).to_contain_text("High")
    assert not peer.runs


def test_command_acknowledgement_preserves_draft_after_leaving_child_view(page, live_app):
    sid = prepare(page, live_app, turns(2))
    peer = live_app[1]
    with signed_in(live_app[0]) as client:
        child = client.post("/api/sessions", json={"title": "Read-only child"}).json()["id"]
    admissions = []
    page.route(
        "**/api/commands",
        lambda route: (
            admissions.append(route) if route.request.method == "POST" else route.continue_()
        ),
    )
    composer = page.get_by_role("textbox", name="Message Hermes")
    run_command(page, "/compress ")
    expect(page.locator(".command-card")).to_contain_text("Compressing context")
    page.evaluate(
        "async ids => (await import('/static/store.js')).openSession(ids.child, {id: ids.sid})",
        {"child": child, "sid": sid},
    )
    expect(page.locator(".composer")).to_have_count(0)
    page.get_by_role("button", name="Back to parent session").click()
    composer.fill("New draft written before command acknowledgement")
    wait_in_browser(page, lambda: admissions)
    assert len(admissions) == 1
    admissions[0].continue_()
    peer.finish_command(session_id=sid, changed=True, text="Context compressed")
    expect(page.locator(".message.notice")).to_contain_text("Context compressed")
    expect(composer).to_have_value("New draft written before command acknowledgement")
    page.reload()
    expect(composer).to_have_value("New draft written before command acknowledgement")
