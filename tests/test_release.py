"""Release versions and registry failures must never silently replace stable images."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
from pre_commit_hooks.check_yaml import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release", ROOT / ".github/scripts/release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.mark.parametrize(
    "tag,package", [("v1.2.3", "1.2.3"), ("v1.2.3-rc.1", "1.2.3rc1"), ("v0.4.0-beta.2", "0.4.0b2")]
)
def test_package_version_matches_release_tag(tag, package):
    assert release.version(tag)[2] == package


@pytest.mark.parametrize("tag", ["latest", "v01.2.3", "v1.2.3;echo bad", "v1.2.3-rc.01"])
def test_invalid_release_tags_are_rejected(tag):
    with pytest.raises(ValueError):
        release.version(tag)


def index(tag, revision="test-commit"):
    return {
        "annotations": {release.VERSION: tag, release.REVISION: revision},
        "manifests": [
            {"platform": {"os": "linux", "architecture": arch}} for arch in ("amd64", "arm64")
        ],
    }


@pytest.fixture
def registry(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/app")
    monkeypatch.setenv("GITHUB_SHA", "test-commit")
    monkeypatch.setenv("RELEASE_TAG", "v1.2.3")
    monkeypatch.setenv("RELEASE_PRERELEASE", "false")
    (tmp_path / "digests").mkdir()
    for arch, letter in (("amd64", "a"), ("arm64", "b")):
        (tmp_path / "digests" / arch).write_text("sha256:" + letter * 64)
    images, writes = {}, []
    monkeypatch.setattr(release, "inspect", lambda ref: images.get(ref))

    def create(ref, sources, annotations=()):
        writes.append((ref, sources))
        if annotations:
            images[ref] = index(ref.split(":")[-1])
        else:
            images[ref] = images[sources[0]]

    monkeypatch.setattr(release, "create", create)
    return images, writes


def test_first_release_promotes_tested_digests_and_rerun_keeps_version(registry):
    images, writes = registry
    release.publish()
    assert writes[0] == (
        "ghcr.io/owner/app:v1.2.3",
        ["ghcr.io/owner/app@sha256:" + "a" * 64, "ghcr.io/owner/app@sha256:" + "b" * 64],
    )
    assert writes[1] == ("ghcr.io/owner/app:latest", ["ghcr.io/owner/app:v1.2.3"])
    release.publish()
    assert len(writes) == 2
    assert images["ghcr.io/owner/app:latest"] == images["ghcr.io/owner/app:v1.2.3"]


def test_older_release_cannot_rewind_latest(registry):
    images, writes = registry
    images["ghcr.io/owner/app:latest"] = index("v2.0.0")
    release.publish()
    assert [ref for ref, _ in writes] == ["ghcr.io/owner/app:v1.2.3"]
    assert images["ghcr.io/owner/app:latest"] == index("v2.0.0")


def test_prerelease_never_promotes_latest(registry, monkeypatch):
    _, writes = registry
    monkeypatch.setenv("RELEASE_TAG", "v1.2.3-rc.1")
    monkeypatch.setenv("RELEASE_PRERELEASE", "true")
    release.publish()
    assert [ref for ref, _ in writes] == ["ghcr.io/owner/app:v1.2.3-rc.1"]


def test_prerelease_checkbox_must_match_tag(registry, monkeypatch):
    monkeypatch.setenv("RELEASE_PRERELEASE", "true")
    with pytest.raises(ValueError, match="prerelease setting"):
        release.publish()
    assert registry[1] == []


def test_existing_version_from_another_commit_is_not_overwritten(registry):
    images, writes = registry
    images["ghcr.io/owner/app:v1.2.3"] = index("v1.2.3", "different-commit")
    with pytest.raises(ValueError, match="different or unverified"):
        release.publish()
    assert writes == []


def test_incomplete_architectures_cannot_be_released(registry):
    images, writes = registry
    images["ghcr.io/owner/app:v1.2.3"] = index("v1.2.3")
    images["ghcr.io/owner/app:v1.2.3"]["manifests"].pop()
    with pytest.raises(ValueError, match="unverified"):
        release.publish()
    assert writes == []


@pytest.mark.parametrize(
    "error,missing",
    [
        ("ERROR: ghcr.io/owner/app:latest: not found", True),
        ("manifest unknown", True),
        ("unauthorized", False),
        ("connection timed out", False),
    ],
)
def test_registry_failures_are_not_treated_as_absent_tags(monkeypatch, error, missing):
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr=error),
    )
    if missing:
        assert release.inspect("ghcr.io/owner/app:latest") is None
    else:
        with pytest.raises(RuntimeError, match="registry access"):
            release.inspect("ghcr.io/owner/app:latest")


def test_validation_checks_both_package_versions_and_immutable_tag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASE_TAG", "v1.2.3")
    monkeypatch.setenv("RELEASE_PRERELEASE", "false")
    monkeypatch.setenv("GITHUB_SHA", "tag-commit")
    monkeypatch.setattr(release.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(release.subprocess, "check_output", lambda *a, **k: "tag-commit\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
    module = tmp_path / "src/talaria/__init__.py"
    module.parent.mkdir(parents=True)
    module.write_text('__version__ = "1.2.3"\n')
    release.validate()
    module.write_text('__version__ = "1.2.2"\n')
    with pytest.raises(ValueError, match="must agree"):
        release.validate()
    monkeypatch.setattr(release.subprocess, "check_output", lambda *a, **k: "moved-commit\n")
    with pytest.raises(ValueError, match="no longer points"):
        release.validate()


def test_workflow_gates_publication_and_keeps_prs_lightweight():
    ci = yaml.load((ROOT / ".github/workflows/ci.yml").read_text())
    workflow = yaml.load((ROOT / ".github/workflows/release.yml").read_text())
    assert workflow["on"] == {"release": {"types": ["published"]}}
    assert workflow["jobs"]["checks"]["with"]["full"] is True
    assert workflow["jobs"]["publish"]["needs"] == "checks"
    assert workflow["jobs"]["manifest"]["needs"] == "publish"
    assert "repository.private" not in json.dumps(ci)
    for name in ("platforms", "browsers", "hermes", "wsl"):
        assert ci["jobs"][name]["if"] == "inputs.full"
    steps = workflow["jobs"]["publish"]["steps"]
    smoke = next(i for i, step in enumerate(steps) if "docker_smoke.py" in step.get("run", ""))
    push = next(i for i, step in enumerate(steps) if step.get("id") == "publish")
    assert smoke < push
    assert "push-by-digest=true" in steps[push]["with"]["outputs"]


def test_new_stable_release_advances_latest(registry):
    images, writes = registry
    images["ghcr.io/owner/app:latest"] = index("v1.2.2", "previous-commit")
    release.publish()
    assert writes[-1] == ("ghcr.io/owner/app:latest", ["ghcr.io/owner/app:v1.2.3"])
    assert images["ghcr.io/owner/app:latest"] == index("v1.2.3")


def test_latest_with_same_version_but_wrong_content_is_reported(registry):
    images, _ = registry
    images["ghcr.io/owner/app:latest"] = index("v1.2.3", "wrong-commit")
    with pytest.raises(ValueError, match="unverified"):
        release.publish()


def test_invalid_digest_cannot_be_published(registry):
    Path("digests/arm64").write_text("invalid")
    with pytest.raises(ValueError, match="digest"):
        release.publish()
    assert registry[1] == []
