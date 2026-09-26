---
name: fresh-start
description: >
  Reset EvoTrader for a new user. Clears all account-specific data
  (trades, metrics, sessions, broker tokens, agent memory) while
  preserving algorithm configs, instructions, constitution, and
  evolution history. Use when forking the repo for a different
  brokerage account.
---

# Fresh Start — Reset for a New User

This skill resets EvoTrader to a clean state for a new user while
preserving the algorithm tuning and instructions that took time to
develop. Think of it as "fork for a fresh brokerage account."

## When to Use

- A new person clones/copies the project and wants to run it on their
  own brokerage account.
- You want to wipe all trading history and start tracking P&L from
  scratch without losing algorithm configurations.

## What Gets Reset vs. Preserved

### 🗑️ RESET (Account-specific data)

| Category | Path(s) | Description |
|----------|---------|-------------|
| **Trade database** | `data/evotrader.db`, `data/evotrader.db-shm`, `data/evotrader.db-wal` | All trades, daily metrics, pending orders, agent thoughts, cycle runs, market snapshots, cash adjustments, evolution log entries, audit log, config snapshots |
| **Sim database** | `data/evotrader_sim.db`, `data/sim/db/evotrader_sim.db` | Paper-trading history |
| **Telemetry database** | `data/telemetry.db`, `data/sim/db/telemetry.db` | OpenTelemetry LLM call traces |
| **Agent memory** | `data/memory/` (ChromaDB: `chroma.sqlite3` + collection dirs) | Semantic embeddings of trade experiences and market patterns |
| **Sim memory** | `data/sim/memory/` | Sim-mode semantic memory |
| **Session state** | `data/sessions/`, `data/sim/sessions/` | Agent cycle session artifacts |
| **OAuth tokens** | `~/.evotrader/oauth/` | Cached Robinhood/Schwab OAuth tokens and client registration. **Must be cleared** so the new user can authenticate with their own account |
| **Memory backup** | `data/memory_backup/` | Backup copies of ChromaDB |

### ✅ PRESERVE (Shared intellectual property)

| Category | Path(s) | Description |
|----------|---------|-------------|
| **Algorithm configs** | `data/algorithms/` (all versions, `active.yaml`, `strategy_manifest.yaml`) | Tuned parameters and regime weights |
| **Instructions** | `data/instructions/` | Agent prompt templates (orchestrator, strategy, execution, risk, evolution) |
| **Constitution** | `data/constitution.yaml` | Immutable risk rails |
| **Settings** | `data/settings.yaml` | Schedule, MCP config, risk limits, dry-run config |
| **Evolution proposals** | `data/evolution/proposals/`, `data/evolution/reviews/` | Algorithm improvement proposals and code reviews |
| **Notes** | `data/notes/` | User research notes |
| **Skills** | `skills/` | All skill definitions (including this one) |
| **Source code** | `src/` | Application code and migrations |
| **DB migrations** | `src/evotrader/db/migrations/` | Schema definitions — the app auto-applies these on first run to create a fresh database |

## Execution Steps

> [!CAUTION]
> **Stop the application before running this.** If the web server or
> trading loop is active, the SQLite databases will be locked and
> deletion will fail or corrupt data.

### Step 1: Confirm the app is stopped

```bash
# Check for running processes
ps aux | grep evotrader | grep -v grep
# If any are running, stop them first (Ctrl+C or kill the process)
```

### Step 2: Run the reset script

From the project root directory, execute:

```bash
#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$PROJECT_ROOT/data"

echo "🧹 EvoTrader — Fresh Start Reset"
echo "==================================="
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
read -p "Are you sure? Type 'RESET' to confirm: " confirm
if [ "$confirm" != "RESET" ]; then
    echo "Aborted."
    exit 1
fi

echo ""

# ── 1. Remove trade/metrics databases ──
echo "🗄️  Removing databases..."
rm -f "$DATA_DIR/evotrader.db"
rm -f "$DATA_DIR/evotrader.db-shm"
rm -f "$DATA_DIR/evotrader.db-wal"
rm -f "$DATA_DIR/evotrader_sim.db"

# Sim-mode databases
rm -f "$DATA_DIR/sim/db/evotrader.db"
rm -f "$DATA_DIR/sim/db/evotrader_sim.db"
rm -f "$DATA_DIR/sim/db/evotrader.db-shm"
rm -f "$DATA_DIR/sim/db/evotrader.db-wal"
rm -f "$DATA_DIR/sim/db/evotrader_sim.db-shm"
rm -f "$DATA_DIR/sim/db/evotrader_sim.db-wal"

# Telemetry databases
rm -f "$DATA_DIR/telemetry.db"
rm -f "$DATA_DIR/db/telemetry.db"
rm -f "$DATA_DIR/sim/db/telemetry.db"

echo "  ✓ Databases removed"

# ── 2. Remove agent memory (ChromaDB) ──
echo "🧠  Removing agent memory..."
rm -rf "$DATA_DIR/memory/"
rm -rf "$DATA_DIR/sim/memory/"
rm -rf "$DATA_DIR/memory_backup/"
echo "  ✓ Agent memory cleared"

# ── 3. Remove session state ──
echo "📋  Removing session state..."
rm -rf "$DATA_DIR/sessions/"
rm -rf "$DATA_DIR/sim/sessions/"
echo "  ✓ Sessions cleared"

# ── 4. Remove OAuth tokens ──
echo "🔑  Removing OAuth tokens..."
rm -rf "$HOME/.evotrader/oauth/"
echo "  ✓ OAuth tokens cleared (you will re-authenticate on first run)"

# ── 5. Recreate empty directories ──
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
echo "  2. Run the app: uv run python -m evotrader"
echo "  3. You will be prompted to authenticate with your brokerage"
echo "  4. Add your initial deposit via the dashboard (Settings → Cash Adjustments)"
echo "     so the Account Value chart has a correct starting point"
```

