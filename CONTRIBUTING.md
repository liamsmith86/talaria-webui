# Contributing

Use Python 3.12+, uv, Git, Node.js 24 and npm 12+.

## Setup

```sh
uv sync --locked
npm ci
uv run --locked playwright install --with-deps chromium firefox webkit
git config --local core.hooksPath .githooks
```

If you already use Git hooks, integrate ours with them. Node and npm are only
needed for development; the application has no JavaScript build step. Keep the
lockfiles and seven-day dependency cooldown in place.

For a local preview with sample chats:

```sh
uv run --locked talaria demo
```

Open `http://127.0.0.1:8768`. The demo uses fictional sessions and placeholder
replies. Visitor input stays in the browser tab; refreshing resets it.

## Checks and pull requests

Run the checks selected for your changes:

```sh
uv run --locked python contrib/check.py check --changed
```

This compares your work with `origin/main`, including uncommitted files. Use
`--base REF` for another comparison. Git hooks check staged files before a
commit and the committed revision before a push.

For a bug fix, add a test that reproduces the failure. The `prove` command can
check that the test fails on the old revision and passes on the new one; see
`uv run --locked python contrib/check.py prove --help`. In the PR, describe the
change and the checks you ran.

The full suite is `uv run --locked python contrib/check.py full`. It also needs
`HERMES_SOURCE` pointing to a separate Hermes checkout with `venv/bin/python3`,
and `TALARIA_AXE_PATH` pointing to the verified axe-core script. The
[CI workflow](.github/workflows/ci.yml) has the exact setup and checksum. Native
Hermes tests use temporary profiles and a local model simulator; keep production
sessions and credentials out of tests.

## Translations

Wrap English UI text with `t`, `rich`, `msg` or `n`, then extract it:

```sh
uv run --locked node contrib/i18n.mjs --extract
```

Edit the values in `src/talaria/static/locales/LOCALE.json`, keeping English keys
and placeholders such as `{name}`. Use `rich` placeholders for links and code;
translation files contain no HTML. Leave Hermes messages, commands, model names
and paths unchanged.

Register new languages in `i18n.js`. Run `uv run --locked node contrib/i18n.mjs`
and the changed-file checks, then check the layout on a small screen.

## Releases

Update the version in `pyproject.toml` and `src/talaria/__init__.py`, run
`uv lock`, and merge to `main`. Publish a GitHub Release with the matching tag;
mark prereleases as such and never move an existing tag.

The [release workflow](.github/workflows/release.yml) runs the full checks and
publishes container images. A successful stable release advances `stable` and
`:latest`, which are used by normal installs and updates. Documentation-only
changes do not need a release.

## Plugin catalog

The plugin lives in `src/talaria/hermes_plugin` and has its own version in
`plugin.yaml`. Keep its manifest and [data disclosures](src/talaria/hermes_plugin/README.md#data-and-permissions)
in sync with its behavior. Follow the
[Hermes submission guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission).

Commit the changes first. From the repository root, prepare the pinned validator
used by CI and generate a submission:

```sh
git clone https://github.com/NousResearch/hermes-agent.git .local/hermes-catalog
git -C .local/hermes-catalog checkout 4ec2e50b3cff8c3a1cf43c8675fa2c25d07bde49
UV_EXCLUDE_NEWER="14 days" uv sync --project .local/hermes-catalog --frozen --no-dev --extra sms --python 3.14
.local/hermes-catalog/.venv/bin/python contrib/plugin_catalog.py --revision HEAD --output .local/plugin-catalog
```

This validates the committed plugin, checks its dependencies against a 14-day
release cutoff, and writes the catalog entry, validation report, resolved
dependencies and PR description to `.local/plugin-catalog`. Review those files;
the command does not submit them.

The repository owner or a major contributor submits `talaria-webui.yaml` to
Hermes as `plugin-catalog/talaria-webui.yaml` once the pinned commit is public.
Code, documentation and images must use the same full commit SHA. Each catalog
update needs a new plugin version, pin and PR for Hermes maintainers to review.
