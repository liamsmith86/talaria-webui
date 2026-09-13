# Talaria WebUI

[![CI](https://github.com/liamsmith86/talaria-webui/actions/workflows/ci.yml/badge.svg?event=pull_request)](https://github.com/liamsmith86/talaria-webui/actions/workflows/ci.yml)
[![MIT license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![Linux · macOS · WSL2](https://img.shields.io/badge/platform-Linux%20%C2%B7%20macOS%20%C2%B7%20WSL2-555)](#installation)

A lightweight web-based interface for using Hermes Agent.

Talaria WebUI is a lightweight, performant, and secure application for chatting with your Hermes Agent. The core development principle is that Talaria should remain a thin interface layer over your existing Hermes Agent, and as such all agentic system management including settings, prefills, and sessions are handled by your Hermes Agent software and are not subject to a third party reinterpretation.

Talaria WebUI uses Hermes Agent's [API server](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server) feature to interface wtih your agent and upon installation you will be prompted to enter an API key for connectivity.

Optionally you can install the Talaria Hermes Agent plugin, a lightweight extension for your Hermes Agent app which extends the information returned by the API. This allows Talaria WebUI to deliver a more polished experience; for example, the Hermes Agent API does not yet return context information with session data. This plugin allows us to continue offloading as much functionality to Hermes Agent's existing code without reinventing the wheel ourselves.

## Installation

Talaria connects remotely to your Hermes Agent's API and does not need to be installed on the same server.

### Ask your agent (Recommended)

```text
Install Talaria WebUI from https://github.com/liamsmith86/talaria-webui and
connect it to my Hermes Agent, including the recommended plugin. Use the
installer's --help for headless options and give me the URL and login details file path.
```

### Install manually

Linux, macOS, and Windows through WSL2:

```sh
curl -fsSL https://raw.githubusercontent.com/liamsmith86/talaria-webui/stable/install.sh | bash
```

Follow the prompts, then open the URL printed by the installer. Installs and updates follow tested stable releases.

For HTTPS, set `--bind local --public-url https://hermes.example.com` during setup and use your reverse proxy. For example, with Caddy and your own certificate:

```caddyfile
hermes.example.com {
    tls /path/to/fullchain.pem /path/to/privkey.pem
    reverse_proxy 127.0.0.1:8766
}
```

Omit `tls` to let Caddy manage the certificate. Keep the private key readable only by the proxy account, and reload the proxy after replacing it.

### Install via Docker

Install [Docker](https://docs.docker.com/get-started/get-docker/), then choose either option below.

#### GitHub Container Registry

For registry login, use your GitHub username and a [classic token](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry#authenticating-with-a-personal-access-token-classic) with `read:packages` as the password.

```sh
docker login ghcr.io
docker pull ghcr.io/liamsmith86/talaria-webui:latest
docker run -d --name talaria --restart unless-stopped \
  -p 127.0.0.1:8766:8766 -v talaria-data:/data \
  ghcr.io/liamsmith86/talaria-webui:latest
```

#### Build from Git

```sh
git clone --branch stable https://github.com/liamsmith86/talaria-webui.git
cd talaria-webui
docker build -t talaria-webui .
docker run -d --name talaria --restart unless-stopped \
  -p 127.0.0.1:8766:8766 -v talaria-data:/data \
  talaria-webui
```

After either option, open **http://127.0.0.1:8766** and retrieve the login password:

```sh
docker exec talaria cat /data/talaria/initial-password.txt
```

Connect to your Hermes API in Settings.

## Hermes plugin

Talaria can also use an optional, but highly recommended, plugin that you install on your Hermes Agent to extend the native API capabilities and improve the amount of information we can display in the WebUI. As Hermes Agent developers continue to extend the capabilities of their native API we will update Talaria to use native routes.

Run on your Hermes host:

```sh
hermes plugins install liamsmith86/talaria-webui/src/talaria/hermes_plugin --enable \
  --ref "$(git ls-remote https://github.com/liamsmith86/talaria-webui.git refs/heads/stable | cut -f1)"
hermes gateway restart
```

## Features

### Chat

- Live streaming replies with inline reasoning and tool activity.
- Markdown, syntax-highlighted code, and copy controls.
- Image and text attachments.
- Stop a response or send guidance while it runs.

### Sessions

- Search sessions and jump to matching messages in full history.
- Pin, rename, branch, and delete sessions.
- Edit messages, regenerate replies, and download transcripts.
- Native Hermes commands, including `/compress`.
- Context usage, token counts, response details, and API run status.

### Interface

- Mobile and desktop layouts with a collapsible sidebar.
- Light and dark themes with accent colors.
- Seven interface languages with automatic language selection.
- Switch between Hermes profiles and connections.
- Per-session model and reasoning settings.
- Inline approval requests and clarification questions.

Some features require the Hermes plugin.

## Configuration

| Setting | Where |
| --- | --- |
| Connection, login, listener, public URL | `~/.config/talaria/config.json` |
| Additional Hermes connections | `~/.config/talaria/profiles.json` |
| API key from an environment variable (initial profile) | `TALARIA_HERMES_API_KEY` |
| Custom configuration file | `talaria --config PATH` |

System installations use `/var/lib/talaria`; Docker uses `/data/talaria`.
