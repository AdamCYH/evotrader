"""Regression tests: practice mode refuses what the broker refuses.

The simulated broker was kinder than the real one in four ways, so a practice
run could "succeed" at things the live account cannot do:

a) No live quote. The sim's price helpers answer a made-up price when the
   quote source fails ($100 a share, $1 an option contract). The resting-order
   fill check already refused to trade on it; a market order still filled at it.
b) Shares held back by resting sells. At the broker a resting sell (stop or
   limit) holds its shares, so a second sell for the same shares is refused
   with "Not enough shares to sell" — the live system hit that refusal again and
   again. The sim let sells stack beyond the position, a stop could later sell
   shares a market sell had already sold, and a plain sell for more than was
   held quietly opened a short the account can never hold.
c) Buying power read before the write. The balance was read outside the write
   turn, so two buys placed at the same moment could both pass the check and
   spend the same cash; deposits and withdrawals could lose an update.
d) Fractional stops. The broker takes fractional shares only in market orders;
   a stop for a fractional quantity is refused.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from evotrader.sim import SimBroker, SimBrokerProxy

# Stops trigger only in the regular session, so the tests run inside one.
# EVOTRADER_MOCK_TIME is the app's own practice-mode clock override.
_REGULAR_SESSION = "2026-09-24T10:30:00-04:00"

_HELD = 10.0
_ENTRY_ASK = 150.10
_CASH_AFTER_ENTRY = 10_000.0 - _HELD * _ENTRY_ASK

_NOT_ENOUGH_SHARES = "Not enough shares to sell"
_WHOLE_SHARES_ONLY = "Stop orders must be for a whole number of shares"


def _quote(broker: SimBroker, bid: float, ask: float) -> None:
    broker._get_live_bid_ask = AsyncMock(return_value=(bid, ask))  # type: ignore[method-assign]
    broker._get_live_price = AsyncMock(return_value=bid)  # type: ignore[method-assign]


def _quote_source_down(broker: SimBroker) -> None:
    """Back to the real quote lookups, against a quote source that cannot be reached."""
    source = MagicMock()
    source._mcp_session_manager.create_session = AsyncMock(
        side_effect=ConnectionError("quote source unreachable")
    )
    broker.real_mcp_toolset = source
    for lookup in (
        "_get_live_bid_ask",
        "_get_live_price",
        "_get_live_option_bid_ask",
        "_get_live_option_price",
    ):
        vars(broker).pop(lookup, None)


@pytest.fixture(autouse=True)
def _regular_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVOTRADER_MOCK_TIME", _REGULAR_SESSION)


@pytest.fixture
async def broker(tmp_path: Path) -> AsyncIterator[SimBroker]:
    """A practice account holding 10 MSTR."""
    b = SimBroker(tmp_path / "sim_broker.db")
    b.slippage_model = "none"
    await b.initialize()
    await b.deposit(10_000.0)
    _quote(b, 150.00, _ENTRY_ASK)
    entry = await b.place_equity_order(
        {"symbol": "MSTR", "side": "buy", "type": "market", "quantity": _HELD}
    )
    assert entry["data"]["status"] == "filled"
    yield b
    await b.close()


async def _place(broker: SimBroker, **args: Any) -> dict[str, Any]:
    return (await broker.place_equity_order({"symbol": "MSTR", **args}))["data"]


async def _sell_stop(broker: SimBroker, qty: float, stop: str = "142.50") -> dict[str, Any]:
    return await _place(
        broker,
        side="sell",
        type="stop_market",
        stop_price=stop,
        quantity=qty,
        time_in_force="gtc",
    )


async def _take_profit(broker: SimBroker, qty: float, limit: str = "170.00") -> dict[str, Any]:
    return await _place(
        broker, side="sell", type="limit", limit_price=limit, quantity=qty, time_in_force="gtc"
    )


async def _held(broker: SimBroker, ticker: str = "MSTR") -> float:
    """Shares held, read straight from the sim's table (no fill check runs)."""
    conn = await broker._get_conn()
    async with conn.execute(
        "SELECT quantity FROM sim_positions WHERE asset_type = 'EQUITY' AND ticker = ?",
        (ticker,),
    ) as cur:
        row = await cur.fetchone()
    return float(row["quantity"]) if row else 0.0


async def _cash(broker: SimBroker) -> float:
    conn = await broker._get_conn()
    async with conn.execute(
        "SELECT cash_balance FROM sim_accounts WHERE account_number = ?",
        (broker.account_number,),
    ) as cur:
        return float((await cur.fetchone())["cash_balance"])


async def _pending(broker: SimBroker) -> list[str]:
    conn = await broker._get_conn()
    async with conn.execute("SELECT id FROM sim_orders WHERE status = 'pending'") as cur:
        return [row["id"] for row in await cur.fetchall()]


