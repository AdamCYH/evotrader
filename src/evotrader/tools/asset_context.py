"""One place to ask "what are we trading?".

Before this, seven modules each answered that question with their own literal —
``primary_ticker = "QQQ"`` in the tools layer, ``or "QQQ"`` in the indicator
registry, ``ticker: str = "SPY"`` in reconciliation, ``.get("ticker", "QQQ")``
twice in the thought log. They disagreed with each other, none of them read
configuration, and switching instrument left stale symbols behind in whichever
code path happened to hit its own default.

Those defaults were also *silent*: a snapshot missing its ticker would be
labelled QQQ and written to the journal as though that were a fact.

So: bind once at startup, read everywhere, and warn loudly when something asks
before anything is bound rather than inventing an answer.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_primary_ticker: str | None = None
_allowed_tickers: tuple[str, ...] = ()
_warned_unbound = False


def bind_asset_context(config: Any) -> None:
    """Record the configured instrument(s). Called once during startup."""
    global _primary_ticker, _allowed_tickers, _warned_unbound

    asset = getattr(getattr(config, "settings", None), "asset", None)
    primary = getattr(asset, "primary_ticker", None)
    _primary_ticker = str(primary).upper() if primary else None

    rules = getattr(getattr(config, "constitution", None), "trading_rules", None)
    allowed = [str(t).upper() for t in (getattr(rules, "allowed_tickers", None) or [])]
    if _primary_ticker and _primary_ticker not in allowed:
        allowed.insert(0, _primary_ticker)
    _allowed_tickers = tuple(allowed)
    _warned_unbound = False

    logger.info(
        "Asset context bound: primary=%s allowed=%s",
        _primary_ticker or "(unset)",
        ", ".join(_allowed_tickers) or "(none)",
    )


def primary_ticker(fallback: str | None = None) -> str | None:
    """The instrument being traded.

    Returns *fallback* only when nothing has been bound, and warns once so the
    gap is visible instead of being papered over with a plausible-looking symbol.
    """
    global _warned_unbound
    if _primary_ticker:
        return _primary_ticker
    if not _warned_unbound:
        logger.warning(
            "primary_ticker() asked before the asset context was bound; "
            "falling back to %r. Any ticker recorded from here is unreliable.",
            fallback,
        )
        _warned_unbound = True
    return fallback


def allowed_tickers() -> tuple[str, ...]:
    """Every symbol the constitution permits, primary first."""
    return _allowed_tickers


def is_allowed(ticker: str | None) -> bool:
    """Whether *ticker* is permitted. Unbound context permits nothing."""
    if not ticker or not _allowed_tickers:
        return False
    return ticker.upper() in _allowed_tickers


def reset_asset_context() -> None:
    """Clear bound state. For tests."""
    global _primary_ticker, _allowed_tickers, _warned_unbound
    _primary_ticker = None
    _allowed_tickers = ()
    _warned_unbound = False
