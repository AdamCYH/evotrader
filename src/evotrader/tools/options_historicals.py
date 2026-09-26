"""Options historicals and implied volatility trend analysis.

Fetches historical premium data from the Robinhood MCP
``get_option_historicals`` tool and computes IV trend direction,
which feeds into the ``options_positioning`` strategy to modulate
contrarian vs. confirmation signal weighting.

IV trend is classified as:
- ``rising``: IV expanding → market pricing in risk → stronger contrarian signal
- ``falling``: IV contracting (crush) → risk unwinding → weaker contrarian signal
- ``flat``: IV stable within noise band
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def fetch_option_historicals(
    instrument_id: str,
    call_mcp_tool,
    span: str = "week",
) -> list[dict[str, Any]]:
    """Fetch historical data for a single option instrument.

    Args:
        instrument_id: Robinhood option instrument ID or URL.
        call_mcp_tool: Async callable to invoke MCP tools.
        span: Time span — "day", "week", "month", "3month", "year".

    Returns:
        List of historical data points, or empty list on failure.
    """
    # The Robinhood MCP server's `get_option_historicals` accepts
    # `symbol` (option instrument URL or ID) plus a time window.
    # Map span to interval + start_time to match the server's schema.
    import datetime as _dt

    _span_map = {
        "day": ("5minute", 1),
        "week": ("day", 7),
        "month": ("day", 30),
        "3month": ("day", 90),
        "year": ("day", 365),
    }
    interval, days = _span_map.get(span, ("day", 7))
    start_time = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    raw = await call_mcp_tool(
        "get_option_historicals",
        {"instrument_ids": [instrument_id], "interval": interval, "start_time": start_time},
    )
    if not raw:
        return []

    data = raw.get("data", {})
    results = data.get("results", data.get("historicals", []))
    return results if isinstance(results, list) else []


def compute_iv_trend(
    historicals: list[dict[str, Any]],
    lookback_points: int = 10,
    noise_threshold: float = 0.02,
) -> dict[str, Any]:
    """Compute IV trend direction from option historicals.

    Uses simple linear regression slope on the implied volatility
    series to determine whether IV is rising, falling, or flat.

    Args:
        historicals: Historical data points with ``implied_volatility``
            or ``mark_price`` fields.
        lookback_points: Number of most recent points to use.
        noise_threshold: Minimum absolute slope (per-point) to classify
            as rising/falling vs flat.

    Returns:
        Dict with:
        - ``iv_trend``: "rising", "falling", or "flat"
        - ``iv_slope``: Raw slope of IV over the window.
        - ``iv_current``: Most recent IV value.
        - ``iv_percentile``: Current IV as percentile of the window.
        - ``data_points``: Number of data points used.
        - ``available``: Whether IV data was available.
    """
    # Extract IV values from historicals
    iv_values: list[float] = []
    for point in historicals:
        iv = point.get("implied_volatility")
        if iv is not None:
            try:
                iv_float = float(iv)
                if iv_float > 0:
                    iv_values.append(iv_float)
            except (ValueError, TypeError):
                continue

    if len(iv_values) < 3:
        return {
            "available": False,
            "iv_trend": "flat",
            "iv_slope": 0.0,
            "iv_current": None,
            "iv_percentile": None,
            "data_points": len(iv_values),
        }

    # Use the most recent lookback_points
    recent = iv_values[-lookback_points:]
    n = len(recent)

    # Simple linear regression: y = mx + b
    x_mean = (n - 1) / 2.0
    y_mean = sum(recent) / n
    numerator = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(recent))
    denominator = sum((i - x_mean) ** 2 for i in range(n))

    slope = numerator / denominator if denominator > 0 else 0.0

    # Classify trend
    if slope > noise_threshold:
        trend = "rising"
    elif slope < -noise_threshold:
        trend = "falling"
    else:
        trend = "flat"

    # IV percentile: where current IV sits relative to the full history
    current_iv = recent[-1]
    sorted_iv = sorted(iv_values)
    rank = sum(1 for v in sorted_iv if v <= current_iv)
    percentile = round(rank / len(sorted_iv) * 100, 1)

    return {
        "available": True,
        "iv_trend": trend,
        "iv_slope": round(slope, 6),
        "iv_current": round(current_iv, 4),
        "iv_percentile": percentile,
        "data_points": n,
    }


async def get_atm_iv_trend(
    ticker: str,
    atm_instrument_id: str | None,
    call_mcp_tool,
) -> dict[str, Any]:
    """Convenience: fetch historicals for the ATM strike and compute IV trend.

    Args:
        ticker: Underlying symbol (for logging).
        atm_instrument_id: Option instrument ID for the ATM strike.
            If None, returns unavailable result.
        call_mcp_tool: Async callable to invoke MCP tools.
    """
    if not atm_instrument_id:
        return {
            "available": False,
            "iv_trend": "flat",
            "iv_slope": 0.0,
            "iv_current": None,
            "iv_percentile": None,
            "data_points": 0,
            "reason": "no_atm_instrument",
        }

    historicals = await fetch_option_historicals(
        atm_instrument_id,
        call_mcp_tool,
        span="week",
    )
    result = compute_iv_trend(historicals)
    result["ticker"] = ticker
    result["instrument_id"] = atm_instrument_id
    return result
