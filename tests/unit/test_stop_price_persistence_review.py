"""Regression tests: the stop TRIGGER price must be stored structurally.

See: data/evolution/reviews/20260908_210841_unverifiable_stop_price_and_range_bound_composite_dilution.md

Findings 1 and 2. The stop trigger price was never persisted in any structured
field — it survived only inside free-text `reasoning`, while the `price` column
on a STOP_LOSS row held the related lot's ENTRY price, which reads as a
plausible-but-wrong stop level.

Measured consequence: across eight live cycles the Strategy Agent kept changing
the level it asserted for one single resting stop, and finally conceded it
could not see the trigger price at all.
The STOPS instruction "verify the stop actually rests" was unsatisfiable.

The root cause was deeper than a missing column: `stop_price` was never passed
from the record_trade tool payload into `TradeProposal`, so it was None on
every write. A column alone would have stayed permanently NULL.
"""

from __future__ import annotations

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


def _stop_proposal(stop: float | None, entry_related: int | None = None) -> TradeProposal:
    return TradeProposal(
        ticker="QQQ",
        direction=TradeDirection.LONG,
        action=TradeAction.STOP_LOSS,
        quantity=4.0,
        order_type=OrderType.STOP,
        stop_price=stop,
        regime="range_bound",
        algo_version="v023_staleness_annihilation_fix",
        algo_signal=0.0,
        reasoning="Protective stop for the 4-share QQQ book at 702.00",
        related_trade_id=entry_related,
        timestamp=datetime.now(UTC),
    )


class TestStopPricePersistence:
    async def test_stop_price_column_exists(self, journal: TradeJournal) -> None:
        async with journal._db.connection() as conn:
            cursor = await conn.execute("PRAGMA table_info(trades)")
            cols = {r["name"] for r in await cursor.fetchall()}
        assert "stop_price" in cols

    async def test_stop_price_round_trips(self, journal: TradeJournal) -> None:
        """The level the agent set must be the level it reads back."""
        entry = TradeProposal(
            ticker="QQQ",
            direction=TradeDirection.LONG,
            action=TradeAction.OPEN,
            quantity=4.0,
            order_type=OrderType.MARKET,
            limit_price=716.97,
            regime="range_bound",
            algo_version="v023",
            algo_signal=0.1,
            reasoning="entry",
            timestamp=datetime.now(UTC),
        )
        await journal.record_trade(entry)
        await journal.record_trade(_stop_proposal(702.00))

        rows = await journal.get_recent_trades(limit=5)
        stop_rows = [r for r in rows if r["action"] == "STOP_LOSS"]
        assert stop_rows, "stop row not journaled"
        assert stop_rows[0]["stop_price"] == pytest.approx(702.00)

    async def test_stop_price_is_not_the_entry_price(self, journal: TradeJournal) -> None:
        """The exact confusion from the review: the entry price read as the stop.

        Reading `price` off a STOP_LOSS row gives the related lot's entry, which
        is a plausible-looking wrong answer. `stop_price` must disagree with it.
        """
        # A stop whose proposal ALSO carries the related lot's entry as a
        # limit price: `price` resolves to 716.97, the stop rests at 702.00.
        prop = _stop_proposal(702.00)
        prop.limit_price = 716.97
        await journal.record_trade(prop)
        row = (await journal.get_recent_trades(limit=1))[0]
        assert row["stop_price"] == pytest.approx(702.00)
        assert row["price"] == pytest.approx(716.97)

    async def test_missing_stop_price_stays_null_not_fabricated(
        self, journal: TradeJournal
    ) -> None:
        """Unknown must read as unknown, never as a guessed level."""
        prop = _stop_proposal(None)
        prop.limit_price = 716.97
        await journal.record_trade(prop)
        row = (await journal.get_recent_trades(limit=1))[0]
        assert row["stop_price"] is None

    async def test_stop_order_with_a_price_is_not_flagged_price_unavailable(
        self, journal: TradeJournal
    ) -> None:
        """Finding 2's root cause.

        A stop carrying a real trigger price yields a derivable price, so it
        must NOT pick up the PRICE_UNAVAILABLE tag that seven historically
        FAILED stop orders carry. Sharing that string made a live resting order
        indistinguishable from a failed one.
        """
        # No fill and no limit price: the ONLY derivable price is the stop
        # trigger. Before the fix that was None, so the row was tagged
        # PRICE_UNAVAILABLE — the same string seven failed stops carry.
        await journal.record_trade(_stop_proposal(702.00), order_status="PENDING")
        row = (await journal.get_recent_trades(limit=1))[0]
        reason = row["broker_status_reason"] or ""
        assert "PRICE_UNAVAILABLE" not in reason, (
            f"live pending stop tagged with a failure reason: {reason!r}"
        )


