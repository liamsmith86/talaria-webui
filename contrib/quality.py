"""Shared static gates for local development, staged snapshots, and CI."""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

from pre_commit_hooks.check_json import main as check_json
from pre_commit_hooks.check_merge_conflict import CONFLICT_PATTERNS
from pre_commit_hooks.check_toml import main as check_toml
from pre_commit_hooks.check_yaml import main as check_yaml
from pre_commit_hooks.detect_private_key import main as check_keys

EXCLUDED = {
    ".git",
    ".venv",
    ".local",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    "test-results",
    "htmlcov",
}


def files():
    if Path(".git").exists():
        names = (
            subprocess.check_output(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
            )
            .decode()
            .split("\0")
        )
        return sorted({Path(name) for name in names if name and Path(name).is_file()})
    # git archive snapshots have no index; prune dependency/cache directories.
    result = []
    for root, directories, names in os.walk("."):
        directories[:] = [name for name in directories if name not in EXCLUDED]
        result.extend(Path(root) / name for name in names)
    return sorted(result)


def run(*args, **kwargs):
    print("+", " ".join(map(str, args)), flush=True)
    subprocess.run(args, check=True, **kwargs)


def js_identity(root=Path(".")):
    digest = hashlib.sha256()
    for name in ("package.json", "package-lock.json", ".npmrc"):
        digest.update((root / name).read_bytes())
    digest.update(subprocess.check_output(["node", "--version"]))
    digest.update(subprocess.check_output(["npm", "--version"]))
    return digest.hexdigest()


def ensure_javascript():
    stamp = Path("node_modules/.talaria-lock")
    identity = js_identity()
    if not stamp.is_file() or stamp.read_text() != identity:
        run("npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund")
        stamp.write_text(identity)


def reuse_javascript(destination):
    """Reuse installed dev tools only when snapshot and installed inputs match."""
    stamp = Path("node_modules/.talaria-lock")
    if (
        (destination / "package-lock.json").exists()
        and stamp.is_file()
        and stamp.read_text() == js_identity() == js_identity(destination)
    ):
        (destination / "node_modules").symlink_to(Path("node_modules").resolve())


def hygiene(paths):
    result = 0
    names = [str(path) for path in paths]
    for path in paths:
        if path.stat().st_size > 1024 * 1024:
            print(f"{path}: exceeds the 1 MiB repository file limit")
            result = 1
    if result:
        raise SystemExit(result)
    for path in paths:
        with path.open("rb") as stream:
            for number, line in enumerate(stream, 1):
                if line.startswith(tuple(CONFLICT_PATTERNS)):
                    print(f"{path}:{number}: unresolved merge conflict")
                    result = 1
    checks = (
        (check_yaml, [str(p) for p in paths if p.suffix in {".yaml", ".yml"}]),
        (check_toml, [str(p) for p in paths if p.suffix == ".toml"]),
        (check_json, [str(p) for p in paths if p.suffix == ".json"]),
        (check_keys, names),
    )
    for check, arguments in checks:
        result |= check(arguments)
    if result:
        raise SystemExit(result)


def main():
    paths = files()
    hygiene(paths)
    run("ruff", "check", ".", ".github/scripts")
    run("ruff", "format", "--check", ".", ".github/scripts")
    run("vulture")
    ensure_javascript()
    run("node", "node_modules/eslint/bin/eslint.js", ".", "--max-warnings", "0")
    run("node", "contrib/i18n.mjs", env={**os.environ, "TALARIA_I18N_PYTHON": sys.executable})
    shell = [str(p) for p in paths if p.suffix == ".sh" or p.parent == Path(".githooks")]
    if shell:
        run("shellcheck", *shell)
    workflows = [str(p) for p in paths if p.parent == Path(".github/workflows")]
    if workflows:
        run("actionlint", "-oneline", *workflows)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
