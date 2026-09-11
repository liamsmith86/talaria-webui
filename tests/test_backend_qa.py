"""Backend regressions using isolated transports; no installed Hermes service is contacted."""

import asyncio
import json

import httpx
import pytest
from starlette.requests import ClientDisconnect, Request

from talaria import auth, transcripts
from talaria.app import create_app
from talaria.config import Settings
from talaria.extensions import numeric
from talaria.hermes import APIError, Hermes
from talaria.relay import Channel, Relay

from .fake_hermes import KEY, FakeHermes


@pytest.fixture
async def backend(tmp_path):
    peer = FakeHermes()
    settings = Settings(
        hermes_url="http://isolated-hermes.test", api_key=KEY, signing_key="isolated-signing-key"
    )
    app = create_app(
        settings, tmp_path / "config.json", transport=httpx.ASGITransport(app=peer.app)
    )
    async with httpx.AsyncClient(
        base_url="http://talaria.test", transport=httpx.ASGITransport(app=app)
    ) as client:
        client.cookies.set(auth.COOKIE, auth.issue_cookie(settings))
        client.headers["X-Talaria-Request"] = "1"
        client.headers["X-CSRF-Token"] = (await client.get("/api/bootstrap")).json()["csrf"]
        yield client, app, peer
    await app.state.profiles.close()


@pytest.mark.parametrize("status", [404, 409, 503])
async def test_fork_only_falls_back_when_the_plugin_endpoint_is_missing(backend, status):
    client, _, peer = backend
    peer.sessions["original"] = {"id": "original", "source": "discord"}
    peer.messages["original"] = [{"id": 1, "role": "user", "content": "Original"}]
    peer.discovery_overrides["/talaria/v1/sessions/original/fork"] = (
        {"error": "Unavailable"}, status,
    )
    response = await client.post("/api/sessions/original/fork", json={"title": "Copy"})
    native_calls = [c for c in peer.calls if c[0:2] == ("POST", "/api/sessions/original/fork")]
    assert len(native_calls) == (1 if status == 404 else 0)
    assert len(peer.sessions) == (2 if status == 404 else 1)
    assert response.status_code == (201 if status == 404 else 409 if status == 409 else 502)


@pytest.mark.parametrize("plugin", [False, True])
async def test_default_branch_names_are_chosen_by_hermes(backend, plugin):
    client, _, peer = backend
    peer.extension = {"session_fork": True} if plugin else None
    peer.sessions["original"] = {"id": "original", "title": "Original", "source": "discord"}
    peer.messages["original"] = []
    for number in (2, 3):
        response = await client.post("/api/sessions/original/fork", json={})
        assert response.status_code == 201, response.text
        assert response.json()["title"] == f"Original #{number}"
    assert len(peer.sessions) == 3


