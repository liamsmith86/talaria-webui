"""Measure independent admissions behind a 400 ms Hermes request (isolated ASGI).

Run: uv run python -m benchmarks.admission
"""

import asyncio
import json
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

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


async def measure(path, profile):
    entered = asyncio.Event()

    async def handler(request):
        data = json.loads(request.content)
        if data["input"] == "slow":
            entered.set()
            await asyncio.sleep(0.4)
        return httpx.Response(202, json={"run_id": data["session_id"]})

    async with admission_app(path, handler) as (client, _):
        slow = asyncio.create_task(
            client.post("/api/runs", json={"session_id": "slow", "input": "slow"})
        )
        await entered.wait()
        start = time.perf_counter()
        responses = await asyncio.gather(
            *(
                client.post(
                    f"/api/runs?talaria_profile={profile}",
                    json={"session_id": f"fast-{i}", "input": "fast"},
                )
                for i in range(8)
            )
        )
        elapsed = (time.perf_counter() - start) * 1000
        assert all(r.status_code == 202 for r in responses)
        await slow
        return {"profile": profile, "eight_fast_admissions_ms": elapsed, "slow_upstream_ms": 400}


async def main():
    with tempfile.TemporaryDirectory(prefix="talaria-admission-") as folder:
        for profile in ("default", "other"):
            print(json.dumps(await measure(Path(folder) / "config.json", profile)))


if __name__ == "__main__":
    asyncio.run(main())
