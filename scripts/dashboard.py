#!/usr/bin/env python3
"""EvoTrader — Terminal Dashboard.

A static CLI report that reads the SQLite database and displays:
- Current market session status
- System overview (algorithm version, memory stats)
- Today's trading activity and P&L
- Open positions
- 30-day performance summary
- Recent evolution changes
- Recent audit log entries

Usage::

    uv run python scripts/dashboard.py
    uv run python scripts/dashboard.py --days 7
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# Resolve project paths
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from evotrader.config import AppConfig  # noqa: E402 (after the sys.path setup)

# ═══════════════════════════════════════════════════════════════════════
# ANSI Colors
# ═══════════════════════════════════════════════════════════════════════


class _C:
    """ANSI escape codes for terminal styling."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"

    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_BLUE = "\033[44m"

    @staticmethod
    def pnl(value: float) -> str:
        """Colour a P&L value green (positive) or red (negative)."""
        if value > 0:
            return f"{_C.GREEN}+${value:.2f}{_C.RESET}"
        elif value < 0:
            return f"{_C.RED}-${abs(value):.2f}{_C.RESET}"
        return f"{_C.DIM}$0.00{_C.RESET}"

    @staticmethod
    def pct(value: float) -> str:
        """Colour a percentage green/red."""
        if value > 0:
            return f"{_C.GREEN}+{value:.1%}{_C.RESET}"
        elif value < 0:
            return f"{_C.RED}{value:.1%}{_C.RESET}"
        return f"{_C.DIM}{value:.1%}{_C.RESET}"

    @staticmethod
    def session_badge(session: str) -> str:
        """Coloured badge for market session."""
        badges = {
            "regular": f"{_C.BG_GREEN}{_C.WHITE}{_C.BOLD} ● MARKET OPEN {_C.RESET}",
            "pre_market": f"{_C.BG_YELLOW}{_C.WHITE}{_C.BOLD} ◐ PRE-MARKET {_C.RESET}",
            "after_hours": f"{_C.BG_YELLOW}{_C.WHITE}{_C.BOLD} ◑ AFTER HOURS {_C.RESET}",
            "overnight": f"{_C.BG_BLUE}{_C.WHITE}{_C.BOLD} ☾ OVERNIGHT {_C.RESET}",
            "closed": f"{_C.BG_RED}{_C.WHITE}{_C.BOLD} ○ CLOSED {_C.RESET}",
        }
        return badges.get(session, session)


# ═══════════════════════════════════════════════════════════════════════
# Dashboard Sections
# ═══════════════════════════════════════════════════════════════════════


def _header() -> None:
    """Print the dashboard header."""
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    print()
    print(
        f"  {_C.BOLD}{_C.CYAN}╔══════════════════════════════════════════════════════════╗{_C.RESET}"
    )
    print(
        f"  {_C.BOLD}{_C.CYAN}║{_C.RESET}  {_C.BOLD}💰 EvoTrader — Dashboard{_C.RESET}                               {_C.BOLD}{_C.CYAN}║{_C.RESET}"
    )
    print(
        f"  {_C.BOLD}{_C.CYAN}║{_C.RESET}  {_C.DIM}{now}{_C.RESET}                             {_C.BOLD}{_C.CYAN}║{_C.RESET}"
    )
    print(
        f"  {_C.BOLD}{_C.CYAN}╚══════════════════════════════════════════════════════════╝{_C.RESET}"
    )
    print()


def _section(title: str) -> None:
    """Print a section divider."""
    print(f"  {_C.BOLD}{_C.BLUE}── {title} {'─' * (52 - len(title))}{_C.RESET}")


def _kv(key: str, value: str, indent: int = 4) -> None:
    """Print a key-value row."""
    pad = " " * indent
    print(f"{pad}{_C.DIM}{key:<24s}{_C.RESET} {value}")


