"""Custom tools for EvoTrader agents.

Each function here becomes a tool that ADK agents can call. Tools are grouped
by the agent that primarily uses them, but can be shared across agents.

ADK tools are plain Python functions with descriptive docstrings — the LLM
uses the docstring to understand when and how to call each tool.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from evotrader.callbacks.exit_policy import is_exit_action
from evotrader.models.mcp import McpEquityQuote
from evotrader.tools.asset_context import primary_ticker
from evotrader.tools.universe import get_resolver, relevant_symbols
from evotrader.utils import safe_parse_json, select_agentic_account

logger = logging.getLogger(__name__)

# Module-level references set during system initialisation.
# These are injected by the agent factory before agents start.
_db = None
_journal = None
_metrics = None
_algo_registry = None
_config = None
_memory = None
_mcp_toolset = None
_sim_proxy = None
_strategy_loader = None
_current_session_id: str | None = None
OPTION_ID_TO_TICKER: dict[str, str] = {}

# Short-lived cache for get_open_positions — avoids redundant DB queries
# when multiple agents call it within the same trading cycle.
_open_positions_cache: dict | None = None
_open_positions_cache_ts: float = 0.0
_OPEN_POSITIONS_TTL: float = 30.0  # seconds

# Top QQQ constituents whose earnings events trigger the event window.
# These are the largest by weight; a single one reporting can move QQQ 1-3%.
# How many of a fund's holdings to pull earnings history for. Each one is a
# separate MCP round-trip, so this caps latency rather than relevance — the
# selection itself comes from `UniverseResolver`, weight-ordered.
_EARNINGS_HISTORY_MAX_CONSTITUENTS = 8

# Cross-cycle state for event context enrichment.  Persists within the
# running process (resets on restart, which is acceptable — the agent
# re-snapshots IV on the next cycle inside the blackout window).
_event_state: dict[str, Any] = {}


def set_current_session_id(session_id: str | None) -> None:
    """Set the active cycle session ID for trade linkage.

    Called at the start of each trading cycle to ensure all trades
    recorded during the cycle are linked to the correct session.
    Reset to None at the end of the cycle.
    """
    global _current_session_id
    _current_session_id = session_id


def bind_dependencies(
    db: Any,
    journal: Any,
    metrics: Any,
    algo_registry: Any,
    config: Any,
    memory: Any = None,
    mcp_toolset: Any = None,
    sim_proxy: Any = None,
    strategy_loader: Any = None,
) -> None:
    """Bind runtime dependencies so tools can access them.

    Called once during system startup by the agent factory.
    """
    global \
        _db, \
        _journal, \
        _metrics, \
        _algo_registry, \
        _config, \
        _memory, \
        _mcp_toolset, \
        _sim_proxy, \
        _strategy_loader
    _db = db
    _journal = journal
    _metrics = metrics
    _algo_registry = algo_registry
    _config = config
    _memory = memory
    _mcp_toolset = mcp_toolset
    _sim_proxy = sim_proxy
    _strategy_loader = strategy_loader
    logger.info("Tools: dependencies bound successfully")


def _get_strategy_loader() -> Any:
    global _strategy_loader
    if _strategy_loader is None:
        from evotrader import paths
        from evotrader.algorithms.loader import StrategyLoader

        manifest_path = paths.data_dir() / "algorithms" / "strategy_manifest.yaml"
        _strategy_loader = StrategyLoader(manifest_path)
    return _strategy_loader


# ═══════════════════════════════════════════════════════════════════════
# Market Intelligence Tools
# ═══════════════════════════════════════════════════════════════════════


def compute_indicators(ohlcv_json: str) -> dict:
    """Compute all technical indicators from OHLCV price data.

    Takes raw OHLCV data (JSON array of {open, high, low, close, volume}
    objects) and returns a complete set of technical indicators including
    RSI, MACD, Bollinger Bands, VWAP, IBS, moving averages, ATR, and
    regime classification.

    Args:
        ohlcv_json: JSON string containing an array of OHLCV candles.
            Each candle should have: open, high, low, close, volume.
            At least 60 candles are recommended for accurate regime detection.
    """
    import pandas as pd

    from evotrader.indicators.atr import compute_atr
    from evotrader.indicators.bollinger import compute_bollinger_bands
    from evotrader.indicators.ibs import compute_ibs
    from evotrader.indicators.macd import compute_macd
    from evotrader.indicators.moving_averages import compute_moving_averages
    from evotrader.indicators.regime import detect_regime_rule_based
    from evotrader.indicators.rsi import compute_rsi
    from evotrader.indicators.volume import compute_relative_volume
    from evotrader.indicators.vwap import compute_vwap

    candles = safe_parse_json(ohlcv_json)
    if not candles:
        return {"error": "No candle data provided"}

    df = pd.DataFrame(candles)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)

    # Compute all indicators
    rsi = compute_rsi(close)
    macd_result = compute_macd(close)
    bb = compute_bollinger_bands(close)
    vwap = compute_vwap(high, low, close, volume)
    ibs = compute_ibs(high, low, close)
    mas = compute_moving_averages(close)
    atr = compute_atr(high, low, close)
    rvol = compute_relative_volume(volume)

    # Regime detection
    regime = detect_regime_rule_based(close, high, low, volume)

    # Extract latest values
    i = len(close) - 1
    return {
        "rsi_14": _safe_float(rsi.iloc[i]),
        "macd_line": _safe_float(macd_result.macd_line.iloc[i]),
        "macd_signal": _safe_float(macd_result.signal_line.iloc[i]),
        "macd_histogram": _safe_float(macd_result.histogram.iloc[i]),
        "bollinger_upper": _safe_float(bb.upper.iloc[i]),
        "bollinger_middle": _safe_float(bb.middle.iloc[i]),
        "bollinger_lower": _safe_float(bb.lower.iloc[i]),
        "bollinger_width": _safe_float(bb.width.iloc[i]),
        "ema_9": _safe_float(mas.ema_9.iloc[i]),
        "ema_21": _safe_float(mas.ema_21.iloc[i]),
        "sma_20": _safe_float(mas.sma_20.iloc[i]),
        "sma_50": _safe_float(mas.sma_50.iloc[i]),
        "vwap": _safe_float(vwap.iloc[i]),
        "vwap_anchor": "prior_session" if _safe_float(vwap.iloc[i]) is not None else None,
        "ibs": _safe_float(ibs.iloc[i]),
        "atr_14": _safe_float(atr.iloc[i]),
        "relative_volume": _safe_float(rvol.iloc[i]),
        # The last daily bar's volume does not update intraday, so this is the
        # prior session's ratio all day long. Say so, as vwap_anchor does.
        "relative_volume_source": "prior_session_daily",
        "regime": {
            "type": regime.regime.value,
            "confidence": regime.confidence,
            "reasoning": regime.reasoning,
            "adx": regime.adx,
            "trend_direction": regime.trend_direction,
            "volatility_percentile": regime.volatility_percentile,
        },
    }


async def _call_mcp_tool(tool_name: str, arguments: dict) -> dict | None:
    """Call an MCP tool directly through the bound toolset session.

    Uses the same session pattern as ReconciliationService — creates
    a session from the MCP session manager and invokes the tool.

    Returns:
        Parsed JSON response dict, or None on failure.
    """
    if _sim_proxy is not None and tool_name in _sim_proxy.INTERCEPTED_TOOLS:
        try:
            return await _sim_proxy._handle_sim(tool_name, arguments)
        except Exception as e:
            logger.error("Sim proxy intercept failed for %s: %s", tool_name, e)
            return None

    if not _mcp_toolset:
        logger.error("MCP toolset not bound — cannot call %s", tool_name)
        return None

    try:
        session = await _mcp_toolset._mcp_session_manager.create_session()
        result = await session.call_tool(tool_name, arguments=arguments)

        if getattr(result, "isError", False) or not result.content:
            logger.error("MCP tool '%s' returned error or empty content", tool_name)
            return None

        return safe_parse_json(result.content[0].text)
    except Exception as e:
        logger.error("MCP tool '%s' failed: %s", tool_name, e, exc_info=True)
        return None


def _parse_hist_candles(
    raw_hist: dict | list | None, now: datetime, *, require_timestamp: bool = False
) -> list[dict]:
    """Parse MCP historicals response into a normalised list of OHLCV dicts.

    A bar without a timestamp is given one spaced a DAY apart from its
    neighbours — tolerable for daily bars. For intraday bars that invents a
    separate "session" per bar, so callers that group bars by session pass
    ``require_timestamp=True`` and such bars are dropped instead.
    """
    if not raw_hist:
        return []

    results = raw_hist.get("data", {}).get("results", []) if isinstance(raw_hist, dict) else []
    hist_list: list = []
    if results:
        hist_list = results[0].get("bars", [])
    if not hist_list and isinstance(raw_hist, dict):
        hist_list = raw_hist.get("data", {}).get("historicals", [])
    if not hist_list:
        hist_list = raw_hist if isinstance(raw_hist, list) else []

    candles: list[dict] = []
    for i, h in enumerate(hist_list):
        c_time = h.get("begins_at") or h.get("timestamp")
        if not c_time:
            if require_timestamp:
                continue
            c_time = (now - timedelta(days=len(hist_list) - i)).isoformat()
        candles.append(
            {
                "timestamp": c_time,
                "open": float(h.get("open_price", 0) or h.get("open", 0)),
                "high": float(h.get("high_price", 0) or h.get("high", 0)),
                "low": float(h.get("low_price", 0) or h.get("low", 0)),
                "close": float(h.get("close_price", 0) or h.get("close", 0)),
                "volume": float(h.get("volume", 0)),
            }
        )
    return candles


def _patch_last_bar(candles: list[dict], live_price: float) -> None:
    """Update the last daily candle's H/L/C with the live quote.

    This makes indicators computed from daily candles reflect the *developing*
    session instead of freezing at the prior close for the entire trading day.
    Mutates ``candles[-1]`` in place.
    """
    if not candles or live_price <= 0:
        return
    last = candles[-1]
    last["close"] = live_price
    if live_price > last["high"]:
        last["high"] = live_price
    if live_price < last["low"]:
        last["low"] = live_price


async def _enrich_event_context(
    indicators: dict,
    ticker: str,
    now: datetime,
) -> None:
    """Populate event context fields on the indicators dict.

    Fetches earnings calendar data via MCP and computes:
    - ``hours_to_event``: hours until the nearest upcoming top-constituent
      earnings report (or None if no relevant event is near).
    - ``hours_since_event``: hours since the most recent top-constituent
      earnings report cleared.
    - ``atm_iv_30dte``: ATM implied volatility from the nearest ~30 DTE
      option contract.
    - ``atm_iv_pre_event``: IV snapshot persisted when first entering the
      blackout window (hours_to_event <= 30h).
    - ``event_type``: currently always ``"earnings"`` (FOMC/CPI/PPI will be
      added when a macro calendar data source is available).

    Mutates ``indicators`` in place and uses the module-level
    ``_event_state`` dict for cross-cycle persistence.
    """
    global _event_state

    # ── 1. Fetch earnings calendar ─────────────────────────────
    try:
        raw_earnings = await _call_mcp_tool("get_earnings_calendar", {})
    except Exception as e:
        logger.info("Event context: earnings calendar fetch failed: %s", e)
        return

    if not raw_earnings:
        logger.info("Event context: get_earnings_calendar returned empty/null")
        return

    # Detect Alpha Vantage rate-limit payloads that masquerade as valid
    # 200 OK responses. These contain an "Information" or "Note" key
    # instead of actual data, or return corrupted CSV-like strings.
    if isinstance(raw_earnings, dict) and ("Information" in raw_earnings or "Note" in raw_earnings):
        logger.info(
            "Event context: earnings calendar rate-limited (payload keys: %s)",
            list(raw_earnings.keys()),
        )
        return

    # Parse earnings rows from the MCP response.
    # Robinhood returns { data: { results: [ { symbol, report, ... }, ... ] } }
    earnings_list = (
        raw_earnings.get("data", {}).get("results", []) if isinstance(raw_earnings, dict) else []
    )
    if not earnings_list and isinstance(raw_earnings, dict):
        # Alternate structure: flat list under "data"
        data = raw_earnings.get("data")
        if isinstance(data, list):
            earnings_list = data

    if not earnings_list:
        logger.info(
            "Event context: no earnings rows after parsing (raw keys: %s)",
            list(raw_earnings.keys())
            if isinstance(raw_earnings, dict)
            else type(raw_earnings).__name__,
        )
        return

    # ── 2. Filter to symbols that can actually move what we trade ──
    # Resolved from the traded ticker's live holdings rather than a hardcoded
    # list, so this follows `asset.primary_ticker` wherever it points.
    relevant = await relevant_symbols(_config)
    if not relevant:
        # Resolution failed with no cache to fall back on. Narrow to the traded
        # ticker rather than accepting every symbol: an unrelated small-cap's
        # report setting `hours_to_event` would corrupt the event-window
        # strategy, which is worse than reporting no event at all.
        relevant = (ticker.upper(),)
        logger.warning(
            "Event context: could not resolve %s's constituents; considering "
            "only %s for earnings events this cycle.",
            ticker,
            ticker,
        )
    relevant_set = set(relevant)

    upcoming: list[dict] = []
    past: list[dict] = []

    for row in earnings_list:
        symbol = (row.get("symbol") or "").upper()
        if symbol not in relevant_set:
            continue

        # Parse the report datetime.  Robinhood formats vary:
        # "report_date" (date string) or "report" with nested fields.
        report_dt = None
        for dt_key in ("report_date", "expected_report_date", "date"):
            raw_dt = row.get(dt_key)
            if raw_dt:
                try:
                    # Try ISO datetime first, then date-only
                    if "T" in str(raw_dt):
                        report_dt = datetime.fromisoformat(str(raw_dt).replace("Z", "+00:00"))
                    else:
                        # Date-only: assume 16:00 ET (after-market close)
                        from evotrader.tools.market_hours import ET

                        report_dt = datetime.strptime(str(raw_dt), "%Y-%m-%d").replace(
                            hour=16, tzinfo=ET
                        )
                        report_dt = report_dt.astimezone(UTC)
                except Exception:
                    continue
                break

        if report_dt is None:
            continue

        # Determine if this is upcoming or past based on actual EPS
        eps_actual = (
            row.get("eps", {}).get("actual")
            if isinstance(row.get("eps"), dict)
            else row.get("eps_actual")
        )

        if eps_actual is None and report_dt > now:
            upcoming.append({"symbol": symbol, "report_dt": report_dt})
        elif report_dt <= now:
            past.append({"symbol": symbol, "report_dt": report_dt})

    # ── 3. Compute hours_to_event / hours_since_event ──────────
    if upcoming:
        nearest = min(upcoming, key=lambda r: r["report_dt"])
        hours_to = (nearest["report_dt"] - now).total_seconds() / 3600
        indicators["hours_to_event"] = round(hours_to, 2)
        indicators["event_type"] = "earnings"
        logger.info(
            "Event context: %s earnings in %.1fh",
            nearest["symbol"],
            hours_to,
        )
    else:
        logger.info(
            "Event context: no upcoming top-constituent earnings found "
            "(parsed %d total rows, %d matched constituents)",
            len(earnings_list),
            len(upcoming) + len(past),
        )

    if past:
        latest = max(past, key=lambda r: r["report_dt"])
        hours_since = (now - latest["report_dt"]).total_seconds() / 3600
        indicators["hours_since_event"] = round(hours_since, 2)
        if "event_type" not in indicators:
            indicators["event_type"] = "earnings"

    # ── 4. Fetch ATM IV from option chain ──────────────────────
    try:
        raw_chain = await _call_mcp_tool("get_option_chains", {"underlying_symbol": ticker})
        if raw_chain:
            chains = raw_chain.get("data", {}).get("chains", []) or []
            if chains:
                chain = chains[0]
                expiration_dates = chain.get("expiration_dates", []) or []

                # Find expiration closest to 30 DTE
                current_date = now.date()
                best_expiry = None
                min_diff = 999
                for d in expiration_dates:
                    try:
                        exp_date = datetime.strptime(d, "%Y-%m-%d").date()
                        diff = abs((exp_date - current_date).days - 30)
                        if diff < min_diff:
                            min_diff = diff
                            best_expiry = d
                    except Exception:
                        continue

                if best_expiry:
                    # Get underlying price for ATM strike selection
                    underlying_price = None
                    raw_quote = await _call_mcp_tool("get_equity_quotes", {"symbols": [ticker]})
                    if raw_quote:
                        results = raw_quote.get("data", {}).get("results", []) or []
                        if results:
                            q = results[0].get("quote") or {}
                            uq = McpEquityQuote(**q)
                            underlying_price = uq.resolve_live_price()

                    if underlying_price and underlying_price > 0:
                        # Fetch option instruments for the target expiration
                        raw_inst = await _call_mcp_tool(
                            "get_option_instruments",
                            {
                                "chain_symbol": ticker,
                                "expiration_dates": best_expiry,
                                "state": "active",
                                "type": "call",  # ATM calls for IV
                            },
                        )
                        if raw_inst:
                            instruments = raw_inst.get("data", {}).get("instruments", []) or []
                            # Find the strike closest to ATM
                            best_inst = None
                            best_strike_diff = float("inf")
                            for inst in instruments:
                                strike = float(inst.get("strike_price", 0) or 0)
                                if strike > 0:
                                    diff = abs(strike - underlying_price)
                                    if diff < best_strike_diff:
                                        best_strike_diff = diff
                                        best_inst = inst

                            if best_inst:
                                # Try to get IV from the instrument or
                                # fetch a quote for it
                                iv = best_inst.get("implied_volatility")
                                if iv is None:
                                    inst_url = best_inst.get("url", "")
                                    if inst_url:
                                        raw_oq = await _call_mcp_tool(
                                            "get_option_quotes",
                                            {
                                                "instruments": [inst_url],
                                            },
                                        )
                                        if raw_oq:
                                            oq_results = (
                                                raw_oq.get("data", {}).get("results", []) or []
                                            )
                                            if oq_results:
                                                iv = oq_results[0].get("implied_volatility")

                                if iv is not None:
                                    try:
                                        atm_iv = float(iv)
                                        indicators["atm_iv_30dte"] = round(atm_iv, 6)
                                        logger.info(
                                            "Event context: ATM IV 30DTE = %.4f",
                                            atm_iv,
                                        )
                                    except (ValueError, TypeError):
                                        pass

    except Exception as e:
        logger.info("Event context: IV fetch failed: %s", e)

    # ── 5. Persist / recall atm_iv_pre_event ───────────────────
    atm_iv = indicators.get("atm_iv_30dte")
    hours_to = indicators.get("hours_to_event")
    hours_since = indicators.get("hours_since_event")

    if atm_iv is not None and hours_to is not None and hours_to <= 30:
        # Entering blackout window — snapshot IV if not already captured
        _event_state.setdefault("atm_iv_pre_event", atm_iv)
        logger.info(
            "Event context: pre-event IV snapshot = %.4f",
            _event_state["atm_iv_pre_event"],
        )

    if "atm_iv_pre_event" in _event_state:
        indicators["atm_iv_pre_event"] = _event_state["atm_iv_pre_event"]

    # Clear pre-event IV when the post-event window has fully elapsed
    if hours_since is not None and hours_since > 48:
        _event_state.pop("atm_iv_pre_event", None)

    # ── Summary: log which event context fields were populated ─
    populated = [
        k
        for k in (
            "hours_to_event",
            "hours_since_event",
            "atm_iv_30dte",
            "atm_iv_pre_event",
            "event_type",
        )
        if indicators.get(k) is not None
    ]
    if populated:
        logger.info(
            "Event context enriched: %s",
            ", ".join(f"{k}={indicators[k]}" for k in populated),
        )
    else:
        logger.info("Event context: no fields populated this cycle")


def _pct_display(value: float | None) -> str | None:
    """A percent-unit field as text a reader can quote without knowing its units.

    ``daily_change_pct`` is in percent (-0.0401 means -0.04%). On 2026-09-24
    13:30 ET the orchestrator's summary printed it as "-4.01%", reading it as a
    fraction. A model that copies a string cannot rescale it.
    """
    if value is None:
        return None
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return None


def _note_engine_change(algo_result: dict, now: datetime) -> bool:
    """Add a SYSTEM line to the trading handoff when the composite's engine or
    algorithm version differs from the previous cycle's.

    Composite values are only comparable within one engine and one version, and
    the strategy agent compares against its entry composite every cycle. This
    runs inside the market-data tool, so the line lands before the strategy
    stage reads the handoff — the first cycle affected is the one told. Returns
    whether a line was written; never raises.
    """
    if _config is None or not algo_result.get("composite_source_fingerprint"):
        return False
    try:
        from evotrader.tools.engine_provenance import note_engine
        from evotrader.tools.trading_handoff import append_system_line

        seen = note_engine(
            _config.data_dir,
            algo_version=algo_result.get("algo_version", ""),
            fingerprint=algo_result["composite_source_fingerprint"],
            now=now,
        )
        return bool(seen["changed"]) and append_system_line(_config.data_dir, seen["notice"])
    except Exception as e:  # never break the market-data tool over it
        logger.debug("engine provenance skipped: %s", e)
        return False


def _composite_provenance(detailed) -> dict:
    """Build provenance + the pools the ensemble arithmetic actually used.

    These live on ``compute_signal``'s metadata, but the cycle path calls
    ``compute_detailed_signal``, so none of it reached the log. Review
    20260911_194320 finding 6 caught that: the fingerprint was designated the
    cheap detector for a stale-build gap and it did not fire even though the
    build was current, because it was never emitted on this path at all.

    The pool counts are included because reconstructing a cycle's composite by
    hand is the documented highest-value diagnostic, and these are exactly the
    denominators that reconstruction needs.
    """
    from evotrader.algorithms.composite import COMPOSITE_SOURCE_FINGERPRINT

    _unattenuated = getattr(detailed, "composite_unattenuated", None)
    _scale = float(getattr(detailed, "participation_scale", 1.0) or 1.0)

    return {
        "composite_source_fingerprint": COMPOSITE_SOURCE_FINGERPRINT,
        "renormalization_pool": "voting" if detailed.n_voting else "applicable",
        "pool_sizes": {
            "additive": detailed.n_additive,
            "applicable": detailed.n_applicable,
            "in_scope": detailed.n_in_scope,
            "voting": detailed.n_voting,
        },
        "amplification_capped": list(detailed.amplification_capped),
        # The composite before the hour was applied, and the factor applied.
        # `composite_value` is scaled by how many channels could speak this
        # hour, so two cycles with identical votes read differently pre-market
        # and during regular hours. Any rule that compares this cycle's
        # composite against an earlier one must compare these instead — see
        # CompositeAlgoSignal.composite_unattenuated.
        # getattr, not attribute access: this helper is called with whatever the
        # cycle path produced, and a caller that predates these fields should
        # lose the two extra keys rather than the whole provenance block.
        "composite_unattenuated": (round(_unattenuated, 4) if _unattenuated is not None else None),
        "participation_scale": round(_scale, 4),
        "attenuation_applied": _scale < 1.0,
    }


async def _agentic_account_number() -> str | None:
    """The broker account the agents trade in (see ``select_agentic_account``).

    None when the broker returns nothing. Broker errors propagate, so each
    caller keeps its own way of reporting them.
    """
    raw = await _call_mcp_tool("get_accounts", {})
    if not raw:
        return None
    return select_agentic_account(raw.get("data", {}).get("accounts", []))


async def _agentic_portfolio() -> dict | None:
    """Cash, total value and buying power of the agentic account, as floats.

    One lookup that six tools used to repeat line for line. None when the
    broker returns nothing; broker errors propagate to the caller.
    """
    account = await _agentic_account_number()
    if not account:
        return None
    raw = await _call_mcp_tool("get_portfolio", {"account_number": account})
    if not raw:
        return None
    data = raw.get("data", {})
    bp_raw = data.get("buying_power", 0.0)
    return {
        "account_number": account,
        "cash_balance": float(data.get("cash", 0.0)),
        "total_value": float(data.get("total_value", 0.0)),
        "buying_power": float(
            bp_raw.get("buying_power", 0.0) if isinstance(bp_raw, dict) else bp_raw
        ),
    }


async def _fetch_quote(ticker: str, now: datetime) -> tuple[dict, list[str]]:
    """Current quote for *ticker*, plus any non-fatal problems fetching it.

    Extracted so every caller shapes a quote identically. Three call sites used
    to inline this with subtly different fallbacks, which is how a symbol could
    be priced one way for indicators and another way for sizing.

    Never raises and never returns None: a zeroed quote with an error string is
    more useful to an agent than an exception, because it can say "I have no
    price" rather than losing the whole cycle.
    """
    errors: list[str] = []
    raw_quote = await _call_mcp_tool("get_equity_quotes", {"symbols": [ticker]})
    if raw_quote:
        results = raw_quote.get("data", {}).get("results", [])
        if results:
            q = results[0].get("quote") or {}
            quote = McpEquityQuote(**q)
            return {
                "ticker": ticker,
                "bid": quote.bid or quote.reg or 0.0,
                "ask": quote.ask or quote.reg or 0.0,
                "last": quote.resolve_live_price(),
                "volume": float(q.get("volume") or 0),
                "timestamp": q.get("updated_at")
                or q.get("venue_last_trade_time")
                or now.isoformat(),
                "previous_close": float(
                    q.get("previous_close") or q.get("adjusted_previous_close") or 0
                ),
                "adjusted_previous_close": float(
                    q.get("adjusted_previous_close") or q.get("previous_close") or 0
                ),
            }, errors

    errors.append("Failed to fetch quote data from MCP")
    return {
        "ticker": ticker,
        "bid": 0.0,
        "ask": 0.0,
        "last": 0.0,
        "volume": 0.0,
        "timestamp": now.isoformat(),
    }, errors


async def _fetch_daily_candles(ticker: str, now: datetime, days: int = 90) -> list[dict]:
    """Daily OHLCV for *ticker* over the trailing *days*."""
    start_time = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw_hist = await _call_mcp_tool(
        "get_equity_historicals",
        {
            "symbols": [ticker],
            "interval": "day",
            "start_time": start_time,
        },
    )
    return _parse_hist_candles(raw_hist, now)


async def get_ticker_snapshot(ticker: str) -> dict:
    """Live quote and volatility for any ticker the constitution permits.

    Use this to price and size a position in a permitted symbol OTHER than the
    one this cycle's analysis ran on — a hedging vehicle, a paired leg, a second
    instrument. Returns the current bid/ask/last plus ATR and the other
    indicators derived from its own daily candles, so a stop can be placed on
    measured volatility instead of an assumption.

    Deliberately generic: it takes a symbol and checks it against the allowed
    list. There is no notion of "primary" or "inverse" here. Add a fourth ticker
    to the constitution and this serves it with no code change.

    It does NOT compute a market regime or run the composite algorithm. Those
    score an instrument against its own price history, and the strategy is
    written for the traded instrument — a signal generated on a hedging
    vehicle's own prices is not a signal, it is noise wearing a signal's name.
    Direction comes from the traded instrument; this supplies the arithmetic
    needed to express it.

    Args:
        ticker: Symbol to price. Must appear in the constitution's
            allowed_tickers, otherwise the call is refused and the permitted
            symbols are listed back.
    """
    from evotrader.tools.asset_context import allowed_tickers, is_allowed

    symbol = (ticker or "").strip().upper()
    if not is_allowed(symbol):
        permitted = ", ".join(allowed_tickers()) or "(none configured)"
        return {
            "error": (
                f"'{symbol or ticker}' is not a permitted ticker. The "
                f"constitution allows: {permitted}."
            ),
            "ticker": symbol or ticker,
            "permitted_tickers": list(allowed_tickers()),
        }

    from evotrader.tools.market_hours import _resolve_now

    now = _resolve_now().astimezone(UTC)
    errors: list[str] = []

    quote, quote_errors = await _fetch_quote(symbol, now)
    errors.extend(quote_errors)

    candles = await _fetch_daily_candles(symbol, now)
    if len(candles) < 10:
        errors.append(
            f"Insufficient OHLCV data: {len(candles)} daily candles "
            f"(indicators need ~60 to be reliable)"
        )

    # Patch the forming bar with the live price, exactly as the primary
    # instrument's path does — otherwise ATR is frozen at yesterday's close and
    # a stop sized from it is a day stale.
    if candles and quote.get("last"):
        _patch_last_bar(candles, quote["last"])

    indicators: dict = {}
    if candles:
        indicators = compute_indicators(json.dumps(candles))
        if "error" in indicators:
            errors.append(f"Indicator computation failed: {indicators['error']}")
            indicators = {}

    last = float(quote.get("last") or 0.0)
    atr = float(indicators.get("atr_14") or 0.0)
    prev_close = float(quote.get("previous_close") or 0.0)

    return {
        "ticker": symbol,
        "timestamp": now.isoformat(),
        "quote": quote,
        "indicators": indicators,
        # Pre-computed because every stop decision needs it and deriving it from
        # the two fields above is the step most likely to be skipped.
        "atr_14": atr or None,
        "atr_pct_of_price": round(atr / last, 5) if atr and last else None,
        "daily_change_pct": (
            round((last / prev_close - 1) * 100, 4) if last and prev_close else None
        ),
        "daily_change_display": _pct_display(
            (last / prev_close - 1) * 100 if last and prev_close else None
        ),
        "candles_used": len(candles),
        "errors": errors,
    }


def compute_gap_pct(
    intraday_candles: list[dict],
    daily_candles: list[dict],
    quote_data: dict | None,
    now: datetime,
) -> tuple[float | None, str | None]:
    """Session opening gap in percent, and where the number came from.

    Prefers the FIRST INTRADAY BAR, exactly as ``backtest/snapshot_builder``
    does. The daily-bar path needs a today-dated daily candle, which the
    provider never supplies intraday (the series ends at the prior session and
    the last bar is live-patched), so before this helper ``gap_pct`` was None on
    EVERY live cycle on record — 09-18 10:34 read ``gap_pct: null`` on a +3.4%
    open — and the gap channel had never once been applicable live, while every
    backtest ran an ensemble that included it. Backtest-vs-live comparisons
    were over different ensembles.

    Returns ``(gap_pct, source)`` with source in
    {"intraday_first_bar", "daily_bar", None}.
    """
    q = quote_data or {}
    prev_close = float(q.get("previous_close") or q.get("adjusted_previous_close") or 0.0)

    if intraday_candles and prev_close > 0:
        try:
            first_open = float(intraday_candles[0]["open"])
            if first_open > 0:
                return round(
                    ((first_open - prev_close) / prev_close) * 100, 4
                ), "intraday_first_bar"
        except (KeyError, TypeError, ValueError):
            pass

    # Fallback: today's DAILY candle, when one exists (post-close runs).
    if daily_candles:
        from evotrader.tools.market_hours import ET

        today_eastern = now.astimezone(ET).date()
        last_candle = daily_candles[-1]
        try:
            last_ts = datetime.fromisoformat(str(last_candle["timestamp"]).replace("Z", "+00:00"))
            last_date = last_ts.astimezone(ET).date()
        except Exception:
            last_date = None
        if last_date == today_eastern:
            curr_open = last_candle.get("open")
            pc = prev_close or (
                float(daily_candles[-2]["close"]) if len(daily_candles) >= 2 else 0.0
            )
            if curr_open and pc > 0:
                return round(((float(curr_open) - pc) / pc) * 100, 4), "daily_bar"
    return None, None


async def _fetch_session_bars(ticker: str, start: datetime, end: datetime) -> list[dict]:
    """Regular-session 5-minute bars for ``ticker`` between ``start`` and ``end``.

    Deliberately the SAME call gather_market_data makes for today's bars —
    same tool, interval and (default, regular) session bounds — so a volume
    profile filled from it counts volume exactly the way today's live curve does.
    """
    raw = await _call_mcp_tool(
        "get_equity_historicals",
        {
            "symbols": [ticker],
            "interval": "5minute",
            "start_time": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_time": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    # Bars are grouped into sessions by timestamp, so an invented one would
    # corrupt the profile rather than merely misdate a bar.
    return _parse_hist_candles(raw, end, require_timestamp=True)


async def gather_market_data(ticker: str) -> dict:
    """Fetch market data, compute indicators, and run the composite algo.

    This is a deterministic tool that replaces the Market Intelligence
    sub-agent. It calls MCP tools directly (no LLM involved) to:
    1. Fetch the current quote for the ticker.
    2. Fetch recent OHLCV historical candles (90 days of daily data).
    3. Compute all technical indicators (RSI, MACD, Bollinger, etc.).
    4. Detect the market regime (trending, range-bound, high-volatility).
    5. Run the composite trading algorithm to produce a signal.

    Returns a structured dict containing the full market snapshot,
    indicator values, regime classification, and algo signal breakdown.

    Args:
        ticker: Stock ticker symbol (e.g., "QQQ", "SPY").
    """
    from evotrader.models.market import MarketSnapshot

    errors: list[str] = []
    from evotrader.tools.market_hours import _resolve_now

    now = _resolve_now().astimezone(UTC)

    # ── 1. Fetch current quote via MCP ─────────────────────────
    quote_data, quote_errors = await _fetch_quote(ticker, now)
    errors.extend(quote_errors)

    # ── 2. Fetch OHLCV historicals via MCP ─────────────────────
    candles: list[dict] = await _fetch_daily_candles(ticker, now)

    if len(candles) < 10:
        errors.append(
            f"Insufficient OHLCV data: got {len(candles)} candles (need ≥60 for accurate indicators)"
        )

    # ── 2b. Patch last daily bar with live quote ───────────────
    # Without this patch, all indicators (RSI, MACD, Bollinger, etc.)
    # are frozen at the prior close for the entire trading day because
    # they're computed from completed daily candles. By updating the
    # last candle's H/L/C with the live quote, indicators reflect the
    # developing session on every cycle.
    if candles and quote_data and quote_data.get("last"):
        live_price = quote_data["last"]
        if live_price > 0:
            _patch_last_bar(candles, live_price)

    # ── 2c. Fetch intraday candles for session VWAP/IBS ────────
    from evotrader.tools.market_hours import ET, is_market_open

    market_open = is_market_open()
    intraday_candles: list[dict] = []
    if market_open:
        today_start = (
            now.astimezone(ET)
            .replace(
                hour=9,
                minute=30,
                second=0,
                microsecond=0,
            )
            .astimezone(UTC)
        )
        raw_intraday = await _call_mcp_tool(
            "get_equity_historicals",
            {
                "symbols": [ticker],
                "interval": "5minute",
                "start_time": today_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        intraday_candles = _parse_hist_candles(raw_intraday, now)

    # ── 3. Compute indicators ──────────────────────────────────
    indicators: dict = {}
    if candles:
        indicators = compute_indicators(json.dumps(candles))
        if "error" in indicators:
            errors.append(f"Indicator computation failed: {indicators['error']}")
            indicators = {}

    # ── 3a. Make sure the time-of-day volume profile can read ──
    # A cold profile (first run, or a change of instrument) is filled from the
    # broker once, so today's relative volume is available from this cycle on
    # rather than after two weeks of recording. No broker call once warm.
    if _config is not None:
        try:
            from evotrader.tools.session_volume import ensure_warm_profile

            await ensure_warm_profile(_config.data_dir, ticker, _fetch_session_bars, now=now)
        except Exception as e:  # never break the market-data tool over it
            logger.debug("volume profile backfill skipped: %s", e)

    # ── 3b. Override VWAP and IBS with live session values ─────
    if intraday_candles and indicators:
        import pandas as pd

        from evotrader.indicators.ibs import compute_live_ibs
        from evotrader.indicators.vwap import compute_session_vwap

        idf = pd.DataFrame(intraday_candles)
        idf_high = idf["high"].astype(float)
        idf_low = idf["low"].astype(float)
        idf_close = idf["close"].astype(float)
        idf_volume = idf["volume"].astype(float)

        # Parse timestamps for session filtering
        if "timestamp" in idf.columns:
            try:
                idf.index = pd.to_datetime(idf["timestamp"], utc=True)
                idf_high.index = idf.index
                idf_low.index = idf.index
                idf_close.index = idf.index
                idf_volume.index = idf.index
            except Exception:
                pass  # Fall back to no session filtering

        session_vwap, anchor = compute_session_vwap(
            idf_high,
            idf_low,
            idf_close,
            idf_volume,
        )
        if session_vwap is not None:
            indicators["vwap"] = session_vwap
            indicators["vwap_anchor"] = anchor

        # Live IBS from session running high/low
        session_high = float(idf_high.max())
        session_low = float(idf_low.min())
        live_price = quote_data.get("last", 0.0) if quote_data else 0.0
        if live_price > 0 and session_high > session_low:
            live_ibs = compute_live_ibs(session_high, session_low, live_price)
            if live_ibs is not None:
                indicators["ibs"] = live_ibs

        # Today's relative volume against the SAME minute of prior sessions.
        # Read from prior sessions first, then record today's (longer) curve,
        # so today is never its own baseline. The daily `relative_volume` is
        # left as it is; channels choose between them via `rvol_source`.
        if _config is not None:
            try:
                from evotrader.tools.session_volume import (
                    record_session_curve,
                    session_relative_volume,
                )

                srv = session_relative_volume(_config.data_dir, ticker, intraday_candles, now=now)
                indicators["session_relative_volume"] = srv["value"]
                indicators["session_relative_volume_sessions"] = srv["sessions"]
                record_session_curve(_config.data_dir, ticker, intraday_candles, now=now)
            except Exception as e:  # never break the market-data tool over it
                logger.debug("session relative volume skipped: %s", e)
    elif not intraday_candles and indicators:
        # No intraday data — tag VWAP as prior-session anchor
        indicators["vwap_anchor"] = "prior_session"

    # ── 3c. Enrich event context (earnings proximity + ATM IV) ─
    if indicators:
        try:
            await _enrich_event_context(indicators, ticker, now)
        except Exception as e:
            errors.append(f"Event context enrichment failed: {e}")

    # ── 4. Build MarketSnapshot ────────────────────────────────
    regime_data = indicators.pop("regime", None) or {
        "regime": "range_bound",
        "confidence": 0.0,
        "reasoning": "Defaulted — insufficient data for regime detection",
    }
    # Map regime type string to the enum value if needed
    regime_type = regime_data.get("type") or regime_data.get("regime", "range_bound")
    regime_dict = {
        "regime": regime_type,
        "confidence": float(regime_data.get("confidence", 0.0)),
        "reasoning": str(regime_data.get("reasoning", "")),
    }

    # Compute daily change percentage and gap percentage
    daily_change_pct = None
    gap_pct = None
    gap_source = None

    # ── Gap from the FIRST INTRADAY BAR, as the backtest does ────────
    # The daily-bar path below needs a today-dated daily candle, which the
    # provider never supplies intraday (the series ends at the prior session
    # and the last bar is live-patched). So gap_pct was None on EVERY live
    # cycle on record — 09-18 10:34 showed gap_pct: null on a +3.4% open —
    # and the gap channel has never once been applicable live, while
    # backtest/snapshot_builder.py computed it from the first 5-minute bar.
    # Every backtest therefore ran an ensemble live never had.
    gap_pct, gap_source = compute_gap_pct(intraday_candles, candles, quote_data, now)

    # Always compute daily_change_pct relative to the previous close from quote if available.
    if (
        quote_data
        and quote_data.get("last")
        and (quote_data.get("previous_close") or quote_data.get("adjusted_previous_close"))
    ):
        last_price = quote_data["last"]
        prev_close = quote_data.get("previous_close") or quote_data.get("adjusted_previous_close")
        if prev_close and prev_close > 0:
            daily_change_pct = round(((last_price - prev_close) / prev_close) * 100, 4)

    # Fall back to candles if quote data is missing or doesn't have prices
    if daily_change_pct is None and len(candles) >= 2:
        prev_close = candles[-2]["close"]
        curr_close = candles[-1]["close"]
        if prev_close > 0:
            daily_change_pct = round(((curr_close - prev_close) / prev_close) * 100, 4)

    # ── 4b. Re-detect regime with intraday trend override ──────
    # The initial regime was computed from daily OHLCV bars only (inside
    # compute_indicators). When daily ADX lags a strong single-session
    # trend, the detector classified range_bound. Now that we have
    # daily_change_pct + live session VWAP + MACD histogram, re-run
    # detection so the intraday override can fire.
    if (
        regime_type == "range_bound"
        and daily_change_pct is not None
        and indicators.get("vwap") is not None
        and indicators.get("macd_histogram") is not None
        and candles
    ):
        import pandas as pd

        from evotrader.indicators.regime import detect_regime_rule_based

        df = pd.DataFrame(candles)
        re_regime = detect_regime_rule_based(
            close=df["close"].astype(float),
            high=df["high"].astype(float),
            low=df["low"].astype(float),
            volume=df["volume"].astype(float),
            daily_change_pct=daily_change_pct,
            vwap=float(indicators["vwap"]),
            macd_histogram=float(indicators["macd_histogram"]),
        )
        if re_regime.regime.value != "range_bound":
            regime_type = re_regime.regime.value
            regime_dict = {
                "regime": regime_type,
                "confidence": re_regime.confidence,
                "reasoning": re_regime.reasoning,
            }

    snapshot_dict = {
        "ticker": ticker,
        "timestamp": now.isoformat(),
        "quote": quote_data,
        "indicators": indicators,
        "regime": regime_dict,
        "recent_candles": intraday_candles if intraday_candles else [],
        "daily_candles": candles if candles else [],
        "daily_change_pct": daily_change_pct,
        "gap_pct": gap_pct,
    }

    # Validate through Pydantic — use normalize_market_snapshot as safety net
    try:
        normalized = normalize_market_snapshot(snapshot_dict)
        snapshot = MarketSnapshot.model_validate(normalized)
    except Exception as e:
        errors.append(f"MarketSnapshot validation failed: {e}")
        # Return what we have even if validation fails
        return {
            "snapshot": snapshot_dict,
            "algo_signal": None,
            "errors": errors,
        }

    # ── 5. Run composite algorithm ─────────────────────────────
    algo_result: dict = {}
    try:
        params = {}
        if _algo_registry:
            params = _algo_registry.load_parameters()

        strategy = _get_strategy_loader().build_composite(
            params,
            active_version=_algo_registry.get_active_version() if _algo_registry else None,
        )

        detailed = strategy.compute_detailed_signal(snapshot)
        algo_result = {
            "composite_signal": detailed.composite_value,
            "algo_version": detailed.algo_version,
            "sub_signals": [
                {
                    "name": s.name,
                    "value": s.value,
                    "weight": s.weight,
                    "metadata": s.metadata,
                }
                for s in detailed.signals
            ],
            **_composite_provenance(detailed),
        }
    except Exception as e:
        errors.append(f"Composite algorithm failed: {e}")
        algo_result = {
            "composite_signal": 0.0,
            "algo_version": "error",
            "sub_signals": [],
            "error": str(e),
        }

    # ── 5b. Tell this cycle if the engine or version changed ───
    _note_engine_change(algo_result, now)

    # ── 6. Fetch agentic account portfolio state ────────────────
    portfolio_state: dict | None = None
    try:
        portfolio_state = await _agentic_portfolio()
    except Exception as e:
        errors.append(f"Failed to fetch portfolio state: {e}")

    # ── 6b. Fetch open positions with live marks ───────────────
    # The strategy agent needs visibility into currently-held positions
    # (and their live P&L) every cycle to make stop-loss / take-profit
    # decisions.  Without this, the agent can only propose new trades
    # and cannot evaluate whether to exit existing ones.
    open_positions: list[dict] = []
    try:
        if _journal is not None:
            open_trades = await _journal.get_open_trades()
            option_ids_to_quote: list[str] = []
            for t in open_trades:
                if t.get("ticker") == ticker:
                    oid = t.get("option_id")
                    if oid:
                        option_ids_to_quote.append(oid)
                        # Ensure the option_id→ticker cache is populated
                        OPTION_ID_TO_TICKER[oid] = ticker

            # Fetch live quotes for held option positions
            option_marks: dict[str, dict] = {}
            if option_ids_to_quote:
                raw_oq = await _call_mcp_tool(
                    "get_option_quotes", {"instrument_ids": option_ids_to_quote}
                )
                if raw_oq:
                    for res in raw_oq.get("data", {}).get("results", []) or []:
                        q = res.get("quote") or {}
                        iid = q.get("instrument_id")
                        if iid:
                            option_marks[iid] = q

            # Build position summaries
            for t in open_trades:
                if t.get("ticker") != ticker:
                    continue
                entry_price = float(t.get("price", 0) or 0)
                qty = float(t.get("remaining_quantity") or t.get("quantity", 0) or 0)
                oid = t.get("option_id")
                pos: dict = {
                    "trade_id": t.get("id"),
                    "direction": t.get("direction"),
                    "quantity": qty,
                    "entry_price": entry_price,
                    "option_id": oid,
                    "option_type": t.get("option_type"),
                    "strike": _safe_float(t.get("strike")),
                    "expiration": t.get("expiration"),
                    "opened_at": t.get("timestamp") or t.get("created_at"),
                }

                if oid and oid in option_marks:
                    oq = option_marks[oid]
                    mark = _safe_float(oq.get("adjusted_mark_price") or oq.get("mark_price"))
                    pos["current_mark"] = mark
                    pos["current_bid"] = _safe_float(oq.get("bid_price"))
                    pos["current_ask"] = _safe_float(oq.get("ask_price"))
                    pos["delta"] = _safe_float(oq.get("delta"))
                    pos["theta"] = _safe_float(oq.get("theta"))
                    # Calculate unrealized P&L (options are 100x multiplier)
                    if mark is not None and entry_price > 0:
                        multiplier = 100.0
                        if t.get("direction") == "LONG":
                            pos["unrealized_pnl"] = round(
                                (mark - entry_price) * qty * multiplier, 2
                            )
                        else:
                            pos["unrealized_pnl"] = round(
                                (entry_price - mark) * qty * multiplier, 2
                            )
                        pos["unrealized_pnl_pct"] = round(
                            ((mark - entry_price) / entry_price) * 100
                            if t.get("direction") == "LONG"
                            else ((entry_price - mark) / entry_price) * 100,
                            1,
                        )
                elif not oid:
                    # Equity position — use the quote from step 1
                    if quote_data and quote_data.get("last"):
                        last = float(quote_data["last"])
                        pos["current_price"] = last
                        if entry_price > 0:
                            if t.get("direction") == "LONG":
                                pos["unrealized_pnl"] = round((last - entry_price) * qty, 2)
                            else:
                                pos["unrealized_pnl"] = round((entry_price - last) * qty, 2)
                            pos["unrealized_pnl_pct"] = round(
                                ((last - entry_price) / entry_price) * 100
                                if t.get("direction") == "LONG"
                                else ((entry_price - last) / entry_price) * 100,
                                1,
                            )

                open_positions.append(pos)
    except Exception as e:
        errors.append(f"Failed to fetch open positions with live marks: {e}")

    # ── 7. Level II order book depth (execution intelligence) ───
    level_ii_data: dict | None = None
    if _config and _config.constitution.level_ii.enabled:
        try:
            from evotrader.tools.level_ii import assess_book_depth, fetch_level_ii

            l2_config = _config.constitution.level_ii
            raw_l2 = await fetch_level_ii(
                ticker,
                _call_mcp_tool,
                depth_levels=l2_config.depth_levels,
            )
            if raw_l2:
                level_ii_data = assess_book_depth(raw_l2, config=l2_config)
        except Exception as e:
            errors.append(f"Level II fetch failed (non-fatal): {e}")

    # ── 8. Assemble structured result ──────────────────────────
    from evotrader.tools.asset_context import primary_ticker as _configured_primary

    _primary = _configured_primary()

    return {
        "ticker": ticker,
        "timestamp": now.isoformat(),
        # The composite signal and regime below score this ticker against its
        # OWN price history, using a strategy configured for the primary
        # instrument. On any other symbol they are not a validated signal — a
        # hedging vehicle scored on its own prices backtests at -10.6% precisely
        # because the edge lives in the traded instrument, not the vehicle.
        # Flagged rather than refused: a future multi-instrument strategy may
        # legitimately analyse several symbols, and this stays true either way.
        # For price and volatility on another permitted symbol, use
        # get_ticker_snapshot, which returns no signal at all.
        "is_primary_instrument": (_primary is not None and ticker.upper() == _primary),
        "configured_primary": _primary,
        "quote": quote_data,
        "indicators": indicators,
        "regime": regime_dict,
        "daily_change_pct": daily_change_pct,
        "gap_pct": gap_pct,
        # The same two numbers as text, for anything that summarises them.
        "daily_change_display": _pct_display(daily_change_pct),
        "gap_display": _pct_display(gap_pct),
        "algo_signal": algo_result,
        "portfolio_state": portfolio_state,
        "open_positions": open_positions if open_positions else None,
        "level_ii": level_ii_data,
        # These were both len(candles) — the DAILY count — while the snapshot's
        # recent_candles is the INTRADAY list. 09-18 10:34 reported 62 against
        # 12 real intraday bars, and that is the number the strategy agent
        # quotes when judging intraday data sufficiency.
        "recent_candles_count": len(intraday_candles),
        "daily_candles_count": len(candles),
        "gap_source": gap_source,
        "errors": errors if errors else None,
    }


async def gather_option_chain(
    ticker: str,
    option_type: str = "both",
    expiration_range_days: int = 45,
) -> dict:
    """Fetch the option chain for a ticker via Robinhood MCP.

    Returns available option contracts with strike prices, premiums,
    Greeks (delta, gamma, theta, vega), open interest, and volume.
    Use this to evaluate option trades when equity positions are too
    expensive for the available buying power.

    Args:
        ticker: Underlying stock ticker (e.g., "QQQ").
        option_type: "call", "put", or "both".
        expiration_range_days: Max days to expiration to include (default 45).
    """
    import urllib.parse
    from datetime import UTC, datetime

    errors: list[str] = []
    from evotrader.tools.market_hours import _resolve_now

    now = _resolve_now().astimezone(UTC)
    current_date = now.date()

    # 1. Fetch Option Chains to get available expiration dates
    raw_chain = await _call_mcp_tool("get_option_chains", {"underlying_symbol": ticker})
    if not raw_chain:
        return {
            "ticker": ticker,
            "option_type": option_type,
            "contracts": [],
            "errors": ["Failed to fetch option chain from MCP — tool may not be available yet"],
        }

    chains = raw_chain.get("data", {}).get("chains", []) or []
    if not chains:
        return {
            "ticker": ticker,
            "option_type": option_type,
            "contracts": [],
            "errors": ["No option chains found for ticker"],
        }

    chain = chains[0]
    expiration_dates = chain.get("expiration_dates", []) or []

    # Find the expiration date closest to 30 DTE (days to expiration)
    best_expiry = None
    min_diff = 999
    for d in expiration_dates:
        try:
            exp_date = datetime.strptime(d, "%Y-%m-%d").date()
            diff = abs((exp_date - current_date).days - 30)
            if diff < min_diff:
                min_diff = diff
                best_expiry = d
        except Exception:
            continue

    if not best_expiry:
        best_expiry = expiration_dates[0] if expiration_dates else None

    if not best_expiry:
        return {
            "ticker": ticker,
            "option_type": option_type,
            "contracts": [],
            "errors": ["No expiration dates found on the option chain"],
        }

    # 2. Fetch current underlying price for context and strike selection
    underlying_price: float | None = None
    raw_quote = await _call_mcp_tool("get_equity_quotes", {"symbols": [ticker]})
    if raw_quote:
        results = raw_quote.get("data", {}).get("results", []) or []
        if results:
            q = results[0].get("quote") or {}
            quote = McpEquityQuote(**q)
            underlying_price = quote.resolve_live_price()

    # 3. Fetch instruments for targeted expiration date (with pagination loop)
    instruments: list[dict] = []
    cursor = None
    for _page_idx in range(5):
        mcp_args = {
            "chain_symbol": ticker,
            "expiration_dates": best_expiry,
            "state": "active",
            "tradability": "tradable",
        }
        if option_type in ("call", "put"):
            mcp_args["type"] = option_type
        if cursor:
            mcp_args["cursor"] = cursor

        raw_inst = await _call_mcp_tool("get_option_instruments", mcp_args)
        if not raw_inst:
            break

        data = raw_inst.get("data", {}) or {}
        page_instruments = data.get("instruments", []) or []
        instruments.extend(page_instruments)

        if not page_instruments:
            break

        # Parse next page cursor
        next_url = data.get("next")
        if not next_url:
            break
        try:
            parsed = urllib.parse.urlparse(next_url)
            query_params = urllib.parse.parse_qs(parsed.query)
            cursor_list = query_params.get("cursor")
            if cursor_list:
                cursor = cursor_list[0]
            else:
                break
        except Exception:
            break

    if not instruments:
        return {
            "ticker": ticker,
            "option_type": option_type,
            "contracts": [],
            "errors": ["No option instruments found for the targeted expiration"],
        }

    # 4. Spaced ATM & OTM selection using percentage offsets (for affordable options under $100)
    selected_instruments: list[dict] = []
    if underlying_price:
        calls = [inst for inst in instruments if inst.get("type") == "call"]
        puts = [inst for inst in instruments if inst.get("type") == "put"]

        # Call offsets: ATM up to ~8% out of the money (focus on actionable 0.20–0.55 delta contracts)
        call_offsets = [1.0, 1.01, 1.02, 1.03, 1.05, 1.08]
        # Put offsets: ATM down to ~8% out of the money
        put_offsets = [1.0, 0.99, 0.98, 0.97, 0.95, 0.92]

        for offset in call_offsets:
            target_strike = underlying_price * offset
            if calls:
                best_c = min(
                    calls,
                    key=lambda c: abs((_safe_float(c.get("strike_price")) or 0.0) - target_strike),
                )
                if best_c not in selected_instruments:
                    selected_instruments.append(best_c)

        for offset in put_offsets:
            target_strike = underlying_price * offset
            if puts:
                best_p = min(
                    puts,
                    key=lambda p: abs((_safe_float(p.get("strike_price")) or 0.0) - target_strike),
                )
                if best_p not in selected_instruments:
                    selected_instruments.append(best_p)

        selected_instruments = selected_instruments[:12]
    else:
        selected_instruments = instruments[:10]

    # 4b. ALWAYS include currently-held option positions ─────────────
    # Offset-based selection can miss contracts the agent already owns
    # (e.g., a held call/put whose strike has drifted far from spot after
    # a big move).  Without live marks for held positions, the strategy
    # agent cannot propose a verifiable CLOSE and gets trapped holding
    # offside positions across many cycles (observed 2026-07-07: 4+ NO_TRADE
    # cycles while offside calls kept losing value).  Force-include them here;
    # they are EXEMPT from the 20-contract cap.
    try:
        held_ids: set[str] = set()
        if _journal is not None:
            open_trades = await _journal.get_open_trades()
            for t in open_trades:
                oid = t.get("option_id")
                # Match this underlying only; skip non-option equity rows.
                if oid and (t.get("ticker") == ticker or OPTION_ID_TO_TICKER.get(oid) == ticker):
                    held_ids.add(oid)
        already = {inst.get("id") for inst in selected_instruments}
        missing_held = held_ids - already
        if missing_held:
            # Try to resolve full instrument metadata from the fetched page;
            # if the held contract is not on this expiration page, still add a
            # minimal stub so its quote is fetched and surfaced to the agent.
            by_id = {inst.get("id"): inst for inst in instruments}
            for hid in missing_held:
                inst = by_id.get(hid) or {
                    "id": hid,
                    "type": "unknown",
                    "strike_price": None,
                    "expiration_date": None,
                }
                inst["_held_position"] = True  # flag for downstream labelling
                selected_instruments.append(inst)
    except Exception as _held_exc:
        errors.append(f"Could not force-include held option positions: {_held_exc}")

    # 5. Fetch Quotes for selected contracts
    instrument_ids = [inst.get("id") for inst in selected_instruments if inst.get("id")]
    quotes_map: dict[str, dict] = {}
    if instrument_ids:
        raw_quotes = await _call_mcp_tool("get_option_quotes", {"instrument_ids": instrument_ids})
        if raw_quotes:
            results = raw_quotes.get("data", {}).get("results", []) or []
            for res in results:
                q = res.get("quote") or {}
                inst_id = q.get("instrument_id")
                if inst_id:
                    quotes_map[inst_id] = q

    # 6. Format final contracts and populate global option_id cache
    contracts: list[dict] = []
    for inst in selected_instruments:
        inst_id = inst.get("id")
        if not inst_id:
            continue
        q = quotes_map.get(inst_id, {})

        # Populate option_id to underlying ticker cache for Risk Gate lookup
        OPTION_ID_TO_TICKER[inst_id] = ticker

        strike = _safe_float(inst.get("strike_price"))
        expiry = inst.get("expiration_date") or ""

        dte = None
        if expiry:
            try:
                expiry_dt = datetime.strptime(expiry, "%Y-%m-%d").date()
                dte = (expiry_dt - current_date).days
            except Exception:
                pass

        contract = {
            "option_id": inst_id,
            "type": inst.get("type") or "unknown",
            "strike": strike,
            "expiration": expiry,
            "dte": dte,
            "is_held_position": bool(inst.get("_held_position", False)),
            "bid": _safe_float(q.get("bid_price")),
            "ask": _safe_float(q.get("ask_price")),
            "mark": _safe_float(q.get("adjusted_mark_price") or q.get("mark_price")),
            "last": _safe_float(q.get("last_trade_price")),
            "volume": int(q.get("volume", 0) or 0),
            "open_interest": int(q.get("open_interest", 0) or 0),
            "implied_volatility": _safe_float(q.get("implied_volatility")),
            "delta": _safe_float(q.get("delta")),
            "gamma": _safe_float(q.get("gamma")),
            "theta": _safe_float(q.get("theta")),
            "vega": _safe_float(q.get("vega")),
        }
        contracts.append(contract)

    # Sort final contracts list by type then strike for readability
    contracts.sort(key=lambda c: (c.get("type") or "", c.get("strike") or 0.0))

    if not contracts:
        errors.append("No active option contracts matched filters")

    # 7. Budget picks — surface the cheapest liquid meaningful-delta contract
    # per side so the agent can prefer it over deep-OTM lottery tickets or
    # all-in ATM sizing (useful for small accounts).
    budget_picks: dict[str, dict | None] = {}
    for side in ("call", "put"):
        cands = [
            c
            for c in contracts
            if c["type"] == side
            and c.get("delta") is not None
            and abs(c["delta"]) >= 0.20
            and (c.get("open_interest") or 0) >= 50
            and (c.get("ask") or 0) > 0
        ]
        budget_picks[side] = min(cands, key=lambda c: c.get("ask") or 0.0) if cands else None

    # 8. IV trend from options historicals (for options_positioning strategy)
    iv_trend_data: dict | None = None
    try:
        from evotrader.tools.options_historicals import get_atm_iv_trend

        # Find the ATM call instrument for IV trend analysis
        atm_call_id: str | None = None
        if underlying_price:
            atm_calls = [
                c
                for c in contracts
                if c.get("type") == "call" and c.get("strike") is not None and c.get("option_id")
            ]
            if atm_calls:
                atm_call = min(
                    atm_calls,
                    key=lambda c: abs((c["strike"] or 0) - underlying_price),
                )
                atm_call_id = atm_call.get("option_id")

        iv_trend_data = await get_atm_iv_trend(
            ticker,
            atm_call_id,
            _call_mcp_tool,
        )
    except Exception as e:
        errors.append(f"IV trend analysis failed (non-fatal): {e}")

    # Record the near-money put/call reading for the options_positioning
    # baseline. Observation only: the composite has already been computed by
    # gather_market_data, so this does not — and cannot yet — change any signal.
    # See evotrader.tools.options_pcr for why feeding the channel is phase 2.
    if _config is not None and contracts:
        try:
            from evotrader.tools.options_pcr import pcr_from_chain, record_pcr

            record_pcr(_config.data_dir, ticker, pcr_from_chain(contracts))
        except Exception as e:  # never break the chain tool over a side record
            logger.debug("put/call recording skipped: %s", e)

    return {
        "ticker": ticker,
        "underlying_price": underlying_price,
        "option_type": option_type,
        "expiration_range_days": expiration_range_days,
        "contracts_count": len(contracts),
        "contracts": contracts,
        "budget_picks": budget_picks,
        "iv_trend": iv_trend_data,
        "timestamp": now.isoformat(),
        "errors": errors if errors else None,
    }


# ═══════════════════════════════════════════════════════════════════════
# Strategy Tools
# ═══════════════════════════════════════════════════════════════════════


def _indicator_field_kinds() -> dict[str, type]:
    """``{field: str | int | float}`` as declared on ``TechnicalIndicators``."""
    import typing

    from evotrader.models.market import TechnicalIndicators

    kinds: dict[str, type] = {}
    for name, field in TechnicalIndicators.model_fields.items():
        args = [a for a in typing.get_args(field.annotation) if a is not type(None)]
        declared = args[0] if len(args) == 1 else field.annotation
        kinds[name] = declared if declared in (str, int, float) else float
    return kinds


def normalize_market_snapshot(data: dict) -> dict:
    """Normalize a raw dictionary into input for ``MarketSnapshot.model_validate``.

    Translates common variations in field names, coerces indicator fields to the
    types ``TechnicalIndicators`` declares, and fills defaults for missing
    required fields so validation does not fail. This is the ENGINE-side
    normaliser: its output feeds the composite and the risk budget.

    Not to be confused with ``indicators.registry.normalize_snapshot_payload``,
    which builds the flat DISPLAY/storage payload for the console and the
    ``market_snapshots`` table. The two take the same raw shapes but serve
    different consumers, so they are kept separate.
    """
    import datetime

    # Fall back to the configured instrument, not a hardcoded symbol: a
    # snapshot missing its ticker must not be silently labelled with one.
    fallback_ticker = primary_ticker()
    if _config:
        fallback_ticker = _config.settings.asset.primary_ticker or fallback_ticker

    now_str = datetime.datetime.now(datetime.UTC).isoformat()

    # Top-level fields
    ticker = data.get("ticker") or data.get("symbol") or fallback_ticker
    timestamp = data.get("timestamp") or data.get("time") or now_str

    # 1. Normalize Quote
    raw_quote = data.get("quote") or {}
    if isinstance(raw_quote, dict):
        q_ticker = raw_quote.get("ticker") or raw_quote.get("symbol") or ticker
        q_last = (
            raw_quote.get("last")
            or raw_quote.get("last_close_price")
            or raw_quote.get("price")
            or 0.0
        )
        q_bid = raw_quote.get("bid") or q_last
        q_ask = raw_quote.get("ask") or q_last
        q_volume = raw_quote.get("volume") or 0.0
        q_timestamp = raw_quote.get("timestamp") or raw_quote.get("time") or timestamp
        # previous_close must survive normalisation: range_break_continuation
        # falls back to it when daily_change_pct is absent.  Dropping it here
        # made that fallback permanently unreachable.
        q_prev_close = raw_quote.get("previous_close") or raw_quote.get("adjusted_previous_close")

        normalized_quote = {
            "ticker": str(q_ticker),
            "bid": float(q_bid),
            "ask": float(q_ask),
            "last": float(q_last),
            "volume": float(q_volume),
            "timestamp": q_timestamp,
            "previous_close": float(q_prev_close) if q_prev_close else None,
        }
    else:
        normalized_quote = {
            "ticker": ticker,
            "bid": 0.0,
            "ask": 0.0,
            "last": 0.0,
            "volume": 0.0,
            "timestamp": timestamp,
            "previous_close": None,
        }

    # 2. Normalize Indicators
    raw_ind = data.get("indicators") or {}
    normalized_ind = {}
    if isinstance(raw_ind, dict):
        # Oscillators
        normalized_ind["rsi_14"] = (
            raw_ind.get("rsi_14") or raw_ind.get("rsi_14_day") or raw_ind.get("rsi")
        )
        normalized_ind["macd_line"] = raw_ind.get("macd_line") or raw_ind.get("macd")
        normalized_ind["macd_signal"] = raw_ind.get("macd_signal") or raw_ind.get("signal")
        normalized_ind["macd_histogram"] = raw_ind.get("macd_histogram") or raw_ind.get("histogram")

        # Bands
        normalized_ind["bollinger_upper"] = raw_ind.get("bollinger_upper") or raw_ind.get(
            "bollinger_upper_band"
        )
        normalized_ind["bollinger_middle"] = raw_ind.get("bollinger_middle") or raw_ind.get(
            "bollinger_middle_band"
        )
        normalized_ind["bollinger_lower"] = raw_ind.get("bollinger_lower") or raw_ind.get(
            "bollinger_lower_band"
        )
        normalized_ind["bollinger_width"] = raw_ind.get("bollinger_width")

        # Trend
        normalized_ind["ema_9"] = raw_ind.get("ema_9") or raw_ind.get("ema_9_day")
        normalized_ind["ema_21"] = raw_ind.get("ema_21") or raw_ind.get("ema_21_day")
        normalized_ind["sma_20"] = (
            raw_ind.get("sma_20") or raw_ind.get("sma_20_day") or raw_ind.get("sma_20_day_sma")
        )
        normalized_ind["sma_50"] = raw_ind.get("sma_50") or raw_ind.get("sma_50_day")

        # Intraday
        normalized_ind["vwap"] = raw_ind.get("vwap")
        normalized_ind["ibs"] = raw_ind.get("ibs")
        # vwap_anchor (string field — MUST NOT be float-coerced)
        normalized_ind["vwap_anchor"] = (
            str(raw_ind["vwap_anchor"]) if raw_ind.get("vwap_anchor") is not None else None
        )

        # Volatility
        normalized_ind["atr_14"] = (
            raw_ind.get("atr_14") or raw_ind.get("atr_14_day") or raw_ind.get("atr")
        )
        normalized_ind["volume_sma_20"] = raw_ind.get("volume_sma_20")
        normalized_ind["relative_volume"] = raw_ind.get("relative_volume")
        normalized_ind["relative_volume_source"] = raw_ind.get("relative_volume_source")
        normalized_ind["session_relative_volume"] = raw_ind.get("session_relative_volume")
        normalized_ind["session_relative_volume_sessions"] = raw_ind.get(
            "session_relative_volume_sessions"
        )

        # Event context
        normalized_ind["atm_iv_30dte"] = raw_ind.get("atm_iv_30dte")
        normalized_ind["atm_iv_pre_event"] = raw_ind.get("atm_iv_pre_event")
        normalized_ind["hours_to_event"] = raw_ind.get("hours_to_event")
        normalized_ind["hours_since_event"] = raw_ind.get("hours_since_event")
        normalized_ind["event_type"] = (
            str(raw_ind["event_type"]) if raw_ind.get("event_type") is not None else None
        )

        # Coerce each field to the type the model declares. This used to be a
        # hand-kept set of "string fields" with everything else forced to
        # float, so any new text field was silently nulled unless someone
        # remembered to add it — `relative_volume_source` was, on 2026-09-23.
        # Reading the type from TechnicalIndicators keeps the two in step.
        field_kinds = _indicator_field_kinds()
        for k, v in list(normalized_ind.items()):
            if v is None:
                normalized_ind[k] = None
                continue
            kind = field_kinds.get(k, float)
            try:
                normalized_ind[k] = str(v) if kind is str else kind(v)
            except Exception:
                normalized_ind[k] = None

    # 3. Normalize Regime
    raw_reg = data.get("regime") or {}
    if isinstance(raw_reg, str):
        normalized_reg = {
            "regime": raw_reg,
            "confidence": 1.0,
            "reasoning": "Inferred from text input",
        }
    elif isinstance(raw_reg, dict):
        reg_val = raw_reg.get("regime") or raw_reg.get("classification") or "range_bound"
        reg_conf = raw_reg.get("confidence")
        if reg_conf is not None:
            if isinstance(reg_conf, str):
                try:
                    reg_conf = float(reg_conf)
                except ValueError:
                    reg_conf = (
                        0.5
                        if reg_conf.lower() == "low"
                        else (0.8 if reg_conf.lower() == "medium" else 1.0)
                    )
        else:
            reg_conf = 1.0

        normalized_reg = {
            "regime": str(reg_val),
            "confidence": float(reg_conf),
            "reasoning": str(raw_reg.get("reasoning") or "Inferred from strategy inputs"),
        }
    else:
        normalized_reg = {
            "regime": "range_bound",
            "confidence": 1.0,
            "reasoning": "Defaulted due to missing classification",
        }

    # 4. Normalize Options Context
    raw_opt = data.get("options_context")
    normalized_opt = None
    if isinstance(raw_opt, dict):

        def _opt_float(v: object) -> float | None:
            if v is None:
                return None
            try:
                return float(v)
            except (ValueError, TypeError):
                return None

        normalized_opt = {
            "pc_volume_ratio": _opt_float(raw_opt.get("pc_volume_ratio")),
            "pc_oi_ratio": _opt_float(raw_opt.get("pc_oi_ratio")),
            "event_hours_away": _opt_float(raw_opt.get("event_hours_away")),
            "event_hours_since": _opt_float(raw_opt.get("event_hours_since")),
            "pc_ratio_change": _opt_float(raw_opt.get("pc_ratio_change")),
        }

    # ── Derived percent fields ────────────────────────────────────
    # These were previously omitted from the returned dict, so Pydantic
    # applied None defaults and FOUR guards silently never fired:
    #   momentum.intraday_divergence_decay, gap (abstained 100% of cycles),
    #   range_break_continuation magnitude gate, trend_persistence counter-day.
    # Units convention (models/market.py): percent, 1.0 == 1%.
    def _coerce_pct(v: object) -> float | None:
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            logger.warning("normalize_market_snapshot: non-numeric percent field %r dropped", v)
            return None

    daily_change_pct = _coerce_pct(data.get("daily_change_pct"))
    gap_pct = _coerce_pct(data.get("gap_pct"))

    # Last-resort derivation so a missing field cannot silently disable
    # the divergence guard again.
    if daily_change_pct is None:
        _last = normalized_quote.get("last") or 0.0
        _prev = normalized_quote.get("previous_close") or 0.0
        if _last > 0 and _prev > 0:
            daily_change_pct = round(((_last - _prev) / _prev) * 100, 4)

    return {
        "ticker": str(ticker),
        "timestamp": timestamp,
        "quote": normalized_quote,
        "indicators": normalized_ind,
        "regime": normalized_reg,
        "daily_change_pct": daily_change_pct,
        "gap_pct": gap_pct,
        "options_context": normalized_opt,
        "recent_candles": [
            {
                "timestamp": c.get("timestamp") or c.get("begins_at") or timestamp,
                "open": float(c.get("open") or c.get("open_price") or 0.0),
                "high": float(c.get("high") or c.get("high_price") or 0.0),
                "low": float(c.get("low") or c.get("low_price") or 0.0),
                "close": float(c.get("close") or c.get("close_price") or 0.0),
                "volume": float(c.get("volume") or 0.0),
            }
            for c in (data.get("recent_candles") or [])
            if isinstance(c, dict)
        ],
        "daily_candles": [
            {
                "timestamp": c.get("timestamp") or c.get("begins_at") or timestamp,
                "open": float(c.get("open") or c.get("open_price") or 0.0),
                "high": float(c.get("high") or c.get("high_price") or 0.0),
                "low": float(c.get("low") or c.get("low_price") or 0.0),
                "close": float(c.get("close") or c.get("close_price") or 0.0),
                "volume": float(c.get("volume") or 0.0),
            }
            for c in (data.get("daily_candles") or [])
            if isinstance(c, dict)
        ],
    }


def run_composite_strategy(market_snapshot_json: str) -> dict:
    """Run the composite trading strategy on a market snapshot.

    Takes a market snapshot (with quote + indicators + regime) and
    returns the detailed signal breakdown from all sub-strategies
    (momentum, mean reversion, gap) with regime-adaptive weighting.

    Args:
        market_snapshot_json: JSON string of a MarketSnapshot object,
            containing quote, indicators, and regime fields.
    """
    from evotrader.models.market import MarketSnapshot

    # Parse and normalize JSON to handle variation in LLM field names and syntax errors
    try:
        raw_data = safe_parse_json(market_snapshot_json)
        if isinstance(raw_data, dict):
            normalized_data = normalize_market_snapshot(raw_data)
            snapshot = MarketSnapshot.model_validate(normalized_data)
        else:
            snapshot = MarketSnapshot.model_validate(raw_data)
    except Exception as e:
        logger.error("Failed to parse or validate market snapshot JSON: %s", e)
        # Return fallback values with error message to prevent the agent cycle from crashing
        return {
            "composite_signal": 0.0,
            "algo_version": "error_fallback",
            "error": f"Invalid market snapshot JSON/schema: {e!s}",
            "sub_signals": [],
        }

    # Load current algo parameters from registry
    params = {}
    if _algo_registry:
        params = _algo_registry.load_parameters()

    strategy = _get_strategy_loader().build_composite(
        params,
        active_version=_algo_registry.get_active_version() if _algo_registry else None,
    )

    detailed = strategy.compute_detailed_signal(snapshot)

    return {
        "composite_signal": detailed.composite_value,
        "algo_version": detailed.algo_version,
        "sub_signals": [
            {
                "name": s.name,
                "value": s.value,
                "weight": s.weight,
                "metadata": s.metadata,
            }
            for s in detailed.signals
        ],
        **_composite_provenance(detailed),
    }


async def _refill_daily_candles(snapshot, ticker: str):
    """Re-fetch the daily history the supplied snapshot is missing.

    The agent hand-summarises ``market_snapshot_json`` rather than pasting 21+
    candles into a tool argument — verbatim from a live call, the whole
    ``daily_candles`` key was simply absent. That made volatility-targeted
    sizing inert in every observed cycle while the return value still looked
    like a measurement. Recovering the history server-side removes the agent's
    retyping from the critical path.

    Read-only: this fetches price history and cannot place or cancel an order.
    """
    from datetime import timedelta

    from evotrader.models.market import OHLCV

    now = datetime.now(UTC)
    raw = await _call_mcp_tool(
        "get_equity_historicals",
        {
            "symbols": [ticker],
            "interval": "day",
            "start_time": (now - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    candles = _parse_hist_candles(raw, now)
    if not candles:
        return snapshot
    return snapshot.model_copy(update={"daily_candles": [OHLCV.model_validate(c) for c in candles]})


async def compute_risk_budget(market_snapshot_json: str, ticker: str = "") -> dict:
    """Compute how much exposure equals one unit of risk right now.

    Returns the risk budget: a multiple of capital that, at current realised
    volatility, carries the configured amount of portfolio risk. Large in calm
    markets, small in turbulent ones.

    This does NOT decide direction. It converts your conviction into a position
    size — the same conviction should buy fewer dollars when the market is
    violent, because those dollars carry more risk.

    Use it as: position = risk_budget x direction x conviction

    Args:
        market_snapshot_json: JSON string of a MarketSnapshot, including
            ``daily_candles`` (realised volatility is measured from them).
            If you are summarising rather than pasting the snapshot verbatim,
            ``daily_candles`` is the ONE field you must not drop — without it
            this tool has nothing to measure volatility from and degrades to a
            flat 1.0.
        ticker: Optional. When the supplied snapshot carries no usable
            ``daily_candles``, the history is fetched server-side for this
            ticker instead. Read-only; this cannot place or cancel an order.
    """
    from evotrader.algorithms.exposure import VolatilityTargetStrategy
    from evotrader.models.market import MarketSnapshot

    try:
        raw = safe_parse_json(market_snapshot_json)
        data = normalize_market_snapshot(raw) if isinstance(raw, dict) else raw
        snapshot = MarketSnapshot.model_validate(data)
    except Exception as e:
        logger.error("compute_risk_budget: bad snapshot: %s", e)
        return {
            "risk_budget": 1.0,
            "realized_volatility": None,
            "degraded": True,
            "error": f"Invalid market snapshot: {e!s}",
            "note": "Falling back to 1.0x. Size conservatively until this is fixed.",
        }

    # Recover the volatility input ourselves rather than return a number that
    # looks like a measurement and is not. VolatilityTargetStrategy needs 21
    # closes; below that it reports `insufficient_history` and falls back to 1.0.
    _MIN_CANDLES = 21
    if len(snapshot.daily_candles or []) < _MIN_CANDLES and (ticker or snapshot.ticker):
        try:
            snapshot = await _refill_daily_candles(snapshot, ticker or snapshot.ticker)
        except Exception as e:  # never break a cycle over a sizing input
            logger.warning("compute_risk_budget: candle refill failed: %s", e)

    target = 0.20
    max_exposure = 1.5
    if _config is not None:
        sizing = getattr(getattr(_config, "settings", None), "position_sizing", None)
        target = getattr(sizing, "volatility_target_annual", target) or target
        limits = getattr(getattr(_config, "constitution", None), "risk_limits", None)
        cap = getattr(limits, "max_total_exposure_pct", None)
        if cap:
            max_exposure = float(cap)

    strategy = VolatilityTargetStrategy(
        target_volatility=target, max_exposure=max_exposure, rebalance_band=0.0
    )
    result = strategy.compute_exposure(snapshot, current_exposure=1.0)
    n_candles = len(snapshot.daily_candles or [])
    degraded = n_candles < _MIN_CANDLES

    # What the volatility measurement asked for, before the floor. On a 6.5%-ATR
    # instrument a 15-20% annual target implies ~0.2x, so the 0.30 floor binds on
    # every cycle and this tool emits a constant — `capped_by` alone reads like a
    # judgement rather than a clamp, and the agent sized against it 24 cycles
    # running. Say which number is actually setting the budget.
    raw_exposure = (
        round(result.target_volatility / result.realized_volatility, 4)
        if result.realized_volatility
        else None
    )
    floor_binding = raw_exposure is not None and raw_exposure < strategy.min_exposure

    return {
        "risk_budget": result.value,
        "realized_volatility": result.realized_volatility,
        "target_volatility": result.target_volatility,
        "raw_exposure": raw_exposure,
        "min_exposure": strategy.min_exposure,
        "floor_binding": floor_binding,
        **(
            {
                "floor_note": (
                    f"risk_budget is the {strategy.min_exposure:g} FLOOR, not a "
                    f"measurement — measured volatility implies {raw_exposure:g}x. It "
                    f"will read the same on every cycle while this holds, so it cannot "
                    f"discriminate between setups. Size on conviction and the regime "
                    f"band, not on this number."
                )
            }
            if floor_binding
            else {}
        ),
        "capped_by": result.capped_by,
        "reason": result.reason,
        "max_exposure": max_exposure,
        # `capped_by` alone reads like a legitimate cap, so the inert path was
        # indistinguishable from a measured one. Say so explicitly.
        "daily_candles_used": n_candles,
        "degraded": degraded,
        **(
            {
                "missing": ["daily_candles"],
                "note": (
                    "THIS IS A FALLBACK, NOT A MEASUREMENT. risk_budget 1.0 means "
                    "volatility could not be measured — it does NOT mean the market "
                    "is calm. Do not cite it as evidence either way."
                ),
            }
            if degraded
            else {}
        ),
        "usage": "position = risk_budget x direction x conviction",
    }


async def log_signal_attribution(attribution_json: str) -> dict:
    """Record what each witness said this cycle, before they were combined.

    Call this EVERY cycle, including cycles where you did not trade. No-trade
    cycles are the control group: without them the record only contains
    situations you already liked, and any measured edge is selection rather
    than skill.

    The Evolution Agent scores each witness against realised forward returns to
    learn which one actually predicts, and checks whether your stated conviction
    matches your realised hit rate. Logging only the blended answer teaches it
    nothing.

    Args:
        attribution_json: JSON object with any of these keys —
            ``ticker`` (required),
            ``algo_direction``, ``algo_strength``, ``algo_author``,
            ``participation_ratio``,
            ``news_direction``, ``news_strength``, ``catalyst``,
            ``llm_direction``, ``llm_conviction``, ``mechanism``,
            ``priced_in_check`` (object: known_fact, when_learned, mechanism,
            falsifier), ``final_direction``, ``final_conviction``,
            ``risk_budget``, ``position_size``, ``traded``,
            ``price_at_decision``,
            ``deviation_atr``, ``trigger_atr``.
            Directions are -1 (short), 0 (flat/abstain), +1 (long).

            ``deviation_atr`` and ``trigger_atr`` apply when
            ``intraday_vwap_zscore`` is the authoring signal: copy them
            straight from that sub-signal's metadata. They record how far
            price sat from session VWAP and the firing threshold in force on
            that cycle, both in ATRs, so realised returns can later be bucketed
            by dislocation DEPTH. That is the physical quantity; the z-score is
            a rescaling whose units move with ``min_std``. Omit them on cycles
            this channel did not author.
    """
    from evotrader.db.signal_attribution import SignalAttributionStore

    if _db is None:
        return {"status": "error", "error": "database not bound"}

    try:
        data = safe_parse_json(attribution_json)
        if not isinstance(data, dict):
            return {"status": "error", "error": "attribution must be a JSON object"}
    except Exception as e:
        return {"status": "error", "error": f"Invalid attribution JSON: {e!s}"}

    ticker = str(data.get("ticker") or "").strip()
    if not ticker:
        return {"status": "error", "error": "ticker is required"}

    allowed = {
        "algo_direction",
        "algo_strength",
        "algo_author",
        "participation_ratio",
        "news_direction",
        "news_strength",
        "catalyst",
        "llm_direction",
        "llm_conviction",
        "mechanism",
        "priced_in_check",
        "final_direction",
        "final_conviction",
        "risk_budget",
        "position_size",
        "traded",
        "price_at_decision",
        # Physical dislocation depth, for bucketing outcomes by how far price
        # actually was from VWAP rather than by a z-score whose units shift
        # with min_std. See migration 0017.
        "deviation_atr",
        "trigger_atr",
        # The composite before the participation attenuation, and the factor
        # applied. Both are reported by gather_market_data. Recorded so a
        # channel can be scored across hours without mixing denominators —
        # see migration 0024.
        "composite_unattenuated",
        "participation_scale",
    }
    kwargs = {k: v for k, v in data.items() if k in allowed}
    if "traded" in kwargs:
        kwargs["traded"] = bool(kwargs["traded"])

    store = SignalAttributionStore(_db)
    row_id = await store.record(ticker, session_id=_current_session_id, **kwargs)
    if row_id is None:
        return {"status": "error", "error": "failed to write attribution row"}
    return {
        "status": "ok",
        "id": row_id,
        "note": "Forward returns are backfilled once the horizon elapses.",
    }


# ═══════════════════════════════════════════════════════════════════════
# Risk Management Tools
# ═══════════════════════════════════════════════════════════════════════


def _daily_loss_verdict(
    today_pnl: float,
    account_value: float | None,
    max_daily_loss_pct: float,
    *,
    is_exit: bool,
) -> tuple[str | None, str | None]:
    """``(violation, warning)`` for the constitution's daily-loss limit.

    The limit is a share of the REAL account. Both risk checks used to test
    ``today_pnl < -(max_daily_loss_pct * max_order_value_usd * 10)`` — 5% of
    $100,000, i.e. $5,000 whatever the real balance — so on a small account it
    could never fire.

    Counts closed-trade losses today. Past the limit, new entries are refused
    for the rest of the day; an exit or protective order never is, because the
    sell that stops a loss is the last order a loss limit should block.
    """
    if today_pnl >= 0:
        return None, None
    if not account_value or account_value <= 0:
        return None, (
            f"Daily-loss limit not evaluated: account value unavailable "
            f"(closed-trade P&L today ${today_pnl:.2f})."
        )
    limit = max_daily_loss_pct * account_value
    if -today_pnl < limit:
        return None, None
    detail = (
        f"Daily loss ${-today_pnl:.2f} exceeds the {max_daily_loss_pct:.0%} limit "
        f"(${limit:.2f} of the ${account_value:.2f} account)"
    )
    if is_exit:
        return None, f"{detail}, but this is an exit — permitted so risk can be reduced."
    return f"{detail}: no new entries until the next session. Exits stay allowed.", None


async def _recorded_account_value() -> float | None:
    """Last account value the metrics job recorded — used when the broker cannot
    be asked, so a broker outage does not switch the daily-loss limit off."""
    if _metrics is None:
        return None
    try:
        latest = await _metrics.get_latest_metrics(days=1)
        return float(latest[-1].portfolio_value) if latest else None
    except Exception as e:
        logger.warning("Recorded account value unavailable: %s", e)
        return None


async def check_risk_limits(
    ticker: str,
    direction: str,
    quantity: float,
    price: float,
    action: str = "OPEN",
) -> dict:
    """Check a proposed trade against all risk limits in the constitution.

    Validates the trade against maximum position size, daily loss limits,
    trade count limits, consecutive loss circuit breakers, and allowed
    tickers. Returns a verdict: APPROVED, MODIFIED, or REJECTED.

    Exit trades (CLOSE, STOP_LOSS, TAKE_PROFIT) are exempt from circuit
    breakers, the daily-loss limit, daily trade count, and buying power
    checks — closing a position reduces risk, it should never be blocked.

    Args:
        ticker: Stock ticker symbol (e.g., "SPY").
        direction: Trade direction ("LONG" or "SHORT").
        quantity: Number of shares.
        price: Expected price per share.
        action: Trade action — "OPEN", "CLOSE", "STOP_LOSS", or "TAKE_PROFIT".
    """
    if not _config or not _journal:
        return {"error": "Risk system not initialised"}

    constitution = _config.constitution
    rules = constitution.trading_rules
    limits = constitution.risk_limits
    breakers = constitution.circuit_breakers

    violations: list[str] = []
    warnings: list[str] = []

    # Computed up front because several checks below are ENTRY policy and must
    # not be enforced on an order that reduces risk. Shared with the
    # pre-execution gate via `callbacks.exit_policy` so the two cannot drift
    # apart again — on 2026-09-11 they did, and every order the gate blocked
    # carried this function's own "APPROVED (0 violations, 0 warnings)"
    # verdict moments earlier.
    is_exit = is_exit_action(action)

    # 1. Allowed ticker check — ENTRY ONLY.
    # An allowlist governs what the system may newly buy into. Applying it to
    # an exit strands any position whose symbol was later removed: the
    # 2026-09-10 narrowing to ["MSTR"] left a QQQ position unexitable and
    # unprotected. Closing a legacy holding is always permitted.
    if ticker not in rules.allowed_tickers:
        if is_exit:
            warnings.append(
                f"Ticker '{ticker}' is not in the allowed list "
                f"{rules.allowed_tickers}, but this is an exit "
                f"({action.upper()}) — permitted so the position can be closed."
            )
        else:
            violations.append(f"Ticker '{ticker}' not in allowed list: {rules.allowed_tickers}")

    # 2. Short selling check
    if direction.upper() == "SHORT" and not rules.allow_short_sell:
        violations.append("Short selling is disabled in constitution")

    # 3. Order value check — an entry limit (see callbacks/risk_gate.py): an exit
    #    returns exposure already held and is never refused for its size.
    order_value = quantity * price
    if order_value > limits.max_order_value_usd and not is_exit:
        violations.append(
            f"Order value ${order_value:.2f} exceeds limit ${limits.max_order_value_usd:.2f}"
        )

    # 4. Daily loss — evaluated after step 8, which fetches the account value
    #    the limit is a share of.
    today_pnl = await _journal.get_today_pnl()

    # 5. Consecutive losses circuit breaker
    # EXIT TRADES ARE EXEMPT — blocking a close traps the agent in a losing
    # position, which is MORE risky than allowing the exit.
    # The breaker only blocks for `pause_duration_minutes` after the last loss;
    # once the cooldown expires, trading resumes even if the streak is unbroken.
    # Session-scoped: the streak resets at each trading session boundary so a
    # Friday streak cannot block Monday morning trading (see review
    # 20260727_204248_circuit_breaker_streak_semantics).
    is_exit = is_exit_action(action)
    consecutive_losses = await _journal.get_session_consecutive_losses()
    if consecutive_losses >= breakers.consecutive_losses_pause and not is_exit:
        last_loss_ts = await _journal.get_last_loss_timestamp()
        pause_expired = last_loss_ts is not None and datetime.now(UTC) - last_loss_ts > timedelta(
            minutes=breakers.pause_duration_minutes
        )
        if not pause_expired:
            violations.append(
                f"Circuit breaker: {consecutive_losses} consecutive losses "
                f"(limit: {breakers.consecutive_losses_pause}), "
                f"pause expires {breakers.pause_duration_minutes}min after last loss"
            )

    # 6. Daily trade count check (exits exempt — you must always be able to close)
    trade_count = await _journal.get_trade_count_today()
    if trade_count >= rules.max_trades_per_day and not is_exit:
        violations.append(f"Daily trade limit reached: {trade_count}/{rules.max_trades_per_day}")

    # 7. Extended/Overnight hours check
    from evotrader.tools.market_hours import (
        MarketSession,
        get_current_session,
        is_twenty_four_hour_eligible,
    )

    session = get_current_session(twenty_four_hour=getattr(rules, "allow_extended_hours", False))
    if session != MarketSession.REGULAR:
        if not getattr(rules, "allow_extended_hours", False):
            violations.append("Trading outside regular hours is disabled in constitution")
        elif session == MarketSession.CLOSED:
            violations.append("Market is closed (no active trading session)")
        else:
            # We are in PRE_MARKET, AFTER_HOURS, or OVERNIGHT
            if session == MarketSession.OVERNIGHT:
                is_eligible = is_twenty_four_hour_eligible(ticker) and (
                    ticker in getattr(rules, "extended_hours_tickers", [])
                )
                if not is_eligible:
                    violations.append(f"Ticker '{ticker}' is not eligible for overnight trading")
            warnings.append(f"Trading in {session.value} session (limit orders only)")

    # 8. Live cash / buying power check
    cash_balance: float | None = None
    buying_power: float | None = None
    portfolio_value: float | None = None
    try:
        portfolio = await _agentic_portfolio()
        if portfolio:
            cash_balance = portfolio["cash_balance"]
            portfolio_value = portfolio["total_value"] or None
            buying_power = portfolio["buying_power"]
            # Position-effect aware: a buy-to-cover on an existing
            # short RELEASES collateral rather than consuming buying
            # power, so it must not be blocked by the BP ceiling.
            is_risk_reducing_cover = False
            if direction.upper() == "SHORT":
                try:
                    open_trades = await _journal.get_open_trades()
                    open_short_qty = sum(
                        float(t.get("quantity", 0) or 0)
                        for t in open_trades
                        if t.get("ticker") == ticker
                        and str(t.get("direction", "")).upper() == "SHORT"
                    )
                    # Covering up to the open short qty is a close,
                    # not an open — it frees collateral.
                    if open_short_qty > 0 and quantity <= open_short_qty + 1e-9:
                        is_risk_reducing_cover = True
                except Exception as exc:
                    warnings.append(f"Could not classify open/close for BP check: {exc}")

            if (
                buying_power is not None
                and order_value > buying_power
                and not is_risk_reducing_cover
                and not is_exit
            ):
                violations.append(
                    f"Order value ${order_value:.2f} exceeds buying power ${buying_power:.2f}"
                )
            elif (is_risk_reducing_cover or is_exit) and order_value > (buying_power or 0):
                warnings.append(
                    "Exit/cover trade exceeds free buying power but is risk-reducing; "
                    "allowed per position-effect aware check."
                )
    except Exception as e:
        warnings.append(f"Could not verify cash balance: {e}")

    # 8b. Daily loss, as a share of the real account (see _daily_loss_verdict).
    account_value = portfolio_value
    if not account_value and today_pnl < 0:
        account_value = await _recorded_account_value()
    loss_violation, loss_warning = _daily_loss_verdict(
        today_pnl,
        account_value,
        limits.max_daily_loss_pct,
        is_exit=is_exit,
    )
    if loss_violation:
        violations.append(loss_violation)
    if loss_warning:
        warnings.append(loss_warning)

    # 9. Wash sale guard (configurable block/warn) — only for new entries
    wash_sale_risk: dict | None = None
    if not is_exit and _config:
        ws_config = constitution.wash_sale_guard
        if ws_config.enabled:
            try:
                from evotrader.tools.tax_lots import check_wash_sale_risk, fetch_tax_lots

                tax_lots = await fetch_tax_lots(ticker, _call_mcp_tool)
                if tax_lots:
                    wash_sale_risk = check_wash_sale_risk(ticker, tax_lots, ws_config)
                    if wash_sale_risk.get("wash_sale_risk"):
                        msg = (
                            f"WASH SALE RISK: {ticker} was sold at a loss "
                            f"{wash_sale_risk['days_since_last_loss_sale']}d ago "
                            f"(disallowed loss: ${wash_sale_risk['disallowed_loss']:.2f}). "
                            f"Re-entering within {ws_config.lookback_days}d triggers IRS wash sale rule."
                        )
                        if ws_config.mode == "block":
                            violations.append(msg)
                        else:
                            warnings.append(f"⚠️ {msg}")
            except Exception as e:
                warnings.append(f"Wash sale check failed (non-fatal): {e}")

    # 10. Level II thin-book warning
    if not is_exit and _config and _config.constitution.level_ii.enabled:
        try:
            from evotrader.tools.level_ii import assess_book_depth, fetch_level_ii

            l2_config = _config.constitution.level_ii
            raw_l2 = await fetch_level_ii(
                ticker,
                _call_mcp_tool,
                depth_levels=l2_config.depth_levels,
            )
            if raw_l2:
                book = assess_book_depth(raw_l2, order_value=order_value, config=l2_config)
                if book.get("thin_book"):
                    warnings.append(
                        f"THIN ORDER BOOK: order ${order_value:.0f} exceeds "
                        f"{l2_config.thin_book_warn_pct * 100:.0f}% of visible depth "
                        f"(bid ${book['bid_depth_usd']:.0f} + ask ${book['ask_depth_usd']:.0f}). "
                        f"Consider limit order to avoid slippage."
                    )
        except Exception as e:
            warnings.append(f"Level II check failed (non-fatal): {e}")

    verdict = "REJECTED" if violations else "APPROVED"
    return {
        "verdict": verdict,
        "violations": violations,
        "warnings": warnings,
        "context": {
            "today_pnl": today_pnl,
            "daily_loss_limit_usd": (
                round(limits.max_daily_loss_pct * account_value, 2) if account_value else None
            ),
            "consecutive_losses": consecutive_losses,
            "trades_today": trade_count,
            "max_trades_per_day": rules.max_trades_per_day,
            "order_value": order_value,
            "cash_balance": cash_balance,
            "buying_power": buying_power,
            "wash_sale_risk": wash_sale_risk,
        },
    }


async def check_option_risk_limits(
    ticker: str,
    option_type: str,
    direction: str,
    contracts: int,
    premium_per_contract: float,
    strike: float,
    expiration: str,
    action: str = "OPEN",
) -> dict:
    """Check a proposed option trade against constitution risk limits.

    Validates the option trade against allowed tickers, option writing
    restrictions, premium limits (percentage-based, scales with portfolio),
    buying power, daily loss limits, and circuit breakers.

    Exit trades (CLOSE, STOP_LOSS, TAKE_PROFIT) are exempt from circuit
    breakers, the daily-loss limit, trade count, premium, and buying
    power checks.

    Args:
        ticker: Underlying stock ticker (e.g., "QQQ").
        option_type: "call" or "put".
        direction: "buy" or "sell" (buy = debit, sell = credit/writing).
        contracts: Number of option contracts.
        premium_per_contract: Premium per contract in USD (per-share price).
        strike: Strike price of the option.
        expiration: Expiration date (YYYY-MM-DD format).
        action: Trade action — "OPEN", "CLOSE", "STOP_LOSS", or "TAKE_PROFIT".
    """
    if not _config or not _journal:
        return {"error": "Risk system not initialised"}

    constitution = _config.constitution
    rules = constitution.trading_rules
    limits = constitution.risk_limits
    breakers = constitution.circuit_breakers

    violations: list[str] = []
    warnings: list[str] = []

    # 1. Options trading enabled check
    if not getattr(rules, "allow_options", True):
        violations.append("Option trading is disabled in constitution")

    # 2. Allowed ticker check (underlying must be in allowed list)
    # ENTRY ONLY, for the same reason as the equity path: an allowlist must
    # never prevent closing a contract on an underlying that was later removed.
    if ticker not in rules.allowed_tickers:
        if is_exit_action(action):
            warnings.append(
                f"Underlying '{ticker}' is not in the allowed list "
                f"{rules.allowed_tickers}, but this is an exit "
                f"({action.upper()}) — permitted so the position can be closed."
            )
        else:
            violations.append(f"Underlying '{ticker}' not in allowed list: {rules.allowed_tickers}")

    # 3. Option writing check — only buying is allowed unless explicitly enabled
    # We must check both "sell" and "short" depending on how the agent phrases it
    is_short_open = direction.upper() in ("SELL", "SHORT") and action.upper() == "OPEN"
    if is_short_open and not getattr(rules, "allow_option_writing", False):
        violations.append(
            "Option writing (selling to open) is disabled in constitution — only buying calls/puts is allowed"
        )

    # 4. Total premium = contracts * premium_per_contract * 100 (each contract = 100 shares)
    total_premium = contracts * premium_per_contract * 100

    # 5. Absolute order value check — entries only, as for shares
    if total_premium > limits.max_order_value_usd and not is_exit_action(action):
        violations.append(
            f"Option premium ${total_premium:.2f} exceeds max order value ${limits.max_order_value_usd:.2f}"
        )

    # 6. Daily loss — evaluated after step 9, which fetches the account value
    #    the limit is a share of.
    today_pnl = await _journal.get_today_pnl()

    # 7. Consecutive losses circuit breaker
    # EXIT TRADES ARE EXEMPT — same rationale as equity: blocking a close
    # traps the agent in a losing position.
    # The breaker only blocks for `pause_duration_minutes` after the last loss;
    # once the cooldown expires, trading resumes even if the streak is unbroken.
    # Session-scoped: the streak resets at each trading session boundary so a
    # Friday streak cannot block Monday morning trading (see review
    # 20260727_204248_circuit_breaker_streak_semantics).
    is_exit = is_exit_action(action)
    consecutive_losses = await _journal.get_session_consecutive_losses()
    if consecutive_losses >= breakers.consecutive_losses_pause and not is_exit:
        last_loss_ts = await _journal.get_last_loss_timestamp()
        pause_expired = last_loss_ts is not None and datetime.now(UTC) - last_loss_ts > timedelta(
            minutes=breakers.pause_duration_minutes
        )
        if not pause_expired:
            violations.append(
                f"Circuit breaker: {consecutive_losses} consecutive losses "
                f"(limit: {breakers.consecutive_losses_pause}), "
                f"pause expires {breakers.pause_duration_minutes}min after last loss"
            )

    # 8. Daily trade count check (exits exempt)
    trade_count = await _journal.get_trade_count_today()
    if trade_count >= rules.max_trades_per_day and not is_exit:
        violations.append(f"Daily trade limit reached: {trade_count}/{rules.max_trades_per_day}")

    # 9. Portfolio-proportional premium check + buying power
    cash_balance: float | None = None
    buying_power: float | None = None
    portfolio_value: float | None = None
    try:
        portfolio = await _agentic_portfolio()
        if portfolio:
            cash_balance = portfolio["cash_balance"]
            portfolio_value = portfolio["total_value"]
            buying_power = portfolio["buying_power"]

            # Check buying power (exits exempt — selling to close returns premium)
            if buying_power is not None and total_premium > buying_power and not is_exit:
                violations.append(
                    f"Option premium ${total_premium:.2f} exceeds buying power ${buying_power:.2f}"
                )

            # Check portfolio-proportional premium limit
            max_prem_pct = getattr(rules, "max_option_premium_pct", 0.05)
            if portfolio_value and portfolio_value > 0:
                max_premium = portfolio_value * max_prem_pct
                # Floor for small portfolios/testing to allow purchasing at least one contract
                if portfolio_value < 1000.0:
                    max_premium = max(max_premium, 100.0)
                if total_premium > max_premium:
                    violations.append(
                        f"Option premium ${total_premium:.2f} exceeds "
                        f"{max_prem_pct:.0%} of portfolio value "
                        f"(${max_premium:.2f})"
                    )
    except Exception as e:
        warnings.append(f"Could not verify portfolio for option risk check: {e}")

    # 9b. Daily loss, as a share of the real account (see _daily_loss_verdict).
    account_value = portfolio_value
    if not account_value and today_pnl < 0:
        account_value = await _recorded_account_value()
    loss_violation, loss_warning = _daily_loss_verdict(
        today_pnl,
        account_value,
        limits.max_daily_loss_pct,
        is_exit=is_exit,
    )
    if loss_violation:
        violations.append(loss_violation)
    if loss_warning:
        warnings.append(loss_warning)

    # 10. Validate expiration is not same-day (too risky)
    # datetime and UTC come from the module imports. A local import here made
    # them local to the whole function, so the circuit breaker above raised
    # UnboundLocalError whenever it had to read the clock.
    try:
        exp_date = datetime.strptime(expiration, "%Y-%m-%d").replace(tzinfo=UTC)
        now = datetime.now(UTC)
        dte = (exp_date - now).days
        if dte <= 0:
            violations.append("Option expires today or has already expired — too risky")
        elif dte <= 2:
            warnings.append(f"Option expires in {dte} day(s) — very high theta decay risk")
    except ValueError:
        warnings.append(f"Could not parse expiration date: {expiration}")

    verdict = "REJECTED" if violations else "APPROVED"
    return {
        "verdict": verdict,
        "violations": violations,
        "warnings": warnings,
        "context": {
            "trade_type": "option",
            "ticker": ticker,
            "option_type": option_type,
            "direction": direction,
            "contracts": contracts,
            "premium_per_contract": premium_per_contract,
            "total_premium": total_premium,
            "strike": strike,
            "expiration": expiration,
            "today_pnl": today_pnl,
            "daily_loss_limit_usd": (
                round(limits.max_daily_loss_pct * account_value, 2) if account_value else None
            ),
            "consecutive_losses": consecutive_losses,
            "trades_today": trade_count,
            "cash_balance": cash_balance,
            "buying_power": buying_power,
            "portfolio_value": portfolio_value,
        },
    }


# ═══════════════════════════════════════════════════════════════════════
# Trade Journal Tools
# ═══════════════════════════════════════════════════════════════════════


def _normalize_regime(raw: object) -> str:
    """Extract a clean regime type string from various LLM formats.

    The LLM may pass regime as:
    - A dict: {"regime": "trending_bull", "confidence": 0.8, ...}
    - A dict: {"type": "trending_bull", ...}
    - A plain string: "trending_bull"
    - None or missing
    """
    if raw is None:
        return "unknown"
    if isinstance(raw, dict):
        return str(raw.get("type") or raw.get("regime") or raw.get("classification") or "unknown")
    return str(raw) if raw else "unknown"


def _compose_broker_reason(
    broker_reason: str | None, resolved_status: str, downgrade_reason: str | None
) -> str | None:
    """The reason string for a journal row, keeping the downgrade marker.

    A rejection reason is only meaningful on a terminal status, but a
    ``STATUS_DOWNGRADED_TO_PENDING`` marker has to survive onto a PENDING row —
    that row is the only place the substitution is recorded, and the next cycle
    reads it to understand why a stop it believes filled reads as resting.
    """
    parts: list[str] = []
    if downgrade_reason:
        parts.append(downgrade_reason)
    if broker_reason and resolved_status in ("REJECTED", "CANCELLED", "FAILED"):
        parts.append(str(broker_reason))
    return "; ".join(parts) or None


async def record_trade(trade_json: str) -> dict:
    """Record a trade decision in the journal.

    Persists the full trade context including signals, reasoning, regime,
    and algorithm version for performance tracking and evolution analysis.

    Args:
        trade_json: JSON string of a TradeProposal object.
    """
    if not _journal:
        return {"error": "Journal not initialised"}

    import json

    from evotrader.models.trade import (
        OrderResult,
        OrderStatus,
        OrderType,
        TradeAction,
        TradeDirection,
        TradeProposal,
    )
    from evotrader.utils import parse_occ_symbol

    try:
        data = json.loads(trade_json)
    except Exception as e:
        logger.error("Failed to parse trade_json as JSON: %s", e)
        return {"error": f"Invalid JSON format: {e}"}

    # Normalize fields from dynamic LLM keys to TradeProposal schema
    ticker = data.get("ticker") or data.get("symbol")
    if not ticker:
        # Fallback to checking keys case-insensitively
        for k, v in data.items():
            if k.lower() in ("ticker", "symbol"):
                ticker = v
                break
        if not ticker:
            return {"error": "Missing ticker or symbol in trade data"}

    action_str = data.get("action")
    direction_str = data.get("direction") or data.get("side")

    action = None
    direction = None

    # Detect if trade data specifies stop loss params
    order_type_raw = str(data.get("order_type") or data.get("type") or "").lower()
    from evotrader.models.trade import STOP_ORDER_TYPES

    has_stop_param = data.get("stop_price") is not None or order_type_raw in STOP_ORDER_TYPES

    # ── Phase 1: Parse explicit action string ────────────────────────
    # Handle compound action strings like "sell_short" that aren't valid
    # TradeAction enum values but carry clear intent.
    if action_str:
        action_upper = str(action_str).upper()
        if action_upper in ("SELL_SHORT", "SHORT_OPEN", "OPEN_SHORT"):
            # Unambiguous: opening a new short position.
            action = TradeAction.OPEN
            direction = TradeDirection.SHORT
        elif action_upper in ("BUY_TO_COVER", "COVER", "CLOSE_SHORT"):
            # Unambiguous: closing an existing short position.
            action = TradeAction.CLOSE
            direction = TradeDirection.SHORT
        elif action_upper in ("SELL", "TRIM", "REDUCE", "PARTIAL_CLOSE"):
            # Reducing actions — map to CLOSE so the FIFO matching path
            # is entered. Phase 3 will resolve direction from open lots.
            # Without this, "SELL" fell through to ValueError and Phase 3
            # fixed it, but "OPEN" with sell intent set action=OPEN and
            # skipped Phase 3 entirely → phantom double-counted lots.
            action = TradeAction.CLOSE
        elif action_upper in ("STOP_LOSS", "STOP_LOSS_SELL", "STOP_SELL", "STOP", "STOPLOSS"):
            action = TradeAction.STOP_LOSS
        elif action_upper in ("TAKE_PROFIT", "TAKE_PROFIT_SELL"):
            action = TradeAction.TAKE_PROFIT
        else:
            try:
                action = TradeAction(action_upper)
            except ValueError:
                pass

    if has_stop_param and action not in (
        TradeAction.OPEN,
        TradeAction.CLOSE,
        TradeAction.TAKE_PROFIT,
    ):
        action = TradeAction.STOP_LOSS

    # ── Phase 2: Parse explicit direction string ─────────────────────
    if direction_str and direction is None:
        dir_upper = str(direction_str).upper()
        if dir_upper == "LONG":
            direction = TradeDirection.LONG
        elif dir_upper == "SHORT":
            direction = TradeDirection.SHORT
        elif dir_upper in ("SELL_SHORT", "SHORT_OPEN", "OPEN_SHORT"):
            direction = TradeDirection.SHORT
            action = action or TradeAction.OPEN
        else:
            try:
                direction = TradeDirection(dir_upper)
            except ValueError:
                pass

    # ── Phase 3: Infer missing action/direction from context ─────────
    # Only runs when Phase 1/2 left gaps. Uses open positions to decide
    # whether a buy/sell is opening or closing.
    if not action or not direction:
        open_trades = await _journal.get_open_trades()
        matching_trades = [t for t in open_trades if t.get("ticker") == ticker]
        # Separate by direction for smarter inference
        open_longs = [t for t in matching_trades if t.get("direction") == "LONG"]
        open_shorts = [t for t in matching_trades if t.get("direction") == "SHORT"]

        side_lower = str(direction_str or data.get("action") or "buy").lower()

        if side_lower in ("sell_short", "short"):
            # Explicit short intent — always open a new short, never close.
            action = action or TradeAction.OPEN
            direction = direction or TradeDirection.SHORT
        elif side_lower in ("buy", "long"):
            if action == TradeAction.STOP_LOSS or has_stop_param:
                # Stop loss on buy side = covering a short
                action = TradeAction.STOP_LOSS
                direction = direction or TradeDirection.SHORT
            elif open_shorts and not open_longs:
                # Buying when only shorts are open → closing the short
                action = action or TradeAction.CLOSE
                direction = direction or TradeDirection.SHORT
            else:
                action = action or TradeAction.OPEN
                direction = direction or TradeDirection.LONG
        elif side_lower in ("sell", "sell_stop", "stop_sell"):
            if action == TradeAction.STOP_LOSS or has_stop_param:
                # Stop loss sell = protecting a long position
                action = TradeAction.STOP_LOSS
                direction = direction or TradeDirection.LONG
            elif open_longs:
                # Selling when longs are open → closing the long
                action = action or TradeAction.CLOSE
                direction = direction or TradeDirection.LONG
            else:
                # No longs to close and not a stop order → opening a new short
                action = action or TradeAction.OPEN
                direction = direction or TradeDirection.SHORT
        else:
            action = action or TradeAction.OPEN
            direction = direction or TradeDirection.LONG

    # Extract quantity
    quantity = data.get("quantity")
    if quantity is not None:
        try:
            quantity = float(quantity)
        except ValueError:
            quantity = None

    # Extract order type
    # parse_order_type understands the broker's `stop_market`; OrderType(...)
    # alone raised on it and the order was silently journaled as MARKET.
    from evotrader.models.trade import parse_order_type

    order_type_str = data.get("order_type") or data.get("type")
    order_type = parse_order_type(order_type_str)

    # Extract prices
    # The stop TRIGGER price, kept separate from limit_price. Without this the
    # proposal carried stop_price=None on every write, so the journal fell back
    # to the related lot's entry price and the real stop level survived only in
    # free-text reasoning — where no agent could verify it.
    stop_price = data.get("stop_price") or data.get("trigger_price") or data.get("stop")
    limit_price = data.get("limit_price") or data.get("price") or data.get("average_price")
    if limit_price is not None:
        try:
            limit_price = float(limit_price)
        except ValueError:
            limit_price = None

    # Retrieve related_trade_id for close actions
    related_trade_id = data.get("related_trade_id")
    if not related_trade_id and action in (
        TradeAction.CLOSE,
        TradeAction.STOP_LOSS,
        TradeAction.TAKE_PROFIT,
    ):
        open_trades = await _journal.get_open_trades()
        matching = [t for t in open_trades if t.get("ticker") == ticker]
        if matching:
            related_trade_id = matching[0].get("id")

    # Extract option details
    option_id = data.get("option_id")
    option_type = data.get("option_type")
    strike = data.get("strike")
    expiration = data.get("expiration")

    # Auto-parse OCC symbol if details are missing
    if option_id and (not option_type or not strike or not expiration):
        parsed = parse_occ_symbol(option_id)
        if parsed:
            option_type = option_type or parsed["option_type"]
            strike = strike or parsed["strike"]
            expiration = expiration or parsed["expiration"]

    # Helper to safely extract floats from strings with LLM commentary
    def _safe_float(val: Any) -> float | None:
        if val is None or val == "":
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            if isinstance(val, str):
                import re

                m = re.search(r"-?\d+\.?\d*", val)
                if m:
                    return float(m.group())
            return None

    # Construct TradeProposal robustly
    try:
        from pydantic import ValidationError

        proposal = TradeProposal(
            ticker=ticker,
            direction=direction,
            action=action,
            quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
            stop_price=_safe_float(stop_price),
            time_in_force=(
                str(data.get("time_in_force")).lower() if data.get("time_in_force") else None
            ),
            hybrid_score=_safe_float(data.get("hybrid_score")),
            confidence=_safe_float(data.get("confidence")),
            algo_signal=_safe_float(data.get("algo_signal")),
            llm_signal=_safe_float(data.get("llm_signal")),
            regime=_normalize_regime(data.get("regime")),
            algo_version=data.get("algo_version") or data.get("algorithm_version"),
            reasoning=data.get("reasoning") or data.get("reason"),
            related_trade_id=related_trade_id,
            option_id=option_id,
            option_type=option_type,
            strike=strike,
            expiration=expiration,
        )
    except ValidationError as e:
        logger.error("TradeProposal validation failed: %s", e)
        return {"error": f"Invalid trade parameters: {e}"}
    except Exception as e:
        logger.error("Error creating TradeProposal: %s", e)
        return {"error": f"Error creating TradeProposal: {e}"}

    # Data-quality guard: surface dropped decision-context fields immediately.
    _missing = [
        k
        for k in ("algo_signal", "hybrid_score", "regime", "algo_version")
        if data.get(k) in (None, "", "unknown")
    ]
    if _missing:
        logger.warning(
            "record_trade: trade_json missing signal provenance fields %s for %s — "
            "executor must copy these from the risk-approved proposal.",
            _missing,
            ticker,
        )

    # Check if there is order execution result info to record as OrderResult
    order_id = data.get("order_id") or data.get("id")
    status_str = data.get("status") or data.get("state")

    # Determine the order lifecycle status for the trades table.
    # All trades are now journaled immediately — PENDING trades are
    # recorded with order_status='PENDING' and excluded from
    # get_open_trades() until they transition to FILLED.
    # ── FILL EVIDENCE, NOT THE LABEL ─────────────────────────────────
    # A status word is a claim; a fill has evidence. On 2026-09-25 the broker
    # answered `state: unconfirmed, cumulative_quantity: "0.000000",
    # executions: []` for a resting stop and the executor called this
    # tool with "status": "FILLED". The journal booked realized P&L against
    # real lots, opened a phantom short, skipped the pending_orders
    # row, and three later cycles read `pending_count: 0` and tried to re-place
    # a stop the broker was already holding.
    #
    # A protective order can never be FILLED at placement: it rests until its
    # trigger is touched. So when the payload claims a fill for an order that is
    # resting by shape and carries no fill evidence, the claim is downgraded to
    # PENDING and the broker is asked later (reconcile_pending_orders).
    _FILL_EVIDENCE_KEYS = (
        "fill_price",
        "average_price",
        "filled_quantity",
        "cumulative_quantity",
    )

    def _has_fill_evidence() -> bool:
        for key in _FILL_EVIDENCE_KEYS:
            raw = data.get(key)
            if raw in (None, "", "None"):
                continue
            try:
                if float(raw) > 0:
                    return True
            except (TypeError, ValueError):
                # A non-numeric value is not evidence of anything.
                continue
        return False

    has_fill_evidence = _has_fill_evidence()
    # Resting BY SHAPE, not by label — the same reasoning as
    # protection_audit._is_stop. A gtc sell limit that is not an entry is a
    # resting take-profit even when the payload calls it CLOSE.
    is_resting_type = (
        action in (TradeAction.STOP_LOSS, TradeAction.TAKE_PROFIT)
        or order_type_raw in STOP_ORDER_TYPES
        or (
            order_type_raw == "limit"
            and str(data.get("time_in_force") or "").lower() == "gtc"
            and action != TradeAction.OPEN
        )
    )

    resolved_order_status = "FILLED"  # default for market orders / fills
    downgrade_reason: str | None = None
    # Where the status came from, so reconciliation can re-ask about a claim.
    # See migration 0023.
    fill_source: str | None = None
    if status_str:
        su = str(status_str).upper()
        if su in ("PENDING", "QUEUED", "UNCONFIRMED", "CONFIRMED", "PARTIALLY_FILLED"):
            resolved_order_status = "PENDING"
        elif su == "REJECTED":
            resolved_order_status = "REJECTED"
        elif su == "CANCELLED":
            resolved_order_status = "CANCELLED"
        elif su == "FAILED":
            resolved_order_status = "FAILED"
        elif su == "FILLED":
            if is_resting_type and not has_fill_evidence:
                resolved_order_status = "PENDING"
                downgrade_reason = (
                    "STATUS_DOWNGRADED_TO_PENDING: executor reported FILLED without fill evidence"
                )
                logger.warning(
                    "record_trade: %s %s (order %s) was reported FILLED with no "
                    "fill evidence (%s all absent or zero). A resting protective "
                    "order cannot fill at placement — journaling PENDING and "
                    "leaving the broker to confirm.",
                    ticker,
                    action.value if action else "?",
                    order_id or "none",
                    ", ".join(_FILL_EVIDENCE_KEYS),
                )
            elif has_fill_evidence:
                fill_source = "broker"
            elif order_id:
                # An OPEN market/limit claimed FILLED with no evidence and a
                # broker id: keep it FILLED (it is usually true, and demoting an
                # entry would hide the position) but mark the claim so
                # reconcile_pending_orders re-asks and the broker can correct it.
                fill_source = "executor_claim"
                logger.warning(
                    "record_trade: %s %s (order %s) reported FILLED without fill "
                    "evidence — recorded as an executor claim for re-check.",
                    ticker,
                    action.value if action else "?",
                    order_id,
                )

    # Also save to pending_orders table as an audit trail for PENDING orders
    if resolved_order_status == "PENDING" and order_id:
        try:
            await _journal.save_pending_order(order_id, json.dumps(data), _current_session_id)
            logger.info("Order %s is pending — saved to pending_orders audit trail.", order_id)
        except Exception as e:
            logger.warning("Failed to save pending_orders audit trail for %s: %s", order_id, e)

    result = None
    if order_id and status_str:
        status_upper = str(status_str).upper()
        order_status = None
        for s in OrderStatus:
            if s.value == status_upper or s.name == status_upper:
                order_status = s
                break
        if not order_status:
            if status_upper == "FILLED":
                order_status = OrderStatus.FILLED
            elif status_upper == "REJECTED":
                order_status = OrderStatus.REJECTED
            elif status_upper == "FAILED":
                order_status = OrderStatus.FAILED
            elif status_upper == "CANCELLED":
                order_status = OrderStatus.CANCELLED
            else:
                order_status = OrderStatus.PENDING

        fill_price = data.get("fill_price") or data.get("average_price")
        if fill_price is not None:
            try:
                fill_price = float(fill_price)
            except ValueError:
                fill_price = None

        filled_quantity = data.get("filled_quantity") or data.get("quantity")
        if filled_quantity is not None:
            try:
                filled_quantity = float(filled_quantity)
            except ValueError:
                filled_quantity = 0.0

        result = OrderResult(
            order_id=str(order_id),
            status=order_status,
            ticker=ticker,
            direction=direction,
            action=action,
            order_type=order_type or OrderType.MARKET,
            requested_quantity=quantity or 0.0,
            filled_quantity=filled_quantity,
            fill_price=fill_price,
            error_message=data.get("reason") or data.get("error_message"),
            option_id=option_id,
            option_type=option_type,
            strike=strike,
            expiration=expiration,
        )

    broker_reason = data.get("reason") or data.get("error_message") or data.get("reject_reason")

    trade_ids = await _journal.record_trade(
        proposal=proposal,
        result=result,
        session_id=_current_session_id,
        order_status=resolved_order_status,
        order_id=str(order_id) if order_id else None,
        broker_status_reason=_compose_broker_reason(
            broker_reason, resolved_order_status, downgrade_reason
        ),
        fill_source=fill_source,
    )

    # ── Populate ChromaDB trade_experiences for semantic recall ──
    if _memory and trade_ids:
        try:
            for tid in trade_ids if isinstance(trade_ids, list) else [trade_ids]:
                trade_row = None
                if _journal and hasattr(_journal, "get_trade_by_id"):
                    import inspect

                    res = _journal.get_trade_by_id(int(tid))
                    trade_row = await res if inspect.isawaitable(res) else res

                pnl = (
                    float(trade_row["realized_pnl"])
                    if (isinstance(trade_row, dict) and trade_row.get("realized_pnl") is not None)
                    else None
                )
                held_s = (
                    int(trade_row["holding_period_s"])
                    if (
                        isinstance(trade_row, dict)
                        and trade_row.get("holding_period_s") is not None
                    )
                    else None
                )
                rel_id = (
                    int(trade_row["related_trade_id"])
                    if (
                        isinstance(trade_row, dict)
                        and trade_row.get("related_trade_id") is not None
                    )
                    else None
                )

                _memory.store_trade_experience(
                    trade_id=int(tid),
                    direction=direction.value if direction else "UNKNOWN",
                    regime=_normalize_regime(data.get("regime")),
                    algo_signal=float(data.get("algo_signal"))
                    if data.get("algo_signal") is not None
                    else 0.0,
                    llm_signal=float(data.get("llm_signal"))
                    if data.get("llm_signal") is not None
                    else None,
                    hybrid_score=float(data.get("hybrid_score"))
                    if data.get("hybrid_score") is not None
                    else 0.0,
                    reasoning=str(data.get("reasoning") or data.get("reason") or ""),
                    outcome_pnl=pnl,
                    holding_period_s=held_s,
                    related_trade_id=rel_id,
                    action=str(data.get("action") or "TRADE").upper(),
                    session_id=_current_session_id,
                    ticker=ticker,
                )
                # If this exit closed a previous entry trade, update the entry trade's outcome in ChromaDB as well
                if rel_id and pnl is not None:
                    _memory.update_trade_outcome(
                        trade_id=rel_id, outcome_pnl=pnl, holding_period_s=held_s
                    )

            logger.info("Stored trade experience(s) %s in ChromaDB", trade_ids)
        except Exception as e:
            logger.warning("Failed to store trade experience in ChromaDB: %s", e)

    # Remove from pending_orders table if this order was previously deferred
    # and is now in a terminal state (filled, rejected, etc.)
    if order_id and resolved_order_status != "PENDING":
        try:
            await _journal.delete_pending_order(order_id)
        except Exception:
            pass  # May not exist in pending_orders — that's fine

    # Invalidate the open-positions cache since positions may have changed.
    global _open_positions_cache
    _open_positions_cache = None

    return {"trade_ids": trade_ids, "status": "recorded", "order_status": resolved_order_status}


async def get_trade_history(limit: int = 20) -> dict:
    """Get recent trade history from the journal.

    Returns the most recent trades with full context for analysis.

    Args:
        limit: Maximum number of trades to return (default 20).
    """
    if not _journal:
        return {"error": "Journal not initialised"}

    trades = await _journal.get_recent_trades(limit=limit)
    return {"trades": trades, "count": len(trades)}


async def get_performance_summary(days: int = 30) -> dict:
    """Get performance summary over the last N days.

    Returns win rate, average win/loss, profit factor, and total P&L.

    Args:
        days: Lookback period in days (default 30).
    """
    if not _journal:
        return {"error": "Journal not initialised"}

    return await _journal.get_performance_summary(days=days)


async def get_open_positions() -> dict:
    """Get currently open positions and pending orders from the trade journal.

    Returns trades that have action='OPEN' and no corresponding closing trade.
    Also returns any pending (unresolved) orders so the agent can monitor them
    and cancel stale orders if needed.

    Uses a short TTL cache (30s) to avoid redundant queries within the same
    trading cycle when multiple agents call this tool in quick succession.
    """
    global _open_positions_cache, _open_positions_cache_ts

    if not _journal:
        return {"error": "Journal not initialised"}

    now = time.monotonic()
    if _open_positions_cache is not None and (now - _open_positions_cache_ts) < _OPEN_POSITIONS_TTL:
        return _open_positions_cache

    positions = await _journal.get_open_trades()

    # Include pending orders so the agent has visibility into outstanding
    # orders that haven't been filled/cancelled yet.
    pending_orders = []
    try:
        raw_pending = await _journal.get_pending_orders()
        for row in raw_pending:
            trade_json = {}
            try:
                trade_json = json.loads(row["trade_json"]) if row.get("trade_json") else {}
            except (json.JSONDecodeError, TypeError):
                pass
            pending_orders.append(
                {
                    "order_id": row["order_id"],
                    "ticker": trade_json.get("ticker", "?"),
                    "action": trade_json.get("action", "?"),
                    "direction": trade_json.get("direction", "?"),
                    "quantity": trade_json.get("quantity", 0),
                    "order_type": trade_json.get("order_type", "?"),
                    "limit_price": trade_json.get("limit_price"),
                    # The trigger level for a resting stop. Its absence used to
                    # force agents to guess: the same single stop was reported at
                    # five different prices across eight cycles because the only
                    # record of it was prose. A null here means genuinely unknown —
                    # treat the position as unverified, not as protected.
                    "stop_price": (
                        trade_json.get("stop_price")
                        or trade_json.get("trigger_price")
                        or trade_json.get("stop")
                    ),
                    # Whether the order survives the close. Its absence made gtc
                    # unverifiable from the book, so the 2026-09-25 10:30 and 11:30
                    # ET cycles cancelled and re-placed a stop that was already
                    # correctly gtc. A null means unknown — not "day".
                    "time_in_force": trade_json.get("time_in_force"),
                    # 'broker_sync' means the broker itself listed this order as
                    # working. Anything else came from our own submission record.
                    "source": trade_json.get("source", "journal"),
                    "broker_state": trade_json.get("state"),
                    "option_id": trade_json.get("option_id"),
                    "created_at": row.get("created_at", "?"),
                }
            )
    except Exception as e:
        logger.warning("Failed to fetch pending orders: %s", e)

    result = {
        "positions": positions,
        "count": len(positions),
        "pending_orders": pending_orders,
        "pending_count": len(pending_orders),
    }
    _open_positions_cache = result
    _open_positions_cache_ts = now
    return result


def _how_a_swept_order_ended(order: dict) -> tuple[str, str]:
    """Journal status and reason for an order the practice broker's sweep ended."""
    tif = str(order.get("time_in_force") or "").lower()
    if tif and tif != "gtc":
        return "EXPIRED", "Day order expired at session close (practice broker)"
    return "CANCELLED", "Cancelled by the practice broker's stale-order sweep"


