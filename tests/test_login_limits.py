"""Login throttling must expire predictably and resist forwarded-IP spoofing."""

from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

from talaria import auth
from talaria.app import create_app
from talaria.config import Settings


def test_login_window_success_and_bounded_memory(monkeypatch):
    clock = SimpleNamespace(monotonic=lambda: 0)
    monkeypatch.setattr(auth, "time", clock)
    limiter = auth.LoginLimiter()
    assert all(limiter.allow("a") for _ in range(30))
    assert not limiter.allow("a")
    assert limiter.allow("b")
    clock.monotonic = lambda: 3599
    assert not limiter.allow("a")
    clock.monotonic = lambda: 3600
    assert limiter.allow("a")
    limiter.success("a")
    assert all(limiter.allow("a") for _ in range(30))
    assert not limiter.allow("a")
    for index in range(2000):
        limiter.allow(str(index))
    assert len(limiter.failures) == 1024


@pytest.mark.parametrize(
    ("trusted", "peer", "forwarded", "expected"),
    [
        ([], "127.0.0.1", "198.51.100.1", "127.0.0.1"),
        (["127.0.0.1"], "198.51.100.1", "198.51.100.2", "198.51.100.1"),
        (["127.0.0.1"], "127.0.0.1", "198.51.100.1", "198.51.100.1"),
        (["127.0.0.1"], "127.0.0.1", "spoofed, 198.51.100.1", "198.51.100.1"),
        (["127.0.0.1", "10.0.0.0/24"], "127.0.0.1", "198.51.100.1, 10.0.0.2", "198.51.100.1"),
        (["::1"], "::1", "2001:db8::1", "2001:db8::1"),
        (["127.0.0.1"], "127.0.0.1", "invalid", "127.0.0.1"),
        (["127.0.0.1"], "127.0.0.1", "", "127.0.0.1"),
        (["127.0.0.1"], "127.0.0.1", "a" * 2049, "127.0.0.1"),
    ],
)
def test_only_trusted_proxies_can_report_client_addresses(trusted, peer, forwarded, expected):
    request = Request(
        {
            "type": "http",
            "client": (peer, 1234),
            "headers": [(b"x-forwarded-for", forwarded.encode())],
        }
    )
    assert auth.LoginLimiter(trusted).address(request) == expected


async def test_proxy_clients_have_separate_login_buckets(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "verify_password", lambda password, stored: password == "right")
    app = create_app(Settings(trusted_proxies=["127.0.0.1"]), tmp_path / "config.json")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app, client=("127.0.0.1", 1234)), base_url="http://test"
    ) as client:
        headers = {"X-Talaria-Request": "1", "X-Forwarded-For": "198.51.100.1"}
        for _ in range(30):
            assert (
                await client.post("/api/login", json={"password": "wrong"}, headers=headers)
            ).status_code == 401
        assert (
            await client.post("/api/login", json={"password": "right"}, headers=headers)
        ).status_code == 429
        headers["X-Forwarded-For"] = "198.51.100.2"
        assert (
            await client.post("/api/login", json={"password": "right"}, headers=headers)
        ).status_code == 200
        robots = await client.get("/robots.txt")
        assert robots.text == "User-agent: *\nDisallow: /\n"
        assert robots.headers["x-robots-tag"] == "noindex, nofollow"
