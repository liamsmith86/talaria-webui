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
FOCUSED = [
    *STREAMING,
    "tests/test_frontend_state_qa.py",
    "tests/test_message_submission.py",
    "tests/test_conversation_features.py",
    "tests/test_settings.py",
]
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
BROWSERS = ("chromium", "firefox", "webkit")
# Where CONTRIBUTING.md puts the verified axe-core script.
AXE = ".local/axe.min.js"
# Passing checks of the same code count this long, for environment drift that
# the identity cannot see, such as reinstalled browsers.
EVIDENCE_SECONDS = 3600
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


def selection(scope, full=False):
    """The tests a check runs after linting: {stage: files}, where [] is every test."""
    tests = {}
    if scope["backend"] is not None:
        tests["backend"] = list(scope["backend"])
    if scope["browsers"]:
        if full:
            tests.update({browser: [] for browser in BROWSERS})
        else:
            translated = [file for file in scope["extra_browser"] if file == I18N_BROWSER]
            tests["chromium"] = list(dict.fromkeys(FOCUSED + scope["extra_browser"]))
            tests["webkit"] = list(dict.fromkeys(STREAMING + translated))
    return tests


def covered(passed, needed):
    """Whether passing runs of the same code together ran every test needed."""
    if not passed:
        return False
    for stage, files in needed.items():
        ran = [tests[stage] for tests in passed if stage in tests]
        if [] in ran:
            continue
        if not files or not set(files) <= {file for run in ran for file in run}:
            return False
    return True


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
            raise SystemExit(f"Save the verified axe-core script as {AXE} or set TALARIA_AXE_PATH.")
    lint(paths=None if full else paths)
    tests = selection(scope, full)
    if "backend" in tests:
        pytest(*tests["backend"], "-m", "not browser and not hermes", env=env)
    for browser in BROWSERS:
        if browser in tests:
            print(f"Browser checks: {browser}", flush=True)
            pytest(
                *(file for file in tests[browser] if Path(file).is_file()),
                "-m",
                "browser",
                env={**env, "TALARIA_TEST_BROWSER": browser},
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


def reuse_javascript(root):
    """Link the installed JavaScript tools into an export when its lockfile matches."""
    if __package__:
        from .quality import reuse_javascript
    else:
        from quality import reuse_javascript
    reuse_javascript(root)


def pre_commit():
    # Export the index, not HEAD or the working tree: partial staging must not
    # hide violations, and deleting the last reference must reach Vulture.
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
    """The environment, with prerequisites found where CONTRIBUTING.md puts them."""
    env = environment()
    if not env.get("HERMES_SOURCE"):
        source = subprocess.run(
            ["git", "config", "--get", "talaria.hermesSource"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if source:
            env["HERMES_SOURCE"] = source
    if not env.get("TALARIA_AXE_PATH") and Path(AXE).is_file():
        env["TALARIA_AXE_PATH"] = str(Path(AXE).resolve())
    return env


def identity(commit, env):
    """What a check's result depends on besides the tests it ran."""
    return [
        git("rev-parse", f"{commit}^{{tree}}"),
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
            # uv's own bookkeeping, set by `uv run`, never changes a result.
            and k != "UV_RUN_RECURSION_DEPTH"
        },
    ]


def read_evidence(cache):
    """Recent passing checks: {identity digest: [{"time", "tests"}]}."""
    now = time.time()
    with contextlib.suppress(OSError, ValueError, TypeError, KeyError, AttributeError):
        evidence = {}
        for key, runs in json.loads(cache.read_text()).items():
            recent = [run for run in runs if 0 <= now - run["time"] < EVIDENCE_SECONDS]
            if recent and all(isinstance(run["tests"], dict) for run in recent):
                evidence[key] = recent
        return evidence
    return {}


def write_evidence(cache, evidence):
    newest = sorted(evidence.items(), key=lambda item: max(run["time"] for run in item[1]))
    try:
        cache.write_text(json.dumps(dict(newest[-32:])))
    except OSError:
        print("The optional local check cache could not be saved.")


def verify_revision(commit, paths, *, fresh=False, native=False, full=False):
    """Check an exported revision, reusing recent passing checks of the same code.

    A passing check counts for its tree and environment, whatever the commit or
    comparison base, and covers any later check that needs only tests it ran.
    Native Hermes checks always run: Hermes can change without a Talaria commit.
    """
    env = check_environment()
    scope = plan(None if full else paths)
    if native or full:
        scope["native"] = ["tests"]
    if scope["native"] and not env.get("HERMES_SOURCE"):
        raise SystemExit("Native checks need HERMES_SOURCE (see CONTRIBUTING.md).")
    needed = selection(scope, full)
    cache = Path(git("rev-parse", "--git-path", "talaria-checks.json"))
    key = hashlib.sha256(json.dumps(identity(commit, env), sort_keys=True).encode()).hexdigest()
    evidence = read_evidence(cache)
    if fresh and evidence.pop(key, None):
        write_evidence(cache, evidence)
    cached = covered([run["tests"] for run in evidence.get(key, [])], needed)
    if cached:
        print(f"Reusing successful local checks for {commit[:10]} (under one hour old).")
        if not scope["native"]:
            return
    with tempfile.TemporaryDirectory(prefix="talaria-push-") as directory:
        root = Path(directory)
        export(commit, root)
        if not cached:
            reuse_javascript(root)
            run(
                "uv",
                "run",
                "--locked",
                "python",
                str(root / "contrib/check.py"),
                "full" if full else "check",
                "--skip-native",
                *(["--paths", *paths] if paths and not full else []),
                cwd=root,
                env=env,
            )
            evidence.setdefault(key, []).append({"time": time.time(), "tests": needed})
            write_evidence(cache, evidence)
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
        planned = (commit, json.dumps(plan(paths), sort_keys=True))
        if planned not in checked:
            verify_revision(commit, paths)
            checked.add(planned)


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
    for name in ("pre-commit", "pre-push", "lint"):
        commands.add_parser(name)
    for name in ("check", "full"):
        command = commands.add_parser(name)
        native_mode = command.add_mutually_exclusive_group()
        native_mode.add_argument("--skip-native", action="store_true", help=argparse.SUPPRESS)
        source = command.add_mutually_exclusive_group()
        source.add_argument("--revision", help="Check a commit's export; pushes reuse the result")
        command.add_argument("--fresh", action="store_true", help="Ignore earlier passing checks")
        if name == "check":
            native_mode.add_argument("--native", action="store_true")
            source.add_argument("--changed", action="store_true", help="Test the changed areas")
            source.add_argument("--paths", nargs="+", help=argparse.SUPPRESS)
            command.add_argument("--base", default="origin/main", help="Base for focused checks")
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
    else:
        full = args.command == "full"
        native = getattr(args, "native", False)
        if args.revision:
            revision = git("rev-parse", f"{args.revision}^{{commit}}")
            paths = None
            if not full:
                paths = changed_paths(git("merge-base", args.base, revision), revision)
            if full or native or (paths and not docs_only(paths)):
                verify_revision(revision, paths, fresh=args.fresh, native=native, full=full)
            else:
                print("No runtime changes to check.")
            return
        paths = getattr(args, "paths", None)
        if getattr(args, "changed", False):
            paths = changed_paths(git("merge-base", args.base, "HEAD"))
        # The same prerequisites as checks of a revision.
        os.environ.update(
            (key, value)
            for key, value in check_environment().items()
            if key in {"HERMES_SOURCE", "TALARIA_AXE_PATH"}
        )
        check(full=full, native=native, paths=paths, skip_native=args.skip_native)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
