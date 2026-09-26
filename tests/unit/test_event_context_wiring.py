"""Regression tests: Event context wiring, overlay semantics, VWAP anchor dampening.

Validates the fixes from:
    data/evolution/reviews/20260716_230311_wire_event_context_into_technical_indicators.md

Covers:
  - Finding #1 (CRITICAL): Event context fields populate into TechnicalIndicators
  - Finding #2 (HIGH): Composite overlay semantics (pre-event dampen, post-event boost)
  - Finding #3 (MEDIUM): Overlay/multiplier role in composite strategy
  - Finding #4 (LOW): VWAP stale-anchor dampening in mean_reversion & intraday_vwap_zscore
"""

from __future__ import annotations

from datetime import datetime

import pytest

from evotrader.algorithms.composite import CompositeStrategy
from evotrader.algorithms.strategies.event_window_timing import (
    EventWindowTimingStrategy,
)
from evotrader.algorithms.strategies.gap import GapStrategy
from evotrader.algorithms.strategies.intraday_vwap_zscore import (
    IntradayVwapZscoreStrategy,
)
from evotrader.algorithms.strategies.mean_reversion import MeanReversionStrategy
from evotrader.algorithms.strategies.momentum import MomentumStrategy
from evotrader.models.market import (
    OHLCV,
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)

# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────


def _make_snapshot(
    *,
    hours_to_event: float | None = None,
    hours_since_event: float | None = None,
    atm_iv_30dte: float | None = None,
    atm_iv_pre_event: float | None = None,
    event_type: str | None = None,
    vwap: float | None = 453.0,
    vwap_anchor: str | None = "current_session",
    price: float = 455.01,
    regime: MarketRegime = MarketRegime.RANGE_BOUND,
    candles: list[OHLCV] | None = None,
) -> MarketSnapshot:
    """Build a MarketSnapshot with configurable event context and VWAP anchor."""
    return MarketSnapshot(
        ticker="QQQ",
        timestamp=datetime(2026, 7, 16, 14, 30),
        quote=Quote(
            ticker="QQQ",
            bid=price - 0.01,
            ask=price + 0.01,
            last=price,
            volume=60e6,
            timestamp=datetime(2026, 7, 16, 14, 30),
        ),
        indicators=TechnicalIndicators(
            rsi_14=35.0,
            macd_line=0.8,
            macd_signal=0.3,
            macd_histogram=0.5,
            bollinger_upper=460.0,
            bollinger_middle=452.0,
            bollinger_lower=444.0,
            ema_9=454.5,
            ema_21=453.0,
            sma_20=452.0,
            sma_50=448.0,
            vwap=vwap,
            vwap_anchor=vwap_anchor,
            ibs=0.25,
            atr_14=3.5,
            volume_sma_20=50e6,
            relative_volume=1.2,
            # Event context fields
            hours_to_event=hours_to_event,
            hours_since_event=hours_since_event,
            atm_iv_30dte=atm_iv_30dte,
            atm_iv_pre_event=atm_iv_pre_event,
            event_type=event_type,
        ),
        regime=RegimeClassification(
            regime=regime,
            confidence=0.7,
            reasoning="test",
        ),
        recent_candles=candles or [],
        daily_change_pct=0.2,
        gap_pct=0.1,
    )


