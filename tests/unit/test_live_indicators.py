"""Regression tests: intraday indicator staleness and VWAP session anchor.

Guards against the bug where all indicator values (RSI, MACD, VWAP, IBS,
Bollinger) were frozen for the entire trading day because they were computed
from completed daily candles and never refreshed intraday.

See: data/evolution/reviews/20260715_210358_intraday_indicator_staleness_and_vwap_session_anchor.md
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from evotrader.agents.tools import compute_indicators
from evotrader.algorithms.composite import _ENGINE_SOURCES
from evotrader.indicators import daily_series
from evotrader.indicators.daily_series import session_daily_bar
from evotrader.indicators.ibs import compute_live_ibs
from evotrader.indicators.vwap import compute_session_vwap

# ── today's daily bar ──────────────────────────────────────────────────
#
# The broker's daily list ends at the PREVIOUS session all day long. Until
# 2026-10-02 the live price was written over that last bar, so yesterday's
# close, high and low were missing from every daily indicator. Today's bar is
# now appended instead. Made-up bars; 2026-03-04 is a Wednesday, before
# daylight saving time (15:00 UTC is 10:00 ET).

DURING = datetime(2026, 3, 4, 15, 0, tzinfo=UTC)  # 10:00 ET
AFTER_CLOSE = datetime(2026, 3, 4, 22, 0, tzinfo=UTC)  # 17:00 ET
SATURDAY = datetime(2026, 3, 7, 15, 0, tzinfo=UTC)


def _daily(n: int = 60, last_day: str = "2026-03-03") -> list[dict]:
    """``n`` daily bars stamped as the broker stamps them, ending ``last_day``."""
    end = datetime.fromisoformat(last_day)
    bars = []
    for k in range(n):
        day = end - timedelta(days=n - 1 - k)
        price = 100 + 5 * math.sin(k * 0.3) + 0.1 * k
        bars.append(
            {
                "timestamp": day.strftime("%Y-%m-%dT00:00:00Z"),
                "open": price - 0.5,
                "high": price + 2,
                "low": price - 2,
                "close": price,
                "volume": 10_000 + 100 * k,
            }
        )
    return bars


SESSION = [
    {"open": 101.0, "high": 104.0, "low": 100.0, "close": 103.0, "volume": 500.0},
    {"open": 103.0, "high": 106.0, "low": 102.0, "close": 105.0, "volume": 700.0},
]


def _overwrite_last_bar(candles: list[dict], live: float) -> None:
    """What the live path did before 2026-10-02, kept here as the before."""
    last = candles[-1]
    last["close"] = live
    last["high"] = max(last["high"], live)
    last["low"] = min(last["low"], live)


class TestTodaysDailyBar:
    def test_is_appended_and_yesterday_is_kept(self) -> None:
        candles = _daily()
        yesterday = dict(candles[-1])

        mode = session_daily_bar(candles, 103.0, DURING)

        assert mode == "forming_bar_appended"
        assert candles[-2] == yesterday, "yesterday's close, high and low are untouched"
        assert candles[-1]["timestamp"] == "2026-03-04T00:00:00+00:00"
        assert [candles[-1][k] for k in ("open", "high", "low", "close", "volume")] == [
            103.0,
            103.0,
            103.0,
            103.0,
            0.0,
        ]
        assert candles[-1]["forming"] == "quote"

    def test_is_built_from_the_sessions_bars(self) -> None:
        candles = _daily()

        session_daily_bar(candles, 104.5, DURING, SESSION)

        today = candles[-1]
        assert (today["open"], today["high"], today["low"], today["close"]) == (
            101.0,
            106.0,
            100.0,
            104.5,
        )
        assert today["volume"] == 1200.0
        assert today["forming"] == "session_bars"

    def test_after_the_close_takes_the_sessions_close_not_an_after_hours_print(self) -> None:
        candles = _daily()

        session_daily_bar(candles, 107.0, AFTER_CLOSE, SESSION)

        today = candles[-1]
        assert (today["high"], today["low"], today["close"]) == (106.0, 100.0, 105.0)

    def test_a_bar_already_dated_today_is_updated(self) -> None:
        candles = _daily(last_day="2026-03-04")

        mode = session_daily_bar(candles, 999.0, DURING)

        assert mode == "patched_today"
        assert len(candles) == 60
        assert candles[-1]["close"] == candles[-1]["high"] == 999.0

    def test_there_is_none_on_a_weekend(self) -> None:
        candles = _daily(last_day="2026-03-06")
        before = [dict(c) for c in candles]

        assert session_daily_bar(candles, 103.0, SATURDAY) == "no_session_today"
        assert candles == before

    def test_nothing_without_a_price(self) -> None:
        candles = _daily()
        assert session_daily_bar(candles, 0.0, DURING) == "unchanged"
        assert session_daily_bar([], 103.0, DURING) == "unchanged"

    def test_is_part_of_the_engine_fingerprint(self) -> None:
        """How the series is built changes every daily reading, so a change to
        it must move the engine fingerprint and raise the engine-change notice."""
        module = Path(daily_series.__file__).resolve()
        path = module.relative_to(module.parents[1]).as_posix()
        assert any(path == s or path.startswith(f"{s}/") for s in _ENGINE_SOURCES)


class TestIndicatorsSeeYesterdayAndToday:
    def test_atr_moves_with_todays_range(self) -> None:
        """The 2026-10-01 proof in miniature: with yesterday overwritten, prints
        inside yesterday's range never moved ATR; with today appended they do."""
        old = []
        new = []
        for live in (99.0, 109.0):  # both inside yesterday's 95-110 range
            overwritten = _daily()
            overwritten[-1].update(high=110.0, low=95.0)
            _overwrite_last_bar(overwritten, live)
            old.append(compute_indicators(json.dumps(overwritten))["atr_14"])

            appended = _daily()
            appended[-1].update(high=110.0, low=95.0)
            session_daily_bar(appended, live, DURING, [dict(SESSION[0], close=live)])
            new.append(compute_indicators(json.dumps(appended), forming_last_bar=True)["atr_14"])
        assert old[0] == old[1], "the defect: a frozen ATR"
        assert new[0] != new[1]

    def test_todays_price_still_moves_the_daily_indicators(self) -> None:
        readings = []
        for live in (95.0, 112.0):
            candles = _daily()
            session_daily_bar(candles, live, DURING)
            readings.append(compute_indicators(json.dumps(candles), forming_last_bar=True))
        assert readings[0]["macd_histogram"] != readings[1]["macd_histogram"]
        assert readings[0]["ema_9"] != readings[1]["ema_9"]

    def test_relative_volume_is_the_last_completed_sessions(self) -> None:
        completed = compute_indicators(json.dumps(_daily()))
        candles = _daily()
        session_daily_bar(candles, 103.0, DURING)  # volume 0 so far today

        forming = compute_indicators(json.dumps(candles), forming_last_bar=True)

        assert forming["relative_volume"] == completed["relative_volume"]
        assert forming["relative_volume_source"] == "prior_session_daily"
        assert forming["vwap"] == completed["vwap"]

    def test_a_bar_with_no_range_yet_has_no_ibs(self) -> None:
        candles = _daily()
        session_daily_bar(candles, 103.0, DURING)

        result = compute_indicators(json.dumps(candles), forming_last_bar=True)

        assert result["ibs"] is None
        assert result["ibs_source"] == "forming_bar_no_range"