async def check_and_journal_pending_fills() -> dict:
    """Check if any pending sim broker orders have been filled and journal them.

    This should be called at the start of each trading cycle to reconcile
    the sim broker's order state with the trade journal. When limit orders
    fill between cycles, this function detects the fills and creates the
    corresponding journal entries so get_open_positions reports correctly.

    Also cancels orders the broker would no longer be working: day orders past
    their session's close, and orders with no recorded time_in_force older than
    24 hours. A gtc order stays.
    """
    if not _sim_proxy:
        return {"fills": [], "cancelled": [], "message": "Not in sim mode"}

    sim_broker = _sim_proxy.sim_broker

    # 1. Process pending orders and get any new fills
    async with sim_broker._lock:
        if sim_broker.auto_close_expired_options:
            await sim_broker._auto_close_expired_options()
        filled_orders = await sim_broker._fill_pending_limit_orders()

    # 2. Cancel expired day orders (and untagged orders older than 24h)
    cancelled = await sim_broker.cancel_stale_pending_orders(max_age_hours=24.0)

    # 2b. Catch up with what the sim settled outside this call. It settles
    # resting orders at the start of most of its tool calls (a portfolio or order
    # lookup, the console's refresh), and an agent's cancel goes straight to it,
    # so those fills and cancels never passed through here and the journal kept
    # them PENDING. Ask the sim about every order the journal is still waiting
    # on, the way live reconciliation asks the broker.
    settled_here = {f["order_id"] for f in filled_orders} | {c.get("id") for c in cancelled}
    cancelled_elsewhere: list[str] = []
    if _journal:
        for waiting in await _journal.get_order_ids_awaiting_broker():
            order_id = waiting.get("order_id")
            if not order_id or order_id in settled_here:
                continue
            order = await sim_broker.settled_order(order_id)
            if order is None:
                continue  # not a practice order: nothing the sim can say about it
            if order["status"] == "filled":
                filled_orders.append(order)
            elif order["status"] == "cancelled":
                cancelled_elsewhere.append(order_id)

    # 3. Load pending proposals from DB
    db_pending = {}
    if _journal:
        for row in await _journal.get_pending_orders():
            db_pending[row["order_id"]] = row

    # 4. Journal any fills — update existing PENDING trades instead of creating new ones
    journaled_ids = []
    for fill in filled_orders:
        order_id = fill["order_id"]
        try:
            rows_updated = await _journal.update_order_status(
                order_id=order_id,
                new_status="FILLED",
                fill_price=fill["fill_price"],
                filled_quantity=fill.get("quantity"),
            )
            if rows_updated:
                journaled_ids.append(order_id)
                logger.info("Updated PENDING→FILLED for sim order %s", order_id)
            else:
                # Fallback: no matching trade found (old order before migration).
                # Create a minimal journal entry for backward compat.
                pending_row = db_pending.get(order_id)
                if pending_row:
                    stashed = json.loads(pending_row["trade_json"])
                    stashed["status"] = "filled"
                    stashed["fill_price"] = fill["fill_price"]
                    stashed["filled_quantity"] = fill["quantity"]
                    result = await record_trade(json.dumps(stashed))
                    journaled_ids.append(result.get("trade_ids"))
                    logger.info("Journaled fill (fallback) for order %s", order_id)
                else:
                    minimal_trade = {
                        "ticker": fill["ticker"],
                        "direction": "LONG" if fill["side"] == "buy" else "SHORT",
                        "action": fill.get("position_effect", "open").upper(),
                        "quantity": fill["quantity"],
                        "order_type": "limit",
                        "limit_price": fill["limit_price"],
                        "price": fill["fill_price"],
                        "fill_price": fill["fill_price"],
                        "filled_quantity": fill["quantity"],
                        "order_id": order_id,
                        "status": "filled",
                        "option_id": fill.get("option_id"),
                        "option_type": fill.get("option_type"),
                        "strike": fill.get("strike"),
                        "expiration": fill.get("expiration"),
                        "reasoning": f"Limit order {order_id} filled at {fill['fill_price']}",
                        "confidence": 0.5,
                        "regime": "unknown",
                        "algo_version": "sim_fill",
                    }
                    result = await record_trade(json.dumps(minimal_trade))
                    journaled_ids.append(result.get("trade_ids"))
                    logger.info("Journaled fill (no stash) for order %s", order_id)
        except Exception as e:
            logger.error("Failed to reconcile sim fill for order %s: %s", order_id, e)

        # Resolve in pending_orders audit trail
        if _journal:
            try:
                await _journal.resolve_pending_order(order_id, "FILLED")
            except Exception:
                pass

    # Settle cancelled orders in the journal: the trade row, not only the
    # pending_orders audit trail — a stop left PENDING there counts as
    # protection that no longer exists. A day order the sweep ended EXPIRED, as
    # live reconciliation calls one; anything else was cancelled.
    ended = [(c.get("id"), *_how_a_swept_order_ended(c)) for c in cancelled] + [
        (oid, "CANCELLED", "Cancelled at the practice broker") for oid in cancelled_elsewhere
    ]
    if _journal:
        for cid, status, reason in ended:
            if not cid:
                continue
            try:
                await _journal.update_order_status(
                    order_id=cid, new_status=status, broker_status_reason=reason
                )
            except Exception as e:
                logger.error("Failed to journal the end of sim order %s: %s", cid, e)
            try:
                await _journal.resolve_pending_order(cid, status)
            except Exception:
                pass

    # Invalidate positions cache since state may have changed
    global _open_positions_cache
    _open_positions_cache = None

    return {
        "fills": [
            {"order_id": f["order_id"], "ticker": f["ticker"], "fill_price": f["fill_price"]}
            for f in filled_orders
        ],
        "fills_count": len(filled_orders),
        "cancelled_count": len(cancelled) + len(cancelled_elsewhere),
        "journaled_ids": journaled_ids,
    }


