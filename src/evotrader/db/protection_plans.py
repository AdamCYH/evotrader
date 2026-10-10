"""An entry's approved protection, kept as data until the entry fills.

See ``tools.protection_followup``. ``record_trade`` saves the plan an entry
carries (``protection_plan``); the follow-up reads the waiting plans and marks
each one with what happened to it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from evotrader.db.connection import Database

logger = logging.getLogger(__name__)

#: A plan is waiting for its entry to fill.
PLANNED = "planned"
#: How a plan can end; ``status_reason`` says the rest.
PLACED, COVERED, REFUSED, FAILED, EXPIRED = "placed", "covered", "refused", "failed", "expired"


def plan_problems(
    plan: Any, direction: str, quantity: float, entry_price: float | None
) -> list[str]:
    """What is wrong with a protection plan for an entry, or nothing.

    A plan needs a stop price, quantities that fit inside the entry, a limit
    price for any take-profit shares, and both levels on the right side of the
    entry when its price is known.
    """
    if not isinstance(plan, dict):
        return ["protection_plan must be an object"]
    problems = []

    def number(key: str) -> float | None:
        value = plan.get(key)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            problems.append(f"{key} is not a number: {value!r}")
            return None

    stop_price, stop_qty = number("stop_price"), number("stop_qty")
    tp_price, tp_qty = number("tp_limit_price"), number("tp_qty") or 0.0
    if problems:
        return problems
    if stop_price is None or stop_price <= 0:
        problems.append("stop_price is required and must be above 0")
    if stop_qty is None or stop_qty <= 0:
        problems.append("stop_qty is required and must be above 0")
    if tp_qty < 0:
        problems.append("tp_qty cannot be negative")
    if tp_qty > 0 and (tp_price is None or tp_price <= 0):
        problems.append("tp_limit_price is required when tp_qty is above 0")
    if stop_qty and stop_qty + tp_qty > quantity + 1e-9:
        problems.append(
            f"stop_qty + tp_qty ({stop_qty + tp_qty:g}) is more than the entry's {quantity:g} shares"
        )
    tif = str(plan.get("time_in_force") or "gtc").lower()
    if tif != "gtc":
        problems.append("protective orders are good-till-cancelled: time_in_force must be gtc")
    if entry_price and stop_price and not problems:
        long = str(direction).upper() != "SHORT"
        if (stop_price >= entry_price) if long else (stop_price <= entry_price):
            problems.append(
                f"stop_price {stop_price:g} is not {'below' if long else 'above'} the entry "
                f"price {entry_price:g}"
            )
        if (
            tp_qty
            and tp_price
            and ((tp_price <= entry_price) if long else (tp_price >= entry_price))
        ):
            problems.append(
                f"tp_limit_price {tp_price:g} is not {'above' if long else 'below'} the entry "
                f"price {entry_price:g}"
            )
    return problems


class ProtectionPlanStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def save(
        self,
        *,
        entry_order_id: str,
        entry_trade_id: int | None,
        session_id: str | None,
        ticker: str,
        direction: str,
        entry_quantity: float,
        plan: dict[str, Any],
    ) -> int:
        """Keep a plan for an entry order; a second plan for it replaces the first."""
        now = datetime.now(UTC).isoformat()
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE protection_plans SET status = ?, status_reason = ?, updated_at = ? "
                "WHERE entry_order_id = ? AND status = ?",
                (
                    EXPIRED,
                    "replaced by a newer plan for the same entry",
                    now,
                    entry_order_id,
                    PLANNED,
                ),
            )
            cursor = await conn.execute(
                """
                INSERT INTO protection_plans (
                    created_at, session_id, entry_trade_id, entry_order_id, ticker, direction,
                    entry_quantity, stop_price, stop_qty, tp_limit_price, tp_qty, status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    now,
                    session_id,
                    entry_trade_id,
                    entry_order_id,
                    ticker.upper(),
                    direction.upper(),
                    float(entry_quantity),
                    float(plan["stop_price"]),
                    float(plan["stop_qty"]),
                    float(plan["tp_limit_price"]) if plan.get("tp_limit_price") else None,
                    float(plan.get("tp_qty") or 0),
                    PLANNED,
                ),
            )
            return int(cursor.lastrowid)

    async def planned(self) -> list[dict[str, Any]]:
        """Every plan still waiting for its entry, oldest first."""
        async with self._db.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM protection_plans WHERE status = ? ORDER BY id", (PLANNED,)
            )
            return [dict(r) for r in await cursor.fetchall()]

    async def mark(self, plan_id: int, status: str, reason: str) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE protection_plans SET status = ?, status_reason = ?, updated_at = ? "
                "WHERE id = ?",
                (status, reason, datetime.now(UTC).isoformat(), plan_id),
            )
        logger.info("Protection plan #%d: %s (%s)", plan_id, status, reason)
