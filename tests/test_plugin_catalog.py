"""The catalog gate must reject failures that Hermes's exit code alone can miss."""

import json
import subprocess

import pytest

from contrib.plugin_catalog import REQUIRED_CHECKS, entry_for, require_clean_validation


def validation(*, stderr="", warnings=None, drop=None, verdict="safe", failed=None):
    checks = [
        {"name": name, "ok": name != failed, "detail": verdict if name == "security scan" else "ok"}
        for name in sorted(REQUIRED_CHECKS)
        if name != drop
    ]
    return subprocess.CompletedProcess(
        ["hermes"],
        0,
        json.dumps({"ok": True, "checks": checks, "warnings": warnings or []}),
        stderr,
    )


@pytest.mark.parametrize(
    "result",
    [
        validation(stderr="dependency preparation failed: resolver unavailable\n"),
        validation(drop="no core override"),
        validation(warnings=["security scan caution: review this behavior"]),
        validation(verdict="caution"),
        validation(failed="declared hooks"),
    ],
)
def test_catalog_gate_rejects_incomplete_or_misleading_success(result):
    with pytest.raises(ValueError):
        require_clean_validation(result)


def test_catalog_gate_accepts_complete_clean_validation():
    assert require_clean_validation(validation())["ok"] is True


def test_catalog_images_and_metadata_follow_the_reviewed_commit():
    sha = "0123456789abcdef0123456789abcdef01234567"
    entry = entry_for(
        {
            "name": "talaria-webui",
            "version": "1.5.0",
            "requires_hermes": ">=0.21.5",
            "provides_hooks": ["pre_api_request"],
            "provides_middleware": ["llm_request"],
        },
        sha,
    )
    assert entry["sha"] == sha
    assert entry["version"] == "1.5.0"
    assert entry["capabilities"]["provides_hooks"] == ["pre_api_request"]
    assert entry["capabilities"]["provides_middleware"] == ["llm_request"]
    assert entry["capabilities"]["requires_env"] == []
    assert all(
        f"/{sha}/" in url for url in [entry["docs_url"], entry["image"], *entry["screenshots"]]
    )
