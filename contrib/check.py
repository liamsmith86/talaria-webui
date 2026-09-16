"""Local checks and regression evidence. No services or production credentials needed."""

import argparse
import contextlib
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
    "tests/test_reply_settlement.py",
]
FOCUSED = [*STREAMING, "tests/test_frontend_state_qa.py", "tests/test_message_submission.py"]
INSTALL_TESTS = [
    "tests/test_setup.py",
    "tests/test_install.py",
    "tests/test_deployment.py",
    "tests/test_supervisor.py",
    "tests/test_updates.py",
    "tests/test_uninstall.py",
    "tests/test_operations_qa.py",
    "tests/test_environments.py",
    "tests/test_proxy_paths.py",
]
PLATFORM_TESTS = [
    *INSTALL_TESTS,
    "tests/test_cli_shutdown.py",
    "tests/test_environment_key.py",
    "tests/test_request_log.py",
    "tests/test_container_health.py",
]
INSTALL_FILES = {
    "src/talaria/control.py",
    "src/talaria/supervisor.py",
    "src/talaria/supervisor_cli.py",
    "src/talaria/supervisor_worker.py",
    "tests/test_supervisor.py",
    "install.sh",
    "src/talaria/cli.py",
    "src/talaria/credentials.py",
    "src/talaria/deployment.py",
    "src/talaria/plugin_install.py",
    "src/talaria/uninstall.py",
    "tests/test_uninstall.py",
    "src/talaria/setup.py",
    "src/talaria/setup_services.py",
    "src/talaria/setup_hermes.py",
    "tests/test_setup.py",
    "tests/test_install.py",
    "tests/test_deployment.py",
    "tests/test_operations_qa.py",
}
CHECK_FILES = {
    "contrib/check.py",
    "contrib/quality.py",
    "contrib/vulture_whitelist.py",
    "tests/test_local_checks.py",
    "tests/test_quality.py",
    "eslint.config.js",
    "package.json",
    "package-lock.json",
    ".npmrc",
}
I18N_BROWSER = "tests/test_i18n.py"
I18N_CATALOGS = "tests/test_i18n_catalogs.py"
I18N_FILES = {
    "src/talaria/static/i18n.js",
    "src/talaria/static/language.js",
    "contrib/i18n.mjs",
    "contrib/i18n.py",
    I18N_BROWSER,
    I18N_CATALOGS,
}


def plan(paths=None):
    if paths is None:
        return {"backend": [], "browsers": True, "native": [], "extra_browser": []}
    paths = [path for path in paths if not docs_only([path])]
    if not paths:
        return {"backend": None, "browsers": False, "native": [], "extra_browser": []}
    translated = {
        path
        for path in paths
        if path in I18N_FILES or path.startswith("src/talaria/static/locales/")
    }
    if translated:
        scope = plan([path for path in paths if path not in translated])
        if scope["backend"] is None:
            scope["backend"] = [I18N_CATALOGS]
        elif scope["backend"]:
            scope["backend"].append(I18N_CATALOGS)
        scope["browsers"] = True
        scope["extra_browser"].append(I18N_BROWSER)
        return scope
    local = all(path in CHECK_FILES or path.startswith(".githooks/") for path in paths)
    installer = all(path in INSTALL_FILES or path in CHECK_FILES for path in paths)
    frontend = all(path.startswith("src/talaria/static/") for path in paths)
    native = any(
        "hermes" in path
        or "contract" in path
        or path
        in {
            "src/talaria/setup.py",
            "src/talaria/plugin_install.py",
            "tests/test_setup.py",
            "pyproject.toml",
            "uv.lock",
            "tests/conftest.py",
            "tests/test_extended_access.py",
        }
        for path in paths
    )
    backend = None if frontend else []
    if local:
        backend = ["tests/test_local_checks.py", "tests/test_quality.py"]
    elif installer:
        backend = list(INSTALL_TESTS)
        if any(p in CHECK_FILES for p in paths):
            backend.extend(["tests/test_local_checks.py", "tests/test_quality.py"])
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
    # Whole-project static checks are cheap and detect newly orphaned code.
    run(sys.executable, "contrib/quality.py")


def pytest(*args, env=None):
    run(
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-n",
        str(workers()),
        "--dist",
        "load",
        "--maxschedchunk=1",
        "--fail-on-skip",
        *args,
        env=env,
    )


