import asyncio
import json
import logging
import math
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from evotrader.db.connection import WriteTurns
from evotrader.models.mcp import McpEquityQuote, McpOptionQuote
from evotrader.tools.market_hours import ET, is_market_open, is_trading_day, regular_close
from evotrader.utils import parse_occ_symbol

logger = logging.getLogger(__name__)

# How each order type the sim accepts is stored. The broker calls a stop order
# `stop_market`, but sim_orders only admits `stop`: its CHECK constraint was
# fixed when the table was created, and SQLite cannot change it in place. The
# journal's own spelling, `stop`, means the same order and is accepted too.
_STORED_ORDER_TYPE = {
    "market": "market",
    "limit": "limit",
    "stop_market": "stop",
    "stop": "stop",
    "stop_limit": "stop_limit",
}
_STOP_TYPES = frozenset({"stop", "stop_limit"})

# How the broker's order book shows a stop: a market (or, for a stop-limit, a
# limit) order with `trigger: "stop"` and a stop price.
_SHOWN_ORDER_TYPE = {"stop": "market", "stop_limit": "limit"}

# The broker treats an order sent without a time_in_force as a day order.
_DEFAULT_TIME_IN_FORCE = "gfd"

_QTY_TOLERANCE = 1e-4

# The broker's refusal when a sell is for more shares than are free: held,
# less those a resting sell already holds back. The executor's instructions
# read this exact text, so practice mode must use it too.
_NOT_ENOUGH_SHARES = "Not enough shares to sell"
# The broker takes fractional shares in market orders only. Its own wording for
# refusing a fractional stop is not on record here; this is the sim's.
_WHOLE_SHARES_ONLY = "Stop orders must be for a whole number of shares"


def _session_close_after(placed_at: datetime) -> datetime:
    """When a day order placed at ``placed_at`` expires: the close of its session.

    Placed before the close on a trading day, it is good for that day. Placed
    after the close, or on a weekend or holiday, the broker holds it for the next
    session, so it lasts until that session's close. The close is that day's
    real one from the market calendar: 1:00 pm ET on NYSE's early-close days, so
    a practice day order does not keep working three hours after the broker's
    would have ended.
    """
    local = placed_at.astimezone(ET)
    day = local.date()
    if not is_trading_day(day) or local.time() >= regular_close(day):
        day += timedelta(days=1)
        while not is_trading_day(day):
            day += timedelta(days=1)
    return datetime.combine(day, regular_close(day), tzinfo=ET)


def _day_order_expired(order: Mapping[str, Any], now: datetime) -> bool:
    """Whether a resting order is a day order whose session has closed.

    Only orders that carry a time_in_force are judged here. Rows without one —
    placed before the sim recorded it, and option orders, which do not carry it
    yet — keep the old age rule in ``cancel_stale_pending_orders``.
    """
    tif = order["time_in_force"]
    if tif is None or str(tif).lower() == "gtc":
        return False
    # The sim stores placement times as SQLite's datetime('now'): UTC, no zone.
    try:
        placed_at = datetime.fromisoformat(str(order["timestamp"]))
    except ValueError:
        return False
    if placed_at.tzinfo is None:
        placed_at = placed_at.replace(tzinfo=UTC)
    return now >= _session_close_after(placed_at)


def _reaches_limit(side: str, quote: float, limit_price: float) -> bool:
    """Whether the quote a buy would pay (or a sell would take) meets the limit."""
    return quote <= limit_price if side == "buy" else quote >= limit_price


def _multiplier(row: Mapping[str, Any]) -> float:
    """Shares per unit of an order or position: 100 for an option contract, else 1."""
    return 100.0 if row["asset_type"] == "OPTION" else 1.0


def _is_whole(quantity: float) -> bool:
    """Whether a share quantity is a whole number, float noise aside (3.00000001 is 3)."""
    return abs(quantity - round(quantity)) <= _QTY_TOLERANCE


