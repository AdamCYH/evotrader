"""Protective orders are good-till-cancelled — enforced in code, not prose.

See: data/evolution/reviews/20260918_195356_20260918_missed_mstr_rally_four_structural_defects.md
(finding 1)

A stop on 2026-09-14 carried ``time_in_force=gtc`` and rested three sessions.
A stop on 2026-09-17, placed by the same agent under the same instructions,
carried nothing, defaulted to a DAY order at the broker, and expired at the
close. Reconcile stamped it "Order cancelled" — indistinguishable from an agent
cancel — and the next morning the strategy agent read "the safety net isn't
there", sold the MSTR long pre-market, and MSTR closed +16% at $153.44. The
TIF was decided by an LLM per call; nothing in code enforced it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from evotrader.callbacks.risk_gate import enforce_protective_time_in_force


class TestStopsAreRewrittenToGtc:
    def test_a_stop_with_no_tif_is_rewritten_to_gtc(self, caplog) -> None:
        """The 09-17 stop's exact shape: a protective stop with no duration."""
        args = {
            "ticker": "MSTR",
            "side": "sell",
            "type": "stop_market",
            "quantity": 3,
            "stop_price": 122.50,
        }
        with caplog.at_level(logging.WARNING):
            violation = enforce_protective_time_in_force(args)
        assert violation is None
        assert args["time_in_force"] == "gtc", "must be injected, not left to the broker"
        assert "injecting" in caplog.text

    @pytest.mark.parametrize("otype", ["stop_market", "stop_limit", "stop", "trailing_stop"])
    def test_every_protective_type_is_covered(self, otype: str) -> None:
        args = {"ticker": "MSTR", "side": "sell", "type": otype, "quantity": 1}
        assert enforce_protective_time_in_force(args) is None
        assert args["time_in_force"] == "gtc"

    def test_gtc_already_present_is_left_alone(self) -> None:
        args = {
            "ticker": "MSTR",
            "side": "sell",
            "type": "stop_market",
            "quantity": 2,
            "time_in_force": "gtc",
        }
        assert enforce_protective_time_in_force(args) is None
        assert args["time_in_force"] == "gtc"

    def test_uppercase_gtc_is_accepted(self) -> None:
        args = {"type": "stop_market", "time_in_force": "GTC"}
        assert enforce_protective_time_in_force(args) is None


class TestDayStopsAreRefused:
    def test_a_stop_with_gfd_is_refused(self) -> None:
        """An explicit day stop is worse than none: the book reads 'covered'
        all day and the position is naked the next morning."""
        args = {
            "ticker": "MSTR",
            "side": "sell",
            "type": "stop_market",
            "quantity": 3,
            "time_in_force": "gfd",
        }
        violation = enforce_protective_time_in_force(args)
        assert violation is not None
        assert "gtc" in violation
        assert "2026-09-17" in violation
        # Refused, NOT silently rewritten — the caller must resubmit correctly.
        assert args["time_in_force"] == "gfd"

    def test_a_stop_with_day_is_refused(self) -> None:
        args = {"type": "stop_limit", "time_in_force": "day"}
        assert enforce_protective_time_in_force(args) is not None


class TestNonProtectiveOrdersAreUntouched:
    """Entries and plain closes may legitimately be day orders."""

    @pytest.mark.parametrize("otype", ["market", "limit"])
    def test_entry_and_close_types_are_not_forced(self, otype: str) -> None:
        args = {"ticker": "MSTR", "side": "buy", "type": otype, "quantity": 3}
        assert enforce_protective_time_in_force(args) is None
        assert "time_in_force" not in args

    def test_a_gfd_limit_is_not_refused(self) -> None:
        args = {"type": "limit", "time_in_force": "gfd"}
        assert enforce_protective_time_in_force(args) is None

    def test_option_orders_are_not_touched(self) -> None:
        """Options carry their own duration on the leg; a different contract."""
        args = {
            "legs": [{"option_id": "abc", "side": "sell", "position_effect": "close"}],
            "type": "stop_market",
            "quantity": 1,
        }
        assert enforce_protective_time_in_force(args) is None
        assert "time_in_force" not in args


class TestExpiredIsNotCancelled:
    """A day order the broker dropped at the close must not read as a decision."""

    def test_no_reason_prior_session_is_expired(self) -> None:
        from evotrader.agents.tools import classify_cancelled_order

        now = datetime(2026, 9, 18, 12, 30, tzinfo=UTC)  # 08:30 ET next morning
        created = datetime(2026, 9, 17, 17, 33, tzinfo=UTC)  # 13:33 ET the day before
        state, reason = classify_cancelled_order(
            {"state": "cancelled", "created_at": created.isoformat()}, now=now
        )
        assert state == "expired"
        assert "expired" in reason.lower() and "gtc" in reason

    def test_a_real_cancel_reason_stays_cancelled(self) -> None:
        from evotrader.agents.tools import classify_cancelled_order

        state, reason = classify_cancelled_order(
            {
                "state": "cancelled",
                "cancel_reason": "user_cancelled",
                "created_at": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
            }
        )
        assert state == "cancelled"
        assert reason == "user_cancelled"

    def test_same_session_no_reason_stays_cancelled(self) -> None:
        """Cancelled intraday with no reason is ambiguous — do not upgrade it."""
        from evotrader.agents.tools import classify_cancelled_order

        now = datetime(2026, 9, 18, 15, 0, tzinfo=UTC)
        state, _ = classify_cancelled_order(
            {
                "state": "cancelled",
                "created_at": datetime(2026, 9, 18, 14, 0, tzinfo=UTC).isoformat(),
            },
            now=now,
        )
        assert state == "cancelled"

    def test_expired_does_not_leave_a_phantom_open_position(self) -> None:
        """EXPIRED must be excluded wherever CANCELLED is when deriving positions."""
        import inspect

        import evotrader.db.journal as j

        src = inspect.getsource(j.TradeJournal.get_open_trades)
        assert "'EXPIRED'" in src, "an expired OPEN order would read as a live position"


class TestTifIsPersisted:
    async def test_time_in_force_round_trips_through_the_journal(self, db) -> None:
        from evotrader.db.journal import TradeJournal
        from evotrader.models.trade import (
            OrderType,
            TradeAction,
            TradeDirection,
            TradeProposal,
        )

        journal = TradeJournal(db)
        open_ids = await journal.record_trade(
            TradeProposal(
                ticker="MSTR",
                direction=TradeDirection.LONG,
                action=TradeAction.OPEN,
                quantity=3.0,
                order_type=OrderType.LIMIT,
                limit_price=132.60,
            )
        )
        stop_ids = await journal.record_trade(
            TradeProposal(
                ticker="MSTR",
                action=TradeAction.STOP_LOSS,
                quantity=3.0,
                order_type=OrderType.STOP,
                stop_price=122.50,
                time_in_force="gtc",
                related_trade_id=open_ids[0],
            )
        )
        recent = await journal.get_recent_trades(limit=10)
        row = next(t for t in recent if t["id"] == stop_ids[0])
        assert row["time_in_force"] == "gtc"

        entry = next(t for t in recent if t["id"] == open_ids[0])
        assert entry["time_in_force"] is None, "unsupplied must read as NULL, not a guess"