def classify_cancelled_order(order_data: dict, now: datetime | None = None) -> tuple[str, str]:
    """Tell an EXPIRED day order apart from a genuine cancellation.

    A broker order in state ``cancelled`` with NO ``cancel_reason``, created on
    a PRIOR session, is a day order the broker dropped at the close — not a
    decision anyone made. A stop placed on 2026-09-17 with no
    ``time_in_force`` expired overnight, and reconcile stamped it
    "Order cancelled" — indistinguishable from an agent cancel. The next
    morning the strategy agent read "the safety net isn't there" and sold the
    position instead of re-placing the stop, just before a large rally.

    What the order was decides, not only when it was placed. A gtc order does
    not expire at the close, so a cancelled one was cancelled. On 2026-09-28
    a gtc stop the executor cancelled to resize was labelled "Day order
    expired ... (no time_in_force)" because it had been placed days earlier:
    the same false message, on an order that had one.

    For a day order, the time of its last transaction tells the two apart:
    the broker drops it at or after the close, while a cancel happens whenever
    someone sends it. Without that time, the order's age is all there is.

    Returns ``(state, broker_reason)`` where state is ``"expired"`` or
    ``"cancelled"``.
    """
    from datetime import time as time_of_day

    from evotrader.tools.market_hours import ET as _ET
    from evotrader.tools.market_hours import is_trading_day, regular_close

    def _at(value: object) -> datetime | None:
        try:
            t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return t if t.tzinfo else t.replace(tzinfo=UTC)

    reason = order_data.get("cancel_reason")
    if reason:
        return "cancelled", str(reason)
    tif = str(order_data.get("time_in_force") or "").lower()
    if tif == "gtc":
        return "cancelled", "gtc order cancelled at the broker (a gtc order does not expire)"
    created = _at(order_data.get("created_at")) if order_data.get("created_at") else None
    if created is None:
        return "cancelled", "Order cancelled"
    expired = "Day order expired at session close" + (
        "" if tif else " (no time_in_force — protective orders must be gtc)"
    )

    # When the order's day ends: the close of the day it was placed, or of the
    # next trading day for an order placed after the close or on a holiday.
    day = created.astimezone(_ET).date()
    if not (is_trading_day(day) and created.astimezone(_ET).time() < regular_close(day)):
        day += timedelta(days=1)
        while not is_trading_day(day):
            day += timedelta(days=1)
    session_end = datetime.combine(day, regular_close(day), tzinfo=_ET)

    last = order_data.get("last_transaction_at")
    last_at = _at(last) if last else None
    if last_at is not None:
        t = last_at.astimezone(_ET)
        in_session = is_trading_day(t.date()) and (
            time_of_day(9, 30) <= t.time() < regular_close(t.date())
        )
        if last_at >= session_end and not in_session:
            return "expired", expired
        return "cancelled", "Order cancelled"
    if (now or datetime.now(UTC)) >= session_end:
        return "expired", expired
    return "cancelled", "Order cancelled"


