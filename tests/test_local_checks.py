"""Exercise the local gates against disposable Git repositories."""

import subprocess
import sys
from pathlib import Path

import pytest

from contrib import check

pytestmark = pytest.mark.quality


@pytest.mark.parametrize(
    "paths,backend,browser",
    [
        (["README.md", ".github/SECURITY.md"], False, False),
        (["contrib/check.py"], False, False),
        (["src/talaria/static/runs.js"], False, True),
        (["src/talaria/setup.py"], True, False),
        (["src/talaria/routes.py"], True, True),
    ],
)
def test_public_ci_selects_runtime_changes_without_full_matrix(paths, backend, browser):
    scope = check.ci_scope(paths)
    assert (scope["backend"] is not None) == backend
    assert scope["browsers"] == browser


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], check=True)
    return tmp_path


def commit():
    subprocess.run(["git", "add", "."], check=True)
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "Fixture"], check=True
    )
    return check.git("rev-parse", "HEAD")


@pytest.mark.parametrize(
    "broken", ["def broken(:\n", "print( 42 )\n", "def abandoned_function():\n    return 42\n"]
)
def test_pre_commit_checks_staged_content_not_the_corrected_working_file(repo, broken):
    from tests.test_quality import prepare_gate_repo

    prepare_gate_repo(repo)
    subprocess.run(["git", "add", "."], check=True)
    source = repo / "src/talaria/example.py"
    source.write_text(broken)
    subprocess.run(["git", "add", "src/talaria/example.py"], check=True)
    source.write_text("print(42)\n")
    with pytest.raises(subprocess.CalledProcessError):
        check.pre_commit()
    subprocess.run(["git", "add", "src/talaria/example.py"], check=True)
    check.pre_commit()


def test_push_tests_the_pushed_revision_even_with_uncommitted_edits(repo, monkeypatch):
    import io

    source = repo / "app.py"
    source.write_text("answer = 42\n")
    revision = commit()
    source.write_text("answer = 'uncommitted'\n")
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(f"refs/heads/main {revision} remote {'0' * 40}\n")
    )
    calls = []

    def inspect(*args, cwd, env):
        calls.append(args)
        assert (cwd / "app.py").read_text() == "answer = 42\n"
        assert "PYTHONPATH" not in env
        assert not any(key.startswith("GIT_") for key in env)

    monkeypatch.setattr(check, "run", inspect)
    check.pre_push()
    assert len(calls) == 1
    assert source.read_text() == "answer = 'uncommitted'\n"


def test_docs_and_deleted_refs_do_not_launch_tests(repo, monkeypatch):
    import io

    (repo / "README.md").write_text("Before\n")
    base = commit()
    (repo / "README.md").write_text("After\n")
    revision = commit()
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            f"refs/heads/main {revision} remote {base}\n(delete) {'0' * 40} remote {base}\n"
        ),
    )
    monkeypatch.setattr(check, "run", lambda *a, **k: pytest.fail("Unexpected runtime check"))
    check.pre_push()


def test_failed_push_check_propagates_the_failure(repo, monkeypatch):
    import io

    (repo / "app.py").write_text("answer = 42\n")
    revision = commit()
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(f"refs/heads/main {revision} remote {'0' * 40}\n")
    )

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(check, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        check.pre_push()


def test_docs_only_ref_cannot_skip_a_code_update_to_another_ref(repo, monkeypatch):
    import io

    (repo / "app.py").write_text("answer = 0\n")
    old = commit()
    (repo / "app.py").write_text("answer = 42\n")
    code = commit()
    (repo / "README.md").write_text("Documentation\n")
    revision = commit()
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            f"refs/heads/docs {revision} docs {code}\nrefs/heads/main {revision} main {old}\n"
        ),
    )
    calls = []
    monkeypatch.setattr(check, "run", lambda *args, **kwargs: calls.append(args))
    check.pre_push()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "paths,backend,browsers,native",
    [
        (
            ["src/talaria/setup.py", "README.md"],
            check.INSTALL_TESTS,
            False,
            ["tests/test_setup.py"],
        ),
        (["contrib/check.py"], ["tests/test_local_checks.py", "tests/test_quality.py"], False, []),
        (["src/talaria/static/markdown.js"], None, True, []),
        (["src/talaria/routes.py"], [], True, []),
        (["uv.lock"], [], True, ["tests"]),
        (["unknown-new-component.py"], [], True, []),
        (["README.md"], None, False, []),
    ],
)
def test_check_selection_is_focused_with_a_conservative_fallback(paths, backend, browsers, native):
    scope = check.plan(paths)
    assert (scope["backend"], scope["browsers"], scope["native"]) == (backend, browsers, native)


