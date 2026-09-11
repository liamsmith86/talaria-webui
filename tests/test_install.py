import json
import subprocess
import sys

from talaria.auth import hash_password, verify_password
from talaria.config import Settings, load, save, validate_url


def test_configuration_is_private_and_portable(tmp_path):
    path = tmp_path / "private" / "config.json"
    settings = Settings(signing_key="test", api_key="synthetic-secret")
    save(path, settings)
    assert path.stat().st_mode & 0o077 == 0
    assert load(path).api_key == "synthetic-secret"
    assert validate_url("https://example.test/hermes/v1/") == "https://example.test/hermes"
    assert json.loads(path.read_text())["host"] == "127.0.0.1"


def test_password_verification():
    stored = hash_password("a long example password")
    assert verify_password("a long example password", stored)
    assert not verify_password("different password", stored)


def test_cli_help_is_available_without_setup():
    result = subprocess.run(
        [sys.executable, "-c", "from talaria.cli import main; main()", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--config" in result.stdout and "--set-password" in result.stdout


def test_public_url_flag_configures_runtime_and_first_start(tmp_path, monkeypatch):
    from talaria import cli

    path = tmp_path / "config.json"
    captured = []
    monkeypatch.setattr(
        sys,
        "argv",
        ["talaria", "--config", str(path), "--public-url", "https://EXAMPLE.com:443/telaria/"],
    )
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: None)
    monkeypatch.setattr(
        "talaria.app.create_app", lambda settings, _: captured.append(settings.public_url)
    )
    cli.main()
    assert captured == ["https://example.com/telaria"]
    assert load(path).public_url == captured[0]