def check(full=False, native=False, paths=None, skip_native=False):
    env = environment()
    scope = plan(None if full else paths)
    if full or native:
        scope["native"] = ["tests"]
    if skip_native:
        scope["native"] = []
    if scope["native"] and not env.get("HERMES_SOURCE"):
        raise SystemExit("Native checks need HERMES_SOURCE (see CONTRIBUTING.md).")
    if full or "tests/test_accessibility.py" in scope["extra_browser"]:
        axe = Path(env.get("TALARIA_AXE_PATH", ""))
        digest = "c24f097bd2f451d4f933e8bc7d8d539f8672a2ebcb5cc9f9f3eec8ca9470a0c1"
        if not axe.is_file() or hashlib.sha256(axe.read_bytes()).hexdigest() != digest:
            raise SystemExit("Set TALARIA_AXE_PATH to the verified script in CONTRIBUTING.md.")
    lint(paths=None if full else paths)
    if scope["backend"] is not None:
        pytest(*scope["backend"], "-m", "not browser and not hermes", env=env)
    browsers = ("chromium", "firefox", "webkit") if full else ("chromium", "webkit")
    for browser in browsers if scope["browsers"] else ():
        print(f"Browser checks: {browser}", flush=True)
        browser_env = {**env, "TALARIA_TEST_BROWSER": browser}
        if full:
            pytest("-m", "browser", env=browser_env)
        else:
            files = (
                FOCUSED + scope["extra_browser"]
                if browser == "chromium"
                else STREAMING + [file for file in scope["extra_browser"] if file == I18N_BROWSER]
            )
            pytest(
                *(file for file in dict.fromkeys(files) if Path(file).is_file()),
                "-m",
                "browser",
                env=browser_env,
            )
    if scope["native"]:
        pytest(*scope["native"], "-m", "hermes", env=env)


def platform_check(full=False):
    pytest(
        *([] if full else PLATFORM_TESTS),
        "-m",
        "not browser and not hermes and not quality",
        "--junitxml=test-results/platform.xml",
        env=environment(),
    )


def pre_commit():
    # Export the index, not HEAD or the working tree: partial staging must not
    # hide violations, and deleting the last reference must reach Vulture.
    if __package__:
        from .quality import reuse_javascript
    else:
        from quality import reuse_javascript

    tree = git("write-tree")
    with tempfile.TemporaryDirectory(prefix="talaria-commit-") as directory:
        root = Path(directory)
        export(tree, root)
        reuse_javascript(root)
        run(
            "uv",
            "run",
            "--locked",
            "--only-group",
            "lint",
            "python",
            "contrib/quality.py",
            cwd=root,
            env=environment(),
        )
    run("git", "diff", "--cached", "--check")


def docs_only(paths):
    return bool(paths) and all(
        path in {"README.md", "CONTRIBUTING.md", "LICENSE", "CHANGELOG.md", ".github/SECURITY.md"}
        or path.startswith("docs/")
        for path in paths
    )


def ci_scope(paths):
    scope = plan(paths)
    # Quality-gate self-tests need the full developer toolchain at release time.
    backend = scope["backend"]
    if backend and all(
        path in {"tests/test_local_checks.py", "tests/test_quality.py"} for path in backend
    ):
        backend = None
    return {**scope, "backend": backend}


def ci_check(base, describe=False):
    scope = ci_scope(changed_paths(base, "HEAD"))
    if describe:
        print(f"runtime={str(scope['backend'] is not None or scope['browsers']).lower()}")
        print(f"browser={str(scope['browsers']).lower()}")
        return
    env = environment()
    if scope["backend"] is not None:
        pytest(*scope["backend"], "-m", "not browser and not hermes and not quality", env=env)
    if scope["browsers"]:
        files = [
            "tests/test_browser.py",
            "tests/test_http_lan.py",
            *FOCUSED,
            *scope["extra_browser"],
        ]
        pytest(
            *(path for path in dict.fromkeys(files) if Path(path).is_file()),
            "-m",
            "browser",
            "--timeout=120",
            "--timeout-method=thread",
            "--max-worker-restart=0",
            env={**env, "TALARIA_TEST_BROWSER": "chromium"},
        )


def changed_paths(base, revision=None):
    paths = git(
        "diff", "--name-only", "--no-renames", "-z", base, *([revision] if revision else [])
    ).split("\0")
    if revision is None:
        paths += git("ls-files", "--others", "--exclude-standard", "-z").split("\0")
    return sorted(set(filter(None, paths)))


