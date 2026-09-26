"""Unit tests for the position reconciliation service."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from evotrader.db.connection import Database
from evotrader.db.journal import TradeJournal
from evotrader.db.reconciliation import ReconciliationService
from evotrader.models.trade import OrderType, TradeAction, TradeDirection, TradeProposal


class MockSession:
    def __init__(self, positions: list[dict], option_positions: list[dict] | None = None):
        self.positions = positions
        self.option_positions = option_positions or []

    async def call_tool(self, name: str, arguments: dict) -> MagicMock:
        res = MagicMock()
        res.isError = False
        if name == "get_accounts":
            res.content = [
                MagicMock(
                    text='{"data": {"accounts": [{"account_number": "ACC123", "is_default": true}]}}'
                )
            ]
        elif name == "get_equity_positions":
            res.content = [MagicMock(text=json.dumps({"data": {"positions": self.positions}}))]
        elif name == "get_option_positions":
            res.content = [
                MagicMock(text=json.dumps({"data": {"positions": self.option_positions}}))
            ]
        elif name == "get_equity_quotes":
            res.content = [
                MagicMock(text='{"data": {"results": [{"last_trade_price": "420.50"}]}}')
            ]
        elif name == "get_option_quotes":
            res.content = [
                MagicMock(text='{"data": {"results": [{"adjusted_mark_price": "1.50"}]}}')
            ]
        return res


class MockToolset:
    def __init__(
        self, positions: list[dict] | None = None, option_positions: list[dict] | None = None
    ):
        self.positions = positions or []
        self.option_positions = option_positions or []
        self._mcp_session_manager = MagicMock()
        self._mcp_session_manager.create_session = self._create_session

    async def _create_session(self) -> MockSession:
        return MockSession(self.positions, self.option_positions)

    async def close(self) -> None:
        pass


@pytest.fixture
def journal(db: Database) -> TradeJournal:
    return TradeJournal(db)


@pytest.fixture
def sample_proposal() -> TradeProposal:
    return TradeProposal(
        ticker="SPY",
        direction=TradeDirection.LONG,
        action=TradeAction.OPEN,
        quantity=10.0,
        order_type=OrderType.MARKET,
        hybrid_score=0.65,
        confidence=0.80,
        algo_signal=0.55,
        llm_signal=0.75,
        regime="range_bound",
        algo_version="v001_initial",
        reasoning="Test entry",
        timestamp=datetime.now(UTC),
    )


class TestReconciliationService:
    async def test_reconcile_in_sync(self, journal: TradeJournal) -> None:
        # Broker has 0, DB has 0
        mock_mcp = MockToolset(positions=[])
        service = ReconciliationService(journal, mock_mcp, dry_run=False)

        res = await service.reconcile_positions(ticker="SPY")
        assert res["status"] == "IN_SYNC"
        assert res["broker_qty"] == 0.0
        assert res["db_qty"] == 0.0

    async def test_reconcile_drift_ignored_paper(
        self, journal: TradeJournal, sample_proposal: TradeProposal
    ) -> None:
        # DB has 10 shares open, Broker has 0 shares. dry_run = True
        await journal.record_trade(sample_proposal)

        mock_mcp = MockToolset(positions=[])
        service = ReconciliationService(journal, mock_mcp, dry_run=True)

        res = await service.reconcile_positions(ticker="SPY")
        assert res["status"] == "DRIFT_IGNORED_PAPER"
        assert res["broker_qty"] == 0.0
        assert res["db_qty"] == 10.0

    async def test_reconcile_live_drift_virtual_close(
        self, journal: TradeJournal, sample_proposal: TradeProposal
    ) -> None:
        # DB has 10 shares open, Broker has 0 shares. dry_run = False
        trade_id = await journal.record_trade(sample_proposal)

        # We verify DB has 1 open trade
        open_trades = await journal.get_open_trades()
        assert len(open_trades) == 1

        mock_mcp = MockToolset(positions=[])
        service = ReconciliationService(journal, mock_mcp, dry_run=False)

        # Trigger sync -> should write a virtual close
        res = await service.reconcile_positions(ticker="SPY")
        assert res["status"] == "RECONCILED"
        assert res["reconciled_trades_count"] == 1

        # Verify DB now has 0 open trades
        open_trades = await journal.get_open_trades()
        assert len(open_trades) == 0

        # Verify recent trades includes the close
        recent = await journal.get_recent_trades(limit=1)
        assert recent[0]["action"] == "CLOSE"
        assert recent[0]["related_trade_id"] == trade_id[0]
        assert recent[0]["realized_pnl"] is not None

    async def test_reconcile_excess_broker_shares(self, journal: TradeJournal) -> None:
        # DB has 0 shares, Broker has 10 shares SPY
        # Reconciliation now auto-records excess broker shares as an OPEN trade
        mock_mcp = MockToolset(
            positions=[{"symbol": "SPY", "quantity": "10.0", "average_buy_price": "400.0"}]
        )
        service = ReconciliationService(journal, mock_mcp, dry_run=False)

        res = await service.reconcile_positions(ticker="SPY")
        assert res["status"] == "RECONCILED"
        assert res["reconciled_trades_count"] >= 1

    async def test_reconcile_options_live_drift_virtual_close(
        self, journal: TradeJournal, sample_proposal: TradeProposal
    ) -> None:
        # DB has 1 option contract open, Broker has 0 option contracts. dry_run = False
        option_proposal = sample_proposal.model_copy(
            update={
                "option_id": "opt_contract_123",
                "option_type": "call",
                "strike": 450.0,
                "expiration": "2026-06-26",
                "quantity": 1.0,
            }
        )
        trade_id = await journal.record_trade(option_proposal)

        # We verify DB has 1 open trade
        open_trades = await journal.get_open_trades()
        assert len(open_trades) == 1
        assert open_trades[0]["option_id"] == "opt_contract_123"

        mock_mcp = MockToolset(positions=[], option_positions=[])
        service = ReconciliationService(journal, mock_mcp, dry_run=False)

        # Trigger sync -> should write a virtual close for the option
        res = await service.reconcile_positions(ticker="SPY")
        assert res["status"] == "RECONCILED"
        assert res["reconciled_trades_count"] == 1

        # Verify DB now has 0 open trades
        open_trades = await journal.get_open_trades()
        assert len(open_trades) == 0

        # Verify recent trades includes the option CLOSE
        recent = await journal.get_recent_trades(limit=1)
        assert recent[0]["action"] == "CLOSE"
        assert recent[0]["related_trade_id"] == trade_id[0]
        assert recent[0]["option_id"] == "opt_contract_123"
        assert recent[0]["realized_pnl"] is not None


class TestReconciliationIsNotTickerScoped:
    """Regression: an instrument switch must not orphan the old positions.

    See: data/evolution/reviews/20260914_225229_reconciliation_is_ticker_scoped_and_orphans_positions_on_instrument_switch.md

    Every other test in this file uses one ticker ('SPY') for the journal rows,
    the mock broker AND the `ticker=` argument. A bug whose entire mechanism is
    "journal ticker != configured ticker" cannot be expressed in that fixture
    shape, which is why the defect shipped and ran for ten days at full green.
    """

    async def test_reconcile_does_not_orphan_non_primary_ticker(
        self, journal: TradeJournal, sample_proposal: TradeProposal
    ) -> None:
        """Reproduces the live 2026-09-14 state.

        The journal holds QQQ, the broker holds only MSTR, and the configured
        primary_ticker is MSTR. Before the fix the QQQ row is filtered out of
        both sides of the comparison and survives forever.
        """
        qqq = sample_proposal.model_copy(update={"ticker": "QQQ", "quantity": 4.0})
        await journal.record_trade(qqq)

        mock_mcp = MockToolset(
            positions=[{"symbol": "MSTR", "quantity": "3.0", "average_buy_price": "132.60"}]
        )
        service = ReconciliationService(journal, mock_mcp, dry_run=False)

        res = await service.reconcile_positions(ticker="MSTR")

        # The orphan must have been examined at all...
        assert "QQQ" in res["tickers_examined"]
        # ...and resolved, not skipped.
        assert res["per_ticker"]["QQQ"]["status"] == "RECONCILED"

        open_trades = await journal.get_open_trades()
        assert not [t for t in open_trades if t["ticker"] == "QQQ"], (
            "QQQ ghost survived reconciliation"
        )

    async def test_clean_focus_ticker_does_not_report_a_clean_account(
        self, journal: TradeJournal, sample_proposal: TradeProposal
    ) -> None:
        """IN_SYNC must never mean "in sync for the one ticker I looked at".

        The original failure was silent by construction: MSTR really was in
        sync, and the return value gave no hint that other shares had
        never been examined.
        """
        qqq = sample_proposal.model_copy(update={"ticker": "QQQ", "quantity": 4.0})
        await journal.record_trade(qqq)

        mock_mcp = MockToolset(positions=[])
        service = ReconciliationService(journal, mock_mcp, dry_run=False)

        res = await service.reconcile_positions(ticker="MSTR")
        assert res["status"] != "IN_SYNC", (
            "a clean focus ticker reported a clean account while QQQ drifted"
        )
        assert res["status"] == "RECONCILED_OTHER_TICKER"
        assert "QQQ" in res["message"]

    async def test_broker_held_ticker_absent_from_journal_is_examined(
        self, journal: TradeJournal
    ) -> None:
        """The union must include broker-side symbols too, not just journal ones."""
        mock_mcp = MockToolset(
            positions=[{"symbol": "NVDA", "quantity": "7.0", "average_buy_price": "100.00"}]
        )
        service = ReconciliationService(journal, mock_mcp, dry_run=False)

        res = await service.reconcile_positions(ticker="MSTR")
        assert "NVDA" in res["tickers_examined"]
        assert res["per_ticker"]["NVDA"]["broker_qty"] == 7.0

    async def test_focus_ticker_headline_fields_still_describe_the_focus(
        self, journal: TradeJournal, sample_proposal: TradeProposal
    ) -> None:
        """The focus hint still drives the headline broker_qty/db_qty."""
        spy = sample_proposal.model_copy(update={"ticker": "SPY", "quantity": 10.0})
        await journal.record_trade(spy)

        mock_mcp = MockToolset(
            positions=[{"symbol": "SPY", "quantity": "10.0", "average_buy_price": "420.50"}]
        )
        service = ReconciliationService(journal, mock_mcp, dry_run=False)

        res = await service.reconcile_positions(ticker="SPY")
        assert res["ticker"] == "SPY"
        assert res["broker_qty"] == 10.0
        assert res["db_qty"] == 10.0
        assert res["status"] == "IN_SYNC"
