"""Steering, stopping and changing turns."""

from playwright.sync_api import expect
from scripted_model import held_terminal, plan, terminal


def test_stopping_during_a_command_lets_the_next_turn_run(chat):
    chat.submit(plan(terminal("sleep 600"), "finished"))
    expect(chat.page.locator(".tool-card.working, .activity.working")).to_have_count(1)
    chat.stop.click()
    expect(chat.stop).to_be_hidden(timeout=60_000)
    expect(chat.page.get_by_text("You stopped this response.")).to_be_visible()
    chat.settle()
    assert chat.send(plan("after")) == "after"
    expect(chat.page.locator(".message.user")).to_have_count(2)
    expect(chat.page.locator(".message.assistant")).to_have_count(2)


def test_guidance_sent_while_working_joins_the_turn(chat, hermes):
    chat.submit(plan(held_terminal("guided", "true"), "done"))
    expect(chat.page.locator(".tool-card.working, .activity.working")).to_have_count(1)
    chat.box.fill("When you reply, end with the word STEERED.")
    chat.page.get_by_role("button", name="Send guidance", exact=True).click()
    expect(chat.box).to_have_value("")
    (hermes.workspace / "guided").touch()
    chat.settle()
    expect(chat.page.locator(".message.user")).to_have_count(1)
    expect(chat.page.locator(".message.assistant")).to_have_count(1)
    guidance = chat.page.locator(".message.assistant .guidance")
    expect(guidance).to_have_text("Your guidance When you reply, end with the word STEERED.")
    # Hermes marks guidance in the transcript; Talaria must not show its marker.
    shown = chat.page.locator(".user-content, .guidance, .message-text")
    expect(shown.filter(has_text="OUT-OF-BAND")).to_have_count(0)


def test_edit_and_resend_replaces_the_turn(chat):
    chat.send(plan("one"))
    chat.page.get_by_role("button", name="Edit and resend", exact=True).last.click()
    dialog = chat.page.get_by_role("dialog", name="Edit and resend", exact=True)
    dialog.get_by_role("textbox", name="Edit message", exact=True).fill(plan("two"))
    chat.confirm("Edit and resend")
    chat.settle()
    expect(chat.page.locator(".message.user")).to_have_count(1)
    expect(chat.page.locator(".message.user")).to_contain_text("two")
    assert chat.reply() == "two"


def test_regenerating_keeps_one_reply(chat):
    chat.send(plan("lion"))
    first = chat.value("state.history.at(-1).id")
    chat.page.get_by_role("button", name="Regenerate response", exact=True).last.click()
    chat.confirm("Regenerate response")
    chat.settle()
    assert chat.value("state.history.at(-1).id") != first
    expect(chat.page.locator(".message.user")).to_have_count(1)
    expect(chat.page.locator(".message.assistant")).to_have_count(1)


def test_deleting_the_last_turn(chat):
    chat.send(plan("first"))
    chat.send(plan("second"))
    chat.page.get_by_role("button", name="Delete turn", exact=True).last.click()
    chat.confirm("Delete turn")
    expect(chat.page.locator(".message.user")).to_have_count(1)
    expect(chat.page.locator(".conversation-content")).not_to_contain_text("second")


def test_branching_from_a_reply_copies_the_conversation_up_to_it(chat):
    chat.send(plan("apple"))
    chat.send(plan("banana"))
    original = chat.value("state.active")
    chat.page.get_by_role("button", name="Branch from here", exact=True).first.click()
    dialog = chat.page.get_by_role("dialog", name="Branch from here", exact=True)
    dialog.get_by_role("button", name="Create branch", exact=True).click()
    chat.store(f"s => s.active && s.active !== {original!r} && !s.loading", 60_000)
    expect(chat.page.locator(".message.user")).to_have_count(1)
    expect(chat.page.locator(".conversation-content")).not_to_contain_text("banana")
    assert chat.send(plan("cherry")) == "cherry"
    chat.page.evaluate("async id => (await import('/static/store.js')).openSession(id)", original)
    expect(chat.page.locator(".message.user")).to_have_count(2)
    expect(chat.page.locator(".conversation-content")).to_contain_text("banana")


def test_approving_a_flagged_command_runs_it(chat, hermes):
    (hermes.workspace / "approve-me").mkdir()
    chat.submit(plan(terminal("rm -r approve-me"), "done"))
    card = chat.page.locator(".approval-card")
    expect(card).to_contain_text("rm -r approve-me")
    # A pending approval survives a reload.
    chat.page.reload()
    expect(card).to_contain_text("rm -r approve-me", timeout=30_000)
    card.get_by_role("button", name="Allow once", exact=True).click()
    chat.settle()
    expect(card).to_have_count(0)
    assert not (hermes.workspace / "approve-me").exists()


def test_denying_a_flagged_command_keeps_it_from_running(chat, hermes):
    (hermes.workspace / "keep-me").mkdir()
    chat.submit(plan(terminal("rm -r keep-me"), "blocked"))
    card = chat.page.locator(".approval-card")
    expect(card).to_contain_text("rm -r keep-me")
    card.get_by_role("button", name="Deny", exact=True).click()
    chat.settle()
    expect(card).to_have_count(0)
    assert (hermes.workspace / "keep-me").is_dir()
