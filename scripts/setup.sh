#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# EvoTrader — first-time setup
#
# Checks the tools, installs the dependencies, creates your data folder from
# the starter data, then asks a few questions (AI provider, keys, console
# password). Safe to run again: it only installs what is missing, and the
# questions keep every answer you don't change.
#
#   scripts/setup.sh                     data folder: ./data
#   EVOTRADER_DATA_DIR=~/my-data scripts/setup.sh
# ──────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")/.."

say() { printf '%s\n' "$*"; }

say "EvoTrader — first-time setup"
say ""

# uv manages Python and the dependencies (and installs Python 3.12 if needed).
if ! command -v uv >/dev/null 2>&1; then
    say "✗ uv is not installed. It installs Python and everything else for you."
    say "  Install it from https://docs.astral.sh/uv/getting-started/installation/"
    say "  then open a new terminal and run scripts/setup.sh again."
    exit 1
fi
say "✓ uv $(uv --version | awk '{print $2}')"

say "… installing dependencies (the first time takes a minute or two)"
uv sync --all-extras --quiet
say "✓ dependencies installed"

# Optional tools: say what they are for, never block on them.
if command -v node >/dev/null 2>&1; then
    say "✓ node — used by the JavaScript tests"
else
    say "· node not found — optional, only the JavaScript tests need it"
fi
if command -v claude >/dev/null 2>&1; then
    say "✓ Claude Code — agents can run on a Claude subscription"
fi

say ""
exec uv run python -m evotrader.onboarding