@pytest.mark.parametrize("fail", [False, True])
async def test_capability_reads_overlap_and_do_not_outlive_the_request(backend, fail):
    client, app, _ = backend
    required, optional, release, cancelled = (asyncio.Event() for _ in range(4))

    async def request(method, path, **kwargs):
        if path == "/v1/capabilities":
            required.set()
            await release.wait()
            if fail:
                raise APIError("Required discovery failed")
            return {"features": {"session_resources": True}}
        optional.set()
        try:
            await (asyncio.Event() if fail else release).wait()
            return {"version": 1, "context_usage": True}
        finally:
            cancelled.set()

    app.state.hermes.request = request
    task = asyncio.create_task(client.get("/api/capabilities"))
    try:
        await asyncio.wait_for(asyncio.gather(required.wait(), optional.wait()), 1)
        assert not task.done()
        release.set()
        response = await asyncio.wait_for(task, 1)
        assert response.status_code == (502 if fail else 200)
        assert cancelled.is_set()
        if not fail:
            assert response.json()["talaria_extensions"]["context_usage"] is True
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_export_yields_between_messages_and_closes_on_cancellation(backend, monkeypatch):
    client, _, peer = backend
    sid = "cooperative-export"
    peer.sessions[sid] = {"id": sid, "title": "Export"}
    peer.messages[sid] = [
        {"id": i + 1, "role": "assistant", "content": "Text" * 1000} for i in range(100)
    ]
    original = transcripts.markdown_message
    original_spool = transcripts.SpooledTemporaryFile
    spools = []
    started = asyncio.Event()
    written = 0

    def format_message(message):
        nonlocal written
        written += 1
        started.set()
        return original(message)

    def new_spool(*args, **kwargs):
        spool = original_spool(*args, **kwargs)
        spools.append(spool)
        return spool

    monkeypatch.setattr(transcripts, "markdown_message", format_message)
    monkeypatch.setattr(transcripts, "SpooledTemporaryFile", new_spool)
    task = asyncio.create_task(client.get(f"/api/sessions/{sid}/export"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        # Even an immediately available upstream page cannot monopolize the
        # event loop until every message has been serialized.
        assert written < 100
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert spools and all(spool.closed for spool in spools)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"text":"\xff"}',
        b'{"value":NaN}',
        b'{"value":1e999}',
        b"[" * 2000 + b"]" * 2000,
        b'{"text":"\\ud800"}',
    ],
    ids=["invalid-utf8", "nan", "infinity", "deep-nesting", "unpaired-surrogate"],
)
async def test_malformed_upstream_json_has_a_controlled_error(raw):
    client = Hermes(
        "http://isolated-hermes.test",
        "",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=raw)),
    )
    try:
        with pytest.raises(APIError):
            await client.request("GET", "/v1/capabilities")
    finally:
        await client.close()


@pytest.mark.parametrize("result", [None, [], "unexpected"])
@pytest.mark.parametrize(
    "method,path,upstream,payload",
    [
        ("GET", "/api/capabilities", "/v1/capabilities", None),
        ("PUT", "/api/connection", "/v1/capabilities", {"url": "http://isolated-hermes.test"}),
        (
            "POST",
            "/api/profiles",
            "/v1/capabilities",
            {"url": "http://other.test", "api_key": KEY, "label": "Other"},
        ),
        ("POST", "/api/sessions", "/api/sessions", {"title": "Example"}),
        ("POST", "/api/sessions/example/fork", "/api/sessions/example/fork", {}),
        ("POST", "/api/runs", "/talaria/v1/runs", {"session_id": "example", "input": "Test"}),
        ("GET", "/api/sessions/example/context", "/talaria/v1/sessions/example/context", None),
    ],
)
async def test_unexpected_response_envelopes_do_not_crash(
    backend, method, path, upstream, payload, result
):
    client, _, peer = backend
    peer.discovery_overrides[upstream] = (result, 200)
    response = await client.request(method, path, json=payload)
    assert response.status_code == 502
    assert response.json()["code"] == "upstream_error"


@pytest.mark.parametrize("choice", [[], {}, None, True])
async def test_malformed_approval_choices_are_rejected_before_upstream(backend, choice):
    client, _, peer = backend
    response = await client.post("/api/runs/example/approval", json={"choice": choice})
    assert response.status_code == 400
    assert peer.calls == []


@pytest.mark.parametrize(
    "raw",
    [b'{"title":"\\ud800"}', b'{"value":NaN}', b'{"nested":' + b"[" * 2000 + b"]" * 2000 + b"}"],
    ids=["unpaired-surrogate", "nan", "deep-nesting"],
)
async def test_invalid_json_input_cannot_escape_the_request_boundary(backend, raw):
    client, _, peer = backend
    response = await client.post("/api/sessions", content=raw)
    assert response.status_code == 400
    assert peer.calls == []


async def test_non_ascii_csrf_is_rejected_without_a_server_error(backend):
    client, _, peer = backend
    response = await client.post(
        "/api/sessions", json={"title": "Example"}, headers={b"x-csrf-token": b"\xff"}
    )
    assert response.status_code == 403
    assert peer.calls == []


