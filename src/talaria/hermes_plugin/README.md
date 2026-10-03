# talaria-webui

An optional plugin for [Talaria WebUI](../../../README.md). It adds response and
context details, profile instructions and prefill, history search, session
controls, and live approval and clarification prompts to Hermes's API.

## Install

Requires Hermes **0.21.5 or later**, Python 3.12+, and Hermes's API server with an
API key. Follow the [plugin installation instructions](../../../README.md#hermes-plugin).

Enable the plugin in each profile you want to use. For a gateway serving several
profiles, also enable it in the primary profile. Restart the gateway after
active work finishes. If an agent is installing it through that gateway, restart
from a separate terminal after the installation session ends.

For installations from the Hermes catalog, update with
`hermes plugins update talaria-webui`. Catalog versions are pinned to a full
commit SHA and reviewed by Hermes maintainers. The plugin has no self-updater;
Talaria's standalone updater also leaves Hermes-managed plugin installations
alone.

## Data and permissions

- **Profile data:** Reads the enabled profile's configuration, session history
  and search index, model inventory, `IDENTITY.md`, `SOUL.md`, and configured
  instructions and prefill. A configured prefill file can be outside the profile
  directory. Instructions and prefill are sent to the configured model;
  status responses omit their contents and file paths.
- **Session changes:** Authenticated actions can branch, rewind or compress API
  sessions. Search includes other sessions visible within the authenticated
  profile. Session changes use Hermes's storage and active-session protections.
- **Local storage:** Keeps up to 10,000 response metadata records in
  `$HERMES_HOME/talaria/observations.db`: identifiers, model names, token counts,
  timings and completion status. These records contain no message bodies or
  credentials. Disabling the plugin leaves this file in place.
- **Network:** Chat and compression use Hermes's configured providers; model
  inventory refreshes use its provider and catalog sources. These requests may
  incur provider charges. The plugin adds no analytics or telemetry.
- **Credentials:** Authentication stays with Hermes. The plugin stores no
  separate credentials and does not read other applications' login files or
  manage their OAuth tokens.
- **Execution:** Runs inside the existing gateway, including background jobs for
  manual commands. It starts no shell subprocesses or separate daemon. Agent
  tools can run commands under Hermes's approval policy. Clarification prompts
  use Hermes's timeouts and cancellation.

## Integration and validation

The plugin registers an `api_server` platform handler and the three hooks listed
in [plugin.yaml](plugin.yaml). It registers no tools or middleware and does not
replace Hermes core methods. Manual `/compress` calls Hermes's own compression
implementation.

Its only additional Python dependency is `aiohttp>=3.9,<4`, resolved within
Hermes's dependency constraints. Catalog CI checks a 14-day dependency release
cutoff separately; this does not change Hermes's install-time dependency policy.

From the repository root:

```sh
hermes plugins validate src/talaria/hermes_plugin --install-deps
```

Check the report and any dependency errors printed to stderr. Our
[catalog preparation command](../../../CONTRIBUTING.md#plugin-catalog) requires
all admission checks to pass, a `safe` scan, successful dependency preparation
and no warnings.

This directory contains the plugin; the WebUI is installed separately. Talaria
is an independent project maintained by [Liam Smith](https://github.com/liamsmith86)
under the [MIT license](../../../LICENSE).
