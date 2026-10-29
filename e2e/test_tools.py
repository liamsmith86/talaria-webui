"""Tool steps as people see them while Hermes works and after a reload."""

import pytest
from playwright.sync_api import expect


def test_consecutive_commands_fold_into_one_group(chat, token):
    values = [token(f"{name}.txt") for name in ("a", "b", "c")]
    reply = chat.send(
        "Use the terminal tool three separate times, one command per call, in this order: "
        "cat a.txt, cat b.txt, cat c.txt. Then reply with only the three outputs, "
        "separated by spaces."
    )
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
        "Use your file writing tool to create notes.txt containing exactly: alpha beta\n"
        "Then use your file reading tool to read notes.txt, and reply with only its contents."
    )
    assert "alpha beta" in reply
    assert (hermes.workspace / "notes.txt").read_text().strip() == "alpha beta"
    expect(chat.page.locator(".tool-card").filter(has_text="notes.txt")).to_have_count(2)


def test_a_failing_command_is_marked_failed(chat):
    chat.send(
        "Use the terminal tool to run exactly: ls /definitely-missing-e2e-dir\n"
        "Do not retry. Then reply with only: checked"
    )
    step = chat.page.locator(".tool-card").filter(has_text="definitely-missing-e2e-dir").first
    expect(step.locator(".tool-status.failed")).to_be_visible()
    expect(step.locator(".tool-meta")).to_contain_text("exit 2")


def test_long_output_is_readable_text(chat):
    chat.send(
        "Use the terminal tool to run exactly: seq 1 6000\n"
        "Then reply with only the last number it printed."
    )
    step = chat.page.locator(".tool-card").filter(has_text="seq 1 6000").first
    step.locator("summary").click()
    result = step.get_by_role("region", name="Tool result", exact=True)
    assert result.evaluate("el => el.textContent").startswith("1\n2\n3\n")
    expect(step.get_by_text("Preview shortened", exact=True)).to_be_visible()


@pytest.mark.viewport(390, 844)
def test_steps_fit_a_phone_screen(chat, token):
    token("a-file-with-a-rather-long-name-to-check-wrapping-on-narrow-screens.txt")
    chat.send(
        "Use the terminal tool to run exactly: "
        "cat a-file-with-a-rather-long-name-to-check-wrapping-on-narrow-screens.txt\n"
        "Then reply with only OK."
    )
    expect(chat.page.locator(".tool-card")).to_have_count(1)
    overflow = chat.page.evaluate("() => document.scrollingElement.scrollWidth - window.innerWidth")
    assert overflow <= 0
