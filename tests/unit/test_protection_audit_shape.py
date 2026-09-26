"""The protection audit must recognise a resting stop by what it IS, not its label.

See: data/evolution/reviews/20260922_221300_20260922_protection_audit_false_positive_and_silent_channel_census.md
(finding 1)

On every cycle of 2026-09-22 (9 in a row) the post-cycle audit reported the
whole position "uncovered by a resting stop" — while a stop for the full
position was resting at the broker the whole time. The
strategy agent overrode the false alarm on all 8 regular cycles and wrote in
its handoff that re-placing would double-cover. The check built to be more
reliable than the agent was less reliable than the agent.

Two facts were destroyed before the audit ever saw the row. The executor sent
`action: STOP_LOSS, order_type: stop_market`; the journal stored
`action: CLOSE, order_type: market`:
  - `record_trade`'s unmatched-close branch relabelled the action to CLOSE,
    because the stop was journaled 5 s after an entry that had not filled;
  - `OrderType("stop_market")` raised — the enum says `stop` — the error was
    swallowed, and the order fell through to the MARKET default.
The audit then matched on the label alone and counted nothing.

The review's own proposed fix would have made it worse: it said to count a stop
only when its `direction` is OPPOSITE the held lot. All 39 correctly-labelled
STOP_LOSS / TAKE_PROFIT rows in the live journal carry `direction=LONG` — the
same side as the position. Direction is recorded inconsistently and is not used.
"""

from __future__ import annotations

from evotrader.db.protection_audit import find_protection_gaps

# ── the live rows, verbatim apart from made-up order ids ─────────────────
_LOT_187 = {
    "id": 187,
    "ticker": "MSTR",
    "direction": "LONG",
    "action": "OPEN",
    "remaining_quantity": 12.0,
    "quantity": 12.0,
    "order_status": "FILLED",
}
_STOP_188 = {
    "id": 188,
    "ticker": "MSTR",
    "direction": "SHORT",
    "action": "CLOSE",
    "order_type": "market",
    "quantity": 12.0,
    "stop_price": 149.8,
    "limit_price": None,
    "order_status": "PENDING",
    "broker_status_reason": "UNMATCHED_CLOSE: no open journal lot",
}
_TP_189 = {
    "id": 189,
    "ticker": "MSTR",
    "direction": "SHORT",
    "action": "CLOSE",
    "order_type": "limit",
    "quantity": 12.0,
    "stop_price": None,
    "limit_price": 180.25,
    "order_status": "FAILED",
    "broker_status_reason": "UNMATCHED_CLOSE: no open journal lot",
}


class TestTheLiveFalseAlarm:
    def test_the_09_22_rows_produce_no_gap(self) -> None:
        """THE REGRESSION CASE. Pre-fix this reported the whole position uncovered."""
        assert find_protection_gaps([_LOT_187], [_STOP_188, _TP_189]) == []

    def test_a_relabelled_stop_is_recognised_by_its_stop_price(self) -> None:
        row = {**_STOP_188, "order_type": None}
        assert find_protection_gaps([_LOT_187], [row]) == []

    def test_a_stop_order_type_alone_is_enough(self) -> None:
        row = {**_STOP_188, "stop_price": None, "order_type": "stop_market"}
        assert find_protection_gaps([_LOT_187], [row]) == []


class TestDirectionIsNotUsed:
    """The review proposed requiring direction OPPOSITE the lot. That would have
    rejected every correctly-journaled stop in the live book."""

    def test_a_normal_stop_with_same_side_direction_still_counts(self) -> None:
        normal = {
            "id": 185,
            "ticker": "MSTR",
            "direction": "LONG",
            "action": "STOP_LOSS",
            "order_type": "market",
            "quantity": 12.0,
            "stop_price": 122.5,
            "order_status": "PENDING",
        }
        assert find_protection_gaps([_LOT_187], [normal]) == []

    def test_an_opposite_side_stop_also_counts(self) -> None:
        assert find_protection_gaps([_LOT_187], [_STOP_188]) == []


