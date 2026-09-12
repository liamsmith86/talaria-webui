import ast
import json
import os
import subprocess
from pathlib import Path

import pytest
from playwright.sync_api import expect

from talaria.config import load, save
from talaria.hermes_plugin.observations import Observations, reasoning
from talaria.plugin_install import main as install_plugin

from .test_app import signed_in
from .test_browser import screenshot
from .test_conversation_features import seed


def open_seed(page, peer, count=4, extension=True):
    peer.extension = {} if extension else None
    sid = seed(peer, count=count)
    peer.sessions[sid]["source"] = "api_server"
    page.reload()
    page.get_by_role("link", name="Design notes", exact=True).click()
    expect(page.locator(".message")).to_have_count(count)
    return sid


def test_response_details_context_and_line_breaks(page, live_app):
    peer = live_app[1]
    open_seed(page, peer)
    page.get_by_role("button", name="Response details", exact=True).last.click()
    dialog = page.get_by_role("dialog", name="Response details", exact=True)
    expect(dialog).to_contain_text("actual-response-model")
    expect(dialog).to_contain_text("High")
    expect(dialog).to_contain_text("12,000")
    page.get_by_role("button", name="Close dialog").click()
    expect(page.locator(".context-indicator")).to_have_attribute(
        "aria-label", "9% context used · last request"
    )
    peer.messages["notes"][-1]["content"] = (
        "First line\nSecond line\nThird line\n\nNew paragraph\n\n```text\ncode one\ncode two\n```"
    )
    page.reload()
    markdown = page.locator(".message.assistant .markdown").last
    expect(markdown.locator("p")).to_have_count(2)
    expect(markdown.locator("p").first.locator("br")).to_have_count(2)
    expect(markdown.locator("code")).to_contain_text("code one\ncode two")
    assert markdown.locator("p").first.evaluate(
        "el => el.getBoundingClientRect().height > "
        "parseFloat(getComputedStyle(el).lineHeight) * 2.5"
    )


