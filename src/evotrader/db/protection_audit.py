"""Detect held positions whose protective orders are not actually resting.

A refused protective order leaves no trace in the order book. On 2026-09-21 the
take-profit for an MSTR long was rejected ("Not enough shares to sell" —
the entry had not filled nine seconds earlier), the cycle summary reported
"entry and protective stop-loss have been placed", and the position ran all day
with no target. That was not the model misreading anything: nothing in the
system ever compared what was intended against what was resting.

Since 2026-09-11 no stop or take-profit has executed at all — every one is
FAILED, CANCELLED or stuck PENDING — and the broker has refused dozens of orders,
many of them for "Not enough shares to sell". A prose instruction to be careful does not
catch that class of gap; a check does.

This module only OBSERVES. It places no orders and cancels nothing: the repair
belongs to the next cycle's agent, which already has that instruction. What this
adds is that the gap becomes visible — in the log, and in the thought log where
the cycle digest will show it.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_RESTING_STATUSES = frozenset({"PENDING", "QUEUED", "CONFIRMED", "UNCONFIRMED"})
_STOP_ORDER_TYPES = frozenset({"stop", "stop_market", "stop_limit", "trailing_stop"})
_QTY_TOLERANCE = 1e-4


def _norm(value: Any) -> str:
    return str(value or "").strip().upper()


def _positive(value: Any) -> bool:
    try:
        return value is not None and float(value) > 0
    except (TypeError, ValueError):
        return False


def _is_stop(row: dict[str, Any]) -> bool:
    """A stop by SHAPE, not by label.

    A resting stop that the journal had relabelled CLOSE (see
    journal.record_trade's unmatched path) was reported missing on 9
    consecutive cycles by matching on the action alone. A stop price, or a stop
    order type, is what makes an order a stop. `direction` is NOT used: it is
    recorded inconsistently — every correctly-labelled protective row in the
    live journal is LONG, while that relabelled stop is SHORT — so it would
    reject real stops either way.
    """
    if _norm(row.get("action")) == "OPEN":
        return False
    if _norm(row.get("action")) == "STOP_LOSS":
        return True
    if _positive(row.get("stop_price")):
        return True
    return str(row.get("order_type") or "").strip().lower() in _STOP_ORDER_TYPES


def _is_take_profit(row: dict[str, Any]) -> bool:
    action = _norm(row.get("action"))
    if action == "OPEN" or _is_stop(row):
        return False
    if action == "TAKE_PROFIT":
        return True
    return action == "CLOSE" and _positive(row.get("limit_price"))


def cover_from_broker_orders(pending_orders: list[dict[str, Any]]) -> dict[str, float]:
    """Resting stop quantity per ticker, from the broker's own working orders.

    ``pending_orders`` rows carry ``source: 'broker_sync'`` when the broker
    listed the order itself (see ``tools.sync_open_orders_from_broker``). Only
    those count here: a row we wrote at submission time is our claim, and this
    function exists to have something independent of our claims.
    """
    cover: dict[str, float] = {}
    for row in pending_orders:
        if str(row.get("source") or "") != "broker_sync":
            continue
        if not _is_stop(row):
            continue
        ticker = _norm(row.get("ticker"))
        try:
            qty = float(row.get("quantity") or 0.0)
        except (TypeError, ValueError):
            continue
        cover[ticker] = cover.get(ticker, 0.0) + qty
    return cover


def find_protection_gaps(
    open_trades: list[dict[str, Any]],
    recent_trades: list[dict[str, Any]],
    broker_cover: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """One entry per held position whose resting stop does not cover it.

    ``recent_trades`` supplies the protective orders to match against; an order
    counts as cover only while it is genuinely resting, so a FAILED or
    CANCELLED one is exactly the case this looks for. A take-profit is reported
    but never counted as cover: it protects nothing on the way down, and full
    stop coverage legitimately leaves it no room (a resting stop reserves its
    shares).

    Each gap names the rows it counted, so a verdict the next cycle disagrees
    with can be checked against the journal rather than merely contradicted.
    """
    stop_qty: dict[str, float] = {}
    tp_qty: dict[str, float] = {}
    stop_rows: dict[str, list[Any]] = {}
    for row in recent_trades:
        if _norm(row.get("order_status")) not in _RESTING_STATUSES:
            continue
        ticker = _norm(row.get("ticker"))
        try:
            qty = float(row.get("quantity") or 0.0)
        except (TypeError, ValueError):
            continue
        if _is_stop(row):
            stop_qty[ticker] = stop_qty.get(ticker, 0.0) + qty
            stop_rows.setdefault(ticker, []).append(row.get("id"))
        elif _is_take_profit(row):
            tp_qty[ticker] = tp_qty.get(ticker, 0.0) + qty

    gaps: list[dict[str, Any]] = []
    for trade in open_trades:
        ticker = _norm(trade.get("ticker"))
        try:
            held = float(trade.get("remaining_quantity") or trade.get("quantity") or 0.0)
        except (TypeError, ValueError):
            continue
        if held <= _QTY_TOLERANCE:
            continue
        journal_cover = stop_qty.get(ticker, 0.0)
        # ── BROKER COVER OVERRIDES JOURNAL COVER ─────────────────────
        # This module exists to catch a position whose stop is not really
        # resting, and on 2026-09-25 it read the corrupted journal and reported
        # "UNPROTECTED POSITION" on three consecutive cycles while the broker
        # held a stop covering the whole position. The check that is supposed
        # to find the gap cannot be downstream of the thing that was wrong.
        #
        # When the broker's own order list is available it decides, because it
        # is the only account of the order book that is not our claim. A
        # disagreement between the two is itself worth reporting: it means a
        # journal row is wrong, which is the more serious defect.
        broker_known = broker_cover is not None and ticker in broker_cover
        covered = broker_cover[ticker] if broker_known else journal_cover
        disagrees = broker_known and abs(broker_cover[ticker] - journal_cover) > _QTY_TOLERANCE
        if covered + _QTY_TOLERANCE >= held:
            if disagrees:
                logger.warning(
                    "%s is covered at the broker (%.4f) but the journal counts "
                    "only %.4f resting. No protection gap, but a journal row is "
                    "wrong — check for a fill claimed without evidence.",
                    ticker,
                    broker_cover[ticker],
                    journal_cover,
                )
            continue
        gaps.append(
            {
                "trade_id": trade.get("id"),
                "ticker": ticker,
                "held_quantity": round(held, 4),
                "stop_quantity_resting": round(covered, 4),
                "journal_cover": round(journal_cover, 4),
                "broker_cover": round(broker_cover[ticker], 4) if broker_known else None,
                "cover_source": "broker" if broker_known else "journal",
                "sources_disagree": disagrees,
                "take_profit_quantity_resting": round(tp_qty.get(ticker, 0.0), 4),
                "uncovered_quantity": round(held - covered, 4),
                "rows_counted": [r for r in stop_rows.get(ticker, []) if r is not None],
            }
        )
    return gaps


async def audit_protection(
    journal: Any, thought_logger: Any = None, session_id: str | None = None
) -> dict[str, Any]:
    """Log any position whose stop is not resting. Observes only."""
    try:
        open_trades = await journal.get_open_trades()
        if not open_trades:
            return {"positions": 0, "gaps": []}
        recent = await journal.get_recent_trades(limit=60)
        # The broker's working-order list, when a cycle has synced one. Absent
        # in sim (no broker orders exist) and on a cycle whose sync failed, and
        # then the journal remains the source — degraded, but never silently:
        # a gap reported from the journal alone says so in `cover_source`.
        broker_cover = None
        try:
            pending = await journal.get_pending_orders()
            parsed: list[dict[str, Any]] = []
            for row in pending:
                try:
                    parsed.append(json.loads(row.get("trade_json") or "{}"))
                except (json.JSONDecodeError, TypeError):
                    continue
            if any(p.get("source") == "broker_sync" for p in parsed):
                broker_cover = cover_from_broker_orders(parsed)
        except Exception as e:
            logger.debug("Could not read the broker order book: %s", e)
        gaps = find_protection_gaps(open_trades, recent, broker_cover=broker_cover)
    except Exception as e:
        logger.warning("Protection audit failed: %s", e)
        return {"positions": 0, "gaps": [], "error": str(e)}

    for gap in gaps:
        logger.error(
            "UNPROTECTED POSITION: %s x%.4f has only %.4f covered by a resting stop "
            "(%.4f uncovered, counted from the %s). A protective order was refused "
            "or cancelled — re-place it next cycle.",
            gap["ticker"],
            gap["held_quantity"],
            gap["stop_quantity_resting"],
            gap["uncovered_quantity"],
            gap.get("cover_source", "journal"),
        )
    # Into the handoff as well, so the next cycle reads it in its prompt rather
    # than having to go looking. This runs post-cycle in a `finally`, so it
    # still lands when the agent died before writing its own note.
    if gaps:
        try:
            from evotrader.agents import tools as _tools_mod
            from evotrader.tools.trading_handoff import append_system_line

            cfg = getattr(_tools_mod, "_config", None)
            if cfg is not None:
                append_system_line(
                    cfg.data_dir,
                    (
                        "protection gap — "
                        + "; ".join(
                            f"{g['ticker']} x{g['held_quantity']:g} has {g['uncovered_quantity']:g} "
                            f"uncovered by a resting stop (stop rows counted: "
                            + (", ".join(f"#{r}" for r in g["rows_counted"]) or "none")
                            + ")"
                            for g in gaps
                        )
                        + ". Counted from the "
                        + "/".join(sorted({g.get("cover_source", "journal") for g in gaps}))
                        + ". A journal-sourced count can be wrong — verify against "
                        "get_equity_orders before re-placing anything."
                    ),
                )
        except Exception as e:
            logger.debug("Could not write the protection gap to the handoff: %s", e)

    if gaps and thought_logger is not None:
        try:
            await thought_logger.record_event(
                session_id=session_id,
                agent_name="orchestrator",
                event_type="runtime",
                content=(
                    "[runtime] protection gap: "
                    + "; ".join(
                        f"{g['ticker']} x{g['held_quantity']:g} uncovered "
                        f"{g['uncovered_quantity']:g}"
                        for g in gaps
                    )
                ),
                meta={"gaps": gaps},
            )
        except Exception as e:
            logger.debug("Could not record protection gap: %s", e)
    return {"positions": len(open_trades), "gaps": gaps}
