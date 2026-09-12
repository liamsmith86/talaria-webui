# Talaria WebUI

[![CI](https://github.com/liamsmith86/talaria-webui/actions/workflows/ci.yml/badge.svg?event=pull_request)](https://github.com/liamsmith86/talaria-webui/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![Linux · macOS · WSL2](https://img.shields.io/badge/platform-Linux%20%C2%B7%20macOS%20%C2%B7%20WSL2-555)](#installation)

A lightweight web-based interface for using Hermes Agent.

Talaria WebUI is a lightweight, performant, and secure application for chatting with your Hermes Agent. The core development principle is that Talaria should remain a thin interface layer over your existing Hermes Agent, and as such all agentic system management including settings, prefills, and sessions are handled by your Hermes Agent software and are not subject to a third party reinterpretation.

Talaria WebUI uses Hermes Agent's [API server](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server) feature to interface wtih your agent and upon installation you will be prompted to enter an API key for connectivity.

Optionally you can install the Talaria Hermes Agent plugin, a lightweight extension for your Hermes Agent app which extends the information returned by the API. This allows Talaria WebUI to deliver a more polished experience; for example, the Hermes Agent API does not yet return context information with session data. This plugin allows us to continue offloading as much functionality to Hermes Agent's existing code without reinventing the wheel ourselves.

## Installation

Supports **Linux, macOS, and Windows through WSL2**. Root is optional; passwordless sudo is supported. The installer asks before installing missing dependencies, configures the connection and optional background service, and prints your URL and password file location.

```sh
git clone https://github.com/liamsmith86/talaria-webui.git
cd talaria-webui
bash install.sh
```

While the repository is private, authenticate Git first. The default URL is **http://127.0.0.1:8766**. Rerun the installer to resume setup or update an existing installation; saved credentials and settings are preserved.

<details>
<summary>Checksum-verified download (once the repository is public)</summary>

```sh
bash -c 'set -eu; f=$(mktemp); cleanup() { rm -f -- "$f"; }; trap cleanup EXIT; curl -qfsSL --proto "=https" --proto-redir "=https" --connect-timeout 20 --max-time 300 https://raw.githubusercontent.com/liamsmith86/talaria-webui/500fc5e91ae44d1930e4e66921d34ff25671b258/install.sh -o "$f"; if command -v sha256sum >/dev/null; then sum=$(sha256sum "$f"); else sum=$(shasum -a 256 "$f"); fi; [ "${sum%% *}" = 44f11ce129bd4b4021d1a27a3e6c63632f8319b2c06c215f7efbecda28e01683 ] || { echo "Installer checksum mismatch; nothing executed." >&2; exit 1; }; bash "$f"'
```

Verifies the pinned installer before execution, then installs the latest `main` with locked dependencies.

</details>

### Ask your agent

Paste this into Hermes:

```text
Install https://github.com/liamsmith86/talaria-webui for my existing Hermes Agent.
Clone it and read `bash install.sh --help`. Use --non-interactive --install-deps
--bind local --service auto. For local Hermes, supply --hermes-home with my
profile directory, --enable-hermes-api and --plugin. For remote Hermes, use
--hermes-url and --hermes-key-file. Preserve existing credentials and settings.
Ask before exposing it remotely or restarting active Hermes work; use
--restart-hermes when safe. Verify connectivity and give me the URL, password
file location, and service commands. Do not configure automatic updates.
```

## Hermes plugin

Select the plugin during setup for full-history search, context and response details, turn editing, reasoning streaming, and native commands such as `/compress`.

For Hermes on another host, run there:

```sh
hermes plugins install liamsmith86/talaria-webui/src/talaria/hermes_plugin --enable
hermes gateway restart
```

Use the same Hermes profile as Talaria. A multiplexed gateway also needs the plugin in its primary profile. The plugin uses Hermes's existing API key.

**Hermes Desktop:** use **Settings → Connection → Install in Hermes Desktop**, or paste this link into your browser:

```text
hermes://plugin/install?repo=liamsmith86/talaria-webui/src/talaria/hermes_plugin&enable=1
```

To update a native installation, repeat the install command with `--force`, then restart the gateway. Talaria shows plugin status in **Settings → Connection**. See [Hermes's plugin documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins).

## Docker

```sh
docker build -t talaria-webui .
docker run -d --name talaria --restart unless-stopped \
  -p 127.0.0.1:8766:8766 -v talaria-data:/data \
  talaria-webui --host 0.0.0.0
docker exec talaria cat /data/talaria/initial-password.txt
```

Open **http://127.0.0.1:8766** and connect to a reachable Hermes API. Docker Desktop can reach host services at `host.docker.internal`. On Linux, for Hermes bound to host localhost, replace `-p ...` with `--network host` and use `--host 127.0.0.1`.

The container runs unprivileged. Keep the `talaria-data` volume when rebuilding and replacing it to update. Install the plugin on the Hermes host; do not run Talaria's managed installer or updater inside Docker.

## Features

- Streaming chat with inline tools, approvals, and clarification questions.
- Session search, pinning, branching, editing, and transcript downloads.
- Model and reasoning selection, image and text attachments.
- Multiple Hermes profiles, mobile layouts, and light/dark themes.

Plugin-backed history search opens the matching message; **View latest messages** returns to the current session. Activity indicators reflect known API runs.

## Configuration

| Setting | Where |
| --- | --- |
| Connection, login, listener, public URL | `~/.config/talaria/config.json` |
| Additional Hermes connections | `~/.config/talaria/profiles.json` |
| Runtime API key override (initial profile only) | `TALARIA_HERMES_API_KEY` |
| Custom configuration file | `talaria --config PATH` |

System installations use `/var/lib/talaria`; Docker uses `/data/talaria`. Setup prints the actual paths. Saved API keys are plaintext in owner-only files. The environment override is never saved or returned to the browser; inject it into the service environment, or pass `-e TALARIA_HERMES_API_KEY` to Docker. Restart after configuration or key changes.

### Reverse proxy

Bind Talaria locally and set `public_url` to the exact HTTPS browser URL, including any port or subpath, e.g. `https://hermes.example.com/telaria`. Setup and Docker also accept `--public-url`.

Example inside an HTTPS nginx server block:

```nginx
location = /telaria { return 308 /telaria/$is_args$args; }
location /telaria/ {
    proxy_pass http://127.0.0.1:8766;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_read_timeout 3600s;
    client_max_body_size 32m;
}
```

Keep buffering off for streaming. To apply login limits per client behind a proxy, set `trusted_proxies` in Talaria's config to that proxy's address/CIDR, e.g. `["127.0.0.1"]`.

## Updates and removal

Use **Settings → Talaria** to check for updates and install them. Managed services restart automatically; failed activation restores the previous release.

```sh
talaria update          # Update manually
talaria rollback        # Restore the previous release
talaria --set-password  # Change the login password; restart afterward
talaria uninstall       # Preview removal and confirm
```

Use the launcher path printed by setup if `talaria` is not on PATH, and `sudo` for a system installation. Uninstall preserves Hermes, its sessions/plugin, and shared dependencies.

To include an existing **bundled local** plugin in managed updates:

```sh
talaria hermes-plugin --home /path/to/hermes-profile --manage-updates --directory /opt/talaria
```

Substitute your installation directory. Linked updates restart Hermes only when needed; native-managed or remote plugins stay manually managed. No automatic-update service or timer is installed.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, local checks, browser tests, and native Hermes contracts. Dependencies use committed lockfiles and a seven-day release cooldown. Native Windows installation is unsupported; use WSL2 or Docker.
