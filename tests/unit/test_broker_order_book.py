"""The order book the agents read comes from the broker, not from our claims.

See: data/evolution/reviews/
20260925_210715_record_trade_trusts_executor_fill_claims_phantom_stop_fills
_and_cost_basis_rebase.md
(findings 3 and 4)

On 2026-09-25 the journal wrongly believed a resting stop had filled, so
``pending_orders`` held nothing and ``get_open_positions`` reported
``pending_count: 0``. The broker had the stop order resting, reserving every share held.

Three consecutive cycles (14:30, 15:30, 17:00 ET) read that zero, concluded the
position was naked, proposed the identical repair stop, and were refused "Not
enough shares to sell" each time. The post-cycle protection audit — the check
that exists precisely to catch an uncovered position — wrote an "UNPROTECTED
POSITION" warning into the handoff on all three, because it was reading the
same corrupted journal. A fourth attempt was carried into Monday in the handoff.

Two independent accounts of the order book existed all along. Only one of them
was ours.
"""

from __future__ import annotations

import json

import pytest

from evotrader.db.protection_audit import (
    cover_from_broker_orders,
    find_protection_gaps,
)

# The live broker answer for the resting stop, shaped as get_equity_orders returns.
_BROKER_STOP = {
    "id": "6ab6b111",
    "state": "confirmed",
    "side": "sell",
    "trigger": "stop",
    "type": "market",
    "quantity": "16.000000",
    "stop_price": "149.800000",
    "price": None,
    "time_in_force": "gtc",
}
_BROKER_FILLED = {**_BROKER_STOP, "id": "6ab69111", "state": "filled"}

_HELD_16 = [
    {
        "id": 187,
        "ticker": "MSTR",
        "direction": "LONG",
        "action": "OPEN",
        "remaining_quantity": 16.0,
        "quantity": 16.0,
        "order_status": "FILLED",
    }
]


class TestABrokerOrderIsTranslatedFaithfully:
    def test_a_sell_with_a_stop_trigger_is_protection(self) -> None:
        from evotrader.agents.tools import _pending_row_from_broker_order

        row = _pending_row_from_broker_order(_BROKER_STOP, "MSTR")
        assert row["action"] == "STOP_LOSS"
        assert row["quantity"] == pytest.approx(16.0)
        assert row["stop_price"] == pytest.approx(149.80)
        assert row["time_in_force"] == "gtc"
        assert row["source"] == "broker_sync"

    def test_a_plain_sell_limit_is_a_take_profit(self) -> None:
        from evotrader.agents.tools import _pending_row_from_broker_order

        row = _pending_row_from_broker_order(
            {
                "id": "x",
                "state": "confirmed",
                "side": "sell",
                "type": "limit",
                "quantity": "12.000000",
                "price": "180.250000",
                "time_in_force": "gtc",
            },
            "MSTR",
        )
        assert row["action"] == "TAKE_PROFIT"
        assert row["limit_price"] == pytest.approx(180.25)

    def test_a_buy_is_an_entry(self) -> None:
        from evotrader.agents.tools import _pending_row_from_broker_order

        row = _pending_row_from_broker_order(
            {
                "id": "x",
                "state": "queued",
                "side": "buy",
                "type": "limit",
                "quantity": "4.000000",
                "price": "163.900000",
            },
            "MSTR",
        )
        assert row["action"] == "OPEN"

    def test_only_working_states_count_as_open(self) -> None:
        from evotrader.agents.tools import _BROKER_OPEN_STATES

        for state in ("unconfirmed", "confirmed", "queued", "partially_filled"):
            assert state in _BROKER_OPEN_STATES
        for state in ("filled", "cancelled", "rejected", "failed"):
            assert state not in _BROKER_OPEN_STATES


class TestTheAuditReadsBrokerCover:
    def test_broker_cover_is_counted_from_broker_rows_only(self) -> None:
        from evotrader.agents.tools import _pending_row_from_broker_order

        ours = {
            "ticker": "MSTR",
            "action": "STOP_LOSS",
            "quantity": 99.0,
            "stop_price": 149.8,
            "source": "journal",
        }
        theirs = _pending_row_from_broker_order(_BROKER_STOP, "MSTR")
        cover = cover_from_broker_orders([ours, theirs])
        assert cover == {"MSTR": pytest.approx(16.0)}, (
            "our own submission record is a claim, not independent evidence"
        )

    def test_no_gap_is_reported_when_the_broker_holds_the_stop(self) -> None:
        """The 14:30, 15:30 and 17:00 ET false alarms."""
        # The journal, corrupted: the stop reads FILLED, so it counts as nothing.
        recent = [
            {
                "id": 194,
                "ticker": "MSTR",
                "action": "STOP_LOSS",
                "quantity": 16.0,
                "stop_price": 149.8,
                "order_status": "FILLED",
                "order_type": "stop",
            }
        ]
        assert find_protection_gaps(_HELD_16, recent), (
            "precondition: the journal alone reports the position naked"
        )
        gaps = find_protection_gaps(_HELD_16, recent, broker_cover={"MSTR": 16.0})
        assert gaps == [], "the broker was holding the full quantity the whole time"

    def test_a_real_gap_is_still_reported(self) -> None:
        gaps = find_protection_gaps(_HELD_16, [], broker_cover={"MSTR": 0.0})
        assert len(gaps) == 1
        assert gaps[0]["uncovered_quantity"] == pytest.approx(16.0)
        assert gaps[0]["cover_source"] == "broker"

    def test_a_partial_broker_cover_reports_the_remainder(self) -> None:
        gaps = find_protection_gaps(_HELD_16, [], broker_cover={"MSTR": 12.0})
        assert len(gaps) == 1
        assert gaps[0]["uncovered_quantity"] == pytest.approx(4.0)

    def test_without_a_broker_answer_the_journal_still_decides(self) -> None:
        """Sim mode, and any cycle whose sync failed. Degraded, but it must say
        which source the verdict came from."""
        gaps = find_protection_gaps(_HELD_16, [])
        assert len(gaps) == 1
        assert gaps[0]["cover_source"] == "journal"
        assert gaps[0]["broker_cover"] is None

    def test_a_disagreement_is_recorded(self) -> None:
        recent = [
            {
                "id": 194,
                "ticker": "MSTR",
                "action": "STOP_LOSS",
                "quantity": 4.0,
                "stop_price": 149.8,
                "order_status": "PENDING",
                "order_type": "stop",
            }
        ]
        gaps = find_protection_gaps(_HELD_16, recent, broker_cover={"MSTR": 8.0})
        assert len(gaps) == 1
        assert gaps[0]["sources_disagree"] is True
        assert gaps[0]["journal_cover"] == pytest.approx(4.0)
        assert gaps[0]["broker_cover"] == pytest.approx(8.0)
        assert gaps[0]["stop_quantity_resting"] == pytest.approx(8.0), (
            "the broker's number is the one the verdict uses"
        )


class TestTheAgentSeesTheTimeInForce:
    def test_time_in_force_survives_into_the_book(self) -> None:
        """Its absence made gtc unverifiable from the order book, so the 10:30
        and 11:30 ET cycles cancelled and re-placed a stop that was already
        correctly gtc."""
        from evotrader.agents.tools import _pending_row_from_broker_order

        stored = json.dumps(_pending_row_from_broker_order(_BROKER_STOP, "MSTR"))
        assert json.loads(stored)["time_in_force"] == "gtc"
