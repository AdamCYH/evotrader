"""Position reconciliation engine.

Reconciles the agent's SQLite Trade Journal with real-time broker holdings.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from evotrader.db.journal import TradeJournal
from evotrader.models.mcp import McpEquityQuote, McpOptionQuote, McpPosition
from evotrader.models.trade import (
    OrderResult,
    OrderStatus,
    OrderType,
    TradeAction,
    TradeDirection,
    TradeProposal,
)
from evotrader.utils import select_agentic_account

logger = logging.getLogger(__name__)


def root_cause(exc: BaseException) -> BaseException:
    """The innermost error, through exception groups and "raised from" chains."""
    seen: set[int] = set()
    while id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
            exc = exc.exceptions[0]
        elif exc.__cause__ is not None:
            exc = exc.__cause__
        elif exc.__context__ is not None and not exc.__suppress_context__:
            exc = exc.__context__
        else:
            break
    return exc


class ReconciliationService:
    """Synchronises Trade Journal positions with live Robinhood holdings."""

    def __init__(self, journal: TradeJournal, mcp_toolset: Any, dry_run: bool = True) -> None:
        """Initialize service.

        Args:
            journal: Database trade journal.
            mcp_toolset: Configured MCP client toolset.
            dry_run: Whether the system is in dry run (paper trading) mode.
        """
        self._journal = journal
        self._mcp_toolset = mcp_toolset
        self._dry_run = dry_run

    async def get_broker_positions(self, account_number: str | None = None) -> list[dict[str, Any]]:
        """Fetch all stock and option positions from the broker.

        Args:
            account_number: Optional specific account number. If not provided,
                will auto-discover from accounts list.
        """
        if not self._mcp_toolset:
            logger.warning("Reconciliation: MCP toolset not available.")
            return []

        try:
            session = await self._mcp_toolset._mcp_session_manager.create_session()

            # Resolve account number if not provided
            if not account_number:
                accounts_res = await session.call_tool("get_accounts", arguments={})
                # Check for isError or invalid format
                if getattr(accounts_res, "isError", False) or not accounts_res.content:
                    logger.error("Failed to list accounts from broker")
                    return []

                accounts_data = json.loads(accounts_res.content[0].text)
                accounts_list = accounts_data.get("data", {}).get("accounts", [])
                account_number = select_agentic_account(accounts_list)

            if not account_number:
                logger.error("No account number could be resolved.")
                return []

            logger.info("Fetching positions for account: %s", account_number)

            # Fetch equities
            equity_pos = []
            pos_res = await session.call_tool(
                "get_equity_positions", arguments={"account_number": account_number}
            )
            if not getattr(pos_res, "isError", False) and pos_res.content:
                pos_data = json.loads(pos_res.content[0].text)
                equity_pos = pos_data.get("data", {}).get("positions", []) or []

            # Fetch options
            option_pos = []
            opt_res = await session.call_tool(
                "get_option_positions", arguments={"account_number": account_number}
            )
            if not getattr(opt_res, "isError", False) and opt_res.content:
                opt_data = json.loads(opt_res.content[0].text)
                option_pos = opt_data.get("data", {}).get("positions", []) or []

            # Unify both lists using McpPosition model.
            # Filter out zero-quantity positions — Robinhood returns
            # historical closed lots (qty=0) alongside active ones.
            unified = []
            for p in equity_pos:
                pos = McpPosition.from_dict(p)
                if abs(pos.quantity) < 1e-8:
                    continue
                # Keep dict output for backward compatibility, mapping back to average_buy_price for UI
                d = pos.model_dump()
                d["average_buy_price"] = d.pop("average_price", 0.0)
                unified.append(d)

            for p in option_pos:
                pos = McpPosition.from_dict(p)
                if abs(pos.quantity) < 1e-8:
                    continue
                d = pos.model_dump()
                d["average_buy_price"] = d.pop("average_price", 0.0)
                unified.append(d)

            return unified

        except Exception as e:
            # One line with the cause: the traceback through the broker client's
            # task groups ran to about 100 lines for a sign-in that timed out.
            cause = root_cause(e)
            logger.error("Failed to load broker positions: %s", str(cause) or type(cause).__name__)
            logger.debug("Failed to load broker positions", exc_info=True)
            return []

    async def get_current_stock_price(self, ticker: str) -> float | None:
        """Fetch current stock price from broker quotes."""
        if not self._mcp_toolset:
            return None
        try:
            session = await self._mcp_toolset._mcp_session_manager.create_session()
            quote_res = await session.call_tool(
                "get_equity_quotes", arguments={"symbols": [ticker]}
            )
            if getattr(quote_res, "isError", False) or not quote_res.content:
                return None
            data = json.loads(quote_res.content[0].text)
            results = data.get("data", {}).get("results", [])
            if results:
                # Standard Robinhood MCP quote structure nests attributes inside "quote"
                q = results[0].get("quote") or results[0]
                quote = McpEquityQuote(**q)
                return quote.resolve_live_price()

            return None
        except Exception as e:
            logger.warning("Could not fetch stock price for %s: %s", ticker, e)
            return None

    async def get_current_option_price(self, option_id: str) -> float | None:
        """Fetch current option premium price from broker quotes."""
        if not self._mcp_toolset:
            return None
        try:
            session = await self._mcp_toolset._mcp_session_manager.create_session()
            quote_res = await session.call_tool(
                "get_option_quotes", arguments={"instrument_ids": [option_id]}
            )
            if getattr(quote_res, "isError", False) or not quote_res.content:
                return None
            data = json.loads(quote_res.content[0].text)
            results = data.get("data", {}).get("results", [])
            if results:
                # Option quote attributes are nested inside "quote"
                q = results[0].get("quote") or results[0]
                quote = McpOptionQuote(**q)
                return quote.resolve_live_price()
            return None
        except Exception as e:
            logger.warning("Could not fetch option price for %s: %s", option_id, e)
            return None

    async def reconcile_positions(
        self, account_number: str | None = None, ticker: str | None = None
    ) -> dict[str, Any]:
        """Verify and synchronise database state with real broker state.

        ``ticker`` is a FOCUS HINT, not a filter. Scoping the comparison to one
        configured ticker orphaned every open QQQ row the moment
        ``asset.primary_ticker`` moved QQQ -> MSTR on 2026-09-10: the open QQQ
        rows were filtered out of BOTH sides of the comparison, so they could
        never drift-resolve and were still reported open on 2026-09-14 — ten
        days after the broker stopped holding them, injecting phantom inventory
        into agent context every cycle.

        The failure was silent by construction: reconciliation returned
        IN_SYNC for the configured ticker and was telling the truth about it,
        while nothing in the payload hinted that the QQQ shares had never been
        examined.

        So reconcile the UNION of what the broker holds and what the journal
        believes it holds. A position the system has forgotten how to look at
        is precisely the position that needs looking at.

        Args:
            account_number: The account to check.
            ticker: Focus ticker for the headline fields and logging. Never
                used to exclude anything from the comparison.

        Returns:
            Dict with the focus ticker's headline status plus ``per_ticker``
            (every ticker examined) and ``tickers_examined``.
        """
        # Fetched ONCE and passed down — extra tickers add no broker traffic.
        broker_positions = await self.get_broker_positions(account_number)
        open_db_trades = await self._journal.get_open_trades()
        pending_trades = await self._journal.get_trades_by_order_status("PENDING")

        symbols = {(p.get("symbol") or "").upper() for p in broker_positions}
        symbols |= {(t.get("ticker") or "").upper() for t in open_db_trades}
        symbols |= {(t.get("ticker") or "").upper() for t in pending_trades}
        symbols.discard("")
        focus = (ticker or "").upper()
        if focus:
            symbols.add(focus)

        logger.info(
            "Running position reconciliation over %d ticker(s): %s (focus: %s)",
            len(symbols),
            sorted(symbols),
            ticker,
        )

        per_ticker: dict[str, dict[str, Any]] = {}
        for sym in sorted(symbols):
            per_ticker[sym] = await self._reconcile_one_ticker(
                sym,
                broker_positions,
                open_db_trades,
                pending_trades,
            )
            if sym != focus and per_ticker[sym].get("status") != "IN_SYNC":
                logger.warning(
                    "[RECONCILE] Drift found in NON-FOCUS ticker %s: %s. This "
                    "instrument is no longer the configured primary but still "
                    "has journal rows.",
                    sym,
                    per_ticker[sym].get("status"),
                )

        focus_result = per_ticker.get(focus)
        if focus_result is not None:
            merged = dict(focus_result)
        else:
            merged = {
                "status": "IN_SYNC",
                "ticker": ticker,
                "broker_qty": 0.0,
                "db_qty": 0.0,
                "message": "No positions for the focus ticker.",
            }
        merged["per_ticker"] = per_ticker
        merged["tickers_examined"] = sorted(symbols)

        # A clean focus ticker must never present as a clean ACCOUNT. If any
        # other ticker moved, say so in the headline status — the whole defect
        # this guards against was IN_SYNC meaning "in sync for the one ticker I
        # happened to look at".
        if merged.get("status") == "IN_SYNC" and any(
            v.get("status") != "IN_SYNC" for k, v in per_ticker.items() if k != focus
        ):
            merged["status"] = "RECONCILED_OTHER_TICKER"
            merged["message"] = (
                "Focus ticker in sync; drift was found and handled in "
                + ", ".join(
                    k for k, v in per_ticker.items() if k != focus and v.get("status") != "IN_SYNC"
                )
                + "."
            )
        return merged

    async def get_order_state(self, order_id: str) -> str | None:
        """The broker's own word for one order, lower-cased, or None.

        None means the question could not be answered — network, a purged
        order, an id the broker does not recognise. A caller must treat that
        as "unknown", never as "not filled": voiding a row on a failed lookup
        would delete a real exit.
        """
        if not self._mcp_toolset or not order_id:
            return None
        try:
            session = await self._mcp_toolset._mcp_session_manager.create_session()
            res = await session.call_tool("get_equity_orders", arguments={"order_id": order_id})
            if getattr(res, "isError", False) or not res.content:
                return None
            data = json.loads(res.content[0].text).get("data", {}) or {}
            orders = data.get("orders") or data.get("results") or []
            if not orders:
                return None
            return str(orders[0].get("state") or "").lower() or None
        except Exception as e:
            logger.warning("Could not fetch broker state for order %s: %s", order_id, e)
            return None

    async def _void_unfilled_close_rows(
        self, ticker: str, db_equity_trades: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Walk back close rows whose broker order never filled.

        The sync used to reach straight for synthetic offsetting trades, which
        compounds a journalling mistake instead of correcting it. On 2026-09-25
        four rows recorded exits that had not happened (a stop the executor
        called FILLED while the broker held it unconfirmed, plus the phantom
        short the flip path built from it); the 16:00 ET sync then wrote two more
        rows to make the books balance and rebased the cost basis to the current
        mark.

        So: ask the broker about every close row's order first. Where the broker
        says the order is still working or was cancelled, the row is the error —
        correct it, and most of the drift disappears without synthesising
        anything. Only genuinely unexplained drift reaches the offset path.

        Returns the rows that were voided, so the caller can exclude them.
        """
        voided: list[dict[str, Any]] = []
        try:
            recent = await self._journal.get_recent_trades(limit=60, ticker=ticker)
        except Exception as e:
            logger.warning("[RECONCILE] Could not read recent trades for %s: %s", ticker, e)
            return voided

        close_actions = {"CLOSE", "STOP_LOSS", "TAKE_PROFIT"}
        candidates: dict[str, list[dict[str, Any]]] = {}
        for row in recent:
            if str(row.get("action") or "").upper() not in close_actions:
                continue
            if str(row.get("order_status") or "").upper() != "FILLED":
                continue
            order_id = row.get("order_id")
            if not order_id:
                continue
            candidates.setdefault(str(order_id), []).append(row)

        for order_id, rows in candidates.items():
            state = await self.get_order_state(order_id)
            if state is None or state == "filled":
                continue  # unknown, or genuinely filled — leave it alone
            if state in ("unconfirmed", "confirmed", "queued", "partially_filled"):
                new_status = "PENDING"
            elif state in ("cancelled", "canceled"):
                new_status = "CANCELLED"
            elif state in ("rejected", "failed"):
                new_status = "FAILED"
            else:
                continue
            try:
                await self._journal.update_order_status(
                    order_id=order_id,
                    new_status=new_status,
                    broker_status_reason=(
                        f"VOIDED_BY_SYNC: journaled FILLED but the broker reports '{state}'"
                    ),
                )
            except Exception as e:
                logger.error("[RECONCILE] Could not void order %s: %s", order_id, e)
                continue
            voided.extend(rows)
            logger.warning(
                "[RECONCILE] Order %s was journaled FILLED but the broker reports "
                "'%s' — %d row(s) set to %s and their P&L cleared before drift "
                "was computed.",
                order_id,
                state,
                len(rows),
                new_status,
            )
        return voided

    async def _reconcile_one_ticker(
        self,
        ticker: str,
        broker_positions: list[dict[str, Any]],
        open_db_trades: list[dict[str, Any]],
        pending_trades: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Reconcile a single ticker against already-fetched broker state.

        Broker positions, open trades and pending trades are passed in rather
        than fetched here, so driving N tickers costs exactly the same broker
        traffic as driving one.
        """
        _t = (ticker or "").upper()

        # Filter broker positions matching this underlying ticker
        broker_ticker_positions = [
            p for p in broker_positions if (p.get("symbol") or "").upper() == _t
        ]

        # Local DB positions for this underlying ticker
        ticker_db_trades = [t for t in open_db_trades if (t.get("ticker") or "").upper() == _t]

        # PENDING trades — journaled at submission but not yet broker-confirmed.
        # If the broker shows a position matching a PENDING trade, promote it to
        # FILLED rather than creating a reconciliation entry.
        pending_by_option_id: dict[str, list[dict]] = {}
        pending_equity: list[dict] = []
        for pt in pending_trades:
            if (pt.get("ticker") or "").upper() != _t:
                continue
            oid = pt.get("option_id")
            if oid:
                pending_by_option_id.setdefault(oid, []).append(pt)
            else:
                pending_equity.append(pt)

        # 3. Separate into Equity and Options groups
        # 3a. Equity group
        broker_equity_pos = next(
            (p for p in broker_ticker_positions if p["asset_type"] == "EQUITY"), None
        )
        broker_equity_qty = broker_equity_pos["quantity"] if broker_equity_pos else 0.0
        broker_equity_cost = broker_equity_pos["average_buy_price"] if broker_equity_pos else 0.0

        db_equity_trades = [t for t in ticker_db_trades if t.get("option_id") is None]

        # Correct the journal before measuring it against the broker. A close
        # row whose order never filled is a journalling error, not drift, and
        # synthesising an offset for it records a second untrue trade on top of
        # the first. Live only: in sim there is no broker order to ask about.
        voided_rows: list[dict[str, Any]] = []
        if not self._dry_run:
            voided_rows = await self._void_unfilled_close_rows(_t, db_equity_trades)
            if voided_rows:
                # The voided closes restored quantity to their lots, so the open
                # position has changed underneath us — re-read it.
                try:
                    refreshed = await self._journal.get_open_trades()
                    ticker_db_trades = [
                        t for t in refreshed if (t.get("ticker") or "").upper() == _t
                    ]
                    db_equity_trades = [t for t in ticker_db_trades if t.get("option_id") is None]
                except Exception as e:
                    logger.warning("[RECONCILE] Could not re-read open trades after voiding: %s", e)

        db_equity_qty = sum(
            float(t.get("remaining_quantity", 0.0))
            if t.get("direction") == "LONG"
            else -float(t.get("remaining_quantity", 0.0))
            for t in db_equity_trades
        )

        # 3b. Options groups (keyed by option_id)
        broker_option_positions = {
            p["option_id"]: p for p in broker_ticker_positions if p["asset_type"] == "OPTION"
        }
        db_option_trades_by_id = {}
        for t in ticker_db_trades:
            opt_id = t.get("option_id")
            if opt_id:
                db_option_trades_by_id.setdefault(opt_id, []).append(t)

        # All unique option IDs to check
        all_option_ids = set(broker_option_positions.keys()) | set(db_option_trades_by_id.keys())

        # Track reconciliation status & mismatches
        mismatches = []
        promoted_pending = 0

        # Check Equity Drift
        equity_diff = broker_equity_qty - db_equity_qty
        if abs(equity_diff) >= 1e-4:
            # Check if PENDING equity trades explain the drift
            pending_eq_qty = sum(
                float(pt.get("quantity", 0))
                if pt.get("direction") == "LONG"
                else -float(pt.get("quantity", 0))
                for pt in pending_equity
            )
            if abs(equity_diff - pending_eq_qty) < 1e-4 and pending_equity:
                # Drift is fully explained by PENDING trades — promote them
                for pt in pending_equity:
                    order_id = pt.get("order_id")
                    if order_id:
                        await self._journal.update_order_status(
                            order_id=order_id,
                            new_status="FILLED",
                            fill_price=broker_equity_cost,
                        )
                        promoted_pending += 1
                        logger.info(
                            "[RECONCILE] Promoted PENDING equity trade (order=%s) to FILLED",
                            order_id,
                        )
            else:
                mismatches.append(
                    {
                        "asset_type": "EQUITY",
                        "option_id": None,
                        "broker_qty": broker_equity_qty,
                        "db_qty": db_equity_qty,
                        "broker_cost": broker_equity_cost,
                        "trades": db_equity_trades,
                    }
                )
        else:
            # Quantities match — verify fill-price integrity.
            # A journaled fill_price sourced from a limit price can differ
            # materially from the true broker fill (7/17 incident: journaled
            # ~40% above the actual fill, producing phantom P&L).
            await self._verify_fill_prices(
                db_equity_trades,
                broker_equity_cost,
            )

        # Check Options Drift per option contract
        for opt_id in all_option_ids:
            b_opt = broker_option_positions.get(opt_id)
            b_qty = b_opt["quantity"] if b_opt else 0.0
            b_cost = b_opt["average_buy_price"] if b_opt else 0.0

            db_trades = db_option_trades_by_id.get(opt_id, [])
            d_qty = sum(
                float(t.get("remaining_quantity", 0.0))
                if t.get("direction") == "LONG"
                else -float(t.get("remaining_quantity", 0.0))
                for t in db_trades
            )

            opt_diff = b_qty - d_qty
            if abs(opt_diff) >= 1e-4:
                # Check if PENDING trades for this option explain the drift
                pending_for_opt = pending_by_option_id.get(opt_id, [])
                pending_opt_qty = sum(
                    float(pt.get("quantity", 0))
                    if pt.get("direction") == "LONG"
                    else -float(pt.get("quantity", 0))
                    for pt in pending_for_opt
                )
                if abs(opt_diff - pending_opt_qty) < 1e-4 and pending_for_opt:
                    # Drift is fully explained by PENDING trades — promote them
                    for pt in pending_for_opt:
                        order_id = pt.get("order_id")
                        if order_id:
                            await self._journal.update_order_status(
                                order_id=order_id,
                                new_status="FILLED",
                                fill_price=b_cost,
                            )
                            promoted_pending += 1
                            logger.info(
                                "[RECONCILE] Promoted PENDING option trade (order=%s, option=%s) to FILLED",
                                order_id,
                                opt_id,
                            )
                else:
                    mismatches.append(
                        {
                            "asset_type": "OPTION",
                            "option_id": opt_id,
                            "broker_qty": b_qty,
                            "db_qty": d_qty,
                            "broker_cost": b_cost,
                            "trades": db_trades,
                            "option_type": b_opt["option_type"]
                            if b_opt
                            else db_trades[0].get("option_type")
                            if db_trades
                            else None,
                            "strike": b_opt["strike"]
                            if b_opt
                            else db_trades[0].get("strike")
                            if db_trades
                            else None,
                            "expiration": b_opt["expiration"]
                            if b_opt
                            else db_trades[0].get("expiration")
                            if db_trades
                            else None,
                        }
                    )
            else:
                # Quantities match — verify fill-price integrity.
                await self._verify_fill_prices(db_trades, b_cost)

        logger.info(
            "Reconciliation Check: Equity Broker = %.4f, DB = %.4f | Options Mismatches = %d | Promoted PENDING = %d",
            broker_equity_qty,
            db_equity_qty,
            len([m for m in mismatches if m["asset_type"] == "OPTION"]),
            promoted_pending,
        )

        # If no mismatches, we are in sync!
        if not mismatches:
            return {
                "status": "IN_SYNC",
                "broker_qty": broker_equity_qty,
                "db_qty": db_equity_qty,
                "ticker": ticker,
                "message": "Broker and database are in sync.",
            }

        # 4. Handle Mismatch
        if self._dry_run:
            logger.warning(
                "Position drift detected! Mismatches: %s. "
                "Ignored because dry_run (paper trading) is active.",
                mismatches,
            )
            return {
                "status": "DRIFT_IGNORED_PAPER",
                "broker_qty": broker_equity_qty,
                "db_qty": db_equity_qty,
                "ticker": ticker,
                "message": "Drift detected but ignored due to paper trading (dry_run=true).",
                "mismatches": [
                    {
                        "asset_type": m["asset_type"],
                        "option_id": m["option_id"],
                        "broker_qty": m["broker_qty"],
                        "db_qty": m["db_qty"],
                    }
                    for m in mismatches
                ],
            }

        # Live trading drift resolution
        logger.warning("Position drift detected in LIVE trading! Resolving...")
        reconciled_trades_count = 0

        for m in mismatches:
            diff = m["broker_qty"] - m["db_qty"]
            if abs(diff) < 1e-4:
                continue

            # Fetch current price for close calculation
            if m["asset_type"] == "EQUITY":
                live_price = await self.get_current_stock_price(ticker)
            else:
                live_price = await self.get_current_option_price(m["option_id"])
            price_valid = live_price is not None and live_price > 0
            broker_cost_valid = m["broker_cost"] is not None and m["broker_cost"] > 0
            if price_valid:
                current_price = live_price
            elif broker_cost_valid:
                current_price = m["broker_cost"]
                logger.warning(
                    "[RECONCILE] Live quote unavailable; using broker cost %.4f (P&L approximate).",
                    current_price,
                )
            else:
                logger.error(
                    "[RECONCILE] No valid price for %s/%s during drift resolution — deferring this mismatch "
                    "to the next cycle rather than booking sentinel-priced sync trades.",
                    ticker,
                    m.get("option_id"),
                )
                continue  # defer; do not fabricate P&L

            remaining_diff = diff

            if remaining_diff < 0:
                # Need to DECREASE net position
                long_trades = [t for t in m["trades"] if t.get("direction") == "LONG"]
                for db_trade in long_trades:
                    if remaining_diff >= -1e-4:
                        break

                    db_trade_id = db_trade.get("id")
                    db_trade_qty = float(
                        db_trade.get("remaining_quantity") or db_trade.get("quantity", 0.0)
                    )
                    close_qty = min(db_trade_qty, abs(remaining_diff))

                    await self._record_sync_trade(
                        ticker, "LONG", "CLOSE", close_qty, current_price, db_trade_id, m, db_trade
                    )
                    remaining_diff += close_qty
                    reconciled_trades_count += 1

                if remaining_diff < -1e-4:
                    await self._record_sync_trade(
                        ticker, "SHORT", "OPEN", abs(remaining_diff), current_price, None, m
                    )
                    remaining_diff = 0.0
                    reconciled_trades_count += 1

            elif remaining_diff > 0:
                # Need to INCREASE net position
                short_trades = [t for t in m["trades"] if t.get("direction") == "SHORT"]
                for db_trade in short_trades:
                    if remaining_diff <= 1e-4:
                        break

                    db_trade_id = db_trade.get("id")
                    db_trade_qty = float(
                        db_trade.get("remaining_quantity") or db_trade.get("quantity", 0.0)
                    )
                    close_qty = min(db_trade_qty, remaining_diff)

                    await self._record_sync_trade(
                        ticker, "SHORT", "CLOSE", close_qty, current_price, db_trade_id, m, db_trade
                    )
                    remaining_diff -= close_qty
                    reconciled_trades_count += 1

                if remaining_diff > 1e-4:
                    await self._record_sync_trade(
                        ticker, "LONG", "OPEN", remaining_diff, current_price, None, m
                    )
                    remaining_diff = 0.0
                    reconciled_trades_count += 1

        if reconciled_trades_count > 0:
            return {
                "status": "RECONCILED",
                "broker_qty": broker_equity_qty,
                "db_qty": broker_equity_qty,
                "ticker": ticker,
                "reconciled_trades_count": reconciled_trades_count,
                "message": f"Successfully synchronized database. Recorded {reconciled_trades_count} sync trades.",
            }

        return {
            "status": "IN_SYNC",
            "broker_qty": broker_equity_qty,
            "db_qty": db_equity_qty,
            "ticker": ticker,
            "message": "Drift resolved but no sync trades were necessary.",
        }

    async def _verify_fill_prices(
        self,
        db_trades: list[dict[str, Any]],
        broker_cost: float,
    ) -> None:
        """Verify fill-price integrity for positions where quantities match.

        When the broker's average_buy_price differs from the journal's
        fill_price/price by more than 1%, correct the stale price via
        update_order_status() with an audit reason.

        This catches the case where a trade was journaled at the limit price
        and the broker later reports a materially different actual fill.
        """
        if broker_cost is None or broker_cost <= 0:
            return

        for t in db_trades:
            j_fill = t.get("fill_price") or t.get("price")
            order_id = t.get("order_id")
            if (
                j_fill is not None
                and order_id
                and abs(float(j_fill) - broker_cost) / broker_cost > 0.01
            ):
                old_price = float(j_fill)
                await self._journal.update_order_status(
                    order_id=order_id,
                    new_status=t.get("order_status") or "FILLED",
                    fill_price=broker_cost,
                    broker_status_reason=(
                        f"FILL_PRICE_CORRECTED from {old_price:.4f} "
                        f"to {broker_cost:.4f} by reconciliation"
                    ),
                )
                logger.warning(
                    "[RECONCILE] Corrected stale fill price for trade %s: %.4f -> %.4f",
                    t.get("id"),
                    old_price,
                    broker_cost,
                )

    async def _record_sync_trade(
        self,
        ticker: str,
        direction: str,
        action: str,
        quantity: float,
        price: float,
        related_trade_id: Any,
        m: dict[str, Any],
        original_trade: dict[str, Any] | None = None,
        price_reliable: bool = True,
    ) -> None:
        """Helper to record a sync trade in the journal.

        Args:
            price_reliable: When False, downstream journal logic stores
                realized_pnl=NULL so fabricated P&L can never enter
                performance metrics or agent context.
        """
        if not price_reliable:
            logger.warning(
                "[RECONCILE] Recording sync trade with NULL realized P&L (price unreliable)."
            )

        # ── AN OPEN IS RECORDED AT COST, NOT AT THE MARK ─────────────────
        # A sync OPEN says "the broker holds shares the journal did not know
        # about". Their cost basis is a fact the broker already told us
        # (average_buy_price, captured as m['broker_cost']), so recording them
        # at today's price silently rebases the position. On 2026-09-25 that
        # moved MSTR's basis down to the current mark, which changed the
        # resting stop from 1.65 ATR below entry to 0.98, and the 17:00
        # ET agent had to reason its way past the artifact before it could size
        # anything. The mark stays only as a fallback, and then the row says so
        # and carries no P&L.
        reason = "System Sync: manual reconciliation to match broker position"
        if action == "OPEN":
            broker_cost = m.get("broker_cost")
            try:
                broker_cost = float(broker_cost) if broker_cost is not None else 0.0
            except (TypeError, ValueError):
                broker_cost = 0.0
            if broker_cost > 0:
                price = broker_cost
                reason = f"System Sync: OPEN at broker average cost {broker_cost:.4f}"
            else:
                price_reliable = False
                reason = (
                    "System Sync: OPEN at the current mark — the broker reported "
                    "no average cost, so this is NOT a cost basis and P&L is NULL"
                )
                logger.warning(
                    "[RECONCILE] Sync OPEN for %s has no broker average cost; "
                    "recording at the mark with P&L NULL.",
                    ticker,
                )

        proposal = TradeProposal(
            ticker=ticker,
            direction=TradeDirection(direction),
            action=TradeAction(action),
            quantity=quantity,
            order_type=OrderType.MARKET,
            hybrid_score=0.0,
            confidence=1.0,
            algo_signal=0.0,
            llm_signal=0.0,
            regime="reconciliation",
            algo_version="system_sync",
            reasoning=reason,
            related_trade_id=related_trade_id,
            option_id=m.get("option_id"),
            option_type=m.get("option_type"),
            strike=m.get("strike"),
            expiration=m.get("expiration"),
        )

        result = OrderResult(
            order_id=f"sync_reconciled_{related_trade_id or 'new'}",
            status=OrderStatus.FILLED,
            ticker=ticker,
            direction=TradeDirection(direction),
            action=TradeAction(action),
            order_type=OrderType.MARKET,
            requested_quantity=quantity,
            filled_quantity=quantity,
            fill_price=price,
            slippage=0.0,
            option_id=m.get("option_id"),
            option_type=m.get("option_type"),
            strike=m.get("strike"),
            expiration=m.get("expiration"),
        )

        import json

        await self._journal.record_trade(
            proposal=proposal,
            result=result,
            market_snapshot_json=json.dumps({"reconciliation": True}),
            fill_source="sync",
        )
