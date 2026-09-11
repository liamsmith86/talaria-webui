"""Regression coverage for UI focus, search visibility, and draft interactions."""

import pytest
from playwright.sync_api import expect

from .test_conversation_features import png, seed


def open_notes(page, live_app, messages=None):
    sid = seed(live_app[1])
    if messages is not None:
        live_app[1].messages[sid] = messages
    page.reload()
    page.get_by_role("button", name="Design notes", exact=True).click()
    expect(page.locator(".topbar-title")).to_have_text("Design notes")
    expect(page.locator(".history-loading")).to_have_count(0)
    return sid


def hold_attachment_write(page):
    page.evaluate("""() => {
        const complete = Object.getOwnPropertyDescriptor(IDBTransaction.prototype, 'oncomplete');
        Object.defineProperty(IDBTransaction.prototype, 'oncomplete', {
            configurable: true,
            get: complete.get,
            set(callback) {
                complete.set.call(this, event => {
                    if (this.mode === 'readwrite') {
                        window.finishAttachmentWrite = () => {
                            Object.defineProperty(IDBTransaction.prototype, 'oncomplete', complete);
                            window.finishAttachmentWrite = null;
                            callback.call(this, event);
                        };
                    } else callback.call(this, event);
                });
            },
        });
    }""")


def test_dialog_shortcuts_leave_the_underlying_conversation_alone(page, live_app):
    open_notes(page, live_app)
    page.get_by_role("button", name="Session options", exact=True).click()
    page.get_by_role("button", name="Rename", exact=True).click()
    expect(page.get_by_label("Session name")).to_be_focused()
    page.keyboard.press("Control+k")
    expect(page.get_by_label("Session name")).to_be_focused()
    page.keyboard.press("Control+n")
    expect(page.get_by_role("dialog", name="Rename session")).to_be_visible()
    expect(page.locator(".topbar-title")).to_have_text("Design notes")
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(page.get_by_role("button", name="Session options", exact=True)).to_be_focused()


