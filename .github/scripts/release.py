"""Validate releases and promote tested multi-platform images without rewinding latest."""

import ast
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

VERSION = "org.opencontainers.image.version"
REVISION = "org.opencontainers.image.revision"


def version(tag):
    match = re.fullmatch(
        r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(alpha|beta|rc)\.(0|[1-9]\d*))?",
        tag,
    )
    if not match:
        raise ValueError("Use vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-rc.N (also alpha/beta).")
    core = tuple(map(int, match.group(1, 2, 3)))
    suffix = {"alpha": "a", "beta": "b", "rc": "rc"}.get(match[4], "")
    package = ".".join(map(str, core)) + (suffix + match[5] if suffix else "")
    return core, bool(suffix), package


def release_version():
    tag = os.environ["RELEASE_TAG"]
    core, prerelease, package = version(tag)
    if os.environ["RELEASE_PRERELEASE"] != str(prerelease).lower():
        raise ValueError("The GitHub prerelease setting must match the version tag.")
    return tag, core, prerelease, package


def validate():
    tag, _, _, package = release_version()
    subprocess.run(["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"], check=True)
    tagged = subprocess.check_output(["git", "rev-parse", f"refs/tags/{tag}^{{commit}}"], text=True)
    if tagged.strip() != os.environ["GITHUB_SHA"]:
        raise ValueError("Release tag no longer points to the checked commit.")
    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    module = ast.parse(Path("src/talaria/__init__.py").read_text())
    exported = next(
        ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        )
    )
    if project != package or exported != package:
        raise ValueError("Release tag, pyproject.toml and talaria.__version__ must agree.")


def inspect(reference):
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", reference],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        if (
            "manifest unknown" in result.stderr.lower()
            or f"{reference}: not found" in result.stderr
        ):
            return None
        raise RuntimeError("Cannot inspect registry image; check registry access and connectivity.")
    return json.loads(result.stdout)


def create(reference, sources, annotations=()):
    subprocess.run(
        [
            "docker",
            "buildx",
            "imagetools",
            "create",
            "--tag",
            reference,
            *[arg for annotation in annotations for arg in ("--annotation", "index:" + annotation)],
            *sources,
        ],
        check=True,
        timeout=120,
    )


def verify_index(index, tag, revision):
    annotations = index.get("annotations", {})
    platforms = {
        (entry.get("platform", {}).get("os"), entry.get("platform", {}).get("architecture"))
        for entry in index.get("manifests", [])
        if entry.get("platform", {}).get("os") != "unknown"
    }
    if (annotations.get(VERSION), annotations.get(REVISION), platforms) != (
        tag,
        revision,
        {("linux", "amd64"), ("linux", "arm64")},
    ):
        raise ValueError("Image tag already exists with different or unverified release content.")


def publish():
    tag, core, prerelease, _ = release_version()
    image = "ghcr.io/" + os.environ["GITHUB_REPOSITORY"].lower()
    revision = os.environ["GITHUB_SHA"]
    target = image + ":" + tag
    existing = inspect(target)
    if existing is None:
        digests = [Path("digests", arch).read_text().strip() for arch in ("amd64", "arm64")]
        if not all(re.fullmatch(r"sha256:[0-9a-f]{64}", digest) for digest in digests):
            raise ValueError("Missing or invalid tested image digest.")
        create(
            target,
            [image + "@" + digest for digest in digests],
            (f"{VERSION}={tag}", f"{REVISION}={revision}"),
        )
        existing = inspect(target)
    # A rerun keeps the already published version instead of overwriting it.
    verify_index(existing or {}, tag, revision)
    if prerelease:
        return
    latest = inspect(image + ":latest")
    if latest is not None:
        latest_tag = latest.get("annotations", {}).get(VERSION, "")
        latest_core, latest_pre, _ = version(latest_tag)
        if latest_pre:
            raise ValueError("Latest points to a prerelease; review registry tags.")
        if latest_core == core:
            verify_index(latest, tag, revision)
        if latest_core >= core:
            return
    create(image + ":latest", [target])
    verify_index(inspect(image + ":latest") or {}, tag, revision)


if __name__ == "__main__":
    {"validate": validate, "publish": publish}[sys.argv[1]]()
