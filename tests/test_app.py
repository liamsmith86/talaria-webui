from contextlib import contextmanager

import httpx


@contextmanager
def signed_in(url):
    client = httpx.Client(base_url=url, headers={"X-Talaria-Request": "1"})
    assert client.post("/api/login", json={"password": "test-password"}).status_code == 200
    client.headers["X-CSRF-Token"] = client.get("/api/bootstrap").json()["csrf"]
    try:
        yield client
    finally:
        client.close()


def test_auth_and_csrf_boundary(live_app):
    url, _, _ = live_app
    assert httpx.get(url + "/api/sessions").status_code == 401
    with signed_in(url) as client:
        assert client.get("/api/sessions").status_code == 200
        assert (
            client.post(
                "/api/sessions",
                json={"title": "Unsafe"},
                headers={"Origin": "https://untrusted.example"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/sessions", json={"title": "Unsafe"}, headers={"X-CSRF-Token": "wrong"}
            ).status_code
            == 403
        )
        assert "test-hermes-key" not in client.get("/api/connection").text
        assert "frame-ancestors 'none'" in client.get("/").headers["Content-Security-Policy"]


def test_session_lifecycle(live_app):
    with signed_in(live_app[0]) as client:
        created = client.post("/api/sessions", json={"title": "A thought"})
        sid = created.json()["id"]
        assert created.status_code == 201
        assert (
            client.patch(f"/api/sessions/{sid}", json={"title": "A better thought"}).status_code
            == 200
        )
        fork = client.post(f"/api/sessions/{sid}/fork", json={"title": "A branch"}).json()
        assert fork["id"] != sid
        assert len(client.get("/api/sessions").json()["data"]) == 2
        assert client.delete(f"/api/sessions/{sid}").status_code == 200
        assert client.get(f"/api/sessions/{sid}").status_code == 404


def test_idempotent_run_and_replay(live_app):
    url, peer, _ = live_app
    with signed_in(url) as client:
        sid = client.post("/api/sessions", json={"title": "Run"}).json()["id"]
        payload = {"session_id": sid, "input": "Hello", "request_id": "test-request"}
        rid = client.post("/api/runs", json=payload).json()["run_id"]
        assert client.post("/api/runs", json=payload).json()["run_id"] == rid
        first = client.get(f"/api/runs/{rid}/events").text
        assert '"event":"message.delta"' in first and '"event":"run.completed"' in first
        replay = client.get(f"/api/runs/{rid}/events", headers={"Last-Event-ID": "1"}).text
        assert "id: 1\n" not in replay
        assert '"event":"run.completed"' in replay
        assert len(peer.runs) == 1
