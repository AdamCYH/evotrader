#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# EvoTrader — Launch Script
# ──────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# A data folder you name is relative to where you are, not to this folder:
# make it absolute before moving here.
CALLER_DIR="$(pwd)"
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

cd "$SCRIPT_DIR"

# Determine execution environment
RUN_CMD=""
if command -v uv >/dev/null 2>&1; then
    RUN_CMD="uv run python"
else
    if [ ! -d ".venv" ]; then
        echo "❌ Virtual environment not found and 'uv' is not installed."
        echo "Please install 'uv' or run: python3 -m venv .venv && source .venv/bin/activate && pip install -e ."
        exit 1
    fi
    source .venv/bin/activate
    RUN_CMD="python"
fi

# Help message
show_help() {
    cat << 'EOF'
EvoTrader

Usage:
  ./run.sh [command] [options]

Commands:
  (none)             Start the web console. It prints its address (by default
                     http://127.0.0.1:8080). Options:
                       --mode live|sim     trade for real, or practice with play
                                           money (sim). Default: settings.yaml.
                       --mock-time [ISO]   pretend it is this time (default: a
                                           Wednesday, 10:00 New York); always practice.
                       --sim-deposit USD   add play money to the practice account,
                                           then exit.
                       --data-dir FOLDER   use this data folder (settings, keys,
                                           journal) instead of ./data.
                     Example: ./run.sh --mode sim

  setup              Answer a few questions (AI provider, keys, console
                     password) and save them. Safe to run again. Accepts --data-dir.

  offline            Run one trading cycle without the console, then exit (for
                     cron). Logs to <data folder>/logs/cycle.log.
                     Accepts --mode, --mock-time and --data-dir.
                     Example: ./run.sh offline --mock-time

  cli-dashboard      A summary of the journal, in the terminal.

  init               Create the data folder from the starter data without asking
                     anything (setup does this for you). Accepts --data-dir.

  help               Show this help.

Settings from the environment (or put them in <data folder>/.env):
  EVOTRADER_PORT=8081       the console's port (default 8080)
  EVOTRADER_HOST=0.0.0.0    reach the console from other devices (needs a password)
  EVOTRADER_DATA_DIR=FOLDER the data folder, like --data-dir
  Example: EVOTRADER_PORT=8081 ./run.sh
  All of them: docs/settings_reference.md

Safety rules:
  1. --mock-time never trades for real: it forces practice mode, and
     --mode live with --mock-time is refused.
  2. An option it doesn't know stops it, so a typo can't start the wrong mode.
EOF
}

# Parse subcommand or flag
COMMAND="web-dashboard"
if [ $# -gt 0 ]; then
    case "$1" in
        -h|--help|help)
            show_help
            exit 0
            ;;
        offline|--offline|--cron)
            COMMAND="run"
            shift
            ;;
        cli-dashboard)
            COMMAND="cli-dashboard"
            shift
            ;;
        -i|--init|init)
            COMMAND="init"
            shift
            ;;
        setup)
            COMMAND="setup"
            shift
            ;;
        -d|--dashboard|dashboard)
            COMMAND="web-dashboard"
            shift
            ;;
        *)
            # The console is the default; the app checks the options itself.
            COMMAND="web-dashboard"
            ;;
    esac
fi

# Execute corresponding command
case "$COMMAND" in
    web-dashboard)
        # The app prints the console's address once it knows the port.
        $RUN_CMD -m evotrader --dashboard "$@"
        ;;
    cli-dashboard)
        echo "📊 Launching Terminal Dashboard..."
        $RUN_CMD scripts/dashboard.py "$@"
        ;;
    init)
        echo "⚙️  Initialising Data Directory..."
        $RUN_CMD scripts/init_data.py "$@"
        ;;
    setup)
        $RUN_CMD -m evotrader.onboarding "$@"
        ;;
    run)
        # The log goes into the data folder this run uses: --data-dir wins over
        # EVOTRADER_DATA_DIR, as it does in the app.
        DATA_FOLDER="${EVOTRADER_DATA_DIR:-$SCRIPT_DIR/data}"
        prev=""
        for arg in "$@"; do
            if [ "$prev" = "--data-dir" ]; then DATA_FOLDER="$arg"; fi
            case "$arg" in --data-dir=*) DATA_FOLDER="${arg#--data-dir=}" ;; esac
            prev="$arg"
        done
        LOG_DIR="$DATA_FOLDER/logs"
        mkdir -p "$LOG_DIR"
        echo ""
        echo "⛏️  Starting EvoTrader Trading Cycle..."
        echo "   Logs: $LOG_DIR/cycle.log"
        echo ""
        # Run and tee logs
        $RUN_CMD -m evotrader "$@" 2>&1 | tee "$LOG_DIR/cycle.log"
        ;;
esac

