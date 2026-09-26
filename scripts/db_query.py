#!/usr/bin/env python3
"""EvoTrader — Generalized Database Querier.

A reusable script to query the system's SQLite databases (sim or live) from
the command line.

Usage:
    # List all tables in sim database
    python scripts/db_query.py --tables --sim

    # Show schema of the trades table
    python scripts/db_query.py --schema trades --sim

    # View recent evolution logs
    python scripts/db_query.py --evolution --sim

    # Run arbitrary SQL queries
    python scripts/db_query.py --sql "SELECT id, timestamp, direction, action, fill_price FROM trades LIMIT 5" --sim
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

# Resolve project root relative to this script
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent


def get_db_path(args) -> Path:
    """Resolve database path based on CLI arguments and configuration."""
    if args.db_path:
        return Path(args.db_path)

    # Try resolving via AppConfig
    try:
        sys_path = str(_PROJECT_ROOT)
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)

        from evotrader.config import AppConfig
        from evotrader.models.config import TradingMode

        mode = TradingMode.SIM if args.sim else TradingMode.LIVE
        config = AppConfig(project_root=_PROJECT_ROOT, mode_override=mode)
        return config.db_path
    except Exception:
        # Simple fallback paths, in the same data folder the app would use
        from evotrader import paths

        data = paths.data_dir(_PROJECT_ROOT)
        if args.sim:
            return data / "sim" / "db" / "evotrader_sim.db"
        return data / "db" / "evotrader.db"


def print_table(headers: list[str], rows: list[tuple]) -> None:
    """Format and print query results nicely aligned as a table."""
    if not rows:
        print("Empty result set.")
        return

    # Find max width for each column
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for idx, val in enumerate(row):
            str_val = str(val) if val is not None else ""
            if idx < len(widths):
                widths[idx] = max(widths[idx], len(str_val))

    # Format line and border
    fmt = " │ ".join(f"{{:<{w}}}" for w in widths)
    border = "═╪═".join("═" * w for w in widths)
    "─┼─".join("─" * w for w in widths)

    print(fmt.format(*headers))
    print(border)
    for row in rows:
        formatted_row = [str(val) if val is not None else "" for val in row]
        # Pad row to match number of columns
        if len(formatted_row) < len(headers):
            formatted_row.extend([""] * (len(headers) - len(formatted_row)))
        print(fmt.format(*formatted_row))
    print(f"\n({len(rows)} row(s) returned)")


def main() -> None:
    parser = argparse.ArgumentParser(description="EvoTrader — Generalized Database Querier")

    # Mode selection
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--sim", action="store_true", help="Query the simulation database (default)"
    )
    mode_group.add_argument(
        "--live", action="store_true", help="Query the live production database"
    )

    parser.add_argument(
        "-d", "--db-path", type=str, help="Explicit path to the SQLite database file"
    )

    # Query targets
    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument("-t", "--tables", action="store_true", help="List all tables")
    target_group.add_argument(
        "-s", "--schema", type=str, metavar="TABLE", help="Show CREATE TABLE schema for a table"
    )
    target_group.add_argument(
        "-q", "--sql", type=str, metavar="QUERY", help="Run a raw SQL SELECT query"
    )

    # Shortcuts
    target_group.add_argument(
        "--evolution", action="store_true", help="Shortcut: view recent evolution logs"
    )
    target_group.add_argument("--trades", action="store_true", help="Shortcut: view recent trades")
    target_group.add_argument(
        "--metrics", action="store_true", help="Shortcut: view recent daily metrics"
    )
    target_group.add_argument(
        "--sessions", action="store_true", help="Shortcut: view recent cycles/session runs"
    )

    # Formatting
    parser.add_argument("--json", action="store_true", help="Output results in JSON format")
    parser.add_argument(
        "--data-dir", help="Data folder to read (default: EVOTRADER_DATA_DIR, else ./data)"
    )
    parser.add_argument(
        "-l", "--limit", type=int, default=20, help="Row limit for shortcuts (default: 20)"
    )

    args = parser.parse_args()
    if args.data_dir:
        from evotrader import paths

        os.environ[paths.DATA_DIR_ENV] = str(Path(args.data_dir).expanduser().resolve())

    # If neither sim nor live specified, default to sim
    if not args.sim and not args.live:
        args.sim = True

    db_file = get_db_path(args)
    if not db_file.is_file():
        sys.exit(f"Error: Database file does not exist at {db_file}")

    print(f"Connecting to database: {db_file}\n")
    conn = sqlite3.connect(db_file)
    c = conn.cursor()

    try:
        query = ""
        params = ()

        if args.tables:
            query = "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        elif args.schema:
            query = "SELECT sql FROM sqlite_master WHERE type='table' AND name=?"
            params = (args.schema,)
        elif args.sql:
            query = args.sql
        elif args.evolution:
            query = f"SELECT timestamp, change_type, target_component, old_version, new_version, status FROM evolution_log ORDER BY timestamp DESC LIMIT {args.limit}"
        elif args.trades:
            query = f"SELECT id, timestamp, ticker, direction, action, quantity, price, fill_price, algo_version FROM trades ORDER BY timestamp DESC LIMIT {args.limit}"
        elif args.metrics:
            query = f"SELECT date, portfolio_value, cash_balance, daily_pnl, daily_return_pct, trades_count, algo_version FROM daily_metrics ORDER BY date DESC LIMIT {args.limit}"
        elif args.sessions:
            query = f"SELECT timestamp, session_id, cycle_type, status, error FROM cycle_runs ORDER BY timestamp DESC LIMIT {args.limit}"

        c.execute(query, params)
        rows = c.fetchall()
        headers = [d[0] for d in c.description] if c.description else []

        if args.json:
            results = []
            for r in rows:
                results.append(dict(zip(headers, r, strict=False)))
            print(json.dumps(results, indent=2))
        else:
            if args.schema and rows:
                print(rows[0][0])
            else:
                print_table(headers, rows)

    except Exception as e:
        print(f"SQL Error: {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