def test_missing_extension_keeps_chat_and_truthful_response_details(page, live_app):
    open_seed(page, live_app[1], count=2, extension=False)
    expect(page.get_by_role("button", name="Regenerate response", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Response details", exact=True).click()
    expect(page.get_by_role("dialog")).to_contain_text("Not reported")
    expect(page.get_by_role("dialog")).not_to_contain_text("hermes-test")
    page.get_by_role("button", name="Close dialog").click()
    expect(page.locator(".context-indicator")).to_have_count(0)


@pytest.mark.parametrize(
    "context,status",
    [
        (None, 200),
        ({"used": None, "maximum": 128000}, 200),
        ({"used": -1, "maximum": 128000}, 200),
        (None, 503),
    ],
)
def test_unknown_context_is_hidden_even_when_session_totals_exist(page, live_app, context, status):
    peer = live_app[1]
    peer.extension = {}
    sid = seed(peer, count=4)
    peer.sessions[sid].update(source="api_server", input_tokens=14355, api_call_count=3)
    peer.discovery_overrides[f"/talaria/v1/sessions/{sid}/context"] = (
        {"context": context},
        status,
    )
    page.reload()
    with page.expect_response(lambda response: f"/api/sessions/{sid}/context?" in response.url):
        page.get_by_role("link", name="Design notes", exact=True).click()
    for width in (1280, 390, 320):
        page.set_viewport_size({"width": width, "height": 844})
        expect(page.locator(".context-indicator")).to_have_count(0)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if context is None and status == 200:
            screenshot(page, f"context-unavailable-{width}")
    page.get_by_role("button", name="Session details", exact=True).click()
    dialog = page.get_by_role("dialog", name="Session details", exact=True)
    expect(dialog.locator(".usage-grid > div").filter(has_text="Input tokens")).to_contain_text(
        "14,355"
    )
    expect(dialog.locator(".usage-grid > div").filter(has_text="Model calls")).to_contain_text("3")


def test_edit_resend_reuses_run_transport_and_removes_following_turns(page, live_app):
    peer = live_app[1]
    sid = open_seed(page, peer)
    page.get_by_role("button", name="Edit and resend", exact=True).first.click()
    dialog = page.get_by_role("dialog", name="Edit and resend", exact=True)
    expect(dialog).to_contain_text("and 1 later turn")
    expect(page.get_by_label("Edit message", exact=True)).to_have_value("Message 000")
    page.get_by_label("Edit message", exact=True).fill("An edited question")
    assert peer.rewinds == 0
    dialog.get_by_role("button", name="Edit and resend", exact=True).click()
    expect(dialog).to_have_count(0)
    expect(page.get_by_text("What would you like to explore next?", exact=True)).to_be_visible()
    assert peer.rewinds == 1
    assert peer.messages[sid][0]["content"] == "An edited question"
    assert "Message 002" not in json.dumps(peer.messages[sid])
    assert list(peer.runs.values())[-1]["session_id"] == sid


def test_stale_edits_and_missing_images_never_rewind(page, live_app):
    peer = live_app[1]
    sid = open_seed(page, peer)
    page.get_by_role("button", name="Delete turn", exact=True).first.click()
    dialog = page.get_by_role("dialog", name="Delete turn", exact=True)
    expect(dialog).to_contain_text("and 1 later turn")
    peer.messages[sid].append({"id": 10, "role": "user", "content": "Arrived elsewhere"})
    dialog.get_by_role("button", name="Delete turn", exact=True).click()
    expect(dialog.get_by_role("alert")).to_contain_text("conflicts")
    assert peer.rewinds == 0 and len(peer.messages[sid]) == 5
    page.get_by_role("button", name="Close dialog").click()
    peer.messages[sid][0]["content"] = "Inspect this\n[screenshot]"
    page.reload()
    page.get_by_role("button", name="Edit and resend", exact=True).first.click()
    expect(page.get_by_role("alert")).to_contain_text("original images are not available")
    expect(
        page.get_by_role("dialog").get_by_role("button", name="Edit and resend", exact=True)
    ).to_be_disabled()
    assert peer.rewinds == 0


def test_regenerate_delete_and_mobile_toolbar(page, live_app):
    peer = live_app[1]
    sid = open_seed(page, peer, count=2)
    page.set_viewport_size({"width": 360, "height": 800})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    controls = page.locator(".composer-toolbar button").evaluate_all(
        "els => els.filter(e => e.offsetWidth).map(e => {"
        "const r=e.getBoundingClientRect();return [r.left,r.right]})"
    )
    assert all(left >= 0 and right <= 360 for left, right in controls)
    page.get_by_role("button", name="Regenerate response", exact=True).click()
    dialog = page.get_by_role("dialog", name="Regenerate response", exact=True)
    dialog.get_by_role("button", name="Regenerate response", exact=True).click()
    expect(dialog).to_have_count(0)
    expect(page.get_by_text("What would you like to explore next?", exact=True)).to_be_visible()
    assert list(peer.runs.values())[-1]["input"] == "Message 000"
    page.get_by_role("button", name="Delete turn", exact=True).first.click()
    page.get_by_role("dialog").get_by_role("button", name="Delete turn", exact=True).click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(page.locator(".message")).to_have_count(0)
    assert not peer.messages[sid] and peer.rewinds == 2


def test_replacement_validation_precedes_native_mutation(live_app):
    peer = live_app[1]
    peer.extension = {}
    sid = seed(peer, count=2)
    with signed_in(live_app[0]) as client:
        url = f"/api/sessions/{sid}/rewind"
        preview = client.post(url, json={"message_id": 1, "preview": True}).json()
        response = client.post(
            url,
            json={
                "message_id": 1,
                "revision": preview["revision"],
                "replacement": {"input": "retry", "images": [{"url": "bad"}]},
            },
        )
        assert response.status_code == 400 and peer.rewinds == 0
        assert client.post(url, json={"message_id": True}).status_code == 400
        assert (
            client.post(url, json={"message_id": 1}, headers={"X-CSRF-Token": "bad"}).status_code
            == 403
        )


def test_extended_model_limits_and_standard_catalog_fallback(page, live_app):
    peer = live_app[1]
    peer.extension = {"model_details": True}
    peer.discovery_overrides["/talaria/v1/models"] = (
        {
            "model": "limited-model",
            "provider": "test",
            "providers": [
                {
                    "slug": "test",
                    "is_current": True,
                    "models": ["limited-model"],
                    "capabilities": {
                        "limited-model": {
                            "reasoning": True,
                            "supported_efforts": ["high", "xhigh"],
                        }
                    },
                }
            ],
        },
        200,
    )
    page.reload()
    expect(page.get_by_role("button", name="Choose model", exact=True)).to_contain_text(
        "limited-model"
    )
    page.get_by_role("button", name="Choose reasoning", exact=True).click()
    expect(page.get_by_role("button", name="Low", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Extra high", exact=True)).to_be_visible()
    page.get_by_role("button", name="High", exact=True).click()
    with signed_in(live_app[0]) as client:
        # A disabled/incompatible plugin and malformed enrichment keep the picker usable.
        for result in (({"error": "Unavailable"}, 503), ({"future_shape": True}, 200)):
            peer.discovery_overrides["/talaria/v1/models"] = result
            catalog = client.get("/api/models?refresh=1")
            assert catalog.status_code == 200 and catalog.json()["model"] == peer.default_model
        assert ("GET", "/talaria/v1/models", {"refresh": "1"}) in peer.calls
        assert ("GET", "/api/model/options", {"refresh": "1"}) in peer.calls


def test_legacy_path_migration_and_plugin_export(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"hermes_home": "/root/.hermes"}))
    settings = load(config)
    assert not hasattr(settings, "hermes_home")
    save(config, settings)
    assert "hermes_home" not in config.read_text()
    install_plugin(["--home", str(tmp_path / "hermes")])
    plugin = tmp_path / "hermes/plugins/talaria"
    assert (plugin / "plugin.yaml").is_file()
    assert not (plugin / "__pycache__").exists()
    for file in plugin.glob("*.py"):
        for node in ast.walk(ast.parse(file.read_text())):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] != "talaria" for alias in node.names)
            if isinstance(node, ast.ImportFrom) and not node.level:
                assert (node.module or "").split(".")[0] != "talaria"


