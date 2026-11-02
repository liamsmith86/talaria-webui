"""Report anything pinned behind its newest release that has cleared the cooldown.

Runs before a commit that changes dependencies, and weekly in CI, because Dependabot
only reacts afterwards and can't see build requirements or hand-kept tool versions.
Exits 1 when something is outdated and 2 when the check itself couldn't run.
"""

import json
import re
import subprocess
import sys
import tomllib
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COOLDOWN = timedelta(days=7)  # Matches exclude-newer in pyproject.toml and .npmrc.
# Majors we've chosen not to adopt yet, with the reason in dependabot.yml.
HELD = {"preact": "11."}
STABLE = re.compile(r"\d+(\.\d+)+")


def command(*arguments, cwd=ROOT, check=True):
    return subprocess.run(arguments, cwd=cwd, check=check, capture_output=True, text=True)


def fetch(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def key(version):
    return [int(part) for part in version.split(".")]


def requirement(text):
    name, _, version = text.partition("==")
    return name, version


def pinned_uv():
    return re.search(r"astral-sh/uv:([\d.]+)@", (ROOT / "Dockerfile").read_text())[1]


def python_packages():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    groups = config["dependency-groups"].values()
    pins = dict(
        requirement(item)
        for item in [
            *config["build-system"]["requires"],
            *config["project"]["dependencies"],
            *(entry for group in groups for entry in group if isinstance(entry, str)),
        ]
    )
    pins["uv"] = pinned_uv()
    # Resolving bare names shows what the pins hide, and honours the cooldown.
    resolved = subprocess.run(
        [
            *("uv", "pip", "compile", "-", "--no-header", "--no-annotate", "--quiet"),
            *("--exclude-newer", f"{COOLDOWN.days} days", "--python-version", "3.12"),
        ],
        cwd=ROOT,
        input="\n".join(pins),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    newest = dict(requirement(line) for line in resolved.splitlines())
    found = [(name, pin, newest[name]) for name, pin in pins.items() if newest[name] != pin]
    # Pins stop this upgrading direct dependencies, so it reports only indirect ones.
    locked = command("uv", "lock", "--upgrade", "--dry-run").stderr
    found += re.findall(r"^Update (\S+) v(\S+) -> v(\S+)$", locked, re.M)
    return found


def npm_packages():
    # Without an install, npm outdated silently skips devDependencies.
    if not (ROOT / "node_modules").exists():
        raise OSError("node_modules is missing; run npm ci")
    found = []
    for directory in (ROOT, ROOT / "contrib/vendor"):
        report = command("npm", "outdated", "--json", cwd=directory, check=False).stdout
        for name, entry in json.loads(report or "{}").items():
            held = entry["latest"].startswith(HELD.get(name, "-"))
            if entry["wanted"] != entry["latest"] and not held:
                found.append((name, entry["wanted"], entry["latest"]))
    return found


def workflow_pins():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    cutoff = datetime.now(UTC) - COOLDOWN
    found = []
    for name in ("npm", "axe-core"):
        pin = re.search(rf"\b{name}@([\d.]+)", workflow)[1]
        times = fetch(f"https://registry.npmjs.org/{name}")["time"]
        cleared = [
            version
            for version, published in times.items()
            if STABLE.fullmatch(version) and datetime.fromisoformat(published) <= cutoff
        ]
        found.append((name, pin, max(cleared, key=key)))
    pin = re.search(r"node-version: '([\d.]+)'", workflow)[1]
    major = pin.split(".")[0]
    releases = [
        release["version"].removeprefix("v")
        for release in fetch("https://nodejs.org/dist/index.json")
        if release["version"].startswith(f"v{major}.")
        and datetime.fromisoformat(release["date"]).replace(tzinfo=UTC) <= cutoff
    ]
    found.append(("node", pin, max(releases, key=key)))
    return [item for item in found if item[1] != item[2]]


def python_runtimes():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    text += (ROOT / "Dockerfile").read_text()
    pins = set(re.findall(r"""python(?:-version)?"?:\s*['"](\d+\.\d+\.\d+)['"]""", text))
    pins |= set(re.findall(r"python:(\d+\.\d+\.\d+)-", text))
    # CI installs Python with the pinned uv, which only knows the releases it embeds.
    available = json.loads(
        command(
            *("uv", "tool", "run", f"uv@{pinned_uv()}", "python", "list"),
            *("--all-versions", "--only-downloads", "--output-format", "json"),
        ).stdout
    )
    versions = {
        entry["version"]
        for entry in available
        if entry["implementation"] == "cpython" and STABLE.fullmatch(entry["version"])
    }
    found = []
    for pin in sorted(pins, key=key):
        minor = pin.rsplit(".", 1)[0] + "."
        newest = max((v for v in versions if v.startswith(minor)), key=key)
        if key(newest) > key(pin):
            found.append(("python", pin, newest))
    return found


def main():
    try:
        found = python_packages() + npm_packages() + workflow_pins() + python_runtimes()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"Could not check for outdated dependencies: {error}", file=sys.stderr)
        return 2
    for name, current, newest in sorted(set(found)):
        print(f"{name}: {current} is pinned, {newest} is available", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
