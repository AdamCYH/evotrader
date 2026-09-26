"""Market data models.

Schemas for price data, technical indicators, and market regime classification.
These flow from the Market Intelligence Agent to downstream consumers.
"""

from __future__ import annotations

import logging
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)


class MarketRegime(str, Enum):
    """Classified market state used to select strategy weights."""

    TRENDING_BULL = "trending_bull"
    TRENDING_BEAR = "trending_bear"
    RANGE_BOUND = "range_bound"
    HIGH_VOLATILITY = "high_volatility"


class OHLCV(BaseModel):
    """Single candlestick bar."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class Quote(BaseModel):
    """Real-time quote snapshot."""

    ticker: str
    bid: float
    ask: float
    last: float
    volume: float
    timestamp: datetime
    previous_close: float | None = Field(
        None,
        description=(
            "Prior session close. Used to derive daily_change_pct when the "
            "snapshot-level field is unavailable (range_break_continuation "
            "fallback path)."
        ),
    )

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid


class TechnicalIndicators(BaseModel):
    """Computed technical indicator values at a point in time."""

    model_config = ConfigDict(extra="allow")

    # Oscillators
    rsi_14: float | None = Field(None, description="14-period RSI (0-100).")
    macd_line: float | None = Field(None, description="MACD line value.")
    macd_signal: float | None = Field(None, description="MACD signal line value.")
    macd_histogram: float | None = Field(None, description="MACD histogram value.")

    # Bands
    bollinger_upper: float | None = Field(None, description="Upper Bollinger Band (20, 2σ).")
    bollinger_middle: float | None = Field(None, description="Middle Bollinger Band (20-SMA).")
    bollinger_lower: float | None = Field(None, description="Lower Bollinger Band (20, 2σ).")
    bollinger_width: float | None = Field(None, description="Band width (normalized).")

    # Trend
    ema_9: float | None = Field(None, description="9-period EMA.")
    ema_21: float | None = Field(None, description="21-period EMA.")
    sma_20: float | None = Field(None, description="20-period SMA.")
    sma_50: float | None = Field(None, description="50-period SMA.")

    # Intraday
    vwap: float | None = Field(None, description="Volume-weighted average price.")
    ibs: float | None = Field(None, description="Internal Bar Strength (0-1).")
    vwap_anchor: str | None = Field(
        None,
        description=(
            "VWAP session anchor: 'current_session' or 'prior_session'. "
            "MUST be populated whenever `vwap` is non-None. A None/'unknown' "
            "value with a populated vwap indicates a propagation defect and "
            "will mute intraday_vwap_zscore, swing_failure_reversal and the "
            "mean_reversion vwap component simultaneously."
        ),
    )

    @model_validator(mode="after")
    def _warn_on_unanchored_vwap(self) -> TechnicalIndicators:
        if self.vwap is not None and not self.vwap_anchor:
            logger.warning(
                "TechnicalIndicators.vwap=%.4f populated but vwap_anchor is "
                "unset - 3 sub-strategies will mute. Check the snapshot "
                "construction path.",
                self.vwap,
            )
        return self

    # Volatility & volume
    atr_14: float | None = Field(None, description="14-period Average True Range.")
    volume_sma_20: float | None = Field(None, description="20-period volume SMA.")
    relative_volume: float | None = Field(
        None,
        description=(
            "Last DAILY bar's volume / its 20-day average. Computed from the "
            "daily series, whose last bar's volume does not update intraday, so "
            "this is the PRIOR session's ratio and holds one value all day — see "
            "`relative_volume_source`. For today's tape use "
            "`session_relative_volume`."
        ),
    )
    relative_volume_source: str | None = Field(
        None,
        description=(
            "What `relative_volume` measured: 'prior_session_daily'. Emitted for "
            "the same reason `vwap_anchor` is — a number that looks live but is "
            "not must say so."
        ),
    )
    session_relative_volume: float | None = Field(
        None,
        description=(
            "TODAY's cumulative regular-session volume divided by the average "
            "cumulative volume at the same minute of the day over prior sessions. "
            "None until the time-of-day profile has enough sessions, and on "
            "cycles with no regular-session bars (pre-market, after-hours)."
        ),
    )
    session_relative_volume_sessions: int | None = Field(
        None, description="Prior sessions behind `session_relative_volume`."
    )

    # Event context (populated by Market Intelligence Agent when available)
    atm_iv_30dte: float | None = Field(None, description="ATM implied volatility (30 DTE).")
    atm_iv_pre_event: float | None = Field(
        None, description="IV snapshot taken before nearest binary event."
    )
    hours_to_event: float | None = Field(
        None,
        description="Hours until nearest high-impact event (earnings/CPI/FOMC).",
    )
    hours_since_event: float | None = Field(
        None, description="Hours since last high-impact event cleared."
    )
    event_type: str | None = Field(
        None,
        description="Type of nearest event: 'earnings', 'CPI', 'FOMC', 'PPI'.",
    )


class OptionsContext(BaseModel):
    """Options positioning data for contrarian/confirmation signals.

    Populated by the Market Intelligence Agent from options chain data
    when available.  All fields are optional so strategies can gracefully
    abstain when the data pipeline is not yet connected.
    """

    pc_volume_ratio: float | None = Field(
        None, description="Put/call volume ratio for the session."
    )
    pc_oi_ratio: float | None = Field(None, description="Put/call open-interest ratio.")
    event_hours_away: float | None = Field(
        None,
        description="Hours until nearest high-impact event (None = no event pending).",
    )
    event_hours_since: float | None = Field(
        None, description="Hours since last high-impact event cleared."
    )
    pc_ratio_change: float | None = Field(
        None,
        description="Session-over-session change in P/C volume ratio (negative = unwinding).",
    )


class RegimeClassification(BaseModel):
    """Result of market regime detection."""

    regime: MarketRegime
    confidence: float = Field(ge=0.0, le=1.0, description="Detection confidence.")
    reasoning: str = Field(description="Human-readable explanation of classification.")

    # Feature values used in classification
    adx: float | None = Field(None, description="ADX value for trend strength.")
    trend_direction: float | None = Field(
        None, description="Slope of 20-SMA (positive = up, negative = down)."
    )
    volatility_percentile: float | None = Field(
        None, description="Current ATR percentile vs. 60-day range (0-100)."
    )


class MarketSnapshot(BaseModel):
    """Complete market state at a point in time.

    This is the primary output of the Market Intelligence Agent and serves
    as input to the Strategy Agent and Risk Manager.
    """

    model_config = ConfigDict(extra="allow")

    ticker: str
    timestamp: datetime
    quote: Quote
    indicators: TechnicalIndicators
    regime: RegimeClassification
    recent_candles: list[OHLCV] = Field(
        default_factory=list,
        description=(
            "Intraday (5-min) OHLCV bars for the current session. "
            "Used by IntradayVwapZscoreStrategy for z-score context. "
            "Empty when market is closed."
        ),
    )
    daily_candles: list[OHLCV] = Field(
        default_factory=list,
        description=(
            "Daily OHLCV bars (up to 90 sessions). "
            "Used by TrendPersistenceStrategy for multi-session "
            "grind detection. Empty when historical data unavailable."
        ),
    )

    # Derived convenience fields
    daily_change_pct: float | None = Field(
        None,
        description=(
            "Percent change from previous close (e.g. -2.38 means -2.38%). "
            "All _pct fields use percent units: 1.0 = 1%."
        ),
    )
    gap_pct: float | None = Field(
        None,
        description=(
            "Overnight gap as a percent of prior close (e.g. 1.5 means "
            "+1.5% gap up). All _pct fields use percent units: 1.0 = 1%."
        ),
    )

    # Options positioning context (populated when options chain data is available)
    options_context: OptionsContext | None = Field(
        None, description="Options positioning data for contrarian/confirmation signals."
    )
