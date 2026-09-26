#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# EvoTrader — Launch Script
# ──────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
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
EvoTrader CLI Tool

Usage:
  ./run.sh [command/flag] [arguments]

Commands/Flags:
  (default)            Launch the premium interactive Web Console displaying real-time
                       performance, live logs, indicator charts, position alignment,
                       and manual trade approval gates.
                       Accepts:
                         --mode {live,sim}
                                      Select execution mode. 'sim' does paper trading
                                      and routes database to evotrader_sim.db.
                                      'live' routes to evotrader.db and places orders.
                                      (Default: falls back to mode in settings.yaml)
                         --mock-time [ISO]
                                      Simulate a custom system time. If passed without
                                      value, defaults to Wednesday 10:00 AM ET.
                                      (Forces sim mode for safety)
                         --sim-deposit USD
                                      Deposit simulated funds (USD) into the sim account
                                      and exit.
                       Example: ./run.sh --mock-time

  offline, --offline, --cron
                       Run a single one-off offline trading cycle and exit. Useful for cron jobs.
                       Logs output to <data folder>/logs/cycle.log.
                       Accepts:
                         --mode {live,sim}
                         --mock-time [ISO] (forces sim mode)
                       Example: ./run.sh offline --mock-time

  cli-dashboard        Launch the legacy static terminal dashboard.
                       Example: ./run.sh cli-dashboard

  setup                Answer a few questions (AI provider, keys, console password)
                       and save them to your data folder. Safe to run again.
                       Example: ./run.sh setup

  -i, --init, init     Initialise the data directory structure, default config files,
                       and seed algorithms. Safe to run multiple times.
                       Example: ./run.sh --init

  -h, --help, help     Show this help documentation.

Safety Rules:
  1. Live trading mode is strictly blocked when faking the clock (--mode live + --mock-time).
  2. Setting mock time (--mock-time) always forces simulation mode ('sim').
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
            # Keep web-dashboard as default, let Python parse other flags (e.g. --sim)
            COMMAND="web-dashboard"
            ;;
    esac
fi

# Execute corresponding command
case "$COMMAND" in
    web-dashboard)
        echo "🌐 Launching Premium Web Dashboard..."
        echo "   Open your browser at http://127.0.0.1:8080"
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

