"""Regression test: the option risk check answers during a losing streak instead of crashing.

Found 2026-09-25 by the linter during the open-source cleanup (ruff F823).
``check_option_risk_limits`` read ``datetime.now(UTC)`` in its consecutive-loss
circuit breaker, but a ``from datetime import UTC, datetime`` further down the
same function made both names local to the whole function. So the moment the
breaker condition held — five losses in a row, the last one inside the pause
window — the check raised ``UnboundLocalError`` instead of returning its verdict.
The equity check imports nothing locally and was never affected.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from evotrader.agents import tools


@pytest.fixture
def losing_streak(monkeypatch, update_data_yaml):
    from evotrader.config import _load_constitution
    from evotrader.tools import market_hours

    journal = AsyncMock()
    journal.get_today_pnl.return_value = 0.0
    journal.get_session_consecutive_losses.return_value = 5
    journal.get_last_loss_timestamp.return_value = datetime.now(UTC) - timedelta(minutes=10)
    journal.get_trade_count_today.return_value = 5
    journal.get_open_trades.return_value = []
    # A constitution that pauses after 5 losses in a row for 60 minutes, as the
    # starter one does, and permits the underlying traded here.
    data_dir = update_data_yaml(
        "constitution.yaml",
        {
            "trading_rules": {"allowed_tickers": ["MSTR"]},
            "circuit_breakers": {"consecutive_losses_pause": 5, "pause_duration_minutes": 60},
        },
    )
    monkeypatch.setattr(
        tools, "_config", SimpleNamespace(constitution=_load_constitution(data_dir))
    )
    monkeypatch.setattr(tools, "_journal", journal)
    monkeypatch.setattr(tools, "_metrics", None)
    monkeypatch.setattr(
        market_hours, "get_current_session", lambda **_k: market_hours.MarketSession.REGULAR
    )

    async def no_broker(*_a, **_k):
        return None

    monkeypatch.setattr(tools, "_call_mcp_tool", no_broker)


async def test_opening_contract_is_refused_not_crashed(losing_streak) -> None:
    out = await tools.check_option_risk_limits(
        "MSTR", "call", "buy", 1, 5.0, 170.0, "2026-12-18", action="OPEN"
    )
    assert out["verdict"] == "REJECTED"
    assert any("Circuit breaker" in v for v in out["violations"]), out["violations"]


async def test_closing_contract_still_passes_the_breaker(losing_streak) -> None:
    out = await tools.check_option_risk_limits(
        "MSTR", "call", "sell", 1, 5.0, 170.0, "2026-12-18", action="CLOSE"
    )
    assert not any("Circuit breaker" in v for v in out["violations"])
