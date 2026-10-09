"""The stop's cap, in the data and in the risk check.

Found by the evolution agent's code review. The constitution caps a stop's
distance from the entry (``max_stop_loss_pct``), and only the risk manager's
reading of the constitution enforced it: ``check_risk_limits`` took no stop and
said APPROVED, with no violations, to proposals the same review then refused on
the stop. Nothing in the market data told the strategy agent what the cap is in
ATRs for a volatile instrument, so it proposed an ATR-sized stop beyond the cap,
was refused, and concluded the instrument could not be traded at all.

``stop_geometry`` puts the cap beside the ATR stop in both market-data tools;
``check_risk_limits`` checks a ``stop_price`` against the cap in code.

Made-up prices.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from evotrader.agents import tools
from evotrader.config import AppConfig
from evotrader.tools.stop_geometry import stop_cap_check, stop_geometry


class TestTheGeometry:
    def test_a_volatile_fund_the_cap_binds(self) -> None:
        """ATR 18% of the price: a 1.5-ATR stop is 27% away, past a 20% cap."""
        g = stop_geometry(10.0, 1.8, 0.20, 1.5)
        assert g["atr_pct"] == 18.0
        assert g["max_stop_loss_pct"] == 20.0
        assert g["cap_stop_atr"] == pytest.approx(1.11)
        assert g["cap_binding"] is True
        assert g["stop_at_cap_long"] == 8.0
        assert g["stop_at_atr_long"] == 7.3

    def test_a_quiet_stock_the_cap_is_far(self) -> None:
        g = stop_geometry(100.0, 6.0, 0.20, 1.5)
        assert (g["atr_pct"], g["cap_stop_atr"], g["cap_binding"]) == (6.0, 3.33, False)
        assert (g["stop_at_cap_long"], g["stop_at_atr_long"]) == (80.0, 91.0)
        assert (g["stop_at_cap_short"], g["stop_at_atr_short"]) == (120.0, 109.0)

    def test_the_stop_at_the_cap_is_rounded_inside_it(self) -> None:
        """9.064 rounded to 9.06 would be 20.04% away: a long's stop rounds up."""
        g = stop_geometry(11.33, 2.0, 0.20, 1.5)
        assert g["stop_at_cap_long"] == 9.07
        assert stop_cap_check(11.33, g["stop_at_cap_long"], "LONG", 0.20)[0] is None

    def test_a_stop_exactly_at_the_cap_passes(self) -> None:
        """12.35 x 0.8 is 9.880000000000001 in floating point."""
        assert stop_geometry(12.35, 2.0, 0.20)["stop_at_cap_long"] == 9.88
        assert stop_cap_check(12.35, 9.88, "LONG", 0.20)[0] is None

    def test_without_the_atr_multiple_the_cap_alone(self) -> None:
        g = stop_geometry(10.0, 1.8, 0.20)
        assert "cap_binding" not in g and g["cap_stop_atr"] == pytest.approx(1.11)

    @pytest.mark.parametrize(
        ("last", "atr", "cap"), [(None, 1.0, 0.2), (10.0, 0.0, 0.2), (10.0, 1.0, None)]
    )
    def test_nothing_to_measure(self, last, atr, cap) -> None:
        assert stop_geometry(last, atr, cap, 1.5) is None


class TestTheCheck:
    def test_a_stop_past_the_cap(self) -> None:
        problem, context = stop_cap_check(12.5, 9.0, "LONG", 0.20)
        assert "28.0% from the entry price 12.5" in problem
        assert "at or above 10.00" in problem
        assert context["stop_distance_pct"] == pytest.approx(28.0)
        assert context["stop_at_cap"] == 10.0

    def test_a_stop_inside_the_cap(self) -> None:
        problem, context = stop_cap_check(12.5, 10.25, "LONG", 0.20)
        assert problem is None
        assert context["stop_distance_pct"] == pytest.approx(18.0)

    def test_a_stop_on_the_wrong_side_protects_nothing(self) -> None:
        assert "does not protect a long" in stop_cap_check(10.0, 10.5, "LONG", 0.2)[0]

    def test_a_short_mirrors(self) -> None:
        assert stop_cap_check(100.0, 125.0, "SHORT", 0.2)[0].endswith("at or below 120.00")
        assert stop_cap_check(100.0, 118.0, "SHORT", 0.2)[0] is None


@pytest.fixture
def quiet_day(monkeypatch):
    """Starter constitution with a 20% stop cap, XYZ allowed, no losses today."""
    from evotrader.tools import market_hours

    config = AppConfig()
    config.constitution.risk_limits.max_stop_loss_pct = 0.20
    config.constitution.trading_rules.allowed_tickers = ["XYZ"]
    journal = AsyncMock()
    journal.get_today_pnl.return_value = 0.0
    journal.get_session_consecutive_losses.return_value = 0
    journal.get_trade_count_today.return_value = 0
    journal.get_open_trades.return_value = []
    journal.get_last_loss_timestamp.return_value = None
    monkeypatch.setattr(
        tools,
        "_config",
        SimpleNamespace(constitution=config.constitution, settings=config.settings),
    )
    monkeypatch.setattr(tools, "_journal", journal)
    monkeypatch.setattr(tools, "_metrics", None)
    monkeypatch.setattr(
        market_hours, "get_current_session", lambda **_k: market_hours.MarketSession.REGULAR
    )

    async def no_broker(*_a, **_k):
        return None

    monkeypatch.setattr(tools, "_call_mcp_tool", no_broker)
    return config


def _stop_violations(result: dict) -> list[str]:
    return [v for v in result["violations"] if v.startswith("Stop ")]


class TestTheRiskCheckReadsTheStop:
    async def test_an_entry_with_a_stop_past_the_cap_is_refused(self, quiet_day) -> None:
        out = await tools.check_risk_limits("XYZ", "LONG", 20, 12.5, stop_price=9.0)
        assert out["verdict"] == "REJECTED"
        assert _stop_violations(out)
        assert out["context"]["stop"]["stop_at_cap"] == 10.0

    async def test_an_entry_with_a_stop_inside_the_cap_passes_it(self, quiet_day) -> None:
        out = await tools.check_risk_limits("XYZ", "LONG", 20, 12.5, stop_price=10.25)
        assert not _stop_violations(out), out["violations"]
        assert out["context"]["stop"]["stop_distance_pct"] == pytest.approx(18.0)

    async def test_without_a_stop_nothing_changes(self, quiet_day) -> None:
        out = await tools.check_risk_limits("XYZ", "LONG", 20, 12.5)
        assert not _stop_violations(out)
        assert out["context"]["stop"] is None

    @pytest.mark.parametrize("action", ["CLOSE", "STOP_LOSS", "TAKE_PROFIT"])
    async def test_an_exit_is_not_checked(self, quiet_day, action: str) -> None:
        out = await tools.check_risk_limits("XYZ", "LONG", 20, 12.5, action=action, stop_price=5.0)
        assert not _stop_violations(out)


class TestTheMarketDataCarriesIt:
    def test_from_the_constitution_and_the_settings(self, quiet_day) -> None:
        quiet_day.settings.position_sizing.default_stop_loss_atr_multiplier = 1.5
        g = tools._stop_geometry(10.0, 1.8)
        assert (g["max_stop_loss_pct"], g["stop_atr"], g["cap_binding"]) == (20.0, 1.5, True)

    def test_unconfigured_it_is_absent(self, monkeypatch) -> None:
        monkeypatch.setattr(tools, "_config", None)
        assert tools._stop_geometry(10.0, 1.8) is None
