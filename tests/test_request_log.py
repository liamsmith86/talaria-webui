import importlib.util
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from talaria.hermes_plugin import request_log


@pytest.fixture
def recorder(tmp_path, monkeypatch):
    state = SimpleNamespace(home=tmp_path, enabled=True)
    monkeypatch.setitem(
        sys.modules, "hermes_constants", SimpleNamespace(get_hermes_home=lambda: state.home)
    )
    ctx = SimpleNamespace(get_config=Mock(side_effect=lambda *args: state.enabled))
    return request_log.RequestLog(ctx), state


def emit(recorder, **kwargs):
    request_log.scoped_run(recorder.before)(
        platform="api_server",
        session_id="session",
        api_request_id="request",
        request_messages=[{"role": "user", "content": "PRIVATE_QUERY"}],
        system_prompt="PRIVATE_INSTRUCTION",
        **kwargs,
    )


def test_logging_requires_explicit_opt_in_and_talaria_route(recorder, tmp_path):
    logger, state = recorder
    for value in (False, None, "true", 1):
        state.enabled = value
        emit(logger)
    state.enabled = True
    logger.before(platform="api_server", request_messages=[])
    request_log.scoped_run(logger.before)(platform="discord", request_messages=[])
    assert not (tmp_path / "talaria").exists()
    emit(
        logger,
        request={
            "body": {
                "temperature": 0.7,
                "api_key": "NEVER_SAVE_KEY",
                "extra_headers": {"Authorization": "NEVER_SAVE_HEADER"},
                "http_client": "NEVER_SAVE_CLIENT",
                "extra_body": {"reasoning": {"effort": "high"}, "api_key": "NEVER_SAVE_EXTRA"},
            }
        },
    )
    path = tmp_path / "talaria/request-debug.jsonl"
    data = json.loads(path.read_text())
    assert data["messages"][0]["content"] == "PRIVATE_QUERY"
    assert data["parameters"] == {
        "temperature": 0.7,
        "extra_body": {"reasoning": {"effort": "high"}},
    }
    assert "NEVER_SAVE" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    state.enabled = False
    emit(logger)
    assert len(path.read_text().splitlines()) == 1
    state.enabled = True
    state.home = tmp_path / "another-profile"
    emit(logger)
    assert (state.home / "talaria/request-debug.jsonl").exists()
    assert len(path.read_text().splitlines()) == 1


def test_concurrent_records_and_rotation(recorder, tmp_path, monkeypatch):
    logger, _ = recorder
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: emit(logger), range(40)))
    path = tmp_path / "talaria/request-debug.jsonl"
    assert len([json.loads(line) for line in path.read_text().splitlines()]) == 40
    monkeypatch.setattr(request_log, "MAX_FILE", path.stat().st_size)
    for _ in range(130):
        emit(logger)
    files = list(path.parent.iterdir())
    assert len(files) == 4
    for file in files:
        assert file.stat().st_mode & 0o777 == 0o600
        assert file.stat().st_size <= request_log.MAX_FILE
        assert all(
            json.loads(line)["event"] == "model_request" for line in file.read_text().splitlines()
        )


def test_oversized_record_is_explicit_and_failures_do_not_echo_inputs(
    recorder, tmp_path, monkeypatch, caplog
):
    logger, _ = recorder
    monkeypatch.setattr(request_log, "MAX_RECORD", 100)
    emit(logger)
    path = tmp_path / "talaria/request-debug.jsonl"
    record = json.loads(path.read_text())
    assert record["event"] == "request_omitted" and record["bytes"] > 100
    assert "PRIVATE_QUERY" not in path.read_text()
    monkeypatch.setattr(request_log, "append_record", Mock(side_effect=OSError("SECRET_ERROR")))
    emit(logger)
    assert "logging failed (OSError)" in caplog.text
    assert "SECRET_ERROR" not in caplog.text and "PRIVATE_QUERY" not in caplog.text


def test_symlink_is_not_followed(recorder, tmp_path, caplog):
    logger, _ = recorder
    target = tmp_path / "unrelated"
    target.write_text("KEEP")
    directory = tmp_path / "talaria"
    directory.mkdir()
    (directory / "request-debug.jsonl").symlink_to(target)
    emit(logger)
    assert target.read_text() == "KEEP"
    assert "logging failed" in caplog.text


def test_run_scope_restores_context_on_success_and_error():
    assert request_log.scoped_run(lambda: request_log.active_run.get())() is True
    assert request_log.active_run.get() is False

    @request_log.scoped_run
    def fail():
        raise RuntimeError("original failure")

    with pytest.raises(RuntimeError, match="original failure"):
        fail()
    assert request_log.active_run.get() is False


def test_named_profile_hook_recognizes_the_primary_plugins_run_scope(recorder, tmp_path):
    _, state = recorder
    spec = importlib.util.spec_from_file_location("named_profile_request_log", request_log.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.active_run is not request_log.active_run
    ctx = SimpleNamespace(get_config=lambda *args: state.enabled)
    logger = module.RequestLog(ctx)
    emit(logger)  # Admission belongs to the primary module; the hook belongs to the profile.
    path = tmp_path / "talaria/request-debug.jsonl"
    assert json.loads(path.read_text())["messages"][0]["content"] == "PRIVATE_QUERY"
    before = path.read_bytes()
    logger.before(platform="api_server", request_messages=[{"content": "UNRELATED_CLIENT"}])
    assert path.read_bytes() == before
