"""Shared exit-vs-entry predicates for the two risk gates.

EvoTrader enforces constitutional rules in two places:

* ``agents/tools.py`` → ``check_risk_limits`` — the Risk Manager Agent's
  advisory verdict, reasoned about in natural language before an order is
  proposed.
* ``callbacks/risk_gate.py`` → ``_check_constitution`` — the deterministic
  pre-execution callback, the last thing to run before real money moves.

On 2026-09-11 those two gates disagreed and the disagreement cost money. The
Risk Manager tested ``direction == "SHORT"`` (correct); the pre-execution gate
tested ``side == "sell"`` (wrong — with shares on hand a sell REDUCES a long).
Five consecutive exits were stamped ``APPROVED (0 violations, 0 warnings)`` by
the first gate and then rejected as ``CONSTITUTION_BLOCKED`` by the second. A
position ran unprotected for seven hours through a session whose ATR was 6.4%
of price, and the operator had to close it by hand.

This module exists so that the *policy* lives in exactly one place even though
the two gates see different information about an order. Neither gate should
re-derive these rules locally.

THE GOVERNING PRINCIPLE
-----------------------
**Entry policy must never be enforced on an exit.** Rules like the ticker
allowlist, the short-sale ban and the consecutive-loss circuit breaker all
exist to stop the system from taking on NEW risk. Applying them to an order
that reduces existing risk inverts their purpose: it traps the position.

Trapping a position the agent is actively trying to close is strictly more
dangerous than permitting the close, because the downside of a wrongly-allowed
exit is bounded (a position is flat that need not have been) while the downside
of a wrongly-blocked exit is not (an unhedged position through a gap).

That asymmetry is why every ambiguous case here resolves toward allowing the
exit, and why an unresolvable holding is treated permissively rather than as
zero.
"""

from __future__ import annotations

# Order actions that reduce or close an existing position. Anything not in
# this set is treated as opening new exposure.
EXIT_ACTIONS = frozenset({"CLOSE", "STOP_LOSS", "TAKE_PROFIT"})

# Floating-point tolerance when comparing an order quantity against the
# quantity actually held. Fractional shares carry rounding noise, and a
# 5.0000000001-vs-5.0 mismatch must not read as an attempt to open a short.
QTY_TOLERANCE = 1e-4


def is_exit_action(action: str | None) -> bool:
    """True when ``action`` names an order that reduces an existing position.

    Used by the Risk Manager gate, which knows the semantic action but not the
    broker-level side.
    """
    return bool(action) and action.strip().upper() in EXIT_ACTIONS


def is_position_reducing(side: str | None, held_quantity: float | None) -> bool:
    """True when a sell is backed by holdings, i.e. it reduces rather than shorts.

    Used by the pre-execution gate, which knows the broker-level side and the
    resolved holding but not the semantic action.

    ``held_quantity is None`` means "could not be resolved" and is deliberately
    NOT treated as reducing — callers handle the unknown case explicitly and
    permissively rather than silently inferring an answer here.
    """
    if held_quantity is None:
        return False
    return (side or "").strip().lower() == "sell" and held_quantity > 0


def exceeds_holdings(quantity: float, held_quantity: float) -> bool:
    """True when a sell quantity is larger than the position it claims to close.

    The excess is what would actually open a short, so this — not the bare fact
    of a sell — is the condition the short-sale ban should fire on.
    """
    return quantity > held_quantity + QTY_TOLERANCE
