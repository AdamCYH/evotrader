"""vwap_reclaim_continuation must measure freshness on the clock, not in bars.

See: data/evolution/reviews/20260918_195356_20260918_missed_mstr_rally_four_structural_defects.md
(finding 2)

The bars are 5-minute candles but the system samples once an HOUR.
``max_bars_since_reclaim=6`` was a 30-minute window and ``reclaim_decay=0.80``
per bar was 0.33 at its edge, so the channel could only see a reclaim that
landed in the half-hour before a cycle.

Live 2026-09-18 10:34 ET (event 18239): bull_stack true, dip_bars 3, depth_atr
0.417 (inside the 0.10-0.90 band), reclaimed_now true, reclaim 45 minutes old
-> 0.0, reason=reclaim_too_old. Price never re-crossed VWAP all day, so every
later cycle read the same. The one with-trend intraday channel saw the day's
only pullback and threw it away on a clock mismatch.

Reconstruction with the fix: depth_term tanh(0.417/0.35)=0.8306, freshness
0.80**(45/30)=0.7155, vol_term 0.6797 -> 0.75*0.8306*0.7155*0.6797 = +0.303.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evotrader.algorithms.strategies.vwap_reclaim_continuation import (
    VwapReclaimContinuationStrategy,
)
from evotrader.models.market import (
    OHLCV,
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)

_ATR = 9.4868
_VWAP = 140.0
_NOW = datetime(2026, 9, 18, 14, 34, tzinfo=UTC)  # 10:34 ET


def _snapshot(
    closes: list[float], *, reclaim_age_min: float, rvol: float = 0.6797, price: float | None = None
) -> MarketSnapshot:
    """Bars end so that the FIRST bar back above VWAP is `reclaim_age_min` old.

    `closes` is oldest-first; the dip episode is whatever sits below VWAP.
    """
    # Locate the reclaim bar (first close above VWAP after the last dip).
    last_dip = max(i for i, c in enumerate(closes) if c < _VWAP)
    reclaim_idx = last_dip + 1
    reclaim_ts = _NOW - timedelta(minutes=reclaim_age_min)
    candles = [
        OHLCV(
            timestamp=reclaim_ts + timedelta(minutes=5 * (i - reclaim_idx)),
            open=c,
            high=c + 0.3,
            low=c - 0.3,
            close=c,
            volume=40_000.0,
        )
        for i, c in enumerate(closes)
    ]
    px = price if price is not None else closes[-1]
    return MarketSnapshot(
        ticker="MSTR",
        timestamp=_NOW,
        quote=Quote(
            ticker="MSTR", bid=px - 0.02, ask=px + 0.02, last=px, volume=5e6, timestamp=_NOW
        ),
        indicators=TechnicalIndicators(
            vwap=_VWAP,
            vwap_anchor="current_session",
            atr_14=_ATR,
            relative_volume=rvol,
            ema_9=px - 3,
            ema_21=px - 6,
            sma_20=125.0,
            sma_50=110.0,  # bull stack
        ),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BULL, confidence=0.7, reasoning="t"
        ),
        recent_candles=candles,
    )


# The 10:34 shape: three dip bars, deepest 0.417 ATR below VWAP, then reclaimed.
_DIP_EXTREME = _VWAP - 0.417 * _ATR
_LIVE_CLOSES = [
    141.0,
    140.8,
    139.2,
    _DIP_EXTREME,
    139.5,
    140.6,
    141.1,
    141.4,
    141.6,
    141.8,
    142.0,
    142.1,
]


class TestTheLiveMissNowFires:
    def test_10_34_payload_fires_at_plus_0_303(self) -> None:
        """THE REGRESSION CASE. Pre-fix: 0.0 / reclaim_too_old."""
        strat = VwapReclaimContinuationStrategy()  # defaults = the live v026/v027 values
        sig = strat.compute_signal(_snapshot(_LIVE_CLOSES, reclaim_age_min=45.0))

        assert sig.metadata["reason"] == "reclaim_confirmed", sig.metadata
        assert sig.metadata["age_source"] == "candle_timestamps"
        assert sig.metadata["age_min"] == pytest.approx(45.0, abs=0.5)
        assert sig.metadata["depth_atr"] == pytest.approx(0.417, abs=1e-3)
        assert sig.metadata["dip_bars"] == 3
        assert sig.metadata["freshness"] == pytest.approx(0.80**1.5, abs=1e-3)
        assert sig.value == pytest.approx(0.303, abs=0.005)
        assert sig.value > 0, "a bought dip in a bull stack votes LONG"

    def test_the_pre_fix_window_would_have_rejected_it(self) -> None:
        """The same setup under the OLD 30-minute window must NOT fire — this
        pins what the bug was, so the fixture cannot pass for another reason."""
        strat = VwapReclaimContinuationStrategy(max_minutes_since_reclaim=30.0)
        sig = strat.compute_signal(_snapshot(_LIVE_CLOSES, reclaim_age_min=45.0))
        assert sig.value == 0.0
        assert sig.metadata["reason"] == "reclaim_too_old"


class TestFreshnessIsWallClock:
    def test_age_is_read_from_timestamps_not_bar_count(self) -> None:
        """Same bar count, different clock: only the clock should matter."""
        strat = VwapReclaimContinuationStrategy()
        fresh = strat.compute_signal(_snapshot(_LIVE_CLOSES, reclaim_age_min=10.0))
        stale = strat.compute_signal(_snapshot(_LIVE_CLOSES, reclaim_age_min=80.0))
        assert fresh.metadata["bars_since_reclaim"] == stale.metadata["bars_since_reclaim"]
        assert fresh.value > stale.value > 0

    def test_decay_is_per_30_minutes(self) -> None:
        strat = VwapReclaimContinuationStrategy(reclaim_decay=0.80)
        s60 = strat.compute_signal(_snapshot(_LIVE_CLOSES, reclaim_age_min=60.0))
        assert s60.metadata["freshness"] == pytest.approx(0.80**2, abs=1e-3)

    def test_older_than_the_window_is_too_old(self) -> None:
        strat = VwapReclaimContinuationStrategy(max_minutes_since_reclaim=90.0)
        sig = strat.compute_signal(_snapshot(_LIVE_CLOSES, reclaim_age_min=95.0))
        assert sig.metadata["reason"] == "reclaim_too_old"

    def test_the_hourly_cadence_fits_inside_the_window(self) -> None:
        """A reclaim right after the previous cycle must still be visible."""
        strat = VwapReclaimContinuationStrategy()
        sig = strat.compute_signal(_snapshot(_LIVE_CLOSES, reclaim_age_min=59.0))
        assert sig.value > 0

    def test_falls_back_to_bars_when_timestamps_are_missing(self) -> None:
        snap = _snapshot(_LIVE_CLOSES, reclaim_age_min=45.0)
        bare = [c.model_copy(update={"timestamp": None}) for c in snap.recent_candles]
        snap = snap.model_copy(update={"recent_candles": bare})
        sig = VwapReclaimContinuationStrategy().compute_signal(snap)
        assert sig.metadata["age_source"] == "bars_assumed_5min"
        # 8 bars since reclaim x 5 min = 40 min
        assert sig.metadata["age_min"] == pytest.approx(sig.metadata["bars_since_reclaim"] * 5.0)


class TestParameterCompatibility:
    def test_legacy_bars_key_is_converted_not_ignored(self) -> None:
        """v027's first draft used max_bars_since_reclaim; must not silently
        fall back to the default."""
        strat = VwapReclaimContinuationStrategy()
        strat.set_parameters({"max_bars_since_reclaim": 14})
        assert strat.get_parameters()["max_minutes_since_reclaim"] == 70.0

    def test_new_key_wins_over_legacy(self) -> None:
        strat = VwapReclaimContinuationStrategy()
        strat.set_parameters({"max_bars_since_reclaim": 14, "max_minutes_since_reclaim": 90})
        assert strat.get_parameters()["max_minutes_since_reclaim"] == 90.0

    def test_the_shipped_config_carries_the_wall_clock_values(
        self, active_algorithm_config
    ) -> None:
        """In the algorithm version a new user starts with."""
        cfg = active_algorithm_config
        blk = cfg["vwap_reclaim_continuation"]
        assert blk["max_minutes_since_reclaim"] == 90.0
        assert blk["reclaim_decay"] == 0.80
        assert blk["lookback_bars"] == 30
        assert "max_bars_since_reclaim" not in blk
        for regime, rw in cfg["composite"]["regime_weights"].items():
            assert rw["vwap_reclaim_continuation"] == 0.0, (regime, "still shadow")
