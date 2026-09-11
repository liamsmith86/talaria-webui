"""One managed launcher: keep HTTP unprivileged and replace releases outside its lifetime."""

import argparse
import fcntl
import json
import os
import pwd
import selectors
import signal
import socket
import subprocess
import time
from contextlib import suppress
from pathlib import Path

from .control import JOB, SOCKET, ControlError, receive, send, validate
from .deployment import Deployment, DeploymentError, now, select, selected, write_json
from .installation import read_json
from .setup_services import check_system_directory


def clean_environment():
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "UV_PROJECT", "VIRTUAL_ENV", "TALARIA_ADOPT"))
    }
    env.setdefault("HOME", pwd.getpwuid(os.geteuid()).pw_dir)
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
    return env


def web_identity(root, config):
    if os.geteuid() != 0:
        return {}, clean_environment()
    # Running the HTTP process as root would defeat the launcher's boundary.
    account = pwd.getpwuid(Path(config).stat().st_uid)
    if account.pw_uid == 0 or Path(account.pw_shell).name not in {"nologin", "false"}:
        raise DeploymentError("A dedicated talaria-webui service account is required.")
    check_system_directory(root)
    env = {key: os.environ[key] for key in ("LANG", "TZ", "SSL_CERT_FILE") if key in os.environ}
    env.update(
        PATH=os.defpath,
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUNBUFFERED="1",
        HOME=account.pw_dir,
        USER=account.pw_name,
        LOGNAME=account.pw_name,
    )
    return {"user": account.pw_uid, "group": account.pw_gid, "extra_groups": []}, env


class WebProcess:
    def __init__(self, root, config):
        self.root, self.config = root, config
        self.identity, self.env = web_identity(root, config)
        self.process = None
        self.pid = int(os.environ.pop("TALARIA_ADOPT_PID", "0"))
        # An exec keeps the parent PID and its children. Reject arbitrary PIDs.
        if self.pid:
            self.alive()

    def alive(self):
        if not self.pid:
            return False
        pid, status = os.waitpid(self.pid, os.WNOHANG)
        if pid:
            if self.process:
                self.process.returncode = os.waitstatus_to_exitcode(status)
            self.pid = 0
        return bool(self.pid)

    def start(self):
        self.process = subprocess.Popen(
            [str(self.root / "current/venv/bin/talaria"), "--config", self.config],
            env=self.env,
            cwd=self.root,
            start_new_session=True,
            **self.identity,
        )
        self.pid = self.process.pid

    def stop(self):
        if not self.alive():
            return
        os.killpg(self.pid, signal.SIGTERM)
        deadline = time.monotonic() + 12
        while self.alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.alive():
            os.killpg(self.pid, signal.SIGKILL)
            _, status = os.waitpid(self.pid, 0)
            if self.process:
                self.process.returncode = os.waitstatus_to_exitcode(status)
            self.pid = 0

    def restart(self):
        self.stop()
        self.start()