class SimBroker:
    """Simulated brokerage engine utilizing a local SQLite database and live market data.

    This class models Robinhood write operations (orders, cancellations) locally,
    while fetching real-time asset prices for mark-to-market calculations and order execution.
    """

    def __init__(self, db_path: Path | str, real_mcp_toolset: Any = None) -> None:
        self.db_path = Path(db_path)
        self.real_mcp_toolset = real_mcp_toolset
        self._connection: aiosqlite.Connection | None = None
        self.account_number = "SIM_AGENT_TRADER"
        # One fill check at a time.
        self._lock = asyncio.Lock()
        # One transaction at a time on the shared connection (see _transaction).
        self._turns = WriteTurns()

        # Default config options (will be overridden by settings if provided)
        self.slippage_model = "spread"
        self.slippage_bps = 5.0
        self.auto_close_expired_options = True

    async def initialize(self) -> None:
        """Initialize database connections, run schema migration, and create the default account."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(str(self.db_path))
        self._connection.row_factory = aiosqlite.Row

        # Enable WAL mode and foreign keys
        await self._connection.execute("PRAGMA journal_mode=WAL")
        await self._connection.execute("PRAGMA foreign_keys=ON")

        # Apply schema. The one write that does not go through _transaction():
        # executescript commits on its own, and this runs before the broker is
        # shared with any other task.
        schema_path = Path(__file__).parent / "sim_schema.sql"
        if schema_path.is_file():
            schema_sql = schema_path.read_text()
            await self._connection.executescript(schema_sql)
        else:
            raise FileNotFoundError(f"Simulation schema not found at {schema_path}")

        async with self._connection.execute("PRAGMA table_info(sim_orders)") as cursor:
            order_columns = {row["name"] for row in await cursor.fetchall()}

        async with self._transaction() as conn:
            # Sim databases created before stop orders were simulated lack this
            # column; CREATE TABLE IF NOT EXISTS does not add it to them.
            if "time_in_force" not in order_columns:
                await conn.execute("ALTER TABLE sim_orders ADD COLUMN time_in_force TEXT")
            # Ensure default account exists
            await conn.execute(
                """
                INSERT OR IGNORE INTO sim_accounts (account_number, cash_balance)
                VALUES (?, 0.0)
                """,
                (self.account_number,),
            )
        logger.info("SimBroker initialized database at %s", self.db_path)

    async def close(self) -> None:
        """Close database connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None
            logger.info("SimBroker database connection closed")

    async def _get_conn(self) -> aiosqlite.Connection:
        if not self._connection:
            await self.initialize()
        return self._connection  # type: ignore

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Yield the connection inside a transaction — the way to write.

        The agents' tool calls run side by side and share this one connection,
        so transactions take turns (see :class:`WriteTurns`), exactly as the main
        database's do. Commits on a clean finish and rolls back on any other exit,
        cancellation included. Every write goes through here: a write that
        commits on its own can land in the middle of another task's transaction
        and commit half of it.
        """
        conn = await self._get_conn()
        async with self._turns.transaction(conn) as tx:
            yield tx

    # ── Fund Management ──────────────────────────────────────────────────

    async def _cash_balance(self, conn: aiosqlite.Connection) -> float:
        async with conn.execute(
            "SELECT cash_balance FROM sim_accounts WHERE account_number = ?", (self.account_number,)
        ) as cursor:
            row = await cursor.fetchone()
        return row["cash_balance"] if row else 0.0

    async def deposit(self, amount: float) -> dict[str, Any]:
        """Deposit simulated cash into the brokerage account."""
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("Deposit amount must be a positive, finite number")

        # The balance is read inside the write turn. Read before it, two deposits
        # made at the same moment both started from the same balance, and the
        # second write overwrote the first.
        async with self._transaction() as conn:
            new_balance = await self._cash_balance(conn) + amount
            await conn.execute(
                "UPDATE sim_accounts SET cash_balance = ?, updated_at = datetime('now') WHERE account_number = ?",
                (new_balance, self.account_number),
            )
            await conn.execute(
                """
                INSERT INTO sim_transfers (account_number, transfer_type, amount, balance_after)
                VALUES (?, 'DEPOSIT', ?, ?)
                """,
                (self.account_number, amount, new_balance),
            )

        logger.info("Deposited $%s. New cash balance is $%s", amount, new_balance)
        return {
            "account_number": self.account_number,
            "status": "success",
            "deposit_amount": amount,
            "cash_balance": new_balance,
        }

    async def withdraw(self, amount: float) -> dict[str, Any]:
        """Withdraw simulated cash from the brokerage account."""
        if amount <= 0:
            raise ValueError("Withdrawal amount must be positive")

        # Checked and written in one write turn, so two withdrawals made at the
        # same moment cannot both pass on the same balance. Raising here rolls the
        # turn back, and nothing has been written yet.
        async with self._transaction() as conn:
            current_balance = await self._cash_balance(conn)
            if current_balance < amount:
                raise ValueError(
                    f"Insufficient funds for withdrawal. Balance: {current_balance}, Requested: {amount}"
                )
            new_balance = current_balance - amount
            await conn.execute(
                "UPDATE sim_accounts SET cash_balance = ?, updated_at = datetime('now') WHERE account_number = ?",
                (new_balance, self.account_number),
            )
            await conn.execute(
                """
                INSERT INTO sim_transfers (account_number, transfer_type, amount, balance_after)
                VALUES (?, 'WITHDRAWAL', ?, ?)
                """,
                (self.account_number, amount, new_balance),
            )

        logger.info("Withdrew $%s. New cash balance is $%s", amount, new_balance)
        return {
            "account_number": self.account_number,
            "status": "success",
            "withdrawal_amount": amount,
            "cash_balance": new_balance,
        }

    # ── Real MCP Live Price Resolution ───────────────────────────────────

    async def _get_live_price(self, ticker: str) -> float:
        """Resolve stock price via real MCP provider."""
        if not self.real_mcp_toolset:
            logger.warning(
                "No real MCP toolset bound for price resolution of %s. Using default 100.0", ticker
            )
            return 100.0
        try:
            session = await self.real_mcp_toolset._mcp_session_manager.create_session()
            res = await session.call_tool("get_equity_quotes", {"symbols": [ticker]})
            if not getattr(res, "isError", False) and res.content:
                data = json.loads(res.content[0].text)
                results = data.get("data", {}).get("results", []) or []
                if results:
                    q = results[0].get("quote", {})
                    quote = McpEquityQuote(**q)
                    return quote.resolve_live_price()
        except Exception as e:
            logger.error("Failed to fetch live price for %s: %s", ticker, e)
        return 100.0

    async def _get_live_bid_ask(self, ticker: str, strict: bool = False) -> tuple[float, float]:
        """Resolve bid/ask prices via real MCP provider. Returns (bid, ask).

        With no usable quote this falls back to 100.0, unless ``strict``: then it
        raises LookupError. The fill check is strict because a resting order must
        not trade on a made-up price. Read as a real bid, the fallback would
        trigger every sell stop above $100 whenever the quote source failed.
        """
        if not self.real_mcp_toolset:
            if strict:
                raise LookupError(f"no quote source for {ticker}")
            return 100.0, 100.0
        try:
            session = await self.real_mcp_toolset._mcp_session_manager.create_session()
            res = await session.call_tool("get_equity_quotes", {"symbols": [ticker]})
            if not getattr(res, "isError", False) and res.content:
                data = json.loads(res.content[0].text)
                results = data.get("data", {}).get("results", []) or []
                if results:
                    q = results[0].get("quote", {})
                    quote = McpEquityQuote(**q)

                    last = quote.ext or quote.reg or 0.0
                    bid = quote.bid or last or 0.0
                    ask = quote.ask or last or 0.0

                    if bid == 0.0 and ask == 0.0:
                        if strict:
                            raise LookupError(f"empty quote for {ticker}")
                        return last, last
                    if bid == 0.0:
                        bid = ask
                    if ask == 0.0:
                        ask = bid
                    return bid, ask
        except Exception as e:
            logger.error("Failed to fetch live bid/ask for %s: %s", ticker, e)
        if strict:
            raise LookupError(f"no live quote for {ticker}")
        return 100.0, 100.0

    async def _get_live_option_price(self, option_id: str) -> float:
        """Resolve option contract price (premium per share) via real MCP provider."""
        if not self.real_mcp_toolset:
            logger.warning(
                "No real MCP toolset bound for option price resolution of %s. Using default 1.0",
                option_id,
            )
            return 1.0
        try:
            session = await self.real_mcp_toolset._mcp_session_manager.create_session()
            res = await session.call_tool("get_option_quotes", {"instrument_ids": [option_id]})
            if not getattr(res, "isError", False) and res.content:
                data = json.loads(res.content[0].text)
                results = data.get("data", {}).get("results", []) or []
                if results:
                    q = results[0].get("quote", {})
                    quote = McpOptionQuote(**q)
                    return quote.resolve_live_price()
        except Exception as e:
            logger.error("Failed to fetch live option price for %s: %s", option_id, e)
        return 1.0

    async def _get_live_option_bid_ask(
        self, option_id: str, strict: bool = False
    ) -> tuple[float, float]:
        """Resolve bid/ask for option premium via real MCP provider. Returns (bid, ask).

        Falls back to 1.0 without a usable quote, or raises LookupError when
        ``strict`` (see _get_live_bid_ask).
        """
        if not self.real_mcp_toolset:
            if strict:
                raise LookupError(f"no quote source for {option_id}")
            return 1.0, 1.0
        try:
            session = await self.real_mcp_toolset._mcp_session_manager.create_session()
            res = await session.call_tool("get_option_quotes", {"instrument_ids": [option_id]})
            if not getattr(res, "isError", False) and res.content:
                data = json.loads(res.content[0].text)
                results = data.get("data", {}).get("results", []) or []
                if results:
                    q = results[0].get("quote", {})
                    quote = McpOptionQuote(**q)

                    mark = quote.mark or quote.reg or 0.0
                    bid = quote.bid or mark or 0.0
                    ask = quote.ask or mark or 0.0

                    if bid == 0.0 and ask == 0.0:
                        if strict:
                            raise LookupError(f"empty quote for {option_id}")
                        return mark, mark
                    if bid == 0.0:
                        bid = ask
                    if ask == 0.0:
                        ask = bid
                    return bid, ask
        except Exception as e:
            logger.error("Failed to fetch live option bid/ask for %s: %s", option_id, e)
        if strict:
            raise LookupError(f"no live quote for {option_id}")
        return 1.0, 1.0

    # ── Expiration and Limit Order Processing ────────────────────────────

    async def _process_pending_orders_and_expirations(self) -> None:
        """Process any resting limit and stop orders and check for option expirations."""
        async with self._lock:
            if self.auto_close_expired_options:
                await self._auto_close_expired_options()
            await self._fill_pending_limit_orders()

    async def _auto_close_expired_options(self) -> None:
        """Auto-close options positions if the expiration date has passed."""
        conn = await self._get_conn()
        now = datetime.now(UTC)
        current_date_str = now.strftime("%Y-%m-%d")

        # Find expired option positions
        async with conn.execute(
            "SELECT * FROM sim_positions WHERE asset_type = 'OPTION' AND expiration < ?",
            (current_date_str,),
        ) as cursor:
            expired_positions = await cursor.fetchall()

        if not expired_positions:
            return

        for pos in expired_positions:
            option_id = pos["option_id"]
            ticker = pos["ticker"]
            qty = pos["quantity"]
            strike = pos["strike"]
            option_type = pos["option_type"]
            expiration = pos["expiration"]

            # Fetch the underlying stock price at expiration to determine intrinsic value
            underlying_price = await self._get_live_price(ticker)

            # Intrinsic value calculation
            if option_type == "call":
                intrinsic_value = max(0.0, underlying_price - strike)
            else:
                intrinsic_value = max(0.0, strike - underlying_price)

            close_value = qty * intrinsic_value * 100.0  # multiplier

            async with self._transaction() as conn:
                # Add close value back to cash
                await conn.execute(
                    "UPDATE sim_accounts SET cash_balance = cash_balance + ?, updated_at = datetime('now') WHERE account_number = ?",
                    (close_value, self.account_number),
                )
                # Remove the position
                await conn.execute(
                    "DELETE FROM sim_positions WHERE id = ?",
                    (pos["id"],),
                )
                # Record expired order fill
                order_id = f"expire_{uuid.uuid4().hex[:8]}"
                await conn.execute(
                    """
                    INSERT INTO sim_orders (
                        id, account_number, asset_type, ticker, option_id, side, order_type,
                        quantity, limit_price, status, fill_price, filled_quantity, total_cost,
                        timestamp, filled_at, option_type, strike, expiration, position_effect
                    ) VALUES (?, ?, 'OPTION', ?, ?, 'sell', 'market', ?, ?, 'filled', ?, ?, ?, datetime('now'), datetime('now'), ?, ?, ?, 'close')
                    """,
                    (
                        order_id,
                        self.account_number,
                        ticker,
                        option_id,
                        qty,
                        intrinsic_value,
                        intrinsic_value,
                        qty,
                        -close_value,
                        option_type,
                        strike,
                        expiration,
                    ),
                )
            logger.info(
                "Auto-closed expired option %s (%s). Underlyer price: %s, Strike: %s, Intrinsic value: %s, Realized: $%s",
                option_id,
                option_type,
                underlying_price,
                strike,
                intrinsic_value,
                close_value,
            )

    async def _fill_pending_limit_orders(self) -> list[dict[str, Any]]:
        """Fill the resting orders the current price has reached: limits and stops.

        A limit order fills at the quote once the quote is at its limit or
        better. A stop triggers once the quote it trades against reaches the stop
        — the bid falls to or below a sell stop, the ask rises to or above a buy
        stop — and then fills like the market order it has become: at that quote,
        not at the stop price. A stop guarantees the exit, not the price; after a
        gap the quote is already past the stop, and filling at the stop would make
        every practice stop-out look kinder than the broker's. A stop-limit that
        triggers with the quote already past its limit works on as a plain limit.

        As at the broker, stops trigger only in the regular session. A day order
        whose session has closed is not filled here: cancel_stale_pending_orders
        cancels it, and its caller tells the journal.

        Returns a list of dicts describing each order that was filled during
        this call, so callers can journal or notify as needed.
        """
        conn = await self._get_conn()
        async with conn.execute("SELECT * FROM sim_orders WHERE status = 'pending'") as cursor:
            pending_orders = await cursor.fetchall()

        if not pending_orders:
            return []

        now = datetime.now(UTC)
        filled_orders: list[dict[str, Any]] = []

        for order in pending_orders:
            order_id = order["id"]
            ticker = order["ticker"]
            asset_type = order["asset_type"]
            option_id = order["option_id"]
            side = order["side"]
            order_type = order["order_type"]
            limit_price = order["limit_price"]
            stop_price = order["stop_price"]
            qty = order["quantity"]
            is_stop = order_type in _STOP_TYPES

            # Judged on the wall clock, which is what placement times are in.
            if _day_order_expired(order, now):
                continue
            # The market-hours clock, not the wall clock: a practice cycle run
            # with --mock-time is trading in a session the rest of the app treats
            # as open, and its stops must be able to trigger in it.
            if is_stop and (stop_price is None or not is_market_open()):
                continue

            # Resolve current prices. No real quote, no fill.
            try:
                if asset_type == "EQUITY":
                    bid, ask = await self._get_live_bid_ask(ticker, strict=True)
                else:
                    bid, ask = await self._get_live_option_bid_ask(option_id, strict=True)
            except LookupError as e:
                logger.warning("Resting order %s left unchecked: %s", order_id, e)
                continue

            # The side of the quote this order trades against.
            quote = ask if side == "buy" else bid

            if is_stop:
                triggered = quote >= stop_price if side == "buy" else quote <= stop_price
                if not triggered:
                    continue
                if order_type == "stop_limit" and not _reaches_limit(side, quote, limit_price):
                    # Triggered, but already past its limit. From here it is a
                    # resting limit order, as at the broker, so a recovery to the
                    # limit fills it even if the price is back beyond the stop.
                    async with self._transaction() as conn:
                        await conn.execute(
                            "UPDATE sim_orders SET order_type = 'limit' WHERE id = ? AND status = 'pending'",
                            (order_id,),
                        )
                    logger.info(
                        "Stop-limit order %s (%s %s) triggered at %s, beyond its limit %s; "
                        "now resting as a limit order",
                        order_id,
                        side,
                        ticker,
                        quote,
                        limit_price,
                    )
                    continue
            elif not _reaches_limit(side, quote, limit_price):
                continue

            # Calculate costs & slippage
            if asset_type == "EQUITY":
                fill_price = self._apply_slippage(side, quote)
                total_cost = qty * fill_price
            else:
                fill_price = self._apply_option_slippage(side, quote)
                total_cost = qty * fill_price * 100.0

            if side == "sell":
                total_cost = -total_cost

            # Update the order status and update position / account
            async with self._transaction() as conn:
                # A resting sell never sells shares that are not there. Placement
                # holds shares back for resting sells, but a practice database
                # written before it did can hold two stops for the same shares:
                # the first to trigger sells them, and the other is cancelled
                # rather than sell them again into a short the account cannot hold.
                held = None
                if asset_type == "EQUITY" and side == "sell":
                    held = await self._held_shares(conn, ticker)
                if held is not None and qty > held + _QTY_TOLERANCE:
                    cursor = await conn.execute(
                        "UPDATE sim_orders SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
                        (order_id,),
                    )
                    cancelled_for_want_of_shares = cursor.rowcount == 1
                    still_resting = False
                else:
                    cancelled_for_want_of_shares = False
                    # Only an order that is still resting fills. The agents' calls
                    # run side by side, and a cancel can land while this check
                    # waited for its quote; filling anyway would sell shares after
                    # the cancel.
                    cursor = await conn.execute(
                        """
                        UPDATE sim_orders
                        SET status = 'filled', fill_price = ?, filled_quantity = ?, total_cost = ?, filled_at = datetime('now')
                        WHERE id = ? AND status = 'pending'
                        """,
                        (fill_price, qty, total_cost, order_id),
                    )
                    still_resting = cursor.rowcount == 1

                if still_resting:
                    # Deduct the total cost from cash balance upon fill (since it wasn't deducted during placement)
                    if side == "buy":
                        await conn.execute(
                            "UPDATE sim_accounts SET cash_balance = cash_balance - ?, updated_at = datetime('now') WHERE account_number = ?",
                            (total_cost, self.account_number),
                        )
                    else:
                        # Add proceeds to cash balance (nothing was blocked for sells, except position check)
                        await conn.execute(
                            "UPDATE sim_accounts SET cash_balance = cash_balance + ?, updated_at = datetime('now') WHERE account_number = ?",
                            (abs(total_cost), self.account_number),
                        )

                    # Update position
                    await self._update_position_on_fill(conn, order, fill_price)

            if cancelled_for_want_of_shares:
                logger.warning(
                    "Cancelled resting sell %s (%s %s): only %s held, so it cannot fill",
                    order_id,
                    qty,
                    ticker,
                    held,
                )
                continue

            if not still_resting:
                logger.info(
                    "Order %s was cancelled or filled while its fill check ran; left as it is",
                    order_id,
                )
                continue

            logger.info(
                "Filled resting %s order %s (%s %s) at %s",
                order_type,
                order_id,
                side,
                ticker,
                fill_price,
            )

            filled_orders.append(
                {
                    "order_id": order_id,
                    "ticker": ticker,
                    "asset_type": asset_type,
                    "option_id": option_id,
                    "side": side,
                    "quantity": qty,
                    "order_type": order_type,
                    "limit_price": limit_price,
                    "stop_price": stop_price,
                    "fill_price": fill_price,
                    "total_cost": total_cost,
                    "status": "filled",
                    "option_type": order["option_type"],
                    "strike": order["strike"],
                    "expiration": order["expiration"],
                    "position_effect": order["position_effect"],
                }
            )

        return filled_orders

    async def cancel_stale_pending_orders(
        self, max_age_hours: float = 24.0
    ) -> list[dict[str, Any]]:
        """Cancel resting orders the broker would no longer be working.

        A day order (``gfd``, the broker's default) ends at the close of the
        session it was placed for; the broker then lists it as cancelled, as this
        does. A ``gtc`` order rests until it fills or is cancelled. Orders with no
        recorded time_in_force — placed before the sim recorded it, and option
        orders — keep the old rule: cancelled once older than ``max_age_hours``.

        Returns list of cancelled order details.
        """
        conn = await self._get_conn()
        async with conn.execute("SELECT * FROM sim_orders WHERE status = 'pending'") as cursor:
            pending_orders = await cursor.fetchall()

        now = datetime.now(UTC)
        cutoff = (now - timedelta(hours=max_age_hours)).strftime("%Y-%m-%d %H:%M:%S")
        stale_orders = [
            order
            for order in pending_orders
            if (
                order["timestamp"] < cutoff
                if order["time_in_force"] is None
                else _day_order_expired(order, now)
            )
        ]
        if not stale_orders:
            return []

        cancelled: list[dict[str, Any]] = []
        async with self._transaction() as conn:
            for order in stale_orders:
                # A fill may have landed since the read above; leave that one be.
                cursor = await conn.execute(
                    "UPDATE sim_orders SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
                    (order["id"],),
                )
                if cursor.rowcount == 1:
                    cancelled.append(dict(order))

        for order in cancelled:
            logger.info(
                "Auto-cancelled pending order %s (placed %s): %s",
                order["id"],
                order["timestamp"],
                (
                    f"older than {max_age_hours}h"
                    if order["time_in_force"] is None
                    else "day order past its session close"
                ),
            )

        return cancelled

    # ── Slippage Model ───────────────────────────────────────────────────

    def _apply_slippage(self, side: str, base_price: float) -> float:
        if self.slippage_model == "none":
            return base_price
        elif self.slippage_model == "fixed":
            multiplier = (
                1.0 + (self.slippage_bps / 10000.0)
                if side == "buy"
                else 1.0 - (self.slippage_bps / 10000.0)
            )
            return round(base_price * multiplier, 4)
        # For "spread" model, we expect bid/ask inputs directly to be utilized.
        # If we fall back here, just treat it as no extra slippage because spread is already factored in.
        return base_price

    def _apply_option_slippage(self, side: str, base_price: float) -> float:
        # Options usually have higher spreads; we can use fixed bps or standard multiplier
        if self.slippage_model == "none":
            return base_price
        elif self.slippage_model == "fixed":
            # 2x fixed slippage for options due to wider spreads
            mult = (
                1.0 + (2.0 * self.slippage_bps / 10000.0)
                if side == "buy"
                else 1.0 - (2.0 * self.slippage_bps / 10000.0)
            )
            return round(base_price * mult, 4)
        return base_price

    # ── MCP Interception Handlers ────────────────────────────────────────

    async def get_accounts(self) -> dict[str, Any]:
        """Return account details mimicking Robinhood's get_accounts response shape."""
        # Refresh state
        await self._process_pending_orders_and_expirations()

        return {
            "data": {
                "accounts": [
                    {
                        "account_number": self.account_number,
                        "agentic_allowed": True,
                        "is_default": True,
                        "type": "simulated",
                    }
                ]
            }
        }

    async def _mark(self, position: Mapping[str, Any]) -> float:
        """The live price of one position: per share, or per share of an option contract."""
        if position["asset_type"] == "EQUITY":
            return await self._get_live_price(position["ticker"])
        return await self._get_live_option_price(position["option_id"])

    async def _short_marks(self) -> dict[int, float]:
        """Live prices of the short positions, by position id, for the buying-power check.

        Fetched before an order's write turn: a price is a network call, and
        holding the turn through it would make every other write wait on it.
        """
        conn = await self._get_conn()
        async with conn.execute(
            "SELECT * FROM sim_positions WHERE account_number = ? AND quantity < 0",
            (self.account_number,),
        ) as cursor:
            shorts = await cursor.fetchall()
        return {pos["id"]: await self._mark(pos) for pos in shorts}

    async def _buying_power(self, conn: aiosqlite.Connection, marks: Mapping[int, float]) -> float:
        """Cash free for a new buy: cash, less what resting buys may cost, less short margin.

        An order runs this inside its own write turn, through that turn's
        connection, so a buy placed at the same moment waits for the turn and
        then sees what this one spent. ``marks`` prices the short positions by
        position id; a short opened after they were fetched counts at its
        average cost.
        """
        cash = await self._cash_balance(conn)

        async with conn.execute(
            "SELECT quantity, limit_price, stop_price, asset_type FROM sim_orders WHERE status = 'pending' AND side = 'buy'"
        ) as cursor:
            pending_buys = await cursor.fetchall()
        # A buy stop has no limit; its stop price is the best guess at its cost.
        blocked_cash = sum(
            pb["quantity"] * (pb["limit_price"] or pb["stop_price"] or 0.0) * _multiplier(pb)
            for pb in pending_buys
        )

        async with conn.execute(
            "SELECT id, asset_type, quantity, avg_cost_basis FROM sim_positions WHERE account_number = ? AND quantity < 0",
            (self.account_number,),
        ) as cursor:
            shorts = await cursor.fetchall()
        # A short holds back twice its value: the proceeds plus 100% collateral.
        margin_blocked = sum(
            abs(pos["quantity"] * marks.get(pos["id"], pos["avg_cost_basis"]) * _multiplier(pos))
            * 2.0
            for pos in shorts
        )

        return max(0.0, cash - blocked_cash - margin_blocked)

    async def get_portfolio(self, account_number: str) -> dict[str, Any]:
        """Calculate and return portfolio value mimicking Robinhood's shape."""
        await self._process_pending_orders_and_expirations()

        conn = await self._get_conn()
        async with conn.execute(
            "SELECT * FROM sim_positions WHERE account_number = ?", (self.account_number,)
        ) as cursor:
            positions = await cursor.fetchall()
        marks = {pos["id"]: await self._mark(pos) for pos in positions}

        cash = await self._cash_balance(conn)
        total_positions_val = sum(
            pos["quantity"] * marks[pos["id"]] * _multiplier(pos) for pos in positions
        )
        total_value = cash + total_positions_val
        buying_power = await self._buying_power(conn, marks)

        return {
            "data": {
                "account_number": self.account_number,
                "cash": cash,
                "total_value": total_value,
                "buying_power": {"buying_power": buying_power},
            }
        }

    async def get_equity_positions(self, account_number: str) -> dict[str, Any]:
        """Return stock positions mimicking Robinhood's shape."""
        await self._process_pending_orders_and_expirations()

        conn = await self._get_conn()
        async with conn.execute(
            "SELECT * FROM sim_positions WHERE account_number = ? AND asset_type = 'EQUITY'",
            (self.account_number,),
        ) as cursor:
            rows = await cursor.fetchall()

        positions = []
        for r in rows:
            positions.append(
                {
                    "symbol": r["ticker"],
                    "quantity": f"{r['quantity']:.4f}",
                    "average_buy_price": f"{r['avg_cost_basis']:.4f}",
                    "intraday_average_buy_price": f"{r['avg_cost_basis']:.4f}",
                }
            )

        return {"data": {"positions": positions}}

    async def get_option_positions(self, account_number: str) -> dict[str, Any]:
        """Return option positions mimicking Robinhood's shape."""
        await self._process_pending_orders_and_expirations()

        conn = await self._get_conn()
        async with conn.execute(
            "SELECT * FROM sim_positions WHERE account_number = ? AND asset_type = 'OPTION'",
            (self.account_number,),
        ) as cursor:
            rows = await cursor.fetchall()

        positions = []
        for r in rows:
            positions.append(
                {
                    "option_id": r["option_id"],
                    "symbol": r["ticker"],
                    "quantity": f"{r['quantity']:.4f}",
                    "average_price": f"{r['avg_cost_basis']:.4f}",
                    "type": r["option_type"],
                    "strike": r["strike"],
                    "expiration": r["expiration"],
                }
            )

        return {"data": {"positions": positions}}

    # ── Order Placement ──────────────────────────────────────────────────

    async def place_equity_order(self, args: dict[str, Any]) -> dict[str, Any]:
        """Place and execute a simulated stock order.

        A market order fills at once at the live quote, and a limit order when
        the quote already meets its limit; otherwise the order rests. A stop order
        always rests at placement, as the broker's does, even with the price
        already through the stop: the next fill check triggers it (see
        _fill_pending_limit_orders).

        Refused as the broker refuses them: a market order with no live quote, a
        buy beyond buying power, a sell for more shares than are free (held, less
        those resting sells hold back), and a stop for a fractional quantity.
        """
        # Clean up expired option cash before checking buying power
        await self._process_pending_orders_and_expirations()

        ticker = args.get("symbol") or args.get("ticker")
        if not ticker:
            raise ValueError("Ticker symbol is required for stock order")

        side = args.get("side", "buy").lower()
        requested_type = str(args.get("type", "market")).lower()
        order_type = _STORED_ORDER_TYPE.get(requested_type)
        qty = float(args.get("quantity", 0.0))
        time_in_force = str(args.get("time_in_force") or _DEFAULT_TIME_IN_FORCE).lower()

        if qty <= 0:
            raise ValueError("Order quantity must be positive")

        # Resolve order prices
        limit_price = args.get("limit_price") or args.get("price")
        if limit_price is not None:
            limit_price = float(limit_price)
        stop_price = args.get("stop_price")
        if stop_price is not None:
            stop_price = float(stop_price)

        if order_type is None:
            raise NotImplementedError(f"Order type {requested_type} is not simulated")
        if order_type == "limit" and limit_price is None:
            raise ValueError("Limit price is required for limit orders")
        if order_type in _STOP_TYPES:
            if stop_price is None:
                raise ValueError("Stop price is required for stop orders")
            if order_type == "stop_limit" and limit_price is None:
                raise ValueError("Limit price is required for stop limit orders")
            if order_type == "stop":
                # A stop market order has no limit, whatever else was sent with it.
                limit_price = None
            if not _is_whole(qty):
                # The broker refuses a stop for part of a share. Accepted here,
                # practice would show a fractional position as protected in a way
                # the live account cannot protect it.
                return self._refused(_WHOLE_SHARES_ONLY, f"{requested_type} for {qty:g} {ticker}")
        else:
            # Only a stop has a trigger. Kept on another order, a stray stop price
            # would make it look like a stop in the order book.
            stop_price = None

        fill_price = None
        status = "pending"

        # Check execution eligibility. A stop never trades at placement, so it
        # needs no quote here.
        if order_type in ("market", "limit"):
            try:
                bid, ask = await self._get_live_bid_ask(ticker, strict=True)
            except LookupError as e:
                # Without a real quote the price helpers answer a made-up $100.
                # A market order filled at it would record a trade at a price that
                # never existed. A limit order rests instead, and the fill check
                # (strict too) fills it once a real quote reaches the limit.
                if order_type == "market":
                    return self._refused(
                        f"No live quote for {ticker} — market order not filled", str(e)
                    )
                logger.warning("No live quote for %s (%s); the limit order rests", ticker, e)
            else:
                quote = ask if side == "buy" else bid
                # A market order fills now; a limit order when the quote already
                # meets its limit.
                if order_type == "market" or _reaches_limit(side, quote, limit_price):  # type: ignore[arg-type]
                    fill_price = self._apply_slippage(side, quote)
                    status = "filled"

        # What the order may cost: its fill, else its limit, else (a stop market
        # order) its stop.
        if fill_price is not None:
            ref_price = fill_price
        elif limit_price is not None:
            ref_price = limit_price
        else:
            ref_price = stop_price
        order_cost = qty * ref_price  # type: ignore[operator]

        marks = await self._short_marks() if side == "buy" else {}

        order_id = f"sim_{uuid.uuid4().hex[:8]}"
        total_cost = order_cost if side == "buy" else -order_cost
        filled_at = datetime.now(UTC).isoformat() if status == "filled" else None

        # The checks run in the same write turn as the write. Run before it, two
        # orders placed at the same moment both passed on the same cash (or the
        # same shares) and both went through.
        async with self._transaction() as conn:
            refusal = await self._equity_order_refusal(
                conn, side=side, ticker=ticker, qty=qty, order_cost=order_cost, marks=marks
            )
            if refusal is None:
                # Create order record
                await conn.execute(
                    """
                    INSERT INTO sim_orders (
                        id, account_number, asset_type, ticker, side, order_type, quantity, limit_price,
                        stop_price, time_in_force, status, fill_price, filled_quantity, total_cost,
                        timestamp, filled_at
                    ) VALUES (?, ?, 'EQUITY', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
                    """,
                    (
                        order_id,
                        self.account_number,
                        ticker,
                        side,
                        order_type,
                        qty,
                        limit_price,
                        stop_price,
                        time_in_force,
                        status,
                        fill_price,
                        qty if status == "filled" else 0.0,
                        total_cost if status == "filled" else None,
                        filled_at,
                    ),
                )

                # A resting order leaves the cash alone: _buying_power holds back
                # what a resting buy may cost, and _shares_held_back the shares a
                # resting sell may sell.
                if status == "filled":
                    # Apply cash impact immediately
                    await conn.execute(
                        "UPDATE sim_accounts SET cash_balance = cash_balance - ?, updated_at = datetime('now') WHERE account_number = ?",
                        (total_cost, self.account_number),
                    )
                    # Update stock position
                    order_dict = {
                        "side": side,
                        "quantity": qty,
                        "ticker": ticker,
                        "asset_type": "EQUITY",
                        "option_id": None,
                    }
                    await self._update_position_on_fill(conn, order_dict, fill_price)  # type: ignore

        if refusal is not None:
            return self._refused(*refusal)

        logger.info(
            "Placed simulated equity order %s for %s (%s). Status: %s, Fill Price: %s",
            order_id,
            ticker,
            side.upper(),
            status,
            fill_price,
        )

        return {
            "data": self._order_view(
                {
                    "id": order_id,
                    "status": status,
                    "fill_price": fill_price,
                    "limit_price": limit_price,
                    "stop_price": stop_price,
                    "quantity": qty,
                    "filled_quantity": qty if status == "filled" else 0.0,
                    "ticker": ticker,
                    "side": side,
                    "order_type": order_type,
                    "time_in_force": time_in_force,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "filled_at": filled_at,
                }
            )
        }

    @staticmethod
    def _rejected_order(reason: str) -> dict[str, Any]:
        order_id = f"rej_{uuid.uuid4().hex[:8]}"
        return {
            "data": {
                "id": order_id,
                "order_id": order_id,
                "status": "rejected",
                "state": "rejected",
                "reason": reason,
            }
        }

    @classmethod
    def _refused(cls, reason: str, detail: str) -> dict[str, Any]:
        """The broker-shaped rejection for an order the sim refuses, logged with why."""
        rejected = cls._rejected_order(reason)
        logger.warning("Rejected order %s: %s (%s)", rejected["data"]["id"], reason, detail)
        return rejected

    async def _equity_order_refusal(
        self,
        conn: aiosqlite.Connection,
        *,
        side: str,
        ticker: str,
        qty: float,
        order_cost: float,
        marks: Mapping[int, float],
    ) -> tuple[str, str] | None:
        """Why the broker would refuse this stock order — (reason, detail) — or None.

        Runs inside the order's write turn (see place_equity_order).
        """
        if side == "buy":
            buying_power = await self._buying_power(conn, marks)
            if order_cost > buying_power:
                return (
                    "Insufficient buying power",
                    f"costs ${order_cost:,.2f} with ${buying_power:,.2f} of buying power",
                )
        elif side == "sell":
            # Only shares no resting sell holds back can be sold. Checked for every
            # sell: a plain sell for more than was held used to fill and open a
            # short, which a cash account can never hold.
            held = await self._held_shares(conn, ticker)
            held_back = await self._shares_held_back(conn, ticker)
            if qty > held - held_back + _QTY_TOLERANCE:
                return (
                    _NOT_ENOUGH_SHARES,
                    f"sells {qty:g} {ticker} with {held:g} held, "
                    f"{held_back:g} of them held back by resting sells",
                )
        return None

    async def _held_shares(self, conn: aiosqlite.Connection, ticker: str) -> float:
        async with conn.execute(
            "SELECT quantity FROM sim_positions WHERE account_number = ? AND asset_type = 'EQUITY' AND ticker = ?",
            (self.account_number, ticker),
        ) as cursor:
            row = await cursor.fetchone()
        return float(row["quantity"]) if row else 0.0

    async def _shares_held_back(self, conn: aiosqlite.Connection, ticker: str) -> float:
        """Shares of ``ticker`` that resting sell orders hold back.

        At the broker a resting sell (stop, stop-limit or limit) holds its shares
        until it fills or is cancelled, so a second sell for the same shares is
        refused; a cancel frees them again. The sim holds them back the same way.
        """
        async with conn.execute(
            """
            SELECT COALESCE(SUM(quantity), 0.0) AS held_back FROM sim_orders
            WHERE account_number = ? AND asset_type = 'EQUITY' AND ticker = ?
              AND side = 'sell' AND status = 'pending'
            """,
            (self.account_number, ticker),
        ) as cursor:
            row = await cursor.fetchone()
        return float(row["held_back"])

    async def review_equity_order(self, args: dict[str, Any]) -> dict[str, Any]:
        """Review/preview a simulated stock order without executing it."""
        ticker = args.get("symbol") or args.get("ticker")
        if not ticker:
            raise ValueError("Ticker symbol is required for stock order review")

        side = args.get("side", "buy").lower()
        qty = float(args.get("quantity", 0.0))

        if qty <= 0:
            raise ValueError("Order quantity must be positive")

        limit_price = args.get("limit_price") or args.get("price")
        if limit_price is not None:
            limit_price = float(limit_price)
        else:
            limit_price = await self._get_live_price(ticker)

        portfolio = await self.get_portfolio(self.account_number)
        buying_power = portfolio["data"]["buying_power"]["buying_power"]
        order_cost = qty * limit_price

        if side == "buy" and order_cost > buying_power:
            return {
                "data": {
                    "status": "rejected",
                    "state": "rejected",
                    "reason": "Insufficient buying power",
                }
            }

        return {
            "data": {
                "account_number": self.account_number,
                "symbol": ticker,
                "ticker": ticker,
                "side": side,
                "type": args.get("type", "limit").lower(),
                "quantity": str(qty),
                "limit_price": str(limit_price),
                "estimated_cost": str(order_cost),
                "estimated_principal_amount": str(order_cost),
                "fees": "0.00",
                "status": "approved",
            }
        }

    async def review_option_order(self, args: dict[str, Any]) -> dict[str, Any]:
        """Review/preview a simulated option order without executing it."""
        legs = args.get("legs") or []
        if not legs:
            raise ValueError("Option order has no legs")

        leg = legs[0]
        option_id = leg.get("option_id")
        side = leg.get("side", "buy").lower()

        qty = float(args.get("quantity", 1.0))
        limit_price = args.get("price") or args.get("limit_price")
        if limit_price is not None:
            limit_price = float(limit_price)
        else:
            limit_price = await self._get_live_option_price(option_id)

        portfolio = await self.get_portfolio(self.account_number)
        buying_power = portfolio["data"]["buying_power"]["buying_power"]
        order_cost = qty * limit_price * 100.0

        if side == "buy" and order_cost > buying_power:
            return {
                "data": {
                    "status": "rejected",
                    "state": "rejected",
                    "reason": "Insufficient buying power",
                }
            }

        return {
            "data": {
                "account_number": self.account_number,
                "legs": legs,
                "quantity": str(qty),
                "price": str(limit_price),
                "estimated_cost": str(order_cost),
                "fees": "0.00",
                "status": "approved",
            }
        }

    async def place_option_order(self, args: dict[str, Any]) -> dict[str, Any]:
        """Place and execute a simulated option order."""
        await self._process_pending_orders_and_expirations()

        legs = args.get("legs") or []
        if not legs:
            raise ValueError("Option order has no legs")

        leg = legs[0]
        option_id = leg.get("option_id")
        side = leg.get("side", "buy").lower()
        position_effect = leg.get("position_effect", "open").lower()

        # Try to resolve underlying ticker via cache
        from evotrader.agents.tools import OPTION_ID_TO_TICKER

        ticker = OPTION_ID_TO_TICKER.get(option_id, "OPTION")

        qty = float(args.get("quantity", 1.0))
        order_type = args.get("type", "limit").lower()
        limit_price = args.get("price") or args.get("limit_price")
        if limit_price is not None:
            limit_price = float(limit_price)

        if order_type not in ("market", "limit"):
            raise NotImplementedError(f"Order type {order_type} is not simulated for options")
        if order_type == "limit" and limit_price is None:
            raise ValueError("Limit price is required for limit option order")

        fill_price = None
        status = "pending"

        try:
            bid, ask = await self._get_live_option_bid_ask(option_id, strict=True)
        except LookupError as e:
            # Without a real quote the price helper answers a made-up $1.00 a
            # share: refused for a market order, resting for a limit order (see
            # place_equity_order).
            if order_type == "market":
                return self._refused(
                    f"No live quote for {option_id} — market order not filled", str(e)
                )
            logger.warning("No live quote for %s (%s); the limit order rests", option_id, e)
        else:
            quote = ask if side == "buy" else bid
            if order_type == "market" or _reaches_limit(side, quote, limit_price):  # type: ignore[arg-type]
                fill_price = self._apply_option_slippage(side, quote)
                status = "filled"

        # Option cost = qty * premium * multiplier
        ref_price = fill_price if fill_price is not None else limit_price
        order_cost = qty * ref_price * 100.0  # type: ignore

        # Retrieve contract metadata
        option_type = "call"
        strike = 0.0
        expiration = None

        if option_id:
            parsed = parse_occ_symbol(option_id)
            if parsed:
                option_type = parsed["option_type"]
                strike = parsed["strike"]
                expiration = parsed["expiration"]
                logger.debug(
                    f"Parsed OCC symbol {option_id} -> {option_type.upper()} {strike} Exp: {expiration}"
                )
            else:
                if self.real_mcp_toolset:
                    try:
                        session = await self.real_mcp_toolset._mcp_session_manager.create_session()
                        # Fetch option instruments to get precise details
                        res = await session.call_tool("get_option_instruments", {"ids": option_id})
                        if not getattr(res, "isError", False) and res.content:
                            data = json.loads(res.content[0].text)
                            results = data.get("data", {}).get("instruments", []) or []
                            if results:
                                inst = results[0]
                                option_type = inst.get("type", "call")
                                strike = float(inst.get("strike_price", 0.0))
                                expiration = inst.get("expiration_date")
                                ticker = inst.get("chain_symbol", ticker)
                    except Exception as e:
                        logger.debug("Failed to fetch option info: %s", e)

        marks = await self._short_marks() if side == "buy" else {}

        order_id = f"sim_{uuid.uuid4().hex[:8]}"
        total_cost = order_cost if side == "buy" else -order_cost

        # The buying-power check runs in the same write turn as the write, so two
        # buys placed at the same moment cannot both spend the same cash.
        refusal: tuple[str, str] | None = None
        async with self._transaction() as conn:
            if side == "buy":
                buying_power = await self._buying_power(conn, marks)
                if order_cost > buying_power:
                    refusal = (
                        "Insufficient buying power",
                        f"option order costs ${order_cost:,.2f} with ${buying_power:,.2f} "
                        "of buying power",
                    )
            if refusal is None:
                await conn.execute(
                    """
                    INSERT INTO sim_orders (
                        id, account_number, asset_type, ticker, option_id, side, order_type, quantity, limit_price,
                        status, fill_price, filled_quantity, total_cost, timestamp, filled_at,
                        option_type, strike, expiration, position_effect
                    ) VALUES (?, ?, 'OPTION', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, ?)
                    """,
                    (
                        order_id,
                        self.account_number,
                        ticker,
                        option_id,
                        side,
                        order_type,
                        qty,
                        limit_price,
                        status,
                        fill_price,
                        qty if status == "filled" else 0.0,
                        total_cost if status == "filled" else None,
                        datetime.now(UTC).isoformat() if status == "filled" else None,
                        option_type,
                        strike,
                        expiration,
                        position_effect,
                    ),
                )

                if status == "filled":
                    await conn.execute(
                        "UPDATE sim_accounts SET cash_balance = cash_balance - ?, updated_at = datetime('now') WHERE account_number = ?",
                        (total_cost, self.account_number),
                    )
                    order_dict = {
                        "side": side,
                        "quantity": qty,
                        "ticker": ticker,
                        "asset_type": "OPTION",
                        "option_id": option_id,
                        "option_type": option_type,
                        "strike": strike,
                        "expiration": expiration,
                    }
                    await self._update_position_on_fill(conn, order_dict, fill_price)  # type: ignore

        if refusal is not None:
            return self._refused(*refusal)

        logger.info(
            "Placed simulated option order %s for %s (%s). Status: %s, Fill Price: %s",
            order_id,
            option_id,
            side.upper(),
            status,
            fill_price,
        )

        return {
            "data": {
                "id": order_id,
                "order_id": order_id,
                "status": status,
                "state": status,
                "price": str(fill_price) if fill_price is not None else str(limit_price),
                "quantity": str(qty),
                "symbol": ticker,
                "ticker": ticker,
                "side": side,
                "type": order_type,
                "created_at": datetime.now(UTC).isoformat(),
                "filled_at": datetime.now(UTC).isoformat() if status == "filled" else None,
            }
        }

    async def _update_position_on_fill(
        self, conn: aiosqlite.Connection, order: dict[str, Any], fill_price: float
    ) -> None:
        """Update the positions table following a successful fill transaction."""
        if not isinstance(order, dict):
            order = dict(order)
        ticker = order["ticker"]
        asset_type = order["asset_type"]
        option_id = order["option_id"]
        qty = float(order["quantity"])
        side = order["side"]

        # Fetch current position
        async with conn.execute(
            """
            SELECT * FROM sim_positions
            WHERE account_number = ? AND asset_type = ? AND ticker = ? AND (option_id = ? OR (option_id IS NULL AND ? IS NULL))
            """,
            (self.account_number, asset_type, ticker, option_id, option_id),
        ) as cursor:
            pos_row = await cursor.fetchone()

        if pos_row:
            current_qty = pos_row["quantity"]
            current_avg = pos_row["avg_cost_basis"]

            if side == "buy":
                new_qty = current_qty + qty
                if new_qty > 0 and current_qty > 0:
                    new_avg = ((current_qty * current_avg) + (qty * fill_price)) / new_qty
                elif new_qty < 0 and current_qty < 0:
                    # buying back a partial short: avg cost stays the same
                    new_avg = current_avg
                else:
                    # crossed zero
                    new_avg = fill_price
            else:
                new_qty = current_qty - qty
                if new_qty < 0 and current_qty < 0:
                    # shorting more
                    new_avg = ((abs(current_qty) * current_avg) + (qty * fill_price)) / abs(new_qty)
                elif new_qty > 0 and current_qty > 0:
                    # selling partial long
                    new_avg = current_avg
                else:
                    # crossed zero
                    new_avg = fill_price

            if abs(new_qty) < 1e-4:
                # Close out position
                await conn.execute(
                    "DELETE FROM sim_positions WHERE id = ?",
                    (pos_row["id"],),
                )
            else:
                await conn.execute(
                    "UPDATE sim_positions SET quantity = ?, avg_cost_basis = ?, last_updated = datetime('now') WHERE id = ?",
                    (new_qty, new_avg, pos_row["id"]),
                )
        else:
            # Create new position
            if side == "buy":
                position_qty = qty
            else:
                position_qty = -qty

            await conn.execute(
                """
                INSERT INTO sim_positions (
                    account_number, asset_type, ticker, option_id, quantity, avg_cost_basis,
                    option_type, strike, expiration
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.account_number,
                    asset_type,
                    ticker,
                    option_id,
                    position_qty,
                    fill_price,
                    order.get("option_type"),
                    order.get("strike"),
                    order.get("expiration"),
                ),
            )

    # ── Cancellations & Querying ─────────────────────────────────────────

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel a pending simulated order (limit or stop)."""
        async with self._transaction() as conn:
            # Only a resting order can be cancelled, decided inside the write so
            # a fill landing at the same moment cannot be overwritten.
            cursor = await conn.execute(
                "UPDATE sim_orders SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
                (order_id,),
            )
            cancelled = cursor.rowcount == 1

        if not cancelled:
            logger.warning("Order %s not found or already filled/cancelled", order_id)
            return {
                "data": {
                    "id": order_id,
                    "status": "failed",
                    "state": "failed",
                    "message": "Order not found or not pending",
                }
            }

        logger.info("Cancelled simulated order %s", order_id)
        return {
            "data": {
                "id": order_id,
                "order_id": order_id,
                "status": "cancelled",
                "state": "cancelled",
            }
        }

    async def get_orders(self, args: dict[str, Any]) -> dict[str, Any]:
        """Get simulated order list mimicking Robinhood's shape."""
        await self._process_pending_orders_and_expirations()

        conn = await self._get_conn()

        # Build query filter based on arguments if any
        query = "SELECT * FROM sim_orders WHERE account_number = ?"
        params = [self.account_number]

        # A lookup of one order, as reconciliation and the executor do it at the
        # broker. Ignoring it returned every order, newest first.
        order_id_filter = args.get("order_id") or args.get("id")
        if order_id_filter:
            query += " AND id = ?"
            params.append(order_id_filter)

        status_filter = args.get("status")
        if status_filter:
            query += " AND status = ?"
            params.append(status_filter)

        ticker_filter = args.get("ticker") or args.get("symbol")
        if ticker_filter:
            query += " AND ticker = ?"
            params.append(ticker_filter)

        query += " ORDER BY timestamp DESC LIMIT 100"

        async with conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()

        return {"data": {"results": [self._order_view(r) for r in rows]}}

    async def settled_order(self, order_id: str) -> dict[str, Any] | None:
        """One order as the fill check reports fills, or None if the sim never saw it.

        Settles nothing: it is how the cycle catches up with fills and cancels
        the sim made during other calls (see check_and_journal_pending_fills).
        """
        conn = await self._get_conn()
        async with conn.execute("SELECT * FROM sim_orders WHERE id = ?", (order_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "order_id": row["id"],
            "ticker": row["ticker"],
            "asset_type": row["asset_type"],
            "option_id": row["option_id"],
            "side": row["side"],
            "quantity": row["filled_quantity"] or row["quantity"],
            "limit_price": row["limit_price"],
            "fill_price": row["fill_price"],
            "total_cost": row["total_cost"],
            "status": row["status"],
            "time_in_force": row["time_in_force"],
            "option_type": row["option_type"],
            "strike": row["strike"],
            "expiration": row["expiration"],
            "position_effect": row["position_effect"],
        }

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        """Get status of a specific order."""
        await self._process_pending_orders_and_expirations()

        conn = await self._get_conn()
        async with conn.execute("SELECT * FROM sim_orders WHERE id = ?", (order_id,)) as cursor:
            r = await cursor.fetchone()

        if not r:
            return {"data": None}

        return {"data": self._order_view(r)}

    @staticmethod
    def _order_view(order: Mapping[str, Any]) -> dict[str, Any]:
        """One order, shaped as the broker's order tools show it.

        The broker lists a stop as a market order (a stop-limit as a limit order)
        with ``trigger: "stop"``, its ``stop_price`` and no other price, and
        reports what has filled as ``cumulative_quantity``. The executor reads
        those fields to see what protection is resting, so practice shows them
        the same way.
        """
        stored_type = order["order_type"]
        stop_price = order["stop_price"]
        price = order["fill_price"] if order["fill_price"] is not None else order["limit_price"]
        return {
            "id": order["id"],
            "order_id": order["id"],
            "status": order["status"],
            "state": order["status"],
            "price": str(price) if price is not None else None,
            "stop_price": str(stop_price) if stop_price is not None else None,
            "quantity": str(order["quantity"]),
            "cumulative_quantity": str(order["filled_quantity"] or 0.0),
            "symbol": order["ticker"],
            "ticker": order["ticker"],
            "side": order["side"],
            "type": _SHOWN_ORDER_TYPE.get(stored_type, stored_type),
            "trigger": "stop" if stop_price is not None else "immediate",
            "time_in_force": order["time_in_force"],
            "created_at": order["timestamp"],
            "filled_at": order["filled_at"],
        }
