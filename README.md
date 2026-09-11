# Talaria WebUI

A lightweight web-based interface for using Hermes Agent.

Talaria WebUI is a lightweight, performant, and secure application for chatting with your Hermes Agent. The core development principle is that Talaria should remain a thin interface layer over your existing Hermes Agent, and as such all agentic system management including settings, prefills, and sessions are handled by your Hermes Agent software and are not subject to a third party reinterpretation.

Talaria WebUI uses Hermes Agent's [API server](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server) feature to interface wtih your agent and upon installation you will be prompted to enter an API key for connectivity.

Optionally you can install the Talaria Hermes Agent plugin, a lightweight extension for your Hermes Agent app which extends the information returned by the API. This allows Talaria WebUI to deliver a more polished experience; for example, the Hermes Agent API does not yet return context information with session data. This plugin allows us to continue offloading as much functionality to Hermes Agent's existing code without reinventing the wheel ourselves.

## Installation

### Ask your agent

Paste this into your existing Hermes Agent:

```text
Install https://github.com/liamsmith86/talaria-webui for my existing Hermes
Agent. Clone the repo, read `bash install.sh --help`, and use its installer.

Use --non-interactive --install-deps --bind local --service auto,
--hermes-home with my actual profile directory, --enable-hermes-api,
--plugin, and --restart-hermes. Preserve existing keys and settings.
If Hermes is elsewhere, use --hermes-url and --hermes-key-file instead.
Ask before exposing it remotely or interrupting active agent work.

Verify the URL and Hermes connection. Give me the sign-in password file
location and the start, stop, restart, and manual update commands.
Do not configure automatic updates.
```

### Guided or manual

Linux, macOS, and WSL2. Root is optional; passwordless sudo also enables system installation.
Missing prerequisites require consent.
While this repository is private, authenticate Git first.

```sh
git clone https://github.com/liamsmith86/talaria-webui.git
cd talaria-webui
bash install.sh
```

After the repository becomes public, the same installer can be launched with:

```sh
curl -fsSL https://raw.githubusercontent.com/liamsmith86/talaria-webui/main/install.sh | bash
```

Setup offers local/LAN/all-interface binding, a generated or chosen WebUI password,
local Hermes connectivity, the optional plugin, and an app service. It reuses existing
credentials and saves private backups before changing Hermes configuration.
Run `bash install.sh --help` for headless flags. An interrupted setup can be resumed
with the same options. Once installed, rerunning the script updates Talaria using the
saved configuration; it does not repeat password, binding, service, or Hermes setup.
Custom installation paths still need `--directory`; ambiguous installations are rejected.
`talaria update` is the faster routine update command and uses the same deployment code.
The Hermes plugin is updated separately using the plugin command below.
If your Hermes home is missing, root/sudo setup checks at most 32 other account
homes and offers a selection. It does not recursively search or read credentials
until a home is selected. Use `--hermes-home PATH` for headless selection or custom
locations. Interactive highlighting respects `NO_COLOR` and `TERM=dumb`.

For an existing Python 3.12+, Git, and uv installation, the wizard is also available
as `uv run --locked --no-dev talaria setup`. For configuration entirely by hand:

```sh
uv run --locked --no-dev talaria install
~/.local/share/talaria/bin/talaria
```

The default URL is **http://127.0.0.1:8766**. The login password is saved in
`~/.config/talaria/initial-password.txt`. Root installs with a system service use
`/opt/talaria` and `/var/lib/talaria`, and run the app as an unprivileged account.
Git authentication and local Hermes changes retain the invoking user’s identity.
The installer prints the actual paths and commands. Linux uses systemd where
available; macOS uses a user LaunchAgent. Otherwise, start the app manually.
Systemd user services follow the account's login/linger policy; LaunchAgents start
at login. No updater services or timers are installed.

**Plugin only**, on the Hermes host:

```sh
uv run --locked --no-dev talaria hermes-plugin --home /path/to/hermes-profile
hermes plugins enable talaria
hermes gateway restart
```

Run the Hermes commands in the same profile (use `hermes --profile NAME` for a
named profile). A multiplexed gateway also needs the plugin in its primary profile.
The plugin uses the same API key as Hermes; it does not require a separate key.

### Docker

From this checkout, on Linux with Hermes running on the same host:

```sh
docker build -t talaria-webui .
docker run -d --name talaria --restart unless-stopped \
  --network host -v talaria-data:/data \
  talaria-webui --host 127.0.0.1
docker exec talaria cat /data/talaria/initial-password.txt
```

Open **http://127.0.0.1:8766** and connect to Hermes. The container runs as a non-root
user; `talaria-data` preserves its password and configuration across replacements.
For updates, pull this checkout, rebuild the image, stop/remove the old container,
and repeat the run command with the same volume. Do not run `install.sh`,
`talaria install`, or `talaria update` inside a container.

Behind an HTTPS proxy, append `--public-url https://hermes.example.com/telaria`
to the run command above. The same subpath support applies to Docker. Keep the
internal port at 8766; on bridge networks, bind with `--host 0.0.0.0` and choose
the external port using `-p 127.0.0.1:8766:8766`. Docker Desktop can reach host
Hermes through `host.docker.internal`; localhost inside a bridged container is
the container itself.

To choose a password, stop the running container and use:

```sh
docker run --rm -it -v talaria-data:/data talaria-webui --set-password
```

Restart the container afterward. To export the plugin on the Hermes host without
installing Python or uv there (replace the profile path as needed):

```sh
mkdir -p "$HOME/.hermes/plugins"
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$HOME/.hermes/plugins:/export/plugins" \
  talaria-webui hermes-plugin --home /export
hermes plugins enable talaria
hermes gateway restart
```

Run those Hermes commands in the same profile. The container does not enable or
restart the host's Hermes automatically.

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
- Remote access: configure an HTTPS reverse proxy and set `public_url` to the exact browser URL, including any nonstandard port and subpath (see below).
- Change the web password with `~/.local/share/talaria/bin/talaria --set-password`, then restart Talaria.

Managed installations support `update`, `status`, and `rollback` through `~/.local/share/talaria/bin/talaria`. Refresh the bundled plugin separately after updating Talaria, then restart the Hermes gateway.

### Reverse proxy

Keep the listener on localhost and set `public_url` to the external URL, for example
`https://hermes.example.com/telaria`. Restart Talaria after changing it. Setup also
accepts `--public-url`. Both prefix-preserving and prefix-stripping proxies work.

Inside an existing HTTPS nginx server block:

```nginx
location = /telaria { return 308 /telaria/$is_args$args; }
location /telaria/ {
    proxy_pass http://127.0.0.1:8766;
    proxy_set_header Host $http_host;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_read_timeout 3600s;
    client_max_body_size 32m;
}
```

Keep buffering disabled for streaming. Talaria uses its configured public URL for
cookies and origin checks; forwarded headers cannot change it. Login rate limits
are shared when requests arrive through the same proxy address.

## Development and platform support

Private PRs run lint only. There is no duplicate CI run after merging. Other checks stay disabled by default while private, including release validation; use **Actions → CI → Run workflow → full** to run them explicitly. Public PRs and releases automatically enable the full suite; documentation-only PRs still run just lint.

The full suite tests Linux x86-64/Python 3.12, Linux ARM64/Python 3.14, one macOS 26 ARM64/Python 3.14 job, WSL2, both Docker architectures, and native Hermes contracts. Browser and accessibility tests use Chromium, Firefox, and WebKit with two workers per engine and isolated browser contexts.

On Windows, use Ubuntu under WSL2 and follow the Linux installation instructions; keep the checkout in the Linux filesystem. CI tests this route. Native Windows installation is not supported. Docker Desktop can run the Linux image; connect to host services through `host.docker.internal` and publish the port with `-p 127.0.0.1:8766:8766` instead of `--network host`.

See [local development checks](CONTRIBUTING.md) for Git hooks, focused browser tests, native Hermes setup, and verifying regressions against an older revision. These checks run locally without adding CI jobs.

Dependencies use a seven-day cooldown and a committed lockfile. Docker's Python and uv images are pinned by digest; update these pins deliberately. The `Required checks` status gates merges.

Push a `vX.Y.Z` tag matching `pyproject.toml` on a commit from `main` to publish `ghcr.io/liamsmith86/talaria-webui:vX.Y.Z` for `linux/amd64` and `linux/arm64`, after the configured checks pass. Packages stay private; authenticate with `docker login ghcr.io` before pulling. No release tag means no image publication.