@pytest.mark.parametrize("key", ["snowman-\u2603", "bad\x7fkey", "bad key"])
async def test_invalid_bearer_credentials_are_rejected_before_connecting(backend, key):
    client, _, peer = backend
    response = await client.post(
        "/api/connection/test", json={"url": "http://isolated-hermes.test", "api_key": key}
    )
    assert response.status_code == 400
    assert peer.calls == []


@pytest.mark.parametrize("missing", ["label", "api_key"])
async def test_incomplete_saved_profiles_keep_the_default_connection_available(tmp_path, missing):
    record = {"id": "a" * 32, "label": "Example", "api_key": "test-key", "url": "http://other.test"}
    del record[missing]
    (tmp_path / "profiles.json").write_text(json.dumps({"version": 1, "profiles": [record]}))
    app = create_app(Settings(), tmp_path / "config.json")
    try:
        public = app.state.profiles.public()
        assert public["error"]
        assert [row["id"] for row in public["profiles"]] == ["default"]
    finally:
        await app.state.profiles.close()


async def test_connection_save_preserves_inflight_discovery(backend, monkeypatch):
    client, app, _ = backend
    started, release = asyncio.Event(), asyncio.Event()
    old_client = app.state.hermes
    original = old_client.request

    async def delayed(method, path, **kwargs):
        if path == "/health/detailed":
            started.set()
            await release.wait()
        return await original(method, path, **kwargs)

    monkeypatch.setattr(old_client, "request", delayed)
    pending = asyncio.create_task(client.get("/api/agent"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        response = await client.put("/api/connection", json={"url": "http://isolated-hermes.test"})
        assert response.status_code == 409
        assert not old_client.client.is_closed
    finally:
        release.set()
        await pending
    assert (
        await client.put("/api/connection", json={"url": "http://isolated-hermes.test"})
    ).status_code == 200


async def test_connection_commit_does_not_admit_new_streams(backend, monkeypatch):
    client, app, peer = backend
    started, release = asyncio.Event(), asyncio.Event()
    original = app.state.profiles.save_settings

    async def delayed(*args):
        started.set()
        await release.wait()
        await original(*args)

    monkeypatch.setattr(app.state.profiles, "save_settings", delayed)
    pending = asyncio.create_task(
        client.put("/api/connection", json={"url": "http://isolated-hermes.test"})
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        response = await client.get("/api/runs/example/events")
        assert response.status_code == 409
        assert not any("/runs/" in path for _, path, _ in peer.calls)
    finally:
        release.set()
        await pending
    assert app.state.profiles.inflight.get("default", 0) == 0


async def test_cancelled_connection_save_finishes_disk_and_runtime_update(backend, monkeypatch):
    client, app, peer = backend
    started, release = asyncio.Event(), asyncio.Event()
    original = app.state.profiles.save_settings
    old_client = app.state.hermes
    peer.api_key = "rotated-test-key"

    async def delayed(*args):
        await original(*args)
        started.set()
        await release.wait()

    monkeypatch.setattr(app.state.profiles, "save_settings", delayed)
    pending = asyncio.create_task(
        client.put(
            "/api/connection",
            json={"url": "http://isolated-hermes.test", "api_key": peer.api_key},
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert app.state.profiles.writes
        assert "default" in app.state.profiles.changing
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert app.state.settings.api_key == peer.api_key
    assert json.loads(app.state.config_path.read_text())["api_key"] == peer.api_key
    assert app.state.hermes is not old_client and old_client.client.is_closed
    assert not app.state.profiles.changing
    assert not app.state.profiles.writes
    assert (await client.get("/api/capabilities")).status_code == 200


@pytest.mark.parametrize("action", ["add", "remove"])
async def test_cancelled_profile_mutation_keeps_saved_and_loaded_records_consistent(
    backend, monkeypatch, action
):
    client, app, _ = backend
    profiles = app.state.profiles
    record = {"label": "Other", "url": "http://other.test", "api_key": KEY}
    profile_id = None
    child = None
    if action == "remove":
        profile_id = (await client.post("/api/profiles", json=record)).json()["id"]
        child = profiles.resolve(profile_id)
    started, release = asyncio.Event(), asyncio.Event()
    original = profiles.persist

    async def delayed(records):
        await original(records)
        started.set()
        await release.wait()

    monkeypatch.setattr(profiles, "persist", delayed)
    pending = asyncio.create_task(
        client.post("/api/profiles", json=record)
        if action == "add"
        else client.delete(f"/api/profiles/{profile_id}")
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        pending.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    saved = json.loads(profiles.path.read_text())["profiles"]
    assert saved == list(profiles.records.values())
    if child:
        assert profile_id not in profiles.apps
        assert child.state.hermes.client.is_closed
    assert not profiles.removing
    assert not profiles.writes


async def test_failed_connection_save_releases_gates_and_keeps_the_old_client(backend, monkeypatch):
    client, app, _ = backend
    original = app.state.hermes

    async def failed(*args):
        raise OSError("Simulated full disk")

    monkeypatch.setattr(app.state.profiles, "save_settings", failed)
    with pytest.raises(OSError, match="Simulated full disk"):
        await client.put("/api/connection", json={"url": "http://isolated-hermes.test"})
    assert app.state.hermes is original and not original.client.is_closed
    assert not app.state.profiles.changing and not app.state.profiles.writes
    assert not app.state.connection_lock.locked()
    assert (await client.get("/api/capabilities")).status_code == 200


async def test_shutdown_awaits_outstanding_mutations_before_closing_clients(backend):
    _, app, _ = backend
    started, release = asyncio.Event(), asyncio.Event()

    async def write():
        started.set()
        await release.wait()
        assert not app.state.hermes.client.is_closed

    pending = asyncio.create_task(app.state.profiles.finish_mutation(write()))
    await asyncio.wait_for(started.wait(), 1)
    closing = asyncio.create_task(app.state.profiles.close())
    try:
        await asyncio.sleep(0)
        assert not closing.done()
    finally:
        release.set()
        await pending
        await closing
    assert app.state.hermes.client.is_closed
    assert not app.state.profiles.writes


async def test_lifespan_exception_still_closes_connections(backend):
    _, app, _ = backend
    with pytest.raises(RuntimeError, match="Simulated shutdown failure"):
        async with app.router.lifespan_context(app):
            raise RuntimeError("Simulated shutdown failure")
    assert app.state.hermes.client.is_closed


@pytest.mark.parametrize("stored", [None, "", "no-separator", "a" * 32 + ":" + "\u00e9" * 128])
def test_corrupt_password_hashes_cannot_crash_login(stored):
    assert not auth.verify_password("test", stored)


@pytest.mark.parametrize("action", ["rewind", "response"])
async def test_message_ids_outside_database_range_do_not_reach_hermes(backend, action):
    client, _, peer = backend
    mid = 2**63
    path = f"/api/sessions/example/{action}"
    response = (
        await client.post(path, json={"message_id": mid})
        if action == "rewind"
        else await client.get(path, params={"message_id": mid})
    )
    assert response.status_code == 400 and peer.calls == []


def test_unrepresentable_usage_numbers_are_optional():
    assert numeric(10**1000) is None
    assert numeric(True) is None
    assert numeric(123) == 123


async def test_export_tolerates_unknown_content_part_and_tool_shapes(backend):
    client, _, peer = backend
    peer.sessions["example"] = {"id": "example", "title": "Example"}
    peer.messages["example"] = [
        {
            "role": [],
            "content": [{"type": {"future": True}}, {"text": "Saved text"}],
            "tool_calls": 7,
        }
    ]
    response = await client.get("/api/sessions/example/export")
    assert response.status_code == 200
    assert "Saved text" in response.text


async def test_read_timeout_recovers_without_stopping_or_restarting_the_run():
    calls = []

    class TimedOutStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"event":"message.delta","delta":"First part"}\n\n'
            raise httpx.ReadTimeout("Simulated SSE transport loss")

    async def respond(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/events"):
            return httpx.Response(200, stream=TimedOutStream())
        return httpx.Response(
            200,
            json={"status": "running"}
            if len(calls) == 2
            else {"status": "completed", "output": "Complete output"},
        )

    client = Hermes("http://isolated-hermes.test", "", transport=httpx.MockTransport(respond))
    relay = Relay(client)
    channel = relay.attach("example")
    try:
        await asyncio.wait_for(channel.task, 3)
        frames = "".join([frame async for frame in relay.stream(channel, 0)])
        assert '"event":"talaria.reconcile"' in frames
        assert '"event":"run.completed"' in frames
        assert "Complete output" in frames
        assert "run.failed" not in frames and "run.cancelled" not in frames
        assert calls == [
            ("GET", "/v1/runs/example/events"),
            ("GET", "/v1/runs/example"),
            ("GET", "/v1/runs/example"),
        ]
    finally:
        await relay.close()
        await client.close()


@pytest.mark.parametrize("event", [{"event": []}, {"event": None}, {"event": "completed"}])
async def test_malformed_stream_events_recover_through_run_status(event):
    class Upstream:
        async def events(self, run_id):
            yield event

        async def request(self, *args, **kwargs):
            return {"status": "completed", "output": "Recovered output"}

    relay = Relay(Upstream())
    channel = relay.attach("example")
    try:
        await asyncio.wait_for(channel.task, 1)
        frames = "".join([frame async for frame in relay.stream(channel, 0)])
        assert '"event":"run.completed"' in frames
        assert "Recovered output" in frames
    finally:
        await relay.close()


async def test_new_stream_reconciles_when_its_initial_events_have_expired():
    channel = Channel("example")
    for _ in range(1501):
        await channel.publish({"event": "message.delta", "delta": "part"})
    channel.finished = True
    frames = [frame async for frame in Relay(None).stream(channel, 0)]
    assert '"event":"talaria.reconcile"' in frames[1]
    assert len(channel.events) == 1500


@pytest.mark.parametrize("row", [None, [], "message"])
async def test_unreadable_message_rows_fail_before_export_headers(backend, row):
    client, _, peer = backend
    peer.sessions["example"] = {"id": "example", "title": "Example"}
    peer.discovery_overrides["/api/sessions/example/messages"] = ({"data": [row]}, 200)
    for path in ("/api/sessions/example/messages", "/api/sessions/example/export"):
        response = await client.get(path)
        assert response.status_code == 502
        assert "content-disposition" not in response.headers


@pytest.mark.parametrize("failed_message", ["http.response.start", "http.response.body"])
async def test_download_disconnect_closes_the_spool_even_before_iteration(
    backend, monkeypatch, failed_message
):
    _, app, peer = backend
    peer.sessions["example"] = {"id": "example", "title": "Example"}
    peer.messages["example"] = [{"role": "user", "content": "Saved message"}]
    files = []
    original = transcripts.SpooledTemporaryFile

    def track(*args, **kwargs):
        file = original(*args, **kwargs)
        files.append(file)
        return file

    monkeypatch.setattr(transcripts, "SpooledTemporaryFile", track)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/sessions/example/export",
        "path_params": {"session_id": "example"},
        "query_string": b"",
        "headers": [],
        "app": app,
        "asgi": {"spec_version": "2.4"},
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == failed_message:
            raise OSError("Simulated client disconnect")

    response = await transcripts.download(Request(scope, receive))
    try:
        with pytest.raises(ClientDisconnect):
            await response(scope, receive, send)
        assert files[0].closed
    finally:
        files[0].close()
