"""Run the existing plugin transaction without exposing a private bootstrap runtime."""

import os
import selectors
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from .deployment import DeploymentError, stop
from .plugin_install import owned_worker

BOOT = (
    "import sys; sys.path.insert(0, sys.argv.pop(1)); "
    f"from {owned_worker.__module__} import {owned_worker.__name__}; {owned_worker.__name__}()"
)


def stage(directory, source, owner):
    package = directory / "talaria"
    native = package / "hermes_plugin"
    native.mkdir(parents=True)
    (package / "__init__.py").touch()
    (native / "__init__.py").touch()
    code = Path(__file__).parent
    for name in ("plugin_install.py", "maintenance.py", "hermes_plugin/release.py"):
        (package / name).write_bytes((code / name).read_bytes())
    bundle = directory / "bundle"
    bundle.mkdir()
    for path in source.iterdir():
        if path.is_file() and path.suffix in {".py", ".yaml"}:
            (bundle / path.name).write_bytes(path.read_bytes())
    for path in [directory, *directory.rglob("*")]:
        path.chmod(0o700 if path.is_dir() else 0o600)
        if os.geteuid() == 0:
            os.chown(path, owner.pw_uid, owner.pw_gid)
    return bundle


def supervise(process, channel, verify, *, timeout=1320):
    deadline = time.monotonic() + timeout
    channel.settimeout(2)
    with selectors.DefaultSelector() as selector, channel.makefile("rwb") as stream:
        selector.register(channel, selectors.EVENT_READ)
        while process.poll() is None:
            if time.monotonic() >= deadline:
                raise DeploymentError("Local plugin maintenance timed out.")
            if not selector.select(min(1, max(0, deadline - time.monotonic()))):
                continue
            message = stream.readline(9)
            if not message:
                break
            if message != b"restart\n" or verify is None:
                raise DeploymentError("Invalid plugin maintenance request.")
            try:
                verify()
            except Exception:
                # The worker still owns the installation lock and must restore its
                # backup before asking us to restart that previous version.
                response = b"failed\n"
            else:
                response = b"ok\n"
            stream.write(response)
            stream.flush()
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
    if process.returncode:
        raise DeploymentError(
            "Local plugin maintenance failed. Previous plugin files are retained for recovery; "
            "check the Hermes gateway status."
        )


def run(python, home, source, owner, identity, env, verify):
    with tempfile.TemporaryDirectory(prefix="talaria-plugin-") as temporary:
        directory = Path(temporary)
        bundle = stage(directory, source, owner)
        with tempfile.TemporaryFile() as output:
            run_staged(python, directory, home, bundle, identity, env, verify, output)


def run_staged(python, directory, home, bundle, identity, env, verify, output):
    parent, child = socket.socketpair()
    with parent, child:
        process = subprocess.Popen(
            [
                str(python),
                "-I",
                "-S",
                "-c",
                BOOT,
                str(directory),
                str(home),
                str(bundle),
                str(child.fileno()),
                "1" if verify else "0",
            ],
            pass_fds=(child.fileno(),),
            env=env,
            cwd=directory,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            umask=0o077,
            **identity,
        )
        child.close()
        try:
            supervise(process, parent, verify)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeploymentError("Local plugin maintenance connection interrupted.") from exc
        finally:
            stop(process)
