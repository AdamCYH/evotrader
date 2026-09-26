"""Regime confidence must discriminate, and its reasoning must be true.

See: data/evolution/reviews/20260916_223008_regime_confidence_saturates_and_bear_reasoning_states_a_false_inequality.md

Three defects, all in the same 35-line block that existed in two near-verbatim
copies (the high-volatility branch and step 3):

1. ``confidence`` had only TWO reachable values on a trending name. The linear
   term ``base_conf * 10.0`` saturates the 0.95 clamp above ~3.5% MA separation;
   MSTR's was 16.7%, five times that. Observed 2026-09-16: 0.95 on seven of
   eight cycles, 0.57 on the eighth — exactly two values, nothing between.
2. The reasoning string re-asserted the SMA comparison even when an override had
   reversed it, producing the live output ``SMA20 126.21 <= SMA50 108.16``,
   which is arithmetically false.
3. The price-location override demanded unanimity across EMA9/EMA21/SMA20, so
   the EMA21 leg alone held a stale bull tag through a -4.25% session while MACD
   ran negative all day. The composite printed LONG on all eight cycles as MSTR
   went 130.02 -> 124.09. Zero for eight on direction.
"""

from __future__ import annotations

from evotrader.indicators.regime import _resolve_structure

# The live 2026-09-16 19:30 payload (MSTR), verbatim from the review.
_LIVE = dict(
    sma_20_val=126.209,
    sma_50_val=108.160,
    trend_slope=6.90,
    price=124.09,
    ema_9_val=130.715,
    ema_21_val=124.755,
    macd_histogram=-1.02,
)

# The seven cycles where the EMA21 leg alone blocked the flip: price below EMA9
# and SMA20, but still above EMA21, so unanimity never fired.
_STALE = dict(
    sma_20_val=126.209,
    sma_50_val=108.160,
    trend_slope=6.90,
    price=125.50,
    ema_9_val=130.715,
    ema_21_val=124.755,
)


class TestConfidenceDiscriminates:
    def test_it_is_not_two_valued_across_realistic_separations(self) -> None:
        """The whole complaint: a number nobody can use is worse than none."""
        values = {
            round(
                _resolve_structure(
                    sma_20_val=100.0 * (1 + sep),
                    sma_50_val=100.0,
                    trend_slope=1.0,
                    price=100.0 * (1 + sep),
                    ema_9_val=0.0,
                    ema_21_val=0.0,
                    macd_histogram=None,
                ).confidence,
                4,
            )
            for sep in (0.005, 0.01, 0.02, 0.035, 0.06, 0.10, 0.167)
        }
        assert len(values) == 7, f"confidence collapsed onto {values}"

    def test_it_is_monotone_in_separation(self) -> None:
        def conf(sep: float) -> float:
            return _resolve_structure(
                sma_20_val=100.0 * (1 + sep),
                sma_50_val=100.0,
                trend_slope=1.0,
                price=100.0 * (1 + sep),
                ema_9_val=0.0,
                ema_21_val=0.0,
                macd_histogram=None,
            ).confidence

        seps = [0.005, 0.01, 0.02, 0.035, 0.06, 0.10, 0.167]
        vals = [conf(s) for s in seps]
        assert vals == sorted(vals), f"not monotone: {vals}"

    def test_the_clamp_is_no_longer_reached_on_a_trending_name(self) -> None:
        """MSTR's 16.7% separation used to pin the ceiling unconditionally."""
        st = _resolve_structure(
            sma_20_val=126.209,
            sma_50_val=108.160,
            trend_slope=6.90,
            price=200.0,
            ema_9_val=0.0,
            ema_21_val=0.0,
            macd_histogram=None,
        )
        assert st.confidence < 0.95

    def test_a_marginal_call_reads_lower_than_a_decisive_one(self) -> None:
        def conf(sep: float) -> float:
            return _resolve_structure(
                sma_20_val=100.0 * (1 + sep),
                sma_50_val=100.0,
                trend_slope=1.0,
                price=100.0 * (1 + sep),
                ema_9_val=0.0,
                ema_21_val=0.0,
                macd_histogram=None,
            ).confidence

        assert conf(0.002) < conf(0.10)

    def test_this_is_a_resolution_change_not_a_threshold_change(self) -> None:
        """It must not gate anything — confidence stays a valid probability."""
        for sep in (0.0, 0.001, 0.05, 0.5):
            st = _resolve_structure(
                sma_20_val=100.0 * (1 + sep),
                sma_50_val=100.0,
                trend_slope=1.0,
                price=100.0 * (1 + sep),
                ema_9_val=0.0,
                ema_21_val=0.0,
                macd_histogram=None,
            )
            assert 0.0 <= st.confidence <= 1.0


