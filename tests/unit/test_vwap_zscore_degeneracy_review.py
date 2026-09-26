"""Regression tests: entry_z/min_std degeneracy and dislocation-depth measurement.

See: data/evolution/reviews/20260909_211917_vwap_zscore_parameter_degeneracy_and_outcome_bucketing.md

Finding 1. On QQQ the dispersion of ATR-normalised deviations (sd_raw ~0.05) sits
well below min_std (0.15), so the floor binds every cycle, `sd` is a constant,
and z reduces to deviation/min_std. The firing test |z| >= entry_z therefore
becomes |deviation_atr| >= entry_z * min_std — a fixed ATR-DISTANCE trigger, not
a statistical-rarity test. Only the PRODUCT of the two parameters is
identifiable while the floor binds, which is why the recorded tuning history of
entry_z (1.5 -> 1.9 -> 1.1) never behaved like z-score tuning.

These tests pin the degeneracy explicitly rather than trying to remove it, so
that if the floor ever stops binding the change is visible instead of silent.
"""

from __future__ import annotations

import math
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


def _snapshot(
    price: float,
    vwap: float,
    atr: float,
    candle_closes: list[float],
    relative_volume: float = 1.0,
) -> MarketSnapshot:
    candles = [
        OHLCV(
            timestamp=datetime(2026, 9, 9, 10, (i * 5) % 60),
            open=c,
            high=c + 0.2,
            low=c - 0.2,
            close=c,
            volume=50000.0,
        )
        for i, c in enumerate(candle_closes)
    ]
    return MarketSnapshot(
        ticker="QQQ",
        timestamp=datetime(2026, 9, 9, 14, 0),
        quote=Quote(
            ticker="QQQ",
            bid=price - 0.01,
            ask=price + 0.01,
            last=price,
            volume=60e6,
            timestamp=datetime(2026, 9, 9, 14, 0),
        ),
        indicators=TechnicalIndicators(
            vwap=vwap,
            vwap_anchor="current_session",
            atr_14=atr,
            relative_volume=relative_volume,
        ),
        regime=RegimeClassification(
            regime=MarketRegime.RANGE_BOUND, confidence=0.7, reasoning="test"
        ),
        recent_candles=candles,
    )


# Live QQQ scale: $716 with ATR $9.32. A tight cluster of closes keeps sd_raw
# far below min_std, reproducing the binding floor.
_VWAP = 716.0
_ATR = 9.32
_TIGHT = [716.0, 716.1, 715.9, 716.05, 715.95, 716.02] * 4


class TestCalibrationIsObservable:
    def test_trigger_atr_equals_entry_z_times_min_std(self) -> None:
        """The degeneracy itself, asserted as an identity."""
        for entry_z, min_std in ((1.1, 0.15), (1.5, 0.15), (1.9, 0.2)):
            strat = IntradayVwapZscoreStrategy(entry_z=entry_z, z_scale=0.5, min_std=min_std)
            snap = _snapshot(_VWAP + 0.30 * _ATR, _VWAP, _ATR, _TIGHT)
            md = strat.compute_signal(snap).metadata
            assert md["sd_floored"] is True, "floor must bind for this fixture"
            assert md["trigger_atr"] == pytest.approx(entry_z * min_std, abs=1e-4)

    def test_active_config_trigger_is_one_sixth_of_an_atr(self) -> None:
        """v025 ships entry_z=1.1, min_std=0.15 -> 0.165 ATR (~$1.54 on QQQ)."""
        strat = IntradayVwapZscoreStrategy(entry_z=1.1, z_scale=0.5, min_std=0.15)
        snap = _snapshot(_VWAP + 0.30 * _ATR, _VWAP, _ATR, _TIGHT)
        md = strat.compute_signal(snap).metadata
        assert md["trigger_atr"] == pytest.approx(0.165, abs=1e-4)
        assert md["trigger_atr"] * _ATR == pytest.approx(1.538, abs=0.01)

    def test_saturation_atr_is_where_magnitude_stops_discriminating(self) -> None:
        """tanh reaches ~0.964 two z_scale units past the trigger."""
        strat = IntradayVwapZscoreStrategy(entry_z=1.1, z_scale=0.5, min_std=0.15)
        snap = _snapshot(_VWAP + 0.30 * _ATR, _VWAP, _ATR, _TIGHT)
        md = strat.compute_signal(snap).metadata
        assert md["saturation_atr"] == pytest.approx(0.315, abs=1e-4)

        at_sat = _snapshot(_VWAP + 0.315 * _ATR, _VWAP, _ATR, _TIGHT)
        raw = abs(strat.compute_signal(at_sat).metadata["raw_signal"])
        assert raw == pytest.approx(math.tanh(2.0), abs=0.02)

    def test_calibration_emitted_when_within_band_too(self) -> None:
        """A non-firing cycle must still report its effective calibration."""
        strat = IntradayVwapZscoreStrategy(entry_z=1.1, z_scale=0.5, min_std=0.15)
        snap = _snapshot(_VWAP + 0.05 * _ATR, _VWAP, _ATR, _TIGHT)
        md = strat.compute_signal(snap).metadata
        assert md["reason"] == "within_band"
        assert md["trigger_atr"] == pytest.approx(0.165, abs=1e-4)
        assert md["saturation_atr"] == pytest.approx(0.315, abs=1e-4)


