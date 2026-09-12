"""Export consistency and control capacity against isolated Hermes peers."""

import asyncio
import threading
from contextlib import AsyncExitStack
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from talaria import auth, hermes, transcripts
from talaria.app import create_app
from talaria.config import Settings

from .conftest import serve


class ExportPeer:
    def __init__(self):
        self.sessions = {}
        self.rows = {}
        self.redirects = {}
        self.calls = []
        self.mutate = lambda request: None
        self.counts = True

    def seed(self, sid, count=230, title="Fixture session"):
        self.sessions[sid] = {"id": sid, "title": title}
        self.rows[sid] = [
            {"id": index + 1, "session_id": sid, "role": "assistant", "content": f"Row {index}"}
            for index in range(count)
        ]

    def respond(self, request):
        self.calls.append((request.url.path, dict(request.url.params)))
        self.mutate(request)
        sid = request.url.path.split("/")[3]
        if request.url.path.endswith("/messages"):
            sid = self.redirects.get(sid, sid)
            rows = self.rows[sid]
            offset, limit = (int(request.url.params[key]) for key in ("offset", "limit"))
            if request.url.params["order"] == "latest":
                end = max(0, len(rows) - offset)
                rows = rows[max(0, end - limit) : end]
            else:
                rows = rows[offset : offset + limit]
            return httpx.Response(200, json={"session_id": sid, "data": rows})
        session = self.sessions[sid]
        return httpx.Response(
            200,
            json={
                "session": {
                    **session,
                    **({"message_count": len(self.rows[sid])} if self.counts else {}),
                }
            },
        )


@pytest.fixture
async def exports(tmp_path, monkeypatch):
    peer = ExportPeer()
    settings = Settings(hermes_url="http://synthetic.invalid", signing_key="synthetic-key")
    app = create_app(
        settings, tmp_path / "config.json", transport=httpx.MockTransport(peer.respond)
    )
    spools = []
    original = transcripts.SpooledTemporaryFile

    def spool(*args, **kwargs):
        result = original(*args, **kwargs)
        spools.append(result)
        return result

    monkeypatch.setattr(transcripts, "SpooledTemporaryFile", spool)
    try:
        async with httpx.AsyncClient(
            base_url="http://talaria.test", transport=httpx.ASGITransport(app=app)
        ) as client:
            client.cookies.set(auth.COOKIE, auth.issue_cookie(settings))
            yield client, peer, spools
    finally:
        await app.state.profiles.close()


@pytest.mark.parametrize("format_", ["json", "markdown"])
async def test_export_uses_canonical_metadata_and_filename(exports, format_):
    client, peer, spools = exports
    peer.seed("original", title="Before compression")
    title = "After compression 🦋"
    peer.seed("canonical", count=3, title=title)
    peer.redirects["original"] = "canonical"
    response = await client.get(f"/api/sessions/original/export?format={format_}")
    assert response.status_code == 200
    extension = "json" if format_ == "json" else "md"
    assert response.headers["content-disposition"].endswith(quote(title) + "." + extension)
    if format_ == "json":
        exported = response.json()
        assert exported["session"] == {"id": "canonical", "title": title, "message_count": 3}
        assert exported["messages"] == peer.rows["canonical"]
    else:
        assert response.text.startswith(f"# {title}\n")
    assert all(spool.closed for spool in spools)


@pytest.mark.parametrize("counts", [False, True])
@pytest.mark.parametrize("mutation", ["rewind_head", "replace_tail", "append"])
async def test_history_changes_fail_before_download_headers(exports, counts, mutation):
    client, peer, spools = exports
    peer.seed("changing")
    peer.counts = counts
    changed = False

    def mutate(request):
        nonlocal changed
        offset = "200" if mutation == "replace_tail" else "100"
        if changed or request.url.params.get("offset") != offset:
            return
        changed = True
        if mutation == "append":
            peer.rows["changing"].append({"id": 1000, "role": "assistant", "content": "New reply"})
            return
        keep = 50 if mutation == "rewind_head" else 120
        count = 1 if mutation == "rewind_head" else 110
        peer.rows["changing"] = peer.rows["changing"][:keep] + [
            {"id": 1000 + index, "role": "assistant", "content": "Replacement"}
            for index in range(count)
        ]

    peer.mutate = mutate
    response = await client.get("/api/sessions/changing/export?format=json")
    assert changed
    assert response.status_code == 409
    assert "changed during download" in response.json()["error"]
    assert "content-disposition" not in response.headers
    assert spools and all(spool.closed for spool in spools)