#: Broker order states that mean "this order is still working".
_BROKER_OPEN_STATES = frozenset(
    {"unconfirmed", "confirmed", "queued", "partially_filled", "new", "pending"}
)


def _pending_row_from_broker_order(order: dict, ticker: str) -> dict:
    """Describe a broker-open order the way ``pending_orders`` stores one.

    The order book the agents read was journal-only until 2026-09-25, and the
    journal can be wrong (see ``_compose_broker_reason``). When it was, three
    consecutive cycles saw ``pending_count: 0`` against a position the broker had
    fully reserved, proposed the same repair, and were refused each time. The
    broker's own list of working orders cannot make that mistake, so it becomes
    the source and the journal row is the thing that gets corrected.

    The action is inferred from the order's SHAPE, because the broker does not
    carry our vocabulary: a sell with a stop trigger is protection, a plain sell
    limit is a take-profit, anything else is an entry.
    """
    side = str(order.get("side") or "").lower()
    trigger = str(order.get("trigger") or "").lower()
    order_type = str(order.get("type") or order.get("order_type") or "").lower()
    stop_price = order.get("stop_price") or order.get("trigger_price")

    if side == "sell" and (trigger == "stop" or stop_price):
        action = "STOP_LOSS"
    elif side == "sell":
        action = "TAKE_PROFIT"
    else:
        action = "OPEN"

    return {
        "ticker": ticker,
        "action": action,
        "direction": "LONG",
        "quantity": _to_float(order.get("quantity")),
        "order_type": order_type or ("stop_market" if stop_price else "limit"),
        "limit_price": _to_float(order.get("price")),
        "stop_price": _to_float(stop_price),
        "time_in_force": order.get("time_in_force"),
        "order_id": order.get("id") or order.get("order_id"),
        "state": order.get("state"),
        "source": "broker_sync",
    }


