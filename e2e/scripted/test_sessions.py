"""Sessions, questions and transcripts."""

import json
import re
import subprocess
from pathlib import Path

from playwright.sync_api import expect
from scripted_model import ECHO, plan, terminal, tool

CREATE_LAUNCH_STUB = """
import sys
from hermes_state import SessionDB
db = SessionDB()
db.create_session(sys.argv[1], "tui")
db.end_session(sys.argv[1], "tui_close")
db.close()
"""


def session_action(page, name):
    page.get_by_role("button", name="Session options", exact=True).click()
    page.get_by_role("button", name=name, exact=True).click()


def test_rename_search_and_delete_a_session(chat):
    chat.send(plan("zephyr-quokka"))
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


def test_a_terminal_session_with_nothing_saved(chat, hermes):
    # Hermes's terminal once saved an untitled session for each launch, typed into or not.
    sid = "e2e-launch-stub"
    python = Path(hermes.command[0]).with_name("python3")
    subprocess.run([python, "-c", CREATE_LAUNCH_STUB, sid], env=hermes.env, check=True)
    page = chat.page
    page.evaluate("id => import('/static/store.js').then((store) => store.openSession(id))", sid)
    expect(page.get_by_text("This session has no chat history.", exact=True)).to_be_visible()
    expect(
        page.get_by_text("Hermes has no saved messages from Terminal.", exact=True)
    ).to_be_visible()
    expect(page.get_by_role("button", name="Continue in a branch", exact=True)).to_have_count(0)
    # Asked for anyway, as a menu can before a session opens, a branch copies nothing.
    # These go outside the page, where an expected refusal is not a console problem.
    token = page.request.get(f"{chat.url}/api/bootstrap").json()["csrf"]
    headers = {"X-CSRF-Token": token, "X-Talaria-Request": "1"}
    refused = page.request.post(f"{chat.url}/api/sessions/{sid}/fork", data={}, headers=headers)
    assert refused.status == 409
    assert refused.json()["error"] == "This session has no chat history to branch."
    listed = page.request.get(f"{chat.url}/api/sessions?limit=100").json()["data"]
    assert not [row for row in listed if row.get("parent_session_id") == sid]
    exported = page.request.get(f"{chat.url}/api/sessions/{sid}/export?format=json")
    assert json.loads(exported.text())["messages"] == []
    page.locator(".child-return").get_by_role("button", name="New session", exact=True).click()
    expect(page.get_by_label("Message Hermes")).to_be_visible()
    chat.send(plan("First message saved."))
    expect(page.get_by_text("This session has no chat history.", exact=True)).to_have_count(0)


def test_markdown_transcript_has_the_conversation(chat):
    chat.send(plan(terminal("echo transcript-check"), "OK"))
    session_action(chat.page, "Download transcript")
    with chat.page.expect_download() as downloaded:
        chat.page.get_by_role("button", name=re.compile("^Markdown")).click()
    text = downloaded.value.path().read_text()
    assert "echo transcript-check" in text
    assert "transcript-check" in text.split("echo transcript-check", 1)[1]
    assert "session_meta" not in text


def test_answering_a_question_from_the_agent(chat):
    question = {"question": "Which colour?", "choices": ["red", "blue"]}
    chat.submit(plan(tool("clarify", questions=[question]), ECHO))
    card = chat.page.get_by_role("region", name="Question from your agent")
    expect(card).to_be_visible()
    card.get_by_label("Or write an answer").fill("blue")
    card.get_by_role("button", name="Send answer").click()
    chat.settle()
    assert "blue" in chat.reply().lower()


def test_a_background_subagent_result_arrives_without_a_reload(chat, token):
    value = token("sub.txt")
    task = plan(terminal("cat sub.txt"), ECHO)
    chat.send(plan(tool("delegate_task", goal=task), "dispatched"))
    # Hermes saves the result to the session later, without starting a turn.
    notice = chat.page.locator(".message.notice")
    expect(notice).to_be_visible(timeout=60_000)
    expect(notice).to_contain_text("Background delegation completed")
    assert value in notice.evaluate("node => node.textContent")