def _make_composite_with_event_timing(
    *,
    pre_event_dampener: float = 0.05,
    post_event_boost: float = 1.5,
) -> CompositeStrategy:
    """Build a composite with event_window_timing as an overlay strategy."""
    # Supply explicit regime weights for this 4-strategy test composite so
    # it doesn't depend on the global defaults (which now cover 7 strategies).
    from evotrader.models.market import MarketRegime

    _test_regime_weights = {
        MarketRegime.RANGE_BOUND: {
            "momentum": 0.35,
            "mean_reversion": 0.25,
            "gap": 0.20,
            "event_window_timing": 0.05,
        },
        MarketRegime.TRENDING_BULL: {
            "momentum": 0.45,
            "mean_reversion": 0.20,
            "gap": 0.20,
            "event_window_timing": 0.05,
        },
        MarketRegime.TRENDING_BEAR: {
            "momentum": 0.45,
            "mean_reversion": 0.20,
            "gap": 0.20,
            "event_window_timing": 0.05,
        },
        MarketRegime.HIGH_VOLATILITY: {
            "momentum": 0.50,
            "mean_reversion": 0.15,
            "gap": 0.20,
            "event_window_timing": 0.05,
        },
    }
    return CompositeStrategy(
        sub_strategies={
            "momentum": MomentumStrategy(),
            "mean_reversion": MeanReversionStrategy(),
            "gap": GapStrategy(),
            "event_window_timing": EventWindowTimingStrategy(
                pre_event_dampener=pre_event_dampener,
                post_event_boost=post_event_boost,
            ),
        },
        weights={
            "momentum": 0.40,
            "mean_reversion": 0.30,
            "gap": 0.25,
            "event_window_timing": 0.05,
        },
        regime_weights=_test_regime_weights,
    )


# ─────────────────────────────────────────────────────────────────
# Finding #1: Event context field population
# ─────────────────────────────────────────────────────────────────


class TestEventContextFieldPopulation:
    """Verify that TechnicalIndicators event context fields are set correctly."""

    def test_event_context_fields_exist_on_model(self) -> None:
        """TechnicalIndicators has all expected event context fields."""
        ind = TechnicalIndicators(
            hours_to_event=24.5,
            hours_since_event=12.0,
            atm_iv_30dte=0.25,
            atm_iv_pre_event=0.30,
            event_type="earnings",
        )
        assert ind.hours_to_event == 24.5
        assert ind.hours_since_event == 12.0
        assert ind.atm_iv_30dte == 0.25
        assert ind.atm_iv_pre_event == 0.30
        assert ind.event_type == "earnings"

    def test_event_context_defaults_to_none(self) -> None:
        """All event context fields default to None when unset."""
        ind = TechnicalIndicators()
        assert ind.hours_to_event is None
        assert ind.hours_since_event is None
        assert ind.atm_iv_30dte is None
        assert ind.atm_iv_pre_event is None
        assert ind.event_type is None

    def test_snapshot_passes_event_context_to_strategy(self) -> None:
        """Event context flows from snapshot through to strategy."""
        snapshot = _make_snapshot(
            hours_to_event=18.0,
            event_type="earnings",
        )
        strategy = EventWindowTimingStrategy()
        signal = strategy.compute_signal(snapshot)
        # Should be in pre-event blackout mode, not abstaining
        assert signal.metadata.get("mode") == "pre_event_blackout"
        assert signal.metadata.get("applicable") is True


# ─────────────────────────────────────────────────────────────────
# Finding #2 & #3: Composite overlay semantics
# ─────────────────────────────────────────────────────────────────


