"""Exercise the local gates against disposable Git repositories."""

import subprocess
import sys
from pathlib import Path

import pytest

from contrib import check


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
    check.verify_revision(revision, ["src/talaria/setup.py"])
    check.verify_revision(revision, ["src/talaria/setup.py"])
    assert len(calls) == 5


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
