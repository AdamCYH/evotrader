"""Tests for cash adjustment CRUD and cumulative calculation in MetricsStore."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from evotrader.db.connection import Database
from evotrader.db.metrics import MetricsStore


@pytest_asyncio.fixture
async def metrics_store(tmp_path: Path):
    """Stand up a fresh DB with the cash_adjustments table."""
    db = Database(tmp_path / "test.db")
    await db.initialize()

    # Create the table directly (migration runner not used in unit tests)
    async with db.transaction() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cash_adjustments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                date       TEXT    NOT NULL,
                amount     REAL    NOT NULL,
                note       TEXT    NOT NULL DEFAULT '',
                created_at TEXT    NOT NULL
            )
            """
        )
    yield MetricsStore(db)
    await db.close()


class TestCashAdjustmentCRUD:
    """CRUD operations on the cash_adjustments table."""

    @pytest.mark.asyncio
    async def test_save_and_get(self, metrics_store: MetricsStore) -> None:
        row_id = await metrics_store.save_cash_adjustment("2026-07-01", 500.0, "Monthly deposit")
        assert row_id is not None and row_id > 0

        adjustments = await metrics_store.get_cash_adjustments()
        assert len(adjustments) == 1
        adj = adjustments[0]
        assert adj["date"] == "2026-07-01"
        assert adj["amount"] == 500.0
        assert adj["note"] == "Monthly deposit"
        assert adj["id"] == row_id

    @pytest.mark.asyncio
    async def test_save_withdrawal(self, metrics_store: MetricsStore) -> None:
        await metrics_store.save_cash_adjustment("2026-07-05", -200.0, "Withdrawal")
        adjustments = await metrics_store.get_cash_adjustments()
        assert len(adjustments) == 1
        assert adjustments[0]["amount"] == -200.0

    @pytest.mark.asyncio
    async def test_delete(self, metrics_store: MetricsStore) -> None:
        row_id = await metrics_store.save_cash_adjustment("2026-07-01", 500.0)
        assert await metrics_store.delete_cash_adjustment(row_id)

        adjustments = await metrics_store.get_cash_adjustments()
        assert len(adjustments) == 0

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, metrics_store: MetricsStore) -> None:
        result = await metrics_store.delete_cash_adjustment(9999)
        assert result is False

    @pytest.mark.asyncio
    async def test_ordering(self, metrics_store: MetricsStore) -> None:
        """Adjustments are returned in date order."""
        await metrics_store.save_cash_adjustment("2026-07-10", 100.0)
        await metrics_store.save_cash_adjustment("2026-07-01", 500.0)
        await metrics_store.save_cash_adjustment("2026-07-05", -200.0)

        adjustments = await metrics_store.get_cash_adjustments()
        dates = [a["date"] for a in adjustments]
        assert dates == ["2026-07-01", "2026-07-05", "2026-07-10"]


class TestCumulativeAdjustments:
    """Test the cumulative sum computation used by the equity curve."""

    @pytest.mark.asyncio
    async def test_empty(self, metrics_store: MetricsStore) -> None:
        result = await metrics_store.get_cumulative_adjustments_by_date()
        assert result == {}

    @pytest.mark.asyncio
    async def test_single_deposit(self, metrics_store: MetricsStore) -> None:
        await metrics_store.save_cash_adjustment("2026-07-01", 500.0)
        result = await metrics_store.get_cumulative_adjustments_by_date()
        assert result == {"2026-07-01": 500.0}

    @pytest.mark.asyncio
    async def test_multiple_dates(self, metrics_store: MetricsStore) -> None:
        await metrics_store.save_cash_adjustment("2026-07-01", 500.0)
        await metrics_store.save_cash_adjustment("2026-07-05", 300.0)
        await metrics_store.save_cash_adjustment("2026-07-10", -200.0)

        result = await metrics_store.get_cumulative_adjustments_by_date()
        assert result["2026-07-01"] == 500.0
        assert result["2026-07-05"] == 800.0  # 500 + 300
        assert result["2026-07-10"] == 600.0  # 800 - 200

    @pytest.mark.asyncio
    async def test_multiple_on_same_date(self, metrics_store: MetricsStore) -> None:
        """Multiple adjustments on the same date are summed together."""
        await metrics_store.save_cash_adjustment("2026-07-01", 500.0)
        await metrics_store.save_cash_adjustment("2026-07-01", 250.0)

        result = await metrics_store.get_cumulative_adjustments_by_date()
        assert result["2026-07-01"] == 750.0

    @pytest.mark.asyncio
    async def test_pnl_and_account_normalization(self, metrics_store: MetricsStore) -> None:
        """Verify both Account and P&L computations.

        Deposits:     D0: $500, D2: $500, D4: $300 → total = $1,300
        History:      D0:$500, D1:$520, D2:$1040, D3:$1100, D4:$1450
                      (D2 raw includes $500 deposit, D4 includes $300 deposit)

        P&L = (value - start) - deposits_since_start:
          start = 500, startDeposits = 500
          D0: (500-500) - 0 = 0
          D1: (520-500) - 0 = 20
          D2: (1040-500) - 500 = 40
          D3: (1100-500) - 500 = 100
          D4: (1450-500) - 800 = 150

        Account = totalDeposits + pnl:
          D0: 1300 + 0 = 1300
          D1: 1300 + 20 = 1320
          D2: 1300 + 40 = 1340
          D3: 1300 + 100 = 1400
          D4: 1300 + 150 = 1450
        """
        await metrics_store.save_cash_adjustment("2026-07-01", 500.0)
        await metrics_store.save_cash_adjustment("2026-07-03", 500.0)
        await metrics_store.save_cash_adjustment("2026-07-05", 300.0)

        cumulative = await metrics_store.get_cumulative_adjustments_by_date()
        adj_dates_sorted = sorted(cumulative.keys())

        history = [
            {"date": "2026-07-01", "portfolio_value": 500.0},
            {"date": "2026-07-02", "portfolio_value": 520.0},
            {"date": "2026-07-03", "portfolio_value": 1040.0},
            {"date": "2026-07-04", "portfolio_value": 1100.0},
            {"date": "2026-07-05", "portfolio_value": 1450.0},
        ]

        total_net = cumulative[adj_dates_sorted[-1]]  # 1300

        def get_cum_at(date):
            result = 0.0
            for ad in adj_dates_sorted:
                if ad <= date:
                    result = cumulative[ad]
                else:
                    break
            return result

        # P&L: (value - start) - deposits_since_start → always starts at $0
        start_val = history[0]["portfolio_value"]
        start_deps = get_cum_at(history[0]["date"])
        pnl_vals = [
            (h["portfolio_value"] - start_val) - (get_cum_at(h["date"]) - start_deps)
            for h in history
        ]
        assert pnl_vals == [0.0, 20.0, 40.0, 100.0, 150.0]

        # Account: totalDeposits + pnl → starts at totalDeposits
        account_vals = [total_net + pnl for pnl in pnl_vals]
        assert account_vals == [1300.0, 1320.0, 1340.0, 1400.0, 1450.0]
