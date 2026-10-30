"""Native turn-limit handoffs stay resumable and reconcile their wrap-up transcript."""

import json
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .conftest import wait_for_store
from .test_commands import prepare


@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize("fixture", ["turn-limit", "turn-limit-v0215"])
def test_turn_limit_handoff_reconciles_and_allows_compression(
    page, live_app, monkeypatch, restored, fixture
):
    sid = prepare(page, live_app)
    trace = json.loads((Path(__file__).with_name("fixtures") / f"{fixture}.json").read_text())
    terminal = trace["events"][-1]
    rows = [{"id": index + 1, **row} for index, row in enumerate(trace["messages"])]

    async def events(run):
        live_app[1].messages[sid] = rows
        run.update(terminal, status="failed")
        for event in trace["events"]:
            yield "data: " + json.dumps(event) + "\n\n"

    if restored:
        live_app[1].messages[sid] = rows
        page.route(
            "**/api/runs/limited-run",
            lambda route: route.fulfill(json={**terminal, "status": "failed"}),
        )
        page.evaluate(
            """async sid => {
              const {writeStorage} = await import('/static/lib.js');
              writeStorage('runs', JSON.stringify({[sid]:{
                id:'limited-run', userText:'Work until the turn limit', baseMessageId:0
              }}));
              await (await import('/static/runs.js')).restoreRuns();
            }""",
            sid,
        )
    else:
        monkeypatch.setattr(live_app[1], "events", events)
        page.get_by_label("Message Hermes").fill("Work until the turn limit")
        page.get_by_role("button", name="Send message", exact=True).click()
    wait_for_store(page, "s => s.lives[s.active]?.status === 'failed'")
    expect(page.locator(".run-error")).to_have_count(0)
    expect(
        page.get_by_role("status").filter(has_text="Hermes reached its turn limit")
    ).to_be_visible()
    wait_for_store(page, "s => s.lives[s.active]?.persisted === true")
    # Hermes's request for this wrap-up isn't something the reader said.
    expect(page.get_by_text("maximum number of tool-calling iterations")).to_have_count(0)
    reply = page.locator(".message.assistant")
    expect(reply).to_have_count(1)
    expect(reply).to_contain_text("Before the turn limit.")
    expect(reply).to_contain_text(terminal["output"])
    # Exercise the reported next action through the actual command UI.
    page.get_by_label("Message Hermes").fill("/compress ")
    page.get_by_role("button", name="Send message", exact=True).click()
    card = page.get_by_role("region", name="/compress command")
    expect(card).to_contain_text("Compressing context…")
    live_app[1].finish_command(session_id=sid, text="Compression complete")
    expect(card).to_contain_text("Compression complete")
    expect(card).to_be_in_viewport()


@pytest.mark.parametrize(
    "override",
    [
        {"error": "Provider unavailable"},
        {"output": ""},
        {"partial": True},
        {"interrupted": True},
        {"completed": True},
        {"turn_exit_reason": "error_near_max_iterations(1/1)"},
        {"turn_exit_reason": None},
    ],
)
def test_other_incomplete_responses_still_report_errors(module_page, override):
    result = module_page.evaluate(
        """async override => {
          const {applyEvent} = await import('/static/runs.js');
          const live = applyEvent({status:'running',text:'',tools:[]}, {
            event:'run.failed', completed:false, partial:false, interrupted:false,
            output:'Work remaining', turn_exit_reason:'max_iterations_reached(1/1)', ...override
          });
          return {error:live.error, limited:live.turnLimitReached};
        }""",
        override,
    )
    assert result["limited"] is False
    assert result["error"] == override.get(
        "error", "Hermes could not finish this response. Check the model/provider and try again."
    )


@pytest.mark.parametrize("boundary,output", [(5, "Work remaining"), (0, "Another reply")])
def test_turn_limit_reconciliation_rejects_old_or_unrelated_wrapups(module_page, boundary, output):
    saved = module_page.evaluate(
        """async ({boundary,output}) => {
          const {applyEvent,responseSaved} = await import('/static/runs.js');
          const live = applyEvent({userText:'New work',baseMessageId:boundary,text:'',tools:[]}, {
            event:'run.failed', completed:false, output,
            turn_exit_reason:'max_iterations_reached(1/1)'
          });
          return responseSaved([
            {id:1,role:'user',content:'Old work'},
            {id:2,role:'user',content:'Internal summary request'},
            {id:3,role:'assistant',content:'Work remaining'}
          ], live);
        }""",
        {"boundary": boundary, "output": output},
    )
    assert saved is False