class TestItStillCatchesRealGaps:
    def test_a_failed_stop_is_not_cover(self) -> None:
        dead = {**_STOP_188, "order_status": "FAILED"}
        gaps = find_protection_gaps([_LOT_187], [dead])
        assert len(gaps) == 1 and gaps[0]["uncovered_quantity"] == 12.0

    def test_a_take_profit_is_not_stop_cover(self) -> None:
        """A resting limit sell above the market reserves shares but protects
        nothing on the way down."""
        tp = {**_TP_189, "order_status": "PENDING"}
        gaps = find_protection_gaps([_LOT_187], [tp])
        assert len(gaps) == 1
        assert gaps[0]["take_profit_quantity_resting"] == 12.0
        assert gaps[0]["stop_quantity_resting"] == 0.0

    def test_a_pending_entry_is_never_mistaken_for_cover(self) -> None:
        """An unfilled BUY limit carries a limit price and rests — and it is an
        entry, not a take-profit or a stop."""
        entry = {
            "id": 190,
            "ticker": "MSTR",
            "direction": "LONG",
            "action": "OPEN",
            "order_type": "limit",
            "quantity": 5.0,
            "limit_price": 160.0,
            "stop_price": 150.0,
            "order_status": "PENDING",
        }
        gaps = find_protection_gaps([_LOT_187], [entry])
        assert len(gaps) == 1 and gaps[0]["stop_quantity_resting"] == 0.0

    def test_another_tickers_stop_is_not_cover(self) -> None:
        other = {**_STOP_188, "ticker": "SMST"}
        assert len(find_protection_gaps([_LOT_187], [other])) == 1


class TestTheReportShowsItsEvidence:
    def test_a_gap_names_the_rows_it_counted(self) -> None:
        """Review (d): the next cycle should see WHICH rows were counted, so a
        wrong verdict is checkable rather than merely contradicted."""
        partial = {**_STOP_188, "quantity": 11.0}
        gaps = find_protection_gaps([_LOT_187], [partial])
        assert gaps[0]["rows_counted"] == [188]

    def test_an_empty_count_is_explicit(self) -> None:
        gaps = find_protection_gaps([_LOT_187], [])
        assert gaps[0]["rows_counted"] == []


class TestTheBrokersWordForAStopIsUnderstood:
    def test_stop_market_parses_as_a_stop(self) -> None:
        """Pre-fix OrderType('stop_market') raised and the order was journaled
        as MARKET."""
        from evotrader.models.trade import OrderType, parse_order_type

        assert parse_order_type("stop_market") == OrderType.STOP
        assert parse_order_type("STOP_MARKET") == OrderType.STOP
        assert parse_order_type("limit") == OrderType.LIMIT
        assert parse_order_type("nonsense") is None
        assert parse_order_type(None) is None

    async def test_the_record_trade_tool_keeps_the_stop_type(self, monkeypatch) -> None:
        """End to end through the tool's JSON parser, with the executor's exact
        payload for the stop."""
        import json

        import evotrader.agents.tools as tools
        from evotrader.models.trade import OrderType

        captured: dict = {}

        class _Journal:
            async def get_open_trades(self):
                return []

            async def record_trade(self, proposal, *a, **kw):
                captured["proposal"] = proposal
                return [1]

        monkeypatch.setattr(tools, "_journal", _Journal())
        await tools.record_trade(
            json.dumps(
                {
                    "ticker": "MSTR",
                    "action": "STOP_LOSS",
                    "direction": "SHORT",
                    "order_type": "stop_market",
                    "quantity": 12,
                    "stop_price": 149.8,
                    "status": "PENDING",
                    "time_in_force": "gtc",
                }
            )
        )
        assert captured["proposal"].order_type == OrderType.STOP, (
            "pre-fix this was MARKET: the broker's word raised and was swallowed"
        )


class TestTheJournalKeepsTheProtectiveLabel:
    async def test_an_unmatched_stop_is_recorded_as_a_stop(self, db) -> None:
        """Review (b). The 09-21 sequence: entry still unconfirmed, stop
        journaled 5 s later. Pre-fix the stop was stored as action=CLOSE."""
        from evotrader.db.journal import TradeJournal
        from evotrader.models.trade import (
            OrderType,
            TradeAction,
            TradeDirection,
            TradeProposal,
        )

        journal = TradeJournal(db)
        await journal.record_trade(
            TradeProposal(
                ticker="MSTR",
                direction=TradeDirection.LONG,
                action=TradeAction.OPEN,
                quantity=12.0,
                order_type=OrderType.LIMIT,
                limit_price=165.15,
            ),
            order_status="PENDING",
            order_id="6ab12111",
        )
        ids = await journal.record_trade(
            TradeProposal(
                ticker="MSTR",
                direction=TradeDirection.SHORT,
                action=TradeAction.STOP_LOSS,
                quantity=12.0,
                order_type=OrderType.STOP,
                stop_price=149.8,
                time_in_force="gtc",
            ),
            order_status="PENDING",
            order_id="6ab12222",
        )
        row = next(r for r in await journal.get_recent_trades(limit=10) if r["id"] == ids[0])
        assert row["action"] == "STOP_LOSS", "the protective label must survive the unmatched path"
        assert row["order_type"] == "stop"
        assert str(row["broker_status_reason"]).startswith("UNMATCHED_CLOSE"), (
            "the audit fact is still recorded — in the reason, where it belongs"
        )

    async def test_a_genuine_unmatched_close_is_still_a_close(self, db) -> None:
        from evotrader.db.journal import TradeJournal
        from evotrader.models.trade import (
            OrderType,
            TradeAction,
            TradeDirection,
            TradeProposal,
        )

        journal = TradeJournal(db)
        ids = await journal.record_trade(
            TradeProposal(
                ticker="MSTR",
                direction=TradeDirection.LONG,
                action=TradeAction.CLOSE,
                quantity=3.0,
                order_type=OrderType.LIMIT,
                limit_price=150.0,
            ),
            order_status="PENDING",
            order_id="x1",
        )
        row = next(r for r in await journal.get_recent_trades(limit=10) if r["id"] == ids[0])
        assert row["action"] == "CLOSE"