def _market_status(config: AppConfig) -> None:
    """Print current market session status."""
    from evotrader.tools.market_hours import (
        get_allowed_order_types,
        get_current_session,
        get_trading_interval,
        is_twenty_four_hour_eligible,
        seconds_until_next_session,
    )

    rules = config.constitution.trading_rules
    allow_ext = getattr(rules, "allow_extended_hours", False)
    primary_ticker = config.settings.asset.primary_ticker
    overnight_interval = getattr(config.settings.schedule, "overnight_interval_seconds", 3600)

    session = get_current_session(twenty_four_hour=allow_ext)
    interval = get_trading_interval(
        twenty_four_hour=allow_ext,
        overnight_interval=overnight_interval,
    )
    next_session = seconds_until_next_session(twenty_four_hour=allow_ext)
    allowed_types = get_allowed_order_types(primary_ticker, session)
    is_24h = is_twenty_four_hour_eligible(primary_ticker)

    _section("Market Status")
    _kv("Session", _C.session_badge(session.value))
    _kv(
        "Primary Ticker", f"{primary_ticker} ({'24h Eligible' if is_24h else 'Regular Hours Only'})"
    )
    _kv("Allowed Order Types", ", ".join(allowed_types) if allowed_types else "None")
    _kv("Polling interval", f"{interval}s")
    if next_session > 0:
        hours = next_session // 3600
        minutes = (next_session % 3600) // 60
        _kv("Next session in", f"{hours}h {minutes}m")
    print()


def _system_overview(config: AppConfig) -> None:
    """Print system configuration overview."""
    _section("System")

    # Algorithm version
    algo_active = config.algorithms_dir / "active.yaml"
    version = "not configured"
    if algo_active.is_file():
        import yaml

        try:
            with open(algo_active) as f:
                data = yaml.safe_load(f) or {}
            version = data.get("active_version", "v001_initial")
        except Exception:
            pass
    else:
        # Fallback to legacy active.txt
        algo_active_txt = config.algorithms_dir / "active.txt"
        if algo_active_txt.is_file():
            version = algo_active_txt.read_text().strip()

    if version != "not configured":
        _kv("Algorithm", f"{_C.BOLD}{version}{_C.RESET}")
    else:
        _kv("Algorithm", f"{_C.DIM}not configured{_C.RESET}")

    # Dry run status
    dry_run = config.settings.dry_run.enabled
    model = config.settings.active_model.default or "unknown"
    ticker = config.settings.asset.primary_ticker
    _kv(
        "Mode",
        f"{_C.YELLOW}PAPER TRADING{_C.RESET}"
        if dry_run
        else f"{_C.RED}{_C.BOLD}LIVE TRADING{_C.RESET}",
    )
    _kv("Ticker", f"{_C.BOLD}{ticker}{_C.RESET}")
    _kv("Default model", model)

    # Agent instructions
    instructions_dir = config.instructions_dir
    if instructions_dir.is_dir():
        agents = [d.name for d in instructions_dir.iterdir() if d.is_dir()]
        _kv("Agent instructions", f"{len(agents)} agents configured")

    # Memory stats (if ChromaDB exists)
    memory_dir = config.memory_dir
    if memory_dir.is_dir():
        collection_count = sum(1 for _ in memory_dir.iterdir() if _.is_dir())
        _kv("Memory collections", str(collection_count))

    print()


