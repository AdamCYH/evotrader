"""Tests for per-source signal attribution."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest

from evotrader.backtest.attribution import (
    attribute,
    score_agreement,
    score_calibration,
    score_source,
)


def _stamps(n: int, per_day: int = 1) -> list[datetime]:
    base = datetime(2025, 1, 1)
    return [base + timedelta(days=i // per_day) for i in range(n)]


class TestScoreSource:
    def test_perfect_predictor_is_informative(self) -> None:
        rng = np.random.default_rng(0)
        f = rng.normal(0, 0.02, 300)
        d = np.sign(f)  # knows the answer
        s = score_source("oracle", d, f, _stamps(300))
        assert s.hit_rate == pytest.approx(1.0)
        assert s.is_informative

    def test_random_predictor_is_not_informative(self) -> None:
        rng = np.random.default_rng(1)
        f = rng.normal(0, 0.02, 400)
        d = rng.choice([-1.0, 1.0], 400)
        s = score_source("noise", d, f, _stamps(400))
        assert not s.is_informative

    def test_inverted_predictor_shows_negative_t(self) -> None:
        rng = np.random.default_rng(2)
        f = rng.normal(0.001, 0.02, 300)
        d = -np.sign(f)  # always wrong
        s = score_source("inverted", d, f, _stamps(300))
        assert s.t_stat < 0
        assert not s.is_informative

    def test_abstentions_are_excluded(self) -> None:
        f = [0.01] * 100
        d = [0.0] * 90 + [1.0] * 10  # 90 abstentions
        s = score_source("sparse", d, f, _stamps(100))
        assert s.n_calls == 10

    def test_same_day_calls_collapse_to_one_event(self) -> None:
        """Thirty calls on one day is one observation, not thirty."""
        n = 300
        s = score_source("clustered", [1.0] * n, [0.01] * n, _stamps(n, per_day=30))
        assert s.n_calls == n
        assert s.n_events == pytest.approx(10, abs=1)

    def test_rejects_length_mismatch(self) -> None:
        with pytest.raises(ValueError, match="length mismatch"):
            score_source("x", [1.0, 1.0], [0.01], _stamps(2))

    def test_handles_too_few_calls(self) -> None:
        s = score_source("tiny", [1.0], [0.01], _stamps(1))
        assert s.n_calls == 1
        assert not s.is_informative

    def test_baseline_reflects_majority_direction(self) -> None:
        """In a market that rose 80% of the time, always-long hits 80%."""
        f = [0.01] * 80 + [-0.01] * 20
        s = score_source("always_long", [1.0] * 100, f, _stamps(100))
        assert s.baseline_hit_rate == pytest.approx(0.8)
        assert s.edge_over_baseline == pytest.approx(0.0, abs=1e-9)


class TestAgreement:
    def test_consensus_requires_no_dissent(self) -> None:
        f = [0.01, 0.01, 0.01, 0.01]
        srcs = {
            "a": [1.0, 1.0, 1.0, 1.0],
            "b": [1.0, 1.0, 0.0, -1.0],  # 3rd abstains, 4th dissents
            "c": [1.0, 0.0, 0.0, 1.0],
        }
        s = score_agreement(srcs, f, _stamps(4), min_agreeing=2)
        # cycles 1 and 2 qualify; 3 has only one vote; 4 has a dissenter
        assert s.n_calls == 2

    def test_independent_weak_sources_beat_their_parts(self) -> None:
        """Three weak, independent witnesses should combine into something better."""
        rng = np.random.default_rng(3)
        n = 1200
        truth = rng.choice([-1.0, 1.0], n)
        f = truth * np.abs(rng.normal(0, 0.02, n))

        def weak(seed: int) -> np.ndarray:
            g = np.random.default_rng(seed)
            flip = g.random(n) < 0.38  # 62% accurate, independent errors
            return np.where(flip, -truth, truth)

        srcs = {"a": weak(11), "b": weak(12), "c": weak(13)}
        rep = attribute(srcs, f, _stamps(n))
        assert rep.agreement is not None
        assert rep.agreement_adds_value

    def test_correlated_sources_do_not_beat_their_parts(self) -> None:
        """Three copies of one signal are one witness; agreement must not help."""
        rng = np.random.default_rng(4)
        n = 900
        truth = rng.choice([-1.0, 1.0], n)
        f = truth * np.abs(rng.normal(0, 0.02, n))
        flip = rng.random(n) < 0.40
        shared = np.where(flip, -truth, truth)
        srcs = {"a": shared, "b": shared.copy(), "c": shared.copy()}
        rep = attribute(srcs, f, _stamps(n))
        assert rep.agreement is not None
        assert not rep.agreement_adds_value

    def test_rejects_empty_sources(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            score_agreement({}, [0.01], _stamps(1))


class TestAttributeReport:
    def test_scores_every_source(self) -> None:
        rng = np.random.default_rng(5)
        n = 200
        f = rng.normal(0, 0.02, n)
        srcs = {"algo": np.sign(f), "news": rng.choice([-1.0, 1.0], n)}
        rep = attribute(srcs, f, _stamps(n))
        assert {s.source for s in rep.sources} == {"algo", "news"}
        assert rep.best_single_t > 0

    def test_report_renders_verdict(self) -> None:
        rng = np.random.default_rng(6)
        n = 200
        f = rng.normal(0, 0.02, n)
        srcs = {"algo": rng.choice([-1.0, 1.0], n), "news": rng.choice([-1.0, 1.0], n)}
        text = str(attribute(srcs, f, _stamps(n)))
        assert "VERDICT" in text
        assert "algo" in text


class TestCalibration:
    """Conviction is a probability claim, and it drives position size."""

    def test_detects_overconfidence(self) -> None:
        rng = np.random.default_rng(20)
        n = 900
        f = rng.normal(0, 0.02, n)
        truth = np.sign(f)
        conv = rng.uniform(0.1, 1.0, n)
        # accuracy flat at 55% no matter what conviction is claimed
        d = np.where(rng.random(n) < 0.55, truth, -truth)
        top = score_calibration(conv, d, f)[-1]
        assert top.gap > 0.20
        assert "overconfident" in str(top)

    def test_detects_good_calibration(self) -> None:
        rng = np.random.default_rng(21)
        n = 3000
        f = rng.normal(0, 0.02, n)
        truth = np.sign(f)
        conv = rng.uniform(0.5, 1.0, n)
        # accuracy actually tracks stated conviction
        d = np.where(rng.random(n) < conv, truth, -truth)
        for b in score_calibration(conv, d, f):
            if b.n > 50:
                assert abs(b.gap) < 0.12, str(b)

    def test_detects_underconfidence(self) -> None:
        rng = np.random.default_rng(22)
        n = 600
        f = rng.normal(0, 0.02, n)
        truth = np.sign(f)
        conv = np.full(n, 0.3)
        d = np.where(rng.random(n) < 0.85, truth, -truth)  # far better than claimed
        band = next(b for b in score_calibration(conv, d, f) if b.n > 0)
        assert band.gap < -0.20
        assert "underconfident" in str(band)

    def test_abstentions_excluded(self) -> None:
        f = [0.01] * 100
        d = [0.0] * 90 + [1.0] * 10
        conv = [0.9] * 100
        total = sum(b.n for b in score_calibration(conv, d, f))
        assert total == 10

    def test_rejects_length_mismatch(self) -> None:
        with pytest.raises(ValueError, match="length mismatch"):
            score_calibration([0.5], [1.0, 1.0], [0.01, 0.01])

    def test_empty_band_renders_cleanly(self) -> None:
        b = next(x for x in score_calibration([0.9], [1.0], [0.01]) if x.n == 0)
        assert "no calls" in str(b)
