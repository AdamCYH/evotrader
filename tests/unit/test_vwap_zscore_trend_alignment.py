"""The VWAP fade must know which way the trend is pointing.

See: data/evolution/reviews/20260917_200126_vwap_zscore_unconditional_countertrend_fade.md
(findings 1-5)

``compute_signal`` never read the MA stack. At weight 0.18 in both trending
regimes the channel fired short 3x on 2026-09-17 (MSTR closed +4.49%) and long
once on 09-16 (MSTR closed at the session low): 0-for-4 as composite author in
trending regimes, against 2-for-2 in range_bound.

⚠ SIGN CONVENTION. This channel FADES — it emits OPPOSITE the deviation. Price
above VWAP gives z > 0 and emits a SELL. So in a BULL stack z > 0 is the
COUNTER-trend leg, and z < 0 (a dip being bought) is WITH-trend. Getting it
backwards silently inverts both knobs and amplifies the leg being damped.

Per the standing rule, tests 1, 2, 3 and 6 were verified to FAIL pre-fix — the
precedent being the 2026-09-16 wiring test that passed against the buggy
implementation for the wrong reason.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from evotrader.algorithms.strategies.intraday_vwap_zscore import (
    IntradayVwapZscoreStrategy,
)
from evotrader.models.market import (
    OHLCV,
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)

_ATR = 8.67


def _snapshot(
    price: float,
    vwap: float,
    *,
    stack: str = "bull",
    closes: list[float] | None = None,
    atr: float = _ATR,
) -> MarketSnapshot:
    """A snapshot whose MA stack is explicit and whose VWAP dispersion is tight."""
    if stack == "bull":
        ema_9, ema_21, sma_20, sma_50 = price - 2, price - 4, 120.0, 100.0
    elif stack == "bear":
        ema_9, ema_21, sma_20, sma_50 = price + 2, price + 4, 100.0, 120.0
    else:  # no_stack — MAs interleaved so neither pattern holds
        ema_9, ema_21, sma_20, sma_50 = price + 2, price - 4, 120.0, 100.0

    tight = closes if closes is not None else [vwap + 0.05 * i % 0.2 for i in range(24)]
    candles = [
        OHLCV(
            timestamp=datetime(2026, 9, 17, 10, (i * 5) % 60),
            open=c,
            high=c + 0.2,
            low=c - 0.2,
            close=c,
            volume=50000.0,
        )
        for i, c in enumerate(tight)
    ]
    return MarketSnapshot(
        ticker="MSTR",
        timestamp=datetime(2026, 9, 17, 11, 30),
        quote=Quote(
            ticker="MSTR",
            bid=price - 0.01,
            ask=price + 0.01,
            last=price,
            volume=60e6,
            timestamp=datetime(2026, 9, 17, 11, 30),
        ),
        indicators=TechnicalIndicators(
            vwap=vwap,
            vwap_anchor="current_session",
            atr_14=atr,
            relative_volume=0.77,
            ema_9=ema_9,
            ema_21=ema_21,
            sma_20=sma_20,
            sma_50=sma_50,
        ),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BULL, confidence=0.7, reasoning="test"
        ),
        recent_candles=candles,
    )


def _strategy(**kw) -> IntradayVwapZscoreStrategy:
    """Built with the LIVE v026 calibration, not the class defaults.

    The class defaults are entry_z 1.5 / z_scale 1.0; the deployed config runs
    entry_z 1.1 / z_scale 0.5, which is what every number in the review and in
    these tests refers to. Testing the defaults would measure a channel that is
    not the one trading.
    """
    kw.setdefault("entry_z", 1.1)
    kw.setdefault("z_scale", 0.5)
    kw.setdefault("min_std", 0.15)
    kw.setdefault("staleness_decay", 1.0)
    # The trend-alignment values also live in the v026 CONFIG, not in the code
    # defaults — the code defaults are identity so that rolling back to v025
    # restores the pre-fix channel. Pin them here for the same reason as above:
    # test the channel that trades, not the one the constructor happens to build.
    kw.setdefault("countertrend_dampener", 0.5)
    kw.setdefault("with_trend_trigger_scale", 0.7)
    return IntradayVwapZscoreStrategy(**kw)


class TestCounterTrendIsDampened:
    def test_1_counter_trend_leg_is_dampened_in_a_bull_stack(self) -> None:
        """Price stretched ABOVE VWAP in a bull stack → SELL → counter-trend."""
        snap = _snapshot(130.0 + 0.30 * _ATR, 130.0, stack="bull")
        damped = _strategy(countertrend_dampener=0.5).compute_signal(snap)
        identity = _strategy(countertrend_dampener=1.0).compute_signal(snap)

        assert damped.metadata["leg"] == "counter_trend"
        assert damped.metadata["stack_context"] == "bull"
        assert damped.metadata["countertrend_dampener_applied"] is True
        assert abs(damped.value) < abs(identity.value)
        assert damped.value == pytest.approx(identity.value * 0.5, rel=1e-6)

    def test_2_with_trend_leg_is_not_dampened(self) -> None:
        """A dip BELOW VWAP in a bull stack → BUY → with-trend."""
        snap = _snapshot(130.0 - 0.30 * _ATR, 130.0, stack="bull")
        damped = _strategy(countertrend_dampener=0.5).compute_signal(snap)
        identity = _strategy(countertrend_dampener=1.0).compute_signal(snap)

        assert damped.metadata["leg"] == "with_trend"
        assert damped.metadata["countertrend_dampener_applied"] is False
        assert damped.value == pytest.approx(identity.value, rel=1e-9)
        assert damped.value > 0, "a bought dip in an uptrend must vote LONG"

    def test_6_bear_stack_is_the_exact_mirror(self) -> None:
        """Symmetry, or the knob becomes a directional bias."""
        below = _snapshot(130.0 - 0.30 * _ATR, 130.0, stack="bear")
        above = _snapshot(130.0 + 0.30 * _ATR, 130.0, stack="bear")

        counter = _strategy(countertrend_dampener=0.5).compute_signal(below)
        with_trend = _strategy(countertrend_dampener=0.5).compute_signal(above)

        assert counter.metadata["stack_context"] == "bear"
        assert counter.metadata["leg"] == "counter_trend"
        assert counter.value > 0, "fading a dip emits BUY, which opposes a downtrend"

        assert with_trend.metadata["leg"] == "with_trend"
        assert with_trend.metadata["countertrend_dampener_applied"] is False
        assert with_trend.value < 0, "a faded rally in a downtrend votes SHORT"


class TestTheMissedTradeNowFires:
    def test_3_the_live_2026_09_17_11_30_pullback(self) -> None:
        """THE REGRESSION CASE, from live data.

        MSTR $129.095 against VWAP $130.18, bull stack intact. Pre-fix this was
        `within_band` and emitted 0.0, missing the symmetric 0.165 ATR trigger.
        By 12:30 price was $131.225, back above VWAP; it ran to $132.65 and
        closed $131.85.

        NOTE on the depth: the review cites -0.1277 ATR, but (130.18 - 129.095)
        / 8.67 = -0.1251. The cited figure implies ATR ~8.50 rather than the
        8.67 used here, so the exact ATR of that cycle is not recoverable from
        the review. The prices are the cited ones and the arithmetic below is
        this fixture's own; either value clears the scaled 0.1155 trigger and
        misses the unscaled 0.165 one, which is the behaviour under test.
        """
        snap = _snapshot(129.095, 130.18, stack="bull", atr=_ATR)
        sig = _strategy().compute_signal(snap)

        assert sig.metadata["leg"] == "with_trend"
        assert sig.metadata["deviation_atr"] == pytest.approx(-0.1251, abs=1e-3)
        # Pre-fix bar it missed, post-fix bar it clears.
        assert 0.1155 < abs(sig.metadata["deviation_atr"]) < 0.165
        # 0.165 * 0.7 = 0.1155 ATR, which -0.1277 now clears.
        assert sig.metadata["trigger_atr"] == pytest.approx(0.1155, abs=1e-3)
        assert sig.metadata.get("reason") != "within_band"
        assert sig.value > 0, "the pullback must now vote LONG"

    def test_the_with_trend_trigger_is_scaled_and_saturation_follows_it(self) -> None:
        snap = _snapshot(130.0 - 0.30 * _ATR, 130.0, stack="bull")
        sig = _strategy().compute_signal(snap)
        assert sig.metadata["effective_entry_z"] == pytest.approx(1.1 * 0.7, abs=1e-6)
        # saturation must be recomputed from the EFFECTIVE trigger, not entry_z
        assert sig.metadata["saturation_atr"] == pytest.approx(
            (1.1 * 0.7 + 2 * 0.5) * 0.15, abs=1e-4
        )

    def test_counter_trend_trigger_is_unchanged(self) -> None:
        """Only the actionable leg is lowered. The fade bar does not move."""
        snap = _snapshot(130.0 + 0.30 * _ATR, 130.0, stack="bull")
        sig = _strategy().compute_signal(snap)
        assert sig.metadata["effective_entry_z"] == pytest.approx(1.1, abs=1e-6)
        assert sig.metadata["trigger_atr"] == pytest.approx(0.165, abs=1e-4)


class TestRangeBoundIsUntouched:
    def test_4_no_stack_is_bit_identical_to_the_identity_parameters(self) -> None:
        """range_bound is where the 2-for-2 record lives. It must not move."""
        for depth in (-0.40, -0.25, 0.25, 0.40):
            snap = _snapshot(130.0 + depth * _ATR, 130.0, stack="no_stack")
            live = _strategy().compute_signal(snap)
            identity = _strategy(
                countertrend_dampener=1.0, with_trend_trigger_scale=1.0
            ).compute_signal(snap)
            assert live.metadata["stack_context"] == "no_stack"
            assert live.metadata["leg"] == "no_stack"
            # Full float precision, not approx — this must be untouched.
            assert live.value == identity.value, f"range_bound moved at {depth} ATR"

    def test_5_identity_parameters_reproduce_pre_fix_behaviour(self) -> None:
        """The audit and rollback path: 1.0 / 1.0 is the old channel exactly."""
        for stack in ("bull", "bear", "no_stack"):
            for depth in (-0.40, -0.25, -0.10, 0.10, 0.25, 0.40):
                snap = _snapshot(130.0 + depth * _ATR, 130.0, stack=stack)
                identity = _strategy(
                    countertrend_dampener=1.0, with_trend_trigger_scale=1.0
                ).compute_signal(snap)
                # Pre-fix: symmetric 1.1 trigger, no trend factor anywhere.
                assert identity.metadata["effective_entry_z"] == pytest.approx(1.1)
                if identity.metadata.get("reason") != "within_band":
                    assert identity.metadata["trend_factor"] == pytest.approx(1.0)


class TestTheCalibrationIsObservable:
    def test_both_metadata_blocks_carry_the_leg(self) -> None:
        """Finding 3: countable rather than inferred, on BOTH paths."""
        firing = _snapshot(130.0 + 0.30 * _ATR, 130.0, stack="bull")
        within = _snapshot(130.0 + 0.02 * _ATR, 130.0, stack="bull")
        for snap in (firing, within):
            md = _strategy().compute_signal(snap).metadata
            for key in ("stack_context", "leg", "effective_entry_z"):
                assert key in md, f"{key} missing from {md.get('reason', 'firing')}"

    def test_the_stack_is_read_from_indicators_not_the_regime_classifier(self) -> None:
        """The regime tag says trending_bull; the MAs say otherwise. The MAs win."""
        snap = _snapshot(130.0 + 0.30 * _ATR, 130.0, stack="no_stack")
        assert snap.regime.regime is MarketRegime.TRENDING_BULL
        assert _strategy().compute_signal(snap).metadata["stack_context"] == "no_stack"


class TestRollbackActuallyRollsBack:
    """Behaviour must live in the VERSIONED config, not in code defaults.

    `registry.rollback()` only rewrites `active.yaml` — it is a config pointer
    and touches no Python. So a non-identity CODE default applies to every
    historical version at once: v023's recorded backtest silently stops
    describing what v023 does, and no rollback can undo it.

    Caught on 2026-09-17 in this very change, which originally shipped
    `countertrend_dampener=0.5` as a code default.
    """

    def test_code_defaults_are_identity(self) -> None:
        bare = IntradayVwapZscoreStrategy()
        params = bare.get_parameters()
        assert params["countertrend_dampener"] == 1.0, (
            "a non-identity code default retroactively changes every historical "
            "algorithm version and cannot be rolled back"
        )
        assert params["with_trend_trigger_scale"] == 1.0

    def test_the_shipped_version_names_the_values_explicitly(self, active_algorithm_config) -> None:
        """In the algorithm version a new user starts with."""
        vz = active_algorithm_config["intraday_vwap_zscore"]
        assert vz["countertrend_dampener"] == 0.5
        assert vz["with_trend_trigger_scale"] == 0.7

    # v025's block, written before these two parameters existed.
    _V025_INTRADAY_VWAP_ZSCORE = {
        "zscore_window": 20,
        "entry_z": 1.1,
        "z_scale": 0.5,
        "staleness_decay": 0.92,
        "min_std": 0.15,
        "max_rvol_for_fade": 1.5,
        "high_vol_dampener": 0.4,
    }

    def test_an_older_version_still_gets_the_pre_fix_channel(
        self, starter_data_dir, active_algorithm_config
    ) -> None:
        """The property that makes rollback meaningful."""
        from evotrader.algorithms.loader import StrategyLoader

        loader = StrategyLoader(starter_data_dir / "algorithms" / "strategy_manifest.yaml")
        cfg = {**active_algorithm_config, "intraday_vwap_zscore": self._V025_INTRADAY_VWAP_ZSCORE}
        params = loader.build_composite(cfg)._strategies["intraday_vwap_zscore"].get_parameters()
        assert params["countertrend_dampener"] == 1.0
        assert params["with_trend_trigger_scale"] == 1.0
