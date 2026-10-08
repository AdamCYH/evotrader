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


def _quantity(row: dict[str, Any]) -> float:
    try:
        return float(row.get("quantity") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def held_by_ticker(open_trades: list[dict[str, Any]]) -> dict[str, tuple[float, list[Any]]]:
    """Shares held per ticker, summed over every open equity lot, with the lot ids.

    Summed, never lot by lot. The audit used to compare each lot with the
    ticker's whole resting stop, so a position bought in two lots — say 10
    shares, then 4 more — under a stop still sized for the first 10 passed:
    each lot alone fits under 10. The 4 shares added last ran with no stop
    until a later cycle's agent redid the arithmetic by hand.

    Option lots are left out: they are contracts, not shares, and a stop on
    the stock does not protect them. A SHORT lot is left out too: a sell stop
    does not protect a short.
    """
    held: dict[str, float] = {}
    lots: dict[str, list[Any]] = {}
    for trade in open_trades:
        if trade.get("option_id") or _norm(trade.get("direction")) == "SHORT":
            continue
        try:
            qty = float(trade.get("remaining_quantity") or trade.get("quantity") or 0.0)
        except (TypeError, ValueError):
            continue
        if qty <= _QTY_TOLERANCE:
            continue
        ticker = _norm(trade.get("ticker"))
        held[ticker] = held.get(ticker, 0.0) + qty
        lots.setdefault(ticker, []).append(trade.get("id"))
    return {ticker: (qty, lots[ticker]) for ticker, qty in held.items()}


def coverage_by_ticker(
    open_trades: list[dict[str, Any]], book: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Shares held against shares a resting stop would sell, per ticker.

    The number every cycle used to work out by hand, from the two lists
    ``get_open_positions`` already returns: the journal's open lots and the
    resting order book (``pending_orders``, which reconciliation refreshes from
    the broker's own list at the start and the end of every cycle). One
    function, so the strategy agent, the executor, the orchestrator and the
    post-cycle audit all quote the same number.

    The rules are the audit's:

    * held: every open equity lot of the ticker, summed (``held_by_ticker``).
    * covered: resting STOPS only. A take-profit is listed with its quantity
      but protects nothing on the way down.
    * A stop is recognised by its shape (action, stop price or stop order
      type), never by the direction field, which protective rows record
      inconsistently.
    * One row per order id, the broker's own copy over our submission record.

    ``status``:

    * ``full``: the stops cover every share held, and each has a known
      trigger price and time in force.
    * ``unknown``: the quantity is covered, but a counted stop lacks its
      trigger price or its time in force. Unverified, not protected.
    * ``split``: some shares are under a resting take-profit instead of the
      stop, and every share is under one or the other: the designed split of
      a take-profit tranche (a resting order reserves its shares, so the stop
      and the take-profit cannot both sit on a share). ``tp_only_qty`` says
      how many shares have no stop.
    * ``partial``: some shares are under no resting sell order at all;
      ``no_order_qty`` says how many (``uncovered_qty`` counts every share
      without a stop). A shortfall is never hidden behind ``unknown`` or
      ``split``.
    * ``none``: shares held and no resting stop (a take-profit alone is not
      protection).
    * ``flat``: nothing held. A stop still resting here is a leftover
      (``excess_stop_qty``).
    """
    by_id: dict[str, dict[str, Any]] = {}
    unnamed: list[dict[str, Any]] = []
    for row in book:
        if row.get("option_id"):
            continue
        order_id = str(row.get("order_id") or "")
        if not order_id:
            unnamed.append(row)
            continue
        kept = by_id.get(order_id)
        if kept is None or (
            row.get("source") == "broker_sync" and kept.get("source") != "broker_sync"
        ):
            by_id[order_id] = row
    rows_by_ticker: dict[str, list[dict[str, Any]]] = {}
    for row in [*by_id.values(), *unnamed]:
        rows_by_ticker.setdefault(_norm(row.get("ticker")), []).append(row)

    held = held_by_ticker(open_trades)
    out: dict[str, dict[str, Any]] = {}
    for ticker in sorted(set(held) | set(rows_by_ticker)):
        if ticker in ("", "?"):
            continue
        held_qty, lot_ids = held.get(ticker, (0.0, []))
        stops: list[dict[str, Any]] = []
        take_profits: list[dict[str, Any]] = []
        entries: list[dict[str, Any]] = []
        for row in rows_by_ticker.get(ticker, []):
            if _is_stop(row):
                stops.append(row)
            elif _is_take_profit(row):
                take_profits.append(row)
            elif _norm(row.get("action")) == "OPEN":
                entries.append(row)
        if held_qty <= _QTY_TOLERANCE and not stops and not take_profits and not entries:
            continue

        covered = sum(_quantity(r) for r in stops)
        tp_qty = sum(_quantity(r) for r in take_profits)
        entry_qty = sum(_quantity(r) for r in entries)
        uncovered = max(0.0, held_qty - covered)
        tp_only = min(tp_qty, uncovered)
        no_order = max(0.0, uncovered - tp_qty)
        unverified = any(
            not _positive(r.get("stop_price")) or not r.get("time_in_force") for r in stops
        )
        if held_qty <= _QTY_TOLERANCE:
            status = "flat"
        elif covered <= _QTY_TOLERANCE:
            status = "none"
        elif no_order > _QTY_TOLERANCE:
            status = "partial"
        elif unverified:
            status = "unknown"
        elif uncovered > _QTY_TOLERANCE:
            status = "split"
        else:
            status = "full"
        sources = {"broker" if r.get("source") == "broker_sync" else "journal" for r in stops}

        out[ticker] = {
            "status": status,
            "held_qty": round(held_qty, 4),
            "covered_qty": round(covered, 4),
            "uncovered_qty": round(uncovered if uncovered > _QTY_TOLERANCE else 0.0, 4),
            "excess_stop_qty": round(max(0.0, covered - held_qty), 4),
            "take_profit_qty": round(tp_qty, 4),
            # Shares under a resting take-profit and no stop (a split), and
            # shares under no resting sell order at all (a gap).
            "tp_only_qty": round(tp_only if tp_only > _QTY_TOLERANCE else 0.0, 4),
            "no_order_qty": round(no_order if no_order > _QTY_TOLERANCE else 0.0, 4),
            "pending_entry_qty": round(entry_qty, 4),
            # Resting buys: if they fill, these shares join the position, and
            # this is what the stops would then leave uncovered.
            "uncovered_if_entries_fill": round(max(0.0, held_qty + entry_qty - covered), 4),
            "gtc_all": bool(stops)
            and all(str(r.get("time_in_force") or "").lower() == "gtc" for r in stops),
            "cover_source": (sources.pop() if len(sources) == 1 else "mixed") if sources else None,
            "lot_ids": [i for i in lot_ids if i is not None],
            "orders": [
                {
                    "order_id": r.get("order_id"),
                    "role": role,
                    "quantity": round(_quantity(r), 4),
                    "stop_price": r.get("stop_price"),
                    "limit_price": r.get("limit_price"),
                    "time_in_force": r.get("time_in_force"),
                    "source": r.get("source"),
                }
                for role, rows in (
                    ("stop", stops),
                    ("take_profit", take_profits),
                    ("entry", entries),
                )
                for r in rows
            ],
        }
    return out


def take_profit_coverage(
    block: dict[str, Any] | None,
    open_trades: list[dict[str, Any]],
    ticker: str,
    atr: float | None = None,
    mark: float | None = None,
) -> dict[str, Any] | None:
    """The upside half of the book for one ticker: which take-profits rest, and where.

    ``coverage_by_ticker`` answers "is every share under a stop"; this answers
    "is any share under a resting take-profit, and how far away is it". The
    levels are in daily ATRs from the position's blended entry and from the
    current mark when those are known (the market-data snapshot passes them;
    the positions tool, which reads only the journal, does not).
    """
    if not block:
        return None
    ticker = _norm(ticker)
    lots = [
        t
        for t in open_trades
        if _norm(t.get("ticker")) == ticker
        and not t.get("option_id")
        and _norm(t.get("direction")) != "SHORT"
    ]
    qty_sum = 0.0
    cost_sum = 0.0
    for lot in lots:
        try:
            qty = float(lot.get("remaining_quantity") or lot.get("quantity") or 0.0)
            entry = float(lot.get("fill_price") or lot.get("price") or 0.0)
        except (TypeError, ValueError):
            continue
        if qty > _QTY_TOLERANCE and entry > 0:
            qty_sum += qty
            cost_sum += qty * entry
    blended = cost_sum / qty_sum if qty_sum > _QTY_TOLERANCE else None
    atr_ok = atr is not None and atr > 0

    def _in_atr(level: float | None, base: float | None) -> float | None:
        if level is None or base is None or not atr_ok:
            return None
        return round((float(level) - base) / float(atr), 4)

    levels = []
    for order in block.get("orders") or []:
        if order.get("role") != "take_profit":
            continue
        limit = order.get("limit_price")
        levels.append(
            {
                "order_id": order.get("order_id"),
                "qty": order.get("quantity"),
                "limit_price": limit,
                "atr_from_entry": _in_atr(limit, blended),
                "atr_from_mark": _in_atr(limit, mark),
                "time_in_force": order.get("time_in_force"),
            }
        )
    tp_qty = float(block.get("take_profit_qty") or 0.0)
    held = float(block.get("held_qty") or 0.0)
    return {
        "status": "resting" if tp_qty > _QTY_TOLERANCE else "none",
        "tp_qty": round(tp_qty, 4),
        "tp_qty_uncovered": round(max(0.0, held - tp_qty), 4),
        "blended_entry": round(blended, 4) if blended is not None else None,
        "levels": levels,
    }


def coverage_record(block: dict[str, Any] | None) -> str | None:
    """One ticker's coverage as the compact JSON the journal keeps on a row."""
    if not block:
        return None
    keep = (
        "status",
        "held_qty",
        "covered_qty",
        "uncovered_qty",
        "excess_stop_qty",
        "take_profit_qty",
        "tp_only_qty",
        "pending_entry_qty",
        "gtc_all",
        "cover_source",
    )
    record = {key: block.get(key) for key in keep}
    record["stop_order_ids"] = [
        o.get("order_id") for o in block.get("orders") or [] if o.get("role") == "stop"
    ]
    return json.dumps(record, separators=(",", ":"))


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


def take_profit_cover_from_broker_orders(pending_orders: list[dict[str, Any]]) -> dict[str, float]:
    """Resting take-profit quantity per ticker, from the broker's own working orders."""
    cover: dict[str, float] = {}
    for row in pending_orders:
        if str(row.get("source") or "") != "broker_sync" or not _is_take_profit(row):
            continue
        cover[_norm(row.get("ticker"))] = cover.get(_norm(row.get("ticker")), 0.0) + _quantity(row)
    return cover


def find_protection_gaps(
    open_trades: list[dict[str, Any]],
    recent_trades: list[dict[str, Any]],
    broker_cover: dict[str, float] | None = None,
    broker_take_profit: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """One entry per held ticker whose resting stop does not cover it.

    Held is the ticker's whole position, every open lot summed
    (``held_by_ticker``): a stop covers a position, not a lot.

    ``recent_trades`` supplies the protective orders to match against; an order
    counts as cover only while it is genuinely resting, so a FAILED or
    CANCELLED one is exactly the case this looks for. A take-profit is reported
    but never counted as cover: it protects nothing on the way down, and full
    stop coverage legitimately leaves it no room (a resting stop reserves its
    shares). The one exception is a SPLIT: when the stop and a resting
    take-profit between them hold every share, the take-profit's shares are
    the designed tranche, not a gap (``coverage_by_ticker`` status ``split``).
    That is logged, not reported.

    Each gap names the lots it summed and the rows it counted, so a verdict the
    next cycle disagrees with can be checked against the journal rather than
    merely contradicted.
    """
    stop_qty: dict[str, float] = {}
    tp_qty: dict[str, float] = {}
    stop_rows: dict[str, list[Any]] = {}
    for row in recent_trades:
        if _norm(row.get("order_status")) not in _RESTING_STATUSES:
            continue
        if row.get("option_id"):
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
    for ticker, (held, lot_ids) in held_by_ticker(open_trades).items():
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
        broker_tp_known = broker_take_profit is not None and broker_cover is not None
        tp_resting = (
            broker_take_profit.get(ticker, 0.0) if broker_tp_known else tp_qty.get(ticker, 0.0)
        )
        if covered + _QTY_TOLERANCE < held and covered > _QTY_TOLERANCE:
            if covered + tp_resting + _QTY_TOLERANCE >= held:
                logger.info(
                    "%s is split by design: %.4f under the stop, %.4f under a resting "
                    "take-profit with no stop, of %.4f held.",
                    ticker,
                    covered,
                    held - covered,
                    held,
                )
                continue
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
        lots = [i for i in lot_ids if i is not None]
        gaps.append(
            {
                # The oldest lot, as before; every lot summed is in trade_ids.
                "trade_id": lots[0] if lots else None,
                "trade_ids": lots,
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
        broker_take_profit = None
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
                broker_take_profit = take_profit_cover_from_broker_orders(parsed)
        except Exception as e:
            logger.debug("Could not read the broker order book: %s", e)
        gaps = find_protection_gaps(
            open_trades,
            recent,
            broker_cover=broker_cover,
            broker_take_profit=broker_take_profit,
        )
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
