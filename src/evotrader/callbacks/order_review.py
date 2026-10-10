"""How far a limit order sits from the market when the broker reviews it.

The executor reviews every order with ``review_equity_order`` before placing it.
The broker's answer carries the quote but says nothing about the limit's
distance from it, and an empty ``order_checks`` reads as "clean". A buy limit
taken from an earlier quote can sit well under the ask, rest, and fill only
when the price comes back down through it: it fills when the price is falling
(adverse selection by construction), and nothing said so at the moment of
placing.

``limit_vs_market`` adds the distance to the review: in basis points from the
ask for a buy (from the bid for a sell), and whether the order is marketable
(it would trade at once). Nothing is refused; what to do with it is the
instructions' call.
"""

from __future__ import annotations

import json
from typing import Any


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _review_data(tool_response: Any) -> dict[str, Any] | None:
    """The ``data`` block of a ``review_equity_order`` answer, however it is wrapped."""
    if not isinstance(tool_response, dict):
        return None
    structured = tool_response.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("data"), dict):
        return structured["data"]
    for part in tool_response.get("content") or []:
        if isinstance(part, dict) and part.get("type") == "text":
            try:
                parsed = json.loads(part.get("text") or "")
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict):
                return parsed["data"]
    return None


def limit_vs_market(args: dict[str, Any], tool_response: Any) -> dict[str, Any] | None:
    """The limit's distance from the quote in a review, or None (not a limit, no quote)."""
    data = _review_data(tool_response)
    if data is None:
        return None
    limit = _number(args.get("limit_price") or data.get("limit_price"))
    side = str(args.get("side") or data.get("side") or "").lower()
    if limit is None or side not in ("buy", "sell"):
        return None
    quote = data.get("quote_data") or {}
    reference = "ask" if side == "buy" else "bid"
    price = _number(quote.get(f"{reference}_price"))
    if price is None:
        return None
    marketable = limit >= price if side == "buy" else limit <= price
    out = {
        "side": side,
        "limit_price": limit,
        "reference": reference,
        "reference_price": price,
        "bps": round((limit - price) / price * 1e4, 1),
        "marketable": marketable,
    }
    if not marketable:
        out["note"] = (
            f"This {side} limit is not marketable: it rests until the price comes "
            f"{'down' if side == 'buy' else 'up'} to it."
        )
    return out


def annotate_review(args: dict[str, Any], tool_response: Any) -> Any:
    """``tool_response`` with ``limit_vs_market`` added beside the broker's answer."""
    distance = limit_vs_market(args, tool_response)
    if distance is None or not isinstance(tool_response, dict):
        return tool_response
    return {**tool_response, "limit_vs_market": distance}
