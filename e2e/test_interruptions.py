"""Leaving and coming back while Hermes is still working on a reply."""

from playwright.sync_api import expect

WORKING = ".tool-card.working, .activity.working"


def slow_read(chat, token, name):
    value = token(name)
    chat.submit(
        f"Use the terminal tool to run exactly: sleep 10; cat {name}\n"
        "Then reply with only its output."
    )
    expect(chat.page.locator(WORKING)).to_have_count(1, timeout=120_000)
    return value


def test_reloading_mid_reply_picks_it_back_up(chat, token):
    value = slow_read(chat, token, "reload.txt")
    chat.page.reload()
    expect(chat.page.locator(".message.user")).to_have_count(1, timeout=30_000)
    expect(chat.stop).to_be_visible()
    chat.settle()
    assert value in chat.reply()
    expect(chat.page.locator(".message.assistant")).to_have_count(1)


def test_switching_sessions_mid_reply_keeps_both_conversations(chat, token):
    value = slow_read(chat, token, "first.txt")
    first = chat.value("state.active")
    link = chat.page.locator(f'a.session-select[href*="session={first}"]')
    expect(link.locator(".session-dot.live")).to_have_count(1, timeout=30_000)

    chat.new_session()
    assert "second" in chat.send("Reply with only the word second.").lower()
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
