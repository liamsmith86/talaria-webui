"""CLI-only wheel deployments from Git, with atomic activation and recovery.

Git and uv are build tools; neither is imported or invoked by the web server.
Releases are never edited in place. Configuration stays outside the installation.
"""

import argparse
import fcntl
import json
import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from .config import default_path, load
from .installation import managed_root, read_json, release_info

REPOSITORY = "https://github.com/liamsmith86/talaria-webui.git"
COMMIT = re.compile(r"[0-9a-f]{40,64}")
PROBE = """
import socket, sys
from pathlib import Path
import uvicorn
from talaria.app import create_app
from talaria.config import load
path = Path(sys.argv[1])
app = create_app(load(path), path)
sock = socket.socket()
sock.bind(('127.0.0.1', 0))
print(sock.getsockname()[1], flush=True)
uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False)).run(sockets=[sock])
"""


class DeploymentError(Exception):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def default_directory() -> Path:
    return (
        managed_root()
        or Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "talaria"
    )


def write_file(path: Path, text: str, mode=0o644):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".talaria-")
    try:
        with os.fdopen(fd, "w") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
            os.fchmod(file.fileno(), mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path: Path, data: dict):
    write_file(path, json.dumps(data, indent=2) + "\n")


def stop(process):
    """Also stop build-tool children when an update is interrupted."""
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def run(command, *, cwd=None, timeout=180) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "UV_NO_PROGRESS": "1"}
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen(
                [str(arg) for arg in command],
                cwd=cwd,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            raise DeploymentError(f"Cannot start {command[0]}: {exc}") from exc
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise DeploymentError(f"{command[0]} timed out; production was not changed.") from exc
        finally:
            stop(process)
        output.seek(max(0, output.tell() - 16_384))
        result = output.read().decode(errors="replace").strip()
        if process.returncode:
            raise DeploymentError(f"{command[0]} failed:\n{result}")
        return result


@contextmanager
def locked(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".update.lock").open("a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeploymentError("Another update is running. Try again when it finishes.") from exc
        yield


def select(root: Path, name: str, release: str | None):
    link = root / name
    if release is None:
        link.unlink(missing_ok=True)
        return
    # Journal paths can only name direct release directories inside this installation.
    if not re.fullmatch(r"releases/[0-9a-f]{7,64}", release):
        raise DeploymentError("Invalid release path; no activation was performed.")
    pending = root / f".{name}.next"
    pending.unlink(missing_ok=True)
    pending.symlink_to(release, target_is_directory=True)
    os.replace(pending, link)


def selected(root: Path, name: str) -> str | None:
    path = root / name
    if not path.is_symlink():
        if path.exists():
            raise DeploymentError(f"{path} must be a release symlink.")
        return None
    value = os.readlink(path)
    if not re.fullmatch(r"releases/[0-9a-f]{7,64}", value):
        raise DeploymentError(f"{path} points outside the release directory.")
    return value


def check_health(url: str, commit: str | None, *, timeout=15, assets=False):
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while True:
        try:
            with opener.open(url.rstrip("/") + "/health", timeout=2) as response:
                data = json.loads(response.read(4096))
            if data.get("status") != "ok" or (commit and data.get("commit") != commit):
                raise ValueError("Unexpected release or health response")
            if assets:
                for path in ("/", "/static/app.js", "/static/style.css", "/api/bootstrap"):
                    with opener.open(url.rstrip("/") + path, timeout=2) as response:
                        if response.status != 200 or not response.read(1):
                            raise ValueError(f"Missing packaged asset: {path}")
            return
        except (OSError, URLError, ValueError) as exc:
            if time.monotonic() >= deadline:
                raise DeploymentError(f"Release health check failed at {url}.") from exc
            time.sleep(0.2)


class Deployment:
    def __init__(self, root: Path, *, report=print):
        self.root = root.expanduser().resolve()
        self.report = report
        self.config = read_json(self.root / "deployment.json")
        if self.config.get("schema") != 1:
            raise DeploymentError(
                "No supported managed installation found. Run talaria install first."
            )
        self.state = read_json(self.root / "update.json")

    def record(self, **changes):
        self.state.update(changes)
        write_json(self.root / "update.json", self.state)

    def fetch(self) -> str:
        branch = self.config["branch"]
        repo = self.root / "repository.git"
        run(["git", "check-ref-format", f"refs/heads/{branch}"])
        if not repo.exists():
            run(["git", "init", "--bare", repo])
        run(
            [
                "git",
                "--git-dir",
                repo,
                "fetch",
                "--no-tags",
                "--depth=1",
                "--force",
                "--",
                self.config["repository"],
                f"refs/heads/{branch}",
            ]
        )
        commit = run(["git", "--git-dir", repo, "rev-parse", "--verify", "FETCH_HEAD^{commit}"])
        if not COMMIT.fullmatch(commit):
            raise DeploymentError("Git did not return an unambiguous commit.")
        return commit

    def probe(self, release: Path):
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                [str(release / "venv/bin/python"), "-c", PROBE, self.config["config"]],
                stdout=subprocess.PIPE,
                stderr=errors,
                start_new_session=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    if not selector.select(timeout=15):
                        raise DeploymentError("The staged application did not start.")
                    port = process.stdout.readline(16).strip()
                if not port.isdigit() or not 0 < int(port) < 65536:
                    raise DeploymentError(
                        "The staged application could not load its configuration."
                    )
                check_health(
                    f"http://127.0.0.1:{port.decode()}",
                    release.name,
                    assets=True,
                )
            finally:
                stop(process)
                process.stdout.close()

    def stage(self, commit: str) -> str:
        release = self.root / "releases" / commit
        if release.exists():
            if read_json(release / "release.json").get("commit") == commit:
                self.probe(release)
                return f"releases/{commit}"
            shutil.rmtree(release)
        uv = shutil.which("uv")
        if not uv:
            raise DeploymentError("Install uv to build updates: https://docs.astral.sh/uv/")
        release.mkdir(parents=True)
        try:
            with tempfile.TemporaryDirectory(prefix=".source-", dir=self.root) as temporary:
                source = Path(temporary) / "source"
                source.mkdir()
                archive = Path(temporary) / "source.tar"
                run(
                    [
                        "git",
                        "--git-dir",
                        self.root / "repository.git",
                        "archive",
                        "--format=tar",
                        f"--output={archive}",
                        commit,
                    ]
                )
                with tarfile.open(archive) as file:
                    file.extractall(source, filter="data")
                write_json(source / "src/talaria/_build.json", {"commit": commit})
                self.report("Building the wheel and installing locked runtime dependencies…")
                run([uv, "build", "--wheel", "--out-dir", release / "artifacts", source])
                requirements = release / "requirements.txt"
                run(
                    [
                        uv,
                        "export",
                        "--project",
                        source,
                        "--frozen",
                        "--no-dev",
                        "--no-emit-project",
                        "--output-file",
                        requirements,
                    ]
                )
                run([uv, "venv", "--python", sys._base_executable, release / "venv"])
                python = release / "venv/bin/python"
                run([uv, "pip", "sync", "--python", python, "--require-hashes", requirements])
                wheels = list((release / "artifacts").glob("talaria_webui-*.whl"))
                if len(wheels) != 1:
                    raise DeploymentError("Expected exactly one Talaria wheel.")
                run([uv, "pip", "install", "--python", python, "--no-deps", wheels[0]])
                self.report("Checking startup, configuration, and packaged assets…")
                self.probe(release)
                version = run([python, "-c", "from talaria import __version__; print(__version__)"])
                write_json(
                    release / "release.json",
                    {
                        "commit": commit,
                        "version": version,
                        "installed_at": now(),
                        "health_commit": True,
                    },
                )
            return f"releases/{commit}"
        except BaseException:
            shutil.rmtree(release)
            raise

    def restart(self):
        if not self.config.get("service"):
            return
        command = ["systemctl"]
        if self.config.get("scope") == "user":
            command += ["--user"]
        elif os.geteuid() != 0:
            command = ["sudo", "-n", *command]
        run([*command, "restart", self.config["service"]], timeout=45)

    def verify_running(self, release: str):
        if self.config.get("service"):
            info = read_json(self.root / release / "release.json")
            check_health(
                self.config["health_url"], info.get("commit") if info.get("health_commit") else None
            )

    def recover(self):
        journal = self.root / "activation.json"
        if not journal.exists():
            return
        data = read_json(journal)
        if "current" not in data or "previous" not in data:
            raise DeploymentError("Activation journal is unreadable; inspect it before updating.")
        self.report("Restoring the previous release after an interrupted activation…")
        select(self.root, "current", data["current"])
        select(self.root, "previous", data["previous"])
        if "manager" in data:
            select(self.root, "manager", data["manager"])
        if data["current"]:
            self.restart()
            self.verify_running(data["current"])
        journal.unlink()

    def activate(self, target: str, *, promote=False):
        old = selected(self.root, "current")
        journal = self.root / "activation.json"
        write_json(
            journal,
            {
                "current": old,
                "previous": selected(self.root, "previous"),
                "manager": selected(self.root, "manager"),
            },
        )
        try:
            select(self.root, "current", target)
            self.restart()
            self.verify_running(target)
            select(self.root, "previous", old)
            if promote:
                select(self.root, "manager", target)
            journal.unlink()
        except BaseException as exc:
            try:
                self.recover()
            except Exception as recovery:
                raise DeploymentError(
                    "Activation failed and service recovery needs attention. "
                    "The recovery journal has been retained. Run talaria status."
                ) from recovery
            raise DeploymentError("Activation failed; the previous release was restored.") from exc

    def cleanup(self):
        keep = {selected(self.root, name) for name in ("current", "previous", "manager")}
        for path in (self.root / "releases").iterdir():
            if (
                path.is_dir()
                and not path.is_symlink()
                and re.fullmatch(r"[0-9a-f]{7,64}", path.name)
            ):
                if f"releases/{path.name}" not in keep:
                    shutil.rmtree(path)

    def launcher(self):
        """Keep management commands usable after rolling back to a pre-updater release."""
        directory = self.root / "bin"
        directory.mkdir(exist_ok=True)
        root, config = shlex.quote(str(self.root)), shlex.quote(self.config["config"])
        write_file(
            directory / "talaria",
            (
                '#!/bin/sh\ncase "${1-}" in\n'
                f'  install|update|rollback|status) exec {root}/manager/venv/bin/talaria "$@" ;;\n'
                f'  *) exec {root}/current/venv/bin/talaria --config {config} "$@" ;;\n'
                "esac\n"
            ),
            0o755,
        )

    def update(self, *, check=False, expect=None):
        with locked(self.root):
            if check and (self.root / "activation.json").exists():
                raise DeploymentError(
                    "An activation is incomplete. Run talaria update to recover it."
                )
            self.recover()
            self.report(f"Checking {self.config['branch']}…")
            try:
                commit = self.fetch()
                current = release_info(self.root / "current")["commit"]
                available = commit != current
                self.record(checked_at=now(), latest_commit=commit, available=available, error=None)
                if expect and expect != commit:
                    raise DeploymentError(
                        "The branch has moved from the expected commit. Review its new head first."
                    )
                if not available:
                    self.report(f"Already up to date ({commit[:10]}).")
                    return
                if check:
                    self.report(
                        f"Update available: {commit[:10]}. Run talaria update to install it."
                    )
                    return
                target = self.stage(commit)
                self.report("Activating the checked release…")
                self.activate(target, promote=True)
                self.launcher()
                self.record(available=False, error=None)
            except Exception:
                self.record(
                    error="The last update attempt failed. Run talaria update on the server "
                    "for details."
                )
                raise
            self.cleanup()
            self.report(f"Installed {commit[:10]}. Previous release retained for talaria rollback.")
            if not self.config.get("service"):
                self.report("Restart your Talaria process to use the selected release.")

    def rollback(self):
        with locked(self.root):
            self.recover()
            target = selected(self.root, "previous")
            if not target or not (self.root / target / "release.json").is_file():
                raise DeploymentError("No previous release is available.")
            self.activate(target)
            commit = release_info(self.root / "current")["commit"]
            self.record(available=bool(self.state.get("latest_commit") != commit), error=None)
            self.report(f"Restored {commit[:10]}.")
            if not self.config.get("service"):
                self.report("Restart your Talaria process to use the selected release.")

    def status(self) -> dict:
        return {
            "current": release_info(self.root / "current"),
            "previous": release_info(self.root / "previous")
            if selected(self.root, "previous")
            else None,
            "branch": self.config["branch"],
            "service": self.config.get("service"),
            "scope": self.config.get("scope"),
            "update": self.state,
            "recovery_pending": (self.root / "activation.json").exists(),
        }


def main(argv):
    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description="Install and update isolated Talaria releases.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "update", "rollback", "status"):
        command = commands.add_parser(name)
        command.add_argument(
            "--directory",
            type=Path,
            default=default_directory(),
            help="Managed installation directory",
        )
        if name == "install":
            command.add_argument("--repository", default=REPOSITORY, help="Trusted Git repository")
            command.add_argument("--branch", default="main")
            command.add_argument("--config", type=Path, default=default_path())
            command.add_argument(
                "--service", help="Existing systemd unit to restart after activation"
            )
            command.add_argument("--scope", choices=("user", "system"), default="user")
        elif name == "update":
            command.add_argument(
                "--check", action="store_true", help="Check the remote branch without installing"
            )
            command.add_argument(
                "--expect", help="Only deploy if the remote head is this full commit SHA"
            )
        elif name == "status":
            command.add_argument(
                "--json", action="store_true", help="Print machine-readable installation status"
            )
    args = parser.parse_args(argv)
    try:
        root = args.directory.expanduser().resolve()
        if args.command == "install":
            with locked(root):
                if (root / "deployment.json").exists():
                    raise DeploymentError(
                        "This installation is already initialized. Use talaria update."
                    )
                if args.repository.startswith("-") or any(c in args.repository for c in "\n\r\x00"):
                    raise DeploymentError("Invalid repository address.")
                run(["git", "check-ref-format", f"refs/heads/{args.branch}"])
                if args.service and not re.fullmatch(r"[a-zA-Z0-9_.@-]+\.service", args.service):
                    raise DeploymentError(
                        "Use a complete systemd unit name, such as talaria.service."
                    )
                path = args.config.expanduser().resolve()
                settings = load(path)
                host = settings.host
                if host in {"0.0.0.0", "::"}:
                    host = "127.0.0.1" if host == "0.0.0.0" else "[::1]"
                elif ":" in host:
                    host = f"[{host}]"
                write_json(
                    root / "deployment.json",
                    {
                        "schema": 1,
                        "repository": args.repository,
                        "branch": args.branch,
                        "config": str(path),
                        "service": args.service,
                        "scope": args.scope,
                        "health_url": f"http://{host}:{settings.port}",
                    },
                )
            deployment = Deployment(root)
            deployment.update()
            print(f"Launcher: {root / 'bin/talaria'}")
        else:
            deployment = Deployment(root)
            if args.command == "update":
                if args.expect and not COMMIT.fullmatch(args.expect):
                    raise DeploymentError("--expect needs a full commit SHA.")
                deployment.update(check=args.check, expect=args.expect)
            elif args.command == "rollback":
                deployment.rollback()
            else:
                data = deployment.status()
                if args.json:
                    print(json.dumps(data, indent=2))
                else:
                    for key in ("current", "previous"):
                        item = data[key]
                        print(
                            f"{key.capitalize()}: {item['version']} · {item['commit'][:10]}"
                            if item
                            else f"{key.capitalize()}: none"
                        )
                    print(f"Branch: {data['branch']}")
                    if data["recovery_pending"]:
                        print(
                            "An activation was interrupted. The next update will restore "
                            "the previous release first."
                        )
                    if data["update"].get("error"):
                        print(data["update"]["error"])
    except (DeploymentError, OSError, KeyError, ValueError) as exc:
        parser.exit(1, f"Talaria: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Talaria: interrupted. The current release is preserved.\n")