# ── compute_session_vwap ───────────────────────────────────────────────


class TestComputeSessionVwap:
    """Verify session-aware VWAP computation from intraday bars."""

    def test_basic_session_vwap(self):
        """VWAP from simple intraday bars should equal the typical-price-weighted mean."""
        high = pd.Series([102.0, 104.0, 103.0])
        low = pd.Series([98.0, 100.0, 99.0])
        close = pd.Series([100.0, 102.0, 101.0])
        volume = pd.Series([1000.0, 2000.0, 1500.0])

        vwap_val, anchor = compute_session_vwap(high, low, close, volume)

        assert anchor == "current_session"
        assert vwap_val is not None
        # Verify manually: TP = (H+L+C)/3, cum(TP*V)/cum(V)
        tp = (high + low + close) / 3.0
        expected = (tp * volume).sum() / volume.sum()
        assert abs(vwap_val - expected) < 0.001

    def test_empty_series_returns_fallback(self):
        """Empty intraday data should return prior_session fallback."""
        vwap_val, anchor = compute_session_vwap(
            pd.Series([], dtype=float),
            pd.Series([], dtype=float),
            pd.Series([], dtype=float),
            pd.Series([], dtype=float),
        )
        assert anchor == "prior_session"
        assert vwap_val is None

    def test_none_series_returns_fallback(self):
        vwap_val, anchor = compute_session_vwap(
            pd.Series([], dtype=float),
            pd.Series([], dtype=float),
            None,
            pd.Series([], dtype=float),
        )
        assert anchor == "prior_session"
        assert vwap_val is None

    def test_session_start_filter(self):
        """Only bars at or after session_start should be included."""
        timestamps = pd.to_datetime(
            [
                "2026-07-15 13:00:00+00:00",  # Before 09:30 ET (13:30 UTC)
                "2026-07-15 14:00:00+00:00",  # After session start
                "2026-07-15 15:00:00+00:00",
            ]
        )
        high = pd.Series([100.0, 104.0, 106.0], index=timestamps)
        low = pd.Series([98.0, 100.0, 102.0], index=timestamps)
        close = pd.Series([99.0, 103.0, 105.0], index=timestamps)
        volume = pd.Series([500.0, 1000.0, 1500.0], index=timestamps)

        session_start = pd.Timestamp("2026-07-15 13:30:00+00:00")
        vwap_val, anchor = compute_session_vwap(
            high,
            low,
            close,
            volume,
            session_start=session_start,
        )

        assert anchor == "current_session"
        assert vwap_val is not None
        # Only the last 2 bars should be used
        filtered_high = high.iloc[1:]
        filtered_low = low.iloc[1:]
        filtered_close = close.iloc[1:]
        filtered_volume = volume.iloc[1:]
        tp = (filtered_high + filtered_low + filtered_close) / 3.0
        expected = (tp * filtered_volume).sum() / filtered_volume.sum()
        assert abs(vwap_val - expected) < 0.001


# ── compute_live_ibs ───────────────────────────────────────────────────


class TestComputeLiveIbs:
    """Verify live IBS from session running high/low."""

    def test_price_at_high(self):
        assert compute_live_ibs(110.0, 100.0, 110.0) == 1.0

    def test_price_at_low(self):
        assert compute_live_ibs(110.0, 100.0, 100.0) == 0.0

    def test_price_at_midpoint(self):
        assert compute_live_ibs(110.0, 100.0, 105.0) == 0.5

    def test_zero_range_returns_none(self):
        assert compute_live_ibs(100.0, 100.0, 100.0) is None

    def test_normal_value(self):
        ibs = compute_live_ibs(724.33, 712.14, 720.0)
        assert ibs is not None
        assert 0 < ibs < 1
        expected = (720.0 - 712.14) / (724.33 - 712.14)
        assert abs(ibs - expected) < 0.0001
