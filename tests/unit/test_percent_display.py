"""Regression tests: headline percent fields ship with a pre-formatted display string.

2026-09-24 13:30 ET (session 746c55b9): the snapshot said
``daily_change_pct: -0.0401`` — percent units, so -0.04% (162.135 against a
previous close of 162.20) — and the orchestrator's cycle summary printed
"Day Chg: -4.01%". The strategy agent got the JSON intact and decided correctly,
but the digest and the console carried a phantom -4% day, and every later reader
had to re-derive the number from the quote before trusting it.

A tool-shape fix, not an instruction change: a model that copies a string
cannot rescale it.

See: data/evolution/reviews/20260924_224217_mean_reversion_bull_stack_guard_unreachable_strategy_stage_503_skip.md
(finding 3)
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

import pytest

from evotrader.agents.tools import _pct_display, gather_market_data


class TestDisplayString:
    def test_the_live_misread_value(self) -> None:
        assert _pct_display(-0.0401) == "-0.04%"

    def test_the_gap_that_was_read_correctly(self) -> None:
        assert _pct_display(-0.857) == "-0.86%"

    def test_signed_and_missing(self) -> None:
        assert _pct_display(0.5949) == "+0.59%"
        assert _pct_display(16.02) == "+16.02%"
        assert _pct_display(None) is None
        assert _pct_display("n/a") is None


async def test_gather_market_data_ships_both_strings() -> None:
    quote = {
        "data": {
            "results": [
                {
                    "quote": {
                        "bid_price": "162.10",
                        "ask_price": "162.20",
                        "last_trade_price": "162.135",
                        "previous_close": "162.20",
                        "volume": "5000000",
                        "updated_at": "2026-09-24T17:30:00Z",
                    }
                }
            ]
        }
    }
    now = datetime.datetime.now(datetime.UTC)
    bars = [
        {
            "begins_at": (now - datetime.timedelta(days=70 - i)).isoformat(),
            "open_price": str(140 + i * 0.3),
            "high_price": str(142 + i * 0.3),
            "low_price": str(138 + i * 0.3),
            "close_price": str(141 + i * 0.3),
            "volume": "1000000",
        }
        for i in range(65)
    ]

    async def mcp(tool_name, arguments):
        if tool_name == "get_equity_quotes":
            return quote
        if tool_name == "get_equity_historicals":
            return {"data": {"results": [{"bars": bars}]}}
        return None

    with patch("evotrader.agents.tools._call_mcp_tool", side_effect=mcp):
        result = await gather_market_data("MSTR")

    assert result["daily_change_pct"] is not None
    assert result["daily_change_display"] == _pct_display(result["daily_change_pct"])
    assert "gap_display" in result
    assert result["gap_display"] == _pct_display(result["gap_pct"])
    assert result["daily_change_display"].endswith("%")
    assert result["daily_change_pct"] == pytest.approx(
        float(result["daily_change_display"].rstrip("%")), abs=0.005
    ), "the string is the same number in the same units, not a rescaling"
