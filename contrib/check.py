"""Local checks and regression evidence. No services or production credentials needed."""

import argparse
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

STREAMING = [
    "tests/test_response_turns.py",
    "tests/test_stream_reveal.py",
]
FOCUSED = STREAMING + [
    "tests/test_frontend_state_qa.py",
    "tests/test_message_submission.py",
]
INSTALL_TESTS = [
    "tests/test_setup.py", "tests/test_install.py", "tests/test_deployment.py",
    "tests/test_uninstall.py",
    "tests/test_operations_qa.py", "tests/test_environments.py", "tests/test_proxy_paths.py",
]
INSTALL_FILES = {
    "install.sh", "src/talaria/cli.py", "src/talaria/credentials.py",
    "src/talaria/deployment.py", "src/talaria/plugin_install.py",
    "src/talaria/uninstall.py", "tests/test_uninstall.py",
    "src/talaria/setup.py", "src/talaria/setup_services.py", "src/talaria/setup_hermes.py",
    "tests/test_setup.py", "tests/test_install.py", "tests/test_deployment.py",
    "tests/test_operations_qa.py",
}
CHECK_FILES = {"contrib/check.py", "tests/test_local_checks.py"}


def plan(paths=None):
    if paths is None:
        return {"backend": [], "browsers": True, "native": [], "extra_browser": []}
    paths = [path for path in paths if not docs_only([path])]
    if not paths:
        return {"backend": None, "browsers": False, "native": [], "extra_browser": []}
    local = all(path in CHECK_FILES or path.startswith(".githooks/") for path in paths)
    installer = all(path in INSTALL_FILES or path in CHECK_FILES for path in paths)
    frontend = all(path.startswith("src/talaria/static/") for path in paths)
    native = any(
        "hermes" in path or "contract" in path
        or path in {
            "src/talaria/setup.py", "src/talaria/plugin_install.py", "tests/test_setup.py",
            "pyproject.toml", "uv.lock", "tests/conftest.py", "tests/test_extended_access.py",
        }
        for path in paths
    )
    backend = None if frontend else []
    if local:
        backend = ["tests/test_local_checks.py"]
    elif installer:
        backend = list(INSTALL_TESTS)
        if any(p in CHECK_FILES for p in paths):
            backend.append("tests/test_local_checks.py")
    return {
        "backend": backend,
        "browsers": not (local or installer),
        "native": (["tests/test_setup.py"] if installer else ["tests"]) if native else [],
        "extra_browser": [p for p in paths if p.startswith("tests/test_") and p.endswith(".py")],
    }


def workers():
    available = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    try:
        count = int(os.environ.get("TALARIA_TEST_WORKERS", min(4, available or 1)))
    except ValueError:
        raise SystemExit("TALARIA_TEST_WORKERS must be a number from 0 to 16.") from None
    if not 0 <= count <= 16:
        raise SystemExit("TALARIA_TEST_WORKERS must be a number from 0 to 16.")
    return count


def run(*args, **kwargs):
    print("+", " ".join(map(str, args)), flush=True)
    return subprocess.run(args, check=True, **kwargs)


def git(*args):
    return subprocess.check_output(["git", *args]).decode().strip()


def export(revision, destination):
    data = subprocess.check_output(["git", "archive", revision])
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(destination, filter="data")


