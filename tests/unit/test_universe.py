"""Tests for trading-universe resolution.

The contract: nothing here may assume a particular ticker, index or asset class.
Switching ``asset.primary_ticker``, or trading several instruments, must work
with no code change — that is what these tests pin down.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from evotrader.tools.universe import (
    Holding,
    Universe,
    UniverseResolver,
    bind_resolver,
    cached_relevant_symbols,
    trading_tickers,
)


def _fund(ticker: str, weights: dict[str, float]) -> Universe:
    return Universe(
        ticker=ticker,
        is_fund=True,
        holdings=tuple(
            Holding(symbol=s, weight=w) for s, w in sorted(weights.items(), key=lambda kv: -kv[1])
        ),
        coverage=sum(weights.values()),
        source="test",
        resolved_at=datetime.now(UTC).isoformat(),
    )


# ── Selection ─────────────────────────────────────────────────────


def test_traded_ticker_always_comes_first():
    """The instrument is what we trade — it can never be filtered out."""
    universe = _fund("QQQ", {"NVDA": 0.085, "AAPL": 0.074})
    assert universe.select(15, 1.0)[0] == "QQQ"


def test_top_n_and_min_weight_are_a_union():
    weights = {f"S{i}": 0.001 for i in range(30)}
    weights["BIG"] = 0.20  # above the floor, and ranked first
    universe = _fund("FUND", weights)

    selected = universe.select(top_n=3, min_weight_pct=1.0)

    assert selected[0] == "FUND"
    assert "BIG" in selected
    # top_n=3 takes three holdings; the rest are all below the 1% floor.
    assert len(selected) == 1 + 3


def test_min_weight_admits_holdings_beyond_top_n():
    weights = {f"S{i}": 0.05 for i in range(10)}  # ten holdings, all at 5%
    universe = _fund("FUND", weights)

    selected = universe.select(top_n=2, min_weight_pct=1.0)

    assert len(selected) == 1 + 10, "every 5% holding clears a 1% floor"


def test_selection_is_deterministic():
    """The old code sliced a set, so which constituents it picked varied by run."""
    weights = {"A": 0.1, "B": 0.1, "C": 0.1, "D": 0.05}
    universe = _fund("F", weights)

    runs = {universe.select(3, 50.0) for _ in range(20)}
    assert len(runs) == 1


def test_selection_orders_by_weight_then_symbol():
    universe = _fund("F", {"ZZZ": 0.2, "AAA": 0.2, "MID": 0.1})
    assert universe.select(10, 100.0) == ("F", "AAA", "ZZZ", "MID")


def test_single_stock_is_its_own_universe():
    universe = Universe(ticker="AAPL", is_fund=False, coverage=1.0, source="test")
    assert universe.select(15, 1.0) == ("AAPL",)


# ── Resolution, caching and fallbacks ─────────────────────────────


@pytest.mark.asyncio
async def test_resolution_result_is_cached_to_disk(tmp_path, monkeypatch):
    import evotrader.tools.universe as mod

    calls = []

    def provider(ticker: str) -> Universe:
        calls.append(ticker)
        return _fund(ticker, {"NVDA": 0.09})

    monkeypatch.setattr(mod, "_PROVIDERS", (provider,))

    first = UniverseResolver(cache_dir=tmp_path)
    await first.resolve("QQQ")
    assert (tmp_path / "QQQ.json").is_file()

    # A fresh resolver must read the cache rather than call the provider again.
    second = UniverseResolver(cache_dir=tmp_path)
    universe = await second.resolve("QQQ")
    assert calls == ["QQQ"]
    assert universe.holdings[0].symbol == "NVDA"


@pytest.mark.asyncio
async def test_expired_cache_triggers_re_resolution(tmp_path, monkeypatch):
    import evotrader.tools.universe as mod

    stale = _fund("QQQ", {"OLD": 0.5})
    old_time = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    data = stale.as_dict() | {"resolved_at": old_time}
    (tmp_path / "QQQ.json").write_text(json.dumps(data))

    monkeypatch.setattr(mod, "_PROVIDERS", (lambda t: _fund(t, {"NEW": 0.4}),))
    resolver = UniverseResolver(cache_dir=tmp_path, ttl_days=7)

    universe = await resolver.resolve("QQQ")
    assert universe.holdings[0].symbol == "NEW"


@pytest.mark.asyncio
async def test_stale_cache_is_preferred_over_nothing(tmp_path, monkeypatch):
    """Degrade to slightly-old holdings rather than to no holdings at all."""
    import evotrader.tools.universe as mod

    old_time = (datetime.now(UTC) - timedelta(days=99)).isoformat()
    data = _fund("QQQ", {"NVDA": 0.09}).as_dict() | {"resolved_at": old_time}
    (tmp_path / "QQQ.json").write_text(json.dumps(data))

    monkeypatch.setattr(mod, "_PROVIDERS", (lambda t: None,))
    resolver = UniverseResolver(cache_dir=tmp_path, ttl_days=1)

    universe = await resolver.resolve("QQQ")
    assert universe.stale is True
    assert universe.holdings[0].symbol == "NVDA"


@pytest.mark.asyncio
async def test_total_failure_degrades_to_the_ticker_itself(tmp_path, monkeypatch):
    import evotrader.tools.universe as mod

    monkeypatch.setattr(mod, "_PROVIDERS", (lambda t: None,))
    resolver = UniverseResolver(cache_dir=tmp_path)

    universe = await resolver.resolve("WHATEVER")
    assert universe.is_fund is False
    assert universe.select(15, 1.0) == ("WHATEVER",)


@pytest.mark.asyncio
async def test_a_raising_provider_does_not_propagate(tmp_path, monkeypatch):
    import evotrader.tools.universe as mod

    def boom(_ticker: str):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(mod, "_PROVIDERS", (boom,))
    resolver = UniverseResolver(cache_dir=tmp_path)

    universe = await resolver.resolve_many(["QQQ"])
    assert universe["QQQ"].select(15, 1.0) == ("QQQ",)


@pytest.mark.asyncio
async def test_unreadable_cache_is_ignored(tmp_path, monkeypatch):
    import evotrader.tools.universe as mod

    (tmp_path / "QQQ.json").write_text("{ not json")
    monkeypatch.setattr(mod, "_PROVIDERS", (lambda t: _fund(t, {"NVDA": 0.09}),))

    universe = await UniverseResolver(cache_dir=tmp_path).resolve("QQQ")
    assert universe.holdings[0].symbol == "NVDA"


# ── Multi-ticker ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_tickers_are_unioned(tmp_path, monkeypatch):
    import evotrader.tools.universe as mod

    holdings = {
        "QQQ": {"NVDA": 0.09, "AAPL": 0.07},
        "IWM": {"SMCI": 0.02, "NVDA": 0.01},
    }
    monkeypatch.setattr(mod, "_PROVIDERS", (lambda t: _fund(t, holdings.get(t, {})),))
    resolver = UniverseResolver(cache_dir=tmp_path, top_n=15, min_weight_pct=1.0)

    symbols = await resolver.symbols(["QQQ", "IWM"])

    assert set(symbols) == {"QQQ", "IWM", "NVDA", "AAPL", "SMCI"}
    assert len(symbols) == len(set(symbols)), "no duplicates across tickers"


@pytest.mark.asyncio
async def test_symbols_is_deterministic_across_tickers(tmp_path, monkeypatch):
    import evotrader.tools.universe as mod

    monkeypatch.setattr(mod, "_PROVIDERS", (lambda t: _fund(t, {"A": 0.1, "B": 0.1}),))

    runs = set()
    for _ in range(5):
        r = UniverseResolver(cache_dir=tmp_path)
        runs.add(await r.symbols(["QQQ", "SPY"]))
    assert len(runs) == 1


@pytest.mark.asyncio
async def test_a_ticker_change_changes_the_universe(tmp_path, monkeypatch):
    """The whole point: no code change needed to switch instrument."""
    import evotrader.tools.universe as mod

    holdings = {"QQQ": {"NVDA": 0.09}, "XLE": {"XOM": 0.22, "CVX": 0.18}}
    monkeypatch.setattr(mod, "_PROVIDERS", (lambda t: _fund(t, holdings.get(t, {})),))
    resolver = UniverseResolver(cache_dir=tmp_path)

    assert set(await resolver.symbols(["QQQ"])) == {"QQQ", "NVDA"}
    assert set(await resolver.symbols(["XLE"])) == {"XLE", "XOM", "CVX"}


# ── Config integration ────────────────────────────────────────────


def test_trading_tickers_combines_primary_and_allowed():
    from evotrader.config import AppConfig

    config = AppConfig()
    config.settings.asset.primary_ticker = "QQQ"
    config.constitution.trading_rules.allowed_tickers = ["QQQ", "SPY"]

    assert trading_tickers(config) == ("QQQ", "SPY")


def test_trading_tickers_handles_a_lone_primary():
    from evotrader.config import AppConfig

    config = AppConfig()
    config.settings.asset.primary_ticker = "IWM"
    config.constitution.trading_rules.allowed_tickers = []

    assert trading_tickers(config) == ("IWM",)


def test_cached_symbols_returns_empty_before_resolution(tmp_path):
    from evotrader.config import AppConfig

    bind_resolver(UniverseResolver(cache_dir=tmp_path))
    try:
        assert cached_relevant_symbols(AppConfig()) == ()
    finally:
        bind_resolver(None)


def test_cached_symbols_empty_without_a_bound_resolver():
    from evotrader.config import AppConfig

    bind_resolver(None)
    assert cached_relevant_symbols(AppConfig()) == ()


@pytest.mark.asyncio
async def test_cached_symbols_reads_the_warm_resolver(tmp_path, monkeypatch):
    """The synchronous curator path relies on this after startup warms it."""
    import evotrader.tools.universe as mod
    from evotrader.config import AppConfig

    monkeypatch.setattr(mod, "_PROVIDERS", (lambda t: _fund(t, {"NVDA": 0.09}),))
    resolver = UniverseResolver(cache_dir=tmp_path)
    bind_resolver(resolver)
    try:
        config = AppConfig()
        config.settings.asset.primary_ticker = "QQQ"
        config.constitution.trading_rules.allowed_tickers = ["QQQ"]

        await resolver.symbols(trading_tickers(config))
        assert set(cached_relevant_symbols(config)) == {"QQQ", "NVDA"}
    finally:
        bind_resolver(None)
