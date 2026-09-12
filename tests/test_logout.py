"""Copied credentials stay revoked across profiles, restarts, and failed writes."""

import asyncio
import json
import stat
import threading
import time
from contextlib import AsyncExitStack

import httpx
import pytest

from talaria import auth, config
from talaria.app import create_app
from talaria.hermes import APIError


@pytest.fixture
async def sessions(tmp_path):
    settings = config.Settings(
        password_hash=auth.hash_password("synthetic-password"), signing_key="key"
    )
    path = tmp_path / "config.json"
    config.save(path, settings)
    profile = "a" * 32
    config.save_data(
        path.with_name("profiles.json"),
        {
            "version": 1,
            "profiles": [
                {
                    "id": profile,
                    "label": "Second profile",
                    "url": "http://synthetic.invalid",
                    "api_key": "key",
                }
            ],
        },
    )
    apps = []
    async with AsyncExitStack() as stack:

        async def client(*, app=None):
            if app is None:
                app = create_app(
                    settings,
                    path,
                    transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
                )
                apps.append(app)
            peer = await stack.enter_async_context(
                httpx.AsyncClient(
                    base_url="http://talaria.test",
                    transport=httpx.ASGITransport(app=app),
                    headers={"X-Talaria-Request": "1"},
                )
            )
            return peer, app

        yield client, path, settings, profile
    for app in apps:
        await app.state.profiles.close()


async def login(client):
    assert (
        await client.post("/api/login", json={"password": "synthetic-password"})
    ).status_code == 200
    client.headers["X-CSRF-Token"] = (await client.get("/api/bootstrap")).json()["csrf"]
    return client.cookies.get(auth.COOKIE)


@pytest.mark.parametrize("via_profile", [False, True])
async def test_logout_revokes_copies_across_profiles_and_restart_only_for_this_token(
    sessions, via_profile
):
    make, path, _, profile = sessions
    owner, app = await make()
    token = await login(owner)
    other, _ = await make(app=app)
    other_token = await login(other)
    profile_query = f"?talaria_profile={profile}"
    # Instantiate the cached profile app before logout to test shared revocations.
    assert (await owner.get("/api/connection" + profile_query)).status_code == 200
    response = await owner.post("/api/logout" + (profile_query if via_profile else ""), json={})
    assert response.status_code == 200
    assert not (await owner.get("/api/bootstrap")).json()["authenticated"]
    assert (await other.get("/api/bootstrap")).json()["authenticated"]
    copy, _ = await make(app=app)
    copy.cookies.set(auth.COOKIE, token)
    for route in ["/api/connection", "/api/connection" + profile_query]:
        assert (await copy.get(route)).status_code == 401
    restarted, _ = await make()
    restarted.cookies.set(auth.COOKIE, token)
    assert not (await restarted.get("/api/bootstrap")).json()["authenticated"]
    assert (await restarted.post("/api/logout", json={})).status_code == 401
    restarted.cookies.set(auth.COOKIE, other_token)
    assert (await restarted.get("/api/bootstrap")).json()["authenticated"]
    from talaria import auth_sessions

    saved = auth_sessions.revocation_path(path)
    assert token not in saved.read_text() and other_token not in saved.read_text()
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600


async def test_failed_logout_save_remains_visible_and_can_be_retried(sessions, monkeypatch):
    from talaria import auth_sessions

    make, _, _, _ = sessions
    client, app = await make()
    token = await login(client)
    original = auth_sessions.save_data

    def fail(*args):
        raise OSError("Synthetic full filesystem")

    monkeypatch.setattr(auth_sessions, "save_data", fail)
    response = await client.post("/api/logout", json={})
    assert response.status_code == 503
    assert "Could not save your sign-out" in response.json()["error"]
    assert client.cookies.get(auth.COOKIE) == token
    assert not app.state.revocations.contains(token)
    assert not app.state.profiles.writes
    monkeypatch.setattr(auth_sessions, "save_data", original)
    assert (await client.post("/api/logout", json={})).status_code == 200
    assert app.state.revocations.contains(token)


async def test_bad_csrf_does_not_revoke_a_valid_login(sessions):
    from talaria import auth_sessions

    make, path, _, _ = sessions
    client, _ = await make()
    await login(client)
    response = await client.post("/api/logout", json={}, headers={"X-CSRF-Token": "wrong"})
    assert response.status_code == 403
    assert (await client.get("/api/bootstrap")).json()["authenticated"]
    assert not auth_sessions.revocation_path(path).exists()


async def test_concurrent_logout_and_cancelled_request_finish_persisting(sessions, monkeypatch):
    from talaria import auth_sessions

    make, path, settings, _ = sessions
    first, app = await make()
    token = await login(first)
    second, _ = await make(app=app)
    other = await login(second)
    entered, release = threading.Event(), threading.Event()
    original = auth_sessions.save_data

    def delayed(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(auth_sessions, "save_data", delayed)
    task = asyncio.create_task(first.post("/api/logout", json={}))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        other_task = asyncio.create_task(second.post("/api/logout", json={}))
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await other_task).status_code == 200
    fresh = auth_sessions.Revocations(path)
    assert fresh.contains(token) and fresh.contains(other)
    assert not fresh.contains(auth.issue_cookie(settings))
    assert not app.state.profiles.writes


async def test_revocations_are_bounded_without_evicting_live_entries(tmp_path, monkeypatch):
    from talaria import auth_sessions

    monkeypatch.setattr(auth_sessions, "MAX_REVOCATIONS", 2)
    path = tmp_path / "custom.json"
    store = auth_sessions.Revocations(path)
    tokens = [f"{int(time.time())}.{index}.signature" for index in range(3)]
    for token in tokens[:2]:
        await store.revoke(token, auth.TTL)
    with pytest.raises(APIError, match="Could not sign out"):
        await store.revoke(tokens[2], auth.TTL)
    assert all(auth_sessions.Revocations(path).contains(token) for token in tokens[:2])
    assert not store.contains(tokens[2])
    assert not auth_sessions.Revocations(tmp_path / "another.json").contains(tokens[0])
    # Only expired revocations can be removed to admit another sign-out.
    monkeypatch.setattr(
        auth_sessions.time, "time", lambda: int(tokens[0].split(".")[0]) + auth.TTL + 1
    )
    await store.revoke("9999999999.new.signature", auth.TTL)
    assert len(json.loads(store.path.read_text())["revoked"]) == 1


@pytest.mark.parametrize(
    "raw", ["{", "[]", '{"version": 2, "revoked": {}}', '{"version": 1, "revoked": {"bad": 123}}']
)
def test_invalid_revocation_file_fails_closed(tmp_path, raw):
    from talaria import auth_sessions

    path = tmp_path / "config.json"
    auth_sessions.revocation_path(path).write_text(raw)
    with pytest.raises(ValueError, match="Cannot read sign-out history"):
        auth_sessions.Revocations(path)