@pytest.mark.parametrize(
    "path",
    [*sorted(check.I18N_FILES), "src/talaria/static/locales/fr.json"],
)
def test_translation_changes_run_catalog_and_both_browser_checks(path, monkeypatch):
    calls = []
    monkeypatch.setattr(check, "lint", lambda **kwargs: None)
    monkeypatch.setattr(check, "pytest", lambda *args, env: calls.append((args, env)))
    check.check(paths=[path])
    backend = [(args, env) for args, env in calls if "not browser and not hermes" in args]
    assert len(backend) == 1 and check.I18N_CATALOGS in backend[0][0]
    browser_calls = [(args, env) for args, env in calls if "browser" in args]
    assert {env["TALARIA_TEST_BROWSER"] for _, env in browser_calls} == {"chromium", "webkit"}
    assert all(check.I18N_BROWSER in args for args, _ in browser_calls)
    assert all(any(arg.startswith("tests/") for arg in args) for args, _ in browser_calls)


@pytest.mark.parametrize(
    "paths, expected_backend, expected_native",
    [
        (["README.md"], [check.I18N_CATALOGS], []),
        (
            ["contrib/check.py"],
            ["tests/test_local_checks.py", "tests/test_quality.py", check.I18N_CATALOGS],
            [],
        ),
        (["src/talaria/static/markdown.js"], [check.I18N_CATALOGS], []),
        (["src/talaria/routes.py"], [], []),
        (
            ["src/talaria/setup.py"],
            [*check.INSTALL_TESTS, check.I18N_CATALOGS],
            ["tests/test_setup.py"],
        ),
    ],
)
def test_translation_checks_preserve_other_changed_scopes(paths, expected_backend, expected_native):
    scope = check.plan([*paths, "contrib/i18n.py"])
    assert scope["backend"] == expected_backend
    assert scope["native"] == expected_native
    assert scope["browsers"] and check.I18N_BROWSER in scope["extra_browser"]


def test_ordinary_frontend_changes_do_not_add_translation_browser_checks(monkeypatch):
    calls = []
    monkeypatch.setattr(check, "lint", lambda **kwargs: None)
    monkeypatch.setattr(check, "pytest", lambda *args, **kwargs: calls.append(args))
    check.check(paths=["src/talaria/static/markdown.js"])
    assert len(calls) == 2
    assert all(check.I18N_BROWSER not in args for args in calls)


def test_changed_checks_include_new_untracked_tests(repo):
    (repo / "app.py").write_text("answer = 42\n")
    base = commit()
    (repo / "new_test.py").write_text("def test_new(): pass\n")
    assert check.changed_paths(base) == ["new_test.py"]