class TestTheReasoningAgreesWithTheArithmetic:
    def test_the_live_false_inequality_cannot_recur(self) -> None:
        """'SMA20 126.21 <= SMA50 108.16' was printed verbatim on 2026-09-16."""
        st = _resolve_structure(**_LIVE)
        assert st.bull is False, "the classification itself was correct"
        assert "<=" not in st.basis, f"still asserting a false inequality: {st.basis}"
        assert "126.21" in st.basis and "overrides stale bullish cross" in st.basis

    def test_an_unoverridden_verdict_still_cites_the_cross(self) -> None:
        st = _resolve_structure(
            sma_20_val=110.0,
            sma_50_val=100.0,
            trend_slope=1.0,
            price=115.0,
            ema_9_val=0.0,
            ema_21_val=0.0,
            macd_histogram=None,
        )
        assert st.bull is True
        assert st.basis == "SMA20 110.00 > SMA50 100.00"

    def test_a_slope_override_says_so(self) -> None:
        st = _resolve_structure(
            sma_20_val=110.0,
            sma_50_val=100.0,
            trend_slope=-5.0,
            price=105.0,
            ema_9_val=0.0,
            ema_21_val=0.0,
            macd_histogram=None,
        )
        assert st.bull is False
        assert "slope" in st.basis and "rolling over" in st.basis


class TestStaleTagsCanBeDropped:
    def test_two_of_three_with_macd_confirmation_flips_a_stale_bull(self) -> None:
        """The exact shape that held a bull tag through a -4.25% session."""
        assert _resolve_structure(**_STALE, macd_histogram=None).bull is True
        st = _resolve_structure(**_STALE, macd_histogram=-0.64)
        assert st.bull is False
        assert "2/3 fast MAs" in st.basis and "-0.64" in st.basis

    def test_macd_must_actually_confirm(self) -> None:
        """A majority alone is not enough — confirmation is the whole point."""
        st = _resolve_structure(**_STALE, macd_histogram=+0.64)
        assert st.bull is True, "flipped without confirmation"

    def test_it_is_symmetric(self) -> None:
        """A labelling correction, not a directional lean."""
        mirror = dict(
            sma_20_val=108.160,
            sma_50_val=126.209,
            trend_slope=-6.90,
            price=109.00,
            ema_9_val=104.0,
            ema_21_val=110.0,
        )
        assert _resolve_structure(**mirror, macd_histogram=None).bull is False
        st = _resolve_structure(**mirror, macd_histogram=+0.64)
        assert st.bull is True
        assert "2/3 fast MAs" in st.basis

    def test_unanimity_still_wins_outright(self) -> None:
        st = _resolve_structure(**_LIVE)
        assert "3 fast MAs" not in st.basis, "unanimous path must take precedence"


class TestBothRegimePathsShareOneImplementation:
    """Finding 4. The duplicate had already drifted; a fix applied to one copy
    would silently miss the other."""

    def test_regime_module_has_no_second_confidence_formula(self) -> None:
        import inspect

        import evotrader.indicators.regime as mod

        src = inspect.getsource(mod)
        assert src.count("0.30 * math.tanh") == 1, "confidence formula duplicated"
        assert "base_conf * 10.0" not in src, "the saturating formula survives somewhere"

    def test_both_branches_call_the_helper(self) -> None:
        import inspect

        import evotrader.indicators.regime as mod

        src = inspect.getsource(mod.detect_regime_rule_based)
        assert src.count("_resolve_structure(") == 2
