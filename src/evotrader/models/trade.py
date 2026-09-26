"""Trade models.

Schemas for trade proposals, risk verdicts, and order execution results.
These flow through the Strategy → Risk Manager → Execution pipeline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class TradeDirection(str, Enum):
    """Whether a trade opens a long or short position."""

    LONG = "LONG"
    SHORT = "SHORT"


class TradeAction(str, Enum):
    """Lifecycle action for a trade."""

    OPEN = "OPEN"
    CLOSE = "CLOSE"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"


class OrderType(str, Enum):
    """Supported order types."""

    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
    TRAILING_STOP = "trailing_stop"


# The broker's vocabulary is not this enum's. Robinhood calls a stop order
# `stop_market`; the enum calls it `stop`. `OrderType("stop_market")` raised,
# the caller swallowed the error, and the order fell through to the MARKET
# default — so every stop placed under the broker's own name was journaled as a
# market order (2026-09-21), and anything reading the journal to ask
# "is a stop resting?" could not tell.
_BROKER_ORDER_TYPE_ALIASES: dict[str, str] = {"stop_market": "stop"}

#: Every spelling of a stop-triggered order, broker and journal alike.
STOP_ORDER_TYPES: frozenset[str] = frozenset({"stop", "stop_market", "stop_limit", "trailing_stop"})


def parse_order_type(value: object) -> OrderType | None:
    """Parse an order type in either the broker's or the journal's vocabulary."""
    if value is None:
        return None
    raw = str(value).strip().lower()
    raw = _BROKER_ORDER_TYPE_ALIASES.get(raw, raw)
    try:
        return OrderType(raw)
    except ValueError:
        return None


class TradeProposal(BaseModel):
    """Output of the Strategy Agent — a proposed trade awaiting risk approval.

    Contains everything the Risk Manager needs to validate the trade
    and everything the Execution Agent needs to place the order.
    """

    ticker: str = Field(description="Ticker symbol (e.g., 'SPY').")
    direction: TradeDirection | None = Field(None, description="Long or short.")
    action: TradeAction = Field(description="Open, close, stop-loss, or take-profit.")

    # Order parameters
    quantity: float | None = Field(None, gt=0, description="Number of shares.")
    order_type: OrderType | None = Field(None, description="How to execute the order.")
    limit_price: float | None = Field(
        None, description="Limit price (for limit/stop-limit orders)."
    )
    stop_price: float | None = Field(
        None, description="Stop price (for stop/stop-limit/trailing orders)."
    )
    time_in_force: str | None = Field(
        None,
        description=(
            "Order duration as sent to the broker ('gtc' or 'gfd'). Protective "
            "orders must be 'gtc': a day stop expires at the close and leaves "
            "the position naked the next morning (2026-09-17)."
        ),
    )

    # Risk parameters
    stop_loss_price: float | None = Field(
        None, description="Stop-loss exit price for the position."
    )
    take_profit_price: float | None = Field(
        None, description="Take-profit exit price for the position."
    )

    # Decision context (for journaling)
    hybrid_score: float | None = Field(None, description="Hybrid score that triggered this trade.")
    confidence: float | None = Field(None, ge=0.0, le=1.0, description="Signal confidence.")
    algo_signal: float | None = Field(None, description="Algorithmic signal component.")
    llm_signal: float | None = Field(None, description="LLM signal component.")
    regime: str | None = Field(None, description="Market regime at decision time.")
    algo_version: str | None = Field(None, description="Algorithm version that generated this.")
    reasoning: str | None = Field(None, description="Natural language reasoning for the trade.")

    # Metadata
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    related_trade_id: int | None = Field(
        None, description="ID of the trade being closed (for CLOSE actions)."
    )

    # Option details (optional)
    option_id: str | None = Field(None, description="Option contract ID if option trade.")
    option_type: str | None = Field(
        None, description="Option type: 'call' or 'put' if option trade."
    )
    strike: float | None = Field(None, description="Strike price if option trade.")
    expiration: str | None = Field(
        None, description="Expiration date (YYYY-MM-DD) if option trade."
    )

    @property
    def estimated_value(self) -> float:
        """Estimated order value in USD."""
        price = self.limit_price or self.stop_price or 0.0
        return (self.quantity or 0.0) * price


class RiskVerdictStatus(str, Enum):
    """Outcome of risk validation."""

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    MODIFIED = "MODIFIED"


class RiskVerdict(BaseModel):
    """Output of the Risk Manager Agent.

    Approves, rejects, or modifies a trade proposal with full reasoning.
    """

    status: RiskVerdictStatus
    original_proposal: TradeProposal
    modified_proposal: TradeProposal | None = Field(
        None,
        description="Modified version of the proposal (e.g., reduced size). "
        "Only present when status is MODIFIED.",
    )
    rejection_reasons: list[str] = Field(
        default_factory=list,
        description="Reasons for rejection (empty if approved).",
    )
    risk_checks_passed: list[str] = Field(
        default_factory=list,
        description="Names of risk checks that passed.",
    )
    risk_checks_failed: list[str] = Field(
        default_factory=list,
        description="Names of risk checks that failed.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-blocking risk warnings.",
    )
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def approved_proposal(self) -> TradeProposal | None:
        """Return the proposal to execute (original or modified), or None if rejected."""
        if self.status == RiskVerdictStatus.REJECTED:
            return None
        if self.status == RiskVerdictStatus.MODIFIED:
            return self.modified_proposal
        return self.original_proposal


class OrderStatus(str, Enum):
    """Lifecycle states of an order."""

    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    # A day order the broker dropped at the session close. Distinct from
    # CANCELLED so an expired stop is never mistaken for an agent's decision.
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class OrderResult(BaseModel):
    """Result of order execution via the Robinhood MCP.

    Produced by the Execution Agent after placing and monitoring an order.
    """

    order_id: str = Field(description="Broker-assigned order ID.")
    status: OrderStatus
    ticker: str
    direction: TradeDirection
    action: TradeAction
    order_type: OrderType
    requested_quantity: float
    filled_quantity: float = 0.0
    requested_price: float | None = None
    fill_price: float | None = None
    slippage: float | None = Field(None, description="Difference between requested and fill price.")
    commission: float = 0.0
    error_message: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Option details (optional)
    option_id: str | None = Field(None, description="Option contract ID if option trade.")
    option_type: str | None = Field(
        None, description="Option type: 'call' or 'put' if option trade."
    )
    strike: float | None = Field(None, description="Strike price if option trade.")
    expiration: str | None = Field(
        None, description="Expiration date (YYYY-MM-DD) if option trade."
    )

    @property
    def is_terminal(self) -> bool:
        """Whether the order has reached a final state."""
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.FAILED,
        )

    @property
    def fill_value(self) -> float:
        """Total fill value in USD."""
        return self.filled_quantity * (self.fill_price or 0.0)
