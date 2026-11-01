"""Find the Python a Hermes installation really runs.

Current Hermes keeps its Python and dependencies outside the checkout and starts them through a
published launcher. A `venv` beside that launcher is left over from before Hermes migrated.
"""

import json
import shlex
import subprocess
from pathlib import Path

from .maintenance import DeploymentError

LAUNCHER = Path(".hermes/bin/hermes")


def is_launcher(path):
    return Path(path).parts[-len(LAUNCHER.parts) :] == LAUNCHER.parts


def launcher_behind(script):
    """Return the published launcher a command runs, directly or through its PATH wrapper."""
    if is_launcher(script):
        return script
    try:
        lines = Path(script).read_text().splitlines()
        wrapped = next(line for line in lines if line.startswith("exec "))
        target = Path(shlex.split(wrapped.removeprefix("exec "))[0])
    except (OSError, UnicodeError, ValueError, StopIteration, IndexError):
        return None
    return target if is_launcher(target) else None


def hermes_runtime(python, *, env, cwd, identity):
    """Return the interpreter to start, and the Hermes root it needs on its path if any."""
    if not is_launcher(python):
        return python, None
    try:
        result = subprocess.run(
            [str(python), "--print-runtime-command"],
            text=True,
            capture_output=True,
            cwd=cwd,
            env=env,
            timeout=45,
            **identity,
        )
        interpreter = json.loads(result.stdout.strip().rsplit("\n", 1)[-1])[0]
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError, KeyError) as exc:
        raise DeploymentError(
            "Cannot read Hermes's launcher. Supply --hermes-python or use --skip-hermes."
        ) from exc
    return Path(interpreter), Path(python).parents[2]
