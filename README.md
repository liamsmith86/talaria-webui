# Talaria WebUI

A lightweight web-based interface for using Hermes Agent.

Talaria is a responsive, password-protected interface for chatting with your existing Hermes Agent. It stays a thin interface layer: agent configuration, tools, memory, instructions, prefills, and sessions remain managed by Hermes.

Talaria connects through Hermes Agent’s [API server](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server). On first sign-in, enter your Hermes address, profile, and API key.

The optional Talaria plugin extends the Hermes API with context usage, response details, and conversation editing. It reuses Hermes’s existing functionality; standard chat works without it.

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
- Per-conversation model and reasoning choices; image and text attachments.
- Multiple Hermes profiles, mobile layouts, and light/dark themes.
- Optional plugin: context usage, response details, and turn editing.

## Configuration

- `~/.config/talaria/config.json`: connection, web login, bind address, port, and `public_url`.
- `~/.config/talaria/profiles.json`: additional Hermes connections. API keys are stored in plaintext in private files with owner-only permissions.
- `--config PATH`: choose another configuration file. Docker stores configuration under `/data/talaria/`.
- Remote access: configure an HTTPS reverse proxy and set `public_url` to the exact browser origin, including any nonstandard port.
- Change the web password with `~/.local/share/talaria/bin/talaria --set-password`, then restart Talaria.

Managed installations support `update`, `status`, and `rollback` through `~/.local/share/talaria/bin/talaria`. Refresh the bundled plugin separately after updating Talaria, then restart the Hermes gateway.
