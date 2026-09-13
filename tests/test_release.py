"""Release versions and registry failures must never silently replace stable images."""

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest
from pre_commit_hooks.check_yaml import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release", ROOT / ".github/scripts/release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.mark.parametrize("git_exit,install_exit", [(0, 0), (2, 0), (128, 0), (0, 1)])
def test_documented_plugin_install_requires_stable_pin_before_install_or_restart(
    tmp_path, git_exit, install_exit
):
    command = next(
        block.split("```", 1)[0]
        for block in (ROOT / "README.md").read_text().split("```sh\n")[1:]
        if "hermes plugins install" in block
    )
    commit = "a" * 40
    git = tmp_path / "git"
    git.write_text(
        '#!/bin/sh\n[ "$GIT_EXIT" = 0 ] || exit "$GIT_EXIT"\n'
        f"printf '%s\\trefs/heads/stable\\n' {commit}\n"
    )
    hermes = tmp_path / "hermes"
    hermes.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n[ "$1" != plugins ] || exit "$INSTALL_EXIT"\n'
    )
    git.chmod(0o755)
    hermes.chmod(0o755)
    calls = tmp_path / "calls"
    result = subprocess.run(
        ["bash", "-c", command],
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "GIT_EXIT": str(git_exit),
            "INSTALL_EXIT": str(install_exit),
            "CALLS": str(calls),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == (git_exit or install_exit), result.stderr
    recorded = calls.read_text().splitlines() if calls.exists() else []
    if git_exit:
        assert recorded == []
    else:
        assert recorded[0].endswith("--enable --ref " + commit)
        assert recorded[1:] == ([] if install_exit else ["gateway restart"])


@pytest.mark.parametrize("exit_code", [0, 7, 124, 137])
def test_browser_log_pipeline_preserves_failure_and_deadline_status(tmp_path, exit_code):
    workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text())
    step = next(
        s for s in workflow["jobs"]["browsers"]["steps"] if s.get("name") == "Browser regressions"
    )
    assert step["shell"] == "bash"  # Actions enables pipefail for an explicit bash shell.
    commands = {
        "vmstat": "exec sleep 60",
        "timeout": 'shift 2\nexec "$@"',
        "uv": f"echo synthetic-browser-result\nprintf '%0800000d\\n' 0\nexit {exit_code}",
    }
    for name, body in commands.items():
        path = tmp_path / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert max(map(len, result.stdout.splitlines())) < 2100
    assert "line shortened; see browser.log" in result.stdout
    saved = (tmp_path / "test-results/browser.log").read_text()
    assert "synthetic-browser-result" in saved
    assert max(map(len, saved.splitlines())) == 800000


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
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
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
    assert workflow["jobs"]["manifest"]["permissions"]["statuses"] == "write"
    assert all(
        job.get("permissions", {}).get("statuses") != "write"
        for name, job in workflow["jobs"].items()
        if name != "manifest"
    )
    assert ci["jobs"]["behavior"]["if"] == (
        "github.event_name == 'pull_request' && !github.event.repository.private"
    )
    for name in ("platforms", "wsl"):
        assert ci["jobs"][name]["if"] == "inputs.full"
    assert ci["on"]["workflow_dispatch"]["inputs"]["browser"]["options"] == [
        "none",
        "chromium",
        "firefox",
        "webkit",
    ]
    assert ci["jobs"]["browsers"]["if"].startswith("inputs.full || ")
    assert "inputs.full &&" in ci["jobs"]["browsers"]["strategy"]["matrix"]["browser"]
    quality = next(
        step
        for step in ci["jobs"]["lint"]["steps"]
        if step.get("name") == "Verify the quality gates"
    )
    assert quality["if"] == "inputs.full"
    assert "-m quality --fail-on-skip" in quality["run"]
    assert "not quality" in json.dumps(ci["jobs"]["platforms"])
    assert "not quality" in (ROOT / ".github/scripts/wsl.sh").read_text()
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