async def test_exposed_metadata_changes_are_checked_even_with_stable_boundary_rows(exports):
    client, peer, spools = exports
    peer.seed("activity")
    peer.sessions["activity"]["last_active"] = 1

    def mutate(request):
        if request.url.params.get("offset") == "100":
            peer.sessions["activity"]["last_active"] = 2

    peer.mutate = mutate
    response = await client.get("/api/sessions/activity/export")
    assert response.status_code == 409
    assert "content-disposition" not in response.headers
    assert all(spool.closed for spool in spools)


async def test_stable_exports_keep_every_row_without_optional_metadata(exports, monkeypatch):
    client, peer, spools = exports
    peer.seed("large", count=20)
    peer.counts = False
    for row in peer.rows["large"]:
        row["content"] = "🦋" * 300
    # Exercise adaptive pages without allocating multi-megabyte fixtures.
    monkeypatch.setattr(hermes, "MAX_RESPONSE", 6000)
    response = await client.get("/api/sessions/large/export?format=json")
    assert response.status_code == 200, response.text
    assert response.json()["messages"] == peer.rows["large"]
    assert any(int(params.get("limit", 100)) < 100 for _, params in peer.calls)
    assert all(spool.closed for spool in spools)


@pytest.mark.parametrize("failure", ["redirect", "upstream"])
async def test_later_page_failure_closes_the_unpublished_export(exports, failure):
    client, peer, spools = exports
    peer.seed("original")
    peer.seed("new-tip")

    def mutate(request):
        if request.url.params.get("offset") != "100":
            return
        if failure == "upstream":
            raise httpx.ReadError("Synthetic connection failure", request=request)
        peer.redirects["original"] = "new-tip"

    peer.mutate = mutate
    response = await client.get("/api/sessions/original/export")
    assert response.status_code == (409 if failure == "redirect" else 502)
    assert "content-disposition" not in response.headers
    assert spools and all(spool.closed for spool in spools)


@pytest.fixture
def capacity_server(tmp_path):
    opened, stopped, completed = set(), [], {}
    full = threading.Event()

    async def handle(request):
        rid = request.path_params["run_id"]
        if request.url.path.endswith("/events"):

            async def frames():
                completed[rid] = asyncio.Event()
                opened.add(rid)
                if len(opened) == 32:
                    full.set()
                yield 'data: {"event":"message.delta","delta":"Fixture"}\n\n'
                await completed[rid].wait()
                yield 'data: {"event":"run.cancelled"}\n\n'

            return StreamingResponse(frames(), media_type="text/event-stream")
        if request.method == "POST":
            stopped.append(rid)
            completed[rid].set()
        return JSONResponse({"run_id": rid, "status": "cancelled" if rid in stopped else "running"})

    peer = Starlette(
        routes=[
            Route("/v1/runs/{run_id}", handle),
            Route("/v1/runs/{run_id}/{action}", handle, methods=["GET", "POST"]),
        ]
    )
    upstream, upstream_thread, address = serve(peer)
    settings = Settings(hermes_url=address, signing_key="synthetic-signing-key")
    app = create_app(settings, tmp_path / "config.json")
    # Real httpcore pool exhaustion, with a short test deadline instead of 30 s.
    app.state.hermes.client.timeout = httpx.Timeout(3, pool=0.3)
    server, thread, url = serve(app)
    try:
        yield SimpleNamespace(url=url, settings=settings, full=full, stopped=stopped)
    finally:
        server.should_exit = True
        thread.join(4)
        upstream.should_exit = True
        upstream_thread.join(4)
        assert not thread.is_alive() and not upstream_thread.is_alive()


async def test_full_stream_capacity_leaves_status_and_stop_requests_usable(capacity_server):
    fixture = capacity_server
    async with (
        httpx.AsyncClient(
            base_url=fixture.url,
            timeout=3,
            trust_env=False,
            limits=httpx.Limits(max_connections=40),
        ) as client,
        AsyncExitStack() as streams,
    ):
        client.cookies.set(auth.COOKIE, auth.issue_cookie(fixture.settings))
        client.headers["X-Talaria-Request"] = "1"
        client.headers["X-CSRF-Token"] = (await client.get("/api/bootstrap")).json()["csrf"]
        for index in range(32):
            response = await streams.enter_async_context(
                client.stream("GET", f"/api/runs/run{index}/events")
            )
            assert response.status_code == 200
        assert await asyncio.to_thread(fixture.full.wait, 3)
        assert (await client.get("/api/runs/overflow/events")).status_code == 429
        status, stop = await asyncio.wait_for(
            asyncio.gather(
                client.get("/api/runs/run1"),
                client.post("/api/runs/run0/stop", json={}),
            ),
            2,
        )
        assert status.status_code == stop.status_code == 200
        assert status.json()["status"] == "running"
        assert stop.json()["status"] == "cancelled"
        assert fixture.stopped == ["run0"]
