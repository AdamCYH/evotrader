"""The executor can wait for an order to fill.

Found by the evolution agent's code review. The executor's instructions say to
re-check a placed entry every ~15 seconds for up to ~90, but a model's turn
cannot pause: repeated order-status checks land seconds apart and the turn
ends long before a resting limit entry fills, so every such entry ended its
cycle unfilled as far as the executor knew, and its protection waited for
someone else. ``await_order_state`` does the waiting: it checks the broker
every few seconds and returns once the order is filled or has ended, or with
its last state when the time is up, never longer than two minutes.

Made-up order id, account and prices; the broker is a stand-in.
"""

from __future__ import annotations

import pytest

from evotrader.agents import tools


def _order(state: str, **extra) -> dict:
    return {
        "data": {
            "orders": [
                {
                    "id": "0rd3r-0001",
                    "state": state,
                    "quantity": "7.000000",
                    "cumulative_quantity": "7.000000" if state == "filled" else "0.000000",
                    "average_price": "42.170000" if state == "filled" else None,
                    **extra,
                }
            ]
        }
    }


@pytest.fixture
def broker(monkeypatch):
    calls: list[dict] = []
    replies: list = []
    slept: list[float] = []

    async def call_mcp(tool: str, args: dict):
        calls.append({"tool": tool, **args})
        return replies.pop(0) if replies else None

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    async def account():
        return "ACCT1"

    monkeypatch.setattr(tools, "_call_mcp_tool", call_mcp)
    monkeypatch.setattr(tools.asyncio, "sleep", sleep)
    monkeypatch.setattr(tools, "_agentic_account_number", account)
    return calls, replies, slept


class TestAwaitOrderState:
    async def test_it_returns_on_the_fill_with_the_fill_price(self, broker) -> None:
        calls, replies, slept = broker
        replies += [_order("confirmed"), _order("confirmed"), _order("filled")]
        out = await tools.await_order_state("0rd3r-0001", "ACCT1")
        assert (out["state"], out["filled"], out["polls"]) == ("filled", True, 3)
        assert (out["average_price"], out["cumulative_quantity"]) == ("42.170000", "7.000000")
        assert slept == [10.0, 10.0]
        assert calls[0] == {
            "tool": "get_equity_orders",
            "order_id": "0rd3r-0001",
            "account_number": "ACCT1",
        }

    @pytest.mark.parametrize("state", ["cancelled", "rejected", "failed"])
    async def test_an_order_that_ended_returns_at_once(self, broker, state) -> None:
        _, replies, slept = broker
        replies.append(_order(state))
        out = await tools.await_order_state("0rd3r-0001", "ACCT1")
        assert (out["state"], out["filled"], out["polls"], slept) == (state, False, 1, [])

    async def test_at_the_timeout_it_returns_the_last_state(self, broker, monkeypatch) -> None:
        _, replies, _ = broker
        replies += [_order("confirmed")] * 10
        clock = iter(range(0, 1000, 5))
        monkeypatch.setattr(tools.time, "monotonic", lambda: float(next(clock)))
        out = await tools.await_order_state(
            "0rd3r-0001", "ACCT1", timeout_seconds=12, poll_seconds=5
        )
        assert (out["state"], out["filled"]) == ("confirmed", False)
        assert out["elapsed_seconds"] <= 12

    async def test_the_wait_is_capped(self, broker, monkeypatch) -> None:
        _, replies, slept = broker
        replies += [_order("partially_filled")] * 200
        clock = iter(range(0, 10_000, 1))
        monkeypatch.setattr(tools.time, "monotonic", lambda: float(next(clock)))
        out = await tools.await_order_state(
            "0rd3r-0001", "ACCT1", timeout_seconds=3600, poll_seconds=0
        )
        assert set(slept) == {2.0}, "polls no faster than every 2 seconds"
        assert out["elapsed_seconds"] <= tools._AWAIT_ORDER_MAX_SECONDS

    async def test_no_answer_is_not_a_state(self, broker, monkeypatch) -> None:
        _, _, _ = broker
        clock = iter(range(0, 1000, 30))
        monkeypatch.setattr(tools.time, "monotonic", lambda: float(next(clock)))
        out = await tools.await_order_state("0rd3r-0001", "", timeout_seconds=60)
        assert (out["state"], out["filled"]) == (None, False)

    async def test_the_account_is_looked_up_when_not_given(self, broker) -> None:
        calls, replies, _ = broker
        replies.append(_order("filled"))
        await tools.await_order_state("0rd3r-0001")
        assert calls[0]["account_number"] == "ACCT1"

    def test_the_executor_has_it(self) -> None:
        from evotrader.agents.factory import create_execution_agent
        from evotrader.config import AppConfig

        agent = create_execution_agent(AppConfig())
        names = {getattr(t, "__name__", getattr(t, "name", "")) for t in agent.tools}
        assert "await_order_state" in names
