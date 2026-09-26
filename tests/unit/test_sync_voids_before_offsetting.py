"""The sync corrects a wrong journal row before inventing one to balance it.

See: data/evolution/reviews/
20260925_210715_record_trade_trusts_executor_fill_claims_phantom_stop_fills
_and_cost_basis_rebase.md
(finding 3)

2026-09-25 16:00 ET. The journal held four rows recording exits that had not
happened — a stop the executor called FILLED while the broker held it
unconfirmed, the two chunks of its replacement, and the phantom short the flip
path built from the leftover. The journal therefore believed the account was
short while the broker held a long position.

The sync did not ask whether those rows were true. It measured the gap between
its own wrong total and the broker, and wrote two more rows to close it: a
SHORT CLOSE and a LONG OPEN, both at the current mark. Six untrue rows where
there had been four, and the cost basis moved from the real cost to the mark —
which turned the resting stop from 1.65 ATR below entry into 0.98. The
17:00 ET agent had to work out that the new row's price was "a sync
artifact, not cost basis" before it could size anything.

Two rules follow. Ask the broker about a close row's order before believing it,
because a row whose order never filled is the error and not the drift. And record
a sync OPEN at the cost the broker already told us, never at today's price.
"""

from __future__ import annotations

import pytest

from evotrader.db.journal import TradeJournal
from evotrader.db.reconciliation import ReconciliationService
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


def _close(quantity: float, price: float) -> TradeProposal:
    return TradeProposal(
        ticker="MSTR",
        direction=TradeDirection.LONG,
        action=TradeAction.CLOSE,
        quantity=quantity,
        order_type=OrderType.MARKET,
        limit_price=price,
        algo_signal=0.0,
        hybrid_score=0.0,
        confidence=0.5,
        regime="trending_bull",
        algo_version="composite_v030",
    )


class _Service(ReconciliationService):
    """Reconciliation with the broker's order answers supplied directly.

    Only ``get_order_state`` is replaced — the decision logic under test is the
    real one.
    """

    def __init__(self, journal: TradeJournal, states: dict[str, str | None]) -> None:
        super().__init__(journal=journal, mcp_toolset=None, dry_run=False)
        self._states = states
        self.asked: list[str] = []

    async def get_order_state(self, order_id: str) -> str | None:
        self.asked.append(order_id)
        return self._states.get(order_id)


