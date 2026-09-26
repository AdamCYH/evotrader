"""Live gap_pct must come from the first intraday bar, as the backtest's does.

See: data/evolution/reviews/20260918_195356_20260918_missed_mstr_rally_four_structural_defects.md
(findings 4 and 5)

Live ``gap_pct`` required the last DAILY bar to be dated today. The provider's
daily series ends at the prior session (the last bar is live-patched), so that
was never true intraday and ``gap_pct`` was None on every live cycle on record —
09-18 10:34 read ``gap_pct: null`` on a +3.4% open. The gap channel has
therefore NEVER been applicable live, while ``backtest/snapshot_builder.py``
computed it from the first 5-minute bar: every backtest ran an ensemble that
live never had.

The fix must land at weight 0.0: the channel FADES gaps over 1%, and on 09-18
it would have voted -0.98 at 09:33 against a gap that ran +16%. A blind channel
was hiding a wrong-direction design for this instrument.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evotrader.agents.tools import compute_gap_pct

_NOW = datetime(2026, 9, 18, 14, 34, tzinfo=UTC)  # 10:34 ET, market open
_PREV_CLOSE = 132.25  # MSTR 09-17 close
_QUOTE = {"last": 141.90, "previous_close": _PREV_CLOSE}


def _daily_ending_yesterday() -> list[dict]:
    """What the provider actually returns intraday: last bar is the PRIOR session."""
    return [
        {
            "timestamp": (_NOW - timedelta(days=d)).replace(hour=20, minute=0).isoformat(),
            "open": 130.0,
            "high": 134.0,
            "low": 128.0,
            "close": 132.25,
            "volume": 1e6,
        }
        for d in (3, 2, 1)
    ]


class TestGapComesFromTheFirstIntradayBar:
    def test_daily_series_ending_yesterday_plus_one_intraday_bar_yields_a_gap(self) -> None:
        """The exact live shape. Pre-fix this returned None."""
        intraday = [
            {
                "timestamp": _NOW.replace(hour=13, minute=30).isoformat(),
                "open": 136.75,
                "high": 137.5,
                "low": 136.0,
                "close": 137.0,
                "volume": 2e5,
            }
        ]
        gap, source = compute_gap_pct(intraday, _daily_ending_yesterday(), _QUOTE, _NOW)
        assert gap is not None
        assert source == "intraday_first_bar"
        assert gap == pytest.approx((136.75 - _PREV_CLOSE) / _PREV_CLOSE * 100, abs=1e-4)
        assert gap == pytest.approx(3.4026, abs=1e-3)  # the +3.4% open the review cites

    def test_it_matches_the_backtest_formula(self) -> None:
        """snapshot_builder: round(((first_open - prev_close) / prev_close) * 100, 4)."""
        first_open = 150.0
        intraday = [{"timestamp": _NOW.isoformat(), "open": first_open, "close": 151.0}]
        gap, _ = compute_gap_pct(intraday, [], {"previous_close": 140.0}, _NOW)
        assert gap == round(((first_open - 140.0) / 140.0) * 100.0, 4)

    def test_pre_fix_path_alone_returns_none_intraday(self) -> None:
        """No intraday bars + daily ending yesterday = the old behaviour: None.
        Pins the bug so the fixture above cannot pass for another reason."""
        gap, source = compute_gap_pct([], _daily_ending_yesterday(), _QUOTE, _NOW)
        assert gap is None and source is None


class TestTheDailyPathIsTheFallback:
    def test_a_today_dated_daily_bar_still_works_post_close(self) -> None:
        daily = _daily_ending_yesterday() + [
            {
                "timestamp": _NOW.replace(hour=13, minute=30).isoformat(),
                "open": 136.75,
                "high": 155.0,
                "low": 136.0,
                "close": 153.44,
                "volume": 5e6,
            }
        ]
        gap, source = compute_gap_pct([], daily, _QUOTE, _NOW)
        assert source == "daily_bar"
        assert gap == pytest.approx(3.4026, abs=1e-3)

    def test_intraday_wins_when_both_exist(self) -> None:
        intraday = [{"timestamp": _NOW.isoformat(), "open": 136.75}]
        daily = _daily_ending_yesterday() + [{"timestamp": _NOW.isoformat(), "open": 137.5}]
        gap, source = compute_gap_pct(intraday, daily, _QUOTE, _NOW)
        assert source == "intraday_first_bar"
        assert gap == pytest.approx(3.4026, abs=1e-3)

    def test_no_previous_close_yields_none(self) -> None:
        intraday = [{"timestamp": _NOW.isoformat(), "open": 136.75}]
        assert compute_gap_pct(intraday, [], {"last": 140.0}, _NOW) == (None, None)


class TestGapIsWeightedZeroEverywhere:
    """Finding 4's instruction: do NOT let a fade channel go live at 0.07/0.17
    on an instrument whose gaps run."""

    # v026's trending_bull momentum weight once gap's 0.07 had been spread over the
    # other channels in proportion; it was 0.19 before.
    _V026_TRENDING_BULL_MOMENTUM = 0.2045

    def test_gap_weight_is_zero_in_every_regime(self, active_algorithm_config) -> None:
        """In the algorithm version a new user starts with."""
        cfg = active_algorithm_config
        for regime, rw in cfg["composite"]["regime_weights"].items():
            assert rw["gap"] == 0.0, regime
            assert abs(sum(rw.values()) - 1.0) < 1e-6, (regime, sum(rw.values()))
        assert cfg["composite"]["gap_weight"] == 0.0

    def test_zeroing_gap_left_the_live_composite_unchanged(self) -> None:
        """Proportional redistribution must reproduce what actually traded.

        Two independent reasons it does: gap never sat in the voting pool, so
        every other channel's renormalised authority was already as if gap were
        0; and CompositeStrategy.__init__ normalises the weight vector to sum to
        1, so a uniform rescale is a no-op before compute even runs. Checked
        against the 09-17 live fixture that reproduces the engine to 4 decimals.
        """
        import types

        from evotrader.algorithms.composite import CompositeStrategy
        from evotrader.models.market import MarketRegime
        from tests.unit.test_composite_live_fixture import _CYCLES, _W, _Stub

        k = self._V026_TRENDING_BULL_MOMENTUM / 0.19
        assert k > 1.05, "the redistribution factor should be ~1/(1-0.07)"
        snap = types.SimpleNamespace(
            regime=types.SimpleNamespace(regime=MarketRegime.TRENDING_BULL)
        )

        for label, values, expected in _CYCLES:
            comp = CompositeStrategy(
                sub_strategies={n: _Stub(n, v) for n, v in values.items()},
                weights={n: _W[n] * k for n in values},  # the SAME rescale the config used
                regime_adaptive=False,
                version="fixture",
            )
            got = comp.compute_detailed_signal(snap).composite_value
            assert got == pytest.approx(expected, abs=5e-5), label
