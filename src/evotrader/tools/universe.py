"""Resolve the trading universe for a ticker — its constituents and weights.

Why this exists
---------------
The system used to carry a hardcoded set of eleven "top constituents" and apply
it to whatever was being traded. That is wrong in three separate ways:

1. **It assumed the ticker.** Change ``asset.primary_ticker`` from QQQ to SPY,
   IWM, or a single stock and the list silently stops describing reality.
2. **It went stale.** Checked against live data, that list omitted **MU**
   (Micron, QQQ's 4th largest holding at 4.75%) and **AMD** (3.37%), while
   including **TSM**, which is not in the Nasdaq-100 at all. Earnings-event
   detection was therefore blind to two of QQQ's largest semiconductor
   positions — a trading-quality defect, not a cost one.
3. **It was non-deterministic.** ``list(TOP_CONSTITUENTS)[:5]`` slices a *set*,
   and CPython randomises string hashing per process, so which five constituents
   got checked varied between runs.

This module replaces all of that with resolution from live data, keyed only on
the ticker being traded. No ETF list is hardcoded: whether a ticker is a fund is
something we *ask*, and a ticker with no fund data resolves to itself.

Design
------
* **Provider chain.** ``yfinance`` is used first because it is already a project
  dependency, needs no API key, and runs locally — so the trading path never
  waits on a third-party API to learn what it trades. The chain is ordered and
  extensible; a provider that returns a fuller holdings list (Alpha Vantage's
  ``ETF_PROFILE``, say) can be appended without touching callers.
* **Disk cache with TTL.** Holdings change slowly. A cached answer is used until
  it expires, and a *stale* cached answer is preferred over no answer at all when
  resolution fails — degrading to "we know a bit less" rather than "we know
  nothing".
* **Deterministic output.** Always sorted by weight descending, then symbol, so
  two runs on the same data select the same universe.
* **Multi-ticker by construction.** Every entry point takes a collection of
  tickers, so trading several instruments needs no change here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Selection defaults. Deliberately generous: an extra constituent costs a few
# tokens, whereas missing one that reports earnings can cost a trade.
DEFAULT_TOP_N = 15
DEFAULT_MIN_WEIGHT_PCT = 1.0
DEFAULT_CACHE_TTL_DAYS = 7


@dataclass(frozen=True)
class Holding:
    """One constituent of a fund."""

    symbol: str
    weight: float  # fraction of the fund, 0.0-1.0
    name: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "weight": self.weight, "name": self.name}


@dataclass(frozen=True)
class Universe:
    """What a ticker is made of.

    ``is_fund`` False means the ticker is its own universe — a single stock.
    ``coverage`` is the summed weight of the holdings we know about, so callers
    can tell "the top 10, covering 46% of the fund" from "everything".
    """

    ticker: str
    is_fund: bool
    holdings: tuple[Holding, ...] = ()
    coverage: float = 0.0
    source: str = "unknown"
    resolved_at: str = ""
    stale: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "is_fund": self.is_fund,
            "holdings": [h.as_dict() for h in self.holdings],
            "coverage": round(self.coverage, 6),
            "source": self.source,
            "resolved_at": self.resolved_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, stale: bool = False) -> Universe:
        return cls(
            ticker=data["ticker"],
            is_fund=bool(data.get("is_fund")),
            holdings=tuple(
                Holding(
                    symbol=h["symbol"],
                    weight=float(h.get("weight", 0.0)),
                    name=h.get("name", ""),
                )
                for h in data.get("holdings", [])
            ),
            coverage=float(data.get("coverage", 0.0)),
            source=data.get("source", "cache"),
            resolved_at=data.get("resolved_at", ""),
            stale=stale,
        )

    def select(
        self,
        top_n: int = DEFAULT_TOP_N,
        min_weight_pct: float = DEFAULT_MIN_WEIGHT_PCT,
    ) -> tuple[str, ...]:
        """Symbols worth watching: the top *n* plus anything above a weight floor.

        The traded ticker itself always comes first — it is the instrument, not a
        constituent, and dropping it would be nonsensical.
        """
        selected: list[str] = [self.ticker]
        floor = min_weight_pct / 100.0

        ranked = sorted(self.holdings, key=lambda h: (-h.weight, h.symbol))
        for index, holding in enumerate(ranked):
            in_scope = index < top_n or holding.weight >= floor
            if in_scope and holding.symbol not in selected:
                selected.append(holding.symbol)

        return tuple(selected)


def _self_universe(ticker: str, source: str = "self") -> Universe:
    """A ticker that is not a fund is its own universe."""
    return Universe(
        ticker=ticker,
        is_fund=False,
        holdings=(),
        coverage=1.0,
        source=source,
        resolved_at=datetime.now(UTC).isoformat(),
    )


# ── Providers ─────────────────────────────────────────────────────


def _resolve_via_yfinance(ticker: str) -> Universe | None:
    """Resolve holdings locally with yfinance. Blocking — call in a thread.

    Returns None when the lookup fails outright (so the caller can fall back to
    cache), and a non-fund ``Universe`` when the ticker simply isn't a fund.
    """
    try:
        import yfinance as yf
    except ImportError:  # pragma: no cover - yfinance is a project dependency
        logger.warning("yfinance unavailable — cannot resolve holdings for %s", ticker)
        return None

    try:
        handle = yf.Ticker(ticker)
        try:
            funds = handle.funds_data
            table = funds.top_holdings
        except Exception:
            # No fund data → an ordinary instrument, which is a real answer.
            return _self_universe(ticker, source="yfinance")

        if table is None or len(table) == 0:
            return _self_universe(ticker, source="yfinance")

        holdings: list[Holding] = []
        for symbol, row in table.iterrows():
            weight = row.get("Holding Percent")
            if weight is None:
                continue
            try:
                weight = float(weight)
            except (TypeError, ValueError):
                continue
            holdings.append(
                Holding(
                    symbol=str(symbol).upper(),
                    weight=weight,
                    name=str(row.get("Name", "") or ""),
                )
            )

        if not holdings:
            return _self_universe(ticker, source="yfinance")

        holdings.sort(key=lambda h: (-h.weight, h.symbol))
        return Universe(
            ticker=ticker,
            is_fund=True,
            holdings=tuple(holdings),
            coverage=sum(h.weight for h in holdings),
            source="yfinance",
            resolved_at=datetime.now(UTC).isoformat(),
        )
    except Exception as exc:
        logger.info("yfinance holdings lookup failed for %s: %s", ticker, exc)
        return None


# Ordered provider chain. Local first: the trading path should not depend on a
# remote call to learn what it is trading.
_PROVIDERS: tuple[Any, ...] = (_resolve_via_yfinance,)


# ── Resolver ──────────────────────────────────────────────────────


@dataclass
class UniverseResolver:
    """Resolves and caches the universe for any ticker."""

    cache_dir: Path | None = None
    ttl_days: int = DEFAULT_CACHE_TTL_DAYS
    top_n: int = DEFAULT_TOP_N
    min_weight_pct: float = DEFAULT_MIN_WEIGHT_PCT
    _memory: dict[str, Universe] = field(default_factory=dict, repr=False)

    # ── Cache ─────────────────────────────────────────────────

    def _cache_path(self, ticker: str) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"{ticker.upper()}.json"

    def _read_cache(self, ticker: str) -> tuple[Universe | None, bool]:
        """Return ``(universe, is_fresh)`` from disk."""
        path = self._cache_path(ticker)
        if path is None or not path.is_file():
            return None, False
        try:
            data = json.loads(path.read_text())
            universe = Universe.from_dict(data)
        except Exception as exc:
            logger.info("Ignoring unreadable universe cache %s: %s", path, exc)
            return None, False

        fresh = False
        if universe.resolved_at:
            try:
                age = datetime.now(UTC) - datetime.fromisoformat(universe.resolved_at)
                fresh = age < timedelta(days=self.ttl_days)
            except ValueError:
                fresh = False
        return universe, fresh

    def _write_cache(self, universe: Universe) -> None:
        path = self._cache_path(universe.ticker)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(universe.as_dict(), indent=2))
        except Exception as exc:
            logger.info("Could not write universe cache %s: %s", path, exc)

    # ── Resolution ────────────────────────────────────────────

    async def resolve(self, ticker: str, *, refresh: bool = False) -> Universe:
        """Resolve one ticker's universe, using cache unless *refresh*."""
        key = ticker.upper()

        if not refresh and key in self._memory:
            return self._memory[key]

        cached, fresh = self._read_cache(key)
        if cached is not None and fresh and not refresh:
            self._memory[key] = cached
            return cached

        resolved: Universe | None = None
        for provider in _PROVIDERS:
            resolved = await asyncio.to_thread(provider, key)
            if resolved is not None:
                break

        if resolved is None:
            # Resolution failed. A stale answer beats no answer.
            if cached is not None:
                logger.warning(
                    "Universe resolution failed for %s — using cached data from %s",
                    key,
                    cached.resolved_at or "unknown time",
                )
                stale = Universe.from_dict(cached.as_dict(), stale=True)
                self._memory[key] = stale
                return stale
            logger.warning(
                "Universe resolution failed for %s and no cache exists — "
                "treating it as a single instrument.",
                key,
            )
            resolved = _self_universe(key, source="fallback")
        else:
            self._write_cache(resolved)
            if resolved.is_fund:
                logger.info(
                    "Resolved %s: %d holdings via %s, covering %.1f%% of the fund",
                    key,
                    len(resolved.holdings),
                    resolved.source,
                    resolved.coverage * 100,
                )

        self._memory[key] = resolved
        return resolved

    async def resolve_many(
        self, tickers: list[str] | tuple[str, ...], *, refresh: bool = False
    ) -> dict[str, Universe]:
        """Resolve several tickers concurrently."""
        unique = sorted({t.upper() for t in tickers if t})
        results = await asyncio.gather(
            *(self.resolve(t, refresh=refresh) for t in unique),
            return_exceptions=True,
        )
        out: dict[str, Universe] = {}
        for ticker, result in zip(unique, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("Universe resolution raised for %s: %s", ticker, result)
                out[ticker] = _self_universe(ticker, source="error")
            else:
                out[ticker] = result
        return out

    def cached_symbols(self, tickers: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        """Like :meth:`symbols` but never resolves — in-memory cache only.

        For synchronous callers (the MCP response curators run inside a sync
        function). Returns ``()`` when nothing has been resolved yet, which
        callers must read as "don't filter" rather than "filter everything".
        """
        ordered: list[str] = []
        for ticker in sorted({t.upper() for t in tickers if t}):
            universe = self._memory.get(ticker)
            if universe is None:
                continue
            for symbol in universe.select(self.top_n, self.min_weight_pct):
                if symbol not in ordered:
                    ordered.append(symbol)
        return tuple(ordered)

    async def symbols(
        self,
        tickers: list[str] | tuple[str, ...],
        *,
        refresh: bool = False,
    ) -> tuple[str, ...]:
        """The union of every ticker's selected universe, deterministically ordered.

        This is what callers filtering an earnings calendar or a news feed want:
        "the symbols that can move what we trade".
        """
        universes = await self.resolve_many(tickers, refresh=refresh)
        ordered: list[str] = []
        for ticker in sorted(universes):
            for symbol in universes[ticker].select(self.top_n, self.min_weight_pct):
                if symbol not in ordered:
                    ordered.append(symbol)
        return tuple(ordered)


# ── Module-level access for tools ─────────────────────────────────

_resolver: UniverseResolver | None = None


def bind_resolver(resolver: UniverseResolver | None) -> None:
    """Install the process-wide resolver (called during startup)."""
    global _resolver
    _resolver = resolver


def get_resolver() -> UniverseResolver | None:
    return _resolver


def trading_tickers(config: Any) -> tuple[str, ...]:
    """Every ticker the system may trade, from configuration.

    Derived from ``asset.primary_ticker`` plus the constitution's
    ``allowed_tickers`` — both already exist, and the latter is already a list,
    so multi-ticker operation needs no new configuration.
    """
    tickers: list[str] = []

    primary = getattr(getattr(config, "settings", None), "asset", None)
    primary_ticker = getattr(primary, "primary_ticker", None)
    if primary_ticker:
        tickers.append(str(primary_ticker).upper())

    rules = getattr(getattr(config, "constitution", None), "trading_rules", None)
    for ticker in getattr(rules, "allowed_tickers", None) or []:
        symbol = str(ticker).upper()
        if symbol not in tickers:
            tickers.append(symbol)

    return tuple(tickers)


async def relevant_symbols(config: Any = None) -> tuple[str, ...]:
    """Symbols that can move what we trade, or an empty tuple if unresolvable.

    An empty result means "we don't know". What that should mean is caller
    specific and genuinely differs:

    * Filtering a **news feed**: treat it as "filter nothing". Carrying extra
      articles costs tokens; dropping relevant ones costs signal.
    * Filtering an **earnings calendar for event timing**: fall back to the
      traded ticker alone. Filtering nothing there would let an unrelated
      small-cap's report set ``hours_to_event`` and corrupt the event-window
      strategy, which is worse than detecting no event.
    """
    resolver = _resolver
    if resolver is None:
        return ()
    tickers = trading_tickers(config) if config is not None else ()
    if not tickers:
        return ()
    try:
        return await resolver.symbols(tickers)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not determine relevant symbols: %s", exc)
        return ()


def cached_relevant_symbols(config: Any = None) -> tuple[str, ...]:
    """Synchronous counterpart to :func:`relevant_symbols`, cache-only."""
    resolver = _resolver
    if resolver is None:
        return ()
    tickers = trading_tickers(config) if config is not None else ()
    if not tickers:
        return ()
    try:
        return resolver.cached_symbols(tickers)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not read cached relevant symbols: %s", exc)
        return ()
