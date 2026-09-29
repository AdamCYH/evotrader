"""Trade journal CRUD operations.

Provides typed read/write access to the ``trades`` table for recording
every trade decision, execution, and outcome. All operations are async.
"""

from __future__ import annotations

import copy
import logging
from datetime import UTC, datetime
from typing import Any

from evotrader.db.connection import Database
from evotrader.models.trade import (
    OrderResult,
    OrderType,
    TradeAction,
    TradeDirection,
    TradeProposal,
)

logger = logging.getLogger(__name__)

#: How long an executor-claimed fill stays on the re-check list. Roughly five
#: trading sessions — long enough to span a weekend and a holiday, short enough
#: that the list does not grow without bound.
_CLAIM_RECHECK_DAYS = 9

# Sentinel values written by the broker-reconciliation sync path. Rows tagged
# with these are NOT agent trading decisions and MUST be excluded from every
# performance metric and risk-breaker computation.
RECONCILIATION_REGIME = "reconciliation"
SYNC_ALGO_VERSION = "system_sync"
# Reusable SQL predicate (append to WHERE clauses).
EXCLUDE_RECONCILIATION_SQL = " AND regime != 'reconciliation' AND algo_version != 'system_sync' "


def _warn_on_exit_row_shape(
    proposal: TradeProposal,
    matching: list[dict],
) -> None:
    """Warn when an exit ROW contradicts the order it claims to record.

    Called per written chunk, on the post-inheritance ``chunk_proposal``,
    so it validates the values actually persisted rather than the proposal
    as submitted. Previously it ran once before the inheritance block, so
    rule 2 was checking a field that had not been populated yet — dead code
    for exactly the population most in need of checking.

    Blocked-exit analysis cannot be automated while the journal disagrees with
    what was actually proposed. On 2026-09-11 cycle 8 proposed two distinct
    orders — a STOP_MARKET and a LIMIT for a different quantity, which the
    Risk Manager evaluated separately as STOP_LOSS and TAKE_PROFIT — and the
    journal recorded BOTH as action='STOP_LOSS', with the stop's quantity and
    order_type='limit'. The take-profit lost its action, its quantity and its
    price.

    These are warnings, never blocks. A refused write would lose the audit trail
    entirely, which is worse than an inconsistent one — the whole point of this
    row is to record what happened, including when what happened was wrong.
    """
    action = proposal.action
    otype = proposal.order_type.value if proposal.order_type else None
    limit_p, stop_p = proposal.limit_price, proposal.stop_price

    # 0. A protective exit with NO price at all. This is the worst case in the
    #    class this guard was written for, and until 2026-09-14 it fell through
    #    every rule below: rule 1 requires limit_p, and rule 2's stop branch
    #    requires otype to ALREADY be a stop type. A broker-accepted
    #    stop_market was journaled with stop_price=NULL, limit_price=NULL,
    #    order_type='limit' (inherited from the OPEN row) and price set to the
    #    ENTRY price — silently.
    #
    #    A stop recorded at break-even is not an obviously missing value. It is
    #    a plausible and wrong one, which is strictly worse than a NULL: a NULL
    #    invites a lookup, whereas break-even is a configuration a trader might
    #    genuinely choose and will be read as fact by the next agent.
    if (
        action in (TradeAction.STOP_LOSS, TradeAction.TAKE_PROFIT)
        and limit_p is None
        and stop_p is None
    ):
        logger.warning(
            "record_trade: %s recorded as %s with neither stop_price nor "
            "limit_price. The protective level is UNRECOVERABLE from this row, "
            "and `price` will fall back to the ENTRY price — the row will read "
            "as a stop sitting exactly at break-even. Pass stop_price/"
            "limit_price through from the order that was actually placed.",
            proposal.ticker,
            action.value,
        )

    # 1. A stop whose only price is a LIMIT above the entry is structurally a
    #    take-profit wearing a stop's label.
    if action == TradeAction.STOP_LOSS and limit_p is not None and stop_p is None:
        entry = None
        if matching:
            _f = matching[0].get("fill_price")
            entry = float(_f) if _f is not None else float(matching[0].get("price") or 0.0)
        direction = (matching[0].get("direction") if matching else None) or "LONG"
        if entry and (
            (direction == "LONG" and limit_p > entry) or (direction == "SHORT" and limit_p < entry)
        ):
            logger.warning(
                "record_trade: %s recorded as STOP_LOSS but its only price is a "
                "limit of %.4f, which is favourable versus the %.4f entry on a "
                "%s lot — structurally this is a TAKE_PROFIT. Exit attribution "
                "will be wrong for this row.",
                proposal.ticker,
                limit_p,
                entry,
                direction,
            )

    # 2. order_type must agree with which price fields are populated.
    if otype == "limit" and limit_p is None and stop_p is not None:
        logger.warning(
            "record_trade: %s order_type='limit' but only stop_price is set "
            "(%.4f) — the recorded type contradicts the order's own prices.",
            proposal.ticker,
            stop_p,
        )
    elif otype in ("stop", "stop_limit", "trailing_stop") and stop_p is None:
        logger.warning(
            "record_trade: %s order_type='%s' but no stop_price is set — a stop "
            "order with no trigger price is unverifiable after the fact.",
            proposal.ticker,
            otype,
        )


def _warn_on_exit_quantity(
    proposal: TradeProposal,
    matching: list[dict],
    close_qty: float,
) -> None:
    """Warn when an exit claims more than the matched lots hold.

    Separate from the row-shape guard because this is a property of the WHOLE
    order, checked once against the full close quantity. Folding it into the
    per-chunk guard would re-check it against a quantity the loop has already
    decremented, and warn N times for one order.
    """
    total_rem = sum(float(t.get("remaining_quantity") or 0.0) for t in matching)
    if matching and close_qty > total_rem + 1e-4:
        logger.warning(
            "record_trade: %s exit quantity %.4f exceeds the %.4f remaining "
            "across %d matched lot(s). FIFO will flip the excess into a new "
            "position — confirm that is intended.",
            proposal.ticker,
            close_qty,
            total_rem,
            len(matching),
        )


