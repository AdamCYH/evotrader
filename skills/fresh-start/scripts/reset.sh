#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
# EvoTrader — Fresh Start Reset Script
# ═══════════════════════════════════════════════════════════════════
#
# Resets all account-specific data (trades, metrics, sessions, broker
# tokens, agent memory) while preserving algorithm configs,
# instructions, constitution, and evolution proposals.
#
# Usage:  ./skills/fresh-start/scripts/reset.sh
#
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

# Resolve project root (two levels up from scripts/ → fresh-start/ → skills/ → root)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
# The data folder the app uses: EVOTRADER_DATA_DIR if set, else data/.
DATA_DIR="${EVOTRADER_DATA_DIR:-$PROJECT_ROOT/data}"

# Verify we're in the right project
if [ ! -f "$PROJECT_ROOT/pyproject.toml" ]; then
    echo "❌ Error: Could not find pyproject.toml at $PROJECT_ROOT"
    echo "   Are you running this from the EvoTrader project?"
    exit 1
fi

echo ""
echo "🧹 EvoTrader — Fresh Start Reset"
echo "==================================="
echo ""
echo "Project root: $PROJECT_ROOT"
echo "Data folder:  $DATA_DIR"
echo ""
echo "This will DELETE all account-specific data:"
echo "  • Trade journals and metrics databases"
echo "  • Agent memory (ChromaDB)"
echo "  • Session state"
echo "  • OAuth tokens (you will need to re-authenticate)"
echo "  • Telemetry logs"
echo ""
echo "The following will be PRESERVED:"
echo "  • Algorithm configs and tuned parameters"
echo "  • Agent instructions and constitution"
echo "  • Evolution proposals and code reviews"
echo "  • Settings and notes"
echo ""

# Check for running processes
RUNNING=$(ps aux | grep "[g]old_digger" | grep -v "reset.sh" || true)
if [ -n "$RUNNING" ]; then
    echo "⚠️  WARNING: EvoTrader appears to be running:"
    echo "$RUNNING"
    echo ""
    echo "Please stop the application first to avoid database corruption."
    echo ""
    read -p "Continue anyway? (y/N): " force
    if [ "$force" != "y" ] && [ "$force" != "Y" ]; then
        echo "Aborted."
        exit 1
    fi
fi

read -p "Are you sure? Type 'RESET' to confirm: " confirm
if [ "$confirm" != "RESET" ]; then
    echo "Aborted."
    exit 1
fi

echo ""

# ── 1. Remove trade/metrics databases ──────────────────────────
echo "🗄️  Removing databases..."
rm -f "$DATA_DIR/evotrader.db"
rm -f "$DATA_DIR/evotrader.db-shm"
rm -f "$DATA_DIR/evotrader.db-wal"
rm -f "$DATA_DIR/evotrader_sim.db"
rm -f "$DATA_DIR/evotrader_sim.db-shm"
rm -f "$DATA_DIR/evotrader_sim.db-wal"

# Runtime db directory (used in some configurations)
rm -f "$DATA_DIR/db/"*.db 2>/dev/null || true
rm -f "$DATA_DIR/db/"*.db-shm 2>/dev/null || true
rm -f "$DATA_DIR/db/"*.db-wal 2>/dev/null || true

# Sim-mode databases
rm -f "$DATA_DIR/sim/db/"*.db 2>/dev/null || true
rm -f "$DATA_DIR/sim/db/"*.db-shm 2>/dev/null || true
rm -f "$DATA_DIR/sim/db/"*.db-wal 2>/dev/null || true

# Telemetry databases (can be in data/ or data/db/ or data/sim/db/)
rm -f "$DATA_DIR/telemetry.db"
rm -f "$DATA_DIR/db/telemetry.db"
rm -f "$DATA_DIR/sim/db/telemetry.db"
echo "  ✓ Databases removed"

# ── 2. Remove agent memory (ChromaDB) ─────────────────────────
echo "🧠  Removing agent memory..."
rm -rf "$DATA_DIR/memory/"
rm -rf "$DATA_DIR/sim/memory/"
rm -rf "$DATA_DIR/memory_backup/"
echo "  ✓ Agent memory cleared"

# ── 3. Remove session state ───────────────────────────────────
echo "📋  Removing session state..."
rm -rf "$DATA_DIR/sessions/"
rm -rf "$DATA_DIR/sim/sessions/"
echo "  ✓ Sessions cleared"

# ── 4. Remove OAuth tokens ───────────────────────────────────
echo "🔑  Removing OAuth tokens..."
rm -rf "$HOME/.evotrader/oauth/"
echo "  ✓ OAuth tokens cleared (you will re-authenticate on first run)"

# ── 5. Recreate empty directories ────────────────────────────
echo "📁  Recreating empty directories..."
mkdir -p "$DATA_DIR/db"
mkdir -p "$DATA_DIR/sessions"
mkdir -p "$DATA_DIR/memory"
mkdir -p "$DATA_DIR/sim/db"
mkdir -p "$DATA_DIR/sim/sessions"
mkdir -p "$DATA_DIR/sim/memory"
echo "  ✓ Directories ready"

echo ""
echo "✅ Fresh start complete!"
echo ""
echo "Next steps:"
echo "  1. Review data/settings.yaml — update primary_ticker if needed"
echo "  2. Run the app:  uv run python -m evotrader"
echo "  3. You will be prompted to authenticate with your brokerage"
echo "  4. Add your initial deposit via the dashboard"
echo "     (Settings → Cash Adjustments)"
echo "     so the Account Value chart has a correct starting point"
echo ""
