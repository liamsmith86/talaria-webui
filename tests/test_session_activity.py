"""Attention states are native observations, never guesses from saved tool rows."""

import pytest
from playwright.sync_api import expect

from .test_conversation_features import seed
from .test_session_search import enable


@pytest.mark.clock
def test_native_attention_changes_and_stale_local_completion(page, live_app):
    peer = live_app[1]
    seed(peer)
    endpoint = "/talaria/v1/activity"
    run = {
        "session_id": "notes",
        "run_id": "remote",
        "status": "waiting_for_input",
        "created_at": 200,
    }
    peer.discovery_overrides[endpoint] = ({"data": [run]}, 200)
    enable(page, peer)
    row = page.locator(".session-row")
    expect(row.get_by_label("Needs input", exact=True)).to_be_visible()
    page.evaluate("""async () => {
      const {update}=await import('/static/store.js');
      update({lives:{notes:{id:'older',createdAt:100000,status:'completed',persisted:true,tools:[]}}});
    }""")
    expect(row.get_by_label("Needs input", exact=True)).to_be_visible()
    run["status"] = "completed"
    page.clock.fast_forward(15001)
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    page.clock.run_for(200)
    expect(row.get_by_label("Finished", exact=True)).to_be_visible()
    # An unavailable endpoint cannot make an unobserved session look finished.
    page.evaluate("import('/static/store.js').then(s => s.update({lives:{}}))")
    peer.discovery_overrides[endpoint] = ({"error": "Offline"}, 503)
    page.clock.fast_forward(15001)
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    page.clock.run_for(200)
    expect(row.locator(".session-activity")).to_have_count(0)
    assert page.locator(".alert").count() == 0


@pytest.mark.clock
def test_activity_pauses_hidden_tabs_and_coalesces_focus(page, live_app):
    peer = live_app[1]
    seed(peer)
    peer.discovery_overrides["/talaria/v1/activity"] = (
        {"data": [{"session_id": "notes", "status": "running"}]},
        200,
    )
    enable(page, peer)
    expect(page.get_by_label("Running", exact=True)).to_be_visible()
    page.evaluate("""() => {
      Object.defineProperty(document,'visibilityState',{configurable:true,value:'hidden'});
      document.dispatchEvent(new Event('visibilitychange'));
    }""")
    before = len([call for call in peer.calls if call[1] == "/talaria/v1/activity"])
    page.clock.fast_forward(60000)
    assert len([call for call in peer.calls if call[1] == "/talaria/v1/activity"]) == before
    peer.discovery_overrides["/talaria/v1/activity"][0]["data"][0]["status"] = "waiting_for_input"
    page.evaluate("""() => {
      Object.defineProperty(document,'visibilityState',{configurable:true,value:'visible'});
      document.dispatchEvent(new Event('visibilitychange'));
      for (let i=0;i<100;i++) window.dispatchEvent(new Event('focus'));
    }""")
    page.clock.run_for(200)
    expect(page.get_by_label("Needs input", exact=True)).to_be_visible()
    assert len([call for call in peer.calls if call[1] == "/talaria/v1/activity"]) == before + 1