async def _today_summary(db_path: Path) -> None:
    """Print today's trading summary."""
    import aiosqlite

    _section("Today's Activity")

    if not db_path.is_file():
        _kv("Status", f"{_C.DIM}No database found{_C.RESET}")
        print()
        return

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    async with aiosqlite.connect(str(db_path)) as conn:
        # Today's trades
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM trades WHERE date(timestamp) = ?",
            (today,),
        )
        row = await cursor.fetchone()
        trade_count = row[0] if row else 0

        # Today's P&L
        cursor = await conn.execute(
            """
            SELECT COALESCE(SUM(realized_pnl), 0.0)
            FROM trades WHERE date(timestamp) = ? AND realized_pnl IS NOT NULL
            """,
            (today,),
        )
        row = await cursor.fetchone()
        today_pnl = float(row[0]) if row else 0.0

        # Wins / losses today
        cursor = await conn.execute(
            """
            SELECT
                SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END)
            FROM trades WHERE date(timestamp) = ? AND realized_pnl IS NOT NULL
            """,
            (today,),
        )
        row = await cursor.fetchone()
        wins = int(row[0] or 0) if row else 0
        losses = int(row[1] or 0) if row else 0

    if trade_count == 0:
        _kv("Status", f"{_C.DIM}No trades today{_C.RESET}")
    else:
        _kv("Trades", f"{_C.BOLD}{trade_count}{_C.RESET}")
        _kv("P&L", _C.pnl(today_pnl))
        _kv("Win / Loss", f"{_C.GREEN}{wins}W{_C.RESET} / {_C.RED}{losses}L{_C.RESET}")
        if wins + losses > 0:
            _kv("Win rate", _C.pct(wins / (wins + losses)))

    print()


async def _open_positions(db_path: Path) -> None:
    """Print currently open positions."""
    import aiosqlite

    _section("Open Positions")

    if not db_path.is_file():
        _kv("Status", f"{_C.DIM}No database{_C.RESET}")
        print()
        return

    async with aiosqlite.connect(str(db_path)) as conn:
        cursor = await conn.execute(
            """
            SELECT t.id, t.ticker, t.direction, t.quantity, t.price,
                   t.timestamp, t.regime, t.confidence
            FROM trades t
            WHERE t.action = 'OPEN'
            AND NOT EXISTS (
                SELECT 1 FROM trades c
                WHERE c.related_trade_id = t.id
                AND c.action IN ('CLOSE', 'STOP_LOSS', 'TAKE_PROFIT')
            )
            ORDER BY t.timestamp DESC
            """
        )
        rows = await cursor.fetchall()

    if not rows:
        _kv("Status", f"{_C.DIM}No open positions{_C.RESET}")
    else:
        print(
            f"    {'ID':<6} {'Ticker':<7} {'Dir':<6} {'Qty':<8} {'Entry':<10} {'Regime':<15} {'Conf':<6}"
        )
        print(f"    {'─' * 6} {'─' * 7} {'─' * 6} {'─' * 8} {'─' * 10} {'─' * 15} {'─' * 6}")
        for row in rows:
            trade_id, ticker, direction, qty, price, _ts, regime, conf = row
            dir_color = _C.GREEN if direction == "LONG" else _C.RED
            print(
                f"    {trade_id:<6} {ticker:<7} "
                f"{dir_color}{direction:<6}{_C.RESET} "
                f"{qty:<8.1f} ${price:<9.2f} {regime:<15} {conf:<.2f}"
            )
    print()