@pytest.mark.parametrize("existing", [None, "older-commit", "test-commit"])
def test_stable_source_promotes_only_verified_images(registry, monkeypatch, existing):
    images, _ = registry
    images["ghcr.io/owner/app:latest"] = index("v1.2.3")
    writes = []
    revision = existing

    def github(path, data=None, **kwargs):
        nonlocal revision
        if data:
            writes.append((path, data))
            if "sha" in data:
                revision = data["sha"]
        return {"object": {"sha": revision}} if revision else None

    monkeypatch.setattr(release, "github", github)
    release.promote()
    assert revision == "test-commit"
    if existing == "test-commit":
        assert writes == []
        return
    assert writes[0] == (
        "repos/owner/app/statuses/test-commit",
        {
            "state": "success",
            "context": "Talaria stable release",
            "description": "v1.2.3: tests and Docker images verified",
            "target_url": "https://github.com/owner/app/actions/runs/123",
        },
    )
    if existing:
        assert writes[1:] == [
            ("repos/owner/app/git/refs/heads/stable", {"sha": "test-commit", "force": False})
        ]
    else:
        assert writes[1:] == [
            ("repos/owner/app/git/refs", {"ref": "refs/heads/stable", "sha": "test-commit"})
        ]


def test_failed_release_status_cannot_advance_stable(registry, monkeypatch):
    registry[0]["ghcr.io/owner/app:latest"] = index("v1.2.3")
    calls = []

    def github(path, data=None, **kwargs):
        calls.append((path, data))
        if data:
            raise RuntimeError("Status publication failed")
        return {"object": {"sha": "previous-commit"}}

    monkeypatch.setattr(release, "github", github)
    with pytest.raises(RuntimeError, match="Status publication failed"):
        release.promote()
    assert [path for path, _ in calls] == [
        "repos/owner/app/git/ref/heads/stable",
        "repos/owner/app/statuses/test-commit",
    ]


@pytest.mark.parametrize("tag,prerelease", [("v1.2.3-rc.1", "true"), ("v1.2.2", "false")])
def test_prerelease_or_older_rerun_cannot_promote_source(registry, monkeypatch, tag, prerelease):
    registry[0]["ghcr.io/owner/app:latest"] = index("v1.2.3")
    monkeypatch.setenv("RELEASE_TAG", tag)
    monkeypatch.setenv("RELEASE_PRERELEASE", prerelease)
    monkeypatch.setattr(release, "github", lambda *a, **k: pytest.fail("Unexpected promotion"))
    release.promote()


@pytest.mark.parametrize("invalid", [None, "wrong-commit", "missing-architecture"])
def test_unverified_images_cannot_promote_source(registry, monkeypatch, invalid):
    if invalid:
        manifest = index("v1.2.3", invalid if invalid == "wrong-commit" else "test-commit")
        if invalid == "missing-architecture":
            manifest["manifests"].pop()
        registry[0]["ghcr.io/owner/app:latest"] = manifest
    monkeypatch.setattr(release, "github", lambda *a, **k: pytest.fail("Unexpected promotion"))
    with pytest.raises(ValueError):
        release.promote()


@pytest.mark.parametrize("error", ["HTTP 403", "HTTP 422", "connection timed out"])
def test_source_promotion_fails_closed_on_github_errors(monkeypatch, error):
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr=error),
    )
    with pytest.raises(RuntimeError, match="promotion failed"):
        release.github("repos/owner/app/git/ref/heads/stable", missing=True)


@pytest.mark.parametrize(
    "full,public,failed,expected",
    [
        (False, False, None, 0),
        (False, True, "behavior", 1),
        (False, True, None, 0),
        (True, False, "hermes", 1),
        (True, False, None, 0),
    ],
)
def test_required_barrier_cannot_hide_missing_behavior_or_release_jobs(
    full, public, failed, expected
):
    import os
    import sys

    ci = yaml.load((ROOT / ".github/workflows/ci.yml").read_text())
    jobs = {name: {"result": "skipped"} for name in ci["jobs"]["required"]["needs"]}
    required = {"lint"}
    if full:
        required.update({"platforms", "browsers", "hermes", "wsl"})
    if public:
        required.add("behavior")
    for name in required - {failed}:
        jobs[name]["result"] = "success"
    script = ci["jobs"]["required"]["steps"][0]["run"].split("\n", 1)[1].rsplit("\nPY", 1)[0]
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "FULL": str(full).lower(),
            "PUBLIC_PR": str(public).lower(),
            "NATIVE": "false",
            "RESULTS": json.dumps(jobs),
        },
    )
    assert result.returncode == expected, result.stderr
