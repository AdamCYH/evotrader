"""Regression tests: Cycle resilience — news_sentiment is non-mandatory.

See: data/evolution/reviews/20260720_192227_wire_event_context_into_indicators_and_harden_cycle_errors.md
Finding #2 [HIGH]: news_sentiment failure should not abort the trading cycle.
"""

from __future__ import annotations


class TestMandatoryStages:
    """Verify that _MANDATORY_STAGES allows cycles without news_sentiment."""

    def test_news_sentiment_not_in_mandatory_stages(self) -> None:
        """news_sentiment should not be a mandatory stage for cycle completion."""
        from evotrader.main import _MANDATORY_STAGES

        assert "news_sentiment" not in _MANDATORY_STAGES, (
            "news_sentiment should not be mandatory — Alpha Vantage rate limits "
            "cause ~30% cycle mortality when it is required"
        )

    def test_gather_market_data_is_mandatory(self) -> None:
        """gather_market_data must remain mandatory."""
        from evotrader.main import _MANDATORY_STAGES

        assert "gather_market_data" in _MANDATORY_STAGES

    def test_strategy_is_mandatory(self) -> None:
        """strategy must remain mandatory."""
        from evotrader.main import _MANDATORY_STAGES

        assert "strategy" in _MANDATORY_STAGES

    def test_news_sentiment_still_in_pipeline(self) -> None:
        """news_sentiment should still be in the pipeline (attempted, not required)."""
        from evotrader.main import _PIPELINE_STAGES

        assert "news_sentiment" in _PIPELINE_STAGES, (
            "news_sentiment should remain in the pipeline — it's still attempted, "
            "just not required for cycle completion"
        )
