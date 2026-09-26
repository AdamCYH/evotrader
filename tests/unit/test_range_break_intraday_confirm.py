"""A one-day event needs a same-day confirmation.

See: data/evolution/reviews/20260918_195356_20260918_missed_mstr_rally_four_structural_defects.md
(finding 3)

The confirmation gate read the DAILY MACD histogram against a 0.75 floor in raw
dollars. A daily 12/26 EMA differential versus its 9-day signal barely moves on
a one-day jump, so it lags a breakout by construction — and a Bollinger band
break is by definition a one-day event. The two gates were near-mutually
exclusive: this channel was 0-for-18 because price sat INSIDE the bands, and on
the first day the band gate was reachable the MACD gate blocked it.

Live 2026-09-18 11:36 (event 18285): close 148.345 > upper band 148.2173,
day_chg +12.17%, macd_histogram -0.250 -> no fire. Same at 12:34 and every
cycle after. Reconstruction with a VWAP/IBS gate: penetration 0.1277/9.4868 =
0.0135 ATR, signal 0.45 + 0.0135*0.35 = +0.455, flipping the day's composite
from -0.034 to a with-trend +0.049 a cash account can act on.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evotrader.algorithms.strategies.range_break_continuation import (
    RangeBreakContinuationStrategy,
)
from evotrader.models.market import (
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)

_NOW = datetime(2026, 9, 18, 15, 36, tzinfo=UTC)  # 11:36 ET


def _snapshot(
    *,
    close: float,
    upper: float,
    lower: float,
    vwap: float | None,
    ibs: float | None,
    macd_hist: float,
    day_chg: float,
    anchor: str = "current_session",
    atr: float = 9.4868,
) -> MarketSnapshot:
    return MarketSnapshot(
        ticker="MSTR",
        timestamp=_NOW,
        quote=Quote(
            ticker="MSTR",
            bid=close - 0.02,
            ask=close + 0.02,
            last=close,
            volume=8e6,
            timestamp=_NOW,
            previous_close=132.25,
        ),
        indicators=TechnicalIndicators(
            bollinger_upper=upper,
            bollinger_lower=lower,
            bollinger_middle=(upper + lower) / 2,
            atr_14=atr,
            macd_histogram=macd_hist,
            vwap=vwap,
            vwap_anchor=anchor if vwap is not None else None,
            ibs=ibs,
        ),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BULL, confidence=0.7, reasoning="t"
        ),
        daily_change_pct=day_chg,
    )


# The 11:36 payload, verbatim from the review.
_LIVE = dict(
    close=148.345,
    upper=148.2173,
    lower=115.0,
    vwap=144.48,
    ibs=0.953,
    macd_hist=-0.250,
    day_chg=12.17,
)


def _live_strategy(**kw) -> RangeBreakContinuationStrategy:
    """The v026 config values, with the new confirmation."""
    kw.setdefault("min_break_pct", 0.7)
    kw.setdefault("base_strength", 0.45)
    kw.setdefault("penetration_scale", 0.35)
    return RangeBreakContinuationStrategy(**kw)


class TestTheLiveBreakoutNowFires:
    def test_11_36_payload_fires_at_plus_0_455(self) -> None:
        """THE REGRESSION CASE. Pre-fix: 0.0, blocked by the daily MACD gate."""
        sig = _live_strategy().compute_signal(_snapshot(**_LIVE))
        assert sig.value == pytest.approx(0.455, abs=0.002), sig.metadata
        assert sig.metadata["break_side"] == "upper"
        assert sig.metadata["confirm_source"] == "vwap+ibs"
        assert sig.metadata["penetration_atr"] == pytest.approx(0.0135, abs=1e-3)

    def test_the_old_dollar_macd_gate_would_have_blocked_it(self) -> None:
        """Pin the bug: the same payload with the legacy gate reads 0.0."""
        strat = _live_strategy(vwap_confirm=False, ibs_confirm=0.0, macd_confirm_floor=0.75)
        sig = strat.compute_signal(_snapshot(**_LIVE))
        assert sig.value == 0.0
        assert sig.metadata["confirm_source"] == "macd_not_confirming"


class TestTheIntradayGate:
    def test_wrong_side_of_vwap_does_not_confirm(self) -> None:
        """Above the band but BELOW session VWAP is a spike, not a break."""
        sig = _live_strategy().compute_signal(_snapshot(**{**_LIVE, "vwap": 149.0}))
        assert sig.value == 0.0
        assert sig.metadata["confirm_source"] == "wrong_side_of_vwap"

    def test_ibs_off_the_high_does_not_confirm(self) -> None:
        """Closing mid-bar after poking above the band is a rejection, not a break."""
        sig = _live_strategy().compute_signal(_snapshot(**{**_LIVE, "ibs": 0.40}))
        assert sig.value == 0.0
        assert sig.metadata["confirm_source"] == "ibs_not_at_extreme"

    def test_a_stale_vwap_anchor_is_a_plumbing_fault_not_a_confirmation(self) -> None:
        sig = _live_strategy().compute_signal(_snapshot(**_LIVE, anchor="prior_session"))
        assert sig.value == 0.0
        assert sig.metadata["confirm_source"] == "vwap_unavailable"

    def test_the_magnitude_gate_still_applies(self) -> None:
        """A +0.3% day that pokes the band is not a breakout."""
        sig = _live_strategy().compute_signal(_snapshot(**{**_LIVE, "day_chg": 0.3}))
        assert sig.value == 0.0


class TestDownsideIsTheMirror:
    def test_lower_band_break_confirms_below_vwap_with_low_ibs(self) -> None:
        sig = _live_strategy().compute_signal(
            _snapshot(
                close=114.8,
                upper=148.0,
                lower=115.0,
                vwap=118.0,
                ibs=0.05,
                macd_hist=+0.2,
                day_chg=-12.0,
            )
        )
        assert sig.value < 0
        assert sig.metadata["break_side"] == "lower"
        assert sig.metadata["confirm_source"] == "vwap+ibs"

    def test_lower_break_above_vwap_does_not_confirm(self) -> None:
        sig = _live_strategy().compute_signal(
            _snapshot(
                close=114.8,
                upper=148.0,
                lower=115.0,
                vwap=113.0,
                ibs=0.05,
                macd_hist=+0.2,
                day_chg=-12.0,
            )
        )
        assert sig.value == 0.0


class TestMacdIsOptionalAndUnitFree:
    def test_macd_floor_is_compared_in_atr_units(self) -> None:
        """hist/atr = -0.25/9.49 = -0.026 ATR. A 0.02-ATR floor rejects the
        live payload; the old 0.75 was $0.75, which is meaningless across
        instruments."""
        strat = _live_strategy(macd_confirm_floor=0.02)
        sig = strat.compute_signal(_snapshot(**_LIVE))
        assert sig.value == 0.0
        assert sig.metadata["confirm_source"] == "macd_not_confirming"

        strat_ok = _live_strategy(macd_confirm_floor=0.02)
        sig_ok = strat_ok.compute_signal(_snapshot(**{**_LIVE, "macd_hist": +0.5}))
        assert sig_ok.value > 0
        assert sig_ok.metadata["confirm_source"] == "vwap+ibs+macd_atr"

    def test_the_shipped_config_turned_the_dollar_gate_off(self, active_algorithm_config) -> None:
        """In the algorithm version a new user starts with."""
        rb = active_algorithm_config["range_break_continuation"]
        assert rb["macd_confirm_floor"] == 0.0, "0.75 now means 0.75 ATR and would never fire"
        assert rb["vwap_confirm"] is True and rb["ibs_confirm"] == 0.70
