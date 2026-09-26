"""Tests for backtest statistical validation.

Each test encodes a false positive found during strategy research, so a
regression here means the harness has stopped catching a mistake it once caught.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from evotrader.backtest.validation import (
    ValidationReport,
    circular_shift_indices,
    clustered_tstat,
    compare_to_benchmark,
    concentration,
    minimum_detectable_effect,
    multiple_testing_penalty,
    out_of_sample_split,
    period_stability,
    placebo_test,
    survivorship_warning,
    validate,
)


def _dates(n: int, start: str = "2015-01-01", step: int = 1) -> list[datetime]:
    d0 = pd.Timestamp(start)
    return [(d0 + timedelta(days=i * step)).to_pydatetime() for i in range(n)]


class TestClusteredTstat:
    def test_same_day_trades_collapse_to_one_event(self) -> None:
        """30 stocks falling on one market day is one observation, not 30."""
        rng = np.random.default_rng(0)
        pnl, dates = [], []
        for day in range(40):
            shared = rng.normal(0.01, 0.005)  # one market event
            for _ in range(30):  # 30 near-identical trades
                pnl.append(shared + rng.normal(0, 0.0005))
                dates.append(pd.Timestamp("2015-01-01") + timedelta(days=day))
        res = clustered_tstat(pnl, dates)
        naive = np.mean(pnl) / (np.std(pnl, ddof=1) / np.sqrt(len(pnl)))
        assert res.value is not None
        assert res.value < naive / 3, "clustering must substantially deflate the t-stat"
        assert "distinct days" in res.detail

    def test_genuinely_independent_trades_are_not_penalised(self) -> None:
        rng = np.random.default_rng(1)
        n = 300
        pnl = rng.normal(0.01, 0.02, n).tolist()
        res = clustered_tstat(pnl, _dates(n))
        assert res.passed
        assert res.value is not None and res.value > 2.0

    def test_empty_input_fails_cleanly(self) -> None:
        assert not clustered_tstat([], []).passed


class TestOutOfSample:
    def test_decayed_edge_is_rejected(self) -> None:
        """The pairs-trading signature: strong first half, nothing after."""
        rng = np.random.default_rng(2)
        early = rng.normal(0.02, 0.02, 150)
        late = rng.normal(0.0, 0.02, 150)
        pnl = np.concatenate([early, late]).tolist()
        res = out_of_sample_split(pnl, _dates(300, "2015-01-01"), split="2015-06-01")
        assert not res.passed
        assert "decayed" in res.detail

    def test_stable_edge_passes(self) -> None:
        rng = np.random.default_rng(3)
        pnl = rng.normal(0.02, 0.015, 400).tolist()
        res = out_of_sample_split(pnl, _dates(400), split="2015-07-01")
        assert res.passed

    def test_insufficient_data_fails(self) -> None:
        res = out_of_sample_split([0.01] * 10, _dates(10), split="2015-01-05")
        assert not res.passed
        assert "insufficient" in res.detail


class TestMultipleTesting:
    def test_bar_rises_with_number_of_variants(self) -> None:
        one = multiple_testing_penalty(1, 2.5)
        many = multiple_testing_penalty(153, 2.5)
        assert one.passed and not many.passed
        assert many.value is not None and one.value is not None
        assert many.value > one.value

    def test_strong_result_survives_many_tests(self) -> None:
        assert multiple_testing_penalty(9, 7.15).passed

    def test_rejects_invalid_count(self) -> None:
        assert not multiple_testing_penalty(0, 5.0).passed


class TestPeriodStability:
    def test_single_year_dominance_is_rejected(self) -> None:
        pnl, dates = [], []
        for year in range(2015, 2021):
            for _ in range(30):
                pnl.append(5.0 if year == 2020 else 0.001)
                dates.append(datetime(year, 6, 1))
        res = period_stability(pnl, dates)
        assert not res.passed
        assert "single year" in res.detail

    def test_losing_overall_is_named_not_shown_as_infinity(self) -> None:
        """A strategy that lost money overall has no 'share of total profit' to report."""
        pnl, dates = [], []
        for year in range(2020, 2024):
            for _ in range(30):
                pnl.append(-0.01)
                dates.append(datetime(year, 6, 1))
        res = period_stability(pnl, dates)
        assert not res.passed
        assert "inf" not in res.detail
        assert "lost money overall" in res.detail

    def test_broadly_positive_years_pass(self) -> None:
        rng = np.random.default_rng(4)
        pnl, dates = [], []
        for year in range(2012, 2026):
            for _ in range(30):
                pnl.append(float(rng.normal(0.01, 0.01)))
                dates.append(datetime(year, 6, 1))
        assert period_stability(pnl, dates).passed


class TestConcentration:
    def test_profit_from_a_few_trades_is_flagged(self) -> None:
        pnl = [0.0] * 190 + [10.0] * 10
        res = concentration(pnl)
        assert not res.passed

    def test_evenly_distributed_profit_passes(self) -> None:
        rng = np.random.default_rng(5)
        assert concentration(rng.normal(0.01, 0.005, 400).tolist()).passed

    def test_unprofitable_strategy_fails(self) -> None:
        res = concentration([-0.01] * 100)
        assert not res.passed
        assert "not profitable" in res.detail


class TestBenchmark:
    @staticmethod
    def _series(mean: float, vol: float, n: int = 500, seed: int = 0) -> pd.Series:
        """Returns standardised to an exact mean and volatility."""
        idx = pd.date_range("2015-01-01", periods=n, freq="D")
        raw = np.random.default_rng(seed).normal(0, 1, n)
        raw = (raw - raw.mean()) / raw.std()
        return pd.Series(raw * vol + mean, index=idx)

    def test_worse_risk_adjusted_return_fails(self) -> None:
        """The CBOE check: option selling returns less per unit of risk."""
        strat = self._series(0.0003, 0.010, seed=6)  # Sharpe ~0.03/day-unit
        bench = self._series(0.0008, 0.011, seed=7)  # clearly higher
        res = compare_to_benchmark(strat, bench)
        assert not res.passed
        assert "benchmark is the better" in res.detail

    def test_better_risk_adjusted_return_passes(self) -> None:
        strat = self._series(0.0010, 0.008, seed=8)
        bench = self._series(0.0003, 0.012, seed=9)
        assert compare_to_benchmark(strat, bench).passed


class TestPlacebo:
    """The shipped composite scored -1.61% against placebos of mean -0.65%,
    sd 1.86% — the 35th percentile of runs with no signal at all."""

    def test_real_result_inside_placebo_spread_is_rejected(self) -> None:
        rng = np.random.default_rng(3)
        placebos = rng.normal(-0.65, 1.86, 60).tolist()
        res = placebo_test(-1.61, placebos)
        assert not res.passed
        assert "indistinguishable" in res.detail

    def test_result_far_above_placebos_passes(self) -> None:
        rng = np.random.default_rng(3)
        placebos = rng.normal(-0.65, 1.86, 60).tolist()
        assert placebo_test(20.0, placebos).passed

    def test_too_few_placebos_is_not_a_pass(self) -> None:
        """A handful of draws cannot describe a distribution."""
        res = placebo_test(50.0, [0.1, 0.2, 0.3])
        assert not res.passed
        assert "need 20+" in res.detail

    def test_lower_is_better_inverts_the_verdict(self) -> None:
        rng = np.random.default_rng(4)
        placebos = rng.normal(0.0, 1.0, 60).tolist()
        assert placebo_test(-10.0, placebos, higher_is_better=False).passed
        assert not placebo_test(-10.0, placebos, higher_is_better=True).passed

    def test_shift_offsets_avoid_the_margins(self) -> None:
        """A near-zero shift leaves the signal aligned and biases the null."""
        offs = circular_shift_indices(3494, 40, seed=1, margin=50)
        assert len(offs) == 40
        assert len(set(offs)) == 40
        assert all(50 <= o < 3494 - 50 for o in offs)

    def test_short_series_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="too short"):
            circular_shift_indices(80, 5, margin=50)

    def test_minimum_detectable_effect_is_two_sigma(self) -> None:
        assert minimum_detectable_effect(1.86) == pytest.approx(3.72)


class TestSurvivorship:
    def test_always_warns(self) -> None:
        """A universe of current tickers can never clear this check."""
        res = survivorship_warning(["AAPL", "MSFT"])
        assert not res.passed
        assert "delisted" in res.detail


class TestValidateEndToEnd:
    def test_pure_noise_is_rejected(self) -> None:
        rng = np.random.default_rng(8)
        n = 600
        pnl = rng.normal(0.0, 0.02, n).tolist()
        report = validate("noise", pnl, _dates(n), split="2016-01-01", n_variants_tested=50)
        assert not report.passed
        assert len(report.failures) >= 3

    def test_report_renders_verdict(self) -> None:
        rng = np.random.default_rng(9)
        report = validate(
            "x", rng.normal(0.01, 0.02, 300).tolist(), _dates(300), split="2015-06-01"
        )
        text = str(report)
        assert "VERDICT" in text
        assert isinstance(report, ValidationReport)

    def test_clean_strong_result_can_pass(self) -> None:
        """A genuine edge, spread across years, must clear every check."""
        rng = np.random.default_rng(10)
        n = 600
        pnl = rng.normal(0.012, 0.012, n).tolist()
        dates = _dates(n, start="2013-01-01", step=7)  # ~11.5 years
        report = validate("strong", pnl, dates, split="2019-01-01")
        assert report.passed, [str(c) for c in report.failures]