class TradeJournal:
    """Async trade journal backed by SQLite."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record_trade(
        self,
        proposal: TradeProposal,
        result: OrderResult | None = None,
        market_snapshot_json: str = "{}",
        session_id: str | None = None,
        order_status: str = "FILLED",
        order_id: str | None = None,
        broker_status_reason: str | None = None,
        fill_source: str | None = None,
    ) -> list[int]:
        """Record a trade decision and its execution result.

        Supports FIFO matching for CLOSE actions, splitting the order across
        multiple open trades and flipping positions if the close quantity
        exceeds the open quantity.

        Args:
            proposal: The trade proposal that was executed.
            result: The order execution result (None for dry-run trades).
            market_snapshot_json: JSON string of the market state at decision time.
            session_id: Agent cycle session ID.
            order_status: Broker lifecycle state (PENDING, FILLED, REJECTED, etc.).
            order_id: Broker order ID for lifecycle tracking.
            broker_status_reason: Rejection/cancellation reason from broker.

        Returns:
            A list of auto-generated trade IDs.
        """
        fill_price = result.fill_price if result else None
        slippage = result.slippage if result else None
        _price_candidates = (fill_price, proposal.limit_price, proposal.stop_price)
        price = next((p for p in _price_candidates if p is not None and p > 0), None)
        price_unavailable = price is None
        if price_unavailable:
            logger.error(
                "record_trade: no valid price for %s %s — realized P&L will be NULL, not sentinel-derived.",
                proposal.ticker,
                proposal.action,
            )

        # ── Defense-in-depth: reclassify OPEN→CLOSE when it's clearly a reduce ──
        # An OPEN with a related_trade_id is contradictory — OPENs create new
        # lots, they don't reference prior ones.  This catches cases where the
        # inference engine missed a mislabelled trim/sell that slipped through
        # with action=OPEN, which previously created phantom duplicate lots.
        if proposal.action == TradeAction.OPEN and proposal.related_trade_id is not None:
            logger.warning(
                "record_trade: action=OPEN with related_trade_id=%s for %s — "
                "reclassifying as CLOSE (likely a mislabelled trim/reduce).",
                proposal.related_trade_id,
                proposal.ticker,
            )
            proposal.action = TradeAction.CLOSE

        if proposal.action not in (
            TradeAction.CLOSE,
            TradeAction.STOP_LOSS,
            TradeAction.TAKE_PROFIT,
        ):
            # Simple OPEN trade — price should always be available, but guard
            # against edge cases to avoid IntegrityError on CHECK(price > 0).
            open_price = price if price is not None else 0.01
            open_reason = broker_status_reason
            if order_status == "FILLED" and fill_price is None and proposal.limit_price is not None:
                logger.warning(
                    "record_trade: OPEN journaled FILLED at limit price %.4f without "
                    "a confirmed broker fill for %s — entry price is PROVISIONAL and "
                    "must be verified by reconciliation before P&L is decision-grade.",
                    proposal.limit_price,
                    proposal.ticker,
                )
                open_reason = ((open_reason or "") + "; PRICE_FROM_LIMIT_UNCONFIRMED").lstrip("; ")
            if price_unavailable:
                open_reason = (broker_status_reason or "") + "; PRICE_UNAVAILABLE"
                open_reason = open_reason.lstrip("; ")
            trade_id = await self._insert_single_trade(
                proposal,
                open_price,
                proposal.quantity,
                fill_price,
                slippage,
                market_snapshot_json,
                None,
                None,
                None,
                session_id,
                order_status=order_status,
                order_id=order_id,
                broker_status_reason=open_reason,
                fill_source=fill_source,
            )

            # ── Post-insert position audit ──
            # Detect phantom-lot drift early. After every OPEN, compute
            # the net journal position.  A mislabelled trim creates a
            # sudden position spike that is visible immediately.
            # We log a WARNING (not reclassify) because reconciliation is
            # the authoritative correction mechanism — this audit just
            # ensures the drift is visible in cycle logs.
            if order_status == "FILLED":
                await self._audit_position_after_open(
                    proposal.ticker,
                    proposal.option_id,
                    trade_id,
                )

            return [trade_id]

        # Handle CLOSE trade with FIFO matching
        close_qty = proposal.quantity

        recorded_ids = []
        open_trades = await self.get_open_trades()
        # Filter for matching ticker (and option_id if applicable)
        matching = [
            t
            for t in open_trades
            if t.get("ticker") == proposal.ticker and t.get("option_id") == proposal.option_id
        ]
        if proposal.direction is not None and any(
            t.get("direction") != proposal.direction.value for t in matching
        ):
            logger.warning(
                "record_trade: CLOSE direction %s mismatches journaled lot direction(s) for %s/%s — "
                "matching on option_id; direction treated as advisory.",
                proposal.direction.value,
                proposal.ticker,
                proposal.option_id,
            )

        # Inherit direction if it was missing but we found matching trades
        if matching and proposal.direction is None:
            proposal.direction = TradeDirection(matching[0]["direction"])

        if close_qty is None:
            if matching:
                close_qty = float(matching[0]["quantity"])
            else:
                close_qty = 1.0

        _warn_on_exit_quantity(proposal, matching, close_qty)

        # ── ONE BROKER ORDER, ONE ROW, FOR A RESTING PROTECTIVE ORDER ────
        # A protective order that has not filled closed nothing, so FIFO
        # chunking it across lots records exits that did not happen. On
        # 2026-09-25 a single resting stop became two exit rows against two
        # different lots plus a phantom SHORT row — three rows sharing one
        # order_id, so a later update_order_status(order_id) could not address
        # them separately, and two of them carried realized P&L against lots
        # that were never sold.
        #
        # The order is one fact: N shares of protection, resting. Write it that
        # way. When it actually fills, the broker says so and the fill path
        # does the FIFO matching then.
        is_protective = proposal.action in (TradeAction.STOP_LOSS, TradeAction.TAKE_PROFIT)
        status_is_filled = (order_status or "FILLED").upper() == "FILLED"
        # `matching` must be non-empty: a protective order with NO journal lot at
        # all is a different situation with its own repair path below
        # (UNMATCHED_CLOSE, which relink_unmatched_protection later keys on when
        # the entry finally fills). Intercepting it here would break that link.
        if is_protective and not status_is_filled and matching:
            total_open = sum(float(t.get("remaining_quantity") or 0.0) for t in matching)
            reason_parts = [broker_status_reason] if broker_status_reason else []
            # A buy placed in this same cycle and still awaiting the broker's
            # word is a lot a moment from now. Resizing the stop to cover it is
            # the normal add path, not an over-cover: on 2026-09-28 a stop
            # resized right after a buy read OVER_COVER for good, because the
            # buy was still PENDING when the stop was written.
            pending = 0.0
            if close_qty > total_open + 1e-4:
                pending = await self._pending_open_quantity(
                    proposal.ticker, proposal.option_id, str(matching[0]["direction"]), session_id
                )
            if pending > 0 and close_qty <= total_open + pending + 1e-4:
                reason_parts.append(
                    f"covers {pending:g} share(s) of a buy awaiting broker confirmation"
                )
            elif close_qty > total_open + 1e-4:
                reason_parts.append(
                    "OVER_COVER: protective qty exceeds journaled lots "
                    f"({close_qty:g} vs {total_open:g})"
                )
                logger.warning(
                    "record_trade: %s %s for %g shares exceeds the %g journaled "
                    "for %s. Recording one %s row for the full quantity — a "
                    "protective order can never flip a position.",
                    proposal.ticker,
                    proposal.action.value,
                    close_qty,
                    total_open,
                    proposal.ticker,
                    order_status,
                )
            single_price = (
                price
                if not price_unavailable
                else (
                    proposal.stop_price
                    or proposal.limit_price
                    or (float(matching[0]["price"]) if matching else 0.01)
                )
            )
            if price_unavailable:
                reason_parts.append("PRICE_UNAVAILABLE")
            trade_id = await self._insert_single_trade(
                proposal,
                single_price,
                close_qty,
                fill_price,
                slippage,
                market_snapshot_json,
                matching[0]["id"] if matching else None,
                None,  # realized_pnl: nothing closed, so there is no P&L
                None,
                session_id,
                order_status=order_status,
                order_id=order_id,
                broker_status_reason="; ".join(reason_parts) or None,
                fill_source=fill_source,
            )
            return [trade_id]

        for db_trade in matching:
            if close_qty <= 1e-4:
                break

            rem_qty = float(db_trade.get("remaining_quantity", 0.0))
            if rem_qty <= 1e-4:
                continue

            chunk_qty = min(close_qty, rem_qty)

            # Calculate PnL and holding period for this chunk.
            # Use fill_price (actual broker execution price) when available,
            # falling back to price (the agent's requested limit/stop price)
            # for trades that filled immediately without a separate fill record.
            _fill = db_trade.get("fill_price")
            entry_price = float(_fill) if _fill is not None else float(db_trade["price"])
            mult = 100.0 if proposal.option_id else 1.0
            if price_unavailable or (order_status and order_status.upper() != "FILLED"):
                pnl = None  # never fabricate or store P&L for unfilled, failed, or cancelled orders
            else:
                lot_direction = db_trade.get("direction", "LONG")
                if lot_direction == "LONG":
                    pnl = (price - entry_price) * chunk_qty * mult
                else:
                    pnl = (entry_price - price) * chunk_qty * mult

            holding_period_s = 0
            db_ts_str = db_trade.get("timestamp")
            if db_ts_str:
                from datetime import UTC, datetime

                try:
                    ts = datetime.fromisoformat(db_ts_str)
                    holding_period_s = int(
                        (proposal.timestamp - ts.astimezone(UTC)).total_seconds()
                    )
                except Exception:
                    pass

            # Inherit context from the open trade for missing fields.
            #
            # `order_type` is deliberately NOT inherited. algo_version, regime
            # and the signal components are properties of the POSITION and
            # inherit sensibly across an exit. `order_type` is a property of
            # THIS order and changes meaning entirely when the action changes:
            # a `limit` entry's stop-loss child inherited 'limit', so a
            # broker-accepted stop_market is now permanently recorded as a
            # limit order (several historical rows read this way). Recording
            # that the type was not supplied is honest; guessing it from the
            # entry is not.
            chunk_proposal = copy.deepcopy(proposal)
            if chunk_proposal.algo_version is None:
                chunk_proposal.algo_version = db_trade.get("algo_version")
            if chunk_proposal.regime is None:
                chunk_proposal.regime = db_trade.get("regime")
            if chunk_proposal.algo_signal is None and db_trade.get("algo_signal") is not None:
                chunk_proposal.algo_signal = float(db_trade["algo_signal"])
            if chunk_proposal.llm_signal is None and db_trade.get("llm_signal") is not None:
                chunk_proposal.llm_signal = float(db_trade["llm_signal"])
            if chunk_proposal.hybrid_score is None and db_trade.get("hybrid_score") is not None:
                chunk_proposal.hybrid_score = float(db_trade["hybrid_score"])
            if chunk_proposal.confidence is None and db_trade.get("confidence") is not None:
                chunk_proposal.confidence = float(db_trade["confidence"])
            if chunk_proposal.reasoning is None:
                chunk_proposal.reasoning = f"Exit trade linked to ID {db_trade['id']}"

            # Validate the row ACTUALLY being written, after inheritance.
            _warn_on_exit_row_shape(chunk_proposal, matching)

            # When price is unavailable, store the entry_price for the audit
            # trail (satisfies CHECK(price > 0)) but NULL the realized_pnl.
            stored_price = entry_price if price_unavailable else price
            chunk_broker_reason = broker_status_reason
            if price_unavailable:
                # Two distinct facts, two distinct markers. PRICE_UNAVAILABLE
                # says the P&L is NULL. It does NOT say that the `price` column
                # now holds the ENTRY price of the lot being closed — which, on
                # a protective exit, reads as a stop sitting exactly at
                # break-even (this happened: a stop well below the entry was
                # journaled at the entry price). That is a plausible and wrong
                # value, so the row must say so about itself. Mirrors
                # PRICE_FROM_LIMIT_UNCONFIRMED on the OPEN path.
                chunk_broker_reason = (
                    (broker_status_reason or "") + "; PRICE_UNAVAILABLE; PRICE_FROM_ENTRY_FALLBACK"
                ).lstrip("; ")
                if proposal.action in (TradeAction.STOP_LOSS, TradeAction.TAKE_PROFIT):
                    logger.warning(
                        "record_trade: %s %s has no usable price; recording the "
                        "ENTRY price %.4f as an audit placeholder. This is NOT "
                        "the order's price — the row will read as a protective "
                        "level at break-even. Marked "
                        "PRICE_FROM_ENTRY_FALLBACK.",
                        proposal.ticker,
                        proposal.action.value,
                        entry_price,
                    )

            trade_id = await self._insert_single_trade(
                chunk_proposal,
                stored_price,
                chunk_qty,
                fill_price,
                slippage,
                market_snapshot_json,
                db_trade["id"],
                pnl,
                holding_period_s,
                session_id,
                order_status=order_status,
                order_id=order_id,
                broker_status_reason=chunk_broker_reason,
                fill_source=fill_source,
            )
            recorded_ids.append(trade_id)
            close_qty -= chunk_qty

        # If there's STILL quantity left over after FIFO matching...
        if close_qty > 1e-4:
            is_genuine_flip = proposal.action == TradeAction.CLOSE and status_is_filled
            if proposal.option_id is not None or not matching or not is_genuine_flip:
                # Options cannot flip to written-short (constitution forbids writing),
                # and an unmatched close is a data-integrity event, not a new position.
                #
                # `is_genuine_flip` also routes here every over-close that is
                # NOT a filled, deliberate CLOSE: a protective order of any
                # status, and any close the broker has not executed. Each is
                # recorded as what it is, without inventing a position — the
                # phantom short rows of 2026-09-25 are what this prevents.
                logger.error(
                    "record_trade: unmatched CLOSE qty %.4f for %s/%s — recording audit row, NOT an OPEN.",
                    close_qty,
                    proposal.ticker,
                    proposal.option_id,
                )
                # Use last matched lot price or proposal limit/stop as stored price
                # for audit trail (must satisfy CHECK(price > 0)).
                audit_price = (
                    price
                    if not price_unavailable
                    else (
                        float(matching[-1]["price"])
                        if matching
                        else proposal.limit_price or proposal.stop_price or 0.01
                    )
                )
                audit = copy.deepcopy(proposal)
                # Keep a protective order's own label. Relabelling a stop as
                # CLOSE destroyed the one fact the post-cycle protection audit
                # needs: a resting stop (2026-09-21) was stored as CLOSE and
                # the audit reported the position uncovered on 9 cycles in a
                # row. The audit fact belongs in the reason, which still says
                # UNMATCHED_CLOSE.
                if proposal.action not in (TradeAction.STOP_LOSS, TradeAction.TAKE_PROFIT):
                    audit.action = TradeAction.CLOSE
                if matching:
                    # Lots exist; the quantity simply exceeded them. Saying
                    # "no open journal lot" here would be false.
                    audit_reason = broker_status_reason or (
                        "OVER_COVER: protective qty exceeds journaled lots"
                        if proposal.action in (TradeAction.STOP_LOSS, TradeAction.TAKE_PROFIT)
                        else "OVER_CLOSE: qty exceeds journaled lots"
                    )
                else:
                    audit_reason = broker_status_reason or "UNMATCHED_CLOSE: no open journal lot"
                if price_unavailable:
                    audit_reason = audit_reason + "; PRICE_UNAVAILABLE"
                    if matching:
                        # audit_price came from matching[-1]["price"] — the
                        # ENTRY price of a lot, not this order's price.
                        audit_reason += "; PRICE_FROM_ENTRY_FALLBACK"
                trade_id = await self._insert_single_trade(
                    audit,
                    audit_price,
                    close_qty,
                    fill_price,
                    slippage,
                    market_snapshot_json,
                    None,
                    None,
                    None,
                    session_id,
                    order_status=(order_status if order_status != "FILLED" else "UNMATCHED"),
                    order_id=order_id,
                    broker_status_reason=audit_reason,
                    fill_source=fill_source,
                )
                recorded_ids.append(trade_id)
            else:
                # Equity direction flip (genuine over-close).
                #
                # Only a FILLED, deliberate CLOSE can flip a position. A
                # protective order never can — it is cover, not a reversal —
                # and neither can an order that has not executed. On
                # 2026-09-25 this branch turned a FAILED stop ("Not enough
                # shares to sell") into a SHORT OPEN twice, and a
                # journaled-FILLED stop into a phantom short, which is what
                # made the journal report a short account while the broker
                # held a long position.
                # Protective and non-filled over-closes take the audit branch
                # above instead, which records the order without inventing a
                # position.
                flipped_dir = (
                    TradeDirection.SHORT
                    if proposal.direction == TradeDirection.LONG
                    else TradeDirection.LONG
                )
                flipped_proposal = copy.deepcopy(proposal)
                flipped_proposal.action = TradeAction.OPEN
                flipped_proposal.direction = flipped_dir
                flip_price = (
                    price
                    if not price_unavailable
                    else (float(matching[-1]["price"]) if matching else 0.01)
                )
                flip_reason = broker_status_reason
                if price_unavailable:
                    flip_reason = (broker_status_reason or "") + "; PRICE_UNAVAILABLE"
                    flip_reason = flip_reason.lstrip("; ")
                trade_id = await self._insert_single_trade(
                    flipped_proposal,
                    flip_price,
                    close_qty,
                    fill_price,
                    slippage,
                    market_snapshot_json,
                    None,
                    None,
                    None,
                    session_id,
                    order_status=order_status,
                    order_id=order_id,
                    broker_status_reason=flip_reason,
                    fill_source=fill_source,
                )
                recorded_ids.append(trade_id)
        return recorded_ids

    async def _audit_position_after_open(
        self,
        ticker: str,
        option_id: str | None,
        new_trade_id: int,
    ) -> None:
        """Emit structured position telemetry after an OPEN insert.

        Detects phantom-lot drift by checking the net journal position
        immediately after every OPEN.  This is a monitoring/alerting
        mechanism — **reconciliation** is the authoritative correction
        path.  The journal should never silently reclassify based on
        heuristics because it does not have access to broker ground truth.

        A mislabelled trim (OPEN instead of CLOSE) produces a tell-tale
        signature: the number of open lots for a single ticker grows
        beyond what the position sizing rules allow, or the net position
        jumps by MORE than the new trade's quantity (impossible for a
        genuine add-to-position).
        """
        try:
            open_lots = await self.get_open_trades()
            matching = [
                t
                for t in open_lots
                if t.get("ticker") == ticker and t.get("option_id") == option_id
            ]
            if len(matching) <= 1:
                return  # First lot for this (ticker, option_id) — nothing to audit

            net_qty = sum(float(t.get("remaining_quantity", 0.0)) for t in matching)
            lot_count = len(matching)

            # Emit structured telemetry on every OPEN when lots > 1
            logger.info(
                "POSITION_AUDIT: %s%s after OPEN trade #%d — %d open lot(s), net_qty=%.1f",
                ticker,
                f"/{option_id}" if option_id else "",
                new_trade_id,
                lot_count,
                net_qty,
            )

            # Flag anomalous states that historically indicate drift.
            # 4+ lots for a single equity position is unusual and was the
            # signature of the PSQ phantom-share drift (6 lots).  The
            # threshold is deliberately conservative — it's a warning, not a
            # block.
            if lot_count >= 4:
                logger.critical(
                    "POSITION_AUDIT ALERT: %s has %d open lots (net %.1f shares) "
                    "— possible phantom-lot drift. Reconciliation should verify "
                    "against broker position.",
                    ticker,
                    lot_count,
                    net_qty,
                )
        except Exception:
            # Audit is best-effort — never block trade recording
            logger.debug(
                "Position audit failed for trade #%d (non-critical)",
                new_trade_id,
                exc_info=True,
            )

    async def _insert_single_trade(
        self,
        proposal: TradeProposal,
        price: float,
        quantity: float,
        fill_price: float | None,
        slippage: float | None,
        market_snapshot_json: str,
        related_trade_id: int | None,
        realized_pnl: float | None,
        holding_period_s: int | None,
        session_id: str | None = None,
        order_status: str = "FILLED",
        order_id: str | None = None,
        broker_status_reason: str | None = None,
        fill_source: str | None = None,
    ) -> int:
        quantity = round(quantity, 4)
        direction = proposal.direction.value if proposal.direction else "LONG"
        # NOTE: the column is NOT NULL, so an unsupplied type cannot be recorded
        # as NULL without a schema change. The default is lower-cased for
        # consistency — every real value is `OrderType`'s lowercase value, and
        # "MARKET" (the enum NAME) was the only uppercase string ever written,
        # so any consumer comparing without normalising saw a phantom category.
        # See carry_forward: recording an unsupplied type honestly still needs
        # a nullable column.
        order_type = proposal.order_type.value if proposal.order_type else OrderType.MARKET.value
        algo_version = proposal.algo_version or "unknown"
        regime = proposal.regime or "unknown"
        # Preserve NULL vs genuine-0.0 distinction: only a missing field
        # should yield NULL; a real flat composite (0.0) is stored as 0.0.
        algo_signal = proposal.algo_signal if proposal.algo_signal is not None else None
        llm_signal = proposal.llm_signal
        hybrid_score = proposal.hybrid_score if proposal.hybrid_score is not None else None
        confidence = proposal.confidence if proposal.confidence is not None else 1.0
        if proposal.algo_signal is None or proposal.regime is None or proposal.algo_version is None:
            logger.warning(
                "record_trade: signal provenance incomplete (algo_signal=%s, regime=%s, algo_version=%s) "
                "for %s %s — executor likely dropped decision context.",
                proposal.algo_signal,
                proposal.regime,
                proposal.algo_version,
                proposal.ticker,
                proposal.action,
            )
        reasoning = proposal.reasoning or "Automated trade execution"

        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO trades (
                    timestamp, ticker, direction, action, quantity, price,
                    order_type, fill_price, slippage, limit_price, stop_price,
                    algo_version, regime, algo_signal, llm_signal,
                    hybrid_score, confidence, reasoning,
                    related_trade_id, market_snapshot,
                    option_id, option_type, strike, expiration,
                    realized_pnl, holding_period_s, session_id,
                    order_status, order_id, broker_status_reason,
                    time_in_force, fill_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal.timestamp.isoformat(),
                    proposal.ticker,
                    direction,
                    proposal.action.value,
                    quantity,
                    price,
                    order_type,
                    fill_price,
                    slippage,
                    proposal.limit_price,
                    # The trigger level, stored structurally so an agent can
                    # verify its protection instead of parsing free text.
                    proposal.stop_price,
                    algo_version,
                    regime,
                    algo_signal,
                    llm_signal,
                    hybrid_score,
                    confidence,
                    reasoning,
                    related_trade_id,
                    market_snapshot_json,
                    proposal.option_id,
                    proposal.option_type,
                    proposal.strike,
                    proposal.expiration,
                    realized_pnl,
                    holding_period_s,
                    session_id,
                    order_status,
                    order_id,
                    broker_status_reason,
                    (proposal.time_in_force or None),
                    fill_source,
                ),
            )
            return cursor.lastrowid

    async def update_outcome(
        self,
        trade_id: int,
        realized_pnl: float,
        holding_period_seconds: int,
    ) -> None:
        """Update a trade record with its outcome after position close.

        Called when a position is closed (via close, stop-loss, or take-profit)
        to record the realized P&L and holding period.
        """
        async with self._db.transaction() as conn:
            await conn.execute(
                """
                UPDATE trades
                SET realized_pnl = ?, holding_period_s = ?
                WHERE id = ?
                """,
                (realized_pnl, holding_period_seconds, trade_id),
            )
        logger.info(
            "Trade outcome updated: id=%d, pnl=$%.2f, held=%ds",
            trade_id,
            realized_pnl,
            holding_period_seconds,
        )

    async def get_recent_trades(
        self,
        limit: int = 50,
        ticker: str | None = None,
        direction: TradeDirection | None = None,
        action: TradeAction | None = None,
    ) -> list[dict]:  # type: ignore[type-arg]
        """Query recent trades with optional filters.

        Returns raw dictionaries for flexibility. Use Pydantic models
        downstream if type safety is needed.
        """
        query = "SELECT * FROM trades WHERE 1=1"
        params: list[str | int] = []

        if ticker:
            query += " AND ticker = ?"
            params.append(ticker)
        if direction:
            query += " AND direction = ?"
            params.append(direction.value)
        if action:
            query += " AND action = ?"
            params.append(action.value)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        async with self._db.connection() as conn:
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return list(rows)

    async def get_trade_by_id(self, trade_id: int) -> dict[str, Any] | None:
        """Fetch a single trade record by ID."""
        async with self._db.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM trades WHERE id = ?",
                (trade_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    def _get_today_bounds(self) -> list[tuple[str, str]]:
        """Resolve today's possible date boundary strings (UTC, Eastern, Local, and Mock Time)."""
        import os

        mock_time_str = os.environ.get("EVOTRADER_MOCK_TIME")
        if mock_time_str:
            try:
                date_str = mock_time_str.split("T")[0]
                return [(f"{date_str}T00:00:00", f"{date_str}T23:59:59.999999")]
            except Exception:
                pass

        dates = [
            datetime.now(UTC).strftime("%Y-%m-%d"),
            datetime.now().strftime("%Y-%m-%d"),
        ]
        try:
            from evotrader.tools.market_hours import ET

            dates.append(datetime.now(ET).strftime("%Y-%m-%d"))
        except Exception:
            pass

        # Return unique dates ordered
        unique_dates = sorted(list(set(dates)))
        return [(f"{d}T00:00:00", f"{d}T23:59:59.999999") for d in unique_dates]

    async def get_today_trades(self) -> list[dict]:  # type: ignore[type-arg]
        """Get all trades from today."""
        bounds = self._get_today_bounds()
        conditions = " OR ".join(["(timestamp >= ? AND timestamp <= ?)"] * len(bounds))
        params = [val for b in bounds for val in b]
        async with self._db.connection() as conn:
            cursor = await conn.execute(
                f"SELECT * FROM trades WHERE {conditions} ORDER BY timestamp",
                params,
            )
            return list(await cursor.fetchall())

    async def get_today_pnl(self) -> float:
        """Calculate today's total realized P&L."""
        bounds = self._get_today_bounds()
        conditions = " OR ".join(["(timestamp >= ? AND timestamp <= ?)"] * len(bounds))
        params = [val for b in bounds for val in b]
        async with self._db.connection() as conn:
            cursor = await conn.execute(
                f"""
                SELECT COALESCE(SUM(realized_pnl), 0.0) as total_pnl
                FROM trades
                WHERE ({conditions}) AND realized_pnl IS NOT NULL
                AND (order_status = 'FILLED' OR order_status IS NULL)
                """,
                params,
            )
            row = await cursor.fetchone()
            return float(row["total_pnl"]) if row else 0.0

    async def get_pnl(self, period: str = "today") -> float:
        """Calculate total realized P&L for a given period."""
        if period == "today":
            return await self.get_today_pnl()

        where_clause = (
            "realized_pnl IS NOT NULL AND (order_status = 'FILLED' OR order_status IS NULL)"
        )

        from evotrader.utils import get_period_cutoff_utc_str

        cutoff_str = get_period_cutoff_utc_str(period)
        params = []
        if cutoff_str:
            where_clause += " AND timestamp >= ?"
            params.append(cutoff_str)

        async with self._db.connection() as conn:
            cursor = await conn.execute(
                f"""
                SELECT COALESCE(SUM(realized_pnl), 0.0) as total_pnl
                FROM trades
                WHERE {where_clause}
                """,
                params,
            )
            row = await cursor.fetchone()
            return float(row["total_pnl"]) if row else 0.0

    async def get_consecutive_losses(self) -> int:
        """Count the current streak of consecutive losing trades."""
        async with self._db.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT realized_pnl FROM trades
                WHERE realized_pnl IS NOT NULL
                AND (order_status = 'FILLED' OR order_status IS NULL)
                AND regime != 'reconciliation'
                AND algo_version != 'system_sync'
                ORDER BY timestamp DESC
                LIMIT 100
                """
            )
            rows = await cursor.fetchall()

        streak = 0
        for row in rows:
            pnl = row["realized_pnl"] if isinstance(row, dict) else row[0]
            if pnl is not None and pnl < 0:
                streak += 1
            else:
                break
        return streak

    async def get_session_consecutive_losses(self) -> int:
        """Count consecutive losses scoped to the current trading session.

        The streak resets at each trading session boundary (market open,
        roughly 09:30 ET).  This prevents a Friday streak from blocking
        Monday morning trading — the breaker's ``pause_duration_minutes``
        provides the wall-clock cooldown, and this session scope prevents
        the streak itself from persisting indefinitely across weekends.

        Falls back to ``get_consecutive_losses()`` if the session boundary
        cannot be determined.
        """
        # Use today's market-open boundary in UTC.  The system operates in
        # ET for session semantics; 09:30 ET is 13:30 UTC (EST) or 13:30 UTC
        # (EDT).  We use a conservative approach: scope to "today" using the
        # same bounds helper used elsewhere.
        try:
            bounds = self._get_today_bounds()
        except Exception:
            # Fallback to the unconstrained streak if bounds fail
            return await self.get_consecutive_losses()

        if not bounds:
            return await self.get_consecutive_losses()

        # Build WHERE clause for today's session window(s)
        conditions = " OR ".join(["(timestamp >= ? AND timestamp <= ?)"] * len(bounds))
        params: list[str] = [val for b in bounds for val in b]

        async with self._db.connection() as conn:
            cursor = await conn.execute(
                f"""
                SELECT realized_pnl FROM trades
                WHERE realized_pnl IS NOT NULL
                AND (order_status = 'FILLED' OR order_status IS NULL)
                AND regime != 'reconciliation'
                AND algo_version != 'system_sync'
                AND ({conditions})
                ORDER BY timestamp DESC
                LIMIT 100
                """,
                params,
            )
            rows = await cursor.fetchall()

        streak = 0
        for row in rows:
            pnl = row["realized_pnl"] if isinstance(row, dict) else row[0]
            if pnl is not None and pnl < 0:
                streak += 1
            else:
                break
        return streak

    async def get_last_loss_timestamp(self) -> datetime | None:
        """Return the timestamp of the most recent losing trade, or None."""
        async with self._db.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT timestamp FROM trades
                WHERE realized_pnl IS NOT NULL
                AND realized_pnl < 0
                AND regime != 'reconciliation'
                AND algo_version != 'system_sync'
                ORDER BY timestamp DESC
                LIMIT 1
                """
            )
            row = await cursor.fetchone()

        if not row:
            return None
        ts_str = row["timestamp"] if isinstance(row, dict) else row[0]
        return datetime.fromisoformat(ts_str)

    async def get_period_metrics(self, period: str = "today") -> dict[str, Any]:
        """Compute aggregate trade metrics for a period in a single query.

        Returns:
            Dict containing:
            - trade_count: int
            - pnl: float
            - wins: int
            - losses: int
        """
        from evotrader.utils import get_period_cutoff_utc_str

        params = []
        if period == "today":
            bounds = self._get_today_bounds()
            conditions = " OR ".join(["(timestamp >= ? AND timestamp <= ?)"] * len(bounds))
            where_clause = f"({conditions})"
            params = [val for b in bounds for val in b]
        else:
            cutoff_str = get_period_cutoff_utc_str(period)
            if cutoff_str:
                where_clause = "timestamp >= ?"
                params.append(cutoff_str)
            else:
                where_clause = "1=1"

        async with self._db.connection() as conn:
            cursor = await conn.execute(
                f"""
                SELECT
                    COUNT(*) as trade_count,
                    COALESCE(SUM(CASE WHEN (order_status = 'FILLED' OR order_status IS NULL) THEN realized_pnl ELSE 0 END), 0.0) as total_pnl,
                    COALESCE(SUM(CASE WHEN realized_pnl > 0 AND (order_status = 'FILLED' OR order_status IS NULL) THEN 1 ELSE 0 END), 0) as wins,
                    COALESCE(SUM(CASE WHEN realized_pnl < 0 AND (order_status = 'FILLED' OR order_status IS NULL) THEN 1 ELSE 0 END), 0) as losses
                FROM trades
                WHERE {where_clause}
                """,
                params,
            )
            row = await cursor.fetchone()
            if row:
                return {
                    "trade_count": int(row["trade_count"] or 0),
                    "pnl": float(row["total_pnl"] or 0.0),
                    "wins": int(row["wins"] or 0),
                    "losses": int(row["losses"] or 0),
                }
            return {"trade_count": 0, "pnl": 0.0, "wins": 0, "losses": 0}

    async def get_period_win_loss_count(self, period: str = "today") -> tuple[int, int]:
        """Count the number of winning and losing trades for a given period."""
        from evotrader.utils import get_period_cutoff_utc_str

        where_clause = (
            "realized_pnl IS NOT NULL AND (order_status = 'FILLED' OR order_status IS NULL)"
        )
        params = []

        if period == "today":
            bounds = self._get_today_bounds()
            conditions = " OR ".join(["(timestamp >= ? AND timestamp <= ?)"] * len(bounds))
            where_clause += f" AND ({conditions})"
            params = [val for b in bounds for val in b]
        else:
            cutoff_str = get_period_cutoff_utc_str(period)
            if cutoff_str:
                where_clause += " AND timestamp >= ?"
                params.append(cutoff_str)

        async with self._db.connection() as conn:
            cursor = await conn.execute(
                f"""
                SELECT
                    SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) as losses
                FROM trades
                WHERE {where_clause}
                """,
                params,
            )
            row = await cursor.fetchone()
            if row:
                wins = row["wins"] or 0
                losses = row["losses"] or 0
                return int(wins), int(losses)
            return 0, 0

    async def get_consecutive_no_trades(self, period: str = "all") -> int:
        """Count the current streak of successful trading cycles that did NOT result in a trade.

        Args:
            period: Time period filter — "today", "7d", "30d", or "all".
        """
        where_clause = "c.cycle_type = 'TRADING' AND c.status = 'SUCCESS'"
        params: list = []

        if period == "today":
            bounds = self._get_today_bounds()
            conditions = " OR ".join(["(c.timestamp >= ? AND c.timestamp <= ?)"] * len(bounds))
            where_clause += f" AND ({conditions})"
            params = [val for b in bounds for val in b]
        elif period != "all":
            from evotrader.utils import get_period_cutoff_utc_str

            cutoff_str = get_period_cutoff_utc_str(period)
            if cutoff_str:
                where_clause += " AND c.timestamp >= ?"
                params.append(cutoff_str)

        async with self._db.connection() as conn:
            cursor = await conn.execute(
                f"""
                SELECT c.session_id,
                       (SELECT count(*) FROM trades t WHERE t.session_id = c.session_id) as trade_count
                FROM cycle_runs c
                WHERE {where_clause}
                ORDER BY c.timestamp DESC
                LIMIT 100
                """,
                params,
            )
            rows = await cursor.fetchall()

        streak = 0
        for row in rows:
            trade_count = row["trade_count"] if isinstance(row, dict) else row[1]
            if trade_count == 0:
                streak += 1
            else:
                break
        return streak

    async def get_open_trades(self) -> list[dict]:  # type: ignore[type-arg]
        """Get trades that opened positions but haven't been fully closed yet.

        Calculates the remaining quantity for each open trade and filters out
        those that have been fully closed. Orders by oldest first for FIFO.

        PENDING trades are excluded — they haven't been confirmed by the broker
        yet and should not create phantom positions. They become visible once
        their order_status is updated to FILLED.
        """
        async with self._db.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT t.*,
                       ROUND(t.quantity - COALESCE(SUM(c.quantity), 0), 4) as remaining_quantity
                FROM trades t
                LEFT JOIN trades c ON c.related_trade_id = t.id
                    AND c.action IN ('CLOSE', 'STOP_LOSS', 'TAKE_PROFIT')
                    AND COALESCE(c.order_status, 'FILLED') = 'FILLED'
                WHERE t.action = 'OPEN'
                  AND COALESCE(t.order_status, 'FILLED') = 'FILLED'
                  AND COALESCE(t.order_status, 'FILLED') NOT IN ('REJECTED', 'FAILED', 'CANCELLED', 'EXPIRED', 'UNMATCHED')
                GROUP BY t.id
                HAVING remaining_quantity > 0
                ORDER BY t.timestamp ASC
                """
            )
            return list(await cursor.fetchall())

    async def get_trade_count_today(self) -> int:
        """Count the number of trades executed today."""
        bounds = self._get_today_bounds()
        conditions = " OR ".join(["(timestamp >= ? AND timestamp <= ?)"] * len(bounds))
        params = [val for b in bounds for val in b]
        async with self._db.connection() as conn:
            cursor = await conn.execute(
                f"SELECT COUNT(*) FROM trades WHERE {conditions}",
                params,
            )
            row = await cursor.fetchone()
            # COUNT(*) returns a single value; with dict factory the key is the expression
            if row:
                # dict access: get the first (only) value
                return int(next(iter(row.values())))
            return 0

    async def get_trade_count(self, period: str = "today") -> int:
        """Count the number of trades executed for a given period."""
        if period == "today":
            return await self.get_trade_count_today()

        where_clause = "1=1"
        from evotrader.utils import get_period_cutoff_utc_str

        cutoff_str = get_period_cutoff_utc_str(period)
        params = []
        if cutoff_str:
            where_clause = "timestamp >= ?"
            params.append(cutoff_str)

        async with self._db.connection() as conn:
            cursor = await conn.execute(f"SELECT COUNT(*) FROM trades WHERE {where_clause}", params)
            row = await cursor.fetchone()
            if row:
                return int(next(iter(row.values())))
            return 0

    async def get_performance_summary(
        self,
        days: int = 30,
    ) -> dict:  # type: ignore[type-arg]
        """Compute performance summary over the last N days.

        Returns a dictionary with win_rate, avg_win, avg_loss, profit_factor,
        total_pnl, and trade_count.
        """
        async with self._db.connection() as conn:
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
                    SUM(COALESCE(realized_pnl, 0)) as total_pnl
                FROM trades
                WHERE realized_pnl IS NOT NULL
                AND (order_status = 'FILLED' OR order_status IS NULL)
                AND regime != 'reconciliation'
                AND algo_version != 'system_sync'
                AND timestamp >= datetime('now', ?)
                """,
                (f"-{days} days",),
            )
            row = await cursor.fetchone()
            if not row:
                return {
                    "trade_count": 0,
                    "win_rate": 0.0,
                    "avg_win": 0.0,
                    "avg_loss": 0.0,
                    "profit_factor": 0.0,
                    "total_pnl": 0.0,
                }

            total = row["total"]
            if total == 0:
                return {
                    "trade_count": 0,
                    "win_rate": 0.0,
                    "avg_win": 0.0,
                    "avg_loss": 0.0,
                    "profit_factor": 0.0,
                    "total_pnl": 0.0,
                }

            wins = row["wins"]
            avg_win = row["avg_win"]
            avg_loss = row["avg_loss"]
            gross_profit = row["gross_profit"]
            gross_loss = row["gross_loss"]
            total_pnl = row["total_pnl"]
            win_rate = (wins or 0) / total if total > 0 else 0.0
            profit_factor = (gross_profit / gross_loss) if gross_loss and gross_loss > 0 else 0.0

            return {
                "trade_count": total,
                "win_rate": win_rate,
                "avg_win": avg_win or 0.0,
                "avg_loss": avg_loss or 0.0,
                "profit_factor": profit_factor,
                "total_pnl": total_pnl or 0.0,
            }

    async def delete_trade(self, trade_id: int) -> None:
        """Delete a trade from the journal by its ID."""
        async with self._db.transaction() as conn:
            await conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
        logger.info("Trade deleted from journal: id=%d", trade_id)

    async def get_first_trade_timestamp(self) -> str | None:
        """Get the earliest trade timestamp."""
        async with self._db.connection() as conn:
            cursor = await conn.execute(
                "SELECT timestamp FROM trades ORDER BY timestamp ASC LIMIT 1"
            )
            row = await cursor.fetchone()
            if row:
                return row.get("timestamp") if isinstance(row, dict) else row[0]
            return None

    async def get_realized_pnl_history(self) -> list[dict[str, Any]]:
        """Get all trades with realized PnL, chronologically ordered."""
        async with self._db.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT timestamp, realized_pnl
                FROM trades
                WHERE realized_pnl IS NOT NULL
                AND (order_status = 'FILLED' OR order_status IS NULL)
                ORDER BY timestamp ASC
                """
            )
            rows = await cursor.fetchall()
            return list(rows)

    # ═══════════════════════════════════════════════════════════════
    # Pending Orders — persist unfilled limit orders across restarts
    # ═══════════════════════════════════════════════════════════════

    async def save_pending_order(
        self, order_id: str, trade_json: str, session_id: str | None = None
    ) -> int:
        """Persist a pending (unfilled) order to the database.

        Args:
            order_id: Broker order ID.
            trade_json: Full trade proposal as JSON string.
            session_id: The agent session that placed this order.

        Returns:
            The auto-generated row ID.
        """
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO pending_orders (order_id, session_id, trade_json, status)
                VALUES (?, ?, ?, 'PENDING')
                ON CONFLICT(order_id) DO UPDATE SET
                    trade_json = excluded.trade_json,
                    session_id = excluded.session_id,
                    updated_at = datetime('now')
                """,
                (order_id, session_id, trade_json),
            )
            return cursor.lastrowid

    async def get_order_ids_awaiting_broker(self) -> list[dict]:
        """Every broker order the journal still believes is live.

        The union of the watch list (``pending_orders``) and any journal row
        that is PENDING with a broker order id. The watch list alone missed a
        take-profit placed 2026-09-14, before orders were watch-listed, so
        reconciliation never asked the broker about it and it sat PENDING for
        nine sessions, counted as resting cover by the protection audit.

        Deliberately no age cutoff on the two PENDING sources: a far gtc
        take-profit can legitimately rest for weeks, and expiring it by age
        would mark it cancelled in the journal while it is still live at the
        broker. The broker's answer decides.

        The third source is the opposite case — a row the journal believes
        FILLED on nothing but the executor's word (``fill_source =
        'executor_claim'``, see migration 0023). Until 2026-09-25 a wrongly
        claimed fill was never re-asked about: it was not PENDING, so it never
        reached this list, and the broker's "unconfirmed" answer could not
        reach the row. Those DO get an age cutoff, because a genuine fill is
        confirmed within a session or two and re-asking forever would put every
        historical entry on the list.
        """
        async with self._db.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT order_id FROM pending_orders WHERE status = 'PENDING'
                UNION
                SELECT order_id FROM trades
                WHERE order_status = 'PENDING' AND order_id IS NOT NULL AND order_id != ''
                UNION
                SELECT order_id FROM trades
                WHERE order_status = 'FILLED'
                  AND fill_source = 'executor_claim'
                  AND order_id IS NOT NULL AND order_id != ''
                  AND timestamp >= datetime('now', ?)
                """,
                (f"-{_CLAIM_RECHECK_DAYS} days",),
            )
            return [{"order_id": r["order_id"]} for r in await cursor.fetchall()]

    async def get_pending_orders(self) -> list[dict]:
        """Retrieve all orders still in PENDING status.

        Returns:
            List of dicts with order_id, session_id, trade_json, status, created_at.
        """
        async with self._db.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT order_id, session_id, trade_json, status, created_at, updated_at
                FROM pending_orders
                WHERE status = 'PENDING'
                ORDER BY created_at ASC
                """
            )
            rows = await cursor.fetchall()
            return list(rows)

    async def resolve_pending_order(self, order_id: str, terminal_status: str) -> None:
        """Mark a pending order as resolved (filled, rejected, cancelled, failed).

        The row is kept for audit purposes but excluded from future reconciliation.

        Args:
            order_id: Broker order ID.
            terminal_status: Final status (e.g. 'FILLED', 'REJECTED').
        """
        async with self._db.transaction() as conn:
            await conn.execute(
                """
                UPDATE pending_orders
                SET status = ?, updated_at = datetime('now')
                WHERE order_id = ?
                """,
                (terminal_status.upper(), order_id),
            )

    async def delete_pending_order(self, order_id: str) -> None:
        """Remove a pending order row entirely (after successful journaling).

        Args:
            order_id: Broker order ID to remove.
        """
        async with self._db.transaction() as conn:
            await conn.execute(
                "DELETE FROM pending_orders WHERE order_id = ?",
                (order_id,),
            )

    async def relink_unmatched_protective_orders(self) -> int:
        """Attach resting protective orders to the lot they were placed for.

        A stop placed seconds after an entry is journaled before the entry has
        filled, so FIFO matching finds no lot and records it as an unmatched
        audit row with no ``related_trade_id``. If that stop later FILLS it
        never reduces its lot, and the journal keeps showing shares the broker
        has already sold. This re-links such rows once the lot exists.

        Deliberately narrow:
          * resting (PENDING) protective rows only — a dead order has nothing
            to protect, and a filled one needs its own reconciliation;
          * only when exactly ONE filled open lot for the ticker can absorb the
            order — with two candidates, picking one is a guess, and a wrong
            link misstates a position;
          * a CLOSE that carries a stop price is restored to STOP_LOSS, since a
            stop price is unambiguous; a limit-priced CLOSE is left as CLOSE,
            because it may be a planned exit rather than a take-profit.

        Returns the number of rows re-linked. Safe to call every cycle.
        """
        try:
            open_lots = await self.get_open_trades()
        except Exception as e:
            logger.warning("relink: could not read open lots: %s", e)
            return 0

        by_ticker: dict[str, list[dict]] = {}
        for lot in open_lots:
            if str(lot.get("action") or "").upper() != "OPEN":
                continue
            by_ticker.setdefault(str(lot.get("ticker") or "").upper(), []).append(lot)

        async with self._db.connection() as conn:
            cur = await conn.execute(
                """
                SELECT id, ticker, action, quantity, stop_price, broker_status_reason
                FROM trades
                WHERE related_trade_id IS NULL
                  AND action IN ('STOP_LOSS', 'TAKE_PROFIT', 'CLOSE')
                  AND order_status = 'PENDING'
                  AND broker_status_reason LIKE 'UNMATCHED_CLOSE%'
                """
            )
            rows = [dict(r) for r in await cur.fetchall()]

        linked = 0
        for row in rows:
            qty = float(row.get("quantity") or 0.0)
            candidates = [
                lot
                for lot in by_ticker.get(str(row.get("ticker") or "").upper(), [])
                if float(lot.get("remaining_quantity") or 0.0) + 1e-4 >= qty
            ]
            if len(candidates) != 1:
                continue
            lot_id = candidates[0]["id"]
            action = row["action"]
            if action == "CLOSE" and row.get("stop_price") is not None:
                action = "STOP_LOSS"
            reason = f"{row.get('broker_status_reason') or ''}; RELINKED_TO_{lot_id}"
            async with self._db.transaction() as conn:
                await conn.execute(
                    "UPDATE trades SET related_trade_id = ?, action = ?, "
                    "broker_status_reason = ? WHERE id = ? AND related_trade_id IS NULL",
                    (lot_id, action, reason, row["id"]),
                )
            logger.info(
                "Relinked unmatched protective order #%s (%s) to lot #%s",
                row["id"],
                action,
                lot_id,
            )
            linked += 1
        return linked

    async def _pending_open_quantity(
        self, ticker: str, option_id: str | None, direction: str, session_id: str | None
    ) -> float:
        """Shares of buys for this instrument placed in this cycle and still PENDING."""
        if not session_id:
            return 0.0
        async with self._db.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT COALESCE(SUM(quantity), 0) AS pending
                FROM trades
                WHERE action = 'OPEN' AND order_status = 'PENDING'
                  AND ticker = ? AND COALESCE(option_id, '') = ?
                  AND direction = ? AND session_id = ?
                """,
                (ticker, option_id or "", direction, session_id),
            )
            row = await cursor.fetchone()
        return float(row["pending"] or 0.0) if row else 0.0

    async def update_order_status(
        self,
        order_id: str,
        new_status: str,
        fill_price: float | None = None,
        filled_quantity: float | None = None,
        broker_status_reason: str | None = None,
        only_if_claimed_fill: bool = False,
        fill_source: str | None = None,
    ) -> int:
        """Update a trade's order lifecycle status by broker order_id.

        Used by reconciliation when the broker confirms a terminal state
        (FILLED, REJECTED, CANCELLED, FAILED) for a previously PENDING trade.

        Every row sharing the order id is updated, except one a data repair
        closed out (CANCELLED/FAILED/EXPIRED/REJECTED with a ``REPAIRED_``
        reason), which stays as the repair left it. Such rows can share the id
        of the order that is still live — the phantom rows of 2026-09-25 shared
        a resting stop's — and news about that order is about the live row: on
        2026-09-28 a sweep relabelled repaired rows "expired" when the stop was
        cancelled, and a fill would have turned a phantom SHORT into a real one.

        Args:
            order_id: Broker order ID.
            new_status: New status (e.g. 'FILLED', 'REJECTED').
            fill_price: Actual fill price (for FILLED transitions).
            filled_quantity: Actual filled quantity (for partial fills).
            broker_status_reason: Rejection/cancellation reason from broker.
            only_if_claimed_fill: Restrict the update to rows the journal marked
                FILLED on the executor's word alone (``fill_source =
                'executor_claim'``). Used to walk back a claimed fill the broker
                says is still working, without touching a row the broker itself
                confirmed. Without this guard the same call would rewrite a
                genuine fill that happens to share the order id.
            fill_source: Where the fill's price came from. Default: ``broker``
                for FILLED, cleared otherwise. ``broker_position`` records a
                fill the broker's position confirms while the price is still
                the journal's own.

        Returns:
            Number of rows updated.
        """
        updates = ["order_status = ?"]
        params: list[Any] = [new_status.upper()]

        # A row in any of these states closed nothing, so it cannot carry
        # realized P&L. PENDING and EXPIRED joined the list on 2026-09-25:
        # downgrading a wrongly-claimed fill back to PENDING has to take its
        # phantom P&L with it, or analyse_performance keeps reporting the
        # phantom loss after the status is corrected.
        if new_status.upper() in ("REJECTED", "CANCELLED", "FAILED", "PENDING", "EXPIRED"):
            updates.append("realized_pnl = NULL")

        if fill_price is not None:
            updates.append("fill_price = ?")
            params.append(fill_price)
            # Also update the canonical `price` column so P&L calculations,
            # get_open_trades(), and the UI all reflect the actual execution
            # price rather than the stale limit price from initial recording.
            updates.append("price = ?")
            params.append(fill_price)
        if filled_quantity is not None:
            updates.append("quantity = ?")
            params.append(filled_quantity)
        if broker_status_reason is not None:
            updates.append("broker_status_reason = ?")
            params.append(broker_status_reason)

        # The broker has now spoken, so the row is no longer an unverified
        # claim either way — record which.
        updates.append("fill_source = ?")
        if fill_source is None:
            fill_source = "broker" if new_status.upper() == "FILLED" else None
        params.append(fill_source)

        params.append(order_id)
        where = "order_id = ?"
        if only_if_claimed_fill:
            where += " AND order_status = 'FILLED' AND fill_source = 'executor_claim'"
        where += (
            " AND NOT (COALESCE(order_status, '') IN ('CANCELLED', 'FAILED', 'EXPIRED',"
            " 'REJECTED') AND substr(COALESCE(broker_status_reason, ''), 1, 9) = 'REPAIRED_')"
        )
        set_clause = ", ".join(updates)

        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                f"UPDATE trades SET {set_clause} WHERE {where}",
                params,
            )
            rows_affected = cursor.rowcount

        if rows_affected:
            logger.info(
                "Order %s status updated to %s (fill_price=%s, reason=%s)",
                order_id,
                new_status,
                fill_price,
                broker_status_reason,
            )
            if fill_price is not None:
                # Surface retroactive price corrections so the evolution agent
                # can audit reasoning/learnings derived from the stale price.
                logger.warning(
                    "FILL_PRICE_RETROACTIVE_CORRECTION order=%s new_fill=%.4f — "
                    "any same-session unrealized P&L or stored learnings computed "
                    "before this update may be contaminated.",
                    order_id,
                    fill_price,
                )

            # If a CLOSE trade was promoted to FILLED, compute realized_pnl from linked entry trade
            if new_status.upper() == "FILLED":
                try:
                    async with self._db.connection() as conn:
                        cursor = await conn.execute(
                            "SELECT id, action, direction, quantity, price, fill_price, option_id, related_trade_id, timestamp, realized_pnl, holding_period_s FROM trades WHERE order_id = ?",
                            (order_id,),
                        )
                        close_rows = [dict(r) for r in await cursor.fetchall()]

                    for crow in close_rows:
                        if crow.get("action") == "CLOSE" and crow.get("related_trade_id"):
                            entry_row = await self.get_trade_by_id(int(crow["related_trade_id"]))
                            if entry_row:
                                e_fill = entry_row.get("fill_price")
                                e_price = (
                                    float(e_fill)
                                    if e_fill is not None
                                    else float(entry_row["price"])
                                )
                                c_fill = (
                                    fill_price if fill_price is not None else crow.get("fill_price")
                                )
                                c_price = (
                                    float(c_fill) if c_fill is not None else float(crow["price"])
                                )
                                qty = float(crow["quantity"])
                                mult = 100.0 if crow.get("option_id") else 1.0
                                lot_dir = entry_row.get("direction", "LONG")
                                calc_pnl = (
                                    (c_price - e_price) * qty * mult
                                    if lot_dir == "LONG"
                                    else (e_price - c_price) * qty * mult
                                )

                                held_s = crow.get("holding_period_s")
                                if (
                                    not held_s
                                    and crow.get("timestamp")
                                    and entry_row.get("timestamp")
                                ):
                                    try:
                                        t_close = datetime.fromisoformat(crow["timestamp"])
                                        t_open = datetime.fromisoformat(entry_row["timestamp"])
                                        held_s = int((t_close - t_open).total_seconds())
                                    except Exception:
                                        pass

                                async with self._db.transaction() as conn:
                                    await conn.execute(
                                        "UPDATE trades SET realized_pnl = ?, holding_period_s = COALESCE(?, holding_period_s) WHERE id = ?",
                                        (calc_pnl, held_s, crow["id"]),
                                    )
                except Exception as ex:
                    logger.warning(
                        "Failed to recalculate realized_pnl on order status fill for %s: %s",
                        order_id,
                        ex,
                    )
        else:
            logger.warning("update_order_status: no trade found with order_id=%s", order_id)
        return rows_affected

    async def get_trades_by_order_status(self, status: str, limit: int = 100) -> list[dict]:  # type: ignore[type-arg]
        """Query trades by their order lifecycle status.

        Useful for finding PENDING trades that need reconciliation, or
        REJECTED/CANCELLED trades for evolution agent diagnostics.

        Args:
            status: Order status to filter by (e.g. 'PENDING', 'REJECTED').
            limit: Maximum number of results.

        Returns:
            List of trade dicts matching the status.
        """
        async with self._db.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM trades
                WHERE order_status = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (status.upper(), limit),
            )
            return list(await cursor.fetchall())
