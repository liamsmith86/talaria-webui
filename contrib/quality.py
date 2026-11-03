"""Shared static gates for local development, staged snapshots, and CI."""

import hashlib
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
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


def gate(*args, **kwargs):
    """Run a check, returning its command and combined output for printing in order."""
    command = "+ " + " ".join(map(str, args))
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
    return command + "\n" + result.stdout.decode(errors="replace"), result.returncode, args


def build_identity():
    """Everything the production build's check depends on, including its committed output."""
    digest = hashlib.sha256(js_identity().encode())
    digest.update(Path("contrib/build.mjs").read_bytes())
    for path in sorted(p for p in Path("src/talaria/static").rglob("*") if p.is_file()):
        digest.update(str(path).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def check_build():
    """Check the production build, unless these exact bytes have already passed."""
    stamp = Path("node_modules/.talaria-build")
    identity = build_identity()
    if stamp.is_file() and stamp.read_text() == identity:
        return "+ node contrib/build.mjs --check (unchanged since it last passed)\n", 0, ()
    result = gate("node", "contrib/build.mjs", "--check")
    if not result[1]:
        stamp.write_text(identity)
    return result


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
        (check_json, [str(p) for p in paths if p.suffix in {".json", ".webmanifest"}]),
        (check_keys, names),
    )
    for check, arguments in checks:
        result |= check(arguments)
    if result:
        raise SystemExit(result)


def main():
    paths = files()
    hygiene(paths)
    ensure_javascript()
    node = {**os.environ, "TALARIA_I18N_PYTHON": sys.executable}
    shell = [str(p) for p in paths if p.suffix == ".sh" or p.parent == Path(".githooks")]
    workflows = [str(p) for p in paths if p.parent == Path(".github/workflows")]
    gates = [
        lambda: gate("ruff", "check", ".", ".github/scripts"),
        lambda: gate("ruff", "format", "--check", ".", ".github/scripts"),
        lambda: gate("vulture"),
        lambda: gate("node", "node_modules/eslint/bin/eslint.js", ".", "--max-warnings", "0"),
        lambda: gate("node", "contrib/i18n.mjs", env=node),
        lambda: gate(sys.executable, "contrib/vendor.py", "--check"),
        check_build,
    ]
    if shell:
        gates.append(lambda: gate("shellcheck", *shell))
    if workflows:
        gates.append(lambda: gate("actionlint", "-oneline", *workflows))
    # The gates only read the tree, and each one's output stays together.
    with ThreadPoolExecutor(len(gates)) as pool:
        results = list(pool.map(lambda check: check(), gates))
    for output, _, _ in results:
        print(output, end="", flush=True)
    for _, code, args in results:
        if code:
            raise subprocess.CalledProcessError(code, args)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
