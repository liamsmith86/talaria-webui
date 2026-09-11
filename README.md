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
bash -c 'set -eu; f=$(mktemp); cleanup() { rm -f -- "$f"; }; trap cleanup EXIT; curl -qfsSL --proto "=https" --proto-redir "=https" --connect-timeout 20 --max-time 300 https://raw.githubusercontent.com/liamsmith86/talaria-webui/500fc5e91ae44d1930e4e66921d34ff25671b258/install.sh -o "$f"; if command -v sha256sum >/dev/null; then sum=$(sha256sum "$f"); else sum=$(shasum -a 256 "$f"); fi; [ "${sum%% *}" = 44f11ce129bd4b4021d1a27a3e6c63632f8319b2c06c215f7efbecda28e01683 ] || { echo "Installer checksum mismatch; nothing executed." >&2; exit 1; }; bash "$f"'
```

The pinned SHA-256 verifies the bootstrap script before execution (Linux/macOS).
The verified bootstrap then installs the latest `main` using its dependency lockfile.

Setup offers local/LAN/all-interface binding, a generated or chosen WebUI password,
local Hermes connectivity, the optional plugin, and an app service. It reuses existing
credentials and saves private backups before changing Hermes configuration.
Run `bash install.sh --help` for headless flags. An interrupted setup can be resumed
with the same options. Once installed, rerunning the script updates Talaria using the
saved configuration; it does not repeat password, binding, service, or Hermes setup.
Custom installation paths still need `--directory`; ambiguous installations are rejected.
`talaria update` is the faster routine update command and uses the same deployment code.
Local plugin updates can be linked to the managed updater (see below).
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
Hermes configuration changes run as the selected home’s owner.
The installer prints the actual paths and commands. Linux uses systemd where
available; macOS uses a user LaunchAgent. Otherwise, start the app manually.
Systemd user services follow the account's login/linger policy; LaunchAgents start
at login. The service uses a small launcher to apply updates while keeping the web
server unprivileged. No updater services or timers are installed.

Open **Settings → Talaria** to check for updates and install them. The page reconnects
after restarting; failed activation restores the previous release. Existing service
installations can rerun `install.sh` once to enable this. For a user-owned manual installation,
run `talaria supervise --directory /path/to/installation` to enable browser updates.

**Uninstall:** run the printed launcher path with `uninstall` (use `sudo` for a
system installation). It previews removal of Talaria's service, installation,
configuration, and passwords, then asks for confirmation. Add `--yes` for headless
use. Hermes, its plugin/sessions, and shared Python/Git/uv installations are kept.

### Hermes plugin

Use **Install in Hermes Desktop** in Settings → Connection, or open this URL:

```text
hermes://plugin/install?repo=liamsmith86/talaria-webui/src/talaria/hermes_plugin&enable=1
```

GitHub strips clickable `hermes://` links. Desktop asks for confirmation; select
the installation/profile used by Talaria. Private repositories require GitHub access.

Alternatively, on the Hermes host:

```sh
hermes plugins install liamsmith86/talaria-webui/src/talaria/hermes_plugin --enable
hermes gateway restart
```

To refresh this native installation, repeat with `--force` (or use the Desktop
link with `&force=1`). Hermes currently cannot `plugins update` subdirectory
installs. For a pinned installation, also supply the reviewed `--ref COMMIT`.
Native installation retains Hermes's source checks and consent flow.

**Bundled plugin**, from a Talaria checkout or installed Talaria package:

```sh
uv run --locked --no-dev talaria hermes-plugin --home /path/to/hermes-profile
hermes plugins enable talaria
hermes gateway restart
```

Run the Hermes commands in the same profile (use `hermes --profile NAME` for a
named profile). A multiplexed gateway also needs the plugin in its primary profile.
The plugin uses the same API key as Hermes; it does not require a separate key.

For temporary prompt diagnostics (plugin 1.2.1+), on the Hermes host:

```sh
hermes config set plugins.entries.talaria.settings.debug_requests true
```

Logs go to `~/.hermes/talaria/request-debug.jsonl` (inside the selected profile).
Talaria chat diagnostics include full assembled messages, system instructions,
prefill, and user input, including subsequent turns and tool continuations.
Generation parameters use Hermes's sanitized metadata and may be truncated.
Authentication headers are excluded; prompts and tool results can still contain
private information. Files are owner-only, with three rotated 16 MiB backups.
Requests above 8 MiB produce an explicit omission record.
Set the same flag to `false` to stop logging without restarting; existing logs remain.

Plugin 1.3.0+ with a current WebUI streams provider reasoning and presents clarification questions
inline. It inherits saved session models, priority and provider routing defaults,
and describes Talaria’s Markdown support while preserving custom platform hints.

Type `/` in chat to browse Hermes commands (plugin 1.2.0+). `/compress` and
`/compact` use Hermes's native compressor; `here 2`, a focus topic, and
`--preview` are supported. Command status appears inline without becoming saved
Hermes messages. Session/model commands open the existing WebUI controls; terminal
and messaging commands without an API equivalent are marked unavailable.


### Local plugin updates

To link an already installed, enabled **bundled local** plugin to a managed Talaria installation,
run this once as the installation owner (root for a system installation):

```sh
talaria hermes-plugin --home /path/to/hermes-profile --manage-updates --directory /opt/talaria
```

