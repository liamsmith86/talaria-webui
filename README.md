# Talaria WebUI

A lightweight web-based interface for using Hermes Agent.

Talaria WebUI is a lightweight, performant, and secure application for chatting with your Hermes Agent. The core development principle is that Talaria should remain a thin interface layer over your existing Hermes Agent, and as such all agentic system management including settings, prefills, and sessions are handled by your Hermes Agent software and are not subject to a third party reinterpretation.

Talaria WebUI uses Hermes Agent's [API server](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server) feature to interface wtih your agent and upon installation you will be prompted to enter an API key for connectivity.

Optionally you can install the Talaria Hermes Agent plugin, a lightweight extension for your Hermes Agent app which extends the information returned by the API. This allows Talaria WebUI to deliver a more polished experience; for example, the Hermes Agent API does not yet return context information with session data. This plugin allows us to continue offloading as much functionality to Hermes Agent's existing code without reinventing the wheel ourselves.

## Installation

### Ask your agent

Paste this into your existing Hermes Agent:

```text
Set up https://github.com/liamsmith86/talaria-webui as a persistent WebUI
for my existing Hermes Agent, including its bundled Hermes plugin.

Read the repository's README and service templates, inspect this machine,
and install any missing prerequisites. Reuse my existing Hermes profile
and preserve its configuration. Enable its API server with a strong key
if needed, and configure Talaria with that key without printing it.

Use the provided systemd user service where supported. Keep Talaria on
localhost unless I request remote access; remote access should use HTTPS
and the matching public_url setting. Verify Talaria's health endpoint,
authenticated Hermes connectivity, and plugin detection. Give me the URL,
login-password file location, and start, stop, and update commands.
```

### Manual

On Linux or macOS, requires Python 3.12+, Git, uv, and a configured Hermes API server. No Node build is needed.

```sh
git clone https://github.com/liamsmith86/talaria-webui.git
cd talaria-webui
uv run talaria install
~/.local/share/talaria/bin/talaria
```

Open **http://127.0.0.1:8766**. First launch saves your login password to `~/.config/talaria/initial-password.txt`. Sign in and connect to Hermes, usually at `http://127.0.0.1:8642` with profile `default`.

For a systemd user service, copy the [service template](contrib/systemd/talaria.service) to `~/.config/systemd/user/` and run `systemctl --user daemon-reload` **before installation**. Add `--service talaria.service --scope user` to the initial install command, then run `systemctl --user enable talaria.service`. The installer starts the service.

**Optional plugin:** on the Hermes host, from a Talaria checkout:

```sh
uv run talaria hermes-plugin
hermes plugins enable talaria
hermes gateway restart
```

For a named profile, export with `--home /path/to/profile` and enable with `hermes --profile NAME plugins enable talaria`. Multiplexed gateways also need the plugin installed and enabled in their primary profile.

### Docker

From this checkout, on Linux with Hermes running on the same host:

```sh
docker build -t talaria-webui .
docker run -d --name talaria --restart unless-stopped \
  --network host -v talaria-data:/data \
  talaria-webui --host 127.0.0.1
docker exec talaria cat /data/talaria/initial-password.txt
```

Open **http://127.0.0.1:8766** and connect to Hermes. The container runs as a non-root user; the named volume preserves configuration. For updates, rebuild and recreate the container using the same volume.

## Features

- Streaming chat, tool activity, approvals, and guidance during responses.
- Conversation search, pinning, branching, and transcript downloads.
- Per-session model and reasoning choices; image and text attachments.
- Multiple Hermes profiles, mobile layouts, and light/dark themes.
- Optional plugin: context usage, response details, turn editing, and correct branch identity on affected Hermes versions.

## Configuration

- `~/.config/talaria/config.json`: connection, web login, bind address, port, and `public_url`.
- `~/.config/talaria/profiles.json`: additional Hermes connections. API keys are stored in plaintext in private files with owner-only permissions.
- `--config PATH`: choose another configuration file. Docker stores configuration under `/data/talaria/`.
- Remote access: configure an HTTPS reverse proxy and set `public_url` to the exact browser origin, including any nonstandard port.
- Change the web password with `~/.local/share/talaria/bin/talaria --set-password`, then restart Talaria.

Managed installations support `update`, `status`, and `rollback` through `~/.local/share/talaria/bin/talaria`. Refresh the bundled plugin separately after updating Talaria, then restart the Hermes gateway.

## Development and platform support

Private PRs run lint only. There is no duplicate CI run after merging. Other checks stay disabled by default while private, including release validation; use **Actions → CI → Run workflow → full** to run them explicitly. Public PRs and releases automatically enable the full suite; documentation-only PRs still run just lint.

The full suite tests Linux x86-64/Python 3.12, Linux ARM64/Python 3.14, one macOS 26 ARM64/Python 3.14 job, WSL2, both Docker architectures, and native Hermes contracts. Browser and accessibility tests use Chromium, Firefox, and WebKit with two workers per engine and isolated browser contexts.

On Windows, use Ubuntu under WSL2 and follow the Linux installation instructions; keep the checkout in the Linux filesystem. CI tests this route. Native Windows installation is not supported. Docker Desktop can run the Linux image; connect to host services through `host.docker.internal` and publish the port with `-p 127.0.0.1:8766:8766` instead of `--network host`.

See [local development checks](CONTRIBUTING.md) for Git hooks, focused browser tests, native Hermes setup, and verifying regressions against an older revision. These checks run locally without adding CI jobs.

Dependencies use a seven-day cooldown and a committed lockfile. Docker's Python and uv images are pinned by digest; update these pins deliberately. The `Required checks` status gates merges.

Push a `vX.Y.Z` tag matching `pyproject.toml` on a commit from `main` to publish `ghcr.io/liamsmith86/talaria-webui:vX.Y.Z` for `linux/amd64` and `linux/arm64`, after the configured checks pass. Packages stay private; authenticate with `docker login ghcr.io` before pulling. No release tag means no image publication.