async def _state(broker: SimBroker, order_id: str) -> str:
    return (await broker.get_order_status(order_id))["data"]["state"]


class TestNoQuoteNoMarketFill:
    """(a) A market order does not trade on a made-up price."""

    async def test_a_market_buy_is_refused(self, broker: SimBroker) -> None:
        _quote_source_down(broker)
        placed = await _place(broker, side="buy", type="market", quantity=1)
        assert placed["state"] == "rejected"
        assert placed["reason"] == "No live quote for MSTR — market order not filled"
        assert await _held(broker) == _HELD
        assert await _cash(broker) == pytest.approx(_CASH_AFTER_ENTRY)

    async def test_a_market_sell_is_refused(self, broker: SimBroker) -> None:
        _quote_source_down(broker)
        placed = await _place(broker, side="sell", type="market", quantity=5)
        assert placed["state"] == "rejected"
        assert placed["reason"] == "No live quote for MSTR — market order not filled"
        assert await _held(broker) == _HELD
        assert await _cash(broker) == pytest.approx(_CASH_AFTER_ENTRY)

    async def test_a_market_option_order_is_refused(self, broker: SimBroker) -> None:
        _quote_source_down(broker)
        res = await broker.place_option_order(
            {
                "legs": [{"option_id": "opt_xyz", "side": "buy", "position_effect": "open"}],
                "type": "market",
                "quantity": 1,
            }
        )
        assert res["data"]["state"] == "rejected"
        assert res["data"]["reason"] == "No live quote for opt_xyz — market order not filled"
        options = await broker.get_option_positions(broker.account_number)
        assert options["data"]["positions"] == []
        assert await _cash(broker) == pytest.approx(_CASH_AFTER_ENTRY)

    async def test_a_limit_order_rests_until_a_real_quote_reaches_it(
        self, broker: SimBroker
    ) -> None:
        """The broker would take a limit order, so it is not refused; but it must
        not fill at the made-up $100 either. The resting-order check fills it once
        a real quote reaches the limit."""
        _quote_source_down(broker)
        placed = await _place(
            broker,
            side="buy",
            type="limit",
            limit_price="150.00",
            quantity=1,
            time_in_force="gtc",
        )
        assert placed["state"] == "pending"
        assert await _held(broker) == _HELD

        _quote(broker, 149.90, 149.95)
        fills = await broker._fill_pending_limit_orders()
        assert [f["order_id"] for f in fills] == [placed["id"]]
        assert fills[0]["fill_price"] == pytest.approx(149.95)


