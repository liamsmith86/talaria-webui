"""The public demo serves immutable samples and has no privileged application routes."""

import httpx
import pytest

from talaria.demo import create_demo


@pytest.mark.parametrize("prefix", ["", "/apps/talaria"])
async def test_demo_is_readonly_and_never_opens_private_configuration(
    tmp_path, monkeypatch, prefix
):
    from talaria import config
    from talaria.hermes import Hermes

    def forbidden(*args, **kwargs):
        raise AssertionError("The demo tried to open a real configuration or Hermes client")

    monkeypatch.setattr(config, "load", forbidden)
    monkeypatch.setattr(Hermes, "__init__", forbidden)
    monkeypatch.setenv("TALARIA_HERMES_API_KEY", "private-test-value-never-for-the-demo")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    private = tmp_path / "config.json"
    private.write_text('{"api_key":"private-test-value-never-for-the-demo"}')
    before = private.read_bytes()
    app = create_demo(public_url="https://demo.example" + prefix)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://demo.example" + prefix + "/"
    ) as client:
        page = await client.get("")
        assert page.status_code == 200
        assert f'href="{prefix}/static/styles/demo.css"' in page.text
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert (await client.get("robots.txt")).text == "User-agent: *\nDisallow: /\n"
        bootstrap = await client.get("api/bootstrap")
        assert bootstrap.json()["environment"] == "demo"
        assert bootstrap.json()["authenticated"] is True
        assert "set-cookie" not in bootstrap.headers
        original = await client.get("api/samples")
        assert len(original.json()) == 3
        sid = original.json()[0]["id"]
        for path in (
            "connection",
            "connection/test",
            "profiles",
            "profiles/test",
            "login",
            "installation/update",
            "installation/check",
            "runs",
            "commands",
            f"sessions/{sid}",
            f"sessions/{sid}/rewind",
            f"sessions/{sid}/fork",
        ):
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                response = await client.request(
                    method, "api/" + path, json={"url": "http://127.0.0.1"}
                )
                assert response.status_code == 405, (method, path)
                assert response.json()["code"] == "demo_read_only"
                assert response.headers["x-robots-tag"] == "noindex, nofollow"
        assert (await client.get("api/samples")).content == original.content
        for path in (
            "api/installation",
            "api/connection",
            "api/runs/private",
            "config.json",
            "api/sessions/private",
            "api/sessions/../config",
        ):
            assert (await client.get(path)).status_code == 404
        for path in (
            "api/bootstrap",
            "api/samples",
        ):
            assert "private-test-value" not in (await client.get(path)).text
    assert private.read_bytes() == before
    assert list(tmp_path.iterdir()) == [private]


@pytest.mark.parametrize("container", [False, True])
def test_demo_command_defaults_and_container_listener(tmp_path, monkeypatch, container):
    import uvicorn

    from talaria import cli
    from talaria.container_health import listener_path

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("TALARIA_CONTAINER", "1" if container else "0")
    monkeypatch.setattr("sys.argv", ["talaria", "demo"])
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    cli.main()
    assert calls[0][1]["host"] == "127.0.0.1"
    assert calls[0][1]["port"] == 8768
    assert calls[0][1]["proxy_headers"] is False
    assert not (tmp_path / "talaria/config.json").exists()
    assert listener_path().exists() is container