class TestFiringIsAnAtrDistanceTest:
    def test_fires_just_above_the_atr_trigger_and_not_below(self) -> None:
        """Firing tracks the ATR distance, not any statistical rarity."""
        strat = IntradayVwapZscoreStrategy(
            entry_z=1.1, z_scale=0.5, min_std=0.15, staleness_decay=1.0
        )
        below = _snapshot(_VWAP + 0.16 * _ATR, _VWAP, _ATR, _TIGHT)
        above = _snapshot(_VWAP + 0.20 * _ATR, _VWAP, _ATR, _TIGHT)
        assert strat.compute_signal(below).value == pytest.approx(0.0)
        assert strat.compute_signal(above).value != pytest.approx(0.0)

    def test_the_two_recorded_winners_still_fire(self) -> None:
        """The winners sat at 0.20 and 0.29 ATR — both modest.

        Raising the trigger to "filter noise" would have excluded the two
        largest winners on record. This test exists so that a
        future trigger increase fails loudly rather than silently discarding
        the only firings that ever worked.
        """
        strat = IntradayVwapZscoreStrategy(
            entry_z=1.1, z_scale=0.5, min_std=0.15, staleness_decay=1.0
        )
        for depth in (0.20, 0.29):
            snap = _snapshot(_VWAP - depth * _ATR, _VWAP, _ATR, _TIGHT)
            sig = strat.compute_signal(snap)
            assert sig.value > 0.0, f"{depth} ATR below VWAP must fade long"
            assert abs(sig.metadata["deviation_atr"]) == pytest.approx(depth, abs=0.02)

    def test_equal_products_of_entry_z_and_min_std_fire_identically(self) -> None:
        """Only the product is identifiable while the floor binds.

        entry_z=1.1/min_std=0.15 and entry_z=0.55/min_std=0.30 both give a
        0.165 ATR trigger and must behave the same, which is exactly why tuning
        the two knobs independently is meaningless.
        """
        a = IntradayVwapZscoreStrategy(entry_z=1.1, z_scale=0.5, min_std=0.15, staleness_decay=1.0)
        b = IntradayVwapZscoreStrategy(
            entry_z=0.55, z_scale=0.25, min_std=0.30, staleness_decay=1.0
        )
        for depth in (0.10, 0.166, 0.25, 0.40):
            snap = _snapshot(_VWAP + depth * _ATR, _VWAP, _ATR, _TIGHT)
            va = a.compute_signal(snap)
            vb = b.compute_signal(snap)
            assert va.metadata["trigger_atr"] == pytest.approx(vb.metadata["trigger_atr"], abs=1e-4)
            assert va.value == pytest.approx(vb.value, abs=1e-6), (
                f"same trigger_atr must give the same emission at {depth} ATR"
            )


