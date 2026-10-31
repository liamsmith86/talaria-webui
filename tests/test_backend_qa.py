"""Backend regressions using isolated transports; no installed Hermes service is contacted."""

import asyncio
import json
import tracemalloc

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
    peer.sessions["original"] = {"id": "original", "source": "api_server"}
    peer.messages["original"] = [{"id": 1, "role": "user", "content": "Original"}]
    peer.discovery_overrides["/talaria/v1/sessions/original/fork"] = (
        {"error": "Unavailable"},
        status,
    )
    response = await client.post("/api/sessions/original/fork", json={"title": "Copy"})
    native_calls = [c for c in peer.calls if c[0:2] == ("POST", "/api/sessions/original/fork")]
    assert len(native_calls) == (1 if status == 404 else 0)
    assert len(peer.sessions) == (2 if status == 404 else 1)
    assert response.status_code == (201 if status == 404 else 409 if status == 409 else 502)


async def test_native_fallback_never_ends_another_apps_session(backend):
    client, _, peer = backend
    peer.sessions["original"] = {"id": "original", "source": "telegram"}
    peer.messages["original"] = [{"id": 1, "role": "user", "content": "Original"}]
    response = await client.post("/api/sessions/original/fork", json={})
    assert response.status_code == 409
    assert "Update the Talaria plugin" in response.json()["error"]
    assert not [c for c in peer.calls if c[0:2] == ("POST", "/api/sessions/original/fork")]
    assert list(peer.sessions) == ["original"] and "end_reason" not in peer.sessions["original"]


@pytest.mark.parametrize("plugin", [False, True])
@pytest.mark.parametrize("source", ["api_server", "tui"])
async def test_branching_a_session_with_no_messages_creates_nothing(backend, plugin, source):
    client, _, peer = backend
    peer.extension = {"session_fork": True} if plugin else None
    peer.sessions["empty"] = {"id": "empty", "source": source}
    peer.messages["empty"] = []
    response = await client.post("/api/sessions/empty/fork", json={})
    assert response.status_code == 409
    assert response.json()["error"] == "This session has no chat history to branch."
    assert list(peer.sessions) == ["empty"] and "end_reason" not in peer.sessions["empty"]
    assert not [c for c in peer.calls if c[0] == "POST" and c[1].endswith("/fork")]


async def test_a_message_too_large_to_read_does_not_look_like_no_messages(backend, monkeypatch):
    client, _, peer = backend
    peer.sessions["huge"] = {"id": "huge", "source": "api_server"}
    peer.messages["huge"] = [{"id": 1, "role": "user", "content": "Huge"}]

    async def too_large(*_args, **_kwargs):
        raise APIError("Hermes returned too much data.", 502, "response_too_large")

    monkeypatch.setattr("talaria.routes.message_page", too_large)
    response = await client.post("/api/sessions/huge/fork", json={})
    assert response.status_code == 201, response.text


async def test_branching_a_missing_session_still_says_it_is_missing(backend):
    client, _, peer = backend
    assert (await client.post("/api/sessions/missing/fork", json={})).status_code == 404
    assert not peer.sessions


async def test_a_session_with_no_messages_still_exports_readable_files(backend):
    client, _, peer = backend
    peer.sessions["empty"] = {"id": "empty", "source": "tui"}
    peer.messages["empty"] = []
    exported = await client.get("/api/sessions/empty/export?format=json")
    assert exported.status_code == 200
    assert json.loads(exported.text)["messages"] == []
    markdown = await client.get("/api/sessions/empty/export")
    assert markdown.status_code == 200 and markdown.text.startswith("# Session")


async def test_session_authority_refusal_explains_how_to_continue(backend):
    client, _, peer = backend
    peer.session_authority = True
    peer.sessions["cli"] = {"id": "cli", "source": "cli"}
    peer.messages["cli"] = []
    response = await client.post("/api/runs", json={"session_id": "cli", "input": "Hello"})
    assert response.status_code == 409
    assert "Continue this conversation in a new branch." in response.json()["error"]
    assert not peer.runs


@pytest.mark.parametrize("status", ["queued", "waiting_for_approval", "completed"])
async def test_guidance_refused_by_hermes_says_why(backend, status):
    client, _, peer = backend
    peer.runs["waiting"] = {"run_id": "waiting", "session_id": "s", "status": status}
    response = await client.post("/api/runs/waiting/steer", json={"input": "Use tabs"})
    assert response.status_code == 409
    assert response.json()["error"] == "Hermes takes guidance only while it is working."
    assert "steer" not in peer.runs["waiting"]


