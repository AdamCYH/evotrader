"""Risk gate callback — last-chance validation before order execution.

Provides ``create_guarded_order_tool`` which wraps the MCP order
placement tool with constitution enforcement, dry-run blocking, and
audit logging. This is the final safety layer before any real money
is committed.

Compatible with any agent framework — uses plain function wrapping.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from evotrader.callbacks.exit_policy import (
    exceeds_holdings,
    is_position_reducing,
)
from evotrader.models.config import Constitution

logger = logging.getLogger(__name__)

# Trading tools that require risk gate validation
_GATED_TOOLS = frozenset(
    {
        "place_stock_order",
        "place_equity_order",
        "place_option_order",
        "place_crypto_order",
    }
)

# Every tool that places or changes an order must be checked here. The broker's
# tool list is read from its server at start-up, so a new order tool (a
# trailing stop, a replace-order) can appear without a code change — and one
# this gate does not know would reach the broker with no check at all. So a
# tool whose name says it places or changes an order, but which is not in
# _GATED_TOOLS, is refused until someone adds it there with a check. Cancels,
# reviews and reads place nothing and are not matched.
_ORDER_TOOL_NAME = re.compile(
    r"^(place|replace|submit|modify|edit|amend|create|update)_\w*order", re.IGNORECASE
)


def is_unchecked_order_tool(name: str) -> bool:
    """True for a tool that looks like it places or changes an order but has no check here."""
    return name not in _GATED_TOOLS and bool(_ORDER_TOOL_NAME.match(name))


def create_risk_gate(constitution: Constitution, dry_run: bool = True) -> RiskGate:
    """Create a risk gate configured with the current constitution.

    Args:
        constitution: The inviolable risk rules to enforce.
        dry_run: If True, block all trading tool calls (paper trading mode).

    Returns:
        A RiskGate instance with validate() method.
    """
    return RiskGate(constitution=constitution, dry_run=dry_run)


class RiskGate:
    """Validates trading operations against the constitution.

    This class is used as a tool wrapper — it sits between the agent's
    decision and the actual MCP call, enforcing constitutional limits.
    """

    def __init__(self, constitution: Constitution, dry_run: bool = True) -> None:
        self._constitution = constitution
        self._dry_run = dry_run
        self._blocked_count = 0
        self._approved_count = 0

    def check_violations(
        self,
        order_args: dict[str, Any],
        held_quantity: float | None = None,
    ) -> list[str]:
        """Check order arguments against the constitution.

        Args:
            order_args: The order parameters.
            held_quantity: Units of this instrument currently held, if known.
                Used to tell a position-reducing sell from a short sale.
                ``None`` means "could not be resolved" and is treated
                permissively for sells — see :func:`_check_constitution`.

        Returns:
            List of violation strings (empty if valid).
        """
        return _check_constitution(order_args, self._constitution, held_quantity)

    def is_exit_order(self, order_args: dict[str, Any], held_quantity: float | None = None) -> bool:
        """True when the order reduces a position it holds; see ``_is_exit_order``."""
        return _is_exit_order(order_args, held_quantity)

    def summarise_order(self, order_args: dict[str, Any]) -> str:
        """Create a summary of the order."""
        return _summarise_order(order_args)

    def validate_order(
        self,
        order_args: dict[str, Any],
        held_quantity: float | None = None,
    ) -> dict[str, Any]:
        """Validate an order against the constitution.

        Args:
            order_args: The order parameters (ticker, side, quantity, price, etc.)
            held_quantity: Units currently held, if known. Threaded through so
                this path cannot diverge from :meth:`check_violations` — the
                whole defect class this review addresses was two gates
                disagreeing about the same order.

        Returns:
            Dict with 'allowed' (bool), 'violations' (list), and 'action' taken.
        """
        violations = _check_constitution(order_args, self._constitution, held_quantity)

        # Dry-run mode: block all trading
        if self._dry_run:
            self._blocked_count += 1
            logger.warning(
                "[RISK GATE] DRY RUN — would have placed order: %s (blocked count: %d)",
                _summarise_order(order_args),
                self._blocked_count,
            )
            return {
                "allowed": False,
                "action": "DRY_RUN_BLOCKED",
                "violations": [],
                "order_summary": _summarise_order(order_args),
            }

        # Constitution violations: hard block
        if violations:
            self._blocked_count += 1
            logger.error(
                "[RISK GATE] BLOCKED: %s — violations: %s",
                _summarise_order(order_args),
                "; ".join(violations),
            )
            return {
                "allowed": False,
                "action": "CONSTITUTION_BLOCKED",
                "violations": violations,
            }

        # All checks passed
        self._approved_count += 1
        logger.info("[RISK GATE] APPROVED: %s", _summarise_order(order_args))
        return {
            "allowed": True,
            "action": "APPROVED",
            "violations": [],
        }

    @property
    def stats(self) -> dict[str, int]:
        """Return gate statistics."""
        return {
            "approved": self._approved_count,
            "blocked": self._blocked_count,
        }


# Broker order types that exist to PROTECT a position. A protective order that
# expires at the close is worse than none: the book reads "covered" all day and
# the position is naked the next morning without anyone having decided that.
_PROTECTIVE_ORDER_TYPES = frozenset({"stop_market", "stop_limit", "stop", "trailing_stop"})
_PROTECTIVE_TIF = "gtc"


def enforce_protective_time_in_force(order_args: dict[str, Any]) -> str | None:
    """Make a protective equity order good-till-cancelled, in code.

    Returns a violation string if the order must be refused, else ``None``.
    MUTATES ``order_args`` to inject ``time_in_force='gtc'`` when absent.

    Why in code and not prose: the TIF was chosen by the executor LLM per call.
    A stop placed on 2026-09-14 carried ``gtc`` and rested three sessions; one
    placed on 2026-09-17, from the same agent under the same instructions, carried
    nothing, defaulted to a DAY order at the broker, and expired at the close.
    Reconcile stamped it "Order cancelled" — indistinguishable from an agent
    cancel — and the next morning the strategy agent read "the safety net
    isn't there" and sold the position pre-market, just before a large rally.

    Scope: order ``type`` in :data:`_PROTECTIVE_ORDER_TYPES` only. A take-profit
    is a plain ``limit`` sell and is indistinguishable from a close by its args,
    so it is covered by the executor instructions (v009) rather than here.
    """
    if _is_option_order(order_args):
        return None
    otype = str(order_args.get("type") or order_args.get("order_type") or "").lower()
    if otype not in _PROTECTIVE_ORDER_TYPES:
        return None
    tif = str(order_args.get("time_in_force") or "").lower()
    if tif and tif != _PROTECTIVE_TIF:
        return (
            f"Protective order ({otype}) carries time_in_force={tif!r}. A day "
            f"stop expires at the session close and leaves the position naked "
            f"overnight (as on 2026-09-17). Protective orders must be "
            f"'{_PROTECTIVE_TIF}'."
        )
    if not tif:
        logger.warning(
            "[RISK GATE] %s order for %s arrived with no time_in_force; injecting "
            "'%s'. The broker would have defaulted this to a DAY order.",
            otype,
            order_args.get("ticker") or order_args.get("symbol") or "<unknown>",
            _PROTECTIVE_TIF,
        )
        order_args["time_in_force"] = _PROTECTIVE_TIF
    return None


def _is_option_order(order_args: dict[str, Any]) -> bool:
    """Detect if the order arguments describe an option trade."""
    return "legs" in order_args


def _is_exit_order(
    order_args: dict[str, Any],
    held_quantity: float | None,
) -> bool:
    """True when this order reduces an existing position rather than opening one.

    Exits must never be blocked by *entry* policy — see
    :mod:`evotrader.callbacks.exit_policy` for why that asymmetry is
    deliberate.
    """
    if _is_option_order(order_args):
        legs = order_args.get("legs", []) or []
        return bool(legs) and legs[0].get("position_effect", "").lower() == "close"
    return is_position_reducing(order_args.get("side", ""), held_quantity)


def _check_constitution(
    order_args: dict[str, Any],
    constitution: Constitution,
    held_quantity: float | None = None,
) -> list[str]:
    """Check an order's arguments against constitution rules.

    Args:
        order_args: The order parameters.
        held_quantity: Units currently held, or ``None`` when unresolvable.
            ``None`` is NOT the same as ``0.0``: zero means "verified flat, so
            a sell would open a short", while None means "unverified", which
            resolves toward allowing the exit.

    Returns a list of violation messages (empty if order is valid).
    """
    violations: list[str] = []
    rules = constitution.trading_rules
    limits = constitution.risk_limits

    is_option = _is_option_order(order_args)
    is_exit = _is_exit_order(order_args, held_quantity)

    if is_option:
        from evotrader.agents.tools import OPTION_ID_TO_TICKER

        legs = order_args.get("legs", []) or []
        if not legs:
            violations.append("Option order has no legs")
            return violations

        leg = legs[0]
        option_id = leg.get("option_id")
        side = leg.get("side", "").lower()
        position_effect = leg.get("position_effect", "").lower()

        ticker = OPTION_ID_TO_TICKER.get(option_id, "")
        if not option_id:
            violations.append("Option order is missing option_id in leg")
        elif not ticker:
            # Logging warning instead of hard blocking to be safe on restart cache misses
            logger.warning(
                "option_id '%s' not found in cache. Bypassing ticker verification.", option_id
            )
            ticker = rules.allowed_tickers[0]  # Fallback to allow validation to proceed

        quantity = float(order_args.get("quantity", 0))
        price = float(order_args.get("price", 0))

        # Check option rules
        if not rules.allow_options:
            violations.append("Option trading is disabled in constitution")

        if side == "sell" and position_effect == "open":
            violations.append("Option writing (selling to open) is disabled in constitution")

        # Options use a 100x multiplier for total premium order value
        order_value = quantity * price * 100
    else:
        # Equity/Crypto order logic
        ticker = order_args.get("ticker", order_args.get("symbol", ""))
        quantity = float(order_args.get("quantity", 0))
        price = float(order_args.get("price", order_args.get("limit_price", 0)))
        side = order_args.get("side", "").lower()
        order_value = quantity * price

        # ── SHORT-SALE PERMISSION ────────────────────────────
        # `sell` is NOT a synonym for `sell short`. With shares on hand it
        # REDUCES a long — it is how every stop-loss, take-profit and close is
        # expressed. It opens a short only for the quantity EXCEEDING what is
        # held, so that excess is what this rule must fire on.
        #
        # Gating on `side == "sell"` alone made every long position
        # permanently unexitable. Not hypothetical: `allow_short_sell` was set
        # false on 2026-09-10 to match `allow_margin: false`, and on
        # 2026-09-11 five consecutive risk-APPROVED MSTR exits (two
        # protective stops, a trim, a full take-profit, and weekend
        # split coverage) were all blocked here. The position ran unprotected
        # for seven hours through a 6.4%-ATR session and was closed by hand.
        #
        # The option branch above already models this correctly, by qualifying
        # the same check with `position_effect == "open"`.
        if side == "sell" and not rules.allow_short_sell:
            if held_quantity is None:
                # Unknown is not zero. Allowing an unverified reduction risks
                # flattening a position that was already flat; blocking it
                # risks riding an unhedged position through a gap. The second
                # is unbounded, so this resolves toward allowing.
                logger.warning(
                    "[RISK GATE] Could not resolve held quantity for %s; "
                    "ALLOWING the sell. Trapping a position the agent is "
                    "trying to exit is a larger risk than permitting an "
                    "unverified reduction.",
                    ticker or "<unknown>",
                )
            elif exceeds_holdings(quantity, held_quantity):
                violations.append(
                    f"Sell quantity {quantity:g} exceeds the "
                    f"{held_quantity:g} unit(s) held in {ticker}. The excess "
                    f"would open a short position, which is disabled in the "
                    f"constitution (allow_short_sell=false)."
                )

    # ── TICKER ALLOWLIST ─────────────────────────────────────
    # `allowed_tickers` is an ENTRY policy. Enforcing it on exits strands any
    # position whose symbol was later removed from the list, with no legitimate
    # way to act on it from inside the system.
    #
    # Live example this fix addresses: the 2026-09-10 switch to MSTR narrowed
    # the list to ["MSTR"], leaving a QQQ position that could
    # be neither stopped nor sold, and which had carried no protective order
    # since 2026-09-07. Entries in unlisted symbols stay blocked.
    if ticker and ticker not in rules.allowed_tickers:
        if is_exit:
            logger.warning(
                "[RISK GATE] Permitting exit of %s, no longer in "
                "allowed_tickers %s — closing a legacy position is always "
                "allowed. Entries remain blocked.",
                ticker,
                rules.allowed_tickers,
            )
        else:
            violations.append(f"Ticker '{ticker}' not in allowed list: {rules.allowed_tickers}")

    # The per-order dollar cap limits NEW exposure. An order that only reduces a
    # holding returns exposure instead, and capping it trapped any position
    # larger than the cap: it could be neither closed nor fully protected by
    # one order. A sell beyond the holding opens a short, so it stays capped.
    purely_reducing = is_exit and (
        held_quantity is None or not exceeds_holdings(quantity, held_quantity)
    )
    if quantity > 0 and price > 0 and not purely_reducing:
        if order_value > limits.max_order_value_usd:
            violations.append(
                f"Order value ${order_value:.2f} exceeds limit ${limits.max_order_value_usd:.2f}"
            )

    if order_args.get("margin", False) and not rules.allow_margin:
        violations.append("Margin trading is disabled in constitution")

    return violations


def _summarise_order(args: dict[str, Any]) -> str:
    """Create a concise summary of an order for logging."""
    if _is_option_order(args):
        from evotrader.agents.tools import OPTION_ID_TO_TICKER

        legs = args.get("legs", []) or []
        if legs:
            leg = legs[0]
            option_id = leg.get("option_id")
            side = leg.get("side", "?").upper()
            effect = leg.get("position_effect", "?").upper()
            ticker = OPTION_ID_TO_TICKER.get(option_id, f"option:{option_id[:8]}")
            qty = args.get("quantity", "?")
            price = args.get("price", "?")
            return f"{side} TO {effect} {qty}x {ticker} contract @ ${price}"
        return "Invalid option order (no legs)"

    ticker = args.get("ticker", args.get("symbol", "?"))
    side = args.get("side", "?")
    qty = args.get("quantity", "?")
    price = args.get("price", args.get("limit_price", "?"))
    return f"{side.upper()} {qty}x {ticker} @ ${price}"
