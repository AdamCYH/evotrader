"""Regression tests: trade signal provenance must preserve NULL vs genuine 0.0.

The journal's _insert_single_trade previously used ``or 0.0`` for algo_signal,
hybrid_score, and confidence, collapsing both None (missing) and a real 0.0
signal into the stored value 0.0. This made a dropped field indistinguishable
from a legitimate flat signal.

See: data/evolution/reviews/20260629_213340_trade_signal_provenance_lost_journal_defaults_to_zero.md
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


def _make_proposal(**overrides) -> TradeProposal:
    """Create a TradeProposal with sensible defaults, overridable per-test."""
    defaults = dict(
        ticker="SPY",
        direction=TradeDirection.LONG,
        action=TradeAction.OPEN,
        quantity=10.0,
        order_type=OrderType.MARKET,
        timestamp=datetime(2026, 6, 29, 14, 30, 0, tzinfo=UTC),
    )
    defaults.update(overrides)
    return TradeProposal(**defaults)


class TestSignalProvenanceNullPreservation:
    """Verify that NULL (missing) signals are stored as NULL, not as 0.0."""

    async def test_none_algo_signal_stored_as_null(
        self,
        journal: TradeJournal,
    ) -> None:
        """algo_signal=None must be stored as NULL, not 0.0."""
        proposal = _make_proposal(algo_signal=None, algo_version="v001", regime="range_bound")
        await journal.record_trade(proposal)

        recent = await journal.get_recent_trades(limit=1)
        assert len(recent) == 1
        assert recent[0]["algo_signal"] is None, (
            f"Expected NULL for missing algo_signal, got {recent[0]['algo_signal']!r}"
        )

    async def test_none_hybrid_score_stored_as_null(
        self,
        journal: TradeJournal,
    ) -> None:
        """hybrid_score=None must be stored as NULL, not 0.0."""
        proposal = _make_proposal(hybrid_score=None, algo_version="v001", regime="range_bound")
        await journal.record_trade(proposal)

        recent = await journal.get_recent_trades(limit=1)
        assert recent[0]["hybrid_score"] is None, (
            f"Expected NULL for missing hybrid_score, got {recent[0]['hybrid_score']!r}"
        )

    async def test_genuine_zero_algo_signal_preserved(
        self,
        journal: TradeJournal,
    ) -> None:
        """algo_signal=0.0 (a real flat signal) must be stored as 0.0, not NULL."""
        proposal = _make_proposal(
            algo_signal=0.0,
            hybrid_score=0.0,
            algo_version="v001",
            regime="range_bound",
        )
        await journal.record_trade(proposal)

        recent = await journal.get_recent_trades(limit=1)
        assert recent[0]["algo_signal"] == 0.0
        assert recent[0]["hybrid_score"] == 0.0

    async def test_normal_signal_round_trip(
        self,
        journal: TradeJournal,
    ) -> None:
        """Non-zero signal values survive the insert→read round trip."""
        proposal = _make_proposal(
            algo_signal=0.55,
            hybrid_score=0.65,
            confidence=0.80,
            algo_version="v001",
            regime="range_bound",
        )
        await journal.record_trade(proposal)

        recent = await journal.get_recent_trades(limit=1)
        assert recent[0]["algo_signal"] == pytest.approx(0.55)
        assert recent[0]["hybrid_score"] == pytest.approx(0.65)
        assert recent[0]["confidence"] == pytest.approx(0.80)

    async def test_none_confidence_defaults_to_one(
        self,
        journal: TradeJournal,
    ) -> None:
        """confidence=None should default to 1.0 (safe default), not NULL."""
        proposal = _make_proposal(confidence=None, algo_version="v001", regime="range_bound")
        await journal.record_trade(proposal)

        recent = await journal.get_recent_trades(limit=1)
        assert recent[0]["confidence"] == 1.0


class TestSignalProvenanceWarning:
    """Verify that incomplete provenance triggers a warning log."""

    async def test_warning_on_missing_algo_signal(
        self,
        journal: TradeJournal,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A warning must fire when algo_signal is None."""
        import logging

        with caplog.at_level(logging.WARNING, logger="evotrader.db.journal"):
            proposal = _make_proposal(algo_signal=None)
            await journal.record_trade(proposal)

        assert any("signal provenance incomplete" in rec.message for rec in caplog.records), (
            "Expected a provenance-incomplete warning but none was logged"
        )

    async def test_no_warning_when_complete(
        self,
        journal: TradeJournal,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No warning when all provenance fields are present."""
        import logging

        with caplog.at_level(logging.WARNING, logger="evotrader.db.journal"):
            proposal = _make_proposal(
                algo_signal=0.5,
                algo_version="v001",
                regime="trending_bull",
            )
            await journal.record_trade(proposal)

        provenance_warnings = [
            rec for rec in caplog.records if "signal provenance incomplete" in rec.message
        ]
        assert len(provenance_warnings) == 0, (
            f"Unexpected provenance warning: {provenance_warnings[0].message}"
        )
