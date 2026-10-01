"""Thresholds on a price move, in the instrument's own units.

A percent means different things on different instruments. 0.7% is about two
thirds of a normal day on a broad index fund and about a tenth of one on a
volatile single stock, so a threshold written in percent is silently tuned to
whichever instrument it was chosen on, and it breaks when the instrument
changes: on the volatile stock the "big move" gate fires on ordinary noise.
Expressed as a multiple of the daily ATR (average true range, the typical size
of one day's move) the same number means the same thing on any ticker.

Every strategy that gates on the size of a move uses the one rule here, so they
cannot drift into several versions of it:

* when an ATR threshold is configured and the ATR can be measured, compare in
  ATR;
* otherwise fall back to the strategy's existing threshold (percent, or a
  fraction of price), exactly as before.

The ATR threshold defaults to ``None`` everywhere, so a configuration that does
not set it behaves bit-for-bit as it did. ``momentum.divergence_day_change_atr``
(2026-09-23) is the same pattern and predates this module.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

#: Upper bound accepted for any ATR-unit threshold. Five normal days in one
#: move is already an extreme event; a larger value is almost certainly a
#: percent or a price typed into the wrong field.
MAX_THRESHOLD_ATR = 5.0


def move_in_atr(price_move: float | None, atr: float | None) -> float | None:
    """An absolute price distance as a multiple of the daily ATR, or None."""
    if price_move is None or atr is None or atr <= 0:
        return None
    return abs(price_move) / atr


def pct_move_in_atr(
    move_pct: float | None, reference_price: float | None, atr: float | None
) -> float | None:
    """A percent move measured from *reference_price*, as a multiple of ATR.

    ``move_pct`` is in percent (``1.2`` = 1.2%), as ``daily_change_pct`` and
    ``gap_pct`` are. Returns None when any input is missing or non-positive.
    """
    if move_pct is None or reference_price is None or reference_price <= 0:
        return None
    return move_in_atr(move_pct / 100.0 * reference_price, atr)


def previous_close(
    last: float | None, day_change_pct: float | None, quoted_previous_close: float | None
) -> float | None:
    """The price a day's percent change is measured from.

    The quote's own previous close when it has one; otherwise derived from the
    last price and the day's change, which is exact because the change was
    computed from that same previous close.
    """
    if quoted_previous_close is not None and quoted_previous_close > 0:
        return quoted_previous_close
    if last is None or last <= 0 or day_change_pct is None:
        return None
    denom = 1.0 + day_change_pct / 100.0
    return last / denom if denom > 0 else None


def session_date_et(ts: datetime) -> date:
    """The US Eastern calendar date of a moment: the trading day a cycle belongs to.

    A naive datetime is read as Eastern time, the convention the rest of the
    code uses for market clocks (``tools.market_hours``).
    """
    from evotrader.tools.market_hours import ET

    return (ts.replace(tzinfo=ET) if ts.tzinfo is None else ts.astimezone(ET)).date()


def _bar_session_date(bar: Any) -> date | None:
    """The trading day a daily bar describes.

    Read from the bar's own timestamp without converting time zones: the broker
    stamps a daily bar at midnight UTC of its session date, so converting that
    to US Eastern would move every bar back one day.
    """
    ts = bar.get("timestamp") if isinstance(bar, dict) else getattr(bar, "timestamp", None)
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(ts, datetime):
        return ts.date()
    return ts if isinstance(ts, date) else None


def _bar_close(bar: Any) -> float | None:
    v = bar.get("close") if isinstance(bar, dict) else getattr(bar, "close", None)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def move_over_sessions_in_atr(
    daily_candles: Sequence[Any] | None,
    sessions: int,
    price: float | None,
    atr: float | None,
    today: date,
) -> tuple[float, date] | None:
    """The move from the close ``sessions`` trading days before ``today``, in ATRs.

    Returns ``(signed move in daily ATRs, the anchor session's date)``, or None
    when it cannot be measured honestly (too little history, no price or ATR).

    The anchor is found BY DATE, not by counting bars from the end of the list.
    Whether the list ends with yesterday's completed bar or with a bar for today
    depends on the data source and on the time of day, so "the bar N places from
    the end" is a different day in different cycles. A date cannot drift that
    way. Bars dated ``today`` or later are ignored; the current price stands for
    today.
    """
    if not daily_candles or sessions < 1 or not price or price <= 0 or not atr or atr <= 0:
        return None
    by_date: dict[date, float] = {}
    for bar in daily_candles:
        d, c = _bar_session_date(bar), _bar_close(bar)
        if d is not None and c is not None and c > 0 and d < today:
            by_date[d] = c
    prior = sorted(by_date, reverse=True)
    if len(prior) < sessions:
        return None
    anchor = prior[sessions - 1]
    return (price - by_date[anchor]) / atr, anchor


@dataclass(frozen=True)
class MoveCheck:
    """The outcome of one threshold test, with what it was measured in.

    ``basis`` is ``"atr"`` when the ATR threshold decided, ``"fallback"`` when
    no ATR threshold is configured, and ``"fallback_no_atr"`` when one is
    configured but the ATR could not be measured this cycle. The last is worth
    telling apart: it means the strategy quietly reverted to the
    instrument-specific rule.

    ``ratio`` is the observed move divided by the threshold, in the basis that
    decided. Strategies that scale their output by "how far past the
    threshold" use it, so the scaling follows the same basis as the gate.
    """

    met: bool
    basis: str
    ratio: float
    observed_atr: float | None


def check_move(
    *,
    fallback_move: float,
    fallback_threshold: float,
    move_atr: float | None,
    threshold_atr: float | None,
) -> MoveCheck:
    """Test the size of a move against its threshold, preferring ATR units.

    ``fallback_move`` and ``fallback_threshold`` are in the strategy's legacy
    unit (percent, or a fraction of price) and are compared exactly as the
    strategy always compared them, so the fallback path is unchanged. Sign is
    the caller's business: this compares magnitudes.
    """
    if threshold_atr is not None and threshold_atr > 0 and move_atr is not None:
        return MoveCheck(
            met=move_atr >= threshold_atr,
            basis="atr",
            ratio=move_atr / threshold_atr,
            observed_atr=move_atr,
        )
    basis = "fallback" if threshold_atr is None else "fallback_no_atr"
    magnitude = abs(fallback_move)
    ratio = magnitude / fallback_threshold if fallback_threshold > 0 else 0.0
    return MoveCheck(
        met=magnitude >= fallback_threshold,
        basis=basis,
        ratio=ratio,
        observed_atr=move_atr,
    )


def validate_threshold_atr(name: str, value: object) -> list[str]:
    """Errors for an ATR-unit threshold parameter. ``None`` means off."""
    if value is None:
        return []
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return [f"{name} must be a number of daily ATRs or null, got {value!r}"]
    if v <= 0 or v > MAX_THRESHOLD_ATR:
        return [
            f"{name} must be in (0, {MAX_THRESHOLD_ATR:g}] daily ATRs "
            f"(e.g. 0.65 = two thirds of a normal day), or null to use the "
            f"percent fallback; got {v}"
        ]
    return []