async def test_discarding_a_lost_turn_resolves_exactly_that_admission(backend):
    client, _, peer = backend
    peer.sessions["lost"] = {"id": "lost", "source": "api_server"}
    peer.messages["lost"] = []
    started = await client.post(
        "/api/runs", json={"session_id": "lost", "input": "Lost in a restart"}
    )
    rid = started.json()["run_id"]
    await asyncio.wait_for(peer.runs[rid]["_task"], 5)
    assert (await client.get(f"/api/runs/{rid}")).json()["status"] == "unknown"
    for _ in range(2):  # A repeated or concurrent discard reports the settled run.
        response = await client.post(f"/api/runs/{rid}/discard", json={})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "cancelled"
    assert peer.messages["lost"][-1]["display_kind"] == "failed_turn"


@pytest.mark.parametrize("plugin", [False, True])
async def test_default_branch_names_are_chosen_by_hermes(backend, plugin):
    client, _, peer = backend
    peer.extension = {"session_fork": True} if plugin else None
    peer.sessions["original"] = {"id": "original", "title": "Original", "source": "api_server"}
    peer.messages["original"] = [{"id": 1, "role": "user", "content": "Original"}]
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
        await asyncio.wait_for(asyncio.gather(required.wait(), optional.wait()), 30)
        assert not task.done()
        release.set()
        response = await asyncio.wait_for(task, 30)
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
        await asyncio.wait_for(started.wait(), 30)
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
    # A plugin-less branch first confirms the API owns the session.
    peer.sessions["example"] = {"id": "example"}
    peer.messages["example"] = [{"id": 1, "role": "user", "content": "Example"}]
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
        await asyncio.wait_for(started.wait(), 30)
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
        await asyncio.wait_for(started.wait(), 30)
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
        await asyncio.wait_for(started.wait(), 30)
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
        await asyncio.wait_for(started.wait(), 30)
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
    await asyncio.wait_for(started.wait(), 30)
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
        },
        {
            "role": "user",
            "display_kind": ["future"],
            "content": "Later text",
            "tool_calls": [{"id": ["future"]}],
        },
    ]
    response = await client.get("/api/sessions/example/export")
    assert response.status_code == 200
    assert "Saved text" in response.text and "Later text" in response.text


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
        await asyncio.wait_for(channel.task, 30)
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
        await asyncio.wait_for(channel.task, 30)
        frames = "".join([frame async for frame in relay.stream(channel, 0)])
        assert '"event":"run.completed"' in frames
        assert "Recovered output" in frames
    finally:
        await relay.close()


async def test_polling_recovers_and_clears_native_clarification():
    question = {"request_id": "pending-question", "question": "Which environment?"}

    class Upstream:
        def __init__(self):
            self.statuses = iter(
                [
                    {"status": "waiting_for_input", "clarification": question},
                    {"status": "running", "clarification": None},
                    {"status": "completed"},
                ]
            )

        async def request(self, *args, **kwargs):
            return next(self.statuses)

    channel = Channel("pending")
    await asyncio.wait_for(Relay(Upstream()).poll(channel), 30)
    events = [json.loads(entry.encoded) for entry in channel.events]
    assert events == [
        {**question, "event": "talaria.clarification.request"},
        {"event": "talaria.clarification.resolved", "request_id": "pending-question"},
        {"event": "run.completed", "status": "completed"},
    ]


@pytest.mark.parametrize(("status", "forgotten"), [(404, True), (401, False), (403, False)])
async def test_only_a_run_hermes_forgot_is_reported_as_ended(status, forgotten):
    class Upstream:
        async def request(self, *args, **kwargs):
            raise APIError("Run not found.", status)

    channel = Channel("gone")
    await asyncio.wait_for(Relay(Upstream()).poll(channel), 2)
    events = [json.loads(entry.encoded) for entry in channel.events]
    assert events == [
        {"event": "talaria.unavailable", "message": "Run not found.", "forgotten": forgotten}
    ]


def replayed(frames):
    events = [json.loads(frame.split("data: ", 1)[1]) for frame in frames if "data: " in frame]
    return events, "".join(e.get("delta", "") for e in events if e["event"] == "message.delta")


