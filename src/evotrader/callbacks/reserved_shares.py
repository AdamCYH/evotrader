"""A sell refused for want of shares, read as what it is: the shares are reserved.

The broker refuses a sell with "Not enough shares to sell" when the shares it
would sell are already held for working sell orders: a stop, a take-profit.
Once, a failed read had made the protection look absent; the executor's
replacement sells were refused for exactly this reason, and it cancelled
the working protection to make room for them. The refusal was the
broker saying the protection was there.

The executor's ``place_equity_order`` answer now carries the working sell
orders beside such a refusal (``reserved_by``) and a note, so it reads as
cover, not as an obstacle. Nothing is placed or cancelled here.
"""

from __future__ import annotations

from typing import Any

REFUSAL = "not enough shares to sell"

#: Broker order states that mean the order is still working.
WORKING_STATES = frozenset(
    {"unconfirmed", "confirmed", "queued", "partially_filled", "new", "pending"}
)


def _text(tool_response: Any) -> str:
    if not isinstance(tool_response, dict):
        return str(tool_response or "")
    parts = [
        str(part.get("text") or "")
        for part in tool_response.get("content") or []
        if isinstance(part, dict)
    ]
    parts += [str(tool_response.get(key) or "") for key in ("error", "detail", "message")]
    return " ".join(parts)


def is_reserved_shares_refusal(args: dict[str, Any] | None, tool_response: Any) -> bool:
    """A sell the broker refused because the shares are held for other sells."""
    if str((args or {}).get("side") or "").lower() != "sell":
        return False
    return REFUSAL in _text(tool_response).lower()


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def reserving_orders(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The working sell orders in a broker listing, described."""
    out = []
    for order in orders:
        if str(order.get("side") or "").lower() != "sell":
            continue
        if str(order.get("state") or "").lower() not in WORKING_STATES:
            continue
        out.append(
            {
                "order_id": order.get("id") or order.get("order_id"),
                "quantity": _number(order.get("quantity")),
                "type": order.get("type"),
                "trigger": order.get("trigger"),
                "stop_price": _number(order.get("stop_price")),
                "limit_price": _number(order.get("price")),
                "state": order.get("state"),
                "time_in_force": order.get("time_in_force"),
            }
        )
    return out


def annotate_refusal(tool_response: Any, reserved: list[dict[str, Any]] | None) -> dict[str, Any]:
    """The refusal with the orders holding the shares beside it.

    ``reserved`` is None when the broker's list could not be read.
    """
    out = dict(tool_response) if isinstance(tool_response, dict) else {"response": tool_response}
    out["reserved_by"] = reserved
    if reserved:
        held = sum(o["quantity"] or 0.0 for o in reserved)
        out["note"] = (
            f"Not enough shares to sell: the broker holds {held:g} shares for the "
            "working sell orders in reserved_by. That is protection that exists, so "
            "coverage is not none. Read the book again; do not cancel these orders to "
            "make room."
        )
    elif reserved is None:
        out["note"] = (
            "Not enough shares to sell: the broker holds the shares for other sell "
            "orders, and its order list could not be read to name them. It is not a "
            "missing stop: read the broker's order list before anything else, and do "
            "not cancel an order to make room."
        )
    else:
        out["note"] = (
            "Not enough shares to sell, and the broker's list shows no working sell "
            "order for this symbol: the shares may be fewer than the order asks. "
            "Check the position's quantity before resubmitting."
        )
    return out
