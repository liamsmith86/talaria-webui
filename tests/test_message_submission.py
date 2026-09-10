"""Regressions for repeated send gestures and tool-only conversation turns."""

import pytest
from playwright.sync_api import expect

from .test_conversation_features import seed


def test_tool_only_messages_do_not_render_a_zero_reply(page, live_app):
    sid = seed(live_app[1], "tool-only")
    live_app[1].messages[sid] = [
        {
            "id": 1,
            "role": "assistant",
            "content": "",
            "reasoning": "Check the configuration",
            "tool_calls": [{
                "id": "call-1",
                "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
            }],
        },
        {"id": 2, "role": "tool", "tool_call_id": "call-1", "content": '{"output":""}'},
        {"id": 3, "role": "assistant", "content": "0"},
    ]
    page.evaluate("async sid => (await import('/static/store.js')).openSession(sid)", sid)
    expect(page.locator(".tool-card")).to_have_count(1)
    tool_message = page.locator(".message.assistant").first
    assert tool_message.evaluate(
        "el => [...el.childNodes].filter(n => n.nodeType === Node.TEXT_NODE)"
        ".map(n => n.textContent.trim()).join('')"
    ) == ""
    # An actual answer of zero is valid content and must remain visible.
    expect(page.locator(".message-text")).to_have_text("0")


@pytest.mark.parametrize("existing", [False, True])
def test_repeated_submit_before_the_composer_repaints_sends_once(page, live_app, existing):
    if existing:
        sid = seed(live_app[1], "existing")
        page.evaluate("async sid => (await import('/static/store.js')).openSession(sid)", sid)
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_label("Message Hermes").fill("A slow response for repeated send gestures")
    expect(page.get_by_role("button", name="Send message", exact=True)).to_be_enabled()
    result = page.evaluate("""async () => {
        const {options} = await import('/static/vendor/preact.js');
        const {state} = await import('/static/store.js');
        const previous = options.debounceRendering;
        const fetch = window.fetch;
        const renders = [];
        const submissions = [];
        options.debounceRendering = callback => renders.push(callback);
        window.fetch = (url, init) => {
            if (init?.method === 'POST' && /\\/runs(?:$|\\/.*\\/steer)/.test(String(url)))
                submissions.push(String(url));
            return fetch(url, init);
        };
        const submit = () => document.querySelector('.composer')
            .dispatchEvent(new Event('submit', {bubbles:true, cancelable:true}));
        try {
            submit();
            submit(); // The same gesture while the request is pending.
            const deadline = Date.now() + 5000;
            while (!state.lives[state.active]?.id && Date.now() < deadline)
                await new Promise(resolve => setTimeout(resolve, 10));
            if (!state.lives[state.active]?.id) throw new Error('Run was not acknowledged');
            await new Promise(resolve => setTimeout(resolve, 0));
            submit(); // A queued gesture after acknowledgement, before the UI repaints.
            return submissions;
        } finally {
            window.fetch = fetch;
            options.debounceRendering = previous;
            for (const render of renders) render();
        }
    }""")
    assert result == ["/api/runs"]
    expect(page.get_by_label("Message Hermes")).to_have_value("")
    assert len(live_app[1].runs) == 1


@pytest.mark.parametrize("repeat_text", [False, True])
def test_history_refresh_keeps_one_bubble_per_saved_user_message(page, live_app, repeat_text):
    sid = seed(live_app[1], "full-history", count=100)
    text = "A slow response in a full history page"
    if repeat_text:
        live_app[1].messages[sid][-2]["content"] = text
    page.evaluate("async sid => (await import('/static/store.js')).openSession(sid)", sid)
    held = []
    page.route("**/api/runs", lambda route: held.append(route), times=1)
    page.get_by_label("Message Hermes").fill(text)
    page.get_by_role("button", name="Send message", exact=True).click()
    # Identical text from an earlier turn must not hide the pending message.
    expect(page.get_by_text(text, exact=True)).to_have_count(2 if repeat_text else 1)
    assert len(held) == 1
    held[0].continue_()
    expect(page.get_by_role("button", name="Stop response", exact=True)).to_be_enabled()
    peer = live_app[1]
    assert len(peer.runs) == 1
    page.evaluate("""async sid => {
        const store = await import('/static/store.js');
        await store.refreshHistory(sid);
    }""", sid)
    expect(page.get_by_text(text, exact=True)).to_have_count(2 if repeat_text else 1)


def test_touch_keyboard_newline_and_repeated_submit_preserve_intentional_guidance(page, live_app):
    context = page.context.browser.new_context(
        viewport={"width": 390, "height": 844},
        is_mobile=True,
        has_touch=True,
        storage_state=page.context.storage_state(),
    )
    try:
        mobile = context.new_page()
        mobile.goto(live_app[0])
        text = mobile.get_by_label("Message Hermes")
        text.fill("A slow mobile response")
        text.press("Enter")
        expect(text).to_have_value("A slow mobile response\n")
        assert not live_app[1].runs
        held = []
        mobile.route("**/api/runs", lambda route: held.append(route), times=1)
        mobile.get_by_role("button", name="Send message", exact=True).tap()
        expect(text).to_be_disabled()
        mobile.locator(".composer").dispatch_event("submit")
        assert len(held) == 1
        held[0].continue_()
        expect(text).to_have_value("")
        assert len(live_app[1].runs) == 1
        text.fill("Please focus on the mobile layout")
        mobile.get_by_role("button", name="Send guidance", exact=True).tap()
        expect(text).to_have_value("")
        assert next(iter(live_app[1].runs.values()))["steer"] == "Please focus on the mobile layout"
        assert len(live_app[1].runs) == 1
    finally:
        context.close()
