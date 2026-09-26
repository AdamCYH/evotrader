"""A protective order must never be journaled as a fill it cannot have had.

See: data/evolution/reviews/
20260925_210715_record_trade_trusts_executor_fill_claims_phantom_stop_fills
_and_cost_basis_rebase.md

2026-09-25, live. At 11:30 ET the broker answered a protective MSTR stop_market
with `state: "unconfirmed"`, `cumulative_quantity: "0.000000"`, `executions: []`.
The executor called ``record_trade`` with ``"status": "FILLED"``.

What followed, in order:
  * the stop was written FILLED with a realized P&L against an open lot, and no
    ``pending_orders`` row, because that branch only runs for PENDING
  * at 13:30 the same for the replacement stop: two close rows sharing one
    broker order, plus a SHORT OPEN the flip path built out of the leftover
    quantity
  * the journal now believed the position was short while the broker held it
    long, so the 14:30, 15:30 and 17:00 cycles each read ``pending_count: 0``,
    proposed the same repair stop, and were refused "Not enough shares to sell"
  * each refusal journaled a FAILED stop plus a SHORT OPEN for the excess quantity
  * the 16:00 sync wrote rows to balance the books and rebased the cost basis
    to the current mark, which turned the resting stop from 1.65 ATR below
    entry into 0.98

All of that realized P&L was phantom, none of it a fill. Four cycles and two
execution stages spent on it.

The single fact that prevents all of it: a resting protective order cannot be
filled at the moment it is placed, and the only evidence of a fill is the
broker's own `state: filled` with a price and a quantity.
"""

from __future__ import annotations

import json
from typing import ClassVar

import pytest

from evotrader.db.journal import TradeJournal
from evotrader.models.trade import (
    OrderType,
    TradeAction,
    TradeDirection,
    TradeProposal,
)


@pytest.fixture
def journal(db) -> TradeJournal:
    return TradeJournal(db)


def _lot(quantity: float, price: float) -> TradeProposal:
    return TradeProposal(
        ticker="MSTR",
        direction=TradeDirection.LONG,
        action=TradeAction.OPEN,
        quantity=quantity,
        order_type=OrderType.MARKET,
        limit_price=price,
        algo_signal=0.38,
        hybrid_score=0.38,
        confidence=0.45,
        regime="trending_bull",
        algo_version="composite_v030",
    )


def _stop(quantity: float, status: str) -> TradeProposal:
    return TradeProposal(
        ticker="MSTR",
        direction=TradeDirection.LONG,
        action=TradeAction.STOP_LOSS,
        quantity=quantity,
        order_type=OrderType.STOP,
        stop_price=149.80,
        time_in_force="gtc",
        algo_signal=0.23,
        hybrid_score=0.23,
        confidence=0.40,
        regime="trending_bull",
        algo_version="composite_v030",
    )


# ── Finding 1: the tool resolves status from evidence ──────────────────────