def _to_float(value: object) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


async def sync_open_orders_from_broker(account_number: str) -> dict:
    """Make ``pending_orders`` reflect the broker's working orders.

    Runs once per cycle in live mode, before reconciliation reads the list.
    Every order the broker is still working gets a row, whether or not the
    journal knows about it; a row the broker no longer lists is left for
    ``reconcile_pending_orders`` to resolve against the broker's answer, which
    is the only thing allowed to declare it terminal.

    Observes and records only. It places nothing and cancels nothing.
    """
    if not _journal:
        return {"synced": 0, "message": "Journal not initialised"}

    from evotrader.tools.asset_context import allowed_tickers

    synced: list[str] = []
    errors: list[str] = []
    for ticker in allowed_tickers():
        try:
            raw = await _call_mcp_tool(
                "get_equity_orders",
                {
                    "account_number": account_number,
                    "symbol": ticker,
                },
            )
        except Exception as e:
            errors.append(f"{ticker}: {e}")
            logger.warning("[ORDERBOOK] Could not list %s orders: %s", ticker, e)
            continue
        if not raw:
            continue
        data = raw.get("data", {}) or {}
        orders = data.get("orders") or data.get("results") or []
        for order in orders:
            if str(order.get("state") or "").lower() not in _BROKER_OPEN_STATES:
                continue
            order_id = order.get("id") or order.get("order_id")
            if not order_id:
                continue
            row = _pending_row_from_broker_order(order, ticker)
            try:
                await _journal.save_pending_order(
                    str(order_id), json.dumps(row), _current_session_id
                )
                synced.append(str(order_id))
            except Exception as e:
                errors.append(f"{order_id}: {e}")
                logger.warning("[ORDERBOOK] Could not record broker order %s: %s", order_id, e)

    if synced:
        logger.info(
            "[ORDERBOOK] %d working broker order(s) synced into pending_orders: %s",
            len(synced),
            ", ".join(synced),
        )
        global _open_positions_cache
        _open_positions_cache = None
    return {"synced": len(synced), "order_ids": synced, "errors": errors if errors else None}


