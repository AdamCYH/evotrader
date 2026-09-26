"""Backfill realised forward returns onto attribution rows.

``signal_attribution`` records, every cycle, what each witness said and what was
decided. That record only becomes *evidence* once the market has answered, and
the answer has to be written back. Until 2026-09-18 nothing did: 74 rows had
accumulated across two instruments and 0 of them carried a forward return, so
the question the table exists to settle — which witness actually predicts, and
does stated conviction match realised accuracy — had never been asked inside
the system.

Definitions, because "forward return" is ambiguous enough to measure the wrong
thing silently:

* The base price is the row's ``price_at_decision`` — the price the decision
  was actually made at, not a candle close near it.
* ``forward_return_1d`` is measured to the close of the first session that ends
  **after the decision's own date**, and ``forward_return_5d`` to the fifth such
  session. Anchoring on "sessions after this date" rather than "index of the
  decision's own candle + 1" is deliberate: intraday, the daily series from the
  provider ends at the PRIOR session, so counting from the last available bar
  would have silently measured the decision day's own close as a one-day-ahead
  return. That is the same defect that left ``gap_pct`` null on every live cycle
  (review 20260918, findings 4-5).
* Units are **percent**: ``+2.16`` means +2.16%. The columns predate the repo's
  ``_pct`` naming convention; the unit is percent regardless.

A row is stamped ``scored_at`` as soon as its 1-day horizon resolves, because
that is a complete measurement of the 1-day question. The 5-day column is
backfilled later by :meth:`AttributionScorer.backfill_long_horizon`. Waiting for
the 5-day horizon before recording anything would leave the most recent week —
which, for a system this young, is most of the data — invisible to evolution.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Sessions after the decision date, per column.
_HORIZONS: dict[str, int] = {"forward_return_1d": 1, "forward_return_5d": 5}

#: ``(ticker, now) -> [{"timestamp": ..., "close": ...}, ...]``, oldest first.
DailyCandleFetcher = Callable[[str, datetime], Awaitable[Sequence[dict[str, Any]]]]


def _as_date(value: Any) -> date | None:
    """Date part of an ISO timestamp, whatever timezone suffix it carries."""
    if isinstance(value, datetime):
        return value.date()
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return datetime.fromisoformat(text[:10]).date()
        except ValueError:
            logger.debug("Unparseable timestamp for attribution scoring: %r", value)
            return None


def forward_closes(candles: Sequence[dict[str, Any]], decision_date: date) -> list[float]:
    """Closes of the sessions that end after *decision_date*, in order.

    Same-day and earlier bars are dropped, so the result is "what happened
    next" and never the decision's own session.
    """
    out: list[float] = []
    for candle in candles:
        candle_date = _as_date(candle.get("timestamp"))
        if candle_date is None or candle_date <= decision_date:
            continue
        close = candle.get("close")
        try:
            close_f = float(close)
        except (TypeError, ValueError):
            continue
        if close_f > 0:
            out.append(close_f)
    return out


def returns_for(
    candles: Sequence[dict[str, Any]], decision_date: date, base_price: float
) -> dict[str, float]:
    """Percent returns from *base_price* to each resolvable horizon."""
    if not base_price or base_price <= 0:
        return {}
    closes = forward_closes(candles, decision_date)
    out: dict[str, float] = {}
    for column, sessions in _HORIZONS.items():
        if len(closes) >= sessions:
            out[column] = round((closes[sessions - 1] / base_price - 1.0) * 100.0, 4)
    return out


class AttributionScorer:
    """Writes realised forward returns back onto ``signal_attribution`` rows."""

    def __init__(
        self,
        store: Any,
        fetch_daily_candles: DailyCandleFetcher,
        *,
        lookback_days: int = 120,
    ) -> None:
        self._store = store
        self._fetch = fetch_daily_candles
        self._lookback_days = lookback_days

    async def _candles_by_ticker(
        self, tickers: Sequence[str], now: datetime
    ) -> dict[str, Sequence[dict[str, Any]]]:
        """One fetch per instrument, not one per row."""
        out: dict[str, Sequence[dict[str, Any]]] = {}
        for ticker in sorted(set(tickers)):
            if not ticker:
                continue
            try:
                out[ticker] = await self._fetch(ticker, now) or []
            except Exception as e:  # a data outage must not break the cycle
                logger.warning("Attribution scoring: no candles for %s: %s", ticker, e)
                out[ticker] = []
        return out

    async def score_pending(self, limit: int = 500, now: datetime | None = None) -> dict[str, int]:
        """Score rows whose 1-day horizon has resolved.

        Rows whose next session has not closed yet (a Friday decision read on
        Saturday, a holiday) resolve to nothing and stay pending — they are
        counted as ``waiting``, not failed.
        """
        now = now or datetime.now(UTC)
        # horizon_days=1: eligible a calendar day on. Whether the market has
        # actually printed a close since is decided by the candles below, which
        # is the only honest test of it.
        rows = await self._store.pending_scoring(horizon_days=1, limit=limit, now=now)
        if not rows:
            return {"examined": 0, "scored": 0, "waiting": 0}

        candles = await self._candles_by_ticker([r.get("ticker") for r in rows], now)
        scored = waiting = 0
        for row in rows:
            decision_date = _as_date(row.get("timestamp"))
            base = row.get("price_at_decision")
            if decision_date is None or not base:
                waiting += 1
                continue
            got = returns_for(candles.get(row.get("ticker"), []), decision_date, float(base))
            if "forward_return_1d" not in got:
                waiting += 1
                continue
            await self._store.score(
                row["id"],
                forward_return_1d=got["forward_return_1d"],
                forward_return_5d=got.get("forward_return_5d"),
            )
            scored += 1

        if scored:
            logger.info(
                "Attribution scoring: %d row(s) scored, %d still waiting on a close",
                scored,
                waiting,
            )
        return {"examined": len(rows), "scored": scored, "waiting": waiting}

    async def backfill_long_horizon(
        self, limit: int = 500, now: datetime | None = None
    ) -> dict[str, int]:
        """Fill ``forward_return_5d`` on rows scored before that horizon existed."""
        now = now or datetime.now(UTC)
        rows = await self._store.pending_long_horizon(horizon_days=5, limit=limit, now=now)
        if not rows:
            return {"examined": 0, "filled": 0}

        candles = await self._candles_by_ticker([r.get("ticker") for r in rows], now)
        filled = 0
        for row in rows:
            decision_date = _as_date(row.get("timestamp"))
            base = row.get("price_at_decision")
            if decision_date is None or not base:
                continue
            got = returns_for(candles.get(row.get("ticker"), []), decision_date, float(base))
            if "forward_return_5d" not in got:
                continue
            await self._store.score(
                row["id"], forward_return_1d=None, forward_return_5d=got["forward_return_5d"]
            )
            filled += 1

        if filled:
            logger.info("Attribution scoring: filled the 5-day horizon on %d row(s)", filled)
        return {"examined": len(rows), "filled": filled}

    async def run(self, limit: int = 500, now: datetime | None = None) -> dict[str, int]:
        """Both passes. Safe to call every cycle; usually a no-op."""
        short = await self.score_pending(limit=limit, now=now)
        long = await self.backfill_long_horizon(limit=limit, now=now)
        return {**short, "backfilled_5d": long["filled"]}
