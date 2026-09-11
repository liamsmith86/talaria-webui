"""User-driven checks, ambiguous requests, restart recovery, and restricted HTTP operations."""

import io
import json
import secrets
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from playwright.sync_api import expect

from talaria import control, installation
from talaria.deployment import read_json
from talaria.supervisor_worker import execute

from .test_app import signed_in
from .test_deployment import A, B, release
from .test_deployment import deployment as deployment_fixture

deployment = deployment_fixture


@pytest.mark.parametrize(
    "data",
    [
        {"action": "shell", "id": "a" * 32},
        {"action": {}, "id": "a" * 32},
        {"action": "update", "id": "a" * 32},
        {"action": "update", "id": "a" * 32, "expect": "--command"},
        {"action": "check", "id": "a" * 32, "repository": "elsewhere"},
    ],
)
def test_control_rejects_unrecognized_or_unbounded_operations(data):
    with pytest.raises(control.ControlError):
        control.validate(data)


@pytest.mark.parametrize("data", [b"{}", b"[]\n", b"x" * 4097, b"[" * 1500 + b"]" * 1500 + b"\n"])
def test_control_rejects_incomplete_and_malformed_frames(data):
    with pytest.raises(control.ControlError):
        control.receive(io.BytesIO(data))


def test_update_routes_require_login_csrf_and_fixed_action(live_app, tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    (root / control.SOCKET).touch()
    monkeypatch.setattr(installation, "managed_root", lambda: root)
    calls = []
    monkeypatch.setattr(control, "request", lambda root, data: calls.append(data) or data)
    url = live_app[0] + "/api/installation/update"
    payload = {"id": secrets.token_hex(16), "expect": B}
    assert httpx.post(url, json=payload).status_code == 401
    with signed_in(live_app[0]) as client:
        token = client.headers.pop("X-CSRF-Token")
        assert client.post("/api/installation/update", json=payload).status_code == 403
        client.headers["X-CSRF-Token"] = token
        assert client.post("/api/installation/rollback", json=payload).status_code == 404
        assert (
            client.post("/api/installation/update", json={**payload, "command": "bad"}).status_code
            == 400
        )
        assert not calls
        assert client.post("/api/installation/update", json=payload).status_code == 202
        assert calls == [{**payload, "action": "update"}]
        live_app[2].state.development = True
        assert client.post("/api/installation/update", json=payload).status_code == 409


def test_failed_activation_reports_failure_and_restores_old_release(deployment, monkeypatch):
    from talaria.deployment import Deployment, DeploymentError, selected

    release(deployment.root, B)
    monkeypatch.setattr(Deployment, "fetch", lambda self: B)
    monkeypatch.setattr(Deployment, "stage", lambda self, _: f"releases/{B}")
    restarts = []

    def health(self, target):
        if target.endswith(B):
            raise DeploymentError("Synthetic failure with private repository information")

    monkeypatch.setattr(Deployment, "verify_running", health)
    operation = {"action": "update", "id": "c" * 32, "expect": B}
    with pytest.raises(DeploymentError):
        execute(deployment.root, operation, lambda: restarts.append(True))
    assert len(restarts) == 2
    assert selected(deployment.root, "current") == f"releases/{A}"
    state = read_json(deployment.root / control.JOB)
    assert state["status"] == "finishing" and "private" not in json.dumps(state)


@pytest.fixture
def update_ui(page, monkeypatch):
    info = {
        "environment": "production",
        "version": "0.3.2",
        "commit": A,
        "managed": True,
        "can_update": True,
        "branch": "main",
        "update": {
            "error": None,
            "available": True,
            "checked_at": "2026-09-08T21:00:00Z",
            "latest_commit": B,
        },
        "operation": {},
    }
    calls = []
    monkeypatch.setattr(installation, "public_info", lambda _: info)

    def check(route):
        body = route.request.post_data_json
        calls.append(body)
        info["update"]["checked_at"] = datetime.now(UTC).isoformat()
        info["operation"] = {**body, "action": "check", "status": "completed"}
        route.fulfill(json=info["operation"])

    page.route("**/api/installation/check", check)
    page.get_by_role("button", name="Settings").click()
    page.get_by_role("tab", name="Talaria", exact=True).click()
    expect(page.get_by_role("button", name="Check for updates", exact=True)).to_be_enabled()
    return info, calls


def test_reopening_uses_cooldown_but_manual_check_is_immediate(page, update_ui):
    info, calls = update_ui
    assert len(calls) == 1
    expect(page.get_by_role("tabpanel")).not_to_contain_text("sudo")
    page.get_by_role("tab", name="Talaria", exact=True).click()
    expect(page.get_by_role("button", name="Check for updates", exact=True)).to_be_enabled()
    assert len(calls) == 1
    info["update"]["available"] = False
    page.get_by_role("button", name="Check for updates", exact=True).click()
    expect(page.get_by_role("tabpanel")).to_contain_text("Up to date")
    assert len(calls) == 2
    expect(page.get_by_role("button", name="Update", exact=True)).to_have_count(0)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")


def test_automatic_check_resumes_after_ten_minutes(page, update_ui):
    _, calls = update_ui
    for minutes, count in [(9, 1), (10, 2)]:
        page.clock.set_fixed_time(datetime.now(UTC) + timedelta(minutes=minutes))
        page.get_by_role("tab", name="Talaria", exact=True).click()
        expect(page.get_by_role("button", name="Check for updates", exact=True)).to_be_enabled()
        assert len(calls) == count


def test_recent_server_check_survives_page_reload(page, update_ui):
    _, calls = update_ui
    page.reload()
    page.get_by_role("button", name="Settings").click()
    page.get_by_role("tab", name="Talaria", exact=True).click()
    expect(page.get_by_role("button", name="Check for updates", exact=True)).to_be_enabled()
    assert len(calls) == 1


def test_failed_check_does_not_repeat_on_every_tab_open(page, update_ui):
    info, calls = update_ui
    # No successful server timestamp: the current page still remembers its attempt.
    info["update"]["checked_at"] = None
    info["update"]["error"] = "Could not reach the update server."
    page.get_by_role("tab", name="Talaria", exact=True).click()
    expect(page.get_by_role("alert")).to_have_text(info["update"]["error"])
    expect(page.get_by_role("button", name="Check for updates", exact=True)).to_be_enabled()
    assert len(calls) == 1


def test_lost_update_ack_is_not_resent_and_reload_waits_for_new_build(page, update_ui):
    info, _ = update_ui
    sent = []
    page.evaluate("sessionStorage.setItem('reloads', '0')")
    page.add_init_script(
        "sessionStorage.setItem('reloads', Number(sessionStorage.getItem('reloads')) + 1)"
    )

    def update(route):
        operation = {
            **route.request.post_data_json,
            "action": "update",
            "status": "running",
            "phase": "building",
        }
        sent.append(operation)
        info["operation"] = operation
        route.abort("connectionreset")

    page.route("**/api/installation/update", update)
    page.get_by_role("button", name="Update", exact=True).click()
    expect(page.get_by_role("button", name="Update", exact=True)).to_be_disabled()
    expect(page.get_by_role("status").filter(has_text="Preparing update")).to_be_visible()
    assert len(sent) == 1 and sent[0]["expect"] == B
    page.get_by_role("tab", name="Talaria", exact=True).click()
    expect(page.get_by_role("status").filter(has_text="Preparing update")).to_be_visible()
    assert len(sent) == 1
    info["operation"]["status"] = "completed"
    page.wait_for_timeout(1200)
    assert page.evaluate("sessionStorage.getItem('reloads')") == "0"
    info["commit"] = B
    page.wait_for_function("() => sessionStorage.getItem('reloads') === '1'")
    assert len(sent) == 1


def test_update_failure_stays_visible_without_reloading(page, update_ui):
    info, _ = update_ui

    def update(route):
        info["operation"] = {
            **route.request.post_data_json,
            "action": "update",
            "status": "failed",
            "error": "Previous release restored.",
        }
        route.fulfill(json=info["operation"])

    page.route("**/api/installation/update", update)
    page.get_by_role("button", name="Update", exact=True).click()
    expect(page.get_by_role("alert")).to_have_text("Previous release restored.")
    expect(page.get_by_role("button", name="Check for updates", exact=True)).to_be_enabled()