def test_observation_store_is_bounded_and_profile_scoped(tmp_path):
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    first, second = Observations(tmp_path / "one"), Observations(tmp_path / "two")
    first.save("same-id", 1, {"model": "recorded-model"})
    assert second.read("same-id") is None
    assert first.read("same-id", 1)["model"] == "recorded-model"
    for i in range(150):
        first.before(task_id=str(i), turn_id=str(i), request={"reasoning_effort": "high"})
    assert len(first.pending) == 128
    assert all("request" not in value for value in first.pending.values())
    assert (tmp_path / "one/talaria/observations.db").stat().st_mode & 0o077 == 0
    assert reasoning({"extra_body": {"reasoning": {"enabled": False}}}) == "none"
    assert reasoning({}) is None


@pytest.mark.hermes
@pytest.mark.skipif(
    not os.getenv("HERMES_SOURCE"), reason="Set HERMES_SOURCE for native contract checks"
)
@pytest.mark.parametrize(
    "contract,provider",
    [
        ("hermes_contract.py", "openai"),
        ("hermes_restart_contract.py", "openai"),
        ("hermes_context_contract.py", "openai"),
        ("hermes_context_contract.py", "openrouter"),
        ("hermes_branch_contract.py", "openai"),
    ],
)
def test_native_hermes_contract(tmp_path, contract, provider):
    source = Path(os.environ["HERMES_SOURCE"])
    result = subprocess.run(
        [
            str(source / "venv/bin/python3"),
            str(Path(__file__).with_name(contract)),
            str(tmp_path),
            provider,
        ],
        env={
            **os.environ,
            "HERMES_HOME": str(tmp_path),
            "PYTHONPATH": f"{source}:{Path(__file__).resolve().parents[1] / 'src'}",
        },
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    if contract == "hermes_context_contract.py":
        expected = Path(__file__).with_name("fixtures") / "hermes-completed.json"
        captured = json.loads((tmp_path / "stream.json").read_text())
        assert captured == json.loads(expected.read_text())


@pytest.mark.parametrize("used,label", [(43565, "43,565 tokens"), (0, "0 tokens")])
def test_context_tokens_remain_visible_without_a_model_limit(page, live_app, used, label):
    peer = live_app[1]
    peer.discovery_overrides["/talaria/v1/sessions/notes/context"] = (
        {"context": {"used": used, "maximum": None}},
        200,
    )
    open_seed(page, peer)
    indicator = page.locator(".context-indicator")
    expect(indicator).to_contain_text(label)
    for width in (1280, 390, 320):
        page.set_viewport_size({"width": width, "height": 844})
        expect(indicator).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        screenshot(page, f"context-tokens-{used}-{width}")
    indicator.click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(page.get_by_role("button", name="Context usage", exact=True)).to_have_count(0)
    assert indicator.evaluate("el => el.tabIndex") == -1


def test_inherited_reasoning_is_shown_without_overriding_hermes(page, live_app):
    peer = live_app[1]
    peer.extension = {"model_details": True}
    peer.discovery_overrides["/talaria/v1/models"] = (
        {
            "model": "configured-model",
            "provider": "test",
            "configured_reasoning": "high",
            "providers": [
                {
                    "slug": "test",
                    "is_current": True,
                    "models": ["configured-model", "other-model"],
                    "capabilities": {"other-model": {"configured_reasoning": "none"}},
                }
            ],
        },
        200,
    )
    page.reload()
    chooser = page.get_by_role("button", name="Choose reasoning", exact=True)
    expect(chooser).to_contain_text("High")
    chooser.click()
    expect(page.locator(".reasoning-list .model-default")).to_contain_text("High")
    expect(page.locator(".reasoning-list .model-default")).to_have_attribute("aria-pressed", "true")
    page.get_by_role("button", name="Close dialog").click()
    page.get_by_label("Message Hermes").fill("Use configured reasoning")
    page.get_by_role("button", name="Send message", exact=True).click()
    expect(page.get_by_text("What would you like to explore next?", exact=True)).to_be_visible()
    assert list(peer.runs.values())[-1]["model_options"] is None
    page.evaluate("""async () => {
        const {chooseModel}=await import('/static/store.js');
        chooseModel({id:'other-model',provider:'test'});
    }""")
    expect(chooser).to_contain_text("Off")
