"""Portfolio models.

Schemas for portfolio state, positions, and performance tracking.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from evotrader.models.trade import TradeDirection


class Position(BaseModel):
    """A single open position in the portfolio."""

    ticker: str
    direction: TradeDirection
    quantity: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    current_price: float = Field(gt=0)
    stop_loss_price: float | None = None
    take_profit_price: float | None = None
    entry_timestamp: datetime
    trade_id: int = Field(description="ID of the opening trade in the journal.")

    @property
    def unrealized_pnl(self) -> float:
        """Unrealized P&L in USD."""
        if self.direction == TradeDirection.LONG:
            return (self.current_price - self.entry_price) * self.quantity
        return (self.entry_price - self.current_price) * self.quantity

    @property
    def unrealized_pnl_pct(self) -> float:
        """Unrealized P&L as percentage of entry value."""
        entry_value = self.entry_price * self.quantity
        if entry_value == 0:
            return 0.0
        return self.unrealized_pnl / entry_value

    @property
    def market_value(self) -> float:
        """Current market value of the position."""
        return self.current_price * self.quantity

    @property
    def notional_value(self) -> float:
        """Notional value (always positive, used for exposure calculations)."""
        return abs(self.market_value)


class PortfolioState(BaseModel):
    """Complete portfolio snapshot at a point in time.

    Loaded from the Robinhood MCP ``get_portfolio`` tool and enriched
    with local position tracking from the Trade Journal.
    """

    timestamp: datetime
    total_value: float = Field(description="Total portfolio value (cash + positions).")
    cash_balance: float = Field(description="Available cash balance.")
    buying_power: float = Field(description="Available buying power.")

    positions: list[Position] = Field(default_factory=list, description="Currently open positions.")

    # Derived daily metrics
    daily_pnl: float = Field(0.0, description="Today's realized + unrealized P&L.")
    daily_return_pct: float = Field(0.0, description="Today's return percentage.")
    peak_value: float = Field(0.0, description="All-time high portfolio value (for drawdown).")

    @property
    def total_long_exposure(self) -> float:
        """Total notional value of long positions."""
        return sum(p.notional_value for p in self.positions if p.direction == TradeDirection.LONG)

    @property
    def total_short_exposure(self) -> float:
        """Total notional value of short positions."""
        return sum(p.notional_value for p in self.positions if p.direction == TradeDirection.SHORT)

    @property
    def total_exposure(self) -> float:
        """Total exposure (long + short notional)."""
        return self.total_long_exposure + self.total_short_exposure

    @property
    def exposure_pct(self) -> float:
        """Total exposure as percentage of portfolio value."""
        if self.total_value == 0:
            return 0.0
        return self.total_exposure / self.total_value

    @property
    def net_exposure(self) -> float:
        """Net exposure (long - short)."""
        return self.total_long_exposure - self.total_short_exposure

    @property
    def unrealized_pnl(self) -> float:
        """Total unrealized P&L across all positions."""
        return sum(p.unrealized_pnl for p in self.positions)

    @property
    def drawdown_pct(self) -> float:
        """Current drawdown from peak as percentage."""
        if self.peak_value == 0:
            return 0.0
        return (self.peak_value - self.total_value) / self.peak_value

    @property
    def position_count(self) -> int:
        return len(self.positions)


class DailyMetrics(BaseModel):
    """Aggregated performance metrics for a single trading day."""

    date: str = Field(description="Trading date (YYYY-MM-DD).")
    portfolio_value: float
    cash_balance: float
    daily_pnl: float
    daily_return_pct: float
    cumulative_return: float
    win_count: int = 0
    loss_count: int = 0
    win_rate: float | None = None
    avg_win: float | None = None
    avg_loss: float | None = None
    profit_factor: float | None = None
    sharpe_30d: float | None = None
    sortino_30d: float | None = None
    max_drawdown: float = 0.0
    algo_version: str = ""
    regime_summary: dict[str, int] = Field(
        default_factory=dict,
        description="Count of trading cycles per regime today.",
    )
    trades_count: int = 0