def test_mobile_modal_escape_keeps_sidebar_and_restores_its_trigger(page):
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("button", name="Open sidebar").click()
    page.get_by_role("button", name="Settings").click()
    expect(page.get_by_role("dialog", name="Settings", exact=True)).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog", name="Settings", exact=True)).to_have_count(0)
    expect(page.get_by_role("dialog", name="Sessions", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Settings")).to_be_focused()
    page.keyboard.press("Escape")
    expect(page.get_by_role("button", name="Open sidebar")).to_be_focused()


def test_mobile_search_shortcut_keeps_focus_after_the_drawer_opens(page):
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_label("Message Hermes").focus()
    page.keyboard.press("Control+k")
    expect(page.get_by_label("Search sessions")).to_be_focused()
    # A frame after the drawer's entrance completes must not steal search focus.
    page.locator(".sidebar").evaluate(
        "async el => { await Promise.all(el.getAnimations().map(a => a.finished)); }"
    )
    expect(page.get_by_label("Search sessions")).to_be_focused()


def test_find_tracks_reasoning_and_tool_results_without_text_deltas(page, live_app):
    sid = open_notes(page, live_app)
    page.evaluate(
        """async sid => {
            const {update} = await import('/static/store.js');
            update({lives: {[sid]: {id:'local-preview', status:'running', text:'',
                reasoning:'', tools:[], userText:'Message 000', baseHistoryLength:0}}});
        }""",
        sid,
    )
    page.get_by_role("button", name="Find in session", exact=True).click()
    page.evaluate("""() => {
        const walker = document.createTreeWalker.bind(document);
        window.searchScans = 0;
        document.createTreeWalker = (...args) => {
            window.searchScans++;
            return walker(...args);
        };
    }""")
    page.get_by_label("Find text in session").fill("needle")
    page.wait_for_function("() => window.searchScans > 0")
    expect(page.locator(".find-count")).to_have_text("No matches")
    page.evaluate(
        """async sid => {
            const {state, update} = await import('/static/store.js');
            update({lives: {[sid]: {...state.lives[sid], reasoning:'Reasoning needle',
                tools:[{id:'tool', name:'terminal', preview:'Tool needle', status:'completed'}]}}});
        }""",
        sid,
    )
    expect(page.locator(".find-count")).to_have_text("1 of 2")
    page.get_by_role("button", name="Next match").click()
    expect(page.locator(".tool-card")).to_have_attribute("open", "")


def test_find_reveals_matches_inside_long_scrollable_message(page, live_app):
    open_notes(
        page,
        live_app,
        [{"id": 1, "role": "user", "content": "Long line\n" * 100 + "Hidden needle"}],
    )
    content = page.locator(".user-content")
    content.focus()
    expect(content).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(content).not_to_have_js_property("scrollTop", 0)
    content.evaluate("el => el.scrollTop = 0")
    page.get_by_role("button", name="Find in session", exact=True).click()
    page.get_by_label("Find text in session").fill("Hidden needle")
    expect(page.locator(".find-count")).to_have_text("1 of 1")
    page.wait_for_function("""() => {
        const el = document.querySelector('.user-content');
        const node = el.firstChild;
        const range = new Range();
        range.setStart(node, node.textContent.indexOf('Hidden needle'));
        range.setEnd(node, node.length);
        const match = range.getBoundingClientRect(), box = el.getBoundingClientRect();
        return match.top >= box.top && match.bottom <= box.bottom;
    }""")


def test_failed_mixed_attachment_does_not_reappear_after_reload(page):
    draft = "x" * 799_990
    page.get_by_label("Message Hermes").fill(draft)
    page.get_by_label("File attachment").set_input_files(
        [
            {"name": "extra.png", "mimeType": "image/png", "buffer": png()},
            {"name": "extra.txt", "mimeType": "text/plain", "buffer": b"A note"},
        ]
    )
    expect(page.locator(".attachment-error")).to_contain_text("message is too long")
    expect(page.locator(".attachment-chip")).to_have_count(0)
    page.reload()
    expect(page.get_by_label("Message Hermes")).to_have_value(draft)
    expect(page.get_by_role("button", name="Attach files")).to_be_enabled()
    expect(page.locator(".attachment-chip")).to_have_count(0)


def test_image_removal_serializes_storage_and_other_attachment_changes(page):
    page.get_by_label("File attachment").set_input_files(
        [
            {"name": "first.png", "mimeType": "image/png", "buffer": png()},
            {"name": "second.png", "mimeType": "image/png", "buffer": png()},
        ]
    )
    expect(page.locator(".attachment-chip")).to_have_count(2)
    hold_attachment_write(page)
    page.get_by_role("button", name="Remove first.png", exact=True).click()
    page.wait_for_function("() => !!window.finishAttachmentWrite")
    expect(page.get_by_role("button", name="Remove second.png", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="Attach files", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="Send message", exact=True)).to_be_disabled()
    page.evaluate("window.finishAttachmentWrite()")
    expect(page.locator(".attachment-chip")).to_have_count(1)
    page.reload()
    expect(page.locator(".attachment-chip")).to_have_count(1)
    expect(page.locator(".attachment-chip")).to_contain_text("second.png")


@pytest.mark.parametrize("overflow", [False, True])
def test_mixed_attachment_preserves_typing_during_storage_commit(page, overflow):
    page.get_by_label("Message Hermes").fill("Initial thought")
    hold_attachment_write(page)
    page.get_by_label("File attachment").set_input_files(
        [
            {"name": "extra.png", "mimeType": "image/png", "buffer": png()},
            {"name": "extra.txt", "mimeType": "text/plain", "buffer": b"A note"},
        ]
    )
    page.wait_for_function("() => !!window.finishAttachmentWrite")
    draft = "x" * 799_990 if overflow else "Continued typing"
    page.get_by_label("Message Hermes").fill(draft)
    page.evaluate("window.finishAttachmentWrite()")
    if overflow:
        expect(page.locator(".attachment-error")).to_contain_text("message is too long")
        count = 0
    else:
        draft += "\n\nFile: extra.txt\n\n```\nA note\n```"
        count = 1
    expect(page.get_by_role("button", name="Attach files")).to_be_enabled()
    expect(page.get_by_label("Message Hermes")).to_have_value(draft)
    expect(page.locator(".attachment-chip")).to_have_count(count)
    page.reload()
    expect(page.get_by_label("Message Hermes")).to_have_value(draft)
    expect(page.get_by_role("button", name="Attach files")).to_be_enabled()
    expect(page.locator(".attachment-chip")).to_have_count(count)


def test_reasoning_only_history_remains_available(page, live_app):
    open_notes(
        page,
        live_app,
        [{"id": 1, "role": "assistant", "content": "", "reasoning": "A retained thought"}],
    )
    expect(page.locator(".reasoning summary")).to_be_visible()
    page.locator(".reasoning summary").click()
    expect(page.get_by_text("A retained thought", exact=True)).to_be_visible()


def test_jump_to_latest_stays_above_a_tall_composer(page, live_app):
    open_notes(
        page,
        live_app,
        [{"id": 1, "role": "assistant", "content": "A long response\n\n" * 100}],
    )
    page.get_by_label("Message Hermes").fill("Draft line\n" * 20)
    page.locator(".conversation-viewport").hover()
    page.mouse.wheel(0, -500)
    jump = page.get_by_role("button", name="Jump to latest message")
    expect(jump).to_be_visible()
    button_box = jump.bounding_box()
    viewport_box = page.locator(".conversation-viewport").bounding_box()
    assert button_box["y"] + button_box["height"] <= viewport_box["y"] + viewport_box["height"]
    jump.click()
    expect(jump).to_have_count(0)


@pytest.mark.parametrize(("width", "height"), [(390, 400), (1024, 500)])
def test_short_viewport_keeps_large_draft_controls_reachable(page, width, height):
    page.set_viewport_size({"width": width, "height": height})
    page.get_by_label("File attachment").set_input_files(
        [{"name": f"image-{i}.png", "mimeType": "image/png", "buffer": png()} for i in range(4)]
    )
    expect(page.locator(".attachment-chip")).to_have_count(4)
    page.get_by_label("Message Hermes").fill("Draft line\n" * 20)
    send = page.get_by_role("button", name="Send message", exact=True)
    send.scroll_into_view_if_needed()
    expect(send).to_be_in_viewport(ratio=1)
    expect(page.locator(".topbar")).to_be_in_viewport(ratio=1)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_welcome_suggestion_is_saved_as_a_draft(page):
    page.get_by_role("button", name="Plan").click()
    expect(page.get_by_label("Message Hermes")).to_have_value("Help me think through ")
    page.reload()
    expect(page.get_by_label("Message Hermes")).to_have_value("Help me think through ")


def test_completed_tool_previews_are_not_reparsed_during_stream_updates(page, live_app):
    preview = '{"command":"A stable historical command"}'
    page.context.add_init_script("""(() => {
        const parse = JSON.parse;
        window.previewParses = 0;
        JSON.parse = function(text, ...rest) {
            if (text === '{"command":"A stable historical command"}') window.previewParses++;
            return parse.call(this, text, ...rest);
        };
    })();""")
    sid = open_notes(
        page,
        live_app,
        [
            {
                "id": 1,
                "role": "assistant",
                "content": "A saved response",
                "tool_calls": [
                    {"id": "call", "function": {"name": "terminal", "arguments": preview}}
                ],
            }
        ],
    )
    expect(page.locator(".tool-card summary")).to_contain_text("A stable historical command")
    result = page.evaluate(
        """async sid => {
            const {state, update} = await import('/static/store.js');
            const before = window.previewParses;
            for (let i = 0; i < 10; i++) {
                update({lives: {[sid]: {id:'local-preview', text:'Delta '.repeat(i + 1),
                    tools:[], status:'running', userText:'', baseHistoryLength:0}}});
                await new Promise(requestAnimationFrame);
            }
            return {before, after:window.previewParses};
        }""",
        sid,
    )
    assert result == {"before": 1, "after": 1}


def test_message_edit_does_not_submit_into_a_different_conversation(page, live_app):
    peer = live_app[1]
    peer.extension = {}
    sid = seed(peer, count=2)
    peer.sessions[sid]["source"] = "api_server"
    other = seed(peer, "other")
    peer.sessions[other]["title"] = "Other notes"
    page.reload()
    page.get_by_role("button", name="Design notes", exact=True).click()
    page.get_by_role("button", name="Edit and resend", exact=True).click()
    expect(page.get_by_label("Edit message")).to_have_value("Message 000")
    held = []
    page.route(f"**/api/sessions/{sid}/rewind", lambda route: held.append(route))
    with page.expect_request(f"**/api/sessions/{sid}/rewind"):
        page.get_by_role("dialog").get_by_role("button", name="Edit and resend", exact=True).click()
    page.evaluate("async id => (await import('/static/store.js')).openSession(id)", other)
    assert len(held) == 1
    held[0].continue_()
    expect(page.get_by_role("dialog").get_by_role("alert")).to_contain_text("session changed")
    assert peer.rewinds == 1
    assert not peer.runs
    assert peer.messages[other] == [{"id": 1, "role": "user", "content": "Message 000"}]


def test_sidebar_uses_calendar_days_at_midnight(page, live_app):
    midnight, yesterday = page.evaluate("""() => {
        const today = new Date();
        today.setHours(0, 0, 0, 0);
        const yesterday = new Date(today);
        yesterday.setDate(today.getDate() - 1);
        return [today.getTime() / 1000, yesterday.getTime() / 1000];
    }""")
    peer = live_app[1]
    for sid, title, stamp in (
        ("midnight", "Midnight today", midnight),
        ("yesterday", "Midnight yesterday", yesterday),
    ):
        seed(peer, sid)
        peer.sessions[sid].update(title=title, last_active=stamp)
    page.reload()
    expect(
        page.get_by_role("button", name="Midnight today", exact=True)
        .locator("../..")
        .locator(".session-group")
    ).to_have_text("Today")
    expect(
        page.get_by_role("button", name="Midnight yesterday", exact=True)
        .locator("../..")
        .locator(".session-group")
    ).to_have_text("Yesterday")


def test_pending_pin_cannot_close_a_newly_opened_session_dialog(page, live_app):
    open_notes(page, live_app)
    page.get_by_role("button", name="Session options", exact=True).click()
    held = []

    def hold_pin(route):
        if route.request.method == "PATCH":
            held.append(route)
        else:
            route.continue_()

    page.route("**/api/sessions/notes", hold_pin)
    with page.expect_request(lambda request: request.method == "PATCH"):
        page.get_by_role("button", name="Pin session", exact=True).click()
    expect(page.get_by_role("button", name="Rename", exact=True)).to_be_disabled()
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_be_visible()
    assert len(held) == 1
    held[0].continue_()
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(page.locator(".session-group")).to_have_text("Pinned")
