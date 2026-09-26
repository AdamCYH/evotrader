"""Tests for building scenarios from recorded system history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from evotrader.scenarios.from_history import (
    NewsReport,
    attach_forward_returns,
    pair,
    strong_cases,
)


def _snaps(times: list[datetime]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "t": pd.to_datetime(times, utc=True),
            "close_price": [100.0] * len(times),
            "composite_signal": [0.1] * len(times),
        }
    )


def _news(
    times: list[datetime], scores: list[float] | None = None, confs: list[float] | None = None
) -> pd.DataFrame:
    n = len(times)
    return pd.DataFrame(
        {
            "t": pd.to_datetime(times, utc=True),
            "score": scores or [0.5] * n,
            "confidence": confs or [0.9] * n,
            "reasoning": ["r"] * n,
        }
    )


BASE = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


class TestBackwardOnlyMatching:
    """The load-bearing correctness property: no news from the future."""

    def test_future_news_is_never_paired(self) -> None:
        snap = _snaps([BASE])
        future = _news([BASE + timedelta(minutes=10)])
        assert pair(snap, future, max_age_minutes=90).empty

    def test_prior_news_is_paired(self) -> None:
        snap = _snaps([BASE])
        prior = _news([BASE - timedelta(minutes=10)])
        out = pair(snap, prior, max_age_minutes=90)
        assert len(out) == 1
        assert out.iloc[0]["news_age_min"] == pytest.approx(10.0)

    def test_picks_most_recent_prior_report(self) -> None:
        snap = _snaps([BASE])
        news = _news(
            [BASE - timedelta(minutes=80), BASE - timedelta(minutes=5)],
            scores=[-0.9, 0.4],
        )
        out = pair(snap, news, max_age_minutes=90)
        assert out.iloc[0]["score"] == pytest.approx(0.4)

    def test_stale_news_beyond_tolerance_is_dropped(self) -> None:
        snap = _snaps([BASE])
        stale = _news([BASE - timedelta(minutes=200)])
        assert pair(snap, stale, max_age_minutes=90).empty

    def test_empty_inputs_return_empty(self) -> None:
        assert pair(pd.DataFrame(), _news([BASE]), 90).empty
        assert pair(_snaps([BASE]), pd.DataFrame(), 90).empty


class TestForwardReturns:
    def _prices(self) -> pd.Series:
        idx = pd.date_range("2026-07-01", periods=20, freq="D", tz="UTC")
        return pd.Series([100.0 + i for i in range(20)], index=idx)

    def test_computes_realised_return(self) -> None:
        paired = pd.DataFrame({"t": pd.to_datetime([datetime(2026, 7, 2, tzinfo=UTC)])})
        out = attach_forward_returns(paired, self._prices(), horizon_days=5)
        assert len(out) == 1
        # 101 -> 106 over five days
        assert out.iloc[0]["forward_return_pct"] == pytest.approx(5 / 101 * 100, rel=1e-3)

    def test_rows_beyond_price_data_are_dropped_not_filled(self) -> None:
        """A decision must never be scored against a return that does not exist."""
        paired = pd.DataFrame({"t": pd.to_datetime([datetime(2026, 7, 19, tzinfo=UTC)])})
        assert attach_forward_returns(paired, self._prices(), horizon_days=5).empty

    def test_empty_input_passes_through(self) -> None:
        assert attach_forward_returns(pd.DataFrame(), self._prices()).empty


class TestStrongCases:
    def test_filters_on_score_and_confidence(self) -> None:
        df = pd.DataFrame(
            {
                "score": [0.5, 0.05, 0.5, -0.4],
                "confidence": [0.9, 0.9, 0.3, 0.8],
            }
        )
        out = strong_cases(df, min_abs_score=0.2, min_confidence=0.7)
        assert len(out) == 2
        assert set(out["score"]) == {0.5, -0.4}

    def test_empty_input(self) -> None:
        assert strong_cases(pd.DataFrame()).empty


class TestNewsRendering:
    def test_renders_agent_facing_block(self) -> None:
        r = NewsReport(
            timestamp=pd.Timestamp("2026-07-01T12:00Z"),
            score=-0.35,
            confidence=0.82,
            reasoning="Jobs   report\n  triggered rate repricing.",
        )
        out = r.render()
        assert "aggregate_score: -0.35" in out
        assert "confidence: 0.82" in out
        # whitespace normalised so the prompt stays clean
        assert "Jobs report triggered rate repricing." in out
