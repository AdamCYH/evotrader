"""Protective coverage is computed, not inferred.

See: data/evolution/reviews/
20261006_225358_protective_coverage_computed_not_inferred.md

Shares held against shares a resting stop would sell is one subtraction, and
nothing did it. Every cycle the strategy agent worked it out by hand from the
positions and the order book, the executor did not work it out at all, and
the post-cycle audit that exists to catch an uncovered position compared each
lot with the ticker's whole stop. So a position bought in two lots (10 shares,
then 4 more) under a stop still sized for the first 10 passed the audit, since
each lot alone fits under 10, and the 4 shares added last had no stop until a
later cycle redid the arithmetic.

One function now does it (``protection_audit.coverage_by_ticker``), and every
reader quotes it: ``get_open_positions``, ``gather_market_data``,
``record_trade`` (as ``coverage_after``, and on the row as
``coverage_at_record``), ``reconcile_pending_orders`` and the audit.

Numbers and the ticker are made up.
"""

from __future__ import annotations

import json

import pytest

from evotrader.db.journal import TradeJournal
from evotrader.db.protection_audit import (
    coverage_by_ticker,
    coverage_record,
    find_protection_gaps,
)
from evotrader.models.trade import OrderType, TradeAction, TradeDirection, TradeProposal


def _lot(trade_id: int, qty: float, **extra) -> dict:
    return {
        "id": trade_id,
        "ticker": "XYZ",
        "direction": "LONG",
        "action": "OPEN",
        "quantity": qty,
        "remaining_quantity": qty,
        "option_id": None,
        **extra,
    }


def _stop(order_id: str, qty: float, **extra) -> dict:
    return {
        "order_id": order_id,
        "ticker": "XYZ",
        "action": "STOP_LOSS",
        "direction": "LONG",
        "quantity": qty,
        "order_type": "stop_market",
        "stop_price": 90.0,
        "time_in_force": "gtc",
        "source": "broker_sync",
        **extra,
    }


def _take_profit(order_id: str, qty: float) -> dict:
    return {
        "order_id": order_id,
        "ticker": "XYZ",
        "action": "TAKE_PROFIT",
        "direction": "LONG",
        "quantity": qty,
        "order_type": "limit",
        "limit_price": 120.0,
        "stop_price": None,
        "time_in_force": "gtc",
        "source": "broker_sync",
    }


# ── The arithmetic ────────────────────────────────────────────────────────