class TestARestingSellHoldsItsShares:
    """(b) available = held - shares held back by resting sells."""

    async def test_a_second_stop_for_the_same_shares_is_refused(self, broker: SimBroker) -> None:
        first = await _sell_stop(broker, _HELD)
        assert first["state"] == "pending"

        second = await _sell_stop(broker, _HELD, stop="140.00")
        assert second["state"] == "rejected"
        assert second["reason"] == _NOT_ENOUGH_SHARES
        assert await _pending(broker) == [first["id"]]

    async def test_a_take_profit_on_top_of_a_full_stop_is_refused(self, broker: SimBroker) -> None:
        """The live refusal: a stop covering the position, then a take-profit for
        the same shares."""
        await _sell_stop(broker, _HELD)
        take_profit = await _take_profit(broker, _HELD)
        assert take_profit["state"] == "rejected"
        assert take_profit["reason"] == _NOT_ENOUGH_SHARES

    async def test_a_stop_limit_holds_its_shares_too(self, broker: SimBroker) -> None:
        stop_limit = await _place(
            broker,
            side="sell",
            type="stop_limit",
            stop_price="142.50",
            limit_price="141.00",
            quantity=_HELD,
            time_in_force="gtc",
        )
        assert stop_limit["state"] == "pending"
        assert (await _sell_stop(broker, 1))["reason"] == _NOT_ENOUGH_SHARES

    async def test_sells_are_accepted_up_to_the_shares_left(self, broker: SimBroker) -> None:
        assert (await _sell_stop(broker, 6))["state"] == "pending"
        assert (await _take_profit(broker, 4))["state"] == "pending"

        refused = await _place(broker, side="sell", type="market", quantity=1)
        assert refused["state"] == "rejected"
        assert refused["reason"] == _NOT_ENOUGH_SHARES
        assert await _held(broker) == _HELD

    async def test_a_market_sell_cannot_take_the_shares_a_stop_holds(
        self, broker: SimBroker
    ) -> None:
        await _sell_stop(broker, 6)
        refused = await _place(broker, side="sell", type="market", quantity=5)
        assert refused["reason"] == _NOT_ENOUGH_SHARES

        sold = await _place(broker, side="sell", type="market", quantity=4)
        assert sold["state"] == "filled"
        assert await _held(broker) == _HELD - 4

    @pytest.mark.parametrize(
        "order",
        [
            {"type": "market"},
            {"type": "limit", "limit_price": "140.00"},  # marketable: fills at once
            {"type": "limit", "limit_price": "170.00", "time_in_force": "gtc"},  # would rest
        ],
        ids=["market", "marketable-limit", "resting-limit"],
    )
    async def test_a_sell_for_more_than_is_held_is_refused(
        self, broker: SimBroker, order: dict[str, Any]
    ) -> None:
        """It used to fill (or rest) and open a short the cash account cannot hold."""
        placed = await _place(broker, side="sell", quantity=_HELD + 1, **order)
        assert placed["state"] == "rejected"
        assert placed["reason"] == _NOT_ENOUGH_SHARES
        assert await _held(broker) == _HELD
        assert await _pending(broker) == []

    async def test_a_sell_with_no_position_is_refused(self, broker: SimBroker) -> None:
        placed = (
            await broker.place_equity_order(
                {"symbol": "SPY", "side": "sell", "type": "market", "quantity": 1}
            )
        )["data"]
        assert placed["reason"] == _NOT_ENOUGH_SHARES
        assert await _held(broker, "SPY") == 0.0

    async def test_a_close_cancels_the_stop_first_then_sells(self, broker: SimBroker) -> None:
        """The executor's close, through the tools the agents use: while the stop
        rests the sell is refused; once the cancel lands the shares are free."""
        proxy = SimBrokerProxy(broker)

        async def call(tool: str, **args: Any) -> dict[str, Any]:
            res = await proxy.call_tool_raw(
                tool, {"account_number": broker.account_number, **args}, original_session=None
            )
            assert res.isError is False, res.content[0].text
            return json.loads(res.content[0].text)["data"]

        stop = await _sell_stop(broker, _HELD)
        close = {"symbol": "MSTR", "side": "sell", "type": "market", "quantity": _HELD}

        refused = await call("place_equity_order", **close)
        assert refused["state"] == "rejected"
        assert refused["reason"] == _NOT_ENOUGH_SHARES

        assert (await call("cancel_equity_order", order_id=stop["id"]))["state"] == "cancelled"
        closed = await call("place_equity_order", **close)
        assert closed["state"] == "filled"
        assert await _held(broker) == 0.0

    async def test_a_stop_never_sells_shares_that_are_gone(self, broker: SimBroker) -> None:
        """A market sell took the shares the stop protected, then the stop
        triggered and sold them again: a short the account can never hold."""
        stop = await _sell_stop(broker, _HELD)
        await _place(broker, side="sell", type="market", quantity=_HELD)

        _quote(broker, 141.00, 141.10)
        await broker._fill_pending_limit_orders()
        assert await _held(broker) == 0.0
        assert await _state(broker, stop["id"]) == "filled"

    async def test_stops_stacked_by_an_older_sim_do_not_sell_twice(self, broker: SimBroker) -> None:
        """A practice database written before this check can already hold two
        resting stops for the same shares. Only one may sell them."""
        async with broker._transaction() as conn:
            for order_id in ("stack001", "stack002"):
                await conn.execute(
                    """
                    INSERT INTO sim_orders (id, account_number, asset_type, ticker, side,
                                            order_type, quantity, stop_price, time_in_force,
                                            status, timestamp)
                    VALUES (?, ?, 'EQUITY', 'MSTR', 'sell', 'stop', ?, 142.50, 'gtc',
                            'pending', datetime('now'))
                    """,
                    (order_id, broker.account_number, _HELD),
                )

        _quote(broker, 141.00, 141.10)
        fills = await broker._fill_pending_limit_orders()
        assert len(fills) == 1
        assert await _held(broker) == 0.0
        states = sorted([await _state(broker, "stack001"), await _state(broker, "stack002")])
        assert states == ["cancelled", "filled"]

    async def test_two_stops_sent_at_once_for_the_same_shares(self, broker: SimBroker) -> None:
        """The executor sends orders side by side; the shares check takes turns."""
        placed = await asyncio.gather(
            _sell_stop(broker, _HELD), _sell_stop(broker, _HELD, stop="140.00")
        )
        assert sorted(p["state"] for p in placed) == ["pending", "rejected"]

    async def test_a_resting_buy_holds_back_no_shares(self, broker: SimBroker) -> None:
        await _place(
            broker, side="buy", type="limit", limit_price="140.00", quantity=5, time_in_force="gtc"
        )
        assert (await _sell_stop(broker, _HELD))["state"] == "pending"

    async def test_another_symbols_stop_holds_back_nothing_here(self, broker: SimBroker) -> None:
        spy = await broker.place_equity_order(
            {"symbol": "SPY", "side": "buy", "type": "market", "quantity": 2}
        )
        assert spy["data"]["status"] == "filled"
        spy_stop = await broker.place_equity_order(
            {
                "symbol": "SPY",
                "side": "sell",
                "type": "stop_market",
                "stop_price": "140.00",
                "quantity": 2,
                "time_in_force": "gtc",
            }
        )
        assert spy_stop["data"]["state"] == "pending"
        assert (await _sell_stop(broker, _HELD))["state"] == "pending"