class TestStopPriceReachesTheProposal:
    """The root cause: `stop_price` never left the tool payload.

    `TradeProposal.stop_price` has existed all along and `record_trade` already
    consulted it — but the record_trade TOOL never populated it, so it was None
    on every write. An existing test even passes `stop_price: 698.0` in its
    payload without ever asserting the value survives, which is how this went
    unnoticed. Adding the DB column without this half would have produced a
    column that is permanently NULL.
    """

    async def test_stop_price_survives_the_tool_boundary(self) -> None:
        import json
        from unittest.mock import AsyncMock, patch

        from evotrader.agents import tools as tools_module

        mock_journal = AsyncMock()
        mock_journal.get_open_trades.return_value = []
        mock_journal.record_trade.return_value = [201]
        mock_journal.save_pending_order = AsyncMock()

        payload = {
            "ticker": "QQQ",
            "side": "sell",
            "order_type": "stop",
            "stop_price": 702.00,
            "quantity": 4.0,
            "reasoning": "Protective stop at 702.00",
            "algo_signal": 0.0,
            "regime": "range_bound",
            "algo_version": "v023",
        }
        with patch.object(tools_module, "_journal", mock_journal):
            res = await tools_module.record_trade(json.dumps(payload))
            assert "error" not in res
            proposal = mock_journal.record_trade.call_args.kwargs["proposal"]
            assert proposal.stop_price == pytest.approx(702.00)

    async def test_stop_price_accepts_the_aliases_agents_actually_emit(self) -> None:
        import json
        from unittest.mock import AsyncMock, patch

        from evotrader.agents import tools as tools_module

        for key in ("stop_price", "trigger_price", "stop"):
            mock_journal = AsyncMock()
            mock_journal.get_open_trades.return_value = []
            mock_journal.record_trade.return_value = [202]
            mock_journal.save_pending_order = AsyncMock()
            payload = {
                "ticker": "QQQ",
                "side": "sell",
                "order_type": "stop",
                key: 702.00,
                "quantity": 4.0,
                "reasoning": "stop",
                "regime": "range_bound",
                "algo_version": "v023",
            }
            with patch.object(tools_module, "_journal", mock_journal):
                await tools_module.record_trade(json.dumps(payload))
                proposal = mock_journal.record_trade.call_args.kwargs["proposal"]
                assert proposal.stop_price == pytest.approx(702.00), f"alias {key} dropped"

    async def test_stop_price_tolerates_llm_commentary(self) -> None:
        """Agents emit values like "702.00 (1.5 ATR below entry)"."""
        import json
        from unittest.mock import AsyncMock, patch

        from evotrader.agents import tools as tools_module

        mock_journal = AsyncMock()
        mock_journal.get_open_trades.return_value = []
        mock_journal.record_trade.return_value = [203]
        mock_journal.save_pending_order = AsyncMock()
        payload = {
            "ticker": "QQQ",
            "side": "sell",
            "order_type": "stop",
            "stop_price": "702.00 (1.5 ATR below entry)",
            "quantity": 4.0,
            "reasoning": "stop",
            "regime": "range_bound",
            "algo_version": "v023",
        }
        with patch.object(tools_module, "_journal", mock_journal):
            await tools_module.record_trade(json.dumps(payload))
            proposal = mock_journal.record_trade.call_args.kwargs["proposal"]
            assert proposal.stop_price == pytest.approx(702.00)


class TestAgentVisibility:
    """The agent must be able to SEE the trigger price, not infer it.

    Note the review's proposed diff read `row["stop_price"]` here. That is the
    wrong source: `pending_orders` rows carry only order_id/session_id/
    trade_json/status/created_at, and the order detail lives inside the
    `trade_json` blob. Reading it off the row would have raised KeyError.
    """

    async def test_pending_orders_payload_carries_the_stop_price(self) -> None:
        import json
        from unittest.mock import AsyncMock, patch

        from evotrader.agents import tools as tools_module

        mock_journal = AsyncMock()
        mock_journal.get_open_trades.return_value = []
        mock_journal.get_pending_orders.return_value = [
            {
                "order_id": "6a9eb111",
                "session_id": "s1",
                "trade_json": json.dumps(
                    {
                        "ticker": "QQQ",
                        "action": "STOP_LOSS",
                        "quantity": 4.0,
                        "order_type": "stop_market",
                        "limit_price": None,
                        "stop_price": 702.00,
                    }
                ),
                "status": "PENDING",
                "created_at": "2026-09-08T20:00:00Z",
            }
        ]
        with patch.object(tools_module, "_journal", mock_journal):
            tools_module._open_positions_cache = None
            tools_module._open_positions_cache_ts = 0.0
            result = await tools_module.get_open_positions()

        order = result["pending_orders"][0]
        assert order["stop_price"] == pytest.approx(702.00)
        # The live order that triggered this review had limit_price null and no
        # stop field at all — the agent could see the order but not its trigger.
        assert order["limit_price"] is None

    async def test_absent_stop_price_reads_as_none_not_as_the_limit(self) -> None:
        import json
        from unittest.mock import AsyncMock, patch

        from evotrader.agents import tools as tools_module

        mock_journal = AsyncMock()
        mock_journal.get_open_trades.return_value = []
        mock_journal.get_pending_orders.return_value = [
            {
                "order_id": "abc",
                "session_id": "s1",
                "trade_json": json.dumps(
                    {
                        "ticker": "QQQ",
                        "action": "STOP_LOSS",
                        "quantity": 4.0,
                        "order_type": "stop_market",
                        "limit_price": 716.97,
                    }
                ),
                "status": "PENDING",
                "created_at": "2026-09-08T20:00:00Z",
            }
        ]
        with patch.object(tools_module, "_journal", mock_journal):
            tools_module._open_positions_cache = None
            tools_module._open_positions_cache_ts = 0.0
            result = await tools_module.get_open_positions()

        order = result["pending_orders"][0]
        assert order["stop_price"] is None, "an unknown stop must not borrow the limit price"