class TestAWrongRowIsCorrectedNotOffset:
    async def test_a_close_whose_order_never_filled_is_voided(self, db, journal) -> None:
        await journal.record_trade(_lot(12.0, 165.10))
        await journal.record_trade(
            _close(4.0, 149.80),
            order_status="FILLED",
            order_id="6ab69111",
        )
        assert sum(
            t["remaining_quantity"] for t in await journal.get_open_trades()
        ) == pytest.approx(8.0), "precondition: the journal believes 4 were sold"

        svc = _Service(journal, {"6ab69111": "unconfirmed"})
        voided = await svc._void_unfilled_close_rows("MSTR", [])

        assert len(voided) == 1
        row = next(
            r for r in await journal.get_recent_trades(limit=5) if r["order_id"] == "6ab69111"
        )
        assert row["order_status"] == "PENDING"
        assert row["realized_pnl"] is None
        assert "VOIDED_BY_SYNC" in (row["broker_status_reason"] or "")
        assert sum(
            t["remaining_quantity"] for t in await journal.get_open_trades()
        ) == pytest.approx(12.0), "the shares come back to the lot"

    async def test_a_cancelled_order_is_voided_as_cancelled(self, db, journal) -> None:
        await journal.record_trade(_lot(12.0, 165.10))
        await journal.record_trade(
            _close(4.0, 149.80),
            order_status="FILLED",
            order_id="6ab69111",
        )
        svc = _Service(journal, {"6ab69111": "cancelled"})
        await svc._void_unfilled_close_rows("MSTR", [])
        row = next(
            r for r in await journal.get_recent_trades(limit=5) if r["order_id"] == "6ab69111"
        )
        assert row["order_status"] == "CANCELLED"

    async def test_a_genuine_fill_is_left_alone(self, db, journal) -> None:
        await journal.record_trade(_lot(12.0, 165.10))
        await journal.record_trade(
            _close(4.0, 158.70),
            order_status="FILLED",
            order_id="6ab6d999",
        )
        svc = _Service(journal, {"6ab6d999": "filled"})
        voided = await svc._void_unfilled_close_rows("MSTR", [])
        assert voided == []
        row = next(
            r for r in await journal.get_recent_trades(limit=5) if r["order_id"] == "6ab6d999"
        )
        assert row["order_status"] == "FILLED"
        assert row["realized_pnl"] is not None

    async def test_an_unanswerable_lookup_changes_nothing(self, db, journal) -> None:
        """A failed lookup is not evidence. Voiding on it would delete a real
        exit whenever the network hiccuped."""
        await journal.record_trade(_lot(12.0, 165.10))
        await journal.record_trade(
            _close(4.0, 158.70),
            order_status="FILLED",
            order_id="6ab6d999",
        )
        svc = _Service(journal, {"6ab6d999": None})
        assert await svc._void_unfilled_close_rows("MSTR", []) == []
        row = next(
            r for r in await journal.get_recent_trades(limit=5) if r["order_id"] == "6ab6d999"
        )
        assert row["order_status"] == "FILLED"

    async def test_the_whole_09_25_drift_disappears(self, db, journal) -> None:
        """The review's fixture: journal believes net short, broker holds long."""
        await journal.record_trade(_lot(12.0, 165.10))
        await journal.record_trade(_lot(4.0, 163.90))
        # The 11:30 stop, journaled FILLED, broker had it unconfirmed.
        await journal.record_trade(
            _close(4.0, 149.80),
            order_status="FILLED",
            order_id="6ab69111",
        )
        # The 13:30 replacement, same mistake.
        await journal.record_trade(
            _close(12.0, 149.80),
            order_status="FILLED",
            order_id="6ab6b111",
        )
        held = sum(t["remaining_quantity"] for t in await journal.get_open_trades())
        assert held == pytest.approx(0.0), "precondition: the journal reads flat"

        svc = _Service(
            journal,
            {
                "6ab69111": "cancelled",  # cancelled at 13:30 ET
                "6ab6b111": "confirmed",  # still resting at the broker
            },
        )
        await svc._void_unfilled_close_rows("MSTR", [])

        open_trades = await journal.get_open_trades()
        assert sum(t["remaining_quantity"] for t in open_trades) == pytest.approx(16.0)
        # And the blended cost basis is the real one, not the mark.
        blended = sum(t["remaining_quantity"] * float(t["price"]) for t in open_trades) / 16.0
        assert blended == pytest.approx(164.80, abs=0.01)


class TestASyncOpenUsesBrokerAverageCost:
    async def test_it_records_cost_not_the_mark(self, db, journal) -> None:
        svc = _Service(journal, {})
        await svc._record_sync_trade(
            "MSTR",
            "LONG",
            "OPEN",
            16.0,
            158.70,  # today's mark
            None,
            {"option_id": None, "broker_cost": 164.80},
        )
        row = (await journal.get_recent_trades(limit=1))[0]
        assert float(row["price"]) == pytest.approx(164.80)
        assert "broker average cost" in (row["reasoning"] or "")

    async def test_without_a_broker_cost_it_says_so(self, db, journal) -> None:
        """The mark remains a fallback, but the row must not read as a cost
        basis, and it must carry no P&L."""
        svc = _Service(journal, {})
        await svc._record_sync_trade(
            "MSTR",
            "LONG",
            "OPEN",
            16.0,
            158.70,
            None,
            {"option_id": None, "broker_cost": 0.0},
        )
        row = (await journal.get_recent_trades(limit=1))[0]
        assert float(row["price"]) == pytest.approx(158.70)
        assert "NOT a cost basis" in (row["reasoning"] or "")

    async def test_a_sync_close_still_uses_the_mark(self, db, journal) -> None:
        """A CLOSE is an exit at today's price — only an OPEN has a cost basis."""
        await journal.record_trade(_lot(4.0, 165.10))
        lot_id = (await journal.get_open_trades())[0]["id"]
        svc = _Service(journal, {})
        await svc._record_sync_trade(
            "MSTR",
            "LONG",
            "CLOSE",
            4.0,
            158.70,
            lot_id,
            {"option_id": None, "broker_cost": 164.80},
        )
        row = (await journal.get_recent_trades(limit=1))[0]
        assert float(row["price"]) == pytest.approx(158.70)

    async def test_sync_rows_are_marked_as_sync(self, db, journal) -> None:
        svc = _Service(journal, {})
        await svc._record_sync_trade(
            "MSTR",
            "LONG",
            "OPEN",
            16.0,
            158.70,
            None,
            {"option_id": None, "broker_cost": 164.80},
        )
        row = (await journal.get_recent_trades(limit=1))[0]
        assert row["fill_source"] == "sync", (
            "so nothing downstream mistakes a synthesised row for a broker fill"
        )
