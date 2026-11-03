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
curl -fsSL https://cdn.jsdelivr.net/npm/axe-core@4.14.0/axe.min.js -o .local/axe.min.js
```

`uv run --locked talaria demo` serves sample chats at http://127.0.0.1:8768.

After updating Python dependencies with `uv sync`, restart a running `talaria --dev`
process. Its reloader keeps imported libraries in the parent process; restarting
only its worker can mix old configuration objects with new library code.

## Checks

```sh
uv run --locked python contrib/check.py check --changed
uv run --locked python contrib/check.py full --revision HEAD
```

The first command checks only what your changes affect. The second runs every
check on a clean copy of a commit; without `--revision` it checks your working
tree. The Git hooks also run checks on each commit and push. A push reuses
passing checks of the same code from the last hour, so after a full check it
reruns at most the native Hermes tests.

Commit subjects and pull request titles follow
[Conventional Commits](https://www.conventionalcommits.org/), such as
`fix(settings): refresh agent details quietly`. The `commit-msg` hook rejects
any other subject.

Releases run every browser test on Chromium and those listed in `CROSS_BROWSER` in `tests/conftest.py`
on Firefox and WebKit; add a file there if its behavior can differ between
browsers. Every browser test runs on every browser nightly.

A change doesn't need a test of its own. Add one only for behavior that could
plausibly break again and that nothing else covers, and delete tests that don't
meet that bar. Keep real sessions and credentials out of tests. Don't bypass the lockfiles or
the 7-day dependency cooldown.

`tests/markdown_stream_fuzz.mjs` checks streamed Markdown against a full parse. Pull requests repeat
one seed and a nightly job tries a new one. To reproduce a failure, pass its seed:
`node tests/markdown_stream_fuzz.mjs 100000 <seed>`.

Dependabot updates the Python and npm lockfiles, GitHub Actions and the Docker base image weekly.
It also tracks the browser libraries in `src/talaria/static/vendor/` through
`contrib/vendor/package-lock.json`. On one of those pull requests, run
`uv run --locked python contrib/vendor.py` to bring in the files, or lint fails. Prism is a custom
bundle, so a new version has to be rebuilt by hand.

Every dependency and tool is pinned to an exact version. The build requirements, and the uv, npm,
Node, Python and axe-core versions in `ci.yml`, `outdated.yml`, `install.sh`,
`.github/scripts/wsl.sh` and the `Dockerfile`, are updated by hand because Dependabot can't see them.

`contrib/outdated.py` reports anything behind its newest release that has cleared the cooldown. It
runs before a commit that changes `pyproject.toml`, `uv.lock`, the `Dockerfile` or an npm manifest,
and blocks the commit if it finds something. Without a network it warns and lets the commit through.
Weekly, `.github/workflows/outdated.yml` runs it with `--hand-kept` for the versions Dependabot
can't see, and keeps one issue open while any are behind. Pull requests fail if they add a
dependency with a known moderate or worse vulnerability. To stay on an older major version on
purpose, add it to `HELD` in that script.

## Frontend build

After changing frontend source, vendored assets or the build toolchain, regenerate
the production assets and include them in the same commit:

```sh
npm run build
npm run build:check
```

`contrib/build.mjs` compiles htm templates into Preact calls, bundles and minifies
JavaScript with esbuild and Terser, and combines the stylesheets. The production
document preloads the application. The small appearance script runs before
paint; Prism loads with the application. A separate boot script retains startup failure recovery.
Fonts, translations, syntax languages and application features stay intact.
Source maps and dependency notices accompany the generated files.

The generated files in `src/talaria/static/built/` are committed so installing
Talaria from a checkout, wheel or Docker image needs only Python. Node is a
development dependency. Packaging verifies every source and output hash and
rejects missing, modified or stale assets; lint rebuilds in memory and checks
byte-for-byte reproducibility, so a dependency update fails only if it changes the output. `talaria --dev` continues to serve source modules.
Normal application and demo launches use the production assets.

## Public demo

The public demo is the `talaria-demo` Cloudflare Pages project, connected to this
repository. Pushing to `main` deploys it, and pushing to `dev` builds a preview.
Pages runs `contrib/demo_export.py`, which writes the demo as static files: the API
responses are files, and `static/demo.js` answers everything else in the browser.
`_headers` carries the same security headers as `web.py`. The build command is set
in the Cloudflare dashboard.

## Performance

`contrib/perf.py` measures the real Talaria server and browser against deterministic
Hermes responses. Desktop and mobile profiles cover loading, streaming, typing,
scrolling and switching conversations. Server CPU and memory come from a separate
process, excluding Playwright and the fake Hermes. Linux reports current/peak RSS,
proportional and private memory; browser memory is reported separately.

```sh
uv run --locked python contrib/perf.py cold_load server_sessions switching stream_typical --repeat 3 --save .local/perf-base.json
uv run --locked python contrib/perf.py cold_load server_sessions switching stream_typical --repeat 3 --compare .local/perf-base.json
```

`server_sessions` opens five authenticated tabs with different conversations and
streams a reply with tools in each. It records memory before use, with tabs open,
after replies and after closing the tabs. `cold_load` distinguishes decoded bytes,
compressed body bytes and actual transfer including headers and the HTML document.
It also counts requests reaching the network, so cached content isn't counted as
a download. Durations are machine-dependent: compare medians on the same machine
and Python version, with other benchmarks stopped.

Add `--resources .local/perf-resources` to save per-resource byte counts and timings
for each cold/warm navigation, separated by scenario, profile and repetition.
The JSON records paths and sizes, without response bodies or credentials. Encoded
sizes also describe cached resources; use `transfer_bytes` to identify network
traffic. Chromium's Resource Timing transfer sizes include a fixed header estimate.

Wheels include gzip and Brotli variants of static text files, generated by
`hatch_build.py`. Encoding happens during packaging, with no encoder or payload
cache in the runtime. Benchmark an installed wheel to include production
compilation and compression:

```sh
UV_PROJECT_ENVIRONMENT=$PWD/.local/perf-runtime uv sync --locked --no-dev --no-editable --reinstall-package talaria-webui --python "$PWD/.venv/bin/python"
uv run --locked python contrib/perf.py cold_load --server-python .local/perf-runtime/bin/python
```

To inspect a running development server with its real Hermes connection, use
`live_load`. This browses up to five existing conversations without sending messages.
The local config signs a browser cookie; credentials and conversation content are
never included in the results. Pass the worker's PID, rather than the reloader's,
to include server memory:

```sh
uv run --locked python contrib/perf.py live_load --url http://127.0.0.1:8767 --config ~/.config/talaria-dev/config.json --dev --server-pid WORKER_PID --save .local/perf-live.json
```

Omit `--url` to launch a fresh server from a private snapshot of that config. This
uses the same real Hermes and supports `--server-python`, so installed versions
can be compared without replacing the running development server.

## Install walkthrough

`contrib/install_walkthrough.py` installs real Hermes with its own installer and real
Talaria with `install.sh` in a throwaway home, then signs in, runs a chat with a tool,
updates and uninstalls. It takes about a minute and needs no provider key. CI runs it for
changes to the install files, for releases, and against Hermes `main` each week.

```sh
uv run --locked python contrib/install_walkthrough.py [--hermes-ref main]
```

## End-to-end tests

`e2e/` runs Talaria against a real Hermes gateway. It isn't in CI. Tools run real
commands, so use an account with no access to your files or credentials.

```sh
TALARIA_E2E_HERMES=/path/to/hermes-agent OPENROUTER_API_KEY=... uv run --locked pytest e2e -n 3
```

`e2e/scripted/` uses a loopback model that follows the plan in each prompt, so a
test there can't fail on what a model chooses. It needs no provider key. Add tests
there. `e2e/test_real_model.py` keeps a few checks of a real provider, using
`z-ai/glm-5.3-flash` for a few cents per run. Set `TALARIA_E2E_MODEL` to use another
model. Set `TALARIA_E2E_ARTIFACTS` to keep failure screenshots and the gateway logs.

## Translations

Wrap UI text in `t`, `rich`, `msg` or `n`. Then run
`uv run --locked node contrib/i18n.mjs --extract` and translate the new keys in
`src/talaria/static/locales/`. Keep `{placeholders}`. Translations contain no
HTML, so use `rich` placeholders for links and code. Don't translate Hermes
messages, commands, model names or paths. Register new languages in `i18n.js`.

## Releases

1. Update the version in `pyproject.toml` and `src/talaria/__init__.py`.
2. Run `uv lock`, then the full checks on the branch:
   `gh workflow run ci.yml --ref dev -f full=true`. A failure here costs no
   version number.
3. Merge to `main` and publish a GitHub Release tagged `vX.Y.Z`. Mark
   prereleases, and never move a tag.

The release workflow publishes the images. It reuses a passing full run of the
same tree from the last 24 hours, and otherwise runs the full checks itself. A stable
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