class TestTheArithmetic:
    def test_lots_are_summed_per_ticker(self) -> None:
        """THE REGRESSION CASE: an add under a stop sized for the first lot."""
        block = coverage_by_ticker([_lot(1, 10.0), _lot(2, 4.0)], [_stop("s1", 10.0)])["XYZ"]
        assert block["status"] == "partial"
        assert block["held_qty"] == pytest.approx(14.0)
        assert block["covered_qty"] == pytest.approx(10.0)
        assert block["uncovered_qty"] == pytest.approx(4.0)
        assert block["lot_ids"] == [1, 2]
        assert block["cover_source"] == "broker"

    def test_a_stop_for_the_whole_position_is_full(self) -> None:
        block = coverage_by_ticker([_lot(1, 10.0), _lot(2, 4.0)], [_stop("s1", 14.0)])["XYZ"]
        assert block["status"] == "full"
        assert block["uncovered_qty"] == 0.0
        assert block["gtc_all"] is True

    def test_a_take_profit_is_listed_but_is_not_cover(self) -> None:
        """It protects nothing on the way down (the audit's rule)."""
        block = coverage_by_ticker([_lot(1, 14.0)], [_stop("s1", 13.0), _take_profit("t1", 1.0)])[
            "XYZ"
        ]
        assert block["status"] == "partial"
        assert block["uncovered_qty"] == pytest.approx(1.0)
        assert block["take_profit_qty"] == pytest.approx(1.0)
        assert {o["role"] for o in block["orders"]} == {"stop", "take_profit"}

    def test_a_stop_without_its_level_is_unverified_not_protected(self) -> None:
        block = coverage_by_ticker([_lot(1, 10.0)], [_stop("s1", 10.0, stop_price=None)])["XYZ"]
        assert block["status"] == "unknown"

    def test_a_stop_without_its_time_in_force_is_unverified(self) -> None:
        block = coverage_by_ticker([_lot(1, 10.0)], [_stop("s1", 10.0, time_in_force=None)])["XYZ"]
        assert block["status"] == "unknown"
        assert block["gtc_all"] is False

    def test_a_shortfall_is_never_hidden_behind_unknown(self) -> None:
        block = coverage_by_ticker(
            [_lot(1, 10.0), _lot(2, 4.0)], [_stop("s1", 10.0, time_in_force=None)]
        )["XYZ"]
        assert block["status"] == "partial"
        assert block["uncovered_qty"] == pytest.approx(4.0)

    def test_the_direction_field_does_not_decide_what_a_stop_is(self) -> None:
        """Protective rows record direction inconsistently; the shape decides."""
        block = coverage_by_ticker([_lot(1, 10.0)], [_stop("s1", 10.0, direction="SHORT")])["XYZ"]
        assert block["status"] == "full"

    def test_one_row_per_order_and_the_broker_copy_wins(self) -> None:
        ours = _stop("s1", 14.0, source="journal")
        theirs = _stop("s1", 10.0)
        for book in ([ours, theirs], [theirs, ours]):
            block = coverage_by_ticker([_lot(1, 14.0)], book)["XYZ"]
            assert block["covered_qty"] == pytest.approx(10.0)
            assert block["cover_source"] == "broker"

    def test_stops_from_both_sources_are_mixed(self) -> None:
        """A stop placed this cycle is in the book from our record until the
        next sync; it still counts, and the source says so."""
        block = coverage_by_ticker(
            [_lot(1, 14.0)], [_stop("s1", 10.0), _stop("s2", 4.0, source="journal")]
        )["XYZ"]
        assert block["status"] == "full"
        assert block["cover_source"] == "mixed"

    def test_option_lots_and_option_orders_are_left_out(self) -> None:
        block = coverage_by_ticker(
            [_lot(1, 10.0), _lot(2, 2.0, option_id="opt-1")],
            [_stop("s1", 10.0), _stop("s2", 2.0, option_id="opt-1")],
        )["XYZ"]
        assert block["held_qty"] == pytest.approx(10.0)
        assert block["covered_qty"] == pytest.approx(10.0)
        assert block["status"] == "full"

    def test_a_resting_entry_shows_what_would_be_uncovered(self) -> None:
        entry = {
            "order_id": "e1",
            "ticker": "XYZ",
            "action": "OPEN",
            "quantity": 4.0,
            "order_type": "limit",
            "limit_price": 100.0,
            "source": "journal",
        }
        block = coverage_by_ticker([_lot(1, 10.0)], [_stop("s1", 10.0), entry])["XYZ"]
        assert block["status"] == "full", "covered now"
        assert block["pending_entry_qty"] == pytest.approx(4.0)
        assert block["uncovered_if_entries_fill"] == pytest.approx(4.0)

    def test_no_stop_at_all_is_none(self) -> None:
        block = coverage_by_ticker([_lot(1, 10.0)], [])["XYZ"]
        assert block["status"] == "none"
        assert block["uncovered_qty"] == pytest.approx(10.0)
        assert block["cover_source"] is None

    def test_a_stop_left_after_an_exit_shows_as_flat_with_excess(self) -> None:
        block = coverage_by_ticker([], [_stop("s1", 5.0)])["XYZ"]
        assert block["status"] == "flat"
        assert block["excess_stop_qty"] == pytest.approx(5.0)

    def test_nothing_held_and_nothing_resting_is_absent(self) -> None:
        assert coverage_by_ticker([], []) == {}

    def test_the_journal_record_is_compact_and_names_the_stops(self) -> None:
        block = coverage_by_ticker([_lot(1, 10.0), _lot(2, 4.0)], [_stop("s1", 10.0)])["XYZ"]
        record = json.loads(coverage_record(block))
        assert record["status"] == "partial"
        assert record["uncovered_qty"] == pytest.approx(4.0)
        assert record["stop_order_ids"] == ["s1"]
        assert coverage_record(None) is None


# ── The post-cycle audit sums lots too ────────────────────────────────────


