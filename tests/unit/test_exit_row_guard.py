"""The exit-row guard must fire on the worst case, not just the tidy ones.

See: data/evolution/reviews/20260914_225229_reconciliation_is_ticker_scoped_and_orphans_positions_on_instrument_switch.md

`_warn_on_exit_row_shape` was added on 2026-09-11 so that blocked-exit analysis
could be automated. Three days later it failed to fire on the first real
instance of the defect class it was written for.

A broker-accepted stop_market was journaled with a row that read
`order_type='limit'` (inherited from the LIMIT entry), `stop_price=NULL`,
`limit_price=NULL`, and `price` equal to the ENTRY price. Not one warning was
emitted, because rule 1 required a limit_price and rule 2's stop branch
required the order_type to already BE a stop type.

A stop recorded at break-even is not an obviously missing value. It is a
plausible and wrong one, which is worse than a NULL: a NULL invites a lookup.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from evotrader.db.connection import Database
from evotrader.db.journal import TradeJournal
from evotrader.models.trade import (
    OrderType,
    TradeAction,
    TradeDirection,
    TradeProposal,
)


@pytest.fixture
def journal(db: Database) -> TradeJournal:
    return TradeJournal(db)


@pytest.fixture
def limit_entry() -> TradeProposal:
    """A LIMIT entry — the shape that poisoned the live stop-loss child."""
    return TradeProposal(
        ticker="MSTR",
        direction=TradeDirection.LONG,
        action=TradeAction.OPEN,
        quantity=9.0,
        order_type=OrderType.LIMIT,
        limit_price=132.60,
        algo_version="v026",
        regime="trending_bull",
        reasoning="Entry",
        timestamp=datetime.now(UTC),
    )


def _priceless_stop(open_id: int) -> TradeProposal:
    """The live case exactly: a protective stop carrying no price at all."""
    return TradeProposal(
        ticker="MSTR",
        action=TradeAction.STOP_LOSS,
        quantity=2.0,
        related_trade_id=open_id,
        timestamp=datetime.now(UTC),
    )


class TestRuleZeroPricelessProtectiveExit:
    async def test_priceless_stop_warns(
        self, journal: TradeJournal, limit_entry: TradeProposal, caplog
    ) -> None:
        open_ids = await journal.record_trade(limit_entry)
        with caplog.at_level(logging.WARNING):
            await journal.record_trade(_priceless_stop(open_ids[0]))
        assert "UNRECOVERABLE" in caplog.text

    async def test_priceless_take_profit_warns(
        self, journal: TradeJournal, limit_entry: TradeProposal, caplog
    ) -> None:
        open_ids = await journal.record_trade(limit_entry)
        tp = TradeProposal(
            ticker="MSTR",
            action=TradeAction.TAKE_PROFIT,
            quantity=2.0,
            related_trade_id=open_ids[0],
        )
        with caplog.at_level(logging.WARNING):
            await journal.record_trade(tp)
        assert "UNRECOVERABLE" in caplog.text

    async def test_stop_with_a_real_stop_price_does_not_warn(
        self, journal: TradeJournal, limit_entry: TradeProposal, caplog
    ) -> None:
        """The guard must stay quiet on a correctly-formed protective exit."""
        open_ids = await journal.record_trade(limit_entry)
        good = TradeProposal(
            ticker="MSTR",
            action=TradeAction.STOP_LOSS,
            quantity=2.0,
            order_type=OrderType.STOP,
            stop_price=118.30,
            related_trade_id=open_ids[0],
        )
        with caplog.at_level(logging.WARNING):
            await journal.record_trade(good)
        assert "UNRECOVERABLE" not in caplog.text


class TestOrderTypeIsNotInherited:
    async def test_stop_child_of_a_limit_entry_is_not_recorded_as_limit(
        self, journal: TradeJournal, limit_entry: TradeProposal
    ) -> None:
        """Five live stop rows all read order_type='limit'."""
        open_ids = await journal.record_trade(limit_entry)
        close_ids = await journal.record_trade(_priceless_stop(open_ids[0]))

        recent = await journal.get_recent_trades(limit=10)
        row = next(t for t in recent if t["id"] == close_ids[0])
        assert row["order_type"] != "limit", (
            "a stop_market must never inherit 'limit' from its entry"
        )

    async def test_position_context_still_inherits(
        self, journal: TradeJournal, limit_entry: TradeProposal
    ) -> None:
        """Removing order_type must not break the inheritance that is correct."""
        open_ids = await journal.record_trade(limit_entry)
        close_ids = await journal.record_trade(_priceless_stop(open_ids[0]))

        recent = await journal.get_recent_trades(limit=10)
        row = next(t for t in recent if t["id"] == close_ids[0])
        assert row["algo_version"] == "v026"
        assert row["regime"] == "trending_bull"
        assert row["direction"] == "LONG"


class TestEntryPriceFallbackIsSelfDescribing:
    async def test_fallback_row_is_marked(
        self, journal: TradeJournal, limit_entry: TradeProposal
    ) -> None:
        """PRICE_UNAVAILABLE says the P&L is NULL. It does not say the `price`
        column holds the ENTRY price — which reads as a stop at break-even."""
        open_ids = await journal.record_trade(limit_entry)
        close_ids = await journal.record_trade(_priceless_stop(open_ids[0]))

        recent = await journal.get_recent_trades(limit=10)
        row = next(t for t in recent if t["id"] == close_ids[0])
        assert "PRICE_FROM_ENTRY_FALLBACK" in (row["broker_status_reason"] or "")
        # The misleading value is still recorded — a refused write would lose
        # the audit trail — but it is now labelled as a placeholder.
        assert row["price"] == pytest.approx(132.60)
        assert row["realized_pnl"] is None

    async def test_priced_exit_is_not_marked(
        self, journal: TradeJournal, limit_entry: TradeProposal
    ) -> None:
        open_ids = await journal.record_trade(limit_entry)
        good = TradeProposal(
            ticker="MSTR",
            action=TradeAction.STOP_LOSS,
            quantity=2.0,
            order_type=OrderType.STOP,
            stop_price=118.30,
            related_trade_id=open_ids[0],
        )
        close_ids = await journal.record_trade(good)
        recent = await journal.get_recent_trades(limit=10)
        row = next(t for t in recent if t["id"] == close_ids[0])
        assert "PRICE_FROM_ENTRY_FALLBACK" not in (row["broker_status_reason"] or "")
        assert row["price"] == pytest.approx(118.30)


class TestQuantityGuardStillFiresOnce:
    async def test_oversized_exit_warns(
        self, journal: TradeJournal, limit_entry: TradeProposal, caplog
    ) -> None:
        open_ids = await journal.record_trade(limit_entry)
        too_big = TradeProposal(
            ticker="MSTR",
            action=TradeAction.CLOSE,
            quantity=99.0,
            order_type=OrderType.MARKET,
            limit_price=137.00,
            related_trade_id=open_ids[0],
        )
        with caplog.at_level(logging.WARNING):
            await journal.record_trade(too_big)
        assert "exceeds the" in caplog.text
        # Once for the order, not once per matched lot.
        assert caplog.text.count("FIFO will flip the excess") == 1