class TestCompositeOverlaySemantics:
    """Verify overlay strategies multiply the composite instead of averaging in."""

    def test_pre_event_blackout_dampens_composite(self) -> None:
        """Pre-event blackout should dramatically reduce composite magnitude."""
        # Baseline: no event context → event_window_timing abstains,
        # composite uses additive strategies only
        baseline_snap = _make_snapshot()
        composite = _make_composite_with_event_timing()
        baseline_signal = composite.compute_signal(baseline_snap)
        baseline_value = abs(baseline_signal.value)

        # During blackout: event_window_timing emits factor=0.05 overlay
        blackout_snap = _make_snapshot(
            hours_to_event=18.0,
            event_type="earnings",
        )
        blackout_signal = composite.compute_signal(blackout_snap)
        blackout_value = abs(blackout_signal.value)

        # The blackout should dramatically suppress the composite
        # (factor=0.05 means ~5% of original magnitude)
        if baseline_value > 0.01:
            assert blackout_value < baseline_value * 0.20, (
                f"Blackout ({blackout_value:.4f}) should be < 20% of "
                f"baseline ({baseline_value:.4f})"
            )

    def test_post_event_amplifies_composite(self) -> None:
        """Post-event with IV crush + overextension should amplify composite."""
        # Post-event scenario: IV crushed, price overextended from VWAP
        post_snap = _make_snapshot(
            hours_since_event=6.0,
            atm_iv_30dte=0.20,
            atm_iv_pre_event=0.30,  # 33% IV crush
            event_type="earnings",
            vwap=450.0,
            price=458.0,  # Overextended above VWAP
        )
        composite = _make_composite_with_event_timing()
        detailed = composite.compute_detailed_signal(post_snap)

        # Find the event_window_timing signal in sub_signals
        ewt_signal = next(s for s in detailed.signals if s.name == "event_window_timing")
        assert ewt_signal.metadata.get("role") == "multiplier"
        assert ewt_signal.metadata.get("factor") == 1.5
        assert ewt_signal.metadata.get("mode") == "post_event_entry"

    def test_no_event_context_abstains(self) -> None:
        """Without event context, strategy abstains (no overlay effect)."""
        snapshot = _make_snapshot()  # No event fields set
        composite = _make_composite_with_event_timing()
        detailed = composite.compute_detailed_signal(snapshot)

        ewt_signal = next(s for s in detailed.signals if s.name == "event_window_timing")
        assert ewt_signal.metadata.get("applicable") is False
        assert ewt_signal.metadata.get("mode") == "no_event_data"
        # No 'role' key → not treated as overlay
        assert "role" not in ewt_signal.metadata

    def test_backward_compat_no_overlays(self) -> None:
        """Composite without any overlay strategies behaves identically to before."""
        # Composite with only additive strategies (no event_window_timing)
        composite_old = CompositeStrategy(
            sub_strategies={
                "momentum": MomentumStrategy(),
                "mean_reversion": MeanReversionStrategy(),
                "gap": GapStrategy(),
            },
            weights={
                "momentum": 0.40,
                "mean_reversion": 0.35,
                "gap": 0.25,
            },
        )
        snapshot = _make_snapshot()
        signal = composite_old.compute_signal(snapshot)
        assert -1.0 <= signal.value <= 1.0
        assert signal.name == "composite"

        # The detailed signal should show all 3 sub-signals
        detailed = composite_old.compute_detailed_signal(snapshot)
        assert len(detailed.signals) == 3
        # No overlays applied → renormalized should match old behavior
        for s in detailed.signals:
            assert s.metadata.get("role") != "multiplier"

    def test_overlay_with_abstaining_event_same_as_no_overlay(self) -> None:
        """When event_window_timing abstains, composite matches no-overlay behavior."""
        from evotrader.models.market import MarketRegime

        # Regime weights matching the 3-strategy subset of the "with" composite
        _3strat_regime_weights = {
            MarketRegime.RANGE_BOUND: {
                "momentum": 0.35,
                "mean_reversion": 0.25,
                "gap": 0.20,
            },
            MarketRegime.TRENDING_BULL: {
                "momentum": 0.45,
                "mean_reversion": 0.20,
                "gap": 0.20,
            },
            MarketRegime.TRENDING_BEAR: {
                "momentum": 0.45,
                "mean_reversion": 0.20,
                "gap": 0.20,
            },
            MarketRegime.HIGH_VOLATILITY: {
                "momentum": 0.50,
                "mean_reversion": 0.15,
                "gap": 0.20,
            },
        }
        # Composite WITH event_window_timing but no event context
        composite_with = _make_composite_with_event_timing()
        # Composite WITHOUT event_window_timing
        composite_without = CompositeStrategy(
            sub_strategies={
                "momentum": MomentumStrategy(),
                "mean_reversion": MeanReversionStrategy(),
                "gap": GapStrategy(),
            },
            weights={
                "momentum": 0.40,
                "mean_reversion": 0.30,
                "gap": 0.25,
            },
            regime_weights=_3strat_regime_weights,
        )
        snapshot = _make_snapshot()  # No event context

        sig_with = composite_with.compute_signal(snapshot)
        sig_without = composite_without.compute_signal(snapshot)

        # Values should be very similar (both renormalize over
        # the same additive strategies since EWT abstains)
        assert abs(sig_with.value - sig_without.value) < 0.05, (
            f"With EWT abstaining ({sig_with.value:.4f}) should ≈ "
            f"without EWT ({sig_without.value:.4f})"
        )