class TestTheAuditSumsLots:
    def test_two_lots_under_a_stop_sized_for_the_first(self) -> None:
        lots = [_lot(1, 10.0), _lot(2, 4.0)]
        # Each lot alone fits under the stop: the old per-lot check passed.
        assert all(lot["remaining_quantity"] <= 10.0 for lot in lots)

        gaps = find_protection_gaps(lots, [], broker_cover={"XYZ": 10.0})
        assert len(gaps) == 1, "one ticker, one gap"
        assert gaps[0]["held_quantity"] == pytest.approx(14.0)
        assert gaps[0]["uncovered_quantity"] == pytest.approx(4.0)
        assert gaps[0]["trade_ids"] == [1, 2]
        assert gaps[0]["trade_id"] == 1

    def test_the_audit_and_the_tools_quote_the_same_number(self) -> None:
        lots = [_lot(1, 10.0), _lot(2, 4.0)]
        book = [_stop("s1", 10.0)]
        gaps = find_protection_gaps(lots, [], broker_cover={"XYZ": 10.0})
        block = coverage_by_ticker(lots, book)["XYZ"]
        assert gaps[0]["uncovered_quantity"] == block["uncovered_qty"]

    def test_an_option_lot_does_not_inflate_the_shares_held(self) -> None:
        lots = [_lot(1, 10.0), _lot(2, 2.0, option_id="opt-1")]
        assert find_protection_gaps(lots, [], broker_cover={"XYZ": 10.0}) == []

    def test_a_covered_multi_lot_position_is_not_a_gap(self) -> None:
        lots = [_lot(1, 10.0), _lot(2, 4.0)]
        assert find_protection_gaps(lots, [], broker_cover={"XYZ": 14.0}) == []


# ── The tools carry it ────────────────────────────────────────────────────


@pytest.fixture
def journal(db) -> TradeJournal:
    return TradeJournal(db)


def _proposal(qty: float) -> TradeProposal:
    return TradeProposal(
        ticker="XYZ",
        direction=TradeDirection.LONG,
        action=TradeAction.OPEN,
        quantity=qty,
        order_type=OrderType.MARKET,
        limit_price=100.0,
        algo_signal=0.3,
        hybrid_score=0.3,
        confidence=0.5,
        regime="trending_bull",
        algo_version="test",
    )


async def _book_with_a_ten_share_stop(journal: TradeJournal) -> None:
    await journal.record_trade(_proposal(10.0))
    await journal.save_pending_order("s1", json.dumps(_stop("s1", 10.0)))


@pytest.fixture
def tools_on(journal):
    from evotrader.agents import tools

    tools._journal = journal
    tools._current_session_id = "test-session"
    tools._open_positions_cache = None
    yield tools
    tools._journal = None
    tools._current_session_id = None
    tools._open_positions_cache = None


_ADD = {
    "ticker": "XYZ",
    "action": "OPEN",
    "direction": "LONG",
    "quantity": 4,
    "order_type": "limit",
    "limit_price": 101.0,
    "order_id": "e1",
    "algo_signal": 0.3,
    "hybrid_score": 0.3,
    "confidence": 0.5,
    "regime": "trending_bull",
    "algo_version": "test",
    "reasoning": "Add 4. Stop resized to cover all 14 shares.",
}


