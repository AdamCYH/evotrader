"""Daily performance metrics computation and storage.

Computes end-of-day aggregated metrics from the Trade Journal and stores
them in the ``daily_metrics`` table for historical tracking and the
Evolution Agent's performance analysis.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from evotrader.db.connection import Database
from evotrader.models.portfolio import DailyMetrics

logger = logging.getLogger(__name__)


class MetricsStore:
    """Async daily metrics store backed by SQLite."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def save_daily_metrics(self, metrics: DailyMetrics) -> None:
        """Insert or replace daily metrics for a given date."""
        async with self._db.transaction() as conn:
            await conn.execute(
                """
                INSERT OR REPLACE INTO daily_metrics (
                    date, portfolio_value, cash_balance,
                    daily_pnl, daily_return_pct, cumulative_return,
                    win_count, loss_count, win_rate,
                    avg_win, avg_loss, profit_factor,
                    sharpe_30d, sortino_30d, max_drawdown,
                    algo_version, regime_summary, trades_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    metrics.date,
                    metrics.portfolio_value,
                    metrics.cash_balance,
                    metrics.daily_pnl,
                    metrics.daily_return_pct,
                    metrics.cumulative_return,
                    metrics.win_count,
                    metrics.loss_count,
                    metrics.win_rate,
                    metrics.avg_win,
                    metrics.avg_loss,
                    metrics.profit_factor,
                    metrics.sharpe_30d,
                    metrics.sortino_30d,
                    metrics.max_drawdown,
                    metrics.algo_version,
                    json.dumps(metrics.regime_summary),
                    metrics.trades_count,
                ),
            )
        logger.info(
            "Daily metrics saved: date=%s, pnl=$%.2f, return=%.2f%%",
            metrics.date,
            metrics.daily_pnl,
            metrics.daily_return_pct * 100,
        )

    async def get_metrics_range(
        self,
        start_date: str,
        end_date: str | None = None,
    ) -> list[DailyMetrics]:
        """Retrieve daily metrics for a date range.

        Args:
            start_date: Start date (inclusive), format 'YYYY-MM-DD'.
            end_date: End date (inclusive). Defaults to today.
        """
        if end_date is None:
            end_date = datetime.now(UTC).strftime("%Y-%m-%d")

        async with self._db.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM daily_metrics
                WHERE date >= ? AND date <= ?
                ORDER BY date
                """,
                (start_date, end_date),
            )
            rows = await cursor.fetchall()
            if not rows:
                return []

            result = []
            for data in rows:
                # Parse JSON fields
                if data.get("regime_summary"):
                    data["regime_summary"] = json.loads(data["regime_summary"])
                else:
                    data["regime_summary"] = {}
                result.append(DailyMetrics.model_validate(data))
            return result

    async def get_latest_metrics(self, days: int = 30) -> list[DailyMetrics]:
        """Get the most recent N days of metrics."""
        async with self._db.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM daily_metrics
                ORDER BY date DESC
                LIMIT ?
                """,
                (days,),
            )
            rows = await cursor.fetchall()
            if not rows:
                return []

            result = []
            for data in rows:
                if data.get("regime_summary"):
                    data["regime_summary"] = json.loads(data["regime_summary"])
                else:
                    data["regime_summary"] = {}
                result.append(DailyMetrics.model_validate(data))
            # Return in chronological order
            result.reverse()
            return result

    async def get_peak_portfolio_value(self) -> float:
        """Get the all-time high portfolio value (for drawdown calculation)."""
        async with self._db.connection() as conn:
            cursor = await conn.execute("SELECT MAX(portfolio_value) FROM daily_metrics")
            row = await cursor.fetchone()
            if row:
                val = next(iter(row.values()))
                return float(val) if val else 0.0
            return 0.0

    async def compute_rolling_sharpe(self, days: int = 30) -> float | None:
        """Compute rolling Sharpe ratio over the last N days.

        Uses daily returns and assumes 252 trading days per year.
        Risk-free rate is approximated as 0 for simplicity.
        """
        import math

        metrics = await self.get_latest_metrics(days)
        if len(metrics) < 5:  # Need at least 5 data points
            return None

        returns = [m.daily_return_pct for m in metrics]
        mean_return = sum(returns) / len(returns)
        variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
        std_dev = math.sqrt(variance) if variance > 0 else 0.0

        if std_dev == 0:
            return None

        # Annualize: Sharpe = (mean_daily / std_daily) * sqrt(252)
        return (mean_return / std_dev) * math.sqrt(252)

    async def compute_rolling_sortino(self, days: int = 30) -> float | None:
        """Compute rolling Sortino ratio over the last N days.

        Like Sharpe but uses only downside deviation.
        """
        import math

        metrics = await self.get_latest_metrics(days)
        if len(metrics) < 5:
            return None

        returns = [m.daily_return_pct for m in metrics]
        mean_return = sum(returns) / len(returns)

        # Downside deviation: only consider negative returns
        downside_returns = [r for r in returns if r < 0]
        if not downside_returns:
            return None  # No downside = infinite Sortino (not meaningful)

        downside_variance = sum(r**2 for r in downside_returns) / len(returns)
        downside_dev = math.sqrt(downside_variance) if downside_variance > 0 else 0.0

        if downside_dev == 0:
            return None

        return (mean_return / downside_dev) * math.sqrt(252)

    # ── Cash Adjustments (deposit / withdrawal tracking) ──────────────

    async def save_cash_adjustment(self, date: str, amount: float, note: str = "") -> int:
        """Insert a cash adjustment (deposit or withdrawal).

        Args:
            date: Date of the adjustment (YYYY-MM-DD).
            amount: Positive for deposit, negative for withdrawal.
            note: Optional human-readable note.

        Returns:
            The row ID of the new adjustment.
        """
        created_at = datetime.now(UTC).isoformat()
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO cash_adjustments (date, amount, note, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (date, amount, note, created_at),
            )
            row_id = cursor.lastrowid
        logger.info(
            "Cash adjustment saved: date=%s, amount=$%.2f, note=%s",
            date,
            amount,
            note,
        )
        return row_id  # type: ignore[return-value]

    async def get_cash_adjustments(self) -> list[dict]:
        """Return all cash adjustments ordered by date."""
        async with self._db.connection() as conn:
            cursor = await conn.execute(
                "SELECT id, date, amount, note, created_at FROM cash_adjustments ORDER BY date, id"
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows] if rows else []

    async def delete_cash_adjustment(self, adjustment_id: int) -> bool:
        """Delete a cash adjustment by ID.

        Returns:
            True if a row was deleted, False if the ID wasn't found.
        """
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "DELETE FROM cash_adjustments WHERE id = ?",
                (adjustment_id,),
            )
            deleted = cursor.rowcount > 0
        if deleted:
            logger.info("Cash adjustment #%d deleted.", adjustment_id)
        return deleted

    async def get_cumulative_adjustments_by_date(self) -> dict[str, float]:
        """Compute cumulative cash adjustments keyed by date.

        Returns a dict mapping each adjustment date (and all subsequent
        dates) to the running total of deposits/withdrawals up to and
        including that date.  The equity curve endpoint uses this to
        subtract external cash flows from portfolio_value.
        """
        adjustments = await self.get_cash_adjustments()
        if not adjustments:
            return {}

        # Build a running sum keyed by date
        daily_totals: dict[str, float] = {}
        for adj in adjustments:
            d = adj["date"]
            daily_totals[d] = daily_totals.get(d, 0.0) + adj["amount"]

        # Convert to cumulative
        cumulative: dict[str, float] = {}
        running = 0.0
        for d in sorted(daily_totals):
            running += daily_totals[d]
            cumulative[d] = running

        return cumulative