# ─────────────────────────────────────────────────────────────────
# Event Window Timing strategy: overlay metadata
# ─────────────────────────────────────────────────────────────────


class TestEventWindowTimingOverlayMetadata:
    """Verify the strategy emits correct overlay metadata in each mode."""

    def test_pre_event_emits_multiplier_role(self) -> None:
        snapshot = _make_snapshot(
            hours_to_event=24.0,
            event_type="earnings",
        )
        strategy = EventWindowTimingStrategy(pre_event_dampener=0.05)
        signal = strategy.compute_signal(snapshot)

        assert signal.metadata["role"] == "multiplier"
        assert signal.metadata["factor"] == 0.05
        assert signal.metadata["mode"] == "pre_event_blackout"
        assert signal.metadata["applicable"] is True

    def test_post_event_entry_emits_multiplier_role(self) -> None:
        snapshot = _make_snapshot(
            hours_since_event=6.0,
            atm_iv_30dte=0.18,
            atm_iv_pre_event=0.25,  # IV crush > 10%
            event_type="earnings",
            vwap=450.0,
            price=458.0,  # Overextended
        )
        strategy = EventWindowTimingStrategy(post_event_boost=1.5)
        signal = strategy.compute_signal(snapshot)

        assert signal.metadata["role"] == "multiplier"
        assert signal.metadata["factor"] == 1.5
        assert signal.metadata["mode"] == "post_event_entry"
        assert signal.metadata["applicable"] is True

    def test_post_event_no_iv_abstains(self) -> None:
        """Post-event without IV data should abstain conservatively."""
        snapshot = _make_snapshot(
            hours_since_event=6.0,
            # No IV data
            event_type="earnings",
        )
        strategy = EventWindowTimingStrategy()
        signal = strategy.compute_signal(snapshot)

        assert signal.metadata["applicable"] is False
        assert signal.metadata["mode"] == "post_event_no_iv"
        assert "role" not in signal.metadata

    def test_outside_event_window_abstains(self) -> None:
        """Outside event windows, the strategy abstains."""
        snapshot = _make_snapshot(
            hours_to_event=100.0,  # Far from event
            hours_since_event=100.0,  # Long past event
            event_type="earnings",
        )
        strategy = EventWindowTimingStrategy(blackout_hours=30, post_event_hours=48)
        signal = strategy.compute_signal(snapshot)

        assert signal.metadata["applicable"] is False
        assert signal.metadata["mode"] == "no_event"
        assert "role" not in signal.metadata

    def test_no_event_data_abstains(self) -> None:
        """Without any event context, the strategy abstains."""
        snapshot = _make_snapshot()
        strategy = EventWindowTimingStrategy()
        signal = strategy.compute_signal(snapshot)

        assert signal.metadata["applicable"] is False
        assert signal.metadata["mode"] == "no_event_data"
        assert "role" not in signal.metadata


# ─────────────────────────────────────────────────────────────────
# Finding #4: VWAP stale-anchor dampening
# ─────────────────────────────────────────────────────────────────


