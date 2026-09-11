"""Execute the documented verifier with local downloads; never run an installation."""

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("hasher", ["sha256sum", "shasum"])
@pytest.mark.parametrize("failure", [None, "tampered", "download"])
def test_readme_checks_before_execution_and_cleans_up(tmp_path, hasher, failure):
    root = Path(__file__).resolve().parents[1]
    command = next(
        line
        for line in (root / "README.md").read_text().splitlines()
        if line.startswith("bash -c 'set -eu;")
    )
    source = (root / "install.sh").read_bytes()
    assert hashlib.sha256(source).hexdigest() in command
    assert re.search(r"/talaria-webui/[0-9a-f]{40}/install.sh", command)
    payload = tmp_path / "download"
    payload.write_bytes(source + (b"\nprintf hacked\n" if failure == "tampered" else b""))
    binaries, temporary = tmp_path / "bin", tmp_path / "temporary files"
    binaries.mkdir()
    temporary.mkdir()
    for name in (hasher, "mktemp", "rm", "cat"):
        tool = shutil.which(name)
        assert tool, f"Missing test prerequisite: {name}"
        (binaries / name).symlink_to(tool)
    (binaries / "curl").write_text(
        f"#!{sys.executable}\nimport os,sys,shutil\n"
        "shutil.copyfile(os.environ['TEST_PAYLOAD'],sys.argv[sys.argv.index('-o')+1])\n"
        f"sys.exit({22 if failure == 'download' else 0})\n"
    )
    (binaries / "bash").write_text(
        '#!/bin/sh\nprintf executed > "$TEST_EXECUTED"\nexec /bin/bash "$@" --help\n'
    )
    for name in ("curl", "bash"):
        (binaries / name).chmod(0o700)
    executed = tmp_path / "executed"
    args = shlex.split(command)
    args[0] = "/bin/bash"
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=10,
        env={
            **os.environ,
            "PATH": str(binaries),
            "TMPDIR": str(temporary),
            "TEST_PAYLOAD": str(payload),
            "TEST_EXECUTED": str(executed),
        },
    )
    assert (result.returncode == 0) == (failure is None), result.stderr
    assert executed.exists() == (failure is None)
    if failure is None:
        assert "Usage: bash install.sh" in result.stdout
    if failure == "tampered":
        assert "checksum mismatch" in result.stderr
    assert not list(temporary.iterdir())