async def _performance_summary(db_path: Path, days: int) -> None:
    """Print N-day performance summary."""
    import aiosqlite

    _section(f"Performance ({days}-day)")

    if not db_path.is_file():
        _kv("Status", f"{_C.DIM}No database{_C.RESET}")
        print()
        return

    async with aiosqlite.connect(str(db_path)) as conn:
        cursor = await conn.execute(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) as losses,
                AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END) as avg_win,
                AVG(CASE WHEN realized_pnl < 0 THEN realized_pnl END) as avg_loss,
                SUM(CASE WHEN realized_pnl > 0 THEN realized_pnl ELSE 0 END) as gross_profit,
                SUM(CASE WHEN realized_pnl < 0 THEN ABS(realized_pnl) ELSE 0 END) as gross_loss,
                SUM(COALESCE(realized_pnl, 0)) as total_pnl,
                MAX(realized_pnl) as best_trade,
                MIN(realized_pnl) as worst_trade,
                AVG(holding_period_s) as avg_hold
            FROM trades
            WHERE realized_pnl IS NOT NULL
            AND timestamp >= datetime('now', ?)
            """,
            (f"-{days} days",),
        )
        row = await cursor.fetchone()

    if not row or row[0] == 0:
        _kv("Status", f"{_C.DIM}No closed trades in period{_C.RESET}")
    else:
        total, wins, losses, avg_win, avg_loss, gp, gl, pnl, best, worst, avg_hold = row
        wins = wins or 0
        losses = losses or 0
        avg_win = avg_win or 0
        avg_loss = avg_loss or 0
        gp = gp or 0
        gl = gl or 0
        pnl = pnl or 0

        win_rate = wins / total if total > 0 else 0
        pf = gp / gl if gl > 0 else 0

        _kv("Total trades", f"{_C.BOLD}{total}{_C.RESET}")
        _kv("Win / Loss", f"{_C.GREEN}{wins}W{_C.RESET} / {_C.RED}{losses}L{_C.RESET}")
        _kv("Win rate", _C.pct(win_rate))
        _kv("Total P&L", _C.pnl(pnl))
        _kv("Avg win", _C.pnl(avg_win))
        _kv("Avg loss", _C.pnl(avg_loss))
        _kv("Profit factor", f"{pf:.2f}")
        _kv("Best trade", _C.pnl(best or 0))
        _kv("Worst trade", _C.pnl(worst or 0))
        if avg_hold:
            hold_min = int(avg_hold) // 60
            _kv("Avg hold time", f"{hold_min}m")

    print()


async def _recent_trades(db_path: Path, limit: int = 10) -> None:
    """Print recent trade log."""
    import aiosqlite

    _section(f"Recent Trades (last {limit})")

    if not db_path.is_file():
        _kv("Status", f"{_C.DIM}No database{_C.RESET}")
        print()
        return

    async with aiosqlite.connect(str(db_path)) as conn:
        cursor = await conn.execute(
            """
            SELECT id, timestamp, ticker, direction, action, quantity, price,
                   realized_pnl, regime, confidence
            FROM trades ORDER BY timestamp DESC LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()

    if not rows:
        _kv("Status", f"{_C.DIM}No trades recorded{_C.RESET}")
    else:
        print(
            f"    {'ID':<5} {'Time':<20} {'Action':<12} {'Dir':<6} {'Qty':<6} {'Price':<9} {'P&L':<12} {'Regime'}"
        )
        print(
            f"    {'─' * 5} {'─' * 20} {'─' * 12} {'─' * 6} {'─' * 6} {'─' * 9} {'─' * 12} {'─' * 15}"
        )
        for row in rows:
            tid, ts, _ticker, direction, action, qty, price, pnl, regime, _conf = row
            # Shorten timestamp
            ts_short = ts[:19] if ts else "?"
            dir_color = _C.GREEN if direction == "LONG" else _C.RED
            pnl_str = _C.pnl(pnl) if pnl is not None else f"{_C.DIM}—{_C.RESET}"
            print(
                f"    {tid:<5} {ts_short:<20} "
                f"{action:<12} {dir_color}{direction:<6}{_C.RESET} "
                f"{qty:<6.0f} ${price:<8.2f} {pnl_str:<22} {regime}"
            )
    print()


