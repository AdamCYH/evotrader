"""Where a protective stop may sit: the constitution's cap and the ATR stop, side by side.

The constitution caps a stop's distance from the entry
(``risk_limits.max_stop_loss_pct``); the strategy sizes stops in daily ATRs
(``position_sizing.default_stop_loss_atr_multiplier``). On a quiet stock the cap
is several ATRs away and never binds. On a volatile instrument, a leveraged
fund above all, whose daily ATR is a large share of its price, the cap sits
inside the ATR stop: an ATR-sized stop is refused, and only a stop at or inside
the cap can be placed. The agents saw the ATR and not the cap, so they proposed
stops the risk manager refused, and then concluded that the instrument could
not be traded at all. The arithmetic is here so neither has to be inferred.

Percentages are percent (17.6 = 17.6%), as the constitution writes
``max_stop_loss_pct``.
"""

from __future__ import annotations

import math
from typing import Any

#: A stop exactly at the cap is within it; float arithmetic must not say otherwise.
_TOLERANCE = 1e-9


def _ceil_cent(value: float) -> float:
    """Up to the cent: a long's stop at the cap, so rounding cannot push it outside."""
    return math.ceil(round(value * 100, 6)) / 100


def _floor_cent(value: float) -> float:
    """Down to the cent: a short's stop at the cap, for the same reason."""
    return math.floor(round(value * 100, 6)) / 100


def stop_at_cap(price: float, max_stop_loss: float, *, long: bool = True) -> float:
    """The widest stop the cap permits for an entry at ``price`` (``max_stop_loss`` a fraction)."""
    if long:
        return _ceil_cent(price * (1 - max_stop_loss))
    return _floor_cent(price * (1 + max_stop_loss))


def stop_geometry(
    last: float | None,
    atr: float | None,
    max_stop_loss: float | None,
    stop_atr: float | None = None,
) -> dict[str, Any] | None:
    """The stop arithmetic for an entry at ``last``, or None without a price, ATR or cap.

    ``max_stop_loss`` is the constitution's cap as a fraction (0.20) and
    ``stop_atr`` the configured ATR multiple of a stop (1.5). ``cap_stop_atr``
    is the widest permitted stop in ATRs; ``cap_binding`` says the ATR stop is
    wider than that, so the stop goes at the cap (``stop_at_cap_long``) or
    closer, and the size follows from that distance.
    """
    if not last or last <= 0 or not atr or atr <= 0 or not max_stop_loss or max_stop_loss <= 0:
        return None
    atr_share = atr / last
    cap_stop_atr = max_stop_loss / atr_share
    out: dict[str, Any] = {
        "price": round(last, 4),
        "atr": round(atr, 4),
        "atr_pct": round(atr_share * 100, 2),
        "max_stop_loss_pct": round(max_stop_loss * 100, 2),
        "cap_stop_atr": round(cap_stop_atr, 2),
        "stop_at_cap_long": stop_at_cap(last, max_stop_loss, long=True),
        "stop_at_cap_short": stop_at_cap(last, max_stop_loss, long=False),
    }
    if stop_atr and stop_atr > 0:
        out.update(
            stop_atr=stop_atr,
            cap_binding=cap_stop_atr < stop_atr,
            stop_at_atr_long=round(last - stop_atr * atr, 2),
            stop_at_atr_short=round(last + stop_atr * atr, 2),
        )
    return out


def stop_cap_check(
    price: float, stop_price: float, direction: str, max_stop_loss: float
) -> tuple[str | None, dict[str, Any]]:
    """A violation message when ``stop_price`` breaks the cap for an entry at ``price``.

    Returns ``(message or None, context)``. A stop on the wrong side of the
    entry protects nothing and is a violation too.
    """
    long = str(direction).upper() != "SHORT"
    side = "long" if long else "short"
    distance = (price - stop_price) / price if long else (stop_price - price) / price
    level = stop_at_cap(price, max_stop_loss, long=long)
    context = {
        "stop_price": stop_price,
        "stop_distance_pct": round(distance * 100, 2),
        "max_stop_loss_pct": round(max_stop_loss * 100, 2),
        "stop_at_cap": level,
    }
    if distance <= 0:
        where = "below" if long else "above"
        return (
            f"Stop {stop_price:g} is not {where} the entry price {price:g}, so it does not "
            f"protect a {side}",
            context,
        )
    if distance > max_stop_loss + _TOLERANCE:
        bound = "above" if long else "below"
        return (
            f"Stop {stop_price:g} is {distance * 100:.1f}% from the entry price {price:g}; the "
            f"constitution's max_stop_loss_pct is {max_stop_loss * 100:g}%. For a {side} the "
            f"stop must be at or {bound} {level:.2f}",
            context,
        )
    return None, context
