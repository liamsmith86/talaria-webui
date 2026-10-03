"""Validate an immutable plugin snapshot and prepare an unsubmitted Hermes catalog entry.

Run with the Python interpreter from a prepared Hermes checkout. No services,
real profiles, repository writes, or GitHub mutations are needed.
"""

import argparse
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = "src/talaria/hermes_plugin"
REPOSITORY = "https://github.com/liamsmith86/talaria-webui"
CAPABILITIES = ("provides_tools", "provides_hooks", "provides_middleware", "requires_env")
REQUIRED_CHECKS = {
    "manifest",
    "manifest fields",
    "requires_hermes",
    "config schema",
    "requires_env",
    "loadable",
    "python dependencies",
    "capability probe",
    "declared tools",
    "declared hooks",
    "declared middleware",
    "built-in tool collisions",
    "security scan",
    "no core override",
}


def git(*args, cwd=ROOT):
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def export(revision, destination):
    sha = git("rev-parse", "--verify", f"{revision}^{{commit}}")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("The catalog needs a full 40-character commit SHA.")
    archive = subprocess.check_output(["git", "archive", sha], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(destination, filter="data")
    return sha


def require_clean_validation(result):
    # Hermes currently reports dependency-preparation exceptions on stderr but
    # can still return zero if the plugin's registration imports happen to work.
    if result.returncode or "dependency preparation failed:" in result.stderr.lower():
        raise ValueError("Hermes validation or dependency preparation failed.")
    report = json.loads(result.stdout)
    checks = {check["name"]: check for check in report.get("checks", [])}
    if not report.get("ok") or not REQUIRED_CHECKS.issubset(checks):
        raise ValueError("The complete current Hermes admission checks must pass.")
    if not all(check.get("ok") is True for check in checks.values()):
        raise ValueError("A Hermes admission check failed.")
    if report.get("warnings"):
        raise ValueError("Review and resolve validation warnings before preparing a submission.")
    if checks["security scan"].get("detail") != "safe":
        raise ValueError("The install scanner must report safe.")
    return report


def validate(plugin, profile):
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "UV_CACHE_DIR"}
    }
    env.update(
        HERMES_HOME=str(profile),
        HERMES_ENABLE_PROJECT_PLUGINS="false",
    )
    command = [
        str(Path(sys.executable).with_name("hermes")),
        "plugins",
        "validate",
        str(plugin),
        "--install-deps",
        "--json",
    ]
    print("+ " + " ".join(command), flush=True)
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=600)
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    print(result.stdout, end="")
    return require_clean_validation(result)


def quarantined_requirements(manifest, root):
    # PM strips ambient UV_EXCLUDE_NEWER. Resolve the plugin's own requirements
    # separately with an explicit cutoff, then validate under core constraints.
    output = root / "plugin-requirements.txt"
    subprocess.run(
        [
            "uv",
            "pip",
            "compile",
            "--no-config",
            "--exclude-newer",
            "14 days",
            "--python",
            sys.executable,
            "--generate-hashes",
            "--no-header",
            "--output-file",
            str(output),
            "-",
        ],
        input="\n".join(manifest.get("python_dependencies", [])) + "\n",
        stdout=subprocess.DEVNULL,
        text=True,
        check=True,
        timeout=120,
    )
    return output.read_text()


def entry_for(manifest, sha):
    raw = f"https://raw.githubusercontent.com/liamsmith86/talaria-webui/{sha}"
    return {
        "name": manifest["name"],
        "repo": REPOSITORY,
        "sha": sha,
        "subdir": PLUGIN,
        "description": manifest["description"],
        "maintainer": "liamsmith86",
        "tier": "community",
        "category": "web",
        "requires_hermes": manifest["requires_hermes"],
        "docs_url": f"{REPOSITORY}/tree/{sha}/{PLUGIN}",
        "version": str(manifest["version"]),
        "image": f"{raw}/.github/assets/social-preview.jpg",
        "screenshots": [
            f"{raw}/.github/assets/demo-desktop.gif",
            f"{raw}/.github/assets/demo-mobile.gif",
        ],
        "readme": True,
        "platforms": ["linux", "macos"],
        "capabilities": {key: manifest.get(key, []) for key in CAPABILITIES},
    }


