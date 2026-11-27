"""The broker's side of an order outranks the action an agent recorded for it.

For an equity held long, a sell can only reduce the position and a buy can
only open or add to it. A row that says otherwise is mislabelled: a sell
recorded as an entry (action OPEN) becomes a lot the account never bought,
the position check then finds more shares in the journal than at the broker,
and its sync rows close the surplus at the day's price, booking a loss that
never happened while the sale's own result is never booked.

``action_for_side`` is the one rule, used where the journal meets the broker:
``record_trade`` when the executor records an order, the pending-order check
when it next reads the order, and the position sync before it closes a
surplus. Only an explicit LONG is judged; a sell recorded as a SHORT entry,
or a buy closing a SHORT, is what those words say.
"""

from __future__ import annotations

from evotrader.models.trade import STOP_ORDER_TYPES

#: The actions that reduce a position.
EXIT_ACTIONS: frozenset[str] = frozenset({"CLOSE", "TAKE_PROFIT", "STOP_LOSS"})


def broker_side(order: dict | None) -> str | None:
    """``"buy"`` or ``"sell"`` from a broker order payload; None when it says neither."""
    side = str((order or {}).get("side") or "").strip().lower()
    return side if side in ("buy", "sell") else None


def action_for_side(
    action: str | None,
    direction: str | None,
    side: str | None,
    order_type: str | None = None,
    time_in_force: str | None = None,
) -> str | None:
    """The action the broker's side implies, when the recorded one contradicts it.

    None when the recorded action stands (it agrees with the side, the side is
    unknown, or the row is not an explicit LONG). For a sell recorded as OPEN:
    STOP_LOSS for a stop order, TAKE_PROFIT for a resting (gtc) limit, CLOSE
    otherwise. For a buy recorded as an exit: OPEN.
    """
    act = str(action or "").upper()
    if str(direction or "").upper() != "LONG" or side not in ("buy", "sell"):
        return None
    if side == "sell" and act == "OPEN":
        kind = str(order_type or "").lower()
        if kind in STOP_ORDER_TYPES:
            return "STOP_LOSS"
        if kind == "limit" and str(time_in_force or "").lower() == "gtc":
            return "TAKE_PROFIT"
        return "CLOSE"
    if side == "buy" and act in EXIT_ACTIONS:
        return "OPEN"
    return None


def side_note(old: str, new: str, side: str) -> str:
    """The reason a corrected row carries (``broker_status_reason``)."""
    return f"SIDE_CORRECTED from {old} to {new}: the broker's order is a {side}"
