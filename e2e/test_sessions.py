"""Sessions, commands and transcripts against a real gateway."""

import re

from playwright.sync_api import expect


def session_action(page, name):
    page.get_by_role("button", name="Session options", exact=True).click()
    page.get_by_role("button", name=name, exact=True).click()


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


def test_rename_search_and_delete_a_session(chat):
    chat.send("Reply with only the word zephyr-quokka.")
    session_action(chat.page, "Rename")
    chat.page.get_by_label("Session name").fill("E2E renamed session")
    chat.page.get_by_role("button", name="Save name").click()
    expect(chat.page.locator(".topbar-title")).to_have_text("E2E renamed session")
    chat.page.get_by_role("textbox", name="Search sessions").fill("zephyr-quokka")
    hit = chat.page.locator(".history-search-hit").first
    expect(hit).to_be_visible(timeout=30_000)
    expect(hit).to_contain_text("E2E renamed session")
    chat.page.get_by_role("textbox", name="Search sessions").fill("")
    deleted = chat.value("state.active")
    session_action(chat.page, "Delete session")
    chat.page.get_by_role("dialog").get_by_role("button", name="Delete session", exact=True).click()
    expect(chat.page.locator(".topbar-title")).to_have_text("New session")
    chat.store(f"s => !s.sessions.some((item) => item.id === {deleted!r})", 30_000)


def test_markdown_transcript_has_the_conversation(chat):
    chat.send(
        "Use the terminal tool to run exactly: echo transcript-check\nThen reply with only OK."
    )
    session_action(chat.page, "Download transcript")
    with chat.page.expect_download() as downloaded:
        chat.page.get_by_role("button", name=re.compile("^Markdown")).click()
    text = downloaded.value.path().read_text()
    assert "echo transcript-check" in text
    assert "transcript-check" in text.split("echo transcript-check", 1)[1]
    assert "session_meta" not in text


def test_answering_a_question_from_the_agent(chat):
    chat.submit(
        "Use the clarify tool to ask me which colour I prefer, offering the choices red and "
        "blue. After I answer, reply with only the colour I chose."
    )
    card = chat.page.get_by_role("region", name="Question from your agent")
    expect(card).to_be_visible(timeout=120_000)
    card.get_by_label("Or write an answer").fill("blue")
    card.get_by_role("button", name="Send answer").click()
    chat.settle()
    assert "blue" in chat.reply().lower()


def test_a_background_subagent_result_arrives_without_a_reload(chat, token):
    value = token("sub.txt")
    chat.send(
        "Use the delegate_task tool to give one subagent this task: run the terminal command "
        "cat sub.txt and report its exact output. Then end your turn with a one-line status."
    )
    # Hermes saves the result to the session later, without starting a turn.
    notice = chat.page.locator(".message.notice")
    expect(notice).to_be_visible(timeout=300_000)
    expect(notice).to_contain_text("Background delegation completed")
    assert value in notice.evaluate("node => node.textContent")
