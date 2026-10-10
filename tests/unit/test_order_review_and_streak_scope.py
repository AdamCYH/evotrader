"""Two readings the agents misread, made explicit.

Found by the evolution agent's code review.

* A buy limit taken from an earlier quote can sit well under the ask when the
  broker reviews it; the review's empty ``order_checks`` read as clean, and the
  order fills only when the price comes back down. The review now carries
  ``limit_vs_market``: the distance in basis points and whether the order
  would trade at once.
* The account limits' loss streak is today's session only, by design, while
  the performance analysis counts across days; the morning after a losing day
  they disagreed and read like a bug. The limits now name the scope and carry
  the streak without the session boundary beside it.

Made-up prices.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from evotrader.callbacks.account_rails import AccountState, evaluate_account_rails
from evotrader.callbacks.order_review import annotate_review, limit_vs_market
from evotrader.config import AppConfig


def _review(bid: float, ask: float, *, structured: bool = False) -> dict:
    data = {
        "data": {
            "symbol": "XYZ",
            "side": "buy",
            "type": "limit",
            "quantity": "3",
            "limit_price": "99.40",
            "order_checks": {},
            "quote_data": {
                "last_trade_price": f"{(bid + ask) / 2:.4f}",
                "bid_price": f"{bid:.6f}",
                "ask_price": f"{ask:.6f}",
            },
        }
    }
    if structured:
        return {"content": [], "structuredContent": data, "isError": False}
    return {"content": [{"type": "text", "text": json.dumps(data)}], "isError": False}


class TestTheLimitAgainstTheMarket:
    def test_a_buy_under_the_ask_is_not_marketable(self) -> None:
        out = limit_vs_market({"side": "buy", "limit_price": "99.40"}, _review(99.92, 100.0))
        assert (out["reference"], out["reference_price"]) == ("ask", 100.0)
        assert out["bps"] == pytest.approx(-60.0)
        assert out["marketable"] is False
        assert "rests until the price comes down" in out["note"]

    def test_a_buy_at_the_ask_is(self) -> None:
        out = limit_vs_market({"side": "buy", "limit_price": "100.05"}, _review(99.92, 100.0))
        assert out["marketable"] is True and out["bps"] == pytest.approx(5.0)
        assert "note" not in out

    def test_a_sell_is_measured_from_the_bid(self) -> None:
        out = limit_vs_market({"side": "sell", "limit_price": "100.50"}, _review(100.0, 100.08))
        assert (out["reference"], out["bps"], out["marketable"]) == ("bid", 50.0, False)
        assert "comes up" in out["note"]

    def test_the_structured_answer_works_too(self) -> None:
        review = _review(99.92, 100.0, structured=True)
        assert limit_vs_market({"side": "buy", "limit_price": "99.40"}, review)["bps"] == -60.0

    @pytest.mark.parametrize(
        "args",
        [
            {"side": "buy", "type": "market"},
            {"side": "hold", "limit_price": "99"},
        ],
    )
    def test_no_limit_no_distance(self, args) -> None:
        review = _review(99.92, 100.0)
        review_data = json.loads(review["content"][0]["text"])
        review_data["data"].pop("limit_price")
        review["content"][0]["text"] = json.dumps(review_data)
        assert limit_vs_market(args, review) is None

    def test_without_a_quote_nothing_is_added(self) -> None:
        review = {"content": [{"type": "text", "text": json.dumps({"data": {"side": "buy"}})}]}
        assert annotate_review({"side": "buy", "limit_price": "99"}, review) is review

    def test_the_review_keeps_everything_and_gains_the_distance(self) -> None:
        review = _review(99.92, 100.0)
        out = annotate_review({"side": "buy", "limit_price": "99.40"}, review)
        assert out["content"] == review["content"] and out["isError"] is False
        assert out["limit_vs_market"]["marketable"] is False


class TestTheStreakNamesItsScope:
    def test_the_report_carries_both(self) -> None:
        state = AccountState(
            account_value=5000.0,
            value_source="broker",
            peak_value=5000.0,
            consecutive_losses=0,
            consecutive_losses_all_time=3,
            last_loss_at=datetime(2026, 3, 5, 19, 34, tzinfo=UTC),
        )
        report = evaluate_account_rails(state, AppConfig().constitution, is_exit=False).report
        assert report["streak_scope"] == "session"
        assert (report["consecutive_losses"], report["consecutive_losses_all_time"]) == (0, 3)

    async def test_the_state_reads_both_streaks(self, monkeypatch) -> None:
        from evotrader.agents import tools

        journal = AsyncMock()
        journal.get_today_pnl.return_value = 0.0
        journal.get_pnl.return_value = 0.0
        journal.get_session_consecutive_losses.return_value = 0
        journal.get_consecutive_losses.return_value = 3
        journal.get_last_loss_timestamp.return_value = None
        journal.get_trade_count_today.return_value = 0
        monkeypatch.setattr(tools, "_journal", journal)
        monkeypatch.setattr(tools, "_metrics", None)
        state = await tools.account_rails_state(ask_broker=False)
        assert (state.consecutive_losses, state.consecutive_losses_all_time) == (0, 3)