class TestVwapStaleAnchorAbstention:
    """Verify VWAP-derived strategies abstain when vwap_anchor != 'current_session'.

    After the 7/24 stale-anchor dead-code fix, any anchor other than
    'current_session' (including None / 'unknown' / 'prior_session')
    causes the VWAP component to abstain (0.0) rather than vote with
    a stale value.

    See: data/evolution/reviews/20260724_210712_stale_vwap_anchor_dampener_dead_code.md
    """

    def test_mean_reversion_abstains_prior_session_vwap(self) -> None:
        """Mean reversion no longer has VWAP component (removed in v017).

        VWAP-deviation authority now lives solely in intraday_vwap_zscore.
        Verify that vwap_signal is absent from metadata regardless of anchor.
        """
        strategy = MeanReversionStrategy()

        # Current session
        current_snap = _make_snapshot(
            vwap=453.0,
            vwap_anchor="current_session",
            price=455.01,
        )
        sig_current = strategy.compute_signal(current_snap)

        # Prior session
        prior_snap = _make_snapshot(
            vwap=453.0,
            vwap_anchor="prior_session",
            price=455.01,
        )
        sig_prior = strategy.compute_signal(prior_snap)

        # Neither should have vwap_signal — VWAP removed in v017
        assert "vwap_signal" not in sig_current.metadata
        assert "vwap_signal" not in sig_prior.metadata

    def test_mean_reversion_abstains_unknown_anchor(self) -> None:
        """Mean reversion no longer has VWAP component — anchor is moot."""
        strategy = MeanReversionStrategy()

        snap = _make_snapshot(
            vwap=453.0,
            vwap_anchor=None,
            price=455.01,
        )
        signal = strategy.compute_signal(snap)

        # VWAP component removed in v017
        assert "vwap_signal" not in signal.metadata
        # Metadata should still record anchor for diagnostics
        assert signal.metadata.get("vwap_anchor_at_compute") == "unknown"

    def test_mean_reversion_surfaces_vwap_anchor_at_compute(self) -> None:
        """Mean reversion should include vwap_anchor_at_compute in metadata."""
        strategy = MeanReversionStrategy()

        snap = _make_snapshot(vwap_anchor="prior_session")
        signal = strategy.compute_signal(snap)
        assert signal.metadata.get("vwap_anchor_at_compute") == "prior_session"

        snap2 = _make_snapshot(vwap_anchor="current_session")
        signal2 = strategy.compute_signal(snap2)
        assert signal2.metadata.get("vwap_anchor_at_compute") == "current_session"

        snap3 = _make_snapshot(vwap_anchor=None)
        signal3 = strategy.compute_signal(snap3)
        assert signal3.metadata.get("vwap_anchor_at_compute") == "unknown"

    def test_intraday_vwap_zscore_abstains_prior_session(self) -> None:
        """Intraday VWAP z-score should abstain (0.0) on prior_session."""
        strategy = IntradayVwapZscoreStrategy(
            zscore_window=5,
            entry_z=0.5,  # Low threshold so we can trigger easily
        )

        # Create candles with enough spread to trigger a z-score signal
        base_price = 450.0
        candles = []
        for i in range(10):
            p = base_price + (i * 0.5)  # Rising prices
            candles.append(
                OHLCV(
                    timestamp=datetime(2026, 7, 16, 10, i * 5),
                    open=p,
                    high=p + 0.5,
                    low=p - 0.5,
                    close=p,
                    volume=1e6,
                )
            )

        current_snap = _make_snapshot(
            vwap=base_price,
            vwap_anchor="current_session",
            price=base_price + 8.0,  # Well above VWAP
            candles=candles,
        )
        sig_current = strategy.compute_signal(current_snap)

        prior_snap = _make_snapshot(
            vwap=base_price,
            vwap_anchor="prior_session",
            price=base_price + 8.0,
            candles=candles,
        )
        sig_prior = strategy.compute_signal(prior_snap)

        # Current session should produce a non-zero signal
        assert sig_current.value != 0.0, "Signal should be non-zero with current_session anchor"

        # Prior session must abstain completely
        assert sig_prior.value == 0.0, (
            f"Signal should be 0.0 with prior_session anchor, got {sig_prior.value:.4f}"
        )

    def test_intraday_vwap_zscore_abstains_unknown_anchor(self) -> None:
        """Intraday VWAP z-score should abstain (0.0) when anchor is None."""
        strategy = IntradayVwapZscoreStrategy(
            zscore_window=5,
            entry_z=0.5,
        )

        base_price = 450.0
        candles = []
        for i in range(10):
            p = base_price + (i * 0.5)
            candles.append(
                OHLCV(
                    timestamp=datetime(2026, 7, 16, 10, i * 5),
                    open=p,
                    high=p + 0.5,
                    low=p - 0.5,
                    close=p,
                    volume=1e6,
                )
            )

        snap = _make_snapshot(
            vwap=base_price,
            vwap_anchor=None,
            price=base_price + 8.0,
            candles=candles,
        )
        sig = strategy.compute_signal(snap)

        assert sig.value == 0.0, f"Signal should be 0.0 with None anchor, got {sig.value:.4f}"

    def test_intraday_vwap_zscore_surfaces_vwap_anchor(self) -> None:
        """Intraday VWAP z-score should include vwap_anchor in metadata."""
        strategy = IntradayVwapZscoreStrategy()

        candles = [
            OHLCV(
                timestamp=datetime(2026, 7, 16, 10 + (i * 5) // 60, (i * 5) % 60),
                open=450.0 + i,
                high=451.0 + i,
                low=449.0 + i,
                close=450.0 + i,
                volume=1e6,
            )
            for i in range(25)
        ]

        snap = _make_snapshot(
            vwap_anchor="prior_session",
            candles=candles,
        )
        signal = strategy.compute_signal(snap)
        assert signal.metadata.get("vwap_anchor") == "prior_session"


# ─────────────────────────────────────────────────────────────────
# Event context enrichment helper (unit test)
# ─────────────────────────────────────────────────────────────────


class TestEnrichEventContextHelper:
    """Unit test the _enrich_event_context helper's parsing logic."""

    @pytest.mark.asyncio
    async def test_enrich_with_upcoming_earnings(self) -> None:
        """Upcoming top-constituent earnings should set hours_to_event."""
        from unittest.mock import patch

        from evotrader.agents import tools

        now = datetime(2026, 7, 16, 14, 0, tzinfo=__import__("datetime").timezone.utc)
        indicators: dict = {}

        # Mock MCP responses
        earnings_response = {
            "data": {
                "results": [
                    {
                        "symbol": "TSLA",
                        "report_date": "2026-07-17",
                        "eps": {"actual": None},
                    },
                    {
                        "symbol": "SOME_SMALL_CAP",
                        "report_date": "2026-07-17",
                        "eps": {"actual": None},
                    },
                ]
            }
        }

        chain_response = None  # No option chain for this test

        async def mock_mcp(tool_name: str, args: dict):
            if tool_name == "get_earnings_calendar":
                return earnings_response
            return chain_response

        # The relevant universe is resolved from the traded ticker's live
        # holdings, so the test states it explicitly rather than relying on a
        # hardcoded constituent list.
        async def universe(_config=None):
            return ("QQQ", "TSLA", "NVDA")

        with (
            patch.object(tools, "_call_mcp_tool", side_effect=mock_mcp),
            patch.object(tools, "relevant_symbols", side_effect=universe),
        ):
            await tools._enrich_event_context(indicators, "QQQ", now)

        # TSLA is in the resolved universe → sets hours_to_event
        assert "hours_to_event" in indicators
        assert indicators["event_type"] == "earnings"
        # SOME_SMALL_CAP is not in the universe → ignored

    @pytest.mark.asyncio
    async def test_enrich_with_past_earnings(self) -> None:
        """Past top-constituent earnings should set hours_since_event."""
        from unittest.mock import patch

        from evotrader.agents import tools

        now = datetime(2026, 7, 16, 14, 0, tzinfo=__import__("datetime").timezone.utc)
        indicators: dict = {}

        earnings_response = {
            "data": {
                "results": [
                    {
                        "symbol": "NFLX",
                        "report_date": "2026-07-15",
                        "eps": {"actual": 5.40},
                    },
                ]
            }
        }

        async def mock_mcp(tool_name: str, args: dict):
            if tool_name == "get_earnings_calendar":
                return earnings_response
            return None

        async def universe(_config=None):
            return ("QQQ", "NFLX")

        with (
            patch.object(tools, "_call_mcp_tool", side_effect=mock_mcp),
            patch.object(tools, "relevant_symbols", side_effect=universe),
        ):
            await tools._enrich_event_context(indicators, "QQQ", now)

        assert "hours_since_event" in indicators
        assert indicators["event_type"] == "earnings"

    @pytest.mark.asyncio
    async def test_unresolved_universe_narrows_to_the_traded_ticker(self) -> None:
        """A failed universe lookup must not let an unrelated symbol set the clock.

        Filtering nothing here would let any small-cap's report drive
        ``hours_to_event`` and corrupt the event-window strategy, so the
        fallback narrows to the traded ticker instead.
        """
        from unittest.mock import patch

        from evotrader.agents import tools

        now = datetime(2026, 7, 16, 14, 0, tzinfo=__import__("datetime").timezone.utc)
        indicators: dict = {}

        earnings_response = {
            "data": {
                "results": [
                    {
                        "symbol": "SOME_SMALL_CAP",
                        "report_date": "2026-07-17",
                        "eps": {"actual": None},
                    },
                ]
            }
        }

        async def mock_mcp(tool_name: str, args: dict):
            return earnings_response if tool_name == "get_earnings_calendar" else None

        async def no_universe(_config=None):
            return ()

        with (
            patch.object(tools, "_call_mcp_tool", side_effect=mock_mcp),
            patch.object(tools, "relevant_symbols", side_effect=no_universe),
        ):
            await tools._enrich_event_context(indicators, "QQQ", now)

        assert indicators.get("hours_to_event") is None

    @pytest.mark.asyncio
    async def test_enrich_no_relevant_earnings(self) -> None:
        """No top-constituent earnings → no event context fields."""
        from unittest.mock import patch

        from evotrader.agents import tools

        now = datetime(2026, 7, 16, 14, 0, tzinfo=__import__("datetime").timezone.utc)
        indicators: dict = {}

        earnings_response = {
            "data": {
                "results": [
                    {
                        "symbol": "OBSCURE_CO",
                        "report_date": "2026-07-17",
                        "eps": {"actual": None},
                    },
                ]
            }
        }

        async def mock_mcp(tool_name: str, args: dict):
            if tool_name == "get_earnings_calendar":
                return earnings_response
            return None

        with patch.object(tools, "_call_mcp_tool", side_effect=mock_mcp):
            await tools._enrich_event_context(indicators, "QQQ", now)

        assert "hours_to_event" not in indicators
        assert "event_type" not in indicators

    @pytest.mark.asyncio
    async def test_enrich_mcp_failure_is_graceful(self) -> None:
        """MCP failure should not crash — just leave indicators unchanged."""
        from unittest.mock import patch

        from evotrader.agents import tools

        now = datetime(2026, 7, 16, 14, 0, tzinfo=__import__("datetime").timezone.utc)
        indicators: dict = {"rsi_14": 45.0}

        async def mock_mcp(tool_name: str, args: dict):
            return None  # MCP call fails

        with patch.object(tools, "_call_mcp_tool", side_effect=mock_mcp):
            await tools._enrich_event_context(indicators, "QQQ", now)

        # Original indicators untouched, no event fields added
        assert indicators == {"rsi_14": 45.0}


# ─────────────────────────────────────────────────────────────────
# Finding #1 (20260720): Alpha Vantage rate-limit payload handling
# ─────────────────────────────────────────────────────────────────


class TestEnrichAlphaVantageRateLimit:
    """Verify that AV rate-limit payloads don't silently break enrichment.

    See: data/evolution/reviews/20260720_192227_wire_event_context_into_indicators_and_harden_cycle_errors.md
    """

    @pytest.mark.asyncio
    async def test_enrich_handles_av_information_payload(self) -> None:
        """AV 'Information' rate-limit payload should be detected gracefully."""
        from unittest.mock import patch

        from evotrader.agents import tools

        now = datetime(2026, 7, 16, 14, 0, tzinfo=__import__("datetime").timezone.utc)
        indicators: dict = {"rsi_14": 45.0}

        # Alpha Vantage returns 200 OK with an "Information" key when rate-limited
        rate_limited_response = {
            "Information": "Thank you for using Alpha Vantage! Our standard API rate limit is 25 requests per day."
        }

        async def mock_mcp(tool_name: str, args: dict):
            if tool_name == "get_earnings_calendar":
                return rate_limited_response
            return None

        with patch.object(tools, "_call_mcp_tool", side_effect=mock_mcp):
            await tools._enrich_event_context(indicators, "QQQ", now)

        # No event fields should be set — rate-limit was detected
        assert "hours_to_event" not in indicators
        assert "event_type" not in indicators
        assert indicators == {"rsi_14": 45.0}

    @pytest.mark.asyncio
    async def test_enrich_handles_av_note_payload(self) -> None:
        """AV 'Note' rate-limit payload should be detected gracefully."""
        from unittest.mock import patch

        from evotrader.agents import tools

        now = datetime(2026, 7, 16, 14, 0, tzinfo=__import__("datetime").timezone.utc)
        indicators: dict = {}

        note_response = {
            "Note": "Thank you for using Alpha Vantage! Please visit https://www.alphavantage.co/premium/ for premium API keys."
        }

        async def mock_mcp(tool_name: str, args: dict):
            if tool_name == "get_earnings_calendar":
                return note_response
            return None

        with patch.object(tools, "_call_mcp_tool", side_effect=mock_mcp):
            await tools._enrich_event_context(indicators, "QQQ", now)

        assert "hours_to_event" not in indicators
        assert "event_type" not in indicators


# ─────────────────────────────────────────────────────────────────
# Finding #3 (20260720): Effective weights in composite metadata
# ─────────────────────────────────────────────────────────────────


class TestCompositeEffectiveWeights:
    """Verify composite metadata includes effective post-renormalization weights.

    See: data/evolution/reviews/20260720_192227_wire_event_context_into_indicators_and_harden_cycle_errors.md
    """

    def test_effective_weights_present_when_renormalized(self) -> None:
        """When a strategy abstains and renormalization occurs,
        metadata should contain both configured and effective weights."""
        composite = _make_composite_with_event_timing()
        # No event context → event_window_timing abstains → renormalization
        snapshot = _make_snapshot()
        signal = composite.compute_signal(snapshot)

        # event_window_timing abstains (no event context), and gap may also
        # abstain (gap_pct below threshold), so renormalization should occur
        assert signal.metadata.get("renormalized"), (
            "Expected renormalization when event_window_timing abstains"
        )

        # At minimum, momentum and mean_reversion are always applicable
        for name in ("momentum", "mean_reversion"):
            key = f"{name}_effective_weight"
            assert key in signal.metadata, f"Missing {key} in metadata when renormalized"
            assert signal.metadata[key] > 0, f"{key} should be positive"

        # event_window_timing should NOT have an effective weight
        # (it abstained and was excluded)
        assert "event_window_timing_effective_weight" not in signal.metadata

    def test_effective_weights_sum_to_one(self) -> None:
        """Effective weights should sum to 1.0 (within rounding)."""
        composite = _make_composite_with_event_timing()
        snapshot = _make_snapshot()  # No event context
        signal = composite.compute_signal(snapshot)

        if signal.metadata.get("renormalized"):
            effective_weights = [
                v for k, v in signal.metadata.items() if k.endswith("_effective_weight")
            ]
            total = sum(effective_weights)
            assert abs(total - 1.0) < 0.01, f"Effective weights should sum to 1.0, got {total:.4f}"

    def test_no_effective_weights_without_renormalization(self) -> None:
        """When no renormalization occurs, no effective_weight keys should exist."""
        # All-additive composite, no abstaining strategies.
        # Use a large gap_pct so the gap strategy doesn't abstain.
        composite = CompositeStrategy(
            sub_strategies={
                "momentum": MomentumStrategy(),
                "mean_reversion": MeanReversionStrategy(),
            },
            weights={
                "momentum": 0.55,
                "mean_reversion": 0.45,
            },
        )
        snapshot = _make_snapshot()
        signal = composite.compute_signal(snapshot)

        # Neither strategy abstains → no renormalization → no effective weights
        assert not signal.metadata.get("renormalized"), (
            "Should not renormalize when no strategy abstains"
        )
        effective_keys = [k for k in signal.metadata if k.endswith("_effective_weight")]
        assert not effective_keys, (
            f"Should have no effective_weight keys without renormalization, got: {effective_keys}"
        )

    def test_configured_weights_always_present(self) -> None:
        """Configured weights (non-effective) should always be in metadata."""
        composite = _make_composite_with_event_timing()
        snapshot = _make_snapshot()
        signal = composite.compute_signal(snapshot)

        for name in ("momentum", "mean_reversion", "gap", "event_window_timing"):
            assert f"{name}_weight" in signal.metadata, (
                f"Configured weight {name}_weight should always be in metadata"
            )
