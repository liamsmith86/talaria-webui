# Contributing

## Setup

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js 24 and npm 12+.

```sh
uv sync --locked
npm ci
uv run --locked playwright install --with-deps chromium firefox webkit
git config --local core.hooksPath .githooks
```

Native Hermes tests and the full suite also need Hermes and axe-core:

```sh
git clone --depth 1 --branch v0.22.5 https://github.com/NousResearch/hermes-agent.git .local/hermes
UV_PROJECT_ENVIRONMENT=$PWD/.local/hermes/venv uv sync --project .local/hermes --frozen --no-dev --extra sms
git config --local talaria.hermesSource "$PWD/.local/hermes"  # or export HERMES_SOURCE
curl -fsSL https://cdn.jsdelivr.net/npm/axe-core@4.13.0/axe.min.js -o .local/axe.min.js
```

`uv run --locked talaria demo` serves sample chats at http://127.0.0.1:8768.

## Checks

```sh
uv run --locked python contrib/check.py check --changed
TALARIA_AXE_PATH=$PWD/.local/axe.min.js uv run --locked python contrib/check.py full
```

The first command checks only what your changes affect. The Git hooks also run
checks on each commit and push. Every bug fix needs a test that fails without
the fix. To confirm that it does, run
`uv run --locked python contrib/check.py prove --base origin/main TEST...`.
Keep real sessions and credentials out of tests. Don't bypass the lockfiles or
the 7-day dependency cooldown.

## End-to-end tests

`e2e/` runs Talaria against a real Hermes gateway and model. It isn't in CI. The
model runs real commands, so use an account with no access to your files or
credentials.

```sh
TALARIA_E2E_HERMES=/path/to/hermes-agent OPENROUTER_API_KEY=... uv run --locked pytest e2e -n 3
```

The default model, `z-ai/glm-5.3-flash`, costs a few cents per run. Set
`TALARIA_E2E_MODEL` to use another model. Set `TALARIA_E2E_ARTIFACTS` to keep
failure screenshots and the gateway log.

## Translations

Wrap UI text in `t`, `rich`, `msg` or `n`. Then run
`uv run --locked node contrib/i18n.mjs --extract` and translate the new keys in
`src/talaria/static/locales/`. Keep `{placeholders}`. Translations contain no
HTML, so use `rich` placeholders for links and code. Don't translate Hermes
messages, commands, model names or paths. Register new languages in `i18n.js`.

## Releases

1. Update the version in `pyproject.toml` and `src/talaria/__init__.py`.
2. Run `uv lock` and merge to `main`.
3. Publish a GitHub Release tagged `vX.Y.Z`. Mark prereleases, and never move a
   tag.

The release workflow runs the full checks and publishes the images. A stable
release also moves `stable` and `:latest`. Documentation changes don't need a
release.

## Plugin catalog

The plugin in `src/talaria/hermes_plugin` has its own version in `plugin.yaml`.
Keep its [data disclosures](src/talaria/hermes_plugin/README.md#data-and-permissions)
accurate. To prepare a catalog submission for `HEAD`:

```sh
git clone https://github.com/NousResearch/hermes-agent.git .local/hermes-catalog
git -C .local/hermes-catalog checkout bf0203a794a07aac3ff764972ab9087baa4e4f4e  # v0.22.5
UV_EXCLUDE_NEWER="14 days" uv sync --project .local/hermes-catalog --frozen --no-dev --extra sms --python 3.14
.local/hermes-catalog/.venv/bin/python contrib/plugin_catalog.py --revision HEAD --output .local/plugin-catalog
```

Review the files in `.local/plugin-catalog`. Once the commit is public, a
maintainer submits `talaria-webui.yaml` to Hermes as
`plugin-catalog/talaria-webui.yaml`, following the [submission guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission).
Every update needs a new plugin version, pinned to a full commit SHA.