class Launcher:
    def __init__(self, root):
        self.root = root
        self.deployment = Deployment(root)
        self.web = WebProcess(root, self.deployment.config["config"])
        self.worker = None
        self.worker_pipe = None
        self.stopping = False
        self.selector = selectors.DefaultSelector()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    def open(self):
        # The lifetime lock prevents unlinking another live launcher's socket.
        path = self.root / SOCKET
        path.unlink(missing_ok=True)
        self.server.bind(str(path))
        if self.web.identity:
            os.chown(path, 0, self.web.identity["group"])
        path.chmod(0o660 if self.web.identity else 0o600)
        self.server.listen(8)
        self.selector.register(self.server, selectors.EVENT_READ, self.accept)
        select(self.root, "supervisor", selected(self.root, "manager"))
        if (self.root / "activation.json").exists():
            recovery = Deployment(self.root, restart=self.web.restart)
            recovery.recover()
        if not self.web.alive():
            self.web.start()
        state = read_json(self.root / JOB)
        if state.get("status") in {"running", "finishing"}:
            state.update(status="failed", phase="failed", error="Update interrupted. Try again.")
            write_json(self.root / JOB, state)

    def begin(self, operation):
        if self.stopping:
            raise ControlError("Talaria is stopping.")
        operation = validate(operation)
        state = read_json(self.root / JOB)
        if state.get("id") == operation["id"]:
            if any(state.get(key) != operation.get(key) for key in ("action", "expect")):
                raise ControlError("Update identifier was already used for another operation.")
            return state
        if self.worker is not None:
            if operation["action"] == state.get("action") == "check":
                return state
            raise ControlError("An update operation is already running.")
        state = {**operation, "status": "running", "phase": "checking", "started_at": now()}
        write_json(self.root / JOB, state)
        parent, child = socket.socketpair()
        try:
            self.worker = subprocess.Popen(
                [
                    str(self.root / "manager/venv/bin/python"),
                    "-m",
                    "talaria.supervisor_worker",
                    str(self.root),
                    str(child.fileno()),
                    json.dumps(operation),
                ],
                pass_fds=(child.fileno(),),
                env={**clean_environment(), "UV_CACHE_DIR": str(self.root / ".build-cache")},
                start_new_session=True,
            )
        except BaseException:
            parent.close()
            state.update(status="failed", error="Could not start the update operation.")
            write_json(self.root / JOB, state)
            raise
        finally:
            child.close()
        self.worker_pipe = parent
        self.selector.register(parent, selectors.EVENT_READ, self.restart_request)
        return state

    def accept(self):
        with self.server.accept()[0] as connection:
            connection.settimeout(2)
            with connection.makefile("rwb") as stream:
                try:
                    result = self.begin(receive(stream))
                except (ControlError, ValueError, OSError):
                    result = {
                        "error": "Update request unavailable or another operation is running."
                    }
                with suppress(OSError):
                    send(stream, result)

    def restart_request(self):
        # This socket is inherited only by the deployment worker, never HTTP.
        try:
            self.worker_pipe.settimeout(2)
            with self.worker_pipe.makefile("rb") as stream:
                data = receive(stream)
            if data.get("restart") is not True or self.stopping:
                raise ControlError("Invalid restart request.")
            self.web.restart()
            self.worker_pipe.sendall(b'{"ok":true}\n')
        except (OSError, ValueError, ControlError):
            with suppress(KeyError):
                self.selector.unregister(self.worker_pipe)
            with suppress(OSError):
                self.worker_pipe.sendall(b'{"ok":false}\n')

    def reap_worker(self):
        if self.worker is None or self.worker.poll() is None:
            return
        with suppress(KeyError):
            self.selector.unregister(self.worker_pipe)
        self.worker_pipe.close()
        succeeded = self.worker.returncode == 0
        self.worker = self.worker_pipe = None
        state = read_json(self.root / JOB)
        if state.get("status") == "finishing":
            state["status"] = "completed" if succeeded else "failed"
        else:
            state.update(status="failed", phase="failed", error="Update interrupted. Try again.")
        write_json(self.root / JOB, state)
        if not self.stopping and selected(self.root, "manager") != selected(
            self.root, "supervisor"
        ):
            # Upgrade the launcher too, retaining the healthy HTTP child across exec.
            env = {**clean_environment(), "TALARIA_ADOPT_PID": str(self.web.pid)}
            python = str(self.root / "manager/venv/bin/python")
            os.execve(
                python, [python, "-m", "talaria.supervisor", "--directory", str(self.root)], env
            )

    def interrupt(self, _signum, _frame):
        self.stopping = True
        if self.worker and self.worker.poll() is None:
            self.worker.send_signal(signal.SIGTERM)

    def run(self):
        signal.signal(signal.SIGTERM, self.interrupt)
        signal.signal(signal.SIGINT, self.interrupt)
        try:
            self.open()
            while not self.stopping or self.worker:
                for key, _ in self.selector.select(0.25):
                    key.data()
                self.reap_worker()
                if not self.stopping and not self.web.alive():
                    time.sleep(1)
                    self.web.start()
        finally:
            self.web.stop()
            self.server.close()
            self.selector.close()
            (self.root / SOCKET).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.directory.expanduser().resolve()
    with (root / ".launcher.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            Launcher(root).run()
        except (OSError, ValueError, DeploymentError) as exc:
            parser.exit(1, f"Talaria launcher: {exc}\n")


if __name__ == "__main__":
    main()
