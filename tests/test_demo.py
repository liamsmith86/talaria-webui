"""The public demo serves immutable samples and has no privileged application routes."""

import re

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
        assert re.search(rf'href="{prefix}/static/build-[a-f0-9]{{16}}/styles/demo.css"', page.text)
        entry = re.search(r'src="([^"]+/boot\.js)"', page.text)[1]
        assets = entry.removesuffix("boot.js")
        for path in ("boot.js", "store.js", "demo.js", "paths.js", "styles/demo.css"):
            asset = await client.get("https://demo.example" + assets + path)
            assert asset.status_code == 200
            assert asset.headers["cache-control"] == "no-cache"
        assert f'href="{prefix}/static/app.webmanifest"' in page.text
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


async def test_demo_asset_urls_change_when_a_dependency_changes(tmp_path, monkeypatch):
    import shutil

    from talaria import demo

    assets = tmp_path / "static"
    shutil.copytree(demo.STATIC, assets)
    monkeypatch.setattr(demo, "STATIC", assets)
    before = create_demo()
    assert create_demo().state.index_html == before.state.index_html
    dependency = assets / "demo.js"
    dependency.write_text(dependency.read_text() + "\n// A new release.\n")
    after = create_demo()
    pattern = r'src="__TALARIA_BASE__([^\"]+/boot\.js)"'
    old_url = re.search(pattern, before.state.index_html)[1]
    new_url = re.search(pattern, after.state.index_html)[1]
    assert old_url != new_url
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(after), base_url="http://demo.test"
    ) as client:
        assert (await client.get("/" + old_url)).status_code == 404
        script = await client.get("/" + new_url.replace("boot.js", "demo.js"))
        assert script.text == dependency.read_text()


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
