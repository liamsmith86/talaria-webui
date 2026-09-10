#!/usr/bin/env bash
set -euo pipefail
apt-get update -qq
apt-get install -y -qq python3 python3-venv git curl ca-certificates
mkdir -p /tmp/talaria-ci
cp -a "$1/." /tmp/talaria-ci/
cd /tmp/talaria-ci
# Pin the bootstrap tool; project dependencies still observe the seven-day cooldown.
curl -fsSL https://astral.sh/uv/0.11.17/install.sh -o /tmp/install-uv.sh
sh /tmp/install-uv.sh
export PATH="/root/.local/bin:$PATH"
uv sync --locked --python 3.12
uv run --locked pytest -q -m 'not browser and not hermes' --fail-on-skip
