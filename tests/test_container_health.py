"""Container health checks follow the effective listener, not a hardcoded port."""

import json
import sys
from urllib.parse import urlsplit

import pytest

from talaria import cli, container_health
from talaria.config import Settings, save


@pytest.mark.parametrize("override", [False, True])
def test_healthcheck_uses_configured_or_overridden_listener(
    live_app, tmp_path, monkeypatch, override
):
    port = urlsplit(live_app[0]).port
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "container-data"))
    monkeypatch.setenv("TALARIA_CONTAINER", "1")
    # A custom config path must not make the separate Docker probe lose its target.
    config = tmp_path / "custom.json"
    save(config, Settings(password_hash="already-initialized", port=8766 if override else port))
    argv = ["talaria", "--config", str(config), "--host", "0.0.0.0"]
    if override:
        argv += ["--port", str(port)]
    monkeypatch.setattr(sys, "argv", argv)
    called = []
    monkeypatch.setattr("talaria.server.run", lambda app, **options: called.append(options))
    cli.main()
    assert called[0]["port"] == port
    path = container_health.listener_path()
    assert json.loads(path.read_text()) == {"host": "127.0.0.1", "port": port}
    assert path.stat().st_mode & 0o077 == 0
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setattr(sys, "argv", ["talaria", "healthcheck"])
    cli.main()


@pytest.mark.parametrize("contents", [None, "bad json", "{}", '{"host":"localhost","port":true}'])
def test_missing_or_invalid_listener_fails_healthcheck(tmp_path, monkeypatch, contents):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    if contents is not None:
        path = container_health.listener_path()
        path.parent.mkdir()
        path.write_text(contents)
    with pytest.raises(SystemExit, match="health check failed"):
        container_health.main()


def test_reconfigured_container_replaces_old_listener(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    container_health.record("0.0.0.0", 8766)
    container_health.record("::", 9876)
    assert json.loads(container_health.listener_path().read_text()) == {"host": "::1", "port": 9876}
