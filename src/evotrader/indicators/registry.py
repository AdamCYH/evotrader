"""Central Technical Indicator Registry & Snapshot Manager.

Provides a unified, extensible system for defining, discovering, formatting,
and serializing technical indicators, statistical features, and market snapshot
payloads. When self-evolution agents propose new strategies or indicators, they
are automatically tracked, persisted, and rendered across backend and frontend
without hardcoding or schema migrations.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from evotrader.tools.asset_context import primary_ticker


class IndicatorFormat(str, Enum):
    """Display formatting style for technical indicators."""

    DECIMAL = "decimal"  # Standard decimal (e.g. 1.25)
    PERCENT = "percent"  # Percentage value (e.g. 1.50%)
    PERCENT_NORM = "pct_norm"  # Normalized ratio where 0.015 = 1.50%
    CURRENCY = "currency"  # Dollar amount (e.g. $502.50)
    OSCILLATOR = "oscillator"  # Bounded scale (e.g. 0-100 for RSI)
    INTEGER = "integer"  # Whole number


@dataclass(frozen=True)
class IndicatorSpec:
    """Metadata specification for a technical indicator."""

    key: str
    label: str
    category: str
    format: IndicatorFormat = IndicatorFormat.DECIMAL
    unit: str = ""
    decimals: int = 2
    description: str = ""


# Central Registry of Core Known Indicators
INDICATOR_REGISTRY: dict[str, IndicatorSpec] = {
    # Oscillators
    "rsi_14": IndicatorSpec(
        "rsi_14",
        "RSI (14)",
        "Oscillators",
        IndicatorFormat.OSCILLATOR,
        decimals=1,
        description="14-period Relative Strength Index",
    ),
    "macd_line": IndicatorSpec(
        "macd_line", "MACD", "Oscillators", IndicatorFormat.DECIMAL, decimals=2
    ),
    "macd_signal": IndicatorSpec(
        "macd_signal", "MACD Signal", "Oscillators", IndicatorFormat.DECIMAL, decimals=2
    ),
    "macd_histogram": IndicatorSpec(
        "macd_histogram", "MACD Hist", "Oscillators", IndicatorFormat.DECIMAL, decimals=2
    ),
    "ibs": IndicatorSpec(
        "ibs",
        "IBS",
        "Oscillators",
        IndicatorFormat.DECIMAL,
        decimals=2,
        description="Internal Bar Strength (0-1)",
    ),
    # Bollinger Bands
    "bollinger_upper": IndicatorSpec(
        "bollinger_upper", "BB Upper", "Volatility", IndicatorFormat.CURRENCY, decimals=2
    ),
    "bollinger_middle": IndicatorSpec(
        "bollinger_middle", "BB Mid (20 SMA)", "Volatility", IndicatorFormat.CURRENCY, decimals=2
    ),
    "bollinger_lower": IndicatorSpec(
        "bollinger_lower", "BB Lower", "Volatility", IndicatorFormat.CURRENCY, decimals=2
    ),
    "bollinger_width": IndicatorSpec(
        "bollinger_width", "BB Width", "Volatility", IndicatorFormat.PERCENT_NORM, decimals=2
    ),
    # Intraday & VWAP
    "vwap": IndicatorSpec("vwap", "VWAP", "Intraday", IndicatorFormat.CURRENCY, decimals=2),
    "vwap_dist": IndicatorSpec(
        "vwap_dist", "VWAP Dist", "Intraday", IndicatorFormat.PERCENT_NORM, decimals=2
    ),
    # Trend & Moving Averages
    "adx_14": IndicatorSpec("adx_14", "ADX (14)", "Trend", IndicatorFormat.DECIMAL, decimals=1),
    "adx": IndicatorSpec("adx", "ADX", "Trend", IndicatorFormat.DECIMAL, decimals=1),
    "ema_9": IndicatorSpec("ema_9", "9 EMA", "Trend", IndicatorFormat.CURRENCY, decimals=2),
    "ema_21": IndicatorSpec("ema_21", "21 EMA", "Trend", IndicatorFormat.CURRENCY, decimals=2),
    "sma_20": IndicatorSpec("sma_20", "20 SMA", "Trend", IndicatorFormat.CURRENCY, decimals=2),
    "sma_50": IndicatorSpec("sma_50", "50 SMA", "Trend", IndicatorFormat.CURRENCY, decimals=2),
    # Volatility & Volume
    "atr_14": IndicatorSpec(
        "atr_14", "ATR (14)", "Volatility", IndicatorFormat.CURRENCY, decimals=2
    ),
    "atr": IndicatorSpec("atr", "ATR", "Volatility", IndicatorFormat.CURRENCY, decimals=2),
    "relative_volume": IndicatorSpec(
        "relative_volume", "RVOL", "Volume", IndicatorFormat.DECIMAL, decimals=2
    ),
    "volume_sma_20": IndicatorSpec(
        "volume_sma_20", "Vol SMA (20)", "Volume", IndicatorFormat.INTEGER
    ),
}


def get_indicator_spec(key: str) -> IndicatorSpec:
    """Retrieve or dynamically generate an IndicatorSpec for any indicator key."""
    if key in INDICATOR_REGISTRY:
        return INDICATOR_REGISTRY[key]

    # Heuristic inference for new indicators added by self-evolution
    clean_label = key.replace("_", " ").title()
    low = key.lower()

    if "dist" in low or "pct" in low or "width" in low or "percent" in low:
        fmt = (
            IndicatorFormat.PERCENT_NORM
            if "dist" in low or "width" in low
            else IndicatorFormat.PERCENT
        )
        cat = "Relative"
    elif (
        "price" in low
        or "vwap" in low
        or "band" in low
        or "upper" in low
        or "lower" in low
        or "level" in low
        or "target" in low
    ):
        fmt = IndicatorFormat.CURRENCY
        cat = "Price"
    elif "vol" in low or "count" in low or "shares" in low:
        fmt = IndicatorFormat.INTEGER
        cat = "Volume"
    elif "rsi" in low or "score" in low or "prob" in low or "zscore" in low:
        fmt = IndicatorFormat.DECIMAL
        cat = "Oscillators"
    else:
        fmt = IndicatorFormat.DECIMAL
        cat = "Signals"

    return IndicatorSpec(
        key=key,
        label=clean_label,
        category=cat,
        format=fmt,
        decimals=2,
    )


def format_indicator_value(key: str, value: Any) -> str:
    """Format an indicator value for UI display according to its specification."""
    if value is None:
        return "--"
    try:
        num = float(value)
    except (ValueError, TypeError):
        return str(value)

    spec = get_indicator_spec(key)
    if spec.format == IndicatorFormat.CURRENCY:
        return f"${num:.{spec.decimals}f}"
    elif spec.format == IndicatorFormat.PERCENT:
        return f"{num:.{spec.decimals}f}%"
    elif spec.format == IndicatorFormat.PERCENT_NORM:
        return f"{num * 100:.{spec.decimals}f}%"
    elif spec.format == IndicatorFormat.INTEGER:
        return f"{int(round(num)):,}"  # noqa: RUF046 (num may be a NumPy float)
    else:
        return f"{num:.{spec.decimals}f}"


def normalize_snapshot_payload(raw_snapshot: dict[str, Any]) -> dict[str, Any]:
    """Normalize any market snapshot dictionary into the flat DISPLAY/storage payload.

    Extracts all indicators, quotes, regime classification, and composite algorithm signals
    dynamically without discarding self-evolved or custom indicators, and flattens the
    indicators to the top level for the console. Used by the web console and when a
    snapshot is stored in ``market_snapshots``.

    The engine-side counterpart is ``agents.tools.normalize_market_snapshot``, which
    prepares input for the ``MarketSnapshot`` model; the two serve different consumers.
    """
    if not isinstance(raw_snapshot, dict):
        return {}

    indicators = raw_snapshot.get("indicators") or {}
    if not isinstance(indicators, dict):
        indicators = {}

    quote = raw_snapshot.get("quote") or {}
    if not isinstance(quote, dict):
        quote = {}

    regime_data = raw_snapshot.get("regime") or {}
    regime_str = (
        regime_data.get("regime")
        if isinstance(regime_data, dict)
        else (regime_data if isinstance(regime_data, str) else None)
    )
    regime_conf = (
        float(regime_data.get("confidence", 0.0))
        if isinstance(regime_data, dict) and "confidence" in regime_data
        else None
    )

    algo_signal_data = raw_snapshot.get("algo_signal")
    if isinstance(algo_signal_data, dict):
        composite_sig = (
            float(algo_signal_data.get("composite_signal", 0.0))
            if algo_signal_data.get("composite_signal") is not None
            else (
                float(raw_snapshot.get("composite_signal", 0.0))
                if raw_snapshot.get("composite_signal") is not None
                else None
            )
        )
        sub_signals = algo_signal_data.get("sub_signals") or raw_snapshot.get("sub_signals") or []
    else:
        composite_sig = (
            float(raw_snapshot.get("composite_signal", 0.0))
            if raw_snapshot.get("composite_signal") is not None
            else None
        )
        sub_signals = raw_snapshot.get("sub_signals") or []

    if not isinstance(sub_signals, list):
        sub_signals = []

    close_price = (
        quote.get("last")
        or quote.get("close")
        or raw_snapshot.get("close")
        or raw_snapshot.get("close_price")
    )
    # Configured instrument, not a literal: mislabelling a snapshot here
    # propagates a wrong ticker into every downstream signal and journal row.
    ticker = quote.get("ticker") or raw_snapshot.get("ticker") or primary_ticker()

    payload: dict[str, Any] = {
        "timestamp": raw_snapshot.get("timestamp"),
        "session_id": raw_snapshot.get("session_id"),
        "ticker": ticker,
        "close": close_price,
        "close_price": close_price,
        "quote": quote,
        "regime": regime_str or raw_snapshot.get("regime"),
        "regime_confidence": regime_conf or raw_snapshot.get("regime_confidence"),
        "composite_signal": composite_sig,
        "algo_version": (
            algo_signal_data.get("algo_version")
            if isinstance(algo_signal_data, dict)
            else raw_snapshot.get("algo_version")
        ),
        "sub_signals": sub_signals,
        "indicators": indicators,
        # Percent units (-0.04 means -0.04%). Carried so a stored snapshot can
        # say how the day stood, not only the live response.
        "daily_change_pct": raw_snapshot.get("daily_change_pct"),
        "gap_pct": raw_snapshot.get("gap_pct"),
    }

    # Flatten all indicators into the top-level payload for direct property access & backwards compatibility
    for k, v in indicators.items():
        if k not in payload:
            payload[k] = v

    return payload
