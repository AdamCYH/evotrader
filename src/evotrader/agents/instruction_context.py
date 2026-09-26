"""Build the template values that make agent instructions ticker-agnostic.

Agent instructions used to name the traded instrument literally — "QQQ ~$700",
"Bearish = LONG PSQ". That coupling meant switching instrument required rewriting
prose in seven markdown files, and the loader papered over it with a blunt
``text.replace("SPY", ticker)`` that would rewrite any occurrence of those three
letters anywhere in the document.

Instructions now use placeholders, and this module supplies the values from
configuration. Switching instrument is a settings change.

Available placeholders
----------------------
``{{PRIMARY_TICKER}}``   The instrument being traded.
``{{ALLOWED_TICKERS}}``  Every symbol the constitution permits, comma-separated.
``{{BEARISH_VEHICLE}}``  How a bearish view is expressed for this instrument,
                         derived from what configuration actually allows — an
                         inverse ETF, puts, a short, or nothing.
``{{SCHEDULE}}``         Human-readable cadence of the trading cycle.

An unknown placeholder is left in the text rather than silently blanked: a
visible ``{{FOO}}`` in a prompt is a bug report, an empty string is a mystery.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z_]+)\}\}")


def _bearish_vehicle(config: Any) -> str:
    """Describe how to take a bearish position, from configuration alone.

    Ordered by preference: an inverse ETF needs no margin and no expiry, puts
    need no margin but decay, shorting needs margin. When none is available the
    honest answer is that there is no bearish expression — which the strategy
    instructions must state plainly rather than leaving the agent to invent one.
    """
    asset = getattr(config.settings, "asset", None)
    rules = getattr(config.constitution, "trading_rules", None)

    inverse = getattr(asset, "inverse_ticker", None)
    if inverse:
        primary = asset.primary_ticker
        leverage = abs(getattr(asset, "leverage_of", lambda _t: 1.0)(inverse)) or 1.0
        described = getattr(asset, "describe", lambda t: t)(inverse)

        lines = [
            f"buy {described} — it rises when {primary} falls. A {inverse} buy "
            f"is `direction: LONG`: the bearish thesis lives in the ticker, not "
            f"the direction. No margin and no expiry.",
        ]
        if leverage != 1.0:
            # The number that changes arithmetic rather than understanding. An
            # agent that sizes a -2x fund like ordinary stock takes twice the
            # risk it intended, and nothing downstream would flag it.
            lines.append(
                f"  - SIZING — {inverse} is LEVERAGED. It moves about "
                f"{leverage:g}x {primary}'s daily move, in the opposite "
                f"direction, so $1 of {inverse} carries ${leverage:g} of "
                f"{primary} exposure. To express a bearish view equal to "
                f"shorting $N of {primary}, buy $N/{leverage:g} of "
                f"{inverse}. Sizing it like ordinary stock multiplies your risk "
                f"by {leverage:g}."
            )
            lines.append(
                f"  - DECAY — it resets daily, so it tracks {leverage:g}x the "
                f"DAILY move, not the move across a longer hold. Measured "
                f"tracking error is ~1.2% over a 6-hour hold and grows the "
                f"longer it is held. Use it for short-horizon expression; do "
                f"not park it as a standing hedge."
            )
        return "\n".join(lines)

    if getattr(rules, "allow_options", False):
        return (
            f"buy {asset.primary_ticker} puts. No margin required, but they "
            f"carry expiry and time decay, so size and tenor matter. There is no "
            f"inverse ETF configured for this instrument."
        )

    if getattr(rules, "allow_short_sell", False) and getattr(rules, "allow_margin", False):
        return (
            f"short {asset.primary_ticker} directly. This uses margin, so "
            f"position size is bounded by the constitution's exposure limits."
        )

    return (
        "NOT AVAILABLE for this instrument — no inverse ETF is configured, "
        "options are disabled, and shorting requires margin which is disabled. "
        "A bearish signal means stand aside in cash, not an inverted position."
    )


def _directions_available(config: Any) -> str:
    """State which order directions are actually placeable, and why.

    Verified against the live Robinhood MCP on 2026-09-11:

    * The instrument is not the constraint. ``get_equity_tradability`` reports a
      per-symbol ``short_selling_tradability``, and for MSTR it is ``tradable``.
    * **The account is.** Short-selling shares requires a margin account; the
      agentic account here is ``type: "cash"``. A cash account cannot borrow, so
      it cannot short shares no matter what the symbol allows.
    * ``place_equity_order`` takes ``side`` as a free-text string documented as
      "'buy' or 'sell'". Its own notes say "no short sells" (for fractional), and
      the order lifecycle does contemplate short-sale states
      (``locating``/``locate_failed``) — so the capability exists at the broker,
      gated on account type.

    The failure mode this guards against is not a rejected order. It is an agent
    holding a long, turning bearish, and sending ``side: "sell"`` — which fills,
    and silently sells the position it meant to invert.
    """
    asset = getattr(config.settings, "asset", None)
    rules = getattr(config.constitution, "trading_rules", None)
    primary = getattr(asset, "primary_ticker", "the instrument")

    shorts_ok = getattr(rules, "allow_short_sell", False) and getattr(rules, "allow_margin", False)
    if shorts_ok:
        return (
            f"**LONG and SHORT.** Both directions are permitted for {primary}. "
            f"A short sale is a distinct order side — a plain `sell` CLOSES A "
            f"LONG and never opens a short, so never substitute one for the "
            f"other. Short selling carries unbounded loss (the price can rise "
            f"without limit), so a stop is mandatory, not optional."
        )

    return (
        f"**LONG ONLY.** Short-selling shares of {primary} requires a margin "
        f"account, and this is a cash account — so it is unavailable regardless "
        f"of what the symbol permits. It is also disabled in the constitution "
        f"(`allow_short_sell`/`allow_margin`).\n"
        f"  - `side: 'buy'` opens or adds to a long.\n"
        f"  - `side: 'sell'` CLOSES a long you already hold. It does NOT open a "
        f"short. If a long is open, sending `sell` for a bearish thesis will "
        f"LIQUIDATE that long — an unintended exit, not a short.\n"
        f"  - Never propose or place `direction: SHORT` on {primary}.\n"
        f"To act on a bearish view, {_bearish_vehicle(config)}\n"
        f"This is the limited-risk route: the most a bought put can lose is the "
        f"premium paid, whereas short-selling shares can lose without limit."
    )


def build_instruction_context(config: Any) -> dict[str, str]:
    """Template values for the current configuration."""
    asset = getattr(config.settings, "asset", None)
    rules = getattr(config.constitution, "trading_rules", None)
    primary = getattr(asset, "primary_ticker", "") or ""
    allowed = list(getattr(rules, "allowed_tickers", None) or [])
    if primary and primary not in allowed:
        allowed = [primary, *allowed]

    describe = getattr(asset, "describe", None)
    listed = [describe(t) for t in allowed] if describe else list(allowed)

    context = {
        "PRIMARY_TICKER": primary,
        "ALLOWED_TICKERS": ", ".join(listed) if listed else primary,
        "BEARISH_VEHICLE": _bearish_vehicle(config),
        "DIRECTIONS_AVAILABLE": _directions_available(config),
    }
    schedule = getattr(config.settings, "schedule", None)
    if schedule is not None:
        context["SCHEDULE"] = getattr(schedule, "description", "") or ""
    return context


def render_instructions(text: str, context: dict[str, str]) -> str:
    """Substitute placeholders, warning about any that have no value.

    Deliberately does **not** rewrite bare ticker symbols found in prose. The
    previous loader did (``replace("SPY", ticker)``), which meant a document
    mentioning "SPY" for any reason — an example, a comparison, a benchmark —
    had it silently rewritten.
    """
    unknown: set[str] = set()

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in context:
            return context[key]
        unknown.add(key)
        return match.group(0)

    rendered = _PLACEHOLDER_RE.sub(_sub, text)
    if unknown:
        logger.warning(
            "Instruction template has placeholder(s) with no value: %s — left "
            "verbatim in the prompt so the gap is visible.",
            ", ".join(sorted(unknown)),
        )
    return rendered


def find_hardcoded_tickers(text: str, known: set[str]) -> set[str]:
    """Return ticker-shaped words in *text* that are not in *known*.

    Used by a regression test to stop literal symbols creeping back into
    instruction files. Heuristic by necessity — matches 2-5 uppercase letters
    that look like a symbol — so ``known`` carries the legitimate exceptions.
    """
    candidates = set(re.findall(r"\b[A-Z]{2,5}\b", text))
    return {c for c in candidates if c not in known}
