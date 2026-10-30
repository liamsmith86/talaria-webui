"""Tool steps as people see them while Hermes works and after a reload."""

import pytest
from playwright.sync_api import expect
from scripted_model import ECHO, plan, reasoned, terminal, tool


def test_consecutive_commands_fold_into_one_group(chat, token):
    values = [token(f"{name}.txt") for name in ("a", "b", "c")]
    reply = chat.send(plan(*[terminal(f"cat {name}.txt") for name in ("a", "b", "c")], ECHO))
    assert all(value in reply for value in values)
    group = chat.page.locator(".conversation-content .activity")
    expect(group).to_have_count(1)
    expect(group.locator(".tool-card")).to_have_count(3)
    summary = group.locator("> summary")
    expect(summary).not_to_contain_text("Thinking")
    summary.click()
    for name in ("a", "b", "c"):
        expect(group.locator(".tool-card").filter(has_text=f"cat {name}.txt")).to_have_count(1)


def test_file_steps_write_and_read_the_workspace(chat, hermes):
    reply = chat.send(
        plan(
            tool("write_file", path="notes.txt", content="alpha beta"),
            tool("read_file", path="notes.txt"),
            ECHO,
        )
    )
    assert "alpha beta" in reply
    assert (hermes.workspace / "notes.txt").read_text().strip() == "alpha beta"
    expect(chat.page.locator(".tool-card").filter(has_text="notes.txt")).to_have_count(2)


def test_a_failing_command_is_marked_failed(chat):
    chat.send(plan(terminal("ls /definitely-missing-e2e-dir"), "checked"))
    step = chat.page.locator(".tool-card").filter(has_text="definitely-missing-e2e-dir").first
    expect(step.locator(".tool-status.failed")).to_be_visible()
    expect(step.locator(".tool-meta")).to_contain_text("exit 2")


def test_a_failed_step_stays_marked_after_a_reload(chat):
    chat.send(plan(tool("read_file", path="definitely-missing.txt"), "checked"))
    expect(chat.page.locator(".tool-status.failed")).to_be_visible()
    chat.assert_reload_matches()
    expect(chat.page.locator(".tool-status.failed")).to_be_visible()


def test_reasoning_appears_once_the_reply_is_done(chat):
    assert chat.send(plan(reasoned("Weighing it up.", "done"))) == "done"
    expect(chat.page.locator(".reasoning")).to_contain_text("Weighing it up.")


def test_long_output_is_readable_text(chat):
    chat.send(plan(terminal("seq 1 6000"), "6000"))
    step = chat.page.locator(".tool-card").filter(has_text="seq 1 6000").first
    step.locator("summary").click()
    result = step.get_by_role("region", name="Tool result", exact=True)
    assert result.evaluate("el => el.textContent").startswith("1\n2\n3\n")
    expect(step.get_by_text("Preview shortened", exact=True)).to_be_visible()


@pytest.mark.viewport(390, 844)
def test_steps_fit_a_phone_screen(chat, token):
    name = "a-file-with-a-rather-long-name-to-check-wrapping-on-narrow-screens.txt"
    token(name)
    chat.send(plan(terminal(f"cat {name}"), "OK"))
    expect(chat.page.locator(".tool-card")).to_have_count(1)
    overflow = chat.page.evaluate("() => document.scrollingElement.scrollWidth - window.innerWidth")
    assert overflow <= 0