async def test_long_responses_replay_completely_to_late_and_lagging_subscribers():
    channel = Channel("example")
    await channel.publish({"event": "tool.started", "tool_call_id": "a", "tool": "terminal"})
    for i in range(3000):
        await channel.publish({"event": "message.delta", "delta": f"{i},"})
    await channel.publish({"event": "tool.completed", "tool_call_id": "a"})
    await channel.publish({"event": "message.delta", "delta": "end"})
    channel.finished = True
    text = "".join(f"{i}," for i in range(3000)) + "end"
    # Another device joining now sees the whole response, in order.
    events, joined = replayed([frame async for frame in Relay(None).stream(channel, 0)])
    assert joined == text
    assert events[0]["event"] == "tool.started"
    assert "talaria.reconcile" not in {e["event"] for e in events}
    assert len(channel.events) == 1500
    # A subscriber that saw part of the older text receives only the rest.
    events, rest = replayed([frame async for frame in Relay(None).stream(channel, 1001)])
    assert rest == text[len("".join(f"{i}," for i in range(1000))) :]
    assert "talaria.reconcile" not in {e["event"] for e in events}


async def test_new_stream_reconciles_when_its_initial_events_have_expired():
    channel = Channel("example")
    await channel.publish({"event": "tool.started", "tool_call_id": "a", "tool": "terminal"})
    for _ in range(3):
        await channel.publish({"event": "message.delta", "delta": "x" * 1024 * 1024})
    channel.finished = True
    frames = [frame async for frame in Relay(None).stream(channel, 1)]
    # Beyond the size limit some text is gone. A client keeps what it already
    # shows, including its tool, and the saved transcript fills the gap.
    assert frames[1] == 'data: {"event":"talaria.reconcile"}\n\n'
    assert channel.size <= 2 * 1024 * 1024
    # A cursor from before Talaria restarted numbers other events: rebuild.
    frames = [frame async for frame in Relay(None).stream(channel, 99)]
    assert frames[1] == 'data: {"event":"talaria.reconcile","replay":true}\n\n'


async def test_relay_holds_no_more_than_its_size_limit_accounts_for():
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        channel = Channel("example")
        for _ in range(60000):
            await channel.publish({"event": "message.delta", "delta": "文字"})
        held = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()
    channel.finished = True
    # Recent events cost their envelope as well, a bounded number of them.
    assert held < 2 * channel.size + 1024 * 1024, (held, channel.size)
    _, text = replayed([frame async for frame in Relay(None).stream(channel, 30000)])
    assert text == "文字" * 30000


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


async def test_session_details_count_a_conversation_split_by_rotating_compaction(backend):
    client, _, peer = backend
    usage = {
        "input_tokens": 100,
        "output_tokens": 10,
        "api_call_count": 1,
        "estimated_cost_usd": 0.5,
    }
    peer.sessions["first"] = {"id": "first", "end_reason": "compression", **usage}
    peer.sessions["second"] = {
        "id": "second",
        "parent_session_id": "first",
        "end_reason": "compression",
        **usage,
    }
    peer.sessions["latest"] = {"id": "latest", "parent_session_id": "second", **usage}
    peer.sessions["branch"] = {"id": "branch", "parent_session_id": "latest", **usage}
    for sid in peer.sessions:
        peer.messages[sid] = []
    details = (await client.get("/api/sessions/latest", params={"usage": "conversation"})).json()
    assert details["talaria_conversation_usage"] == {
        "input_tokens": 300,
        "output_tokens": 30,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "reasoning_tokens": None,
        "api_call_count": 3,
        "estimated_cost_usd": 1.5,
        "actual_cost_usd": None,
    }
    # A branch's parent did not end by compression: it is a separate conversation.
    branch = (await client.get("/api/sessions/branch", params={"usage": "conversation"})).json()
    assert "talaria_conversation_usage" not in branch
    # Nor is a branch or subagent started before its parent was compacted.
    for marker in ("_branched_from", "_delegate_from"):
        peer.sessions["early"] = {
            "id": "early",
            "parent_session_id": "first",
            "model_config": {marker: "first"},
            **usage,
        }
        peer.messages["early"] = []
        early = (await client.get("/api/sessions/early", params={"usage": "conversation"})).json()
        assert "talaria_conversation_usage" not in early, marker
    # Revalidation never pays for the extra reads.
    assert "talaria_conversation_usage" not in (await client.get("/api/sessions/latest")).json()