class TestTheToolsCarryIt:
    async def test_get_open_positions_has_the_block(self, journal, tools_on) -> None:
        await _book_with_a_ten_share_stop(journal)
        await journal.record_trade(_proposal(4.0))

        result = await tools_on.get_open_positions()
        block = result["protective_coverage"]["XYZ"]
        assert block["status"] == "partial"
        assert block["uncovered_qty"] == pytest.approx(4.0)

    async def test_record_trade_returns_coverage_after_a_fill(self, journal, tools_on) -> None:
        await _book_with_a_ten_share_stop(journal)
        filled = {
            **_ADD,
            "status": "FILLED",
            "state": "filled",
            "average_price": 100.9,
            "cumulative_quantity": "4.000000",
        }
        result = await tools_on.record_trade(json.dumps(filled))

        assert "error" not in result, result
        after = result["coverage_after"]
        assert after["status"] == "partial"
        assert after["held_qty"] == pytest.approx(14.0)
        assert after["uncovered_qty"] == pytest.approx(4.0)

    async def test_the_row_keeps_the_book_and_the_reasoning_is_untouched(
        self, journal, tools_on
    ) -> None:
        await _book_with_a_ten_share_stop(journal)
        filled = {
            **_ADD,
            "status": "FILLED",
            "state": "filled",
            "average_price": 100.9,
            "cumulative_quantity": "4.000000",
        }
        result = await tools_on.record_trade(json.dumps(filled))

        row = await journal.get_trade_by_id(result["trade_ids"][0])
        assert row["reasoning"] == _ADD["reasoning"], "the agent's text is never edited"
        record = json.loads(row["coverage_at_record"])
        assert record["status"] == "partial"
        assert record["uncovered_qty"] == pytest.approx(4.0)
        assert record["stop_order_ids"] == ["s1"]

    async def test_a_pending_entry_reports_what_a_fill_would_leave(self, journal, tools_on) -> None:
        await _book_with_a_ten_share_stop(journal)
        pending = {**_ADD, "status": "PENDING", "state": "queued"}
        result = await tools_on.record_trade(json.dumps(pending))

        after = result["coverage_after"]
        assert after["status"] == "full", "nothing new is held yet"
        assert after["pending_entry_qty"] == pytest.approx(4.0)
        assert after["uncovered_if_entries_fill"] == pytest.approx(4.0)

    async def test_a_resized_stop_reads_full(self, journal, tools_on) -> None:
        await journal.record_trade(_proposal(10.0))
        await journal.record_trade(_proposal(4.0))
        stop = {
            "ticker": "XYZ",
            "action": "STOP_LOSS",
            "direction": "LONG",
            "quantity": 14,
            "order_type": "stop_market",
            "stop_price": 90.0,
            "time_in_force": "gtc",
            "status": "PENDING",
            "state": "confirmed",
            "order_id": "s2",
            "algo_signal": 0.3,
            "hybrid_score": 0.3,
            "confidence": 0.5,
            "regime": "trending_bull",
            "algo_version": "test",
            "reasoning": "Protective stop for the whole position.",
        }
        result = await tools_on.record_trade(json.dumps(stop))
        assert result["coverage_after"]["status"] == "full"
        assert result["coverage_after"]["cover_source"] == "journal", (
            "placed this cycle: our record until the next sync"
        )


class TestTheOrderBookCarriesQuantities:
    async def test_each_working_order_is_described(self, journal, tools_on, monkeypatch) -> None:
        from evotrader.tools import asset_context

        async def mcp(tool_name: str, arguments: dict) -> dict:
            assert tool_name == "get_equity_orders"
            return {
                "data": {
                    "orders": [
                        {
                            "id": "s1",
                            "state": "confirmed",
                            "side": "sell",
                            "trigger": "stop",
                            "type": "market",
                            "quantity": "10.000000",
                            "stop_price": "90.000000",
                            "price": None,
                            "time_in_force": "gtc",
                        }
                    ]
                }
            }

        monkeypatch.setattr(tools_on, "_call_mcp_tool", mcp)
        monkeypatch.setattr(asset_context, "allowed_tickers", lambda: ("XYZ",))

        book = await tools_on.sync_open_orders_from_broker("acct")
        assert book["order_ids"] == ["s1"]
        [order] = book["orders"]
        assert order["quantity"] == pytest.approx(10.0)
        assert order["stop_price"] == pytest.approx(90.0)
        assert order["time_in_force"] == "gtc"
        assert order["action"] == "STOP_LOSS"

    async def test_reconcile_states_the_coverage(self, journal, tools_on, monkeypatch) -> None:
        from evotrader.tools import asset_context

        await journal.record_trade(_proposal(10.0))
        await journal.record_trade(_proposal(4.0))

        stop = {
            "id": "s1",
            "state": "confirmed",
            "side": "sell",
            "trigger": "stop",
            "type": "market",
            "quantity": "10.000000",
            "stop_price": "90.000000",
            "price": None,
            "time_in_force": "gtc",
        }

        async def mcp(tool_name: str, arguments: dict) -> dict:
            if tool_name == "get_equity_orders":
                return {"data": {"orders": [stop]}}
            return {"data": {"orders": []}}

        async def account() -> str:
            return "acct"

        monkeypatch.setattr(tools_on, "_call_mcp_tool", mcp)
        monkeypatch.setattr(tools_on, "_agentic_account_number", account)
        monkeypatch.setattr(asset_context, "allowed_tickers", lambda: ("XYZ",))

        result = await tools_on.reconcile_pending_orders()
        block = result["protective_coverage"]["XYZ"]
        assert block["status"] == "partial"
        assert block["uncovered_qty"] == pytest.approx(4.0)
        assert result["order_book"]["orders"][0]["quantity"] == pytest.approx(10.0)


async def test_the_column_exists(db) -> None:
    async with db.connection() as conn:
        cur = await conn.execute("PRAGMA table_info(trades)")
        names = {row["name"] for row in await cur.fetchall()}
    assert "coverage_at_record" in names