async def _evolution_log(db_path: Path, limit: int = 5) -> None:
    """Print recent evolution changes."""
    import aiosqlite

    _section(f"Evolution Log (last {limit})")

    if not db_path.is_file():
        _kv("Status", f"{_C.DIM}No database{_C.RESET}")
        print()
        return

    async with aiosqlite.connect(str(db_path)) as conn:
        try:
            cursor = await conn.execute(
                """
                SELECT timestamp, change_type, target_component,
                       old_version, new_version, status, risk_level
                FROM evolution_log
                ORDER BY timestamp DESC LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        except Exception:
            # Table or columns may not exist in older DB schemas
            rows = []

    if not rows:
        _kv("Status", f"{_C.DIM}No evolution changes{_C.RESET}")
    else:
        for row in rows:
            ts, change_type, target, old_v, new_v, status, _risk = row
            ts_short = ts[:19] if ts else "?"
            status_color = {
                "ACTIVE": _C.GREEN,
                "PROPOSED": _C.YELLOW,
                "ROLLED_BACK": _C.RED,
                "PENDING_REVIEW": _C.MAGENTA,
            }.get(status, _C.DIM)

            print(
                f"    {_C.DIM}{ts_short}{_C.RESET}  "
                f"{change_type:<20} {target:<20} "
                f"{old_v} → {new_v}  "
                f"{status_color}{status}{_C.RESET}"
            )
    print()


async def _audit_summary(db_path: Path) -> None:
    """Print audit log summary (tool call counts today)."""
    import aiosqlite

    _section("Audit Summary (today)")

    if not db_path.is_file():
        _kv("Status", f"{_C.DIM}No database{_C.RESET}")
        print()
        return

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    async with aiosqlite.connect(str(db_path)) as conn:
        try:
            cursor = await conn.execute(
                """
                SELECT tool_name, COUNT(*) as calls,
                       SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as ok,
                       SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as err,
                       AVG(duration_ms) as avg_ms
                FROM audit_log
                WHERE date(timestamp) = ?
                GROUP BY tool_name
                ORDER BY calls DESC
                LIMIT 15
                """,
                (today,),
            )
            rows = await cursor.fetchall()
        except Exception:
            rows = []

    if not rows:
        _kv("Status", f"{_C.DIM}No tool calls today{_C.RESET}")
    else:
        print(f"    {'Tool':<30} {'Calls':<7} {'OK':<5} {'Err':<5} {'Avg ms'}")
        print(f"    {'─' * 30} {'─' * 7} {'─' * 5} {'─' * 5} {'─' * 7}")
        for row in rows:
            tool, calls, ok, err, avg_ms = row
            err_str = f"{_C.RED}{err}{_C.RESET}" if err else f"{_C.DIM}0{_C.RESET}"
            avg_str = f"{avg_ms:.0f}" if avg_ms else "—"
            print(f"    {tool:<30} {calls:<7} {ok:<5} {err_str:<14} {avg_str}")
    print()


def _footer(config: AppConfig) -> None:
    """Print the dashboard footer."""
    # The data folder may live outside the project (EVOTRADER_DATA_DIR).
    db_hint = (
        config.db_path.relative_to(config.project_root)
        if config.db_path.is_relative_to(config.project_root)
        else config.db_path
    )
    print(f"  {_C.DIM}Data: {db_hint} │ Refresh: ./run.sh cli-dashboard{_C.RESET}")
    print()


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════


async def _run(days: int, trades_limit: int) -> None:
    """Run all dashboard sections."""
    config = AppConfig()
    db_path = config.db_path

    _header()
    _market_status(config)
    _system_overview(config)
    await _today_summary(db_path)
    await _open_positions(db_path)
    await _performance_summary(db_path, days)
    await _recent_trades(db_path, trades_limit)
    await _evolution_log(db_path)
    await _audit_summary(db_path)
    _footer(config)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="EvoTrader — Terminal Dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Performance lookback period in days (default: 30)",
    )
    parser.add_argument(
        "--trades",
        type=int,
        default=10,
        help="Number of recent trades to show (default: 10)",
    )
    parser.add_argument(
        "--data-dir", help="Data folder to read (default: EVOTRADER_DATA_DIR, else ./data)"
    )
    args = parser.parse_args()

    from evotrader import paths

    if args.data_dir:
        os.environ[paths.DATA_DIR_ENV] = str(Path(args.data_dir).expanduser().resolve())
    # Before setup there is nothing to show, and reading would create folders.
    if not (paths.data_dir() / "settings.yaml").is_file():
        sys.exit(f"No settings in {paths.data_dir()} yet. Run {paths.run_command('setup')} first.")

    try:
        asyncio.run(_run(args.days, args.trades))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
