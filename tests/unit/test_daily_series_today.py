"""The market-data tool ends the daily series with today's bar.

Found 2026-10-02 by the evolution agent's code review: the broker's daily list
ends at the previous session all day long, and the live price was written over
that last bar, so yesterday's close was missing from every daily indicator.
Today's bar is now appended (the rule itself: test_live_indicators.py). Here,
the whole tool: before the open, after the close and on a weekend. Made-up
prices; 2026-03-04 is a Wednesday, before daylight saving time.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

import pytest

from evotrader.agents.tools import gather_market_data

YESTERDAY = date(2026, 3, 3)
DAILY = [
    {
        "begins_at": f"{(YESTERDAY - timedelta(days=64 - i)).isoformat()}T00:00:00Z",
        "open_price": str(100 + i * 0.1),
        "high_price": str(102 + i * 0.1),
        "low_price": str(98 + i * 0.1),
        "close_price": str(101 + i * 0.1),
        "volume": "1000000",
    }
    for i in range(65)
]
PREVIOUS_CLOSE = 101 + 64 * 0.1  # 107.4
SESSION = [  # today's 5-minute bars, from 09:30 ET
    {
        "begins_at": f"2026-03-04T14:{30 + 5 * k}:00Z",
        "open_price": str(108 + k),
        "high_price": str(109 + k),
        "low_price": str(107 + k),
        "close_price": str(108.5 + k),
        "volume": "20000",
    }
    for k in range(4)
]


def _broker(last: float, calls: list[dict]):
    quote = {
        "data": {
            "results": [
                {
                    "quote": {
                        "bid_price": str(last - 0.01),
                        "ask_price": str(last + 0.01),
                        "last_trade_price": str(last),
                        "previous_close": str(PREVIOUS_CLOSE),
                        "volume": "1000000",
                    }
                }
            ]
        }
    }

    async def call(tool_name, arguments):
        calls.append({"tool": tool_name, **arguments})
        if tool_name == "get_equity_quotes":
            return quote
        if tool_name == "get_equity_historicals":
            bars = SESSION if arguments.get("interval") == "5minute" else DAILY
            return {"data": {"results": [{"bars": bars}]}}
        return None

    return call


async def _gather(monkeypatch, moment: str, last: float) -> tuple[dict, list[dict]]:
    monkeypatch.setenv("EVOTRADER_MOCK_TIME", moment)
    calls: list[dict] = []
    with patch("evotrader.agents.tools._call_mcp_tool", side_effect=_broker(last, calls)):
        result = await gather_market_data("T")
    return result, calls


async def test_after_the_close_todays_bar_has_the_sessions_range(monkeypatch) -> None:
    result, _ = await _gather(monkeypatch, "2026-03-04T17:00:00-05:00", 115.0)

    indicators = result["indicators"]
    assert indicators["daily_bar_mode"] == "forming_bar_appended"
    assert indicators["daily_last_completed_date"] == "2026-03-03"
    assert indicators["vwap_anchor"] == "prior_session", (
        "intraday readings stay off after the close"
    )
    # Today's open is the session's first bar, read from today's daily bar.
    assert result["gap_source"] == "daily_bar"
    assert result["gap_pct"] == pytest.approx(
        (108 - PREVIOUS_CLOSE) / PREVIOUS_CLOSE * 100, abs=1e-4
    )


async def test_before_the_open_today_is_the_quote_alone(monkeypatch) -> None:
    result, calls = await _gather(monkeypatch, "2026-03-04T08:30:00-05:00", 109.0)

    indicators = result["indicators"]
    assert indicators["daily_bar_mode"] == "forming_bar_appended"
    assert indicators["ibs"] is None and indicators["ibs_source"] == "forming_bar_no_range"
    assert result["gap_pct"] is None, "a pre-market print is not the session's open"
    assert not any(c.get("interval") == "5minute" for c in calls), "no session bars yet"


async def test_a_weekend_reads_the_last_session_as_it_was(monkeypatch) -> None:
    result, _ = await _gather(monkeypatch, "2026-03-07T10:00:00-05:00", 108.0)

    assert result["indicators"]["daily_bar_mode"] == "no_session_today"
