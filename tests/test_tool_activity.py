"""Tool and reasoning activity reads as one-line steps, grouped between prose."""

import json

from playwright.sync_api import expect

from .test_conversation_features import seed
from .test_session_continuity import open_session


def call(call_id, name, **arguments):
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(arguments)}}


def result(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps(content)}


def saved_activity(peer):
    sid = seed(peer, "activity")
    rows = [
        {"role": "user", "content": "Fix the failing test"},
        {
            "role": "assistant",
            "content": "",
            "reasoning": "**Planning** the fix\n\nRead the test first.",
            "tool_calls": [
                call("read", "read_file", path="tests/test_app.py"),
                call("grep", "search_files", pattern="needle"),
            ],
        },
        result("read", {"content": "1|def test_app(): ...", "total_lines": 1}),
        result("grep", {"total_count": 0}),
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                call("run", "terminal", command="pytest -q"),
                call("again", "terminal", command="pytest -q -x"),
            ],
        },
        result("run", {"output": "1 failed", "exit_code": 1, "error": None}),
        result("again", {"output": "Saved passing run", "exit_code": 0, "error": None}),
        {
            "role": "assistant",
            "content": "The test passes now.",
            "tool_calls": [call("status", "terminal", command="git status")],
        },
        result("status", {"output": "clean", "exit_code": 0}),
        {"role": "assistant", "content": "All done."},
    ]
    peer.messages[sid] = [{"id": index + 1, **row} for index, row in enumerate(rows)]
    return sid


def test_consecutive_steps_fold_into_one_summarized_group(page, live_app):
    open_session(page, saved_activity(live_app[1]))
    reply = page.locator(".message.assistant")
    group = reply.locator(".activity")
    expect(group).to_have_count(1)
    order = reply.evaluate("""el => [...el.children]
      .filter(node => node.matches('.message-text,.tool-stack'))
      .map(node => node.querySelector('.activity') ? 'group' : node.className)""")
    assert order == ["group", "message-text", "tool-stack", "message-text"]
    expect(group.locator("> summary .tool-label")).to_have_text(
        "Ran 2 commands, read 1 file, and searched 1 time"
    )
    expect(group.locator(".activity-failed")).to_have_text("1 failed")
    expect(group).not_to_have_attribute("open", "")
    # Narration ends the group; a lone tool stays a plain row.
    single = reply.locator(".tool-stack > .tool-card")
    expect(single.locator(".tool-label")).to_have_text("Ran git status")
    expect(single.locator(".tool-status")).to_have_text("Completed")

    group.locator("> summary").click()
    steps = group.locator(".activity-steps > li")
    expect(steps.locator("summary .tool-label")).to_have_text(
        [
            "Thought Planning the fix",
            "Read tests/test_app.py",
            "Searched files for needle",
            "Ran pytest -q",
            "Ran pytest -q -x",
        ]
    )
    expect(steps.locator(".tool-status")).to_have_text(
        ["Finished", "Finished", "Failed", "Completed"]
    )
    failed = steps.nth(3)
    expect(failed.locator(".tool-meta")).to_have_text("exit 1")
    failed.locator("summary").click()
    expect(failed.get_by_role("region", name="Tool details")).to_have_text("pytest -q")
    expect(failed.get_by_role("region", name="Tool result")).to_have_text("1 failed")


def test_find_reveals_a_match_inside_a_folded_group(page, live_app):
    open_session(page, saved_activity(live_app[1]))
    expect(page.locator(".activity")).not_to_have_attribute("open", "")
    page.get_by_role("button", name="Find in session", exact=True).click()
    page.get_by_label("Find text in session").fill("Saved passing run")
    expect(page.locator(".find-count")).to_have_text("1 of 1")
    expect(page.locator(".activity")).to_have_attribute("open", "")
    expect(page.locator(".activity .tool-card").nth(3)).to_have_attribute("open", "")


def test_live_group_follows_the_current_step_then_summarizes(page, live_app):
    sid = seed(live_app[1], "live-activity")
    open_session(page, sid)

    def apply(*events):
        page.evaluate(
            """async ([sid, events]) => {
              const {state, update} = await import('/static/store.js');
              const {applyEvent} = await import('/static/runs.js');
              let live = state.lives[sid] || {id:'live', status:'running', text:'', tools:[],
                userText:'Fix it', baseHistoryLength:0};
              for (const event of events) live = applyEvent(live, event);
              update({lives: {[sid]: live}});
            }""",
            [sid, list(events)],
        )

    apply(
        {"event": "tool.started", "tool": "read_file", "tool_call_id": "a", "preview": "app.py"},
        {"event": "tool.completed", "tool_call_id": "a", "duration": 0.4},
    )
    expect(page.locator(".tool-card .tool-label")).to_have_text("Read app.py")
    apply(
        {"event": "tool.started", "tool": "read_file", "tool_call_id": "c", "preview": "app.py"},
        {"event": "tool.completed", "tool_call_id": "c", "duration": 0.5},
        {"event": "tool.started", "tool": "terminal", "tool_call_id": "b", "preview": "pytest"},
    )
    group = page.locator(".activity")
    expect(group).to_have_class("activity working")
    expect(group.locator("> summary .tool-label")).to_have_text("Running pytest")
    apply(
        {"event": "tool.completed", "tool_call_id": "b", "duration": 1.1},
        {"event": "talaria.reasoning.delta", "block_id": "r", "delta": "Checking the output"},
    )
    expect(group.locator("> summary .tool-label")).to_have_text("Thinking")
    apply({"event": "message.delta", "delta": "Fixed."})
    expect(group).to_have_class("activity")
    # A file read twice is still one file.
    expect(group.locator("> summary .tool-label")).to_have_text("Ran 1 command and read 1 file")
    expect(group.locator("> summary .tool-meta")).to_have_text("2s")
    group.locator("> summary").click()
    expect(group.locator(".reasoning summary")).to_have_text("Thought Checking the output")


def test_activity_labels_and_summaries_follow_the_language(page, live_app):
    open_session(page, saved_activity(live_app[1]))
    page.evaluate("async () => (await import('/static/i18n.js')).setLanguage('de')")
    group = page.locator(".activity")
    expect(group.locator("> summary .tool-label")).to_have_text(
        "2 Befehle ausgeführt, 1 Datei gelesen und 1-mal gesucht"
    )
    expect(group.locator(".activity-failed")).to_have_text("1 fehlgeschlagen")
    expect(page.locator(".tool-stack > .tool-card .tool-label")).to_have_text(
        "Ausgeführt: git status"
    )
    group.locator("> summary").click()
    expect(group.locator(".reasoning summary")).to_have_text("Überlegung Planning the fix")
    expect(group.locator(".tool-meta").first).to_have_text("Exit-Code 1")
