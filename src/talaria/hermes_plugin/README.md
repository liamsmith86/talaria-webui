# talaria-webui

An optional API extension for [Talaria WebUI](../../../README.md). It adds profile
instructions and prefill, response and context details, history search, session
controls, and interactive run events to an existing Hermes gateway.

This directory is the complete plugin. The web application, installer, and
standalone updater live outside it and are not installed by the plugin catalog.
The plugin has no Desktop renderer code and does not replace a bundled Hermes
plugin. Talaria is an independent MIT-licensed project maintained by
[Liam Smith](https://github.com/liamsmith86).

![Talaria desktop chat](../../../.github/assets/demo-desktop.gif)

![Talaria mobile chat](../../../.github/assets/demo-mobile.gif)

## Installation and updates

Requires Hermes **0.21.5 or later**, Python 3.12+, and the enabled Hermes API
server. Hermes release `v2026.9.24` ships SemVer `0.21.5`; the manifest uses the
SemVer, not the dated release tag. Linux, macOS, and Linux under WSL2 are tested.
An API client still needs its configured Hermes API key.

For a catalog release, install the reviewed entry by name:

```sh
hermes plugins install talaria-webui --enable
```

That command requires the `talaria-webui` entry to have been accepted into the Hermes
catalog. For source installation before admission, use the
[repository instructions](../../../README.md#hermes-plugin).

Enable the plugin separately in each profile that should expose its routes. For
a multiplexed gateway, install and enable it in the primary profile as well.
Restart the gateway when no active work needs it; never restart the gateway
hosting the session performing the installation.

Catalog updates use:

```sh
hermes plugins update talaria-webui
```

The catalog entry pins a full 40-character commit. Updates require another
maintainer-reviewed catalog PR, with the plugin version and screenshot pins
updated together. The plugin does not check for, download, or replace its own
code. Talaria's separate standalone installer and updater refuse to overwrite a
plugin managed by Hermes, including a catalog-pinned installation.

## Hermes integration

The manifest declares every registered hook and middleware:

| Surface | Use |
| --- | --- |
| `register_platform_handler("api_server", ...)` | Add `/talaria/v1/*` and `/p/{profile}/talaria/v1/*` routes to the existing aiohttp application. |
| `pre_api_request` | Collect response metadata and, only when explicitly enabled, log Talaria model inputs. |
| `post_api_request` | Observe model, token usage, timing, and completion metadata. |
| `on_session_end` | Associate observations with the saved assistant message. |
| `llm_request` middleware | Describe Talaria's Markdown renderer in the outgoing request while preserving profile instructions and stored history. |

There are no registered tools, CLI commands, provider plugins, environment
requirements, or privileged capability requests. The `api_server` factory is
also disclosed here because the catalog capability block has no field for it.

Routes use Hermes's authentication and native session/run operations. A
per-request adapter view forwards to the existing adapter; it never replaces
methods on Hermes classes, modules, or live agents. Compression uses Hermes's
shared manual-compression implementation. Unsupported optional operations are
reported as unavailable. Native contract tests cover the release baseline and a
current upstream revision.

## Data, network, and execution disclosures

- **Profile reads:** Hermes configuration, session history and search indexes,
  model inventory, `IDENTITY.md`/`SOUL.md` for the agent's display name, and the
  profile's configured instructions and prefill. An explicitly configured
  prefill file may be outside the Hermes profile directory. Instructions and
  prefill go to the configured model; their contents and paths are not returned
  by the capability/status endpoint.
- **Session changes:** Authenticated user actions can branch or rewind API
  sessions and manually compress their history. Hermes owns persistence,
  idempotency, active-turn leases, and cancellation. Search can read other
  visible sessions in the authenticated profile; edits are restricted to API
  sessions.
- **Local storage:** `$HERMES_HOME/talaria/observations.db` keeps up to 10,000
  response metadata records for the enabled profile. These contain identifiers,
  model names, token counts, timings, and completion status, not message bodies
  or credentials. Disabling the plugin stops collection but does not delete
  existing files.
- **Optional sensitive logs:** `debug_requests` defaults to `false`. Enabling
  it writes full Talaria model inputs, including system instructions, messages,
  and tool definitions, to `$HERMES_HOME/talaria/request-debug.jsonl`. Content
  can contain personal information or secrets supplied in a conversation.
  Files are owner-readable/writable only, rotate at 16 MiB with three backups,
  and omit individual records over 8 MiB. API keys and authorization headers
  are excluded from the parameter projection. Disable logging and remove these
  files when they are no longer needed.
- **Network:** Chat and compression use Hermes's configured providers. Model
  inventory refreshes use Hermes's normal provider/catalog sources. Those
  operations can make network requests and incur provider charges. The plugin
  has no independent analytics endpoint, telemetry exporter, or usage-reporting
  service.
- **Credentials:** Provider authentication stays with Hermes's configured
  runtime. The plugin does not independently read browser profiles or another
  CLI's login files, rotate another client's OAuth tokens, or impersonate a
  vendor client. It stores no separate credentials.
- **Execution:** The plugin starts no shell children, installer, listener, or
  daemon. It adds routes to the existing gateway and runs at most four native
  command jobs concurrently. Agent tools can execute commands according to
  Hermes's configuration and approval policy. The plugin does not auto-approve
  tools, disable guards, or enable YOLO mode. Clarification waits use Hermes's
  timeout and stop handling; registration and observer hooks do not prompt or
  launch interactive login flows.

To enable request logging deliberately, set this in the relevant Hermes
profile's configuration, then follow Hermes's normal configuration reload flow:

```yaml
plugins:
  entries:
    talaria-webui:
      settings:
        debug_requests: true
```

## Dependencies and validation

The only additional Python dependency is `aiohttp>=3.9,<4`. Its floor covers the
HTTP APIs used here and its upper bound excludes the next major release. Hermes
resolves it under its core constraints. Development uses reviewed lockfiles.
Catalog CI separately resolves the plugin requirements with an explicit 14-day
release cutoff and saves the versions and hashes for review. Hermes admission
then resolves under its own constraints and security exceptions; it does not
inherit a plugin CI environment variable. No dependency is downloaded during
plugin registration.

From a checkout at the proposed catalog commit, run the admission command:

```sh
hermes plugins validate src/talaria/hermes_plugin --install-deps
```

Read both the checks and stderr: dependency preparation must succeed, even if
the CLI exits zero. Warnings require review before submission. The repository's
[catalog preparation command](../../../CONTRIBUTING.md#plugin-catalog) validates
an exported commit and generates the entry with matching code and image pins.
