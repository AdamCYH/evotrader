"""Level II (order book depth) analysis.

Fetches bid/ask depth from the Robinhood MCP ``get_level_ii_quotes``
tool and computes execution intelligence metrics:
- Total visible depth (dollars) on each side
- Spread in basis points
- Order-book imbalance ratio (bid_depth / total_depth)
- Thin-book flag when visible liquidity is shallow

These metrics help the execution agent choose between limit and market
orders, and warn when slippage risk is elevated.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from evotrader.models.config import LevelIIConfig

logger = logging.getLogger(__name__)


async def fetch_level_ii(
    ticker: str,
    call_mcp_tool,
    depth_levels: int = 5,
) -> dict[str, Any]:
    """Fetch Level II order book data from the broker.

    Args:
        ticker: Equity symbol (e.g., "QQQ").
        call_mcp_tool: Async callable to invoke MCP tools.
        depth_levels: Number of price levels to request each side
            (advisory — server determines actual depth).

    Returns:
        Raw Level II response dict, or empty dict on failure.
    """
    raw = await call_mcp_tool(
        "get_equity_price_book",
        {"symbols": [ticker]},
    )
    return raw or {}


def assess_book_depth(
    level_ii_data: dict[str, Any],
    order_value: float | None = None,
    config: LevelIIConfig | None = None,
) -> dict[str, Any]:
    """Analyse Level II data for execution intelligence.

    Args:
        level_ii_data: Raw response from ``fetch_level_ii``.
        order_value: Proposed order value in dollars (for thin-book check).
        config: Level II configuration from the constitution.

    Returns:
        Dict with:
        - ``bid_depth_usd``: Total visible bid-side dollar depth.
        - ``ask_depth_usd``: Total visible ask-side dollar depth.
        - ``best_bid``: Highest bid price.
        - ``best_ask``: Lowest ask price.
        - ``spread_bps``: Bid-ask spread in basis points.
        - ``imbalance_ratio``: bid_depth / total_depth (0.5 = balanced).
        - ``thin_book``: True if visible depth is shallow relative to order.
        - ``depth_levels``: Number of levels analysed.
        - ``available``: Whether Level II data was available.
    """
    data = level_ii_data.get("data", {})
    bids = data.get("bids", [])
    asks = data.get("asks", [])

    if not bids and not asks:
        return {
            "available": False,
            "bid_depth_usd": 0.0,
            "ask_depth_usd": 0.0,
            "best_bid": None,
            "best_ask": None,
            "spread_bps": None,
            "imbalance_ratio": 0.5,
            "thin_book": False,
            "depth_levels": 0,
        }

    # Parse bids: each entry has {price, size/quantity}
    bid_depth_usd = 0.0
    best_bid = 0.0
    for i, level in enumerate(bids):
        price = float(level.get("price", 0))
        size = float(level.get("size", level.get("quantity", 0)))
        bid_depth_usd += price * size
        if i == 0:
            best_bid = price

    # Parse asks
    ask_depth_usd = 0.0
    best_ask = 0.0
    for i, level in enumerate(asks):
        price = float(level.get("price", 0))
        size = float(level.get("size", level.get("quantity", 0)))
        ask_depth_usd += price * size
        if i == 0:
            best_ask = price

    # Spread in basis points
    spread_bps: float | None = None
    mid = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask > 0 else 0.0
    if mid > 0:
        spread_bps = round((best_ask - best_bid) / mid * 10_000, 2)

    # Imbalance ratio: 0.5 = balanced, >0.5 = bid-heavy (bullish), <0.5 = ask-heavy
    total_depth = bid_depth_usd + ask_depth_usd
    imbalance_ratio = (bid_depth_usd / total_depth) if total_depth > 0 else 0.5

    # Thin book detection
    thin_book = False
    if order_value is not None and config is not None and total_depth > 0:
        if order_value > config.thin_book_warn_pct * total_depth:
            thin_book = True
            logger.warning(
                "⚠️  THIN BOOK for order $%.0f: visible depth $%.0f "
                "(%.0f%% of depth), slippage risk elevated",
                order_value,
                total_depth,
                (order_value / total_depth) * 100,
            )

    return {
        "available": True,
        "bid_depth_usd": round(bid_depth_usd, 2),
        "ask_depth_usd": round(ask_depth_usd, 2),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_bps": spread_bps,
        "imbalance_ratio": round(imbalance_ratio, 4),
        "thin_book": thin_book,
        "depth_levels": max(len(bids), len(asks)),
    }
