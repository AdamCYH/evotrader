"""The volatility target must fit the instrument actually being traded.

Operator decision, 2026-09-18 ("use your best judgement to select an option"):
raise `position_sizing.volatility_target_annual` from 0.15 to 0.40, and make the
regime band govern sizing with the risk budget as a CEILING, not a multiplier.

Why. 0.15 was calibrated for QQQ, which swings ~20% a year: 0.15 / 0.20 = 0.98x,
fully invested. MSTR swings ~84%: 0.15 / 0.84 = 0.18x, and only the 0.30 floor
kept the system in the market at all. The setting never changed; the instrument
did. With the floor binding on 24 of 24 cycles the "risk budget" was a constant
in a measurement's clothing, and v017's `risk_budget x conviction` then capped a
0.5-conviction trade at 15% of buying power — below the regime band's own 30%
floor. The operator does not want 85% of the account idle by accident.

At 0.40 the arithmetic gives ~0.48x on MSTR at today's volatility, so the dial
works again (smaller when wild, larger when calm) and a 5% daily loss — the
constitution's halt — needs a ~10% MSTR move rather than an ordinary one.

Operator decision, 2026-09-25 ("relax a bit so it has more power to buy or
sell"): 0.40 -> 0.60. MSTR's volatility had risen to ~95%, shrinking 0.40 to
0.42x, and whole-share sizing left under one share of room, so every add from
09-21 to 09-24 died. 0.60 gives ~0.63x at 95%. The cost, stated here so it is
not rediscovered: a 5% account day now takes ~1.3 one-sigma MSTR days, not ~2.
The daily-loss limit was fixed the same day (tests/unit/test_daily_loss_limit.py)
so that brake actually exists behind the larger size.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_TARGET = 0.60
_FLOOR = 0.30
_CEILING_PCT = 100  # the constitution's max_total_exposure_pct at the time


def _snapshot(daily_pct_moves: list[float]):
    from evotrader.models.market import (
        OHLCV,
        MarketRegime,
        MarketSnapshot,
        Quote,
        RegimeClassification,
        TechnicalIndicators,
    )

    now = datetime(2026, 9, 18, 14, 30, tzinfo=UTC)
    price, candles = 100.0, []
    for i, move in enumerate(daily_pct_moves):
        price *= 1 + move / 100.0
        candles.append(
            OHLCV(
                timestamp=now - timedelta(days=len(daily_pct_moves) - i),
                open=price,
                high=price * 1.01,
                low=price * 0.99,
                close=price,
                volume=1e6,
            )
        )
    return MarketSnapshot(
        ticker="MSTR",
        timestamp=now,
        quote=Quote(
            ticker="MSTR", bid=price - 0.01, ask=price + 0.01, last=price, volume=1e6, timestamp=now
        ),
        indicators=TechnicalIndicators(atr_14=price * 0.065),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BULL, confidence=0.7, reasoning="t"
        ),
        daily_candles=candles,
    )


# Daily moves whose standard deviation annualises to roughly MSTR's ~84%.
_MSTR_LIKE = [5.5, -6.0, 4.5, -5.0] * 10 + [5.5]
_CALM = [0.25, -0.2, 0.3, -0.15] * 10 + [0.25]
_CHAOS = [15.0, -14.0, 13.0, -16.0] * 10 + [15.0]


@pytest.fixture
def sizing_config(update_data_yaml, monkeypatch):
    """The tool must read the configured value, not a code default.

    So the sizing this regression is about goes where the operator writes it —
    the 0.60 target into settings.yaml, the 100% exposure ceiling into
    constitution.yaml — and is loaded the way the app loads it.
    """
    import evotrader.agents.tools as tools
    from evotrader.config import AppConfig

    update_data_yaml("settings.yaml", {"position_sizing": {"volatility_target_annual": _TARGET}})
    update_data_yaml("constitution.yaml", {"risk_limits": {"max_total_exposure_pct": _CEILING_PCT}})
    cfg = AppConfig()
    monkeypatch.setattr(tools, "_config", cfg)
    return cfg


class TestTheFloorNoLongerBindsOnTheTradedInstrument:
    async def test_mstr_volatility_yields_a_measured_budget(self, sizing_config) -> None:
        """THE REGRESSION CASE. At 0.15 this read 0.30 / floor_binding=True on
        every cycle; the number carried no information."""
        import evotrader.agents.tools as tools

        out = await tools.compute_risk_budget(
            market_snapshot_json=_snapshot(_MSTR_LIKE).model_dump_json()
        )
        rv = out["realized_volatility"]
        assert 0.70 < rv < 1.00, f"fixture should look like MSTR, got {rv:.0%} annualised"
        assert out["floor_binding"] is False, out
        assert out["raw_exposure"] == pytest.approx(_TARGET / rv, rel=1e-3)
        assert _FLOOR < out["risk_budget"] < 1.0
        assert 0.60 < out["risk_budget"] < 0.80, (
            "expected roughly two-thirds of the account at MSTR's volatility"
        )

    async def test_the_dial_still_moves_with_volatility(self, sizing_config) -> None:
        """The point of unbinding the floor: bigger when calm, smaller when wild."""
        import evotrader.agents.tools as tools

        calm = await tools.compute_risk_budget(
            market_snapshot_json=_snapshot(_CALM).model_dump_json()
        )
        mid = await tools.compute_risk_budget(
            market_snapshot_json=_snapshot(_MSTR_LIKE).model_dump_json()
        )
        wild = await tools.compute_risk_budget(
            market_snapshot_json=_snapshot(_CHAOS).model_dump_json()
        )
        assert wild["risk_budget"] < mid["risk_budget"] < calm["risk_budget"]
        assert calm["risk_budget"] == pytest.approx(1.0), (
            "calm markets hit the 100% constitution ceiling"
        )

    async def test_the_floor_still_catches_genuine_chaos(self, sizing_config) -> None:
        """Raising the target must not remove the floor's job: at ~240% vol the
        budget is the floor, and the tool says so."""
        import evotrader.agents.tools as tools

        out = await tools.compute_risk_budget(
            market_snapshot_json=_snapshot(_CHAOS).model_dump_json()
        )
        assert out["floor_binding"] is True
        assert out["risk_budget"] == pytest.approx(_FLOOR)
        assert "floor_note" in out

    def test_the_arithmetic_the_decision_rests_on(self) -> None:
        """Documented so the next reader does not have to re-derive it."""
        qqq_vol, mstr_vol_0918, mstr_vol_0925 = 0.20, 0.84, 0.953
        assert 0.15 / qqq_vol == pytest.approx(0.75, abs=0.25), (
            "old target was ~fully invested on QQQ"
        )
        assert 0.15 / mstr_vol_0918 < _FLOOR, "old target on MSTR fell under the floor"
        # 09-18: 0.40 gave ~0.48x; by 09-25 the same 0.40 gave 0.42x.
        assert 0.40 / mstr_vol_0918 == pytest.approx(0.48, abs=0.01)
        assert 0.40 / mstr_vol_0925 == pytest.approx(0.42, abs=0.01)
        # 09-25: 0.60 gives ~0.63x, inside the working band, under the ceiling.
        assert _TARGET / mstr_vol_0925 == pytest.approx(0.63, abs=0.01)
        assert _FLOOR < _TARGET / mstr_vol_0918 < 1.0
        # The price of the extra room: a 5% account day now takes ~1.3 one-sigma
        # MSTR days (roughly 1 trading day in 10), where 0.40 needed ~2.
        exposure = _TARGET / mstr_vol_0925
        one_sigma_day = mstr_vol_0925 / math.sqrt(252)
        assert 1.2 < (0.05 / exposure) / one_sigma_day < 1.5


class TestTheInstructionsStopContradictingThemselves:
    """v017 carried three sizing formulas that disagreed with each other.

    The fix states one procedure: the regime band governs and the risk budget is
    a ceiling on it. The strategy instructions a new user starts from must say
    it the same way.
    """

    @staticmethod
    def _active_strategy_instructions(data_dir: Path) -> str:
        folder = data_dir / "instructions" / "strategy"
        active = (folder / "active.txt").read_text().strip()
        return (folder / f"{active}.md").read_text()

    def test_the_active_version_has_no_multiplier_formula(self, starter_data_dir) -> None:
        text = self._active_strategy_instructions(starter_data_dir)
        assert "risk_budget × direction × conviction" not in text, (
            "the active version reintroduced the multiplier formula"
        )
        assert "risk_budget ÷ (1.5 × ATR)" not in text, "the third, dimensionally-wrong formula"

    def test_the_active_version_states_the_precedence(self, starter_data_dir) -> None:
        text = self._active_strategy_instructions(starter_data_dir)
        assert "ceiling" in text.lower()
        assert "floor_binding" in text
        assert "{{DIRECTIONS_AVAILABLE}}" in text, "placeholders must survive the edit"
