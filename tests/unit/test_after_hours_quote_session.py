"""A quote says which session its price is from, and the day stays the session's.

Found by the evolution agent's code review. After the close the quote's
``last`` prefers an after-hours trade when the book is wide (right for
pricing), but it kept the close's timestamp and nothing said which session
the price was from, and the day's change was worked out from it: a small fall
in the session and a larger one after the close read as one large fall. The
agents, momentum and the regime's intraday override all read that as the
session. Now:

* the quote carries ``price_session`` (regular, pre_market, after_hours,
  regular_close, ...), ``last_regular`` and ``last_extended``;
* the market data adds ``regular_change_pct`` and ``extended_change_pct``
  beside the unchanged ``daily_change_pct``;
* under an algorithm version that sets
  ``composite.day_change_regular_after_hours``, after the close momentum and
  the regime's override read the regular session's change.

Made-up ticker and prices.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evotrader.agents import tools
from evotrader.algorithms.strategies.momentum import MomentumStrategy
from evotrader.models.market import (
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)
from evotrader.models.mcp import McpEquityQuote

# 2026-03-05, US Eastern standard time.
REGULAR = datetime(2026, 3, 5, 18, 0, tzinfo=UTC)  # 13:00 ET
EVENING = datetime(2026, 3, 5, 22, 0, tzinfo=UTC)  # 17:00 ET
MORNING = datetime(2026, 3, 6, 13, 30, tzinfo=UTC)  # 08:30 ET


def _quote(**fields) -> McpEquityQuote:
    return McpEquityQuote(**{"last_trade_price": 50.0, **fields})


class TestPriceSession:
    def test_in_regular_hours(self) -> None:
        q = _quote(bid_price=49.99, ask_price=50.01)
        assert tools._price_session(q, q.resolve_live_price(), REGULAR) == "regular"

    def test_an_after_hours_trade(self) -> None:
        q = _quote(bid_price=47.0, ask_price=50.5, last_non_reg_trade_price=48.5)
        live = q.resolve_live_price()
        assert live == 48.5
        assert tools._price_session(q, live, EVENING) == "after_hours"

    def test_the_close_when_nothing_traded_after_it(self) -> None:
        q = _quote(bid_price=47.0, ask_price=50.5)
        assert tools._price_session(q, q.resolve_live_price(), EVENING) == "regular_close"

    def test_a_live_after_hours_book(self) -> None:
        q = _quote(bid_price=48.50, ask_price=48.52)
        assert tools._price_session(q, q.resolve_live_price(), EVENING) == "after_hours"

    def test_before_the_open(self) -> None:
        q = _quote(bid_price=45.0, ask_price=50.0, last_non_reg_trade_price=47.0)
        assert tools._price_session(q, q.resolve_live_price(), MORNING) == "pre_market"

    async def test_the_quote_carries_both_trades(self, monkeypatch) -> None:
        async def call_mcp(tool: str, args: dict):
            return {
                "data": {
                    "results": [
                        {
                            "quote": {
                                "last_trade_price": "50.000000",
                                "venue_last_trade_time": "2026-03-05T20:59:59Z",
                                "last_non_reg_trade_price": "48.500000",
                                "venue_last_non_reg_trade_time": "2026-03-05T21:55:00Z",
                                "previous_close": "51.000000",
                                "bid_price": "47.000000",
                                "ask_price": "50.500000",
                            }
                        }
                    ]
                }
            }

        monkeypatch.setattr(tools, "_call_mcp_tool", call_mcp)
        quote, errors = await tools._fetch_quote("XYZ", EVENING)
        assert errors == []
        assert (quote["last"], quote["last_regular"], quote["last_extended"]) == (48.5, 50.0, 48.5)
        assert quote["price_session"] == "after_hours"
        assert quote["last_extended_time"] == "2026-03-05T21:55:00Z"
        assert quote["timestamp"] == "2026-03-05T20:59:59Z", "the close's stamp, as before"


class TestTheChanges:
    QUOTE = {
        "last": 48.5,
        "last_regular": 50.0,
        "previous_close": 51.0,
        "price_session": "after_hours",
    }

    def test_after_hours_both_moves_are_said(self) -> None:
        out = tools._session_changes(self.QUOTE, regular_day_after_hours=False)
        assert out["regular_change_pct"] == pytest.approx(-1.9608, abs=1e-4)
        assert out["extended_change_pct"] == pytest.approx(-3.0, abs=1e-4)
        assert out["session_day_change_pct"] is None, "off unless the version says so"

    def test_the_version_switch_makes_the_day_the_sessions(self) -> None:
        out = tools._session_changes(self.QUOTE, regular_day_after_hours=True)
        assert out["session_day_change_pct"] == out["regular_change_pct"]

    @pytest.mark.parametrize("session", ["regular", "regular_close", "pre_market"])
    def test_only_after_the_close(self, session) -> None:
        out = tools._session_changes(
            {**self.QUOTE, "price_session": session}, regular_day_after_hours=True
        )
        assert out["session_day_change_pct"] is None
        assert (out["extended_change_pct"] is None) == (session != "pre_market")

    def test_a_quote_with_nothing_to_compare(self) -> None:
        out = tools._session_changes({"last": 48.5}, regular_day_after_hours=True)
        assert out == {
            "price_session": None,
            "regular_change_pct": None,
            "extended_change_pct": None,
            "session_day_change_pct": None,
        }

    def test_the_market_data_tool_reads_the_active_version(self, monkeypatch) -> None:
        class Registry:
            def load_parameters(self) -> dict:
                return {"composite": {"day_change_regular_after_hours": True}}

        monkeypatch.setattr(tools, "_algo_registry", Registry())
        assert tools._composite_setting("day_change_regular_after_hours") is True
        monkeypatch.setattr(tools, "_algo_registry", None)
        assert tools._composite_setting("day_change_regular_after_hours") is False


def _snapshot(session_day_change_pct: float | None) -> MarketSnapshot:
    return MarketSnapshot(
        ticker="XYZ",
        timestamp=EVENING,
        quote=Quote(
            ticker="XYZ",
            bid=47.0,
            ask=50.5,
            last=48.5,
            volume=0.0,
            timestamp=EVENING,
            previous_close=51.0,
        ),
        indicators=TechnicalIndicators(
            ema_9=55.0, ema_21=52.5, sma_20=52.0, sma_50=50.5, macd_histogram=0.25, atr_14=2.0
        ),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BULL, confidence=0.6, reasoning="fixture"
        ),
        daily_change_pct=-4.902,
        session_day_change_pct=session_day_change_pct,
    )


class TestMomentum:
    def test_it_reads_the_sessions_day_when_set(self) -> None:
        sig = MomentumStrategy().compute_signal(_snapshot(-1.961))
        assert sig.metadata["day_change_pct"] == -1.961
        assert sig.metadata["day_change_source"] == "regular_session"

    def test_otherwise_the_quotes_day(self) -> None:
        sig = MomentumStrategy().compute_signal(_snapshot(None))
        assert sig.metadata["day_change_pct"] == -4.902
        assert sig.metadata["day_change_source"] == "quote"

    def test_the_snapshot_keeps_it_through_the_normaliser(self) -> None:
        raw = {
            "ticker": "XYZ",
            "timestamp": EVENING.isoformat(),
            "quote": {"last": 48.5, "bid": 47.0, "ask": 50.5},
            "indicators": {},
            "regime": {"regime": "range_bound", "confidence": 0.4},
            "daily_change_pct": -4.902,
            "session_day_change_pct": -1.961,
        }
        snap = MarketSnapshot.model_validate(tools.normalize_market_snapshot(raw))
        assert snap.session_day_change_pct == -1.961
