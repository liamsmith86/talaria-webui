"""Steering, stopping and changing turns while talking to a real model."""

import re

from playwright.sync_api import expect


def test_stopping_during_a_command_lets_the_next_turn_run(chat):
    chat.submit("Use the terminal tool to run exactly: sleep 45\nThen reply with only: finished")
    expect(chat.page.locator(".tool-card.working, .activity.working")).to_have_count(
        1, timeout=120_000
    )
    chat.stop.click()
    expect(chat.stop).to_be_hidden(timeout=60_000)
    expect(chat.page.get_by_text("You stopped this response.")).to_be_visible()
    chat.settle()
    reply = chat.send("Reply with only the word after.")
    assert "after" in reply.lower()


def test_guidance_sent_while_working_joins_the_turn(chat):
    chat.submit(
        "Use the terminal tool to run exactly: sleep 12\nThen reply with one short sentence."
    )
    expect(chat.page.locator(".tool-card.working, .activity.working")).to_have_count(
        1, timeout=120_000
    )
    chat.box.fill("When you reply, end with the word STEERED.")
    chat.page.get_by_role("button", name="Send guidance", exact=True).click()
    expect(chat.box).to_have_value("")
    chat.settle()
    expect(chat.page.locator(".message.user")).to_have_count(1)
    expect(chat.page.locator(".message.assistant")).to_have_count(1)
    guidance = chat.page.locator(".message.assistant .guidance")
    expect(guidance).to_have_text("Your guidance When you reply, end with the word STEERED.")
    # The model's reasoning may quote Hermes's marker; Talaria's own text must not.
    shown = chat.page.locator(".user-content, .guidance, .message-text")
    expect(shown.filter(has_text="OUT-OF-BAND")).to_have_count(0)


def test_edit_and_resend_replaces_the_turn(chat):
    chat.send("Reply with only the number 1.")
    chat.page.get_by_role("button", name="Edit and resend", exact=True).last.click()
    dialog = chat.page.get_by_role("dialog", name="Edit and resend", exact=True)
    dialog.get_by_role("textbox", name="Edit message", exact=True).fill(
        "Reply with only the number 2."
    )
    dialog.get_by_role("button", name="Edit and resend", exact=True).click()
    chat.settle()
    expect(chat.page.locator(".message.user")).to_have_count(1)
    expect(chat.page.locator(".message.user")).to_contain_text("number 2")
    assert "2" in chat.reply()


def test_regenerating_keeps_one_reply(chat):
    chat.send("Reply with only a random animal name.")
    chat.page.get_by_role("button", name="Regenerate response", exact=True).last.click()
    chat.settle()
    expect(chat.page.locator(".message.user")).to_have_count(1)
    expect(chat.page.locator(".message.assistant")).to_have_count(1)


def test_deleting_the_last_turn(chat):
    chat.send("Reply with only the word first.")
    chat.send("Reply with only the word second.")
    chat.page.get_by_role("button", name="Delete turn", exact=True).last.click()
    chat.page.get_by_role("dialog").get_by_role("button", name="Delete turn", exact=True).click()
    expect(chat.page.locator(".message.user")).to_have_count(1)
    expect(chat.page.locator(".conversation-content")).not_to_contain_text("second.")


def test_branching_from_a_reply_copies_the_conversation_up_to_it(chat):
    chat.send("Reply with only the word apple.")
    chat.send("Reply with only the word banana.")
    original = chat.value("state.active")
    chat.page.get_by_role("button", name="Branch from here", exact=True).first.click()
    dialog = chat.page.get_by_role("dialog", name="Branch from here", exact=True)
    dialog.get_by_role("button", name="Create branch", exact=True).click()
    chat.store(f"s => s.active && s.active !== {original!r} && !s.loading", 60_000)
    expect(chat.page.locator(".message.user")).to_have_count(1)
    expect(chat.page.locator(".conversation-content")).not_to_contain_text("banana")
    reply = chat.send("Reply with only the word cherry.")
    assert "cherry" in reply.lower()
    chat.page.evaluate("async id => (await import('/static/store.js')).openSession(id)", original)
    expect(chat.page.locator(".message.user")).to_have_count(2)
    expect(chat.page.locator(".conversation-content")).to_contain_text(re.compile("banana", re.I))


def test_approving_a_flagged_command_runs_it(chat, hermes):
    (hermes.workspace / "approve-me").mkdir()
    chat.submit(
        "Use the terminal tool to run exactly: rm -r approve-me\nThen reply with only: done"
    )
    card = chat.page.locator(".approval-card")
    expect(card).to_contain_text("rm -r approve-me", timeout=120_000)
    # A pending approval survives a reload.
    chat.page.reload()
    expect(card).to_contain_text("rm -r approve-me", timeout=30_000)
    card.get_by_role("button", name="Allow once", exact=True).click()
    chat.settle()
    expect(card).to_have_count(0)
    assert not (hermes.workspace / "approve-me").exists()


def test_denying_a_flagged_command_keeps_it_from_running(chat, hermes):
    (hermes.workspace / "keep-me").mkdir()
    chat.submit(
        "Use the terminal tool to run exactly: rm -r keep-me\n"
        "If it is blocked, do not try again; reply with only: blocked"
    )
    card = chat.page.locator(".approval-card")
    expect(card).to_contain_text("rm -r keep-me", timeout=120_000)
    card.get_by_role("button", name="Deny", exact=True).click()
    chat.settle()
    expect(card).to_have_count(0)
    assert (hermes.workspace / "keep-me").is_dir()