def check_entry(entry, hermes_source):
    path = hermes_source / "scripts/validate_plugin_catalog.py"
    spec = importlib.util.spec_from_file_location("hermes_catalog_validator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    errors, warnings = module.validate_entry(entry)
    if errors or warnings:
        raise ValueError("Catalog entry: " + "; ".join([*errors, *warnings]))


def submission(entry, hermes_sha):
    return f"""Add {entry["name"]} {entry["version"]} to the community catalog.

Talaria is an independent web client for Hermes. This entry installs only its
optional Python API extension from `{entry["subdir"]}`. The web application,
standalone installer, and updater are outside the catalog package.

Pin: `{entry["sha"]}`. The version and all gallery images refer to that tree.

Surfaces: `register_platform_handler("api_server", ...)`, `pre_api_request`,
`post_api_request`, and `on_session_end`. No tools, middleware, privileged
capabilities, Desktop renderer code, or core-method replacements.

Disclosures:
- Reads the enabled Hermes profile's configuration, session database, model
  inventory, identity files, and configured instructions/prefill. A configured
  prefill path can be outside the profile directory.
- Authenticated actions can branch, rewind, or compress API-session history.
- Chat/compression and inventory refreshes use Hermes's configured providers
  and catalog sources; provider charges can apply.
- Stores bounded response metadata locally. The plugin has no full-prompt
  debug logger and adds no formatting instructions to model requests.
- Uses Hermes-owned authentication. No independent vendor-login reads, OAuth
  token rotation, stored credentials, analytics, or telemetry exporter.
- Starts no shell children or separate daemon. Native command jobs run inside
  the gateway; agent tool execution retains Hermes's approvals and guards.
  Clarification waits use native timeouts and cancellation.
- No self-updater. Catalog changes require a reviewed SHA-bump PR and
  `hermes plugins update {entry["name"]}`. The standalone updater protects
  Hermes-owned installations.

Full behavior and storage details: [plugin README]({entry["docs_url"]}).

Validation: exported exactly `{entry["sha"]}` and ran
`hermes plugins validate {PLUGIN} --install-deps` (with JSON output)
using Hermes `{hermes_sha}`. All admission checks passed, the security verdict
was `safe`, dependency preparation succeeded, and there were no warnings.
The entry also passed Hermes's catalog schema validator.

![Desktop]({entry["screenshots"][0]})
![Mobile]({entry["screenshots"][1]})
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default="HEAD", help="Commit to export and pin")
    parser.add_argument("--output", type=Path, required=True, help="Directory for review artifacts")
    args = parser.parse_args()
    import hermes_cli
    import hermes_yaml as yaml

    hermes_source = Path(hermes_cli.__file__).resolve().parents[1]
    hermes_sha = git("rev-parse", "HEAD", cwd=hermes_source)
    with tempfile.TemporaryDirectory(prefix="talaria-catalog-") as temporary:
        root = Path(temporary)
        checkout = root / "checkout"
        checkout.mkdir()
        sha = export(args.revision, checkout)
        plugin = checkout / PLUGIN
        manifest = yaml.safe_load((plugin / "plugin.yaml").read_text())
        requirements = quarantined_requirements(manifest, root)
        report = validate(plugin, root / "profile")
        entry = entry_for(manifest, sha)
        check_entry(entry, hermes_source)
    args.output.mkdir(parents=True, exist_ok=True)
    encoded = yaml.safe_dump(entry, sort_keys=False)
    encoded = re.sub(
        r"^version:.*$", f"version: {json.dumps(entry['version'])}", encoded, flags=re.M
    )
    (args.output / f"{entry['name']}.yaml").write_text(encoded)
    report.update(plugin_revision=sha, hermes_revision=hermes_sha)
    (args.output / "validation.json").write_text(json.dumps(report, indent=2) + "\n")
    (args.output / "plugin-requirements.txt").write_text(requirements)
    (args.output / "submission.md").write_text(submission(entry, hermes_sha))
    print(f"Prepared {entry['name']} {entry['version']} at {sha}: {args.output}")
    print("Publishing this commit and owner-submitted maintainer review are still required.")


if __name__ == "__main__":
    main()