class TestARestingStopReportedFilledIsJournaledPending:
    """A live executor call's arguments, verbatim apart from a made-up order id."""

    _EXECUTOR_PAYLOAD: ClassVar[dict] = {
        "ticker": "MSTR",
        "action": "STOP_LOSS",
        "direction": "LONG",
        "quantity": 4,
        "order_type": "stop_market",
        "stop_price": 149.80,
        "time_in_force": "gtc",
        "status": "FILLED",  # the claim
        "order_id": "6ab69111",
        "state": "unconfirmed",  # what the broker actually said
        "cumulative_quantity": "0.000000",
        "algo_signal": 0.2297,
        "hybrid_score": 0.2297,
        "confidence": 0.40,
        "regime": "trending_bull",
        "algo_version": "composite_v030",
        "reasoning": "Protective stop for the 16-share long.",
    }

    async def test_the_claim_is_downgraded_to_pending(self, db, journal) -> None:
        from evotrader.agents import tools

        tools._journal = journal
        tools._current_session_id = "e37134b3"
        try:
            result = await tools.record_trade(json.dumps(self._EXECUTOR_PAYLOAD))
        finally:
            tools._journal = None
            tools._current_session_id = None

        assert "error" not in result, result
        rows = await journal.get_recent_trades(limit=10)
        stop_rows = [r for r in rows if r["action"] == "STOP_LOSS"]
        assert len(stop_rows) == 1, "one broker order is one journal row"
        row = stop_rows[0]
        assert row["order_status"] == "PENDING"
        assert row["realized_pnl"] is None, "nothing closed, so there is no P&L"
        assert "STATUS_DOWNGRADED_TO_PENDING" in (row["broker_status_reason"] or "")

    async def test_the_open_lots_are_untouched(self, db, journal) -> None:
        from evotrader.agents import tools

        await journal.record_trade(_lot(12.0, 165.10))
        await journal.record_trade(_lot(4.0, 163.90))
        before = {t["id"]: t["remaining_quantity"] for t in await journal.get_open_trades()}

        tools._journal = journal
        tools._current_session_id = "e37134b3"
        try:
            await tools.record_trade(json.dumps(self._EXECUTOR_PAYLOAD))
        finally:
            tools._journal = None
            tools._current_session_id = None

        after = {t["id"]: t["remaining_quantity"] for t in await journal.get_open_trades()}
        assert after == before, "a stop that has not triggered closes nothing"
        assert sum(after.values()) == pytest.approx(16.0)

    async def test_it_reaches_the_order_book(self, db, journal) -> None:
        """The missed ``pending_orders`` row is why three cycles read
        ``pending_count: 0`` and tried to re-place a resting stop."""
        from evotrader.agents import tools

        tools._journal = journal
        tools._current_session_id = "e37134b3"
        try:
            await tools.record_trade(json.dumps(self._EXECUTOR_PAYLOAD))
        finally:
            tools._journal = None
            tools._current_session_id = None

        pending = await journal.get_pending_orders()
        assert [p["order_id"] for p in pending] == ["6ab69111"]

    async def test_a_real_fill_is_still_recorded_as_one(self, db, journal) -> None:
        """The gate keys on evidence, so it must not block a stop that DID fill."""
        from evotrader.agents import tools

        await journal.record_trade(_lot(4.0, 165.10))
        payload = dict(self._EXECUTOR_PAYLOAD)
        payload.update(
            {
                "state": "filled",
                "status": "FILLED",
                "average_price": 149.75,
                "cumulative_quantity": "4.000000",
            }
        )
        tools._journal = journal
        tools._current_session_id = "e37134b3"
        try:
            await tools.record_trade(json.dumps(payload))
        finally:
            tools._journal = None
            tools._current_session_id = None

        rows = await journal.get_recent_trades(limit=10)
        stop_rows = [r for r in rows if r["action"] == "STOP_LOSS"]
        assert len(stop_rows) == 1
        assert stop_rows[0]["order_status"] == "FILLED"
        assert stop_rows[0]["realized_pnl"] is not None, "a real exit has real P&L"


# ── Finding 2: the flip path ───────────────────────────────────────────────


class TestAProtectiveOrderNeverFlipsAPosition:
    async def test_failed_protective_over_cover_creates_no_short_lot(self, db, journal) -> None:
        """The 14:30 and 15:30 ET sequence: a stop refused for more shares than
        the journal held produced a SHORT OPEN for the excess."""
        await journal.record_trade(_lot(4.0, 163.90))

        ids = await journal.record_trade(
            _stop(16.0, "FAILED"),
            order_status="FAILED",
            order_id="6ab6c111",
            broker_status_reason="Not enough shares to sell",
        )

        assert len(ids) == 1, "one refused order is one row"
        rows = await journal.get_recent_trades(limit=10)
        assert not [r for r in rows if r["action"] == "OPEN" and r["direction"] == "SHORT"], (
            "a refused stop cannot open a short position"
        )
        row = next(r for r in rows if r["id"] == ids[0])
        assert row["order_status"] == "FAILED"
        assert row["action"] == "STOP_LOSS", "the protective label survives"
        assert row["quantity"] == pytest.approx(16.0), "the full requested quantity"
        assert row["realized_pnl"] is None
        assert "OVER_COVER" in (row["broker_status_reason"] or "")

    async def test_the_open_lot_still_stands(self, db, journal) -> None:
        await journal.record_trade(_lot(4.0, 163.90))
        await journal.record_trade(
            _stop(16.0, "FAILED"),
            order_status="FAILED",
            order_id="6ab6c111",
            broker_status_reason="Not enough shares to sell",
        )
        open_trades = await journal.get_open_trades()
        assert sum(t["remaining_quantity"] for t in open_trades) == pytest.approx(4.0)

    async def test_one_broker_order_is_one_row(self, db, journal) -> None:
        """Two close rows shared one broker order id, so a later
        ``update_order_status(order_id)`` could not address either alone."""
        await journal.record_trade(_lot(8.0, 165.10))
        await journal.record_trade(_lot(8.0, 163.90))

        ids = await journal.record_trade(
            _stop(16.0, "PENDING"),
            order_status="PENDING",
            order_id="6ab6b111",
        )
        assert len(ids) == 1
        rows = await journal.get_recent_trades(limit=10)
        by_order = [r for r in rows if r["order_id"] == "6ab6b111"]
        assert len(by_order) == 1
        assert by_order[0]["quantity"] == pytest.approx(16.0)
        assert by_order[0]["realized_pnl"] is None

    async def test_a_filled_close_can_still_flip(self, db, journal) -> None:
        """The flip path is legitimate for a deliberate, executed CLOSE — the
        guard must not delete it."""
        await journal.record_trade(_lot(4.0, 163.90))
        await journal.record_trade(
            TradeProposal(
                ticker="MSTR",
                direction=TradeDirection.LONG,
                action=TradeAction.CLOSE,
                quantity=10.0,
                order_type=OrderType.MARKET,
                limit_price=158.70,
                algo_signal=-0.4,
                hybrid_score=-0.4,
                confidence=0.6,
                regime="trending_bear",
                algo_version="composite_v030",
            ),
            order_status="FILLED",
            order_id="6ab6d222",
        )
        rows = await journal.get_recent_trades(limit=10)
        shorts = [r for r in rows if r["action"] == "OPEN" and r["direction"] == "SHORT"]
        assert len(shorts) == 1
        assert shorts[0]["quantity"] == pytest.approx(6.0)


