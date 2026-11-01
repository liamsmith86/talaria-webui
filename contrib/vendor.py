"""Copy the browser libraries in contrib/vendor/package-lock.json into static/vendor.

Dependabot proposes new versions in that lockfile. Run this script on its pull request to
bring in the files, and `--check` (part of lint) to confirm that both sides agree.
"""

import argparse
import base64
import hashlib
import io
import json
import re
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "contrib/vendor/package-lock.json"
VENDOR = ROOT / "src/talaria/static/vendor"
MANIFEST = VENDOR / "manifest.json"
# Where each file comes from in its package. Prism is absent: its file is a custom bundle.
SOURCES = {
    "preact": {"preact.js": "dist/preact.mjs", "hooks.js": "hooks/dist/hooks.mjs"},
    "htm": {"htm.js": "dist/htm.mjs"},
    "marked": {"marked.js": "lib/marked.esm.js"},
    "dompurify": {"purify.js": "dist/purify.es.mjs"},
    "@fontsource-variable/inter": {"inter.woff2": "files/inter-latin-wght-normal.woff2"},
}


def locked():
    packages = json.loads(LOCK.read_text())["packages"]
    return {name: packages[f"node_modules/{name}"] for name in packages[""]["dependencies"]}


def problems(manifest, lock):
    found = []
    for name in sorted(manifest.keys() | lock.keys()):
        if name not in manifest or name not in lock:
            found.append(f"{name} is in only one of the manifest and the lockfile")
        elif manifest[name]["version"] != lock[name]["version"]:
            found.append(f"{name} is locked at {lock[name]['version']}")
        else:
            for file, checksum in manifest[name]["files"].items():
                if hashlib.sha256((VENDOR / file).read_bytes()).hexdigest() != checksum:
                    found.append(f"{file} doesn't match its checksum")
    return found


def localized(file, data):
    # The browser has no bare module names, so hooks import preact by path.
    if file == "hooks.js":
        return re.sub(rb"""from\s*["']preact["']""", b'from"./preact.js"', data)
    return data


def update(name, entry, manifest):
    if name not in SOURCES:
        raise SystemExit(f"{name} is a custom bundle; rebuild it and update the manifest by hand.")
    with urllib.request.urlopen(entry["resolved"], timeout=60) as response:
        archive = response.read()
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(archive).digest()).decode()
    if integrity != entry["integrity"]:
        raise SystemExit(f"{name} doesn't match the integrity in its lockfile.")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as package:
        for file, source in SOURCES[name].items():
            data = localized(file, package.extractfile(f"package/{source}").read())
            (VENDOR / file).write_bytes(data)
            files[file] = hashlib.sha256(data).hexdigest()
    manifest[name] = {
        "version": entry["version"],
        "tarball": entry["resolved"],
        "integrity": integrity,
        "files": files,
    }
    print(f"{name} {entry['version']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Report differences; change nothing")
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text())
    lock = locked()
    if args.check:
        found = problems(manifest, lock)
        for problem in found:
            print(f"static/vendor: {problem}. Run contrib/vendor.py.", file=sys.stderr)
        raise SystemExit(1 if found else 0)
    for name, entry in lock.items():
        if manifest.get(name, {}).get("version") != entry["version"]:
            update(name, entry, manifest)
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