class TestTheCashChecksTakeTurns:
    """(c) The balance is read inside the same write turn as the write."""

    @staticmethod
    def _slow_holdings_prices(broker: SimBroker) -> None:
        """Pricing a holding is a network call. While one order waited on it, the
        other order read the same cash."""

        async def slow_price(ticker: str) -> float:
            await asyncio.sleep(0.05)
            return 150.00

        broker._get_live_price = slow_price  # type: ignore[method-assign]

    async def test_two_buys_at_once_cannot_spend_the_same_cash(self, broker: SimBroker) -> None:
        qty = 30  # 30 x 150.10 = 4,503: either buy fits the 8,499 cash, not both
        self._slow_holdings_prices(broker)
        placed = await asyncio.gather(
            *(_place(broker, side="buy", type="market", quantity=qty) for _ in range(2))
        )
        assert sorted(p["state"] for p in placed) == ["filled", "rejected"]
        assert [p["reason"] for p in placed if p["state"] == "rejected"] == [
            "Insufficient buying power"
        ]
        assert await _cash(broker) == pytest.approx(_CASH_AFTER_ENTRY - qty * _ENTRY_ASK)

    async def test_two_option_buys_at_once_cannot_spend_the_same_cash(
        self, broker: SimBroker
    ) -> None:
        broker._get_live_option_bid_ask = AsyncMock(return_value=(20.0, 20.0))  # type: ignore[method-assign]
        self._slow_holdings_prices(broker)
        order = {
            "legs": [{"option_id": "opt_xyz", "side": "buy", "position_effect": "open"}],
            "type": "market",
            "quantity": 3,  # 3 contracts x $20 x 100 = 6,000 each
        }
        placed = await asyncio.gather(
            broker.place_option_order(dict(order)), broker.place_option_order(dict(order))
        )
        assert sorted(p["data"]["state"] for p in placed) == ["filled", "rejected"]
        assert await _cash(broker) == pytest.approx(_CASH_AFTER_ENTRY - 6_000.0)

    async def test_two_withdrawals_at_once_cannot_overdraw(self, broker: SimBroker) -> None:
        results = await asyncio.gather(
            broker.withdraw(5_000.0), broker.withdraw(5_000.0), return_exceptions=True
        )
        assert sum(isinstance(r, ValueError) for r in results) == 1, results
        assert await _cash(broker) == pytest.approx(_CASH_AFTER_ENTRY - 5_000.0)

    async def test_deposits_at_once_all_count(self, broker: SimBroker) -> None:
        await asyncio.gather(broker.deposit(100.0), broker.deposit(100.0))
        assert await _cash(broker) == pytest.approx(_CASH_AFTER_ENTRY + 200.0)


class TestStopsNeedWholeShares:
    """(d) The broker takes fractional shares only in market orders."""

    @pytest.mark.parametrize(
        "order",
        [
            {"side": "sell", "type": "stop_market", "stop_price": "142.50"},
            {"side": "sell", "type": "stop_limit", "stop_price": "142.50", "limit_price": "141"},
            {"side": "buy", "type": "stop_market", "stop_price": "160.00"},
        ],
        ids=["sell-stop", "sell-stop-limit", "buy-stop"],
    )
    async def test_a_fractional_stop_is_refused(
        self, broker: SimBroker, order: dict[str, Any]
    ) -> None:
        placed = await _place(broker, quantity=2.5, time_in_force="gtc", **order)
        assert placed["state"] == "rejected"
        assert placed["reason"] == _WHOLE_SHARES_ONLY
        assert await _pending(broker) == []

    @pytest.mark.parametrize("qty", [3, 3.0, "3"])
    async def test_a_whole_share_stop_is_accepted(self, broker: SimBroker, qty: Any) -> None:
        assert (await _sell_stop(broker, qty))["state"] == "pending"

    async def test_a_fractional_market_sell_still_fills(self, broker: SimBroker) -> None:
        placed = await _place(broker, side="sell", type="market", quantity=2.5)
        assert placed["state"] == "filled"
        assert await _held(broker) == pytest.approx(_HELD - 2.5)