def check_environment():
    env = environment()
    if not env.get("HERMES_SOURCE"):
        source = subprocess.run(
            ["git", "config", "--get", "talaria.hermesSource"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if source:
            env["HERMES_SOURCE"] = source
    return env


def verify_revision(commit, paths, *, fresh=False, native=False):
    """Cache immutable local checks separately from mutable native Hermes inputs."""
    env = check_environment()
    scope = plan(paths)
    if native:
        scope["native"] = ["tests"]
    if scope["native"] and not env.get("HERMES_SOURCE"):
        raise SystemExit("Native checks need HERMES_SOURCE (see CONTRIBUTING.md).")
    cache = Path(git("rev-parse", "--git-path", "talaria-checks.json"))
    identity = [
        git("rev-parse", f"{commit}^{{tree}}"),
        {**scope, "native": []},
        sys.version,
        sys.executable,
        platform.platform(),
        workers(),
        subprocess.check_output(["node", "--version"]).decode().strip(),
        subprocess.check_output(["uv", "--version"]).decode().strip(),
        subprocess.check_output(["npm", "--version"]).decode().strip(),
        {
            k: v
            for k, v in env.items()
            if k.startswith(("PYTEST_", "TALARIA_", "PLAYWRIGHT_", "NODE_", "UV_"))
        },
    ]
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    records = {}
    with contextlib.suppress(OSError, ValueError, AttributeError):
        records = {
            k: v
            for k, v in json.loads(cache.read_text()).items()
            if isinstance(v, (int, float)) and 0 <= time.time() - v < 3600
        }
    cached = not fresh and key in records
    if cached:
        print(f"Reusing successful local checks for {commit[:10]} (under one hour old).")
        if not scope["native"]:
            return
    elif key in records:
        del records[key]
        cache.write_text(json.dumps(records))
    with tempfile.TemporaryDirectory(prefix="talaria-push-") as directory:
        root = Path(directory)
        export(commit, root)
        if not cached:
            run(
                "uv",
                "run",
                "--locked",
                "python",
                str(root / "contrib/check.py"),
                "check",
                "--skip-native",
                *(["--paths", *paths] if paths else []),
                cwd=root,
                env=env,
            )
            records[key] = time.time()
            records = dict(sorted(records.items(), key=lambda item: item[1])[-32:])
            try:
                cache.write_text(json.dumps(records))
            except OSError:
                print("Checks passed; the optional local result cache could not be saved.")
        # Never cache native results: Hermes can change without a Talaria commit.
        if scope["native"]:
            run(
                "uv",
                "run",
                "--locked",
                "python",
                "-m",
                "pytest",
                "-q",
                "-n",
                str(workers()),
                "--dist",
                "load",
                "--maxschedchunk=1",
                "--fail-on-skip",
                *scope["native"],
                "-m",
                "hermes",
                cwd=root,
                env=env,
            )


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
    native_mode = command.add_mutually_exclusive_group()
    native_mode.add_argument("--native", action="store_true")
    native_mode.add_argument("--skip-native", action="store_true", help=argparse.SUPPRESS)
    selection = command.add_mutually_exclusive_group()
    selection.add_argument("--changed", action="store_true", help="Test the changed areas locally")
    selection.add_argument("--revision", help="Check and cache an immutable committed revision")
    selection.add_argument("--paths", nargs="+", help=argparse.SUPPRESS)
    command.add_argument("--base", default="origin/main", help="Comparison base for focused checks")
    command.add_argument("--fresh", action="store_true", help="Ignore cached revision results")
    command = commands.add_parser("platform", help="Portable install/runtime checks for CI")
    command.add_argument("--full", action="store_true", help="Include all backend logic")
    command = commands.add_parser("prove")
    command.add_argument("--base", required=True)
    command.add_argument("tests", nargs="+")
    command = commands.add_parser("ci", help="Selected backend and Chromium checks for public PRs")
    command.add_argument("--base", default="HEAD^", help="Base commit of the PR merge checkout")
    command.add_argument("--plan", action="store_true", help="Print required CI setup")
    args = parser.parse_args()
    if args.command == "pre-commit":
        pre_commit()
    elif args.command == "pre-push":
        pre_push()
    elif args.command == "lint":
        lint()
    elif args.command == "platform":
        platform_check(full=args.full)
    elif args.command == "prove":
        prove(args.base, args.tests)
    elif args.command == "ci":
        ci_check(args.base, args.plan)
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
        check(
            full=args.command == "full",
            native=getattr(args, "native", False),
            paths=paths,
            skip_native=getattr(args, "skip_native", False),
        )


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
