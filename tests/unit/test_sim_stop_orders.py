"""Regression tests: practice mode handles stop orders the way the broker does.

Found 2026-09-25 in a practice cycle. The executor placed its protective stops
with the broker's own arguments — ``type: "stop_market"``, a ``stop_price`` and
``time_in_force: "gtc"`` — and the simulated broker raised "Order type
stop_market is not simulated". Every protective stop failed in practice mode,
so practice runs never exercised the stop-loss path, which is what they are for.

At the broker a stop RESTS: it is working with nothing filled until the price
reaches the stop, and then it trades at the market. A day order ends at the
close; a gtc order stays until it fills or is cancelled. A sell stop for more
shares than are held is refused ("Not enough shares to sell"). These tests hold
the simulated broker to the same.
"""

from __future__ import annotations

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
_AFTER_HOURS = "2026-09-24T18:00:00-04:00"
_NEXT_MORNING = "2026-09-25T09:31:00-04:00"

# A Tuesday-morning placement time (UTC, as the sim stores it). Its session
# closed long before any test runs, and it is more than 24 hours old.
_EARLIER_SESSION = "2026-09-22 14:00:00"

_HELD = 10.0
_ENTRY_ASK = 150.10
_CASH_AFTER_ENTRY = 10_000.0 - _HELD * _ENTRY_ASK


def _quote(broker: SimBroker, bid: float, ask: float) -> None:
    broker._get_live_bid_ask = AsyncMock(return_value=(bid, ask))  # type: ignore[method-assign]
    broker._get_live_price = AsyncMock(return_value=bid)  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _regular_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVOTRADER_MOCK_TIME", _REGULAR_SESSION)


@pytest.fixture
async def broker(tmp_path: Path) -> AsyncIterator[SimBroker]:
    """A practice account holding 10 MSTR, the position the stops protect."""
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


async def _place_stop(
    broker: SimBroker,
    *,
    side: str = "sell",
    stop: str = "142.50",
    qty: float = 3,
    tif: str | None = "gtc",
    order_type: str = "stop_market",
    **extra: Any,
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "account_number": broker.account_number,
        "symbol": "MSTR",
        "side": side,
        "type": order_type,
        "stop_price": stop,
        "quantity": qty,
        **extra,
    }
    if tif is not None:
        args["time_in_force"] = tif
    return (await broker.place_equity_order(args))["data"]


async def _order(broker: SimBroker, order_id: str) -> dict[str, Any]:
    return (await broker.get_order_status(order_id))["data"]


async def _held(broker: SimBroker) -> float:
    positions = (await broker.get_equity_positions(broker.account_number))["data"]["positions"]
    return sum(float(p["quantity"]) for p in positions if p["symbol"] == "MSTR")


async def _cash(broker: SimBroker) -> float:
    return (await broker.get_portfolio(broker.account_number))["data"]["cash"]


async def _backdate(broker: SimBroker, *order_ids: str) -> None:
    """Move orders back to a session that has since closed."""
    async with broker._transaction() as conn:
        for order_id in order_ids:
            await conn.execute(
                "UPDATE sim_orders SET timestamp = ? WHERE id = ?", (_EARLIER_SESSION, order_id)
            )