class TestUnmatchedProtectionIsRelinkedOnceTheEntryFills:
    """Review (c). An unlinked stop that later FILLS never reduces its lot, so
    the journal would keep showing the shares after the broker sold them."""

    async def _live_sequence(self, db):
        from evotrader.db.journal import TradeJournal
        from evotrader.models.trade import (
            OrderType,
            TradeAction,
            TradeDirection,
            TradeProposal,
        )

        journal = TradeJournal(db)
        entry = await journal.record_trade(
            TradeProposal(
                ticker="MSTR",
                direction=TradeDirection.LONG,
                action=TradeAction.OPEN,
                quantity=12.0,
                order_type=OrderType.LIMIT,
                limit_price=165.15,
            ),
            order_status="PENDING",
            order_id="6ab12111",
        )
        stop = await journal.record_trade(
            TradeProposal(
                ticker="MSTR",
                direction=TradeDirection.SHORT,
                action=TradeAction.STOP_LOSS,
                quantity=12.0,
                order_type=OrderType.STOP,
                stop_price=149.8,
            ),
            order_status="PENDING",
            order_id="6ab12222",
        )
        await journal.update_order_status(
            "6ab12111", "FILLED", fill_price=165.10, filled_quantity=12.0
        )
        return journal, entry[0], stop[0]

    async def test_the_stop_is_linked_to_the_filled_lot(self, db) -> None:
        journal, entry_id, stop_id = await self._live_sequence(db)
        n = await journal.relink_unmatched_protective_orders()
        assert n == 1
        row = next(r for r in await journal.get_recent_trades(limit=10) if r["id"] == stop_id)
        assert row["related_trade_id"] == entry_id
        assert f"RELINKED_TO_{entry_id}" in row["broker_status_reason"]

    async def test_when_the_linked_stop_fills_the_lot_closes(self, db) -> None:
        """The accounting this exists for: a stop-out must leave zero shares."""
        journal, entry_id, _ = await self._live_sequence(db)
        await journal.relink_unmatched_protective_orders()
        await journal.update_order_status(
            "6ab12222", "FILLED", fill_price=149.8, filled_quantity=12.0
        )
        open_lots = [t for t in await journal.get_open_trades() if t["id"] == entry_id]
        assert open_lots == [], "the stop-out must close the lot, not leave a phantom"

    async def test_it_is_idempotent(self, db) -> None:
        journal, _, _ = await self._live_sequence(db)
        assert await journal.relink_unmatched_protective_orders() == 1
        assert await journal.relink_unmatched_protective_orders() == 0

    async def test_it_does_not_guess_between_two_lots(self, db) -> None:
        """Two open lots that could each absorb the stop: linking either one is
        a guess, and a wrong link misstates a position."""
        from evotrader.models.trade import OrderType, TradeAction, TradeDirection, TradeProposal

        journal, _, stop_id = await self._live_sequence(db)
        await journal.record_trade(
            TradeProposal(
                ticker="MSTR",
                direction=TradeDirection.LONG,
                action=TradeAction.OPEN,
                quantity=12.0,
                order_type=OrderType.LIMIT,
                limit_price=166.0,
            ),
            order_status="FILLED",
            order_id="second",
        )
        assert await journal.relink_unmatched_protective_orders() == 0
        row = next(r for r in await journal.get_recent_trades(limit=10) if r["id"] == stop_id)
        assert row["related_trade_id"] is None

    async def test_a_dead_order_is_not_relinked(self, db) -> None:
        journal, _, _stop_id = await self._live_sequence(db)
        await journal.update_order_status("6ab12222", "FAILED")
        assert await journal.relink_unmatched_protective_orders() == 0

    def test_the_cycle_runs_it_before_the_audit(self) -> None:
        from pathlib import Path

        src = Path("src/evotrader/main.py").read_text()
        assert "relink_unmatched_protective_orders" in src
        assert src.index("relink_unmatched_protective_orders") < src.index("audit_protection("), (
            "the audit must read the corrected journal, not the one it is about to fix"
        )