async def reconcile_pending_orders() -> dict:
    """Reconcile pending live orders against Robinhood's actual order state.

    Reads pending orders from the ``pending_orders`` database table (persisted
    across restarts) and checks each order's status via the Robinhood MCP
    ``get_equity_orders`` / ``get_option_orders`` tools. If the order has
    reached a terminal state (filled, rejected, cancelled, failed), updates
    the existing trade in the ``trades`` table (which was journaled at
    submission time with order_status='PENDING') and marks the pending_orders
    row resolved.

    Should be called at the start of each trading cycle.
    """
    if not _journal:
        return {"reconciled": [], "pending_count": 0, "message": "Journal not initialised"}

    # In sim mode, defer to check_and_journal_pending_fills instead
    if _sim_proxy is not None:
        return {
            "reconciled": [],
            "pending_count": 0,
            "message": "Sim mode — use check_and_journal_pending_fills",
        }

    # Fetch account number for querying orders — must use the agentic account
    account_number = None
    try:
        account_number = await _agentic_account_number()
    except Exception as e:
        logger.warning("[RECONCILE] Failed to fetch account number: %s", e)

    if not account_number:
        return {"reconciled": [], "pending_count": 0, "message": "Could not fetch account number"}

    # Ask the broker what it is working FIRST, so an order the journal never
    # recorded — or recorded as filled by mistake — is on the list below and in
    # the order book the agents read. Before this, the list was journal-only and
    # a journal that was wrong could not be corrected by it.
    order_book = await sync_open_orders_from_broker(account_number)

    # Every order the journal believes is live — the watch list (now including
    # the broker's working orders), any PENDING journal row with a broker id,
    # and any recent row marked FILLED on the executor's word alone.
    pending_rows = await _journal.get_order_ids_awaiting_broker()
    if not pending_rows:
        return {
            "reconciled": [],
            "pending_count": 0,
            "order_book": order_book,
            "message": "No pending orders to reconcile",
        }

    reconciled: list[dict] = []
    errors: list[str] = []

    for row in pending_rows:
        order_id = row["order_id"]

        try:
            # Direct lookup by order_id — Robinhood supports this natively.
            # Try both equity and option order endpoints since we don't
            # always know the asset type from the pending_orders table.
            order_data = None
            for tool_name in ("get_option_orders", "get_equity_orders"):
                try:
                    raw = await _call_mcp_tool(
                        tool_name,
                        {
                            "account_number": account_number,
                            "order_id": order_id,
                        },
                    )
                    if raw:
                        orders = raw.get("data", {}).get("orders", [])
                        if not orders:
                            orders = raw.get("data", {}).get("results", [])
                        if orders:
                            order_data = orders[0]
                            break
                except Exception as e:
                    logger.debug("[RECONCILE] %s lookup failed for %s: %s", tool_name, order_id, e)

            if not order_data:
                # Order not found on Robinhood at all — it was either never
                # actually placed, rejected at submission, or already purged
                # after cancellation. Mark as CANCELLED.
                logger.warning(
                    "[RECONCILE] Order %s not found on Robinhood — marking as CANCELLED",
                    order_id,
                )
                try:
                    rows_updated = await _journal.update_order_status(
                        order_id=order_id,
                        new_status="CANCELLED",
                        broker_status_reason="Order not found on broker — cancelled or never placed",
                    )
                    reconciled.append(
                        {
                            "order_id": order_id,
                            "state": "cancelled",
                            "rows_updated": rows_updated,
                            "reason": "not_found_on_broker",
                        }
                    )
                    await _journal.resolve_pending_order(order_id, "CANCELLED")
                except Exception as e:
                    errors.append(f"Failed to mark missing order {order_id} as CANCELLED: {e}")
                    logger.error("[RECONCILE] Failed to mark missing order %s: %s", order_id, e)
                continue

            state = str(order_data.get("state", "") or "").lower()

            # Only act on terminal states
            if state in ("filled", "rejected", "cancelled", "canceled", "failed"):
                # Normalise "canceled" → "cancelled"
                if state == "canceled":
                    state = "cancelled"

                fill_price = None
                filled_qty = None
                broker_reason = None

                if state == "filled":
                    fp = order_data.get("average_price") or order_data.get("price")
                    fq = order_data.get("cumulative_quantity") or order_data.get("quantity")
                    if fp:
                        fill_price = float(fp)
                    if fq:
                        filled_qty = float(fq)
                elif state == "rejected":
                    broker_reason = (
                        order_data.get("reject_reason")
                        or order_data.get("last_trail_price_reject_reason")
                        or "Order rejected by broker"
                    )
                    logger.warning(
                        "[RECONCILE] Order %s was REJECTED: %s",
                        order_id,
                        broker_reason,
                    )
                elif state == "cancelled":
                    state, broker_reason = classify_cancelled_order(order_data)

                # Update the existing trade in the trades table
                try:
                    rows_updated = await _journal.update_order_status(
                        order_id=order_id,
                        new_status=state.upper(),
                        fill_price=fill_price,
                        filled_quantity=filled_qty,
                        broker_status_reason=broker_reason,
                    )
                    reconciled.append(
                        {
                            "order_id": order_id,
                            "state": state,
                            "rows_updated": rows_updated,
                        }
                    )
                    logger.info(
                        "[RECONCILE] Updated order %s to '%s' (rows=%d)",
                        order_id,
                        state,
                        rows_updated,
                    )
                except Exception as e:
                    errors.append(f"Failed to update order {order_id}: {e}")
                    logger.error(
                        "[RECONCILE] Failed to update order %s: %s",
                        order_id,
                        e,
                    )

                # Mark resolved in pending_orders audit trail
                try:
                    await _journal.resolve_pending_order(order_id, state.upper())
                except Exception:
                    pass

            # Non-terminal states (queued, unconfirmed, confirmed,
            # partially_filled) remain in pending_orders for the next pass —
            # UNLESS the journal claims the order filled. The broker saying it
            # is still working is proof the claim was wrong, and it is the only
            # correction the system can make on its own.
            elif state in _BROKER_OPEN_STATES:
                try:
                    rows_updated = await _journal.update_order_status(
                        order_id=order_id,
                        new_status="PENDING",
                        broker_status_reason=(
                            "STATUS_DOWNGRADED_TO_PENDING: broker reports "
                            f"'{state}' — the claimed fill did not happen"
                        ),
                        only_if_claimed_fill=True,
                    )
                    if rows_updated:
                        reconciled.append(
                            {
                                "order_id": order_id,
                                "state": "downgraded_to_pending",
                                "rows_updated": rows_updated,
                                "reason": f"broker reports {state}",
                            }
                        )
                        logger.warning(
                            "[RECONCILE] Order %s was journaled FILLED but the "
                            "broker reports '%s' — %d row(s) downgraded to "
                            "PENDING and their P&L cleared.",
                            order_id,
                            state,
                            rows_updated,
                        )
                except Exception as e:
                    errors.append(f"Failed to downgrade claimed fill {order_id}: {e}")
                    logger.error(
                        "[RECONCILE] Failed to downgrade claimed fill %s: %s",
                        order_id,
                        e,
                    )

        except Exception as e:
            errors.append(f"Error checking order {order_id}: {e}")
            logger.error("[RECONCILE] Error checking order %s: %s", order_id, e)

    # Invalidate positions cache since state may have changed
    if reconciled:
        global _open_positions_cache
        _open_positions_cache = None

    still_pending = len(pending_rows) - len(reconciled)
    return {
        "reconciled": reconciled,
        "reconciled_count": len(reconciled),
        "still_pending_count": max(0, still_pending),
        "order_book": order_book,
        "errors": errors if errors else None,
    }