def test_only_successful_matching_revision_checks_are_reused(repo, monkeypatch):
    (repo / "app.py").write_text("answer = 42\n")
    revision = commit()
    calls = []
    monkeypatch.setattr(check, "run", lambda *a, **kw: calls.append(a))
    check.verify_revision(revision, ["app.py"])
    check.verify_revision(revision, ["app.py"])
    assert len(calls) == 1
    monkeypatch.setenv("PYTEST_ADDOPTS", "--maxfail=1")
    check.verify_revision(revision, ["app.py"])
    assert len(calls) == 2

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(check, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        check.verify_revision(revision, ["app.py"], fresh=True)
    monkeypatch.setattr(check, "run", lambda *a, **kw: calls.append(a))
    check.verify_revision(revision, ["app.py"])
    assert len(calls) == 3
    # Native dependencies are external to the Talaria revision and never cached.
    monkeypatch.setenv("HERMES_SOURCE", "/test/hermes")
    check.verify_revision(revision, ["src/talaria/setup.py"])
    check.verify_revision(revision, ["src/talaria/setup.py"])
    assert len(calls) == 6  # One ordinary check and two fresh native checks.
    assert calls[-2][-2:] == calls[-1][-2:] == ("-m", "hermes")


def test_failed_native_check_does_not_discard_successful_local_evidence(repo, monkeypatch):
    (repo / "app.py").write_text("answer = 42\n")
    revision = commit()
    monkeypatch.setenv("HERMES_SOURCE", "/test/hermes")
    calls = []
    failing = True

    def run(*args, **kwargs):
        calls.append(args)
        if args[-2:] == ("-m", "hermes") and failing:
            raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(check, "run", run)
    for _ in range(2):
        with pytest.raises(subprocess.CalledProcessError):
            check.verify_revision(revision, ["app.py"], native=True)
    failing = False
    check.verify_revision(revision, ["app.py"], native=True)
    assert len(calls) == 4
    assert "--skip-native" in calls[0]
    assert all(args[-2:] == ("-m", "hermes") for args in calls[1:])
    (repo / "app.py").write_text("answer = 43\n")
    newer = commit()
    check.verify_revision(newer, ["app.py"], native=True)
    assert len(calls) == 6 and "--skip-native" in calls[-2]


@pytest.mark.parametrize("full", [False, True])
def test_platform_checks_keep_portable_contracts_and_run_general_logic_once(full, monkeypatch):
    calls = []
    monkeypatch.setattr(check, "pytest", lambda *args, **kwargs: calls.append(args))
    check.platform_check(full=full)
    paths = [arg for arg in calls[0] if arg.startswith("tests/")]
    assert paths == ([] if full else check.PLATFORM_TESTS)
    assert "not browser and not hermes and not quality" in calls[0]
    assert {
        "tests/test_setup.py",
        "tests/test_deployment.py",
        "tests/test_supervisor.py",
        "tests/test_operations_qa.py",
        "tests/test_uninstall.py",
        "tests/test_cli_shutdown.py",
    } <= set(check.PLATFORM_TESTS)


def test_browser_shards_cover_each_case_once_and_ignore_collection_order():
    from types import SimpleNamespace

    from tests.conftest import browser_shard

    items = [
        SimpleNamespace(nodeid=f"tests/test_{i // 4}.py::test_case[{i % 4}]") for i in range(19)
    ]
    parts = [browser_shard(items, f"{index}/2") for index in (1, 2)]
    assert sorted(item.nodeid for part in parts for item in part) == sorted(i.nodeid for i in items)
    assert abs(len(parts[0]) - len(parts[1])) <= 1
    assert browser_shard(list(reversed(items)), "1/2") == parts[0]
    assert browser_shard(items, "1/1") == sorted(items, key=lambda item: item.nodeid)


@pytest.mark.parametrize("shard", ["", "0/2", "3/2", "1/0", "1/17", "1", "-1/2", "1/x"])
def test_invalid_browser_shards_fail_instead_of_omitting_tests(shard):
    from tests.conftest import browser_shard

    with pytest.raises(pytest.UsageError):
        browser_shard([], shard)


def test_missing_accessibility_engine_fails_before_running_other_checks(monkeypatch):
    monkeypatch.delenv("TALARIA_AXE_PATH", raising=False)
    monkeypatch.setattr(
        check, "lint", lambda **kwargs: pytest.fail("Prerequisites were not checked")
    )
    with pytest.raises(SystemExit, match="TALARIA_AXE_PATH"):
        check.check(paths=["tests/test_accessibility.py"])


@pytest.mark.parametrize("mode", ["fixed", "unfixed", "unchanged", "setup-error"])
def test_proof_requires_a_failure_before_and_success_after(repo, mode):
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "uv.lock").write_text("unchanged dependency fixture\n")
    (repo / "tests/conftest.py").write_text(
        "def pytest_addoption(parser):\n"
        "    parser.addoption('--fail-on-skip', action='store_true')\n"
    )
    source = repo / "src/example.py"
    source.write_text("answer = 42\n" if mode == "unchanged" else "answer = 0\n")
    if mode == "setup-error":
        source.unlink()
    baseline = commit()
    (repo / "tests/test_example.py").write_text(
        "from example import answer\ndef test_answer():\n    assert answer == 42\n"
    )
    source.write_text("answer = 0\n" if mode == "unfixed" else "answer = 42\n")
    if mode == "fixed":
        check.prove(baseline, ["tests/test_example.py::test_answer"])
    else:
        with pytest.raises(SystemExit, match="unexpected"):
            check.prove(baseline, ["tests/test_example.py::test_answer"])
    assert Path("src/example.py").read_text() == source.read_text()
