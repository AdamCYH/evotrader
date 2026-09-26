"""Evolution tools — ADK tools for the self-evolving Evolution Agent.

These tools give the Evolution Agent the ability to:
1. Analyse performance deeply
2. Read and review source code
3. Propose algorithm parameter changes
4. Generate new strategy code (validated + sandboxed)
5. Modify agent instructions (versioned + diff-tracked)
6. Review infrastructure code (proposals saved for human review)
7. Evolve the knowledge base

All mutations are gated through validation, rate limits, and
the constitution's safety rules.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from evotrader.evolution.event_summariser import summarise_events
from evotrader.utils import safe_parse_json

logger = logging.getLogger(__name__)

# Module-level references set during initialisation
_analyser = None
_code_evolver = None
_instruction_evolver = None
_algo_registry = None
_memory = None
_thought_logger = None
_config = None
_proposal_manager = None
_evolution_store = None
_attribution_store = None


def bind_evolution_dependencies(
    analyser: Any,
    code_evolver: Any,
    instruction_evolver: Any,
    algo_registry: Any,
    memory: Any,
    config: Any,
    thought_logger: Any = None,
    proposal_manager: Any = None,
    evolution_store: Any = None,
    attribution_store: Any = None,
) -> None:
    """Bind evolution-specific dependencies."""
    global _analyser, _code_evolver, _instruction_evolver
    global _algo_registry, _memory, _thought_logger, _config, _proposal_manager, _evolution_store
    global _attribution_store
    _analyser = analyser
    _code_evolver = code_evolver
    _instruction_evolver = instruction_evolver
    _algo_registry = algo_registry
    _memory = memory
    _thought_logger = thought_logger
    _config = config
    _proposal_manager = proposal_manager
    _evolution_store = evolution_store
    _attribution_store = attribution_store
    logger.info(
        "Evolution tools: dependencies bound (thought_logger=%s, proposal_manager=%s, evolution_store=%s)",
        "yes" if thought_logger else "no",
        "yes" if proposal_manager else "no",
        "yes" if evolution_store else "no",
    )


def _is_auto_promote_enabled() -> bool:
    """Helper to check if auto-promotion is enabled, supporting mocks in unit tests."""
    if not _config:
        return False
    from unittest.mock import Mock

    if isinstance(_config, Mock):
        if hasattr(_config, "evolution") and not isinstance(_config.evolution.auto_promote, Mock):
            return bool(_config.evolution.auto_promote)
        if (
            hasattr(_config, "settings")
            and hasattr(_config.settings, "evolution")
            and not isinstance(_config.settings.evolution.auto_promote, Mock)
        ):
            return bool(_config.settings.evolution.auto_promote)
        return False
    return bool(
        getattr(
            getattr(getattr(_config, "settings", None), "evolution", None), "auto_promote", False
        )
    )


def _get_max_instruction_changes_per_week() -> int | None:
    """Read max_instruction_changes_per_week from settings, supporting mocks."""
    if not _config:
        return None
    from unittest.mock import Mock

    if isinstance(_config, Mock):
        if hasattr(_config, "settings") and hasattr(_config.settings, "evolution"):
            val = getattr(_config.settings.evolution, "max_instruction_changes_per_week", None)
            if val is not None and not isinstance(val, Mock):
                return int(val)
        return None
    val = getattr(
        getattr(getattr(_config, "settings", None), "evolution", None),
        "max_instruction_changes_per_week",
        None,
    )
    return int(val) if val is not None else None


# ═══════════════════════════════════════════════════════════════════════
# Performance Analysis Tools
# ═══════════════════════════════════════════════════════════════════════


async def analyse_performance(lookback_days: int = 30) -> dict:
    """Run a comprehensive performance analysis.

    Analyses trading results across multiple dimensions:
    - Overall metrics (win rate, profit factor, Sharpe, etc.)
    - Regime breakdown (performance per market regime)
    - Signal attribution (algo vs. LLM dominance)
    - Win/loss streak analysis
    - Actionable recommendations

    Args:
        lookback_days: How many days of history to analyse (default 30).
    """
    if not _analyser:
        return {"error": "Performance analyser not initialised"}

    return await _analyser.full_analysis(lookback_days=lookback_days)


def _known_channel_names() -> list[str]:
    """Sub-signal names from the strategy manifest, longest first."""
    import yaml

    try:
        from evotrader.config import AppConfig

        path = AppConfig().algorithms_dir / "strategy_manifest.yaml"
    except Exception:
        from evotrader import paths

        path = paths.data_dir() / "algorithms" / "strategy_manifest.yaml"
    try:
        manifest = yaml.safe_load(Path(path).read_text()) or {}
    except Exception:
        return []
    return sorted((manifest.get("strategies") or {}), key=len, reverse=True)


def _base_channel(author: str, known: list[str]) -> str:
    """The sub-signal that authored a call, stripped of its qualifiers.

    The EARLIEST named channel wins, not the longest. Author strings routinely
    name the dissenter too — ``momentum_solo_2of9_mean_reversion_dissenting``
    was authored by momentum, and longest-match would have filed it under
    mean_reversion, inverting the record for both channels. Ties on position
    go to the longer name, so ``vwap_reclaim_continuation`` is not swallowed by
    a shorter name starting at the same place. An author string naming no known
    channel is kept verbatim — inventing a bucket for it would hide that it is
    unattributed.
    """
    if not author:
        return "unattributed"
    lowered = author.lower()
    hits = [(lowered.index(n), -len(n), n) for n in known if n in lowered]
    if hits:
        return min(hits)[2]
    return author.split("(")[0].strip() or "unattributed"


async def get_signal_calibration(
    lookback_days: int = 60, ticker: str = "", horizon: str = "1d"
) -> dict:
    """Score each witness's calls against what price actually did afterwards.

    This is the live-data calibration record: every cycle logs what the
    algorithm, the news agent and the strategy agent each said *before* they
    were combined, and the realised forward return is written back once the
    horizon elapses. No-trade cycles are included — they are the control group,
    and excluding them turns any measured edge into selection rather than skill.

    Use this, not a backtest, to decide whether a witness or a parameter is
    earning its place. The backtest runs the algorithm alone with mechanical
    exits; only this record contains the agent layer, the real cadence, real
    fills and the instrument being traded now.

    Read the output in this order:

    1. ``n_scored`` and ``base_rate`` — a 40-call sample and a tape that rose
       70% of the time can make a coin flip look like an edge. ``baseline_hit_rate``
       is what always calling the majority direction would have scored.
    2. ``witnesses`` — ``t_stat`` is clustered by day, so several cycles inside
       one move count once. ``informative`` is ``t > 2``; below that, the honest
       reading is "not distinguishable from chance", NOT "slightly better".
    3. ``by_side`` — a with-trend long record routinely hides a short record
       that loses every time, and vice versa.
    4. ``by_author`` — which sub-signal authored the call. This is where a
       channel that should be re-weighted or deprecated shows up.
    5. ``conviction_calibration`` — conviction drives position size, so
       persistent overconfidence in the high bands is the expensive failure.

    Args:
        lookback_days: How far back to score (default 60). Older cycles may
            predate the current instrument or the current instructions.
        ticker: Restrict to one instrument (empty = all). A lesson from one
            instrument is a hypothesis on the next, not a base rate.
        horizon: ``"1d"`` (next session's close) or ``"5d"`` (fifth session).
    """
    import json
    import math

    from evotrader.backtest.attribution import attribute, score_calibration, score_source

    if not _attribution_store:
        return {"error": "Attribution store not initialised"}

    column = "forward_return_5d" if str(horizon).lower().startswith("5") else "forward_return_1d"
    cutoff = datetime.now(UTC) - timedelta(days=max(1, int(lookback_days)))
    want = str(ticker or "").strip().upper()

    rows = [
        r
        for r in await _attribution_store.scored_rows(limit=5000)
        if r.get(column) is not None
        and str(r.get("timestamp", "")) >= cutoff.isoformat()
        and (not want or str(r.get("ticker", "")).upper() == want)
    ]
    # Scoped to the same instrument, or the count answers a different question
    # than the table above it.
    waiting = len(
        [
            r
            for r in await _attribution_store.pending_scoring(horizon_days=1, limit=5000)
            if not want or str(r.get("ticker", "")).upper() == want
        ]
    )

    if len(rows) < 3:
        return {
            "n_scored": len(rows),
            "n_waiting_on_horizon": waiting,
            "horizon": column,
            "note": (
                "Too few scored cycles to score anything. Rows are scored after "
                "each cycle once their horizon elapses; if this stays at zero "
                "while cycles are running, the scoring step itself is broken."
            ),
        }

    returns = [float(r[column]) for r in rows]
    stamps = [r["timestamp"] for r in rows]

    def _num(value: float) -> float | None:
        """NaN is not a number the model should reason about — say None."""
        return None if value is None or not math.isfinite(value) else round(float(value), 4)

    # score_source's own minimum. Below it, it returns 0.0 as a placeholder for
    # the rates — which read as "right 0% of the time" rather than "not enough
    # calls", so they are reported as None with a note instead.
    min_calls = 3

    def _score(
        name: str,
        directions: list[float],
        rets: list[float] | None = None,
        when: list | None = None,
    ) -> dict:

        sc = score_source(
            name, directions, returns if rets is None else rets, stamps if when is None else when
        )
        enough = sc.n_calls >= min_calls

        def _rate(value: float) -> float | None:
            return _num(value) if enough else None

        return {
            "source": sc.source,
            "n_calls": sc.n_calls,
            "n_independent_days": sc.n_events,
            "hit_rate": _rate(sc.hit_rate),
            "baseline_hit_rate": _rate(sc.baseline_hit_rate),
            "edge_over_baseline": _rate(sc.edge_over_baseline),
            "mean_signed_return_pct": _rate(sc.mean_forward_return),
            "information_coefficient": _num(sc.information_coefficient),
            "t_stat": _num(sc.t_stat),
            "informative": sc.is_informative,
            **({} if enough else {"note": f"fewer than {min_calls} calls — nothing to score yet"}),
        }

    def _dirs(key: str, *, agent_side: bool = False) -> list[float]:
        # NaN, not 0.0, for a witness that is not on the row: 0.0 is a call to
        # stand flat, NaN is no call at all. The scorers drop both from hit
        # rates, but only NaN keeps an absent agent out of the agreement and
        # conviction tables. Rows written without the agent (agent_absent,
        # migration 0021) count for the algorithm only.
        nan = float("nan")
        return [
            nan if (agent_side and r.get("agent_absent")) or r.get(key) is None else float(r[key])
            for r in rows
        ]

    sources = {
        "algo": _dirs("algo_direction"),
        "news": _dirs("news_direction", agent_side=True),
        "llm": _dirs("llm_direction", agent_side=True),
        "final": _dirs("final_direction", agent_side=True),
    }
    report = attribute(sources, returns, stamps)

    by_side: dict[str, dict] = {}
    for name in ("algo", "final"):
        dirs = sources[name]
        by_side[name] = {
            "long": _score(f"{name}:long", [d if d > 0 else 0.0 for d in dirs]),
            "short": _score(f"{name}:short", [d if d < 0 else 0.0 for d in dirs]),
        }

    # ``algo_author`` is free text written per cycle — "momentum",
    # "momentum_solo_2of9_mean_reversion_dissenting", "intraday_vwap_zscore
    # (counter-trend)". Grouping on the raw string shatters the record into
    # buckets of one, which teaches nothing. Group on the sub-signal that
    # authored the call and keep the qualifiers alongside.
    known = _known_channel_names()
    authors: dict[str, list[int]] = {}
    variants: dict[str, set[str]] = {}
    for i, r in enumerate(rows):
        raw = str(r.get("algo_author") or "").strip()
        channel = _base_channel(raw, known)
        authors.setdefault(channel, []).append(i)
        if raw and raw != channel:
            variants.setdefault(channel, set()).add(raw)
    by_author = []
    for author, idx in sorted(authors.items(), key=lambda kv: -len(kv[1])):
        calls = [i for i in idx if abs(sources["algo"][i]) > 1e-9]
        if not calls:
            continue
        signed = [sources["algo"][i] / abs(sources["algo"][i]) * returns[i] for i in calls]
        by_author.append(
            {
                "author": author,
                "n_calls": len(calls),
                "hit_rate": _num(sum(1 for v in signed if v > 0) / len(signed)),
                "mean_signed_return_pct": _num(sum(signed) / len(signed)),
                "qualifiers_seen": sorted(variants.get(author, []))[:8],
            }
        )

    # Every channel scored on its OWN votes, independent of who authored the
    # row. by_author can only ever see the channel that led the weighted sum:
    # swing_failure's first confirmed reversal (2026-09-23 11:30, +0.045)
    # sat under momentum's +0.274 and was invisible there. Rows recorded before
    # channel votes existed are skipped, so n_calls is honest about coverage.
    voted = [
        (i, json.loads(r["channel_votes"])) for i, r in enumerate(rows) if r.get("channel_votes")
    ]
    by_channel = []
    for name in sorted({ch for _, v in voted for ch in v}):
        entry = _score(
            name,
            [float((v.get(name) or {}).get("value") or 0.0) for _, v in voted],
            [returns[i] for i, _ in voted],
            [stamps[i] for i, _ in voted],
        )
        entry.pop("source")
        by_channel.append({"channel": name, **entry})

    up = sum(1 for v in returns if v > 0) / len(returns)
    return {
        "window": {
            "lookback_days": int(lookback_days),
            "since": cutoff.date().isoformat(),
            "ticker": want or "all",
        },
        "horizon": column,
        "units": "percent; +2.16 means +2.16%",
        "n_scored": len(rows),
        "n_waiting_on_horizon": waiting,
        "base_rate": {
            "share_of_cycles_price_rose": _num(up),
            "mean_return_pct": _num(sum(returns) / len(returns)),
            "always_long_would_have_hit": _num(up),
        },
        "witnesses": [_score(name, dirs) for name, dirs in sources.items()],
        "agreement": (
            {
                "source": report.agreement.source,
                "n_calls": report.agreement.n_calls,
                "hit_rate": _num(report.agreement.hit_rate),
                "t_stat": _num(report.agreement.t_stat),
                "beats_every_single_witness": report.agreement_adds_value,
                "note": (
                    "If this is false the witnesses are correlated — they are "
                    "re-reading the same information and the ensemble is not "
                    "earning its complexity."
                ),
            }
            if report.agreement is not None
            else None
        ),
        "by_side": by_side,
        "by_author": by_author,
        "by_channel": by_channel,
        "rows_with_channel_votes": len(voted),
        # Cycles the strategy agent never logged (provider outage, crash): the
        # algorithm's call is scored, the agent-side witnesses are not.
        "rows_without_agent": sum(1 for r in rows if r.get("agent_absent")),
        "conviction_calibration": [
            {
                "band": f"{b.low:.1f}-{b.high:.1f}",
                "n": b.n,
                "stated": _num(b.stated),
                "realized": _num(b.realized),
                "gap": _num(b.gap),
                "reading": (
                    "overconfident"
                    if b.gap > 0.10
                    else "underconfident"
                    if b.gap < -0.10
                    else "calibrated"
                ),
            }
            for b in score_calibration(
                [float(r.get("final_conviction") or 0.0) for r in rows],
                sources["final"],
                returns,
            )
        ],
        "caveat": (
            "Hit rate is not P&L: position size and exits decide the money. "
            "A t_stat under 2.0 means the sample cannot tell this apart from "
            "chance — do not tune a parameter on it."
        ),
    }


async def query_cycle_thoughts(
    session_id: str = "",
    agent_name: str = "",
    event_type: str = "",
    limit: int = 50,
) -> dict:
    """Drill into the full reasoning chain of a specific trading cycle.

    Returns agent thoughts, tool calls, and tool responses for targeted
    analysis. Use filters to narrow down to specific agents or event types.

    Agent reasoning (the `content` field) is always returned in full. Large tool
    payloads (the `meta` field) are summarised to a shape description plus a
    preview, and marked with `"_omitted": true` — call
    `get_tool_response(event_id)` with that event's `id` to read one in full.

    IMPORTANT: Prefer `get_cycle_digest` for broad review. Use this tool
    only when you need to deep-dive into a specific cycle's raw events
    (e.g., to trace why a specific indicator produced an unexpected value).

    Available agent_name values: "orchestrator", "strategy", "news_sentiment"
    Available event_type values: "thought", "tool_call", "tool_response",
    "runtime" (harness diagnostics such as quota checks — excluded from the
    digest's final-thought summary, queryable here when a run looks truncated)

    Args:
        session_id: Cycle session ID to query (empty = most recent).
        agent_name: Filter to a specific agent (empty = all agents).
        event_type: Filter to a specific event type (empty = all types).
        limit: Maximum events to return (default 50).
    """
    if not _thought_logger:
        return {"error": "Thought logger not initialised"}

    # If no session_id, get the most recent one
    if not session_id:
        sessions = await _thought_logger.get_unique_sessions(limit=1)
        if not sessions:
            return {"error": "No cycle sessions found"}
        session_id = sessions[0]["session_id"]

    thoughts = await _thought_logger.get_recent_thoughts(
        limit=limit,
        session_id=session_id,
        agent_name=agent_name or None,
        event_type=event_type or None,
    )

    # Agent reasoning (`content`) is always returned in full. Oversized tool
    # payloads (`meta`) are summarised, because research tools can return
    # unbounded data — a market-wide earnings calendar or a full-history rate
    # series — which would otherwise crowd out the reasoning this tool exists
    # to surface. Nothing is lost: use `get_tool_response` to fetch a payload.
    events, stats = summarise_events(thoughts)

    result: dict = {
        "session_id": session_id,
        "events": events,
        "count": len(events),
    }
    if stats.get("payloads_omitted"):
        result["payload_summary"] = stats
    return result


async def get_tool_response(event_id: int) -> dict:
    """Fetch one tool payload in full by its thought-log event id.

    ``query_cycle_thoughts`` summarises oversized tool payloads so the reasoning
    stays readable. When you need the complete payload for a specific event, take
    the ``id`` from that event and pass it here.

    Args:
        event_id: The ``id`` of the thought-log event whose payload you want.
    """
    if not _thought_logger:
        return {"error": "Thought logger not initialised"}

    event = await _thought_logger.get_event(event_id)
    if not event:
        return {"error": f"No thought-log event with id {event_id}"}

    meta = event.get("meta")
    if isinstance(meta, str):
        meta = safe_parse_json(meta) or meta

    return {
        "event_id": event_id,
        "session_id": event.get("session_id"),
        "agent_name": event.get("agent_name"),
        "event_type": event.get("event_type"),
        "tool_name": event.get("content"),
        "timestamp": event.get("timestamp"),
        "meta": meta,
    }


async def get_cycle_digest(limit: int = 10) -> dict:
    """Get a compact digest of recent trading cycles with agent reasoning.

    Returns structured metadata AND the final reasoning from each agent
    (orchestrator, strategy, news_sentiment) for each cycle. This is the
    recommended starting point for reviewing cycle history — it provides
    enough detail to identify patterns and anomalies without overwhelming
    the context window.

    Use `query_cycle_thoughts` to drill into specific cycles/agents only
    when the digest reveals something that needs deeper investigation.

    Args:
        limit: Number of recent cycles to include (default 10).
    """
    if not _thought_logger:
        return {"error": "Thought logger not initialised"}

    sessions = await _thought_logger.get_unique_sessions(limit=limit)
    digests = []
    for s in sessions:
        sid = s["session_id"]
        final_thoughts = await _thought_logger.get_final_thoughts(sid)
        digests.append(
            {
                **s,
                "final_thoughts": {t["agent_name"]: t["content"] for t in final_thoughts},
            }
        )
    return {"cycles": digests, "count": len(digests)}


async def get_cycle_summary(limit: int = 10) -> dict:
    """List recent trading cycle sessions with metadata.

    Returns session IDs, timestamps, event counts, and whether trades
    were executed.  Use this to identify which cycles to deep-dive
    into with ``query_cycle_thoughts``.

    Tip: Cycles with no trades (has_trades=0) are especially valuable
    for understanding if the system is being too conservative.

    Args:
        limit: Number of recent sessions to return (default 10).
    """
    if not _thought_logger:
        return {"error": "Thought logger not initialised"}

    sessions = await _thought_logger.get_unique_sessions(limit=limit)
    return {"sessions": sessions, "count": len(sessions)}


# ═══════════════════════════════════════════════════════════════════════
# Code Reading / Review Tools
# ═══════════════════════════════════════════════════════════════════════


def read_strategy_code(strategy_name: str) -> dict:
    """Read the source code of an existing trading strategy.

    Use this to understand the current implementation before proposing
    improvements. Available strategies: 'momentum', 'mean_reversion',
    'gap', 'composite', 'base'.

    Args:
        strategy_name: Name of the strategy (e.g., 'momentum').
    """
    if not _code_evolver:
        return {"error": "Code evolver not initialised"}

    return _code_evolver.read_strategy_source(strategy_name)


def read_source_file(relative_path: str) -> dict:
    """Read any source file from the project for review.

    Use this to analyse infrastructure code and propose improvements.
    Only files within src/evotrader/ can be read.

    Args:
        relative_path: Path relative to the project root
            (e.g., 'src/evotrader/indicators/rsi.py').
    """
    if not _code_evolver:
        return {"error": "Code evolver not initialised"}

    return _code_evolver.read_source_file(relative_path)


def list_project_files(subdirectory: str = "") -> dict:
    """List Python source files in a project subdirectory.

    Use this to explore the codebase before reviewing specific files.
    Available subdirectories: 'algorithms', 'indicators', 'agents',
    'callbacks', 'models', 'tools', 'evolution', 'mcp', 'db'.

    Args:
        subdirectory: Subdirectory to list (e.g., 'indicators').
            Leave empty to list all files.
    """
    if not _code_evolver:
        return {"error": "Code evolver not initialised"}

    files = _code_evolver.list_source_files(subdirectory)
    return {"files": files, "count": len(files)}


# ═══════════════════════════════════════════════════════════════════════
# Algorithm Parameter Evolution Tools
# ═══════════════════════════════════════════════════════════════════════
# Evolution Database Helpers
# ═══════════════════════════════════════════════════════════════════════


async def _log_evolution(
    change_type: str,
    target_component: str,
    old_version: str,
    new_version: str,
    reasoning: str,
    expected_impact: str | None = None,
    metrics_before: dict | None = None,
    metrics_after: dict | None = None,
    code_diffs: list | None = None,
    status: str = "PROPOSED",
    risk_level: str = "LOW",
) -> None:
    if not _evolution_store:
        logger.warning("No evolution_store available to log evolution event")
        return

    await _evolution_store.insert(
        change_type=change_type,
        target_component=target_component,
        old_version=old_version,
        new_version=new_version,
        reasoning=reasoning,
        expected_impact=expected_impact,
        metrics_before=metrics_before,
        metrics_after=metrics_after,
        code_diffs=code_diffs,
        status=status,
        risk_level=risk_level,
    )


async def _update_evolution_status(
    new_version: str,
    status: str,
) -> None:
    if not _evolution_store:
        return

    await _evolution_store.update_status(version=new_version, status=status)


# ═══════════════════════════════════════════════════════════════════════


async def propose_parameter_change(
    version_name: str,
    description: str,
    reasoning: str,
    parameters_json: str,
) -> dict:
    """Propose new algorithm parameters as a new version.

    Creates a new algorithm version with updated parameters. The version
    is saved to disk but NOT activated — it must pass backtesting first.

    Args:
        version_name: New version identifier (e.g., 'v002_wider_rsi').
        description: Human-readable description of what changed.
        reasoning: Why these parameter changes are expected to improve performance.
        parameters_json: JSON string of the complete parameter set
            (same structure as config.yaml in algorithm versions).
    """
    if not _algo_registry:
        return {"error": "Algorithm registry not initialised"}

    import json

    try:
        parameters = safe_parse_json(parameters_json)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}"}

    metadata = {
        "name": description,
        "version": version_name,
        "description": reasoning,
        "created_by": "evolution_agent",
        "change_type": "ALGORITHM_PARAMS",
        "status": "proposed",
    }

    version_dir = _algo_registry.save_version(version_name, parameters, metadata)

    try:
        previous = _algo_registry.get_active_version()
    except Exception:
        previous = "unknown"

    await _log_evolution(
        change_type="ALGORITHM_PARAMS",
        risk_level="LOW",
        target_component="strategy",
        old_version=previous,
        new_version=version_name,
        reasoning=reasoning,
        expected_impact=description,
        status="PROPOSED",
    )

    return {
        "status": "saved",
        "version": version_name,
        "version_dir": str(version_dir),
        "note": "NOT activated yet. Run backtesting before promoting.",
    }


async def promote_algorithm_version(version: str) -> dict:
    """Promote an algorithm version to active (after backtesting).

    This makes the specified version the active algorithm used for
    all future trading decisions. Only call this after backtesting
    confirms the version meets minimum criteria.

    Args:
        version: Version identifier to activate (e.g., 'v002_wider_rsi').
    """
    if not _algo_registry:
        return {"error": "Algorithm registry not initialised"}

    if _config and not _is_auto_promote_enabled():
        return {
            "status": "pending_review",
            "message": (
                f"Auto-promotion is disabled. Version '{version}' has been proposed and backtested, "
                "but requires human approval and manual activation in the Web Console UI."
            ),
        }

    try:
        previous = _algo_registry.get_active_version()
        _algo_registry.set_active_version(version)

        await _update_evolution_status(version, "ACTIVE")

        return {
            "status": "promoted",
            "previous_version": previous,
            "new_active_version": version,
        }
    except ValueError as e:
        return {"error": str(e)}


async def rollback_algorithm(to_version: str) -> dict:
    """Rollback the algorithm to a previous version.

    Use this when a promoted version underperforms. Records the
    rollback in the audit trail.

    Args:
        to_version: Version to rollback to (e.g., 'v001_initial').
    """
    if not _algo_registry:
        return {"error": "Algorithm registry not initialised"}

    try:
        current = _algo_registry.get_active_version()
        _algo_registry.rollback(to_version)

        await _update_evolution_status(current, "ROLLED_BACK")
        await _update_evolution_status(to_version, "ACTIVE")

        return {
            "status": "rolled_back",
            "from_version": current,
            "to_version": to_version,
        }
    except ValueError as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════
# Code Evolution Tools (Strategy Generation)
# ═══════════════════════════════════════════════════════════════════════


def validate_strategy_code(code: str) -> dict:
    """Validate proposed strategy code before saving.

    Checks syntax, forbidden imports (os, sys, subprocess), forbidden
    patterns (eval, exec, open), and structural requirements (must
    extend TradingAlgorithm, must have compute_signal method).

    Args:
        code: The Python source code to validate.
    """
    if not _code_evolver:
        return {"error": "Code evolver not initialised"}

    return _code_evolver.validate_strategy_code(code)


# ═══════════════════════════════════════════════════════════════════════
# Infrastructure Code Review Tools
# ═══════════════════════════════════════════════════════════════════════


async def submit_code_review(
    review_title: str,
    files_reviewed_json: str,
    findings_json: str,
    proposed_diffs_json: str,
) -> dict:
    """Submit a code review of infrastructure code.

    Reviews are saved as markdown files for HUMAN review — they are
    never auto-applied. The Evolution Agent can review any part of the
    codebase and propose improvements for readability, performance,
    error handling, etc.

    Args:
        review_title: Short title for the review (e.g., 'improve_rsi_edge_cases').
        files_reviewed_json: JSON array of file paths that were reviewed.
        findings_json: JSON array of {severity, file, issue, suggestion} dicts.
        proposed_diffs_json: JSON array of {file, description, diff} dicts.
    """
    if not _code_evolver:
        return {"error": "Code evolver not initialised"}

    import json
    from datetime import UTC, datetime

    try:
        files = safe_parse_json(files_reviewed_json)
        findings = safe_parse_json(findings_json)
        diffs = safe_parse_json(proposed_diffs_json)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}"}

    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    review_id = f"{timestamp}_{review_title}"

    review_path = _code_evolver.save_code_review(
        review_id=review_id,
        files_reviewed=files,
        findings=findings,
        proposed_diffs=diffs,
    )

    await _log_evolution(
        change_type="CODE_REVIEW",
        risk_level="CRITICAL",
        target_component="infrastructure",
        old_version="current",
        new_version=review_id,
        reasoning=review_title,
        code_diffs=diffs,
        status="PENDING_REVIEW",
    )

    return {
        "status": "saved_for_human_review",
        "review_id": review_id,
        "file_path": str(review_path),
        "note": "Infrastructure code changes are NEVER auto-applied. "
        "A human must review and apply the proposed changes.",
    }


# ═══════════════════════════════════════════════════════════════════════
# Instruction Evolution Tools
# ═══════════════════════════════════════════════════════════════════════


async def propose_instruction_change(
    agent_name: str,
    new_instructions: str,
    reasoning: str,
) -> dict:
    """Propose new system instructions for an agent.

    Creates a new versioned instruction file with a diff from the
    current version. The change is NOT applied until explicitly activated.

    Protected agents (risk_manager) cannot be modified.
    Rate-limited per settings.yaml → evolution.max_instruction_changes_per_week.

    Available agents: 'orchestrator', 'news_sentiment', 'strategy',
    'executor', 'evolution'.

    Args:
        agent_name: Target agent identifier.
        new_instructions: The complete new instruction text.
        reasoning: Why this change should improve the agent's behavior.
    """
    if not _instruction_evolver:
        return {"error": "Instruction evolver not initialised"}

    # Read the rate limit from settings.yaml (None = no limit)
    max_per_week = _get_max_instruction_changes_per_week()

    res = _instruction_evolver.propose_change(
        agent_name=agent_name,
        new_instructions=new_instructions,
        reasoning=reasoning,
        max_per_week=max_per_week,
    )

    if res.get("status") == "proposed":
        await _log_evolution(
            change_type="INSTRUCTION_UPDATE",
            risk_level="MEDIUM",
            target_component=agent_name,
            old_version=res["previous_version"],
            new_version=res["new_version"],
            reasoning=reasoning,
            expected_impact=res.get("diff_summary"),
            status="PROPOSED",
        )

    return res


async def activate_instruction_version(agent_name: str, version: str) -> dict:
    """Activate a proposed instruction version for an agent.

    Args:
        agent_name: Target agent.
        version: Version to activate (e.g., 'v002').
    """
    if not _instruction_evolver:
        return {"error": "Instruction evolver not initialised"}

    if _config and not _is_auto_promote_enabled():
        return {
            "status": "pending_review",
            "message": (
                f"Auto-promotion is disabled. Version '{version}' for agent '{agent_name}' has been proposed, "
                "but requires human review and manual activation in the Web Console UI."
            ),
        }

    res = _instruction_evolver.activate_version(agent_name, version)
    if res.get("status") == "activated":
        await _update_evolution_status(version, "ACTIVE")
    return res


def list_instruction_versions(agent_name: str) -> dict:
    """List all instruction versions for an agent.

    Args:
        agent_name: Agent to check (e.g., 'strategy').
    """
    if not _instruction_evolver:
        return {"error": "Instruction evolver not initialised"}

    versions = _instruction_evolver.list_versions(agent_name)
    current = _instruction_evolver.get_current_version(agent_name)
    return {"agent": agent_name, "current_version": current, "versions": versions}


def get_instruction_text(agent_name: str, version: str = "") -> dict:
    """Read the instruction text for a specific agent and version.

    Args:
        agent_name: Agent identifier.
        version: Version to read (empty = current active version).
    """
    if not _instruction_evolver:
        return {"error": "Instruction evolver not initialised"}

    text = _instruction_evolver.get_instructions(agent_name, version if version else None)
    return {"agent": agent_name, "version": version or "active", "text": text}


async def rollback_instructions(agent_name: str, to_version: str) -> dict:
    """Rollback an agent's instructions to a previous version.

    Args:
        agent_name: Agent to rollback.
        to_version: Version to restore (e.g., 'v001').
    """
    if not _instruction_evolver:
        return {"error": "Instruction evolver not initialised"}

    try:
        current = _instruction_evolver.get_current_version(agent_name)
    except Exception:
        current = "unknown"

    res = _instruction_evolver.rollback(agent_name, to_version)
    if res.get("status") == "activated":
        await _update_evolution_status(current, "ROLLED_BACK")
        await _update_evolution_status(to_version, "ACTIVE")
    return res


def get_strategy_manifest() -> dict:
    """Read the current strategy manifest.

    Returns:
        Dict containing strategies and their configurations.
    """
    if not _config:
        return {"error": "Config not bound"}

    manifest_path = _config.algorithms_dir / "strategy_manifest.yaml"
    if not manifest_path.is_file():
        return {"error": f"Manifest not found at {manifest_path}"}
    try:
        import yaml

        with open(manifest_path) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        return {"error": f"Failed to parse manifest: {e}"}


async def propose_new_strategy(
    name: str,
    description: str,
    signal_logic: str,
    indicators: list[str],
    params: dict[str, Any],
    integration: str,
) -> dict:
    """Propose a brand new trading strategy.

    This writes a proposal design document to disk for human or code agent review.

    Args:
        name: Name of the proposed strategy (e.g., 'vwap_scalper').
        description: Brief description of the strategy's core approach.
        signal_logic: Python code block or snippet defining the signal logic.
        indicators: List of technical indicators required by this strategy.
        params: Initial tunable parameters for the strategy.
        integration: Rationale for ensemble weight distribution.
    """
    if not _proposal_manager:
        return {"error": "Proposal manager not bound"}

    import datetime

    proposal_id = f"p_new_{name}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"

    frontmatter = {
        "proposal_id": proposal_id,
        "type": "new_strategy",
        "status": "proposed",
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "target_strategy": name,
        "required_indicators": indicators,
    }

    body = f"""# Proposal: {name}

## Description
{description}

## Rationale
Proposed dynamically by Evolution Agent.

## Signal Logic
```python
{signal_logic}
```

## Suggested Parameters
| Parameter | Value | Description |
|---|---|---|
"""
    for k, v in params.items():
        body += f"| `{k}` | `{v}` | Suggested initial parameter |\n"

    body += f"""
## Integration Details
{integration}
"""
    try:
        path = _proposal_manager.create_proposal(frontmatter, body)

        await _log_evolution(
            change_type="ALGORITHM_NEW",
            target_component=name,
            old_version="none",
            new_version=proposal_id,
            reasoning=description,
            expected_impact=integration,
            status="PROPOSED",
        )
        return {"status": "success", "proposal_id": proposal_id, "file_path": str(path)}
    except Exception as e:
        return {"error": f"Failed to create proposal: {e}"}


async def propose_deprecation(
    strategy_name: str,
    reasoning: str,
    redistribute_weight_to: dict[str, float],
) -> dict:
    """Propose the deprecation of an existing strategy.

    Args:
        strategy_name: Name of the strategy to deprecate (e.g., 'gap').
        reasoning: Rationale and evidence supporting removal.
        redistribute_weight_to: Dict of strategy names to redistributing weights.
    """
    if not _proposal_manager:
        return {"error": "Proposal manager not bound"}

    import datetime

    proposal_id = f"p_deprecate_{strategy_name}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"

    frontmatter = {
        "proposal_id": proposal_id,
        "type": "deprecate",
        "status": "proposed",
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "target_strategy": strategy_name,
        "redistribute_weight_to": redistribute_weight_to,
    }

    body = f"""# Proposal: Deprecate Strategy '{strategy_name}'

## Rationale & Evidence
{reasoning}

## Redistribution Plan
Redistribute weights as follows:
"""
    for k, v in redistribute_weight_to.items():
        body += f"- `{k}`: {v:.2%}\n"

    try:
        path = _proposal_manager.create_proposal(frontmatter, body)

        await _log_evolution(
            change_type="ALGORITHM_PARAMS",
            target_component=strategy_name,
            old_version="active",
            new_version=proposal_id,
            reasoning=reasoning,
            expected_impact=str(redistribute_weight_to),
            status="PROPOSED",
        )
        return {"status": "success", "proposal_id": proposal_id, "file_path": str(path)}
    except Exception as e:
        return {"error": f"Failed to create proposal: {e}"}


async def propose_composition_change(
    weight_changes: dict[str, float],
    reasoning: str,
) -> dict:
    """Propose changes to active strategies weights or composition.

    Args:
        weight_changes: Dict mapping active strategy names to new weights.
        reasoning: Rationale behind composition modification.
    """
    if not _proposal_manager:
        return {"error": "Proposal manager not bound"}

    import datetime

    proposal_id = f"p_composition_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"

    frontmatter = {
        "proposal_id": proposal_id,
        "type": "composition",
        "status": "proposed",
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "weight_changes": weight_changes,
    }

    body = f"""# Proposal: Modify Ensemble Composition

## Rationale
{reasoning}

## Proposed Weights
"""
    for k, v in weight_changes.items():
        body += f"- `{k}`: {v:.2%}\n"

    try:
        path = _proposal_manager.create_proposal(frontmatter, body)

        await _log_evolution(
            change_type="REGIME_WEIGHTS",
            target_component="ensemble",
            old_version="active",
            new_version=proposal_id,
            reasoning=reasoning,
            expected_impact=str(weight_changes),
            status="PROPOSED",
        )
        return {"status": "success", "proposal_id": proposal_id, "file_path": str(path)}
    except Exception as e:
        return {"error": f"Failed to create proposal: {e}"}


def list_proposals(status: str | None = None) -> dict:
    """List all evolution proposals, optionally filtered by status.

    Args:
        status: Status string to filter by (e.g., 'proposed', 'implemented').
    """
    if not _proposal_manager:
        return {"error": "Proposal manager not bound"}
    try:
        proposals = _proposal_manager.list_proposals(status)
        return {"status": "success", "proposals": proposals}
    except Exception as e:
        return {"error": f"Failed to list proposals: {e}"}


def update_carry_forward(content: str) -> dict:
    """Update the carry-forward notes file for cross-cycle continuity.

    The carry-forward notes are read by future evolution cycles to maintain
    context across sessions. Use this tool to:
    - Add new pending items discovered during analysis
    - Move completed items to the Resolved section
    - Remove stale or no-longer-relevant items

    The ``Last self-evolve run`` timestamp is updated automatically — do
    NOT include it in your content.

    Args:
        content: The complete markdown content for carry_forward.md.
            Must include the ``# Evolution Agent — Carry-Forward Notes``
            heading and both ``## Active Items`` and ``## Resolved Items``
            sections. Write the FULL file content, not a diff.
    """
    if not _config:
        return {"error": "Config not bound"}

    from datetime import UTC, datetime
    from pathlib import Path

    carry_forward_path = Path(_config.data_dir) / "evolution" / "notes" / "carry_forward.md"

    # Ensure the directory exists
    carry_forward_path.parent.mkdir(parents=True, exist_ok=True)

    # Auto-stamp the timestamp
    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")
    stamp_line = f"**Last self-evolve run**: {now_str}"

    # Insert timestamp after the first heading if not already present
    import re

    timestamp_re = re.compile(r"^\*\*Last self-evolve run\*\*:\s*\S+", re.MULTILINE)
    if timestamp_re.search(content):
        content = timestamp_re.sub(stamp_line, content, count=1)
    else:
        # Insert after the first line (heading)
        lines = content.split("\n", 1)
        content = f"{lines[0]}\n\n{stamp_line}\n" + (lines[1] if len(lines) > 1 else "")

    carry_forward_path.write_text(content)
    logger.info("📝 Carry-forward notes updated (%d bytes)", len(content))

    return {
        "status": "success",
        "path": str(carry_forward_path),
        "size_bytes": len(content),
        "timestamp": now_str,
    }
