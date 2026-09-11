"""HTTP read methods and unusable connection addresses stay inside their boundaries."""

import httpx
import pytest

from talaria import auth
from talaria.app import create_app
from talaria.config import Settings, validate_url

from .fake_hermes import KEY, FakeHermes


@pytest.fixture
async def method_client(tmp_path):
    peer = FakeHermes()
    settings = Settings(hermes_url="http://hermes.test", api_key=KEY, signing_key="test-signing")
    app = create_app(
        settings, tmp_path / "config.json", transport=httpx.ASGITransport(app=peer.app)
    )
    async with httpx.AsyncClient(
        base_url="http://talaria.test",
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
    ) as client:
        client.cookies.set(auth.COOKIE, auth.issue_cookie(settings))
        yield client, app, peer
    await app.state.profiles.close()


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/sessions", {"title": "Must not be created"}),
        (
            "/api/profiles",
            {"label": "Must not be saved", "url": "http://other.test", "api_key": KEY},
        ),
        ("/api/connection", {"url": "http://other.test", "api_key": KEY}),
    ],
)
@pytest.mark.parametrize("with_body", [False, True])
async def test_head_uses_the_read_branch_even_with_a_body(method_client, path, payload, with_body):
    client, app, peer = method_client
    response = await client.request("HEAD", path, **({"json": payload} if with_body else {}))
    assert response.status_code == 200
    assert response.content == b""
    assert not peer.sessions
    assert not app.state.profiles.records
    assert app.state.settings.hermes_url == "http://hermes.test"
    assert not app.state.profiles.path.exists()
    assert all(method == "GET" for method, _, _ in peer.calls)


@pytest.mark.parametrize(
    "path,upstream,result",
    [
        ("/api/sessions/example", "/api/sessions/example", {"session": {"id": "example"}}),
        (
            "/api/sessions/example/context",
            "/talaria/v1/sessions/example/context",
            {"context": {"used": 10, "maximum": 100}},
        ),
        (
            "/api/sessions/example/response?message_id=1",
            "/talaria/v1/sessions/example/response",
            {"usage": {"input_tokens": 10}},
        ),
        ("/api/commands", "/talaria/v1/commands", {"commands": []}),
        ("/api/commands/example", "/talaria/v1/commands/example", {"status": "completed"}),
    ],
)
async def test_head_proxy_reads_use_the_same_upstream_get(method_client, path, upstream, result):
    client, _, peer = method_client
    peer.discovery_overrides[upstream] = (result, 200)
    response = await client.head(path)
    assert response.status_code == 200
    assert response.content == b""
    assert peer.calls and all(method == "GET" for method, _, _ in peer.calls)


@pytest.mark.parametrize("url", ["http://[v1.foo]", "http://💩.test"])
@pytest.mark.parametrize("path", ["/api/connection/test", "/api/profiles"])
async def test_unusable_client_urls_are_rejected_before_connecting(method_client, path, url):
    client, app, peer = method_client
    csrf = (await client.get("/api/bootstrap")).json()["csrf"]
    response = await client.post(
        path,
        json={"url": url, "api_key": KEY, "label": "Invalid address"},
        headers={"X-Talaria-Request": "1", "X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert not peer.calls
    assert not app.state.profiles.records


def test_valid_internationalized_connection_names_remain_supported():
    url = validate_url("https://bücher.example/hermes/v1/")
    assert url == "https://bücher.example/hermes"
    assert httpx.URL(url).raw_host == b"xn--bcher-kva.example"
