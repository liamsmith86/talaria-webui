"""Leaving and coming back while Hermes is still working on a reply."""

from playwright.sync_api import expect
from scripted_model import ECHO, held_terminal, plan

WORKING = ".tool-card.working, .activity.working"


def held_read(chat, hermes, token, name):
    """Start a reply that is still working until the test creates the returned file."""
    value = token(name)
    chat.submit(plan(held_terminal(f"{name}.go", f"cat {name}"), ECHO))
    expect(chat.page.locator(WORKING)).to_have_count(1)
    return value, hermes.workspace / f"{name}.go"


def test_reloading_mid_reply_picks_it_back_up(chat, hermes, token):
    value, gate = held_read(chat, hermes, token, "reload.txt")
    chat.page.reload()
    expect(chat.page.locator(".message.user")).to_have_count(1, timeout=30_000)
    expect(chat.stop).to_be_visible()
    # Hermes has saved part of the turn; it still shows once.
    expect(chat.page.locator(".message.assistant")).to_have_count(1)
    expect(chat.page.locator(".tool-card")).to_have_count(1)
    gate.touch()
    chat.settle()
    assert value in chat.reply()
    expect(chat.page.locator(".message.assistant")).to_have_count(1)


def test_switching_sessions_mid_reply_keeps_both_conversations(chat, hermes, token):
    value, gate = held_read(chat, hermes, token, "first.txt")
    first = chat.value("state.active")
    link = chat.page.locator(f'a.session-select[href*="session={first}"]')
    expect(link.locator(".session-dot.live")).to_have_count(1, timeout=30_000)

    chat.new_session()
    assert chat.send(plan("second")) == "second"
    gate.touch()
    # The first reply finishes in the background and is marked done in the list.
    expect(link.locator(".session-activity")).to_have_attribute(
        "aria-label", "Finished", timeout=120_000
    )

    link.click()
    chat.store(f"s => s.active === {first!r} && !s.loading", 30_000)
    chat.settle()
    assert value in chat.reply()
    expect(chat.page.locator(".message.user")).to_have_count(1)
    expect(chat.page.locator(".message.assistant")).to_have_count(1)


def test_another_device_follows_a_running_reply(chat, hermes, token, device):
    value, gate = held_read(chat, hermes, token, "device.txt")
    other = device(chat.page.url)
    expect(other.stop).to_be_visible(timeout=30_000)
    expect(other.page.locator(".message.assistant")).to_have_count(1)
    expect(other.page.locator(".tool-card")).to_have_count(1)
    gate.touch()
    other.settle()
    assert value in other.reply()
    chat.settle()
    assert value in chat.reply()