This links future updates; it does not restart Hermes now. It refreshes Talaria's
managed service permissions if needed. Specify `--hermes-command /absolute/path/to/hermes`
if the CLI is not on PATH. Browser updates and `talaria update` then install the
matching plugin and restart/verify Hermes only when needed. Rollbacks restore the
matching plugin too. The gateway may briefly disconnect while restarting.
`Sync linked plugin` also works when Talaria itself is current. Connection shows
the loaded plugin release; comparisons use Talaria's bundled version, not GitHub.
Native-managed plugins and their pins are never overwritten by this updater.

For a standalone local refresh after updating Talaria:

```sh
talaria hermes-plugin --home /path/to/hermes-profile --restart
```

Rerunning `install.sh --plugin --hermes-home /path/to/hermes-profile` also refreshes
an unlinked bundled plugin; add `--restart-hermes` to restart and verify it.

There is no SSH execution or remote plugin updater. On a separate Hermes host,
export the updated plugin there and manage its restart locally. For multiplexed
gateways, link the primary profile; additional profile plugin copies remain manual.

Hermes exposes its version through `/health` and authenticated `/health/detailed`;
Talaria displays it in **Your agent**, and local setup reports it. Compatibility is
checked by capabilities, not a hardcoded version cutoff. The plugin uses some
internal Hermes interfaces, so future breaking changes can still require an update.

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
- Session search, pinning, branching, and transcript downloads.
- Per-session model and reasoning choices; image and text attachments.
- Multiple Hermes profiles, mobile layouts, and light/dark themes.
- Optional plugin: context usage, response details, turn editing, full-history search, and recent API run status.

Search (plugin 1.4.0+) uses Hermes’s index and opens the matching message in a
read-only window. Choose **View latest messages** to continue the session.
Activity reflects known API runs; other channels and expired runs may have no status.

## Configuration

- `~/.config/talaria/config.json`: connection, web login, bind address, port, and `public_url`.
- `~/.config/talaria/profiles.json`: additional Hermes connections. API keys are stored in plaintext in private files with owner-only permissions.
- `TALARIA_HERMES_API_KEY`: runtime override for the initial Hermes connection only; additional profiles keep separate keys. The value is never saved to configuration or returned to the browser. Empty or malformed values are rejected.
- `--config PATH`: choose another configuration file. Docker stores configuration under `/data/talaria/`.
- Remote access: configure an HTTPS reverse proxy and set `public_url` to the exact browser URL, including any nonstandard port and subpath (see below).
- Change the web password with `~/.local/share/talaria/bin/talaria --set-password`, then restart Talaria.

Managed installations support `update`, `status`, and `rollback` through `~/.local/share/talaria/bin/talaria`. Linked local plugins follow updates and rollbacks; remote plugins remain manually managed.

### Secrets managers

Inject `TALARIA_HERMES_API_KEY` into the process running `talaria` (or `talaria supervise`)
using your secrets manager. Configure `hermes_url` in `config.json`; use `--skip-hermes`
during setup if you will supply connectivity this way. Restart Talaria after key rotation.
An existing saved key remains unchanged and is used again if the variable is unset.

For Docker, pass `-e TALARIA_HERMES_API_KEY` to forward the variable from your shell.
For systemd/launchd, configure the service's environment or secrets-manager wrapper;
an export in your terminal does not configure a separately launched service.
The managed launcher forwards this one credential to its unprivileged web process.

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
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_read_timeout 3600s;
    client_max_body_size 32m;
}
```

Keep buffering disabled for streaming. Talaria uses its configured public URL for
cookies and origin checks; forwarded headers cannot change it. Login allows 30 failed attempts per IP per hour (in memory; resets on restart).
For separate client limits behind a proxy, set `"trusted_proxies": ["127.0.0.1"]`
in Talaria’s config, using your proxy’s actual address or CIDR. Only these peers
may supply `X-Forwarded-For`; configure them to overwrite untrusted client headers.

## Development and platform support

Private PRs run lint only. There is no duplicate CI run after merging. Other checks stay disabled by default while private, including release validation; use **Actions → CI → Run workflow → full** to run them explicitly. Public PRs and releases automatically enable the full suite; documentation-only PRs still run just lint.

The full suite tests Linux x86-64/Python 3.12, Linux ARM64/Python 3.14, one macOS 26 ARM64/Python 3.14 job, WSL2, both Docker architectures, and native Hermes contracts. Browser and accessibility tests use Chromium, Firefox, and WebKit with two workers per engine and isolated browser contexts.

On Windows, use Ubuntu under WSL2 and follow the Linux installation instructions; keep the checkout in the Linux filesystem. CI tests this route. Native Windows installation is not supported. Docker Desktop can run the Linux image; connect to host services through `host.docker.internal` and publish the port with `-p 127.0.0.1:8766:8766` instead of `--network host`.

See [local development checks](CONTRIBUTING.md) for Git hooks, focused browser tests, native Hermes setup, and verifying regressions against an older revision. These checks run locally without adding CI jobs.

Dependencies use a seven-day cooldown and a committed lockfile. Docker's Python and uv images are pinned by digest; update these pins deliberately. The `Required checks` status gates merges.

Push a `vX.Y.Z` tag matching `pyproject.toml` on a commit from `main` to publish `ghcr.io/liamsmith86/talaria-webui:vX.Y.Z` for `linux/amd64` and `linux/arm64`, after the configured checks pass. Packages stay private; authenticate with `docker login ghcr.io` before pulling. No release tag means no image publication.
