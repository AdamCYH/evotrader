"""Pydantic models mapping directly to Robinhood MCP provider responses.

These models strictly define the structure of data returned by the MCP,
and provide centralized fallback logic for real-time pricing and valuation.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class McpQuoteBase(BaseModel):
    """Base quote properties common to equities and options."""

    last_trade_price: float | None = Field(default=None)
    bid_price: float | None = Field(default=None)
    ask_price: float | None = Field(default=None)

    @property
    def bid(self) -> float:
        return self.bid_price or 0.0

    @property
    def ask(self) -> float:
        return self.ask_price or 0.0

    @property
    def reg(self) -> float:
        return self.last_trade_price or 0.0


class McpEquityQuote(McpQuoteBase):
    """Equity quote mapping for get_equity_quotes."""

    last_extended_hours_trade_price: float | None = Field(default=None)

    @property
    def ext(self) -> float:
        return self.last_extended_hours_trade_price or 0.0

    def resolve_live_price(self) -> float:
        """Returns the most accurate live price, prioritizing mid-price for 24-hour accuracy."""
        if self.ask > 0 and self.bid > 0:
            return (self.ask + self.bid) / 2.0
        if self.ext > 0:
            return self.ext
        return self.reg


class McpOptionQuote(McpQuoteBase):
    """Option quote mapping for get_option_quotes."""

    adjusted_mark_price: float | None = Field(default=None)

    @property
    def mark(self) -> float:
        return self.adjusted_mark_price or 0.0

    def resolve_live_price(self) -> float:
        """Returns the most accurate live option premium price."""
        if self.ask > 0 and self.bid > 0:
            return (self.ask + self.bid) / 2.0
        if self.mark > 0:
            return self.mark
        return self.reg


class McpPortfolio(BaseModel):
    """Portfolio mapping for get_portfolio."""

    account_number: str
    total_value: float = Field(default=0.0)
    cash: float = Field(default=0.0)
    buying_power: float = Field(default=0.0)
    # The nested buying_power structure can vary, but we can try to extract it loosely

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> McpPortfolio:
        bp_dict = data.get("buying_power", {})
        bp = 0.0
        if isinstance(bp_dict, dict):
            bp = float(bp_dict.get("buying_power", 0.0))
        elif isinstance(bp_dict, (int, float, str)):
            try:
                bp = float(bp_dict)
            except ValueError:
                pass

        return cls(
            account_number=str(data.get("account_number", "")),
            total_value=float(data.get("total_value", 0.0)),
            cash=float(data.get("cash", 0.0)),
            buying_power=bp,
        )


class McpPosition(BaseModel):
    """Position mapping for get_positions and get_option_positions."""

    symbol: str | None = Field(default=None)
    quantity: float = Field(default=0.0)
    average_price: float = Field(default=0.0)
    asset_type: str = Field(default="EQUITY")

    # Option specific
    option_id: str | None = Field(default=None)
    option_type: str | None = Field(default=None)
    strike: float | None = Field(default=None)
    expiration: str | None = Field(default=None)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> McpPosition:
        qty = float(data.get("quantity") or 0.0)
        avg = float(data.get("average_price") or data.get("average_buy_price") or 0.0)

        # Options might use "type", equities usually don't have option_type
        opt_type = data.get("type") or data.get("option_type")
        strike = data.get("strike_price") or data.get("strike")
        if strike is not None:
            try:
                strike = float(strike)
            except ValueError:
                strike = None

        # Resolve asset type
        asset_type = data.get("asset_type")
        if not asset_type:
            if data.get("option_id"):
                asset_type = "OPTION"
            else:
                asset_type = "EQUITY"

        # Robinhood's average_price for option positions is per-contract total
        # (per-share × trade_value_multiplier, typically 100). Normalize to
        # per-share so downstream P&L calculations that apply their own ×100
        # multiplier produce correct results.
        if asset_type.upper() == "OPTION" and avg > 0:
            multiplier = float(data.get("trade_value_multiplier") or 100.0)
            avg = avg / multiplier

        return cls(
            symbol=data.get("symbol") or data.get("chain_symbol"),
            quantity=qty,
            average_price=avg,
            asset_type=asset_type.upper(),
            option_id=data.get("option_id"),
            option_type=opt_type,
            strike=strike,
            expiration=data.get("expiration_date") or data.get("expiration"),
        )
