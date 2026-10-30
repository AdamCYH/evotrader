"""Place a filled entry's protection when the fill comes after its cycle.

The executor places an entry and its protection in one turn. When the entry is
accepted but not yet filled as the turn ends, the plan (a stop for most of the
new shares, a take-profit tranche for the rest) has nowhere to go: nothing runs
until the next cycle, an hour later, and the new shares sit unprotected. The
plan now travels with the entry as data (``record_trade``'s
``protection_plan``, kept in ``db.protection_plans``), and this follow-up
places it when the fill lands.

After a cycle that leaves a plan waiting, it checks every ``interval_seconds``
for up to ``max_minutes``. For each waiting plan:

* nothing while a trading cycle runs (its executor handles its own orders),
  outside regular hours (a stop cannot rest then), or before the entry fills;
* once the entry has filled, it protects the shares still under no order, up
  to the plan's quantities. It only ADDS orders. It never cancels or resizes
  one: a resting sell reserves its shares, so a larger replacement stop is
  refused while the old one rests, and cancelling first leaves the position
  with no stop at all;
* it refuses, and says why in the trading handoff, when the stop breaks the
  constitution's cap from the fill price, when the price is already through
  the stop, when the console asks for approval of every order, or when the
  risk gate objects;
* otherwise it places the stop, then the take-profit, through the broker
  session the tools use, journals both against the entry with
  ``record_trade``, and writes a SYSTEM line for the next cycle;
* an entry that ended without filling ends its plan, and a plan older than the
  window plus an hour is too old to act on.

It also runs once at the start of every cycle, before the agents read the
book: an entry that filled before the open (a stop cannot rest then) is
protected as the first regular-hours cycle starts, not minutes later when that
cycle's executor gets to it. The two never overlap: they take turns on one
lock, so a plan is placed once.

Off unless ``protection_followup.enabled``: it places real orders without an
agent. Which it is goes in the log at start-up and at every cycle start
(``state_line``). While it is off, a cycle start names each filled entry whose
plan waits in the trading handoff, with the plan's levels, so the cycle's
agents place them instead of working the levels out again: a filled entry
once sat an hour with no stop, the follow-up off, and the next cycle found the
shares bare and the plan nowhere in what it read.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from evotrader.db.protection_plans import (
    COVERED,
    EXPIRED,
    FAILED,
    PLACED,
    REFUSED,
    ProtectionPlanStore,
)

logger = logging.getLogger(__name__)

#: Entry statuses that mean it will never fill.
_ENTRY_ENDED = {"CANCELLED", "REJECTED", "EXPIRED", "FAILED", "DUPLICATE"}

_task: asyncio.Task | None = None
#: One check at a time (the follow-up's, or a cycle start's), with its loop.
_lock: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None


def _tools() -> Any:
    from evotrader.agents import tools

    return tools


def _cycle_running() -> bool:
    try:
        from evotrader.web.server import _active_app
    except Exception:
        return False
    return bool(_active_app is not None and getattr(_active_app.state, "is_running", False))


def _one_at_a_time() -> asyncio.Lock:
    global _lock
    loop = asyncio.get_running_loop()
    if _lock is None or _lock[0] is not loop:
        _lock = (loop, asyncio.Lock())
    return _lock[1]


def _switched_on() -> bool:
    tools = _tools()
    cfg = tools._config
    if cfg is None or tools._db is None or not cfg.settings.protection_followup.enabled:
        return False
    # A dry run places nothing, unless the practice broker stands in.
    return not cfg.settings.dry_run.enabled or tools._sim_proxy is not None


def state_line() -> str:
    """Whether the follow-up is on, in one line for the log."""
    tools = _tools()
    cfg = tools._config
    if cfg is None:
        return "protection follow-up: OFF (not set up)"
    settings = cfg.settings.protection_followup
    if _switched_on():
        return (
            f"protection follow-up: ON (interval {settings.interval_seconds} s, "
            f"window {settings.max_minutes} min)"
        )
    why = (
        "protection_followup.enabled is false"
        if not settings.enabled
        else "a dry run with no practice broker"
    )
    return (
        f"protection follow-up: OFF ({why}; a fill after its cycle is protected "
        "by the next cycle's agents)"
    )


def _note(line: str) -> None:
    """A SYSTEM line in the trading handoff, for the next cycle."""
    from evotrader.tools.trading_handoff import append_system_line

    cfg = _tools()._config
    if cfg is not None:
        append_system_line(cfg.data_dir, f"protection follow-up: {line}")


async def start_followup() -> bool:
    """After a cycle: start checking, if switched on and a plan waits. True when started."""
    global _task
    tools = _tools()
    if not _switched_on():
        return False
    if _task is not None and not _task.done():
        return False  # one at a time; each check reads every waiting plan
    if not await ProtectionPlanStore(tools._db).planned():
        return False
    settings = tools._config.settings.protection_followup
    _task = asyncio.create_task(run_followup(settings.interval_seconds, settings.max_minutes))
    return True


async def protect_at_cycle_start() -> int:
    """At a cycle's start, before its agents: protect fills that waited. Returns plans left.

    Off, it places nothing and names the filled entries whose plans wait
    (``waiting_plan_lines``) in the trading handoff the agents read.
    """
    logger.info(state_line())
    if not _switched_on():
        lines = await waiting_plan_lines()
        if lines:
            from evotrader.tools.trading_handoff import append_system_line

            logger.warning(
                "Protection follow-up is OFF and %d filled entr%s wait for protection: %s",
                len(lines),
                "y" if len(lines) == 1 else "ies",
                " | ".join(lines),
            )
            for line in lines:
                append_system_line(_tools()._config.data_dir, line)
        return 0
    if not await ProtectionPlanStore(_tools()._db).planned():
        return 0
    return await followup_once(at_cycle_start=True)


async def waiting_plan_lines(now: datetime | None = None) -> list[str]:
    """One line per filled entry whose plan waits and whose shares are under no order.

    The plans the follow-up would act on now: the entry filled, the plan is
    within the follow-up's window (an older one is the next cycle's to
    decide), and some of the ticker's shares are under no stop or
    take-profit. Reads only; places nothing.
    """
    tools = _tools()
    cfg = tools._config
    if cfg is None or tools._db is None or tools._journal is None:
        return []
    plans = await ProtectionPlanStore(tools._db).planned()
    if not plans:
        return []
    now = now or datetime.now(UTC)
    max_age = timedelta(minutes=cfg.settings.protection_followup.max_minutes + 60)
    coverage: dict[str, dict] | None = None
    lines = []
    for plan in plans:
        if plan.get("entry_trade_id") is None:
            continue
        if now - datetime.fromisoformat(plan["created_at"]) > max_age:
            continue
        entry = await tools._journal.get_trade_by_id(int(plan["entry_trade_id"]))
        if not entry or str(entry.get("order_status") or "FILLED").upper() != "FILLED":
            continue
        if coverage is None:
            coverage = await tools._protective_coverage()
        need = float((coverage.get(plan["ticker"]) or {}).get("no_order_qty") or 0)
        if need <= 0:
            continue
        take = (
            f", take-profit {float(plan['tp_qty']):g} @ {float(plan['tp_limit_price']):g}"
            if plan.get("tp_qty") and plan.get("tp_limit_price")
            else ""
        )
        lines.append(
            f"PROTECTION PLAN WAITING for {plan['entry_order_id'][:8]} ({plan['ticker']}): "
            f"stop {float(plan['stop_qty']):g} @ {float(plan['stop_price']):g}{take} gtc; "
            f"{need:g} shares are under no order and the follow-up is OFF: "
            "place these this cycle."
        )
    return lines


async def run_followup(interval_seconds: float, max_minutes: float) -> None:
    """Check until no plan waits or the window closes."""
    deadline = datetime.now(UTC) + timedelta(minutes=max_minutes)
    while datetime.now(UTC) < deadline:
        await asyncio.sleep(interval_seconds)
        if _cycle_running():
            continue
        try:
            if not await followup_once():
                return
        except Exception as e:
            logger.error("Protection follow-up check failed: %s", e, exc_info=True)


async def followup_once(now: datetime | None = None, *, at_cycle_start: bool = False) -> int:
    """One check of every waiting plan. Returns how many still wait.

    ``at_cycle_start``: called by the cycle that is starting, which has just
    reconciled the orders and whose agents have not started yet.
    """
    tools = _tools()
    store = ProtectionPlanStore(tools._db)
    async with _one_at_a_time():
        plans = await store.planned()
        if not plans:
            return 0
        if not at_cycle_start:
            try:
                await tools.reconcile_pending_orders()  # promotes fills, ends cancelled orders
            except Exception as e:
                logger.warning("Protection follow-up: order reconciliation failed: %s", e)
        now = now or datetime.now(UTC)
        waiting = 0
        for plan in plans:
            try:
                if await _handle(plan, store, now, at_cycle_start) is None:
                    waiting += 1
            except Exception as e:
                logger.error("Protection plan #%s: %s", plan.get("id"), e, exc_info=True)
                waiting += 1
        return waiting


async def _handle(
    plan: dict[str, Any], store: ProtectionPlanStore, now: datetime, at_cycle_start: bool = False
) -> str | None:
    """Act on one plan. Returns the status it ended in, or None while it waits."""
    tools = _tools()
    settings = tools._config.settings.protection_followup
    max_age = timedelta(minutes=settings.max_minutes + 60)
    created = datetime.fromisoformat(plan["created_at"])
    entry = None
    if plan.get("entry_trade_id") is not None:
        entry = await tools._journal.get_trade_by_id(int(plan["entry_trade_id"]))
    if not entry:
        await store.mark(plan["id"], EXPIRED, "the entry row is not in the journal")
        return EXPIRED
    status = str(entry.get("order_status") or "FILLED").upper()
    if status in _ENTRY_ENDED:
        await store.mark(plan["id"], EXPIRED, f"the entry ended {status} without filling")
        return EXPIRED
    if now - created > max_age:
        await store.mark(plan["id"], EXPIRED, "too old to act on; the next cycle decides")
        return EXPIRED
    if status != "FILLED":
        return None

    from evotrader.tools.market_hours import MarketSession, get_current_session

    if get_current_session() != MarketSession.REGULAR:
        return None  # a stop cannot rest outside regular hours
    return await _protect(plan, entry, store, at_cycle_start)


async def _refuse(store: ProtectionPlanStore, plan: dict[str, Any], why: str) -> str:
    await store.mark(plan["id"], REFUSED, why)
    _note(
        f"did not protect the fill of {plan['entry_order_id'][:8]} ({plan['ticker']}): {why}. "
        "The plan's stop and take-profit are not placed: the next cycle decides."
    )
    return REFUSED


async def _protect(
    plan: dict[str, Any],
    entry: dict[str, Any],
    store: ProtectionPlanStore,
    at_cycle_start: bool = False,
) -> str | None:
    tools = _tools()
    cfg = tools._config
    ticker = plan["ticker"]
    long = str(plan["direction"]).upper() != "SHORT"
    fill = float(entry.get("fill_price") or entry.get("price") or 0)
    stop = float(plan["stop_price"])
    tp = float(plan["tp_limit_price"]) if plan.get("tp_limit_price") else None

    # 1. Only shares still under no order, and never more than the plan's.
    coverage = (await tools._protective_coverage()).get(ticker) or {}
    need = float(coverage.get("no_order_qty") or 0)
    if need <= 0:
        await store.mark(
            plan["id"], COVERED, "the shares were already under a stop or a take-profit"
        )
        return COVERED
    stop_qty, tp_qty = float(plan["stop_qty"]), float(plan["tp_qty"] or 0)
    notes = []
    if need < stop_qty + tp_qty:
        stop_qty, tp_qty = need, 0.0
        notes.append(f"only {need:g} shares were unprotected: all under the stop")

    # 2. The stop against the constitution's cap, from the fill.
    from evotrader.tools.stop_geometry import stop_cap_check

    if fill > 0:
        problem, _ = stop_cap_check(
            fill, stop, plan["direction"], cfg.constitution.risk_limits.max_stop_loss_pct
        )
        if problem:
            return await _refuse(store, plan, problem)

    # 3. The price now: a stop already crossed would sell at once.
    from evotrader.tools.market_hours import _resolve_now

    quote, _ = await tools._fetch_quote(ticker, _resolve_now().astimezone(UTC))
    last = float((quote or {}).get("last") or 0)
    if last <= 0:
        return None  # no quote: check again next time
    if (last <= stop) if long else (last >= stop):
        return await _refuse(
            store, plan, f"the price {last:g} is already through the stop {stop:g}"
        )
    if tp_qty and tp is not None and ((last >= tp) if long else (last <= tp)):
        tp_qty = 0.0
        notes.append(f"the price {last:g} is already past the take-profit {tp:g}: stop only")

    # 4. The same checks an agent's order meets.
    if cfg.settings.require_trade_approval:
        return await _refuse(
            store,
            plan,
            "the console asks for approval of every order and no one is there to give it",
        )
    from evotrader.callbacks.risk_gate import create_risk_gate, enforce_protective_time_in_force

    side = "sell" if long else "buy"
    orders = [
        (
            "STOP_LOSS",
            {
                "symbol": ticker,
                "side": side,
                "type": "stop_market",
                "quantity": f"{stop_qty:g}",
                "stop_price": f"{stop:.2f}",
                "time_in_force": "gtc",
            },
        )
    ]
    if tp_qty and tp is not None:
        orders.append(
            (
                "TAKE_PROFIT",
                {
                    "symbol": ticker,
                    "side": side,
                    "type": "limit",
                    "quantity": f"{tp_qty:g}",
                    "limit_price": f"{tp:.2f}",
                    "time_in_force": "gtc",
                },
            )
        )
    account = await tools._agentic_account_number()
    gate = create_risk_gate(cfg.constitution, dry_run=cfg.settings.dry_run.enabled)
    held = float(coverage.get("held_qty") or 0) or None
    for _, args in orders:
        if account:
            args["account_number"] = account
        violation = enforce_protective_time_in_force(args)
        problems = ([violation] if violation else []) + gate.check_violations(
            args, held_quantity=held
        )
        if problems:
            return await _refuse(store, plan, "; ".join(problems))

    # 5. Place them, journal them against the entry.
    placed: list[str] = []
    session_before = tools._current_session_id
    tools._current_session_id = plan.get("session_id") or session_before
    try:
        for action, args in orders:
            if not at_cycle_start and _cycle_running():
                # A cycle started meanwhile: its executor reads the book now.
                if not placed:
                    return None
                notes.append("a cycle started before the take-profit: stop only")
                break
            response = await tools._call_mcp_tool("place_equity_order", args)
            order = ((response or {}).get("data") or {}).get("order") or {}
            order_id = order.get("id")
            label = "stop" if action == "STOP_LOSS" else "take-profit"
            if not order_id:
                reason = f"the broker did not accept the {label}: {json.dumps(response)[:200]}"
                if placed:
                    reason += f" (placed before it: {', '.join(placed)})"
                await store.mark(plan["id"], FAILED, reason)
                _note(f"for the fill of {plan['entry_order_id'][:8]} ({ticker}): {reason}.")
                return FAILED
            level = args.get("stop_price") or args.get("limit_price")
            await tools.record_trade(
                json.dumps(
                    {
                        "ticker": ticker,
                        "action": action,
                        "direction": "LONG" if long else "SHORT",
                        "quantity": float(args["quantity"]),
                        "order_type": args["type"],
                        "stop_price": float(args["stop_price"]) if "stop_price" in args else None,
                        "limit_price": float(args["limit_price"])
                        if "limit_price" in args
                        else None,
                        "time_in_force": "gtc",
                        "order_id": order_id,
                        "status": "PENDING",
                        "related_trade_id": entry.get("id"),
                        "algo_signal": entry.get("algo_signal"),
                        "hybrid_score": entry.get("hybrid_score"),
                        "llm_signal": entry.get("llm_signal"),
                        "confidence": entry.get("confidence"),
                        "regime": entry.get("regime"),
                        "algo_version": entry.get("algo_version"),
                        "reasoning": (
                            f"SYSTEM: {label} placed after the entry filled, per the protection "
                            f"plan recorded with entry order {plan['entry_order_id']}."
                        ),
                    }
                )
            )
            placed.append(f"{label} {args['quantity']} @ {level} ({order_id[:8]})")
    finally:
        tools._current_session_id = session_before

    summary = ", ".join(placed) + (f" — {'; '.join(notes)}" if notes else "")
    await store.mark(plan["id"], PLACED, summary)
    _note(f"protected the fill of {plan['entry_order_id'][:8]} ({ticker} @ {fill:g}): {summary}.")
    return PLACED
