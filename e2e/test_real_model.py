"""That a real provider works: its streams, tools, reasoning and summaries.

Everything else is in scripted/. These follow what a real model chooses to do, so keep
them few, and only ask for what no model has a reason to do differently.
"""

from playwright.sync_api import expect


def test_a_reply_settles_and_the_session_is_listed(chat):
    reply = chat.send("Reply with only the word pong, in lowercase.")
    assert "pong" in reply.lower()
    chat.store("s => s.sessions.some((item) => item.id === s.active)", 60_000)


def test_a_follow_up_keeps_the_conversation_context(chat):
    chat.send("Remember this code word: heliotrope. Reply with only OK.")
    reply = chat.send("What was the code word? Reply with only the word.")
    assert "heliotrope" in reply.lower()
    expect(chat.page.locator(".message.user")).to_have_count(2)


def test_reasoning_appears_once_the_reply_is_done(chat):
    chat.send(
        "Do not use any tools. Think carefully in your reasoning about which is larger, "
        "9^(9^9) or 10^(9^8), checking your work twice. Then reply in one short sentence."
    )
    assert chat.value("state.history.some((row) => row.role === 'assistant' && row.reasoning)")
    expect(chat.page.locator(".reasoning")).not_to_have_count(0)


def test_a_terminal_step_shows_its_command_and_output(chat, token):
    value = token("step.txt")
    reply = chat.send(
        "Use the terminal tool to run exactly: cat step.txt\nThen reply with only its output."
    )
    assert value in reply
    step = chat.page.locator(".tool-card").filter(has_text="cat step.txt")
    expect(step).to_have_count(1)
    step.locator("summary").click()
    expect(step.locator(".tool-content")).to_contain_text(value)


def test_compress_shows_its_result_below_the_conversation(chat):
    for word in ("red", "green", "blue"):
        chat.send(f"Reply with one short sentence that mentions the colour {word}.")
    chat.submit("/compress")
    page = chat.page
    expect(page.locator(".command-card.is-running")).to_be_visible()
    expect(page.locator(".command-card.is-running")).to_have_count(0, timeout=300_000)
    # A compaction that changed the transcript becomes part of the conversation;
    # anything else leaves its result card. Either way it ends the conversation.
    rows = page.locator(".conversation-content > :is(.message, .command-card)")
    expect(rows.last).to_be_in_viewport()
    assert rows.last.evaluate("node => node.matches('.command-card, .message.notice')")
    if rows.last.evaluate("node => node.matches('.message.notice')"):
        expect(rows.last).to_contain_text("Context compressed")
        # Another device, or this one after a reload, shows the same.
        page.reload()
        expect(rows.last).to_contain_text("Context compressed")
    reply = chat.send("Which colours did I mention? Reply with only the colour names.")
    assert "blue" in reply.lower()
