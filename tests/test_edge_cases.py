import json

from playwright.sync_api import expect


def lose_ack(route):
    route.fetch()
    route.abort("connectionreset")


def test_lost_ack_survives_refresh(page, live_app):
    page.route("**/api/runs", lose_ack, times=1)
    page.get_by_label("Message Hermes").fill("Recover after refreshing")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_role("button", name="Retry submission")).to_be_visible()
    page.reload()
    page.get_by_role("button", name="Retry submission").click()
    expect(page.get_by_text("What would you like to explore next?", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="A thoughtful place to start")).to_have_count(1)
    assert len(live_app[1].runs) == 1


def test_ambiguous_submission_without_idempotency_cannot_repeat(page, live_app):
    def older_server(route):
        response = route.fetch()
        caps = response.json()
        caps["features"].pop("runs_idempotency")
        route.fulfill(response=response, body=json.dumps(caps))

    page.route("**/api/capabilities", older_server)
    page.reload()
    page.route("**/api/runs", lose_ack, times=1)
    page.get_by_label("Message Hermes").fill("A message without retry protection")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_text("The submission confirmation was lost.", exact=False)).to_be_visible()
    expect(page.get_by_role("button", name="Retry submission")).to_have_count(0)
    page.get_by_label("Message Hermes").fill("Do not send a duplicate")
    expect(page.get_by_role("button", name="Send message", exact=True)).to_be_disabled()
    page.reload()
    expect(page.get_by_role("button", name="Retry submission")).to_have_count(0)
    assert len(live_app[1].runs) == 1


def test_restricted_approval_choices(page, live_app):
    live_app[1].approval_choices = ["deny"]
    page.get_by_label("Message Hermes").fill("A restricted approval")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_role("button", name="Deny", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Allow once", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Deny", exact=True).click()
    expect(page.get_by_text("What would you like to explore next?", exact=True)).to_be_visible()
    assert next(iter(live_app[1].runs.values()))["approval_choice"] == "deny"


def test_mobile_sidebar_keyboard_focus(page):
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator(".sidebar")).to_have_attribute("inert", "")
    page.get_by_role("button", name="Open sidebar").click()
    home = page.get_by_role("button", name="Talaria home")
    expect(home).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(page.get_by_role("button", name="Settings")).to_be_focused()
    page.keyboard.press("Tab")
    expect(home).to_be_focused()
    page.keyboard.press("Escape")
    expect(page.get_by_role("button", name="Open sidebar")).to_be_focused()


def test_subagent_lifecycle_keeps_its_name(page):
    page.get_by_label("Message Hermes").fill("Delegate a slow review")
    page.get_by_role("button", name="Send message", exact=True).click()
    card = page.locator(".tool-card").filter(has_text="Subagent")
    expect(card).to_have_count(1)
    expect(card).not_to_have_class("tool-card working")
    expect(card).to_contain_text("Review the project notes")
    card.locator("summary").click()
    expect(card).to_contain_text("The notes are ready.")
    page.screenshot(path="test-results/subagent.png", animations="disabled")
    page.get_by_role("button", name="Stop response").click()
    expect(page.get_by_text("You stopped this response.")).to_be_visible()


def test_tool_history_retains_command_and_readable_output(page, live_app):
    peer = live_app[1]
    peer.sessions["tool-history"] = {"id": "tool-history", "title": "A tool session"}
    peer.messages["tool-history"] = [
        {
            "id": 1,
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "terminal", "arguments": '{"command":"printf hello"}'},
                }
            ],
        },
        {
            "id": 2,
            "role": "tool",
            "tool_call_id": "call-1",
            "content": '{"output":"hello","exit_code":0}',
        },
    ]
    page.reload()
    page.get_by_role("button", name="A tool session", exact=True).click()
    card = page.locator(".tool-card")
    expect(card.locator(".tool-label")).to_have_text("terminal")
    card.locator("summary").click()
    expect(card.get_by_role("region", name="Tool details")).to_have_text("printf hello")
    expect(card.get_by_text("hello", exact=True)).to_be_visible()
    expect(card).not_to_contain_text('"exit_code"')


def test_missing_required_capability_explains_unavailable_chat(page):
    def older_server(route):
        response = route.fetch()
        caps = response.json()
        caps["features"]["run_submission"] = False
        route.fulfill(response=response, body=json.dumps(caps))

    page.route("**/api/capabilities", older_server)
    page.reload()
    expect(page.get_by_text("Update Hermes to a version", exact=False)).to_be_visible()
    page.get_by_label("Message Hermes").fill("Unavailable")
    expect(page.get_by_role("button", name="Send message", exact=True)).to_be_disabled()


def test_wide_code_is_keyboard_scrollable(page, live_app):
    peer = live_app[1]
    peer.sessions["wide-code"] = {"id": "wide-code", "title": "A wide code sample"}
    peer.messages["wide-code"] = [
        {
            "id": 1,
            "role": "assistant",
            "content": '```python\nprint("' + "wide text " * 100 + '")\n```',
        }
    ]
    page.reload()
    page.get_by_role("button", name="A wide code sample", exact=True).click()
    code = page.get_by_role("region", name="python code")
    code.focus()
    page.keyboard.press("ArrowRight")
    expect(code).not_to_have_js_property("scrollLeft", 0)
