"""Walk a new user's install: real Hermes from its own installer, then real Talaria from ours.

Everything happens in a throwaway home and through what each project publishes, so a change in how
Hermes lays itself out fails here first. Only the model is scripted, which needs no provider key.

    uv run --locked python contrib/install_walkthrough.py [--hermes-ref REF]
"""

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from e2e.scripted_model import ECHO, plan, scripted_model, terminal  # noqa: E402

STABLE_HERMES = "bf0203a794a07aac3ff764972ab9087baa4e4f4e"  # v0.22.5
OUTPUT = "walkthrough-output"


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Walkthrough:
    def __init__(self, sandbox, logs):
        self.sandbox = sandbox
        self.home = sandbox / "home"
        self.home.mkdir()
        self.logs = logs
        self.hermes_port = free_port()
        self.talaria_port = free_port()
        tools = Path(shutil.which("uv")).parent
        self.env = {
            "HOME": str(self.home),
            "PATH": os.pathsep.join(
                (str(self.home / ".local/bin"), str(tools), "/usr/bin", "/bin")
            ),
            "LANG": "C.UTF-8",
        }

    def run(self, name, *command, timeout=900):
        print(f"--- {name}", flush=True)
        log = self.logs / f"{name}.log"
        with log.open("w") as output:
            subprocess.run(
                command,
                env=self.env,
                cwd=self.sandbox,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=True,
            )
        return log.read_text()

    @contextmanager
    def service(self, name, *command):
        print(f"--- start {name}", flush=True)
        with (self.logs / f"{name}.log").open("w") as output:
            process = subprocess.Popen(
                command,
                env=self.env,
                cwd=self.sandbox,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            try:
                yield process
            finally:
                process.terminate()
                try:
                    process.wait(30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    def install_hermes(self, ref):
        script = self.sandbox / "hermes-install.sh"
        url = (
            f"https://raw.githubusercontent.com/NousResearch/hermes-agent/{ref}/scripts/install.sh"
        )
        script.write_bytes(
            httpx.get(url, timeout=60, follow_redirects=True).raise_for_status().content
        )
        source = ["--commit", ref] if len(ref) == 40 else ["--branch", ref]
        self.run(
            "hermes-install",
            "bash",
            str(script),
            *source,
            "--skip-setup",
            "--skip-browser",
            "--skip-computer-use",
        )

    def configure_hermes(self, model):
        settings = {
            "model.default": "scripted",
            "model.provider": "custom",
            "model.base_url": model,
            "model.api_key": "none",
            "model.context_length": "128000",
            "auxiliary.title_generation.enabled": "false",
            "platforms.api_server.port": str(self.hermes_port),
        }
        for key, value in settings.items():
            self.run("hermes-config", "hermes", "config", "set", key, value)

    def install_talaria(self):
        repository = self.sandbox / "repository"
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        subprocess.run(["git", "clone", "--quiet", str(ROOT), str(repository)], check=True)
        subprocess.run(
            ["git", "checkout", "--quiet", "-B", "walkthrough", commit], cwd=repository, check=True
        )
        output = self.run(
            "talaria-install",
            "bash",
            str(ROOT / "install.sh"),
            "--repository",
            str(repository),
            "--branch",
            "walkthrough",
            "--non-interactive",
            "--service",
            "none",
            "--enable-hermes-api",
            "--plugin",
            "--port",
            str(self.talaria_port),
        )
        for line in ("Start: talaria", "Uninstall: talaria uninstall", "--set-password"):
            assert line in output, f"Setup didn't print {line!r}"
        assert shutil.which("talaria", path=self.env["PATH"]), "talaria is not on PATH"

    def sign_in(self, client):
        password = (self.home / ".config/talaria/initial-password.txt").read_text().strip()
        client.headers["X-Talaria-Request"] = "1"
        client.post("/api/login", json={"password": password}).raise_for_status()
        client.headers["X-CSRF-Token"] = client.get("/api/bootstrap").json()["csrf"]

    def chat(self, client):
        session = client.post("/api/sessions", json={}).raise_for_status().json()
        sid = session.get("id") or session["session"]["id"]
        prompt = plan(terminal(f"echo {OUTPUT}"), ECHO)
        body = {"session_id": sid, "input": prompt}
        client.post("/api/runs", json=body).raise_for_status()
        return wait_for(
            "a reply that used a tool",
            lambda: any(
                row.get("role") == "assistant" and OUTPUT in str(row.get("content"))
                for row in client.get(f"/api/sessions/{sid}/messages").json().get("data", [])
            ),
            timeout=120,
        )

    def use_it(self):
        url = f"http://127.0.0.1:{self.talaria_port}"
        with (
            self.service("hermes-gateway", "hermes", "gateway", "run"),
            self.service("talaria", "talaria"),
            httpx.Client(base_url=url, timeout=30, trust_env=False) as client,
        ):
            wait_for("Talaria", lambda: client.get("/health").status_code == 200)
            self.sign_in(client)

            def connected():
                response = client.get("/api/capabilities")
                return response.status_code == 200 and response.json()

            capabilities = wait_for("Hermes through Talaria", connected, timeout=120)
            extensions = capabilities.get("talaria_extensions") or {}
            assert extensions.get("context_usage"), f"The plugin isn't active: {extensions}"
            self.chat(client)

    def walk(self, ref):
        self.install_hermes(ref)
        with scripted_model() as model:
            self.configure_hermes(model)
            self.install_talaria()
            self.use_it()
        hermes_home = str(self.home / ".hermes")
        self.run("talaria-plugin", "talaria", "hermes-plugin", "--home", hermes_home)
        self.run("talaria-update", "talaria", "update")
        self.run("talaria-uninstall", "talaria", "uninstall", "--yes")
        assert not (self.home / ".local/bin/talaria").is_symlink(), "uninstall left its command"


def wait_for(what, check, timeout=60):
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = check()
        except httpx.HTTPError:
            result = False
        if result:
            return result
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out waiting for {what}")
        time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hermes-ref", default="stable", help="stable, a branch or a full commit")
    parser.add_argument("--logs", type=Path, default=ROOT / "test-results/install-walkthrough")
    args = parser.parse_args()
    logs = args.logs.resolve()
    shutil.rmtree(logs, ignore_errors=True)
    logs.mkdir(parents=True)
    started = time.monotonic()
    # The installers keep long paths, and a short root keeps socket paths valid.
    with tempfile.TemporaryDirectory(prefix=f"tw-{secrets.token_hex(2)}-", dir="/tmp") as sandbox:
        ref = STABLE_HERMES if args.hermes_ref == "stable" else args.hermes_ref
        Walkthrough(Path(sandbox), logs).walk(ref)
    print(json.dumps({"passed": True, "seconds": round(time.monotonic() - started)}))


if __name__ == "__main__":
    main()