class TestAStopRests:
    async def test_the_practice_cycle_stop_is_accepted_and_rests(self, broker: SimBroker) -> None:
        """The executor's arguments from the practice cycle, sent as it sent them."""
        proxy = SimBrokerProxy(broker)
        res = await proxy.call_tool_raw(
            "place_equity_order",
            {
                "account_number": broker.account_number,
                "symbol": "MSTR",
                "side": "sell",
                "type": "stop_market",
                "stop_price": "142.50",
                "time_in_force": "gtc",
                "quantity": 3,
            },
            original_session=None,
        )
        assert res.isError is False, res.content[0].text
        placed = json.loads(res.content[0].text)["data"]

        assert placed["state"] == "pending"
        assert float(placed["cumulative_quantity"]) == 0.0
        assert float(placed["stop_price"]) == 142.50
        assert placed["time_in_force"] == "gtc"
        # The broker's own order book lists a stop as a market order with a
        # stop trigger and no limit price (see test_broker_order_book).
        assert placed["trigger"] == "stop"
        assert placed["type"] == "market"
        assert placed["price"] is None

        # Nothing has traded yet.
        assert await _held(broker) == _HELD
        assert await _cash(broker) == pytest.approx(_CASH_AFTER_ENTRY)

    async def test_a_resting_stop_is_in_the_order_book(self, broker: SimBroker) -> None:
        stop = await _place_stop(broker)
        proxy = SimBrokerProxy(broker)

        listed = json.loads(
            (
                await proxy.call_tool_raw(
                    "get_equity_orders",
                    {"account_number": broker.account_number, "symbol": "MSTR"},
                    original_session=None,
                )
            )
            .content[0]
            .text
        )["data"]["results"]
        resting = [o for o in listed if o["id"] == stop["id"]]
        assert len(resting) == 1
        assert resting[0]["state"] == "pending"
        assert float(resting[0]["cumulative_quantity"]) == 0.0
        assert float(resting[0]["stop_price"]) == 142.50
        assert resting[0]["time_in_force"] == "gtc"
        assert resting[0]["trigger"] == "stop"

        status = await _order(broker, stop["id"])
        assert status["state"] == "pending"
        assert float(status["stop_price"]) == 142.50

    async def test_looking_an_order_up_by_id_returns_that_order(self, broker: SimBroker) -> None:
        """How the executor checks one order; the sim used to return every order."""
        stop = await _place_stop(broker)
        proxy = SimBrokerProxy(broker)
        res = await proxy.call_tool_raw(
            "get_equity_orders",
            {"account_number": broker.account_number, "order_id": stop["id"]},
            original_session=None,
        )
        results = json.loads(res.content[0].text)["data"]["results"]
        assert [o["id"] for o in results] == [stop["id"]]

    async def test_the_journal_spelling_stop_is_the_same_order(self, broker: SimBroker) -> None:
        stop = await _place_stop(broker, order_type="stop")
        assert stop["state"] == "pending"
        assert stop["trigger"] == "stop"
        assert stop["type"] == "market"

    async def test_a_stop_needs_a_stop_price(self, broker: SimBroker) -> None:
        with pytest.raises(ValueError, match="Stop price is required"):
            await broker.place_equity_order(
                {"symbol": "MSTR", "side": "sell", "type": "stop_market", "quantity": 3}
            )


