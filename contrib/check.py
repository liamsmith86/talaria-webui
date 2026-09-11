"""Local checks and regression evidence. No services or production credentials needed."""

import argparse
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
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


def lint():
    run("ruff", "check", ".", ".github/scripts")
    for path in Path("src/talaria/static").glob("*.js"):
        run("node", "--check", str(path))


def pytest(*args, env=None):
    run(sys.executable, "-m", "pytest", "-q", "-n", "2", "--fail-on-skip", *args, env=env)


def check(full=False, native=False):
    env = environment()
    if (full or native) and not env.get("HERMES_SOURCE"):
        raise SystemExit("Native checks need HERMES_SOURCE (see CONTRIBUTING.md).")
    if full:
        axe = Path(env.get("TALARIA_AXE_PATH", ""))
        digest = "c24f097bd2f451d4f933e8bc7d8d539f8672a2ebcb5cc9f9f3eec8ca9470a0c1"
        if not axe.is_file() or hashlib.sha256(axe.read_bytes()).hexdigest() != digest:
            raise SystemExit("Set TALARIA_AXE_PATH to the verified script in CONTRIBUTING.md.")
    lint()
    pytest("-m", "not browser and not hermes", env=env)
    for browser in ("chromium", "firefox", "webkit") if full else ("chromium", "webkit"):
        print(f"Browser checks: {browser}", flush=True)
        browser_env = {**env, "TALARIA_TEST_BROWSER": browser}
        if full:
            pytest("-m", "browser", env=browser_env)
        else:
            pytest(*(FOCUSED if browser == "chromium" else STREAMING), env=browser_env)
    if full or native:
        pytest("-m", "hermes", env=env)


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


def pre_push():
    checked = set()
    for line in sys.stdin.read().splitlines():
        _, revision, _, remote = line.split()
        if set(revision) == {"0"}:
            continue  # Deleting a ref has no revision to test.
        commit = git("rev-parse", f"{revision}^{{commit}}")
        if commit in checked:
            continue
        try:
            base = remote if set(remote) != {"0"} else git("merge-base", commit, "origin/main")
            paths = git("diff", "--name-only", "--no-renames", "-z", base, commit).split("\0")
        except subprocess.CalledProcessError:
            paths = git("ls-tree", "-r", "--name-only", "-z", commit).split("\0")
        paths = list(filter(None, paths))
        if docs_only(paths):
            print("Documentation-only push: no runtime checks needed.", flush=True)
            continue
        checked.add(commit)
        with tempfile.TemporaryDirectory(prefix="talaria-push-") as directory:
            root = Path(directory)
            export(commit, root)
            env = environment()
            if not env.get("HERMES_SOURCE"):
                source = subprocess.run(
                    ["git", "config", "--get", "talaria.hermesSource"],
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                if source:
                    env["HERMES_SOURCE"] = source
            native = any(
                "hermes" in p or "contract" in p
                or p in {"pyproject.toml", "uv.lock", "tests/conftest.py",
                         "tests/test_extended_access.py"}
                for p in paths
            )
            run(
                "uv",
                "run",
                "--locked",
                "python",
                str(root / "contrib/check.py"),
                "check",
                *(["--native"] if native else []),
                cwd=root,
                env=env,
            )


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
    else:
        check(full=args.command == "full", native=getattr(args, "native", False))


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