class TestDislocationDepthIsRecorded:
    """Finding 3. The trigger cannot be calibrated from a z-score whose units
    move with min_std. Outcomes must be keyed on the physical quantity."""

    async def test_attribution_columns_exist(self, db) -> None:
        async with db.connection() as conn:
            cur = await conn.execute("PRAGMA table_info(signal_attribution)")
            cols = {r["name"] for r in await cur.fetchall()}
        assert {"deviation_atr", "trigger_atr"} <= cols

    async def test_depth_reaches_the_scorer(self, db) -> None:
        """The columns must survive the READ path, not just the write path.

        ``scored_rows`` is what feeds ``backtest.attribution``, and it selects
        an explicit column list. Recording depth while omitting it there would
        satisfy every round-trip assertion below and still make the bucketing
        this finding exists to enable impossible — the rows would fill up and
        the scorer would never see them.
        """
        from evotrader.db.signal_attribution import SignalAttributionStore

        store = SignalAttributionStore(db)
        row_id = await store.record(
            "QQQ",
            algo_author="intraday_vwap_zscore",
            algo_direction=-1.0,
            deviation_atr=-0.2512,
            trigger_atr=0.165,
            price_at_decision=716.0,
            traded=True,
        )
        assert row_id is not None
        await store.score(row_id, forward_return_1d=0.021, forward_return_5d=0.207)

        rows = await store.scored_rows()
        assert rows, "scored row should be returned once it has been scored"
        row = next(r for r in rows if r["id"] == row_id)
        assert "deviation_atr" in row, (
            "scored_rows must select deviation_atr or outcomes cannot be "
            "bucketed by dislocation depth"
        )
        assert row["deviation_atr"] == pytest.approx(-0.2512)
        assert row["trigger_atr"] == pytest.approx(0.165)

    async def test_depth_round_trips(self, db) -> None:
        from evotrader.db.signal_attribution import SignalAttributionStore

        store = SignalAttributionStore(db)
        row_id = await store.record(
            "QQQ",
            algo_author="intraday_vwap_zscore",
            algo_direction=1.0,
            deviation_atr=-0.2512,
            trigger_atr=0.165,
            traded=True,
        )
        assert row_id is not None
        async with db.connection() as conn:
            cur = await conn.execute(
                "SELECT deviation_atr, trigger_atr FROM signal_attribution WHERE id = ?",
                (row_id,),
            )
            row = await cur.fetchone()
        assert row["deviation_atr"] == pytest.approx(-0.2512)
        assert row["trigger_atr"] == pytest.approx(0.165)

    async def test_depth_is_null_when_channel_did_not_author(self, db) -> None:
        """NULL must read as 'not applicable', never as a zero dislocation."""
        from evotrader.db.signal_attribution import SignalAttributionStore

        store = SignalAttributionStore(db)
        row_id = await store.record("QQQ", algo_author="momentum", algo_direction=1.0)
        async with db.connection() as conn:
            cur = await conn.execute(
                "SELECT deviation_atr, trigger_atr FROM signal_attribution WHERE id = ?",
                (row_id,),
            )
            row = await cur.fetchone()
        assert row["deviation_atr"] is None
        assert row["trigger_atr"] is None

    async def test_tool_accepts_the_new_fields(self, db) -> None:
        """The allowlist in log_signal_attribution must not silently drop them."""
        import json
        from unittest.mock import patch

        from evotrader.agents import tools as tools_module

        with patch.object(tools_module, "_db", db):
            res = await tools_module.log_signal_attribution(
                json.dumps(
                    {
                        "ticker": "QQQ",
                        "algo_author": "intraday_vwap_zscore",
                        "deviation_atr": -0.2512,
                        "trigger_atr": 0.165,
                        "traded": False,
                    }
                )
            )
        assert res["status"] == "ok"
        async with db.connection() as conn:
            cur = await conn.execute(
                "SELECT deviation_atr, trigger_atr FROM signal_attribution WHERE id = ?",
                (res["id"],),
            )
            row = await cur.fetchone()
        assert row["deviation_atr"] == pytest.approx(-0.2512)
        assert row["trigger_atr"] == pytest.approx(0.165)

    def test_metadata_supplies_exactly_what_attribution_needs(self) -> None:
        """The agent copies these straight across, so the names must match."""
        strat = IntradayVwapZscoreStrategy(
            entry_z=1.1, z_scale=0.5, min_std=0.15, staleness_decay=1.0
        )
        snap = _snapshot(_VWAP - 0.25 * _ATR, _VWAP, _ATR, _TIGHT)
        md = strat.compute_signal(snap).metadata
        assert {"deviation_atr", "trigger_atr"} <= md.keys()
