"""Admission concurrency and capacity, with deliberately stalled upstream requests."""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from talaria import auth
from talaria.app import create_app
from talaria.config import Settings


@asynccontextmanager
async def admission_app(path, handler):
    async def transport(request):
        if request.method == "POST" and request.url.path.endswith("/runs"):
            return await handler(request)
        return httpx.Response(200, json={"platform": "hermes-agent", "features": {}})

    settings = Settings(
        hermes_url="http://hermes.test",
        api_key="synthetic-key",
        signing_key="synthetic-signing-key",
    )
    app = create_app(settings, path, transport=httpx.MockTransport(transport))
    profiles = app.state.profiles
    profiles.records["other"] = {
        "id": "other",
        "url": "http://hermes.test/p/other",
        "api_key": "synthetic-key",
        "label": "Other",
    }
    other = profiles.resolve("other")

    async def observe(channel):
        await asyncio.Event().wait()

    for child in (app, other):
        child.state.relay.observe = observe
    async with httpx.AsyncClient(
        base_url="http://talaria.test", transport=httpx.ASGITransport(app=app)
    ) as client:
        client.cookies.set(auth.COOKIE, auth.issue_cookie(settings))
        client.headers["X-Talaria-Request"] = "1"
        client.headers["X-CSRF-Token"] = (await client.get("/api/bootstrap")).json()["csrf"]
        try:
            yield client, app
        finally:
            await profiles.close()


@pytest.mark.parametrize("profile", ["default", "other"])
async def test_slow_admission_does_not_block_independent_sessions(tmp_path, profile):
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        data = json.loads(request.content)
        if data["input"] == "slow":
            entered.set()
            await release.wait()
        return httpx.Response(202, json={"run_id": data["session_id"]})

    async with admission_app(tmp_path / "config.json", handler) as (client, app):
        slow = asyncio.create_task(
            client.post("/api/runs", json={"session_id": "slow", "input": "slow"})
        )
        try:
            await asyncio.wait_for(entered.wait(), 2)
            fast = await asyncio.wait_for(
                client.post(
                    f"/api/runs?talaria_profile={profile}",
                    json={"session_id": "fast", "input": "fast"},
                ),
                2,
            )
            assert fast.status_code == 202
            assert not slow.done()
            # The old connection must remain alive until admission has finished.
            changed = await client.put(
                "/api/connection",
                json={
                    "url": "http://hermes.test",
                    "api_key": "replacement-key",
                },
            )
            assert changed.status_code == 409
            assert app.state.settings.api_key == "synthetic-key"
        finally:
            release.set()
            await slow
        assert app.state.profiles.pending_runs == 0


async def test_pending_admissions_count_toward_global_capacity_and_release_on_cancel(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def handler(request):
        nonlocal count
        count += 1
        if count == 16:
            entered.set()
        await release.wait()
        return httpx.Response(202, json={"run_id": json.loads(request.content)["session_id"]})

    async with admission_app(tmp_path / "config.json", handler) as (client, app):
        tasks = [
            asyncio.create_task(
                client.post(
                    f"/api/runs?talaria_profile={'other' if i % 2 else 'default'}",
                    json={"session_id": f"session-{i}", "input": "hello"},
                )
            )
            for i in range(16)
        ]
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert app.state.profiles.pending_runs == 16
            overflow = await client.post(
                "/api/runs", json={"session_id": "overflow", "input": "hello"}
            )
            assert overflow.status_code == 429
            assert count == 16
            # Removal sees the stalled request even before a run ID exists.
            assert (await client.delete("/api/profiles/other")).status_code == 409
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
            assert app.state.profiles.pending_runs == 15
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert app.state.profiles.pending_runs == 0
        assert (
            await client.post("/api/runs", json={"session_id": "last", "input": "hello"})
        ).status_code == 202
        assert (
            await client.post("/api/runs", json={"session_id": "extra", "input": "hello"})
        ).status_code == 429


@pytest.mark.parametrize("status,result", [(503, {}), (200, {"run_id": []})])
async def test_failed_admissions_release_capacity(tmp_path, status, result):
    async def handler(request):
        return httpx.Response(status, json=result)

    async with admission_app(tmp_path / "config.json", handler) as (client, app):
        for _ in range(20):
            response = await client.post(
                "/api/runs", json={"session_id": "failed", "input": "hello"}
            )
            assert response.status_code >= 500
        assert app.state.profiles.pending_runs == 0
        assert not app.state.relay.channels
