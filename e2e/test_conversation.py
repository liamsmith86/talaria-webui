"""Everyday conversations with a real model."""

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


def test_markdown_and_code_render(chat):
    chat.send(
        "Reply with a Markdown table with columns Name and Age and two rows of made-up data, "
        "then a fenced python code block that prints hello."
    )
    reply = chat.page.locator(".message.assistant").last
    text = reply.locator(".message-text")
    expect(text.locator("table")).to_be_visible()
    expect(text.locator("pre code")).to_contain_text("print")
    expect(text.get_by_role("button", name="Copy code")).to_be_visible()