### Step 3: Post-reset checklist

After the reset completes, the new user should:

1. **Review `data/settings.yaml`**
   - Update `asset.primary_ticker` if trading a different ETF/stock
   - Confirm the `schedule` section matches their desired trading cadence
   - Verify `mcp.providers` section has the correct broker endpoint

2. **Start the application**
   ```bash
   uv run python -m evotrader
   ```
   The app will automatically:
   - Create a fresh `evotrader.db` with all migration tables
   - Initialize empty ChromaDB collections
   - Prompt for OAuth authentication with the broker

3. **Register initial deposit**
   - Open the dashboard and navigate to **Settings → Cash Adjustments**
   - Add a cash adjustment for the starting account balance
   - This establishes the baseline for the Account Value chart

4. **Verify broker connection**
   - The dashboard should show the broker's account value, cash, and
     buying power in the header cards
   - If OAuth fails, click the **Reconnect Broker** button

## Manual Reset (Alternative)

If you prefer to run the steps manually instead of using the script:

```bash
# From the project root:

# 1. Delete databases
rm -f data/evotrader.db data/evotrader.db-shm data/evotrader.db-wal
rm -f data/evotrader_sim.db
rm -f data/telemetry.db data/db/telemetry.db
rm -f data/sim/db/*.db data/sim/db/*.db-shm data/sim/db/*.db-wal

# 2. Clear memory
rm -rf data/memory/ data/sim/memory/ data/memory_backup/

# 3. Clear sessions
rm -rf data/sessions/ data/sim/sessions/

# 4. Clear OAuth (forces re-authentication)
rm -rf ~/.evotrader/oauth/

# 5. Recreate directories
mkdir -p data/{db,sessions,memory} data/sim/{db,sessions,memory}
```

## Partial Reset Options

### Reset only trade history (keep memory & tokens)

Use this if you just want to clear the P&L ledger but keep the agent's
learned patterns and broker authentication:

```bash
rm -f data/evotrader.db data/evotrader.db-shm data/evotrader.db-wal
rm -f data/telemetry.db
```

### Reset only OAuth (re-authenticate broker)

Use this when switching to a different brokerage account on the same
machine:

```bash
rm -rf ~/.evotrader/oauth/
```

### Reset only agent memory (forget trade patterns)

Use this if the agent has learned patterns from a different trading
style and you want it to start learning fresh:

```bash
rm -rf data/memory/ data/memory_backup/
```

## Technical Notes

- **Database recreation**: The app uses SQLite with a migration system
  (`src/evotrader/db/migrations/`). On startup, if the database file
  doesn't exist, it runs all migrations to create the schema from
  scratch. No manual schema setup is needed.

- **ChromaDB**: The semantic memory is backed by ChromaDB with
  `PersistentClient`. Collections (`trade_experiences`,
  `market_patterns`, `user_notes`) are auto-created on first access.

- **OAuth flow**: On first run after clearing tokens, the app opens a
  browser window for OAuth authentication. The web dashboard also has
  a "Reconnect Broker" button for re-authentication.

- **Evolution history**: The `data/evolution/` directory contains
  proposals and code reviews as markdown files. These are intellectual
  property (algorithm improvements) and are preserved. The evolution
  *log* entries in the database (tracking which proposals were
  accepted/rejected) are cleared with the database, but the proposal
  files remain.

- **Idempotent**: Running the reset script multiple times is safe —
  `rm -f` and `rm -rf` don't fail on missing files.
