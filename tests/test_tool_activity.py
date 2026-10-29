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
        {
            "event": "tool.started",
            "tool": "read_file",
            "tool_call_id": "a",
            "preview": "app.py",
            "timestamp": 100,
        },
        {"event": "tool.completed", "tool_call_id": "a", "duration": 0.4, "timestamp": 100.4},
    )
    expect(page.locator(".tool-card .tool-label")).to_have_text("Read app.py")
    apply(
        {
            "event": "tool.started",
            "tool": "read_file",
            "tool_call_id": "c",
            "preview": "app.py",
            "timestamp": 100.4,
        },
        {"event": "tool.completed", "tool_call_id": "c", "duration": 0.5, "timestamp": 100.9},
        {
            "event": "tool.started",
            "tool": "terminal",
            "tool_call_id": "b",
            "preview": "pytest",
            "timestamp": 100.9,
        },
    )
    group = page.locator(".activity")
    expect(group).to_have_class("activity working")
    expect(group.locator("> summary .tool-label")).to_have_text("Running pytest")
    apply(
        {"event": "tool.completed", "tool_call_id": "b", "duration": 1.1, "timestamp": 102},
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


def test_compaction_copy_does_not_replace_the_original_result(page, live_app):
    peer = live_app[1]
    sid = seed(peer, "compacted")
    peer.messages[sid] = [
        {"id": 5, "role": "user", "content": "Count to three"},
        {
            "id": 6,
            "role": "assistant",
            "content": "",
            "tool_calls": [call("seq", "terminal", command="seq 1 3")],
        },
        {"id": 3, **result("seq", {"output": "1\n2\n3", "exit_code": 0})},
        {"id": 4, "role": "assistant", "content": "Printed three lines."},
        # In-place compaction re-appends the result it pruned under the same call ID.
        {
            "id": 7,
            "role": "tool",
            "tool_call_id": "seq",
            "content": "[Old tool output cleared to save context space]",
        },
        {"id": 8, "role": "assistant", "content": "[CONTEXT COMPACTION]", "display_kind": "hidden"},
    ]
    open_session(page, sid)
    row = page.locator(".tool-card")
    expect(row).to_have_count(1)
    expect(row.locator(".tool-status")).to_have_text("Completed")
    row.locator("summary").click()
    expect(row.get_by_role("region", name="Tool result")).to_have_text("1\n2\n3")


def apply_live(page, sid, *events):
    page.evaluate(
        """async ([sid, events]) => {
          const {state, update} = await import('/static/store.js');
          const {applyEvent} = await import('/static/runs.js');
          let live = state.lives[sid] || {id:'live', status:'running', text:'', tools:[],
            userText:'Go', baseHistoryLength:0};
          for (const event of events) live = applyEvent(live, event);
          update({lives: {[sid]: live}});
        }""",
        [sid, list(events)],
    )


def started(call_id, tool, preview, at):
    return {
        "event": "tool.started",
        "tool": tool,
        "tool_call_id": call_id,
        "preview": preview,
        "timestamp": at,
    }


def completed(call_id, seconds, at):
    return {
        "event": "tool.completed",
        "tool_call_id": call_id,
        "duration": seconds,
        "timestamp": at,
    }


def test_parallel_steps_show_elapsed_time_not_their_sum(page, live_app):
    sid = seed(live_app[1], "parallel")
    open_session(page, sid)
    # Hermes runs read-only tools concurrently: 3s, then three 2s reads at once.
    apply_live(
        page,
        sid,
        started("run", "terminal", "make", 10),
        completed("run", 3, 13),
        *(started(f"read{i}", "read_file", f"f{i}.py", 13) for i in range(3)),
        *(completed(f"read{i}", 2, 15) for i in range(3)),
        {"event": "message.delta", "delta": "Done."},
    )
    expect(page.locator(".activity > summary .tool-meta")).to_have_text("5s")


def test_an_opened_step_stays_open_when_the_run_becomes_a_group(page, live_app):
    sid = seed(live_app[1], "keep-open")
    open_session(page, sid)
    apply_live(
        page,
        sid,
        {"event": "talaria.reasoning.delta", "block_id": "r", "delta": "Planning"},
        started("a", "read_file", "app.py", 1),
        completed("a", 0.2, 1.2),
    )
    page.locator(".tool-stack > .tool-card > summary").click()
    expect(page.locator(".tool-card[open]")).to_have_count(1)
    apply_live(page, sid, started("b", "terminal", "pytest", 2))
    group = page.locator(".activity")
    expect(group).to_have_attribute("open", "")
    expect(group.locator(".activity-steps .tool-card").first).to_have_attribute("open", "")
    # Once formed, the group keeps the reader's own choice as steps arrive.
    group.locator("> summary").click()
    apply_live(page, sid, completed("b", 1, 3), started("c", "terminal", "ruff", 3))
    expect(group).not_to_have_attribute("open", "")


def test_tool_names_never_resolve_through_object_prototypes(page, live_app):
    peer = live_app[1]
    sid = seed(peer, "prototype")
    peer.messages[sid] = [
        {"id": 1, "role": "user", "content": "Use the plugin tools"},
        {
            "id": 2,
            "role": "assistant",
            "content": "",
            "tool_calls": [call("one", "constructor"), call("two", "toString", path="a.py")],
        },
        {"id": 3, **result("one", {"ok": True})},
        {"id": 4, **result("two", {"ok": True})},
        {"id": 5, "role": "assistant", "content": "Done."},
    ]
    open_session(page, sid)
    expect(page.get_by_text("Done.", exact=True)).to_be_visible()
    page.locator(".activity > summary").click()
    expect(page.locator(".activity-steps .tool-label")).to_have_text(
        ["Constructor", "ToString a.py"]
    )


def test_find_searches_tool_inputs_shown_only_in_their_row(page, live_app):
    peer = live_app[1]
    sid = saved_activity(peer)
    peer.messages[sid][1]["tool_calls"][0] = call("read", "read_file", path="src/zebra_unique.py")
    open_session(page, sid)
    page.get_by_role("button", name="Find in session", exact=True).click()
    find = page.get_by_label("Find text in session")
    find.fill("zebra_unique")
    expect(page.locator(".find-count")).to_have_text("1 of 1")
    expect(page.locator(".activity")).to_have_attribute("open", "")
    # Commands keep their copyable block; the row's subject is not a second match.
    find.fill("pytest -q")
    expect(page.locator(".find-count")).to_have_text("1 of 2")


def test_hermes_events_are_notices_not_user_messages(page, live_app):
    peer = live_app[1]
    sid = seed(peer, "notices")
    peer.messages[sid] = [
        {"id": 1, "role": "user", "content": "Delegate the audit"},
        {"id": 2, "role": "assistant", "content": "Dispatched in the background."},
        {
            "id": 3,
            "role": "user",
            "display_kind": "async_delegation_complete",
            "content": "[ASYNC DELEGATION BATCH COMPLETE] The audit found three stale keys.",
        },
        {
            "id": 4,
            "role": "user",
            "display_kind": "process_complete",
            "content": "[Background process finished] make test exited 0",
        },
        {"id": 5, "role": "user", "content": "Summarize it"},
        {"id": 6, "role": "assistant", "content": "Three stale keys."},
    ]
    open_session(page, sid)
    expect(page.locator(".message.user")).to_have_count(2)
    notices = page.locator(".message.notice")
    expect(notices.locator("summary .tool-label")).to_have_text(
        ["Background delegation completed", "Background process finished"]
    )
    expect(notices.first.locator(".markdown")).not_to_be_visible()
    page.get_by_role("button", name="Find in session", exact=True).click()
    page.get_by_label("Find text in session").fill("stale keys")
    expect(page.locator(".find-count")).to_have_text("1 of 2")
    expect(notices.first.locator("details")).to_have_attribute("open", "")
    page.evaluate("async () => (await import('/static/i18n.js')).setLanguage('de')")
    expect(notices.first.locator("summary .tool-label")).to_have_text(
        "Delegierte Hintergrundaufgabe abgeschlossen"
    )


def test_a_notice_after_the_prompt_does_not_unsettle_the_reply(page, live_app):
    peer = live_app[1]
    sid = seed(peer, "notice-settle")
    peer.messages[sid] = [
        {"id": 1, "role": "user", "content": "Earlier"},
        {"id": 2, "role": "assistant", "content": "Earlier reply"},
    ]
    open_session(page, sid)
    page.get_by_label("Message Hermes").fill("Check the build")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_role("button", name="Stop response", exact=True)).to_be_hidden(timeout=10000)
    # A subagent result lands after the prompt while the reply is saved.
    rows = peer.messages[sid]
    rows.insert(
        -1,
        {
            "id": 99,
            "role": "user",
            "display_kind": "async_delegation_complete",
            "content": "[ASYNC DELEGATION BATCH COMPLETE] done",
        },
    )
    page.evaluate("async sid => (await import('/static/store.js')).refreshHistory(sid)", sid)
    expect(page.locator(".message.notice")).to_have_count(1)
    expect(page.locator(".message.user")).to_have_count(2)
    expect(page.locator(".message.assistant")).to_have_count(2)


def test_long_group_summaries_shorten_with_an_ellipsis(page, live_app):
    page.set_viewport_size({"width": 360, "height": 800})
    open_session(page, saved_activity(live_app[1]))
    page.evaluate("async () => (await import('/static/i18n.js')).setLanguage('de')")
    verb = page.locator(".activity > summary .tool-verb")
    expect(verb).to_have_text("2 Befehle ausgeführt, 1 Datei gelesen und 1-mal gesucht")
    assert verb.evaluate(
        """el => [getComputedStyle(el).textOverflow, el.scrollWidth > el.clientWidth,
          el.getBoundingClientRect().right <= el.parentElement.getBoundingClientRect().right]"""
    ) == ["ellipsis", True, True]
