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
#   scripts/setup.sh --data-dir ~/my-data
#   EVOTRADER_DATA_DIR=~/my-data scripts/setup.sh
# ──────────────────────────────────────────────────────────────
set -euo pipefail

# A data folder you name is relative to where you are, not to the project:
# make it absolute before moving there.
CALLER_DIR="$(pwd)"
export EVOTRADER_CALLER_DIR="$CALLER_DIR"  # for advice that names run.sh
absolute() { case "$1" in /*|"~"*) printf '%s' "$1" ;; *) printf '%s/%s' "$CALLER_DIR" "$1" ;; esac; }
if [ -n "${EVOTRADER_DATA_DIR:-}" ]; then
    EVOTRADER_DATA_DIR="$(absolute "$EVOTRADER_DATA_DIR")"
    export EVOTRADER_DATA_DIR
fi
ARGS=()
prev=""
for arg in "$@"; do
    if [ "$prev" = "--data-dir" ]; then arg="$(absolute "$arg")"; fi
    case "$arg" in --data-dir=*) arg="--data-dir=$(absolute "${arg#--data-dir=}")" ;; esac
    ARGS+=("$arg")
    prev="$arg"
done
set -- ${ARGS[@]+"${ARGS[@]}"}

cd "$(dirname "$0")/.."

# Colour for a person at a terminal; plain text in a pipe or a log, or with
# NO_COLOR set (https://no-color.org).
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-}" != "dumb" ]; then
    BOLD=$'\033[1m' DIM=$'\033[2m' GREEN=$'\033[1;32m' RED=$'\033[31m' CYAN=$'\033[1;36m'
    RESET=$'\033[0m'
else
    BOLD="" DIM="" GREEN="" RED="" CYAN="" RESET=""
fi
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
fail() { printf '%s✗ %s%s\n' "$RED" "$*" "$RESET"; }
busy() { printf '%s… %s%s\n' "$DIM" "$*" "$RESET"; }
skip() { printf '%s· %s%s\n' "$DIM" "$*" "$RESET"; }

say "${CYAN}EvoTrader — first-time setup${RESET}"
say ""

# uv manages Python and the dependencies (and installs Python 3.12 if needed).
if ! command -v uv >/dev/null 2>&1; then
    fail "uv is not installed. It installs Python and everything else for you."
    say "  Install it from ${BOLD}https://docs.astral.sh/uv/getting-started/installation/${RESET}"
    say "  then open a new terminal and run scripts/setup.sh again."
    exit 1
fi
ok "uv $(uv --version | awk '{print $2}')"

busy "installing dependencies (the first time takes a minute or two)"
uv sync --all-extras --quiet
ok "dependencies installed"

# Optional tools: say what they are for, never block on them.
if command -v node >/dev/null 2>&1; then
    ok "node — used by the JavaScript tests"
else
    skip "node not found — optional, only the JavaScript tests need it"
fi
if command -v claude >/dev/null 2>&1; then
    ok "Claude Code — agents can run on a Claude subscription"
else
    skip "Claude Code not found — optional, lets two agents run on a Claude subscription"
fi

say ""
exec uv run python -m evotrader.onboarding "$@"