# ── Finding 3(ii): the claim is re-checkable ───────────────────────────────


class TestAClaimedFillCanBeWalkedBack:
    async def test_a_claimed_fill_is_on_the_recheck_list(self, db, journal) -> None:
        from evotrader.agents import tools

        payload = {
            "ticker": "MSTR",
            "action": "OPEN",
            "direction": "LONG",
            "quantity": 4,
            "order_type": "market",
            "status": "FILLED",
            "order_id": "6ab6e333",
            "limit_price": 163.90,
            "algo_signal": 0.38,
            "hybrid_score": 0.38,
            "confidence": 0.45,
            "regime": "trending_bull",
            "algo_version": "composite_v030",
        }
        tools._journal = journal
        tools._current_session_id = "e37134b3"
        try:
            await tools.record_trade(json.dumps(payload))
        finally:
            tools._journal = None
            tools._current_session_id = None

        awaiting = {r["order_id"] for r in await journal.get_order_ids_awaiting_broker()}
        assert "6ab6e333" in awaiting, (
            "an entry claimed FILLED without evidence must stay re-checkable"
        )

    async def test_the_downgrade_is_restricted_to_claims(self, db, journal) -> None:
        """A fill the broker confirmed must survive the same call that walks
        back a claim, or reconciliation would rewrite real history."""
        await journal.record_trade(
            _lot(4.0, 163.90),
            order_status="FILLED",
            order_id="6ab6f444",
            fill_source="broker",
        )
        rows_changed = await journal.update_order_status(
            order_id="6ab6f444",
            new_status="PENDING",
            only_if_claimed_fill=True,
        )
        assert rows_changed == 0
        row = next(
            r for r in await journal.get_recent_trades(limit=5) if r["order_id"] == "6ab6f444"
        )
        assert row["order_status"] == "FILLED"

    async def test_a_downgrade_clears_the_phantom_pnl(self, db, journal) -> None:
        await journal.record_trade(_lot(4.0, 165.10))
        await journal.record_trade(
            TradeProposal(
                ticker="MSTR",
                direction=TradeDirection.LONG,
                action=TradeAction.CLOSE,
                quantity=4.0,
                order_type=OrderType.MARKET,
                limit_price=149.80,
                algo_signal=0.0,
                hybrid_score=0.0,
                confidence=0.5,
                regime="trending_bull",
                algo_version="composite_v030",
            ),
            order_status="FILLED",
            order_id="6ab69111",
            fill_source="executor_claim",
        )
        before = next(
            r for r in await journal.get_recent_trades(limit=5) if r["order_id"] == "6ab69111"
        )
        assert before["realized_pnl"] is not None, "precondition: phantom P&L exists"

        await journal.update_order_status(
            order_id="6ab69111",
            new_status="PENDING",
            only_if_claimed_fill=True,
        )
        after = next(
            r for r in await journal.get_recent_trades(limit=5) if r["order_id"] == "6ab69111"
        )
        assert after["order_status"] == "PENDING"
        assert after["realized_pnl"] is None, (
            "the phantom P&L headline survived the status correction until this"
        )