# ═══════════════════════════════════════════════════════════════════════
# Algorithm Registry Tools
# ═══════════════════════════════════════════════════════════════════════


def get_active_algorithm() -> dict:
    """Get the currently active algorithm version and its parameters.

    Returns the version identifier, metadata, and all tunable parameters.
    """
    if not _algo_registry:
        return {"error": "Algorithm registry not initialised"}

    version = _algo_registry.get_active_version()
    params = _algo_registry.load_parameters(version)
    metadata = _algo_registry.load_metadata(version)
    return {"version": version, "metadata": metadata, "parameters": params}


def list_algorithm_versions() -> dict:
    """List all available algorithm versions with metadata."""
    if not _algo_registry:
        return {"error": "Algorithm registry not initialised"}

    return {"versions": _algo_registry.list_versions()}


# ═══════════════════════════════════════════════════════════════════════
# Market Hours Tools
# ═══════════════════════════════════════════════════════════════════════


async def get_market_status() -> dict:
    """Get the current market session status.

    Returns the current session type (pre_market, regular, after_hours,
    overnight, closed), appropriate polling interval, and time until next session.

    Checks Robinhood's tradability endpoint for accuracy (e.g. market halts,
    early closes). Falls back to local calendar logic if the MCP call fails.
    """
    from evotrader.tools.market_hours import (
        MarketSession,
        get_allowed_order_types,
        get_current_session,
        get_trading_interval,
        is_twenty_four_hour_eligible,
        seconds_until_next_session,
    )

    allow_ext = False
    overnight_interval = 3600
    ticker = primary_ticker()

    if _config:
        rules = _config.constitution.trading_rules
        allow_ext = getattr(rules, "allow_extended_hours", False)
        if _config.settings:
            overnight_interval = getattr(
                _config.settings.schedule, "overnight_interval_seconds", 3600
            )
            ticker = _config.settings.asset.primary_ticker or ticker

    # Local calendar-based session detection (fallback)
    local_session = get_current_session(twenty_four_hour=allow_ext)
    interval = get_trading_interval(
        twenty_four_hour=allow_ext,
        overnight_interval=overnight_interval,
    )
    next_session = seconds_until_next_session(twenty_four_hour=allow_ext)
    is_24h_eligible = is_twenty_four_hour_eligible(ticker)

    # ── Robinhood tradability check (blocking, fallback on failure) ──
    session = local_session
    can_trade = session != MarketSession.CLOSED
    broker_tradability: dict | None = None
    broker_source = False

    try:
        account_number = await _agentic_account_number() or ""

        raw = await _call_mcp_tool(
            "get_equity_tradability", {"account_number": account_number, "symbols": [ticker]}
        )
        if raw:
            broker_tradability = raw
            # Extract tradability status from the response
            data = raw.get("data", raw)
            item = data[0] if isinstance(data, list) and len(data) > 0 else data

            is_tradable = item.get("is_tradable", item.get("tradable"))

            if is_tradable is not None:
                can_trade = bool(is_tradable)
                broker_source = True
                # If broker says not tradable but our calendar says open,
                # trust the broker (could be a halt or early close)
                if not can_trade and local_session != MarketSession.CLOSED:
                    session = MarketSession.CLOSED
                    logger.warning(
                        "Broker reports %s is NOT tradable despite local session=%s. "
                        "Overriding to CLOSED.",
                        ticker,
                        local_session.value,
                    )
                # If broker says tradable but our calendar says closed,
                # trust the broker (could be a special session)
                elif can_trade and local_session == MarketSession.CLOSED:
                    logger.info(
                        "Broker reports %s IS tradable despite local session=CLOSED. "
                        "Overriding can_trade=True.",
                        ticker,
                    )
    except Exception as e:
        logger.warning("Robinhood tradability check failed, using local calendar: %s", e)

    allowed_types = get_allowed_order_types(ticker, session)

    notes = ""
    if session == MarketSession.OVERNIGHT:
        notes = f"Overnight trading active for 24-hour eligible tickers like {ticker}."
    elif session == MarketSession.CLOSED:
        notes = "Market is closed. No trades permitted."
    elif session == MarketSession.REGULAR:
        notes = "Regular trading hours active. All order types allowed."
    else:
        notes = "Extended trading hours active. Limit orders only."

    result = {
        "session": session.value,
        "polling_interval_seconds": interval,
        "seconds_until_next_session": next_session,
        "can_trade": can_trade,
        "allowed_order_types": allowed_types,
        "twenty_four_hour_eligible": is_24h_eligible,
        "notes": notes,
        "source": "broker" if broker_source else "local_calendar",
        "execution_schedule": getattr(
            _config.settings.schedule, "description", "Manual trigger only (on-demand via UI)"
        )
        if _config and _config.settings
        else "Manual trigger only (on-demand via UI)",
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if broker_tradability:
        result["broker_tradability"] = broker_tradability
    return result


# ═══════════════════════════════════════════════════════════════════════
# Memory & Notes Tools
# ═══════════════════════════════════════════════════════════════════════


def query_past_trades(
    query: str,
    n_results: int = 5,
    regime: str = "",
    ticker: str = "",
    max_age_days: int = 60,
) -> dict:
    """Search past trade experiences for similar situations.

    Uses semantic search to find past trades with similar conditions,
    signals, or reasoning. Useful for learning from history.

    Results are scoped to the instrument you are trading and to recent
    history by default. A different instrument is a different market — an
    index ETF with a 1% ATR and a single stock with a 6.5% ATR do not share a
    base rate — and old experiences describe parameters and instructions that
    no longer exist. Treat anything from another ticker as context, never as
    a record.

    Args:
        query: Natural language description of the current situation
            (e.g., "RSI oversold at 25 with bullish news divergence").
        n_results: Maximum number of results to return (default 5).
        regime: Optional filter by regime type (e.g., "range_bound").
        ticker: Instrument to search. Defaults to the configured primary
            ticker. Pass "*" to search every instrument.
        max_age_days: Only experiences newer than this (default 60). Pass 0
            for no limit.
    """
    if not _memory:
        return {"error": "Memory not initialised"}

    scope_ticker: str | None
    if ticker == "*":
        scope_ticker = None
    elif ticker:
        scope_ticker = ticker.upper()
    else:
        scope_ticker = _primary_ticker() or None

    results = _memory.query_similar_trades(
        query=query,
        n_results=n_results,
        regime=regime if regime else None,
        ticker=scope_ticker,
        max_age_days=max_age_days if max_age_days and max_age_days > 0 else None,
    )
    return {
        "results": results,
        "count": len(results),
        "scope": {"ticker": scope_ticker or "all", "max_age_days": max_age_days or None},
    }


def _primary_ticker() -> str:
    """The instrument this system is configured to trade, or '' if unknown."""
    try:
        return str(_config.settings.asset.primary_ticker or "").upper()
    except Exception:
        return ""


def query_user_notes(
    query: str,
    n_results: int = 5,
    category: str = "",
) -> dict:
    """Search user-provided notes and instructions for relevant guidance.

    The user can add notes by editing markdown files in the ``data/notes/``
    directory. This tool searches those notes semantically to find
    relevant instructions, observations, or strategy hints.

    Categories: 'instruction', 'observation', 'market_insight',
    'strategy_hint', 'general'.

    Human-written notes (files, the UI, the CLI) are always searched. Notes
    written by an agent (`store_learning`) are limited to the last 30 days:
    they describe the tape on the day they were written, and hundreds of them
    accumulated on the previous instrument.

    Args:
        query: What you're looking for (e.g., "should I trade during
            earnings season" or "user preferences for position sizing").
        n_results: Maximum results (default 5).
        category: Optional filter by category.
    """
    if not _memory:
        return {"error": "Memory not initialised"}

    results = _memory.query_notes(
        query=query,
        n_results=n_results,
        category=category if category else None,
        agent_note_max_age_days=30,
    )
    return {"results": results, "count": len(results)}


def store_learning(text: str, category: str = "observation") -> dict:
    """Store a new learning or observation in semantic memory.

    Agents can use this to record insights they discover during trading
    so they can be recalled later. For example, after a successful trade:
    "RSI at 28 with volume spike 3x average led to a +0.8% reversal
    trade in range-bound regime."

    Learnings are stored separately from user-provided notes (tagged with
    source='agent_learning') so they don't compete with human instructions
    in semantic search results.

    Args:
        text: The learning to store (natural language).
        category: Type of learning ('observation', 'market_insight',
            'strategy_hint', 'pattern').
    """
    if not _memory:
        return {"error": "Memory not initialised"}

    import hashlib

    note_id = f"agent_{hashlib.md5(text.encode()).hexdigest()[:12]}"
    _memory.store_note(
        note_id=note_id,
        text=text,
        source="agent_learning",
        category=category,
    )
    return {"note_id": note_id, "status": "stored"}


def update_trading_handoff(
    notes: str, cycle: str = "", position: str = "", thesis: str = ""
) -> dict:
    """Leave a SHORT note for the next trading cycle. Replaces the previous one.

    Every cycle starts from a blank session, so nothing carries over unless you
    write it here. This note is prepended to the next cycle's prompt
    automatically — unlike `store_learning`, it does not have to be found.

    KEEP IT UNDER ~1000 CHARACTERS. Longer notes are truncated. The next agent
    reads this BEFORE its own fresh evidence, so length is not free: a long
    note crowds out and pre-frames its thinking. You are writing a handover
    line, not a diary of this cycle.

    Include ONLY two things:

    1. What to watch for next cycle — the specific condition that would change
       the decision, stated so it can be checked ("if VWAP is reclaimed while
       the MA stack holds, that is the entry").
    2. What you could not finish — an order that was refused, protection still
       to place, data that was missing.

    Leave everything else out. Do NOT restate prices, indicators, signal
    readings, or the reasoning behind a decision you already made — all of that
    arrives fresh next cycle, and repeating it here just argues with newer
    evidence. If nothing is pending and nothing is being watched, say so in one
    line.

    These are notes, not orders. The next cycle re-reads broker truth and
    decides for itself.

    Use `store_learning` instead for a durable, generalisable lesson; that one
    is kept for months and recalled by similarity. This one is operational and
    lives exactly one cycle.

    Args:
        notes: The body, a few lines at most: what to watch for, what is
            unfinished. Not a summary of this cycle.
        cycle: This cycle's position in the day, e.g. "4/9", if you know it.
        position: One line of current position state, e.g.
            "10 XYZ @50.00, stop 46.00 gtc resting, no take-profit".
        thesis: One line on the live thesis and what invalidates it.
    """
    if _config is None:
        return {"status": "error", "error": "config not bound"}

    from evotrader.tools.trading_handoff import write_handoff

    parts = []
    if position:
        parts.append(f"POSITION: {position.strip()}")
    if thesis:
        parts.append(f"THESIS: {thesis.strip()}")
    if notes:
        parts.append(notes.strip())
    body = "\n\n".join(parts)
    if not body.strip():
        return {"status": "error", "error": "refusing to write an empty handoff"}

    try:
        path = write_handoff(
            _config.data_dir,
            body,
            agent="strategy",
            cycle=cycle,
            session_id=_current_session_id or "",
        )
    except Exception as e:
        logger.error("update_trading_handoff failed: %s", e)
        return {"status": "error", "error": str(e)}
    return {
        "status": "ok",
        "path": str(path),
        "note": "Replaced the previous handoff. The next cycle sees this verbatim.",
    }


def get_memory_stats() -> dict:
    """Get statistics about the semantic memory collections.

    Returns the number of stored trade experiences, market patterns,
    and user notes.
    """
    if not _memory:
        return {"error": "Memory not initialised"}

    return _memory.stats()


# ═══════════════════════════════════════════════════════════════════════
# Broker Intelligence Tools (new Robinhood MCP capabilities)
# ═══════════════════════════════════════════════════════════════════════


async def reconcile_broker_pnl() -> dict:
    """Cross-reference journal P&L with broker's realized P&L.

    Fetches Robinhood's official realized P&L via the ``get_realized_pnl``
    MCP tool and compares it with the agent's internal trade journal.
    Reports discrepancies > $0.50 as warnings.

    Should be called at session start to catch journal drift early.
    """
    if not _journal:
        return {"error": "Journal not initialised"}

    try:
        from evotrader.tools.pnl_reconciliation import reconcile_pnl

        # Get our journal's total realized P&L
        # (TradeJournal exposes realized P&L via get_pnl(); the old
        # get_total_pnl() call raised AttributeError, silently disabling
        # session-start reconciliation.)
        journal_total = await _journal.get_pnl(period="all")

        # Get account number for the reconciliation
        account_number: str | None = None
        try:
            account_number = await _agentic_account_number()
        except Exception:
            pass

        return await reconcile_pnl(
            journal_pnl=journal_total,
            call_mcp_tool=_call_mcp_tool,
            account_number=account_number,
        )
    except Exception as e:
        logger.error("P&L reconciliation failed: %s", e, exc_info=True)
        return {"reconciled": True, "warnings": [f"Reconciliation failed: {e}"]}


async def assess_order_book(ticker: str) -> dict:
    """Fetch Level II order book depth for a ticker.

    Returns bid/ask depth, spread, imbalance ratio, and thin-book flag.
    Use this before placing orders to assess execution quality and
    slippage risk.

    Args:
        ticker: Stock ticker symbol (e.g., "QQQ").
    """
    if not _config:
        return {"available": False, "error": "Config not initialised"}

    l2_config = _config.constitution.level_ii
    if not l2_config.enabled:
        return {"available": False, "reason": "Level II disabled in constitution"}

    try:
        from evotrader.tools.level_ii import assess_book_depth, fetch_level_ii

        raw_l2 = await fetch_level_ii(
            ticker,
            _call_mcp_tool,
            depth_levels=l2_config.depth_levels,
        )
        if not raw_l2:
            return {"available": False, "reason": "MCP tool returned empty response"}

        return assess_book_depth(raw_l2, config=l2_config)
    except Exception as e:
        logger.error("Level II assessment failed: %s", e, exc_info=True)
        return {"available": False, "error": str(e)}


async def get_earnings_history(ticker: str) -> dict:
    """Fetch recent earnings results for a ticker or its largest constituents.

    A fund has no earnings of its own, so for one this returns results for its
    largest holdings instead — resolved from live holdings data, so it follows
    whatever ticker is configured rather than assuming a particular ETF.
    Returns EPS actual, estimate, and surprise data for the
    ``event_window_timing`` strategy.

    Args:
        ticker: Stock ticker symbol (e.g., "QQQ", "SPY", "AAPL").
    """
    try:
        raw = await _call_mcp_tool("get_earnings_results", {"symbol": ticker})
        if raw:
            return {
                "ticker": ticker,
                "results": raw.get("data", {}).get("results", []),
                "available": True,
            }

        # No direct results. If this ticker is a fund, fetch its holdings'
        # earnings instead. Whether it is a fund comes from the data, not from a
        # hardcoded ETF list.
        resolver = get_resolver()
        if resolver is None:
            return {"ticker": ticker, "results": [], "available": False}

        universe = await resolver.resolve(ticker)
        if not universe.is_fund:
            return {"ticker": ticker, "results": [], "available": False}

        # Weight-ordered and deterministic, unlike the previous `list(set)[:5]`,
        # which sliced a set and so varied between processes.
        constituents = [
            symbol
            for symbol in universe.select(resolver.top_n, resolver.min_weight_pct)
            if symbol != ticker.upper()
        ][:_EARNINGS_HISTORY_MAX_CONSTITUENTS]

        constituent_results = []
        for constituent in constituents:
            raw_c = await _call_mcp_tool(
                "get_earnings_results",
                {"symbol": constituent},
            )
            if raw_c:
                results = raw_c.get("data", {}).get("results", [])
                if results:
                    constituent_results.append(
                        {
                            "ticker": constituent,
                            "results": results[:3],  # Last 3 quarters
                        }
                    )
        return {
            "ticker": ticker,
            "type": "etf_constituents",
            "constituents": constituent_results,
            "coverage": round(universe.coverage, 4),
            "available": len(constituent_results) > 0,
        }
    except Exception as e:
        logger.error("Earnings history fetch failed: %s", e, exc_info=True)
        return {"ticker": ticker, "results": [], "available": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def _safe_float(value: Any) -> float | None:
    """Convert a pandas/numpy value to a Python float, handling NaN."""
    import math

    if value is None:
        return None
    f = float(value)
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, 6)
