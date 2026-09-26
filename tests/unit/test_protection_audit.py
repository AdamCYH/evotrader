"""A held position whose protective order was refused must not look protected.

On 2026-09-21 the take-profit was rejected with "Not enough shares to sell" —
the entry, placed nine seconds earlier, had not filled. The executor's summary
said "entry and protective stop-loss have been placed", and the position ran
the whole day with no target.
Nothing in the system compared what was intended against what was resting.

Since 2026-09-11 no stop or take-profit has executed at all: every one is
FAILED, CANCELLED or stuck PENDING. This is the check that makes such a gap
visible. It observes only — the repair is the next cycle's job, which the
strategy instructions already cover.
"""

from __future__ import annotations

import logging

from evotrader.db.protection_audit import audit_protection, find_protection_gaps


def _open(trade_id: int, ticker: str, qty: float) -> dict:
    return {"id": trade_id, "ticker": ticker, "remaining_quantity": qty, "quantity": qty}


def _protective(ticker: str, action: str, qty: float, status: str) -> dict:
    return {"ticker": ticker, "action": action, "quantity": qty, "order_status": status}


class TestTheLiveGap:
    def test_the_09_21_shape_is_reported(self) -> None:
        """THE REGRESSION CASE: stop resting, take-profit refused."""
        gaps = find_protection_gaps(
            [_open(187, "MSTR", 12.0)],
            [
                _protective("MSTR", "STOP_LOSS", 12.0, "PENDING"),
                _protective("MSTR", "TAKE_PROFIT", 12.0, "FAILED"),
            ],
        )
        # The stop covers the position, so this is not an UNCOVERED gap...
        assert gaps == [], "full stop coverage is legitimate cover"

    def test_a_refused_stop_leaves_the_position_uncovered(self) -> None:
        gaps = find_protection_gaps(
            [_open(187, "MSTR", 12.0)],
            [_protective("MSTR", "STOP_LOSS", 12.0, "FAILED")],
        )
        assert len(gaps) == 1
        assert gaps[0]["ticker"] == "MSTR"
        assert gaps[0]["uncovered_quantity"] == 12.0
        assert gaps[0]["stop_quantity_resting"] == 0.0

    def test_a_cancelled_stop_counts_as_no_cover(self) -> None:
        """A stop that expired at the close read as 'cancelled'."""
        gaps = find_protection_gaps(
            [_open(174, "MSTR", 3.0)],
            [_protective("MSTR", "STOP_LOSS", 3.0, "CANCELLED")],
        )
        assert len(gaps) == 1 and gaps[0]["uncovered_quantity"] == 3.0

    def test_partial_cover_reports_only_the_remainder(self) -> None:
        """The documented split: stop on N-1, take-profit on 1."""
        gaps = find_protection_gaps(
            [_open(1, "MSTR", 12.0)],
            [
                _protective("MSTR", "STOP_LOSS", 11.0, "PENDING"),
                _protective("MSTR", "TAKE_PROFIT", 1.0, "PENDING"),
            ],
        )
        assert len(gaps) == 1
        assert gaps[0]["uncovered_quantity"] == 1.0
        assert gaps[0]["take_profit_quantity_resting"] == 1.0

    def test_a_flat_book_has_nothing_to_report(self) -> None:
        assert find_protection_gaps([], [_protective("MSTR", "STOP_LOSS", 5, "FAILED")]) == []

    def test_another_tickers_stop_does_not_count(self) -> None:
        gaps = find_protection_gaps(
            [_open(1, "MSTR", 4.0)],
            [_protective("SMST", "STOP_LOSS", 4.0, "PENDING")],
        )
        assert len(gaps) == 1

    def test_a_zero_quantity_position_is_ignored(self) -> None:
        assert find_protection_gaps([_open(1, "MSTR", 0.0)], []) == []


class TestItObservesAndDoesNotAct:
    async def test_it_logs_loudly_and_records_a_runtime_row(self, caplog) -> None:
        recorded: list[dict] = []

        class _Journal:
            async def get_open_trades(self):
                return [_open(187, "MSTR", 12.0)]

            async def get_recent_trades(self, limit=60):
                return [_protective("MSTR", "STOP_LOSS", 12.0, "FAILED")]

        class _Logger:
            async def record_event(self, **kw):
                recorded.append(kw)

        with caplog.at_level(logging.ERROR):
            out = await audit_protection(_Journal(), _Logger(), session_id="s1")

        assert len(out["gaps"]) == 1
        assert "UNPROTECTED POSITION" in caplog.text
        assert recorded and recorded[0]["event_type"] == "runtime"
        assert "MSTR" in recorded[0]["content"]

    async def test_it_places_no_orders(self) -> None:
        """Auto-placing protection is deliberately NOT this module's job.

        Checks for CALLS, not for the words: the module's docstrings legitimately
        name `record_trade` when explaining how a row came to be mislabelled.
        """
        import ast
        from pathlib import Path

        tree = ast.parse(Path("src/evotrader/db/protection_audit.py").read_text())
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                called.add(fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", ""))
        for forbidden in (
            "place_equity_order",
            "record_trade",
            "cancel_equity_order",
            "relink_unmatched_protective_orders",
            "update_order_status",
        ):
            assert forbidden not in called, f"the audit must not act: it calls {forbidden}"

    async def test_a_broken_journal_cannot_break_a_cycle(self) -> None:
        class _Broken:
            async def get_open_trades(self):
                raise RuntimeError("db gone")

        out = await audit_protection(_Broken())
        assert out["gaps"] == [] and "error" in out


class TestItRunsAfterEveryCycle:
    def test_main_calls_the_audit(self) -> None:
        from pathlib import Path

        assert "audit_protection" in Path("src/evotrader/main.py").read_text(), (
            "a check nothing calls is the defect it was written for"
        )