class TestAStopFillsWhenThePriceReachesIt:
    @pytest.mark.parametrize(
        "bid",
        [142.50, 141.20],
        ids=["bid-at-the-stop", "bid-gapped-through-the-stop"],
    )
    async def test_a_sell_stop_fills_at_the_bid(self, broker: SimBroker, bid: float) -> None:
        """Once triggered a stop is a market sell: it fills at the bid, not at the
        stop. A stop guarantees the exit, not the price."""
        stop = await _place_stop(broker, stop="142.50", qty=3)

        _quote(broker, bid, bid + 0.10)
        fills = await broker._fill_pending_limit_orders()

        assert [f["order_id"] for f in fills] == [stop["id"]]
        assert fills[0]["fill_price"] == pytest.approx(bid)
        assert fills[0]["quantity"] == 3
        assert fills[0]["side"] == "sell"
        assert await _held(broker) == _HELD - 3
        assert await _cash(broker) == pytest.approx(_CASH_AFTER_ENTRY + 3 * bid)
        filled = await _order(broker, stop["id"])
        assert filled["state"] == "filled"
        assert float(filled["cumulative_quantity"]) == 3.0

    async def test_a_sell_stop_does_not_fill_while_the_price_stays_above(
        self, broker: SimBroker
    ) -> None:
        stop = await _place_stop(broker, stop="142.50")

        _quote(broker, 142.51, 142.60)
        assert await broker._fill_pending_limit_orders() == []

        assert (await _order(broker, stop["id"]))["state"] == "pending"
        assert await _held(broker) == _HELD
        assert await _cash(broker) == pytest.approx(_CASH_AFTER_ENTRY)

    async def test_a_buy_stop_fills_at_the_ask_on_a_rise(self, broker: SimBroker) -> None:
        stop = await _place_stop(broker, side="buy", stop="160.00", qty=2)
        assert stop["state"] == "pending"
        # A resting buy holds back the cash it may need.
        portfolio = (await broker.get_portfolio(broker.account_number))["data"]
        assert portfolio["buying_power"]["buying_power"] == pytest.approx(
            _CASH_AFTER_ENTRY - 2 * 160.00
        )

        _quote(broker, 159.90, 159.99)
        assert await broker._fill_pending_limit_orders() == []

        _quote(broker, 160.40, 160.50)
        fills = await broker._fill_pending_limit_orders()
        assert [f["order_id"] for f in fills] == [stop["id"]]
        assert fills[0]["fill_price"] == pytest.approx(160.50)
        assert await _held(broker) == _HELD + 2
        assert await _cash(broker) == pytest.approx(_CASH_AFTER_ENTRY - 2 * 160.50)

    async def test_a_stop_does_not_trigger_outside_the_regular_session(
        self, broker: SimBroker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """At the broker a stop cannot trigger outside regular hours; it waits
        for the open and then fills at whatever the price is by then."""
        stop = await _place_stop(broker, stop="142.50")

        monkeypatch.setenv("EVOTRADER_MOCK_TIME", _AFTER_HOURS)
        _quote(broker, 139.00, 139.10)
        assert await broker._fill_pending_limit_orders() == []
        assert (await _order(broker, stop["id"]))["state"] == "pending"

        monkeypatch.setenv("EVOTRADER_MOCK_TIME", _NEXT_MORNING)
        _quote(broker, 137.00, 137.10)
        fills = await broker._fill_pending_limit_orders()
        assert [f["order_id"] for f in fills] == [stop["id"]]
        assert fills[0]["fill_price"] == pytest.approx(137.00)


class TestStopLimit:
    async def test_a_sell_stop_limit_fills_between_its_stop_and_its_limit(
        self, broker: SimBroker
    ) -> None:
        stop = await _place_stop(
            broker, order_type="stop_limit", stop="142.50", limit_price="141.00"
        )
        assert stop["state"] == "pending"
        assert stop["trigger"] == "stop"
        assert float(stop["price"]) == 141.00

        _quote(broker, 141.50, 141.60)
        fills = await broker._fill_pending_limit_orders()
        assert [f["order_id"] for f in fills] == [stop["id"]]
        assert fills[0]["fill_price"] == pytest.approx(141.50)

    async def test_a_triggered_stop_limit_past_its_limit_works_on_as_a_limit(
        self, broker: SimBroker
    ) -> None:
        """Triggered but already through its limit, it rests as a limit order — so a
        recovery fills it, even one back above the stop."""
        stop = await _place_stop(
            broker, order_type="stop_limit", stop="142.50", limit_price="141.00"
        )

        _quote(broker, 140.00, 140.10)
        assert await broker._fill_pending_limit_orders() == []
        assert (await _order(broker, stop["id"]))["state"] == "pending"

        _quote(broker, 143.00, 143.10)
        fills = await broker._fill_pending_limit_orders()
        assert [f["order_id"] for f in fills] == [stop["id"]]
        assert fills[0]["fill_price"] == pytest.approx(143.00)


class TestTimeInForce:
    async def test_a_day_stop_expires_at_the_close_and_a_gtc_stop_stays(
        self, broker: SimBroker
    ) -> None:
        day = await _place_stop(broker, stop="142.50", qty=3, tif="gfd")
        gtc = await _place_stop(broker, stop="140.00", qty=3, tif="gtc")
        await _backdate(broker, day["id"], gtc["id"])

        cancelled = await broker.cancel_stale_pending_orders()

        assert [c["id"] for c in cancelled] == [day["id"]]
        assert (await _order(broker, day["id"]))["state"] == "cancelled"
        # Older than a day, and still working: gtc means until filled or cancelled.
        assert (await _order(broker, gtc["id"]))["state"] == "pending"

        _quote(broker, 139.50, 139.60)
        fills = await broker._fill_pending_limit_orders()
        assert [f["order_id"] for f in fills] == [gtc["id"]]

    async def test_a_day_stop_still_rests_before_its_close(self, broker: SimBroker) -> None:
        day = await _place_stop(broker, tif="gfd")
        assert await broker.cancel_stale_pending_orders() == []
        assert (await _order(broker, day["id"]))["state"] == "pending"

    async def test_an_expired_day_stop_does_not_fill_the_next_morning(
        self, broker: SimBroker
    ) -> None:
        """The cycle's fill check runs before its stale-order sweep; a day order
        from a closed session must not trade in between."""
        day = await _place_stop(broker, stop="142.50", tif="gfd")
        await _backdate(broker, day["id"])

        _quote(broker, 139.00, 139.10)
        assert await broker._fill_pending_limit_orders() == []
        assert await _held(broker) == _HELD

        assert [c["id"] for c in await broker.cancel_stale_pending_orders()] == [day["id"]]

    async def test_a_stop_placed_without_time_in_force_is_a_day_order(
        self, broker: SimBroker
    ) -> None:
        """As at the broker: the 2026-09-17 stop had no time_in_force and was gone
        the next morning."""
        stop = await _place_stop(broker, tif=None)
        assert stop["time_in_force"] == "gfd"

        await _backdate(broker, stop["id"])
        assert [c["id"] for c in await broker.cancel_stale_pending_orders()] == [stop["id"]]


class TestCancellingAStop:
    async def test_a_resting_stop_can_be_cancelled_with_the_brokers_cancel_tool(
        self, broker: SimBroker
    ) -> None:
        """The agents cancel with ``cancel_equity_order``, the broker's tool name."""
        stop = await _place_stop(broker)
        proxy = SimBrokerProxy(broker)

        res = await proxy.call_tool_raw(
            "cancel_equity_order",
            {"account_number": broker.account_number, "order_id": stop["id"]},
            original_session=None,
        )
        assert res.isError is False, res.content[0].text
        assert json.loads(res.content[0].text)["data"]["state"] == "cancelled"
        assert (await _order(broker, stop["id"]))["state"] == "cancelled"

        _quote(broker, 139.00, 139.10)
        assert await broker._fill_pending_limit_orders() == []
        assert await _held(broker) == _HELD

    async def test_the_sims_cancel_order_tool_cancels_a_stop(self, broker: SimBroker) -> None:
        stop = await _place_stop(broker)
        res = await broker.cancel_order(stop["id"])
        assert res["data"]["state"] == "cancelled"
        assert (await _order(broker, stop["id"]))["state"] == "cancelled"

    async def test_a_stop_cancelled_while_its_fill_check_waits_for_a_quote_does_not_fill(
        self, broker: SimBroker
    ) -> None:
        """Cancel-then-replace lands in the middle of a fill check (the agent's tool
        calls run side by side). The cancel wins; the check must not sell anyway."""
        stop = await _place_stop(broker, stop="142.50")

        async def quote_while_the_cancel_lands(
            ticker: str, strict: bool = False
        ) -> tuple[float, float]:
            await broker.cancel_order(stop["id"])
            return 139.00, 139.10

        broker._get_live_bid_ask = quote_while_the_cancel_lands  # type: ignore[method-assign]
        assert await broker._fill_pending_limit_orders() == []

        assert (await _order(broker, stop["id"]))["state"] == "cancelled"
        assert await _held(broker) == _HELD
        assert await _cash(broker) == pytest.approx(_CASH_AFTER_ENTRY)


class TestNoQuoteNoFill:
    async def test_a_failed_quote_does_not_trigger_a_stop(self, broker: SimBroker) -> None:
        """Without a quote the sim's price helpers answer a made-up 100.0. Read as
        a real bid, that would trigger this 142.50 stop and sell at $100."""
        stop = await _place_stop(broker, stop="142.50")

        quote_source = MagicMock()
        quote_source._mcp_session_manager.create_session = AsyncMock(
            side_effect=ConnectionError("quote source unreachable")
        )
        broker.real_mcp_toolset = quote_source
        del broker._get_live_bid_ask  # back to the real quote lookup

        assert await broker._fill_pending_limit_orders() == []
        assert (await _order(broker, stop["id"]))["state"] == "pending"
        assert await _held(broker) == _HELD


class TestSellStopNeedsTheShares:
    async def test_a_sell_stop_larger_than_the_position_is_refused(self, broker: SimBroker) -> None:
        refused = await _place_stop(broker, qty=_HELD + 1)
        assert refused["state"] == "rejected"
        assert refused["reason"] == "Not enough shares to sell"
        orders = (await broker.get_orders({"symbol": "MSTR"}))["data"]["results"]
        assert all(o["trigger"] != "stop" for o in orders), "a refused stop is not in the book"

    async def test_a_sell_stop_with_no_position_is_refused(self, broker: SimBroker) -> None:
        refused = (
            await broker.place_equity_order(
                {
                    "symbol": "SPY",
                    "side": "sell",
                    "type": "stop_market",
                    "stop_price": "500.00",
                    "time_in_force": "gtc",
                    "quantity": 1,
                }
            )
        )["data"]
        assert refused["state"] == "rejected"
        assert refused["reason"] == "Not enough shares to sell"

    async def test_a_sell_stop_for_the_whole_position_is_accepted(self, broker: SimBroker) -> None:
        stop = await _place_stop(broker, qty=_HELD)
        assert stop["state"] == "pending"


class TestAnExistingPracticeDatabase:
    async def test_it_gains_time_in_force_and_its_old_orders_keep_working(
        self, tmp_path: Path
    ) -> None:
        """The practice database on disk predates the time_in_force column, and
        CREATE TABLE IF NOT EXISTS does not add a column to an existing table."""
        path = tmp_path / "sim_broker.db"
        old = SimBroker(path)
        await old.initialize()
        async with old._transaction() as conn:
            await conn.execute("ALTER TABLE sim_orders DROP COLUMN time_in_force")
            await conn.execute(
                """
                INSERT INTO sim_orders (id, account_number, asset_type, ticker, side, order_type,
                                        quantity, limit_price, status, timestamp)
                VALUES ('legacy01', ?, 'EQUITY', 'MSTR', 'buy', 'limit', 1, 140.0, 'pending', ?)
                """,
                (old.account_number, _EARLIER_SESSION),
            )
        await old.close()

        b = SimBroker(path)
        b.slippage_model = "none"
        await b.initialize()
        try:
            _quote(b, 150.00, 150.10)
            legacy = (await b.get_order_status("legacy01"))["data"]
            assert legacy["state"] == "pending"
            assert legacy["time_in_force"] is None
            assert legacy["trigger"] == "immediate"
            # A row with no time_in_force keeps the old rule: gone after 24 hours.
            assert [c["id"] for c in await b.cancel_stale_pending_orders()] == ["legacy01"]
        finally:
            await b.close()


class TestAStopFillReachesTheJournal:
    async def test_the_journaled_stop_turns_filled_at_the_sim_fill_price(
        self, broker: SimBroker, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The executor journals the stop from the broker's answer (PENDING); the
        cycle's fill check is how the journal hears that it filled."""
        from evotrader.agents import tools
        from evotrader.db.journal import TradeJournal
        from evotrader.models.trade import (
            OrderType,
            TradeAction,
            TradeDirection,
            TradeProposal,
        )

        journal = TradeJournal(db)
        monkeypatch.setattr(tools, "_journal", journal)
        monkeypatch.setattr(tools, "_sim_proxy", SimBrokerProxy(broker))
        monkeypatch.setattr(tools, "_current_session_id", "practice")
        await journal.record_trade(
            TradeProposal(
                ticker="MSTR",
                direction=TradeDirection.LONG,
                action=TradeAction.OPEN,
                quantity=_HELD,
                order_type=OrderType.MARKET,
                limit_price=_ENTRY_ASK,
                algo_signal=0.3,
                hybrid_score=0.3,
                confidence=0.5,
                regime="trending_bull",
                algo_version="test",
            )
        )

        placed = await _place_stop(broker, stop="142.50", qty=3)
        await tools.record_trade(
            json.dumps(
                {
                    "ticker": "MSTR",
                    "action": "STOP_LOSS",
                    "direction": "LONG",
                    "quantity": 3,
                    "order_type": "stop_market",
                    "stop_price": 142.50,
                    "time_in_force": "gtc",
                    "order_id": placed["id"],
                    "status": placed["state"],
                    "cumulative_quantity": placed["cumulative_quantity"],
                    "algo_signal": 0.3,
                    "hybrid_score": 0.3,
                    "confidence": 0.5,
                    "regime": "trending_bull",
                    "algo_version": "test",
                }
            )
        )

        async def stop_row() -> dict[str, Any]:
            rows = await journal.get_recent_trades(limit=10)
            [row] = [r for r in rows if r["action"] == "STOP_LOSS"]
            return row

        assert (await stop_row())["order_status"] == "PENDING"

        _quote(broker, 141.00, 141.10)
        result = await tools.check_and_journal_pending_fills()

        assert result["fills_count"] == 1
        row = await stop_row()
        assert row["order_status"] == "FILLED"
        assert row["fill_price"] == pytest.approx(141.00)