def environment():
    # Git exports repository-local variables to hooks. Child tests create their
    # own repositories; never let those commands operate on the caller's repo.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    # Fresh-wheel subprocesses must import their installed package, not source
    # injected by a parent shell or the regression-proof runner.
    for key in ("PYTHONPATH", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"):
        env.pop(key, None)
    return env


def lint(paths=None):
    run("ruff", "check", ".", ".github/scripts")
    scripts = Path("src/talaria/static").glob("*.js") if paths is None else (
        Path(path) for path in paths if path.endswith(".js") and Path(path).is_file()
    )
    for path in scripts:
        run("node", "--check", str(path))


def pytest(*args, env=None):
    run(sys.executable, "-m", "pytest", "-q", "-n", str(workers()),
        "--dist", "worksteal", "--fail-on-skip", *args, env=env)


def check(full=False, native=False, paths=None):
    env = environment()
    scope = plan(None if full else paths)
    if full or native:
        scope["native"] = ["tests"]
    if scope["native"] and not env.get("HERMES_SOURCE"):
        raise SystemExit("Native checks need HERMES_SOURCE (see CONTRIBUTING.md).")
    if full:
        axe = Path(env.get("TALARIA_AXE_PATH", ""))
        digest = "c24f097bd2f451d4f933e8bc7d8d539f8672a2ebcb5cc9f9f3eec8ca9470a0c1"
        if not axe.is_file() or hashlib.sha256(axe.read_bytes()).hexdigest() != digest:
            raise SystemExit("Set TALARIA_AXE_PATH to the verified script in CONTRIBUTING.md.")
    lint(paths=None if full else paths)
    if scope["backend"] is not None:
        pytest(*scope["backend"], "-m", "not browser and not hermes", env=env)
    browsers = (("chromium", "firefox", "webkit") if full else ("chromium", "webkit"))
    for browser in browsers if scope["browsers"] else ():
        print(f"Browser checks: {browser}", flush=True)
        browser_env = {**env, "TALARIA_TEST_BROWSER": browser}
        if full:
            pytest("-m", "browser", env=browser_env)
        else:
            files = FOCUSED + scope["extra_browser"] if browser == "chromium" else STREAMING
            pytest(*(file for file in dict.fromkeys(files) if Path(file).is_file()),
                   "-m", "browser", env=browser_env)
    if scope["native"]:
        pytest(*scope["native"], "-m", "hermes", env=env)


def pre_commit():
    paths = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0")
    for path in filter(None, paths):
        data = subprocess.check_output(["git", "show", f":{path}"])
        if path.endswith(".py"):
            run("ruff", "check", "--stdin-filename", path, "-", input=data)
        elif path.endswith(".js") and "/vendor/" not in path:
            run("node", "--input-type=module", "--check", input=data)
    run("git", "diff", "--cached", "--check")


def docs_only(paths):
    return bool(paths) and all(
        path in {"README.md", "CONTRIBUTING.md", "LICENSE", "CHANGELOG.md"}
        or path.startswith("docs/")
        for path in paths
    )


def changed_paths(base, revision=None):
    paths = git("diff", "--name-only", "--no-renames", "-z", base,
                *([revision] if revision else [])).split("\0")
    if revision is None:
        paths += git("ls-files", "--others", "--exclude-standard", "-z").split("\0")
    return sorted(set(filter(None, paths)))


def check_environment():
    env = environment()
    if not env.get("HERMES_SOURCE"):
        source = subprocess.run(
            ["git", "config", "--get", "talaria.hermesSource"],
            capture_output=True, text=True,
        ).stdout.strip()
        if source:
            env["HERMES_SOURCE"] = source
    return env


def verify_revision(commit, paths, *, fresh=False, native=False):
    """Only immutable snapshots can earn a reusable, short-lived local result."""
    env = check_environment()
    scope = plan(paths)
    if native:
        scope["native"] = ["tests"]
    cache = Path(git("rev-parse", "--git-path", "talaria-checks.json"))
    # Native Hermes is mutable external input; always recheck it.
    key = None
    records = {}
    if not scope["native"]:
        identity = [git("rev-parse", f"{commit}^{{tree}}"), scope, sys.version,
                    sys.executable, platform.platform(), workers(),
                    subprocess.check_output(["node", "--version"]).decode().strip(),
                    subprocess.check_output(["uv", "--version"]).decode().strip(),
                    {k: v for k, v in env.items()
                     if k.startswith(("PYTEST_", "TALARIA_", "PLAYWRIGHT_", "NODE_", "UV_"))}]
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        try:
            records = {k: v for k, v in json.loads(cache.read_text()).items()
                       if isinstance(v, (int, float)) and 0 <= time.time() - v < 3600}
        except (OSError, ValueError, AttributeError):
            pass
        if not fresh and key in records:
            print(f"Reusing successful local checks for {commit[:10]} (under one hour old).")
            return
        if key in records:
            del records[key]
            cache.write_text(json.dumps(records))
    with tempfile.TemporaryDirectory(prefix="talaria-push-") as directory:
        root = Path(directory)
        export(commit, root)
        run("uv", "run", "--locked", "python", str(root / "contrib/check.py"),
            "check", *(["--native"] if native else []),
            *(["--paths", *paths] if paths else []), cwd=root, env=env)
    if key:
        records[key] = time.time()
        records = dict(sorted(records.items(), key=lambda item: item[1])[-32:])
        try:
            cache.write_text(json.dumps(records))
        except OSError:
            print("Checks passed; the optional local result cache could not be saved.")


def pre_push():
    checked = set()
    for line in sys.stdin.read().splitlines():
        _, revision, _, remote = line.split()
        if set(revision) == {"0"}:
            continue  # Deleting a ref has no revision to test.
        commit = git("rev-parse", f"{revision}^{{commit}}")
        try:
            base = remote if set(remote) != {"0"} else git("merge-base", commit, "origin/main")
            paths = changed_paths(base, commit)
        except subprocess.CalledProcessError:
            paths = git("ls-tree", "-r", "--name-only", "-z", commit).split("\0")
        paths = list(filter(None, paths))
        if not paths or docs_only(paths):
            print("Documentation-only push: no runtime checks needed.", flush=True)
            continue
        selection = (commit, json.dumps(plan(paths), sort_keys=True))
        if selection not in checked:
            verify_revision(commit, paths)
            checked.add(selection)


def cases(path):
    return ET.parse(path).findall(".//testcase")


def prove(base, selectors):
    # Use today's tests with yesterday's application code. Do not stash or
    # replace the working tree. Dependency changes need a separate migration test.
    with tempfile.TemporaryDirectory(prefix="talaria-proof-") as directory:
        root = Path(directory)
        old = root / "before"
        export(base, old)
        if (old / "uv.lock").read_bytes() != Path("uv.lock").read_bytes():
            raise SystemExit("The baseline has a different lockfile; choose a compatible revision.")
        shutil.rmtree(old / "tests")
        shutil.copytree("tests", old / "tests", ignore=shutil.ignore_patterns("__pycache__"))
        reports = []
        for label, cwd in (("after", Path.cwd()), ("before", old)):
            report = root / f"{label}.xml"
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys, pytest; sys.path.insert(0, sys.argv.pop(1)); "
                    "raise SystemExit(pytest.main(sys.argv[1:]))",
                    str(cwd / "src"),
                    "-q",
                    "--tb=short",
                    "--fail-on-skip",
                    f"--junitxml={report}",
                    *selectors,
                ],
                cwd=cwd,
                env=environment(),
            )
            if result.returncode != (0 if label == "after" else 1) or not report.exists():
                raise SystemExit(f"No regression proof: unexpected {label} test result.")
            tests = cases(report)
            if not tests or any(
                t.find("error") is not None or t.find("skipped") is not None for t in tests
            ):
                raise SystemExit(
                    "Setup errors, skips, and missing tests are not regression evidence."
                )
            if label == "before" and any(t.find("failure") is None for t in tests):
                raise SystemExit("Every selected regression must fail before the fix.")
            reports.append({(t.get("classname"), t.get("name")) for t in tests})
        if reports[0] != reports[1]:
            raise SystemExit("The two revisions did not execute the same test cases.")
        print(f"Verified: {len(reports[0])} regression(s) fail at {base} and pass now.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("pre-commit", "pre-push", "lint", "full"):
        commands.add_parser(name)
    command = commands.add_parser("check")
    command.add_argument("--native", action="store_true")
    selection = command.add_mutually_exclusive_group()
    selection.add_argument("--changed", action="store_true", help="Test the changed areas locally")
    selection.add_argument("--revision", help="Check and cache an immutable committed revision")
    selection.add_argument("--paths", nargs="+", help=argparse.SUPPRESS)
    command.add_argument("--base", default="origin/main", help="Comparison base for focused checks")
    command.add_argument("--fresh", action="store_true", help="Ignore cached revision results")
    command = commands.add_parser("prove")
    command.add_argument("--base", required=True)
    command.add_argument("tests", nargs="+")
    args = parser.parse_args()
    if args.command == "pre-commit":
        pre_commit()
    elif args.command == "pre-push":
        pre_push()
    elif args.command == "lint":
        lint()
    elif args.command == "prove":
        prove(args.base, args.tests)
    elif args.command == "check" and args.revision:
        revision = git("rev-parse", f"{args.revision}^{{commit}}")
        base = git("merge-base", args.base, revision)
        paths = changed_paths(base, revision)
        if args.native or (paths and not docs_only(paths)):
            verify_revision(revision, paths, fresh=args.fresh, native=args.native)
        else:
            print("No runtime changes to check.")
    else:
        paths = getattr(args, "paths", None)
        if getattr(args, "changed", False):
            paths = changed_paths(git("merge-base", args.base, "HEAD"))
        # Share local prerequisite configuration with the isolated push runner.
        env = check_environment()
        if env.get("HERMES_SOURCE"):
            os.environ["HERMES_SOURCE"] = env["HERMES_SOURCE"]
        check(full=args.command == "full", native=getattr(args, "native", False), paths=paths)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
