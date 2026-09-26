"""Application entry point.

Initialises the EvoTrader system: loads configuration, sets up the
database, creates the agent system via Google ADK, and runs the
Orchestrator agent loop.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps.app import App
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from evotrader.agents.factory import (
    create_evolution_agent,
    create_orchestrator_agent,
    get_tool_metadata,
)
from evotrader.agents.provider_retry import run_with_one_resume, short_error
from evotrader.agents.tools import (
    bind_dependencies,
    check_and_journal_pending_fills,
    reconcile_pending_orders,
    set_current_session_id,
)
from evotrader.config import AppConfig
from evotrader.db.connection import Database
from evotrader.db.journal import TradeJournal
from evotrader.db.metrics import MetricsStore

logger = logging.getLogger(__name__)

# Expected pipeline stages (in order)
# gather_market_data is a deterministic tool on the orchestrator (not a sub-agent).
# It replaces the former market_intelligence LLM sub-agent.
_PIPELINE_STAGES = [
    "gather_market_data",
    "news_sentiment",
    "strategy",
    "risk_manager",
    "execution",
]
# Minimum stages for a "complete" cycle (strategy must run)
_MANDATORY_STAGES = {"gather_market_data", "strategy"}

# Stages an ORDER must pass through. Deliberately NOT promoted to mandatory: a
# cycle that ends at `strategy` with a reasoned no-trade is healthy, and making
# these mandatory would mark good judgement as failure.
#
# But when the strategy output NAMES orders and these were skipped, the cycle
# could not have traded even if it wanted to. That is a stalled pipeline, not a
# market judgement, and it must be visible rather than inferred from prose. Two
# full trading days were lost to this being invisible (2026-09-15/16), and the
# blindness was made worse by an otherwise-correct status fix.
_ORDER_PATH_STAGES = {"risk_manager", "execution"}

# Deliberately broad. A false positive costs one extra diagnostic row; a false
# negative costs a silent day of not trading.
_ORDER_INTENT_RE = re.compile(
    r"\b(TRADE PROPOSAL|CANCEL|PLACE|STOP_LOSS|TAKE_PROFIT|"
    r"routing to risk_manager|for risk_manager)\b",
    re.IGNORECASE,
)


def _stage_for_call(tool_name: str, args: dict | None) -> str | None:
    """Which pipeline stage an orchestrator tool call represents, if any.

    ADK reaches an ``LlmAgent`` sub-agent through a function call named after it,
    but a custom ``BaseAgent`` sub-agent — such as one hosted on a CLI runtime —
    through ``transfer_to_agent(agent_name=...)``. Both are the same pipeline
    stage.

    Recognising only the first shape made every cycle running the hosted strategy
    agent record as ``partial`` with ``strategy`` missing from the completed
    stages, even though it had run and produced a decision. That is not cosmetic:
    ``_MANDATORY_STAGES`` gates the cycle status, and the evolution agent reads
    those rows when judging whether a cycle was healthy.

    **But recognising the transfer fixes the STATUS, not the PIPELINE.** A
    ``transfer_to_agent`` is a one-way handoff: ADK runs the target, yields its
    events, and the parent generator ends. Any stage reached that way is
    therefore necessarily the LAST stage of the cycle — everything downstream is
    unreachable. Adding this branch on 2026-09-15 upgraded sixteen structurally
    broken cycles from ``partial`` to ``complete`` and hid a two-day outage in
    which no order could be placed at all. The fix for that is wiring, not
    status: see ``SessionSharingAgentTool``. This sentence is here so the next
    reader does not have to lose two trading days rediscovering it.
    """
    if tool_name in _PIPELINE_STAGES:
        return tool_name
    if tool_name == "transfer_to_agent":
        target = (args or {}).get("agent_name")
        if target in _PIPELINE_STAGES:
            return str(target)
    return None


def _init_telemetry(db_dir: Path) -> None:
    """Initialise OpenTelemetry with SQLite exporter for local model logging.

    Configures:
    - ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS = "true"
      Enables request/response text capturing for legacy ADK-owned spans (like 'call_llm').
    - OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT = "SPAN_ONLY"
      Enables request/response text capturing for standard OpenTelemetry GenAI spans
      (e.g., from opentelemetry-instrumentation-google-genai).
    """
    import os

    # Ensure capture is enabled in environment for both ADK legacy and standard OTel spans
    os.environ["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"] = "true"
    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "SPAN_ONLY"

    from google.adk.telemetry.sqlite_span_exporter import SqliteSpanExporter
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    telemetry_db_path = db_dir / "telemetry.db"
    exporter = SqliteSpanExporter(db_path=str(telemetry_db_path))

    # Set up global OpenTelemetry TracerProvider if not already configured
    try:
        from opentelemetry.trace import ProxyTracerProvider, get_tracer_provider

        current_provider = get_tracer_provider()
        if isinstance(current_provider, ProxyTracerProvider):
            provider = TracerProvider()
            provider.add_span_processor(SimpleSpanProcessor(exporter))
            trace.set_tracer_provider(provider)
            logger.info("Local OTel telemetry initialized at %s", telemetry_db_path)
        else:
            if hasattr(current_provider, "add_span_processor"):
                current_provider.add_span_processor(SimpleSpanProcessor(exporter))
                logger.info(
                    "Added SQLite span exporter to existing TracerProvider at %s", telemetry_db_path
                )
            else:
                logger.warning(
                    "TracerProvider already set to a non-SDK provider. Telemetry logging might be disabled."
                )
    except Exception as e:
        logger.warning("Failed to configure OTel TracerProvider: %s", e)


async def _init_universe(config: AppConfig) -> None:
    """Resolve and cache the constituent universe for every configured ticker.

    Warms the resolver at startup so the trading path reads an in-memory answer
    rather than waiting on a lookup mid-cycle, and so the synchronous MCP
    response curators can see it. A failure here is logged and tolerated: the
    resolver degrades to a stale cache, and failing that to the ticker alone.
    """
    from evotrader.tools.universe import (
        UniverseResolver,
        bind_resolver,
        trading_tickers,
    )

    universe_cfg = config.settings.asset.universe
    resolver = UniverseResolver(
        cache_dir=config.data_dir / "cache" / "universe",
        ttl_days=universe_cfg.cache_ttl_days,
        top_n=universe_cfg.top_n,
        min_weight_pct=universe_cfg.min_weight_pct,
    )
    bind_resolver(resolver)

    tickers = trading_tickers(config)
    if not tickers:
        logger.warning("No tickers configured — universe resolution skipped.")
        return

    try:
        symbols = await resolver.symbols(tickers)
        logger.info(
            "Trading universe for %s: %d symbol(s) — %s",
            ", ".join(tickers),
            len(symbols),
            ", ".join(symbols),
        )
    except Exception as e:
        logger.warning(
            "Universe resolution failed for %s: %s. Event detection will narrow "
            "to the traded ticker(s) until it succeeds.",
            ", ".join(tickers),
            e,
        )


def _create_evolution_service(
    config: AppConfig,
    session_service: Any,
    thought_logger: Any,
) -> Any:
    """Build the evolution service for the runtime configured in settings.

    ``agent_runtime.evolution`` selects either the ``api`` runtime (ADK against
    a completion endpoint, billed per token) or a named ``cli_runtimes`` entry (a
    hosted-agent harness, billable to a subscription seat). Both write identical
    proposals, DB rows and web-console events, so this is a runtime choice rather
    than a fork.

    If a CLI runtime is configured but unavailable (SDK or CLI missing), this
    falls back to the API runtime with a loud warning rather than leaving the
    system unable to evolve.
    """
    from evotrader.agents.cli import load_backend
    from evotrader.models.config import AgentRuntimeKind

    kind, runtime = config.settings.runtime_for("evolution")

    if kind is AgentRuntimeKind.CLI and runtime is not None:
        from evotrader.evolution.claude_code_service import ClaudeCodeEvolutionService

        backend, message = load_backend(runtime.driver, runtime)
        if backend is not None:
            from evotrader.agents import instructions

            evolution_instructions = instructions.load(
                "evolution",
                config.instructions_dir,
                is_sim=(config.settings.mode.value == "sim"),
            )
            # State the CONFIGURED INTENT, never the outcome. The previous
            # version of this line read "subscription-billed" whenever the CLI
            # was present, and settings.yaml told the operator to treat it as
            # proof. It was not proof: the harness silently preferred
            # ANTHROPIC_API_KEY from .env, so every run was API-billed while
            # this line claimed otherwise. Actual billing is reported after the
            # run from the result's `billing_used`, which is established by
            # which credential the subprocess was given.
            logger.info(
                "Evolution runtime: %s driver=%s model=%s billing=%s (requested; "
                "actual billing is logged after the run)",
                runtime.driver,
                runtime.driver,
                runtime.model or "harness default",
                runtime.billing,
            )
            if runtime.billing != "api":
                from evotrader.agents.cli.claude_code import (
                    subscription_credential_source,
                )

                if subscription_credential_source() is None:
                    logger.warning(
                        "No subscription credential found (CLAUDE_CODE_OAUTH_TOKEN "
                        "or ~/.claude/.credentials.json). Run 'claude setup-token' "
                        "and put the token in .env, or this run will %s.",
                        "fall back to API billing" if runtime.billing == "auto" else "fail",
                    )

            def _api_fallback():
                """Build the API-backed service, only if the quota wall is hit.

                A factory, not an instance: the ADK agent and its session
                service cost nothing to skip, and the common case never hits
                this. Honours `quota.on_exhausted: "api"`, which before
                2026-09-18 was config that no code read.
                """
                from evotrader.evolution.evolution_service import EvolutionService

                return EvolutionService(
                    agent=create_evolution_agent(config),
                    session_service=session_service,
                    thought_logger=thought_logger,
                    config=config,
                )

            return ClaudeCodeEvolutionService(
                thought_logger=thought_logger,
                config=config,
                instructions=evolution_instructions,
                api_fallback=_api_fallback,
            )

        logger.error(
            "COST WARNING: agent_runtime.evolution requests the %r runtime but "
            "it is unavailable — %s Falling back to the API runtime, which is "
            "BILLED PER TOKEN. Fix the CLI or leave evolution_cron weekly to "
            "limit spend.",
            runtime.driver,
            message,
        )

    from evotrader.evolution.evolution_service import EvolutionService

    evolution_agent = create_evolution_agent(config)
    logger.info("Evolution backend: adk (agent created, scheduled for post-market)")
    return EvolutionService(
        agent=evolution_agent,
        session_service=session_service,
        thought_logger=thought_logger,
        config=config,
    )


async def start(
    dashboard: bool = False,
    mode_override: str | None = None,
    sim_deposit: float | None = None,
) -> None:
    """Initialise and run the EvoTrader trading system."""
    # ── Load configuration ──────────────────────────────────────
    config = AppConfig(mode_override=mode_override)

    # ── Initialize telemetry ──────────────────────────────────
    _init_telemetry(config.db_dir)

    # ── Configure logging ───────────────────────────────────────
    logging.basicConfig(
        level=getattr(logging, config.settings.logging.level.upper(), logging.INFO),
        format="%(asctime)s │ %(name)-30s │ %(levelname)-7s │ %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Quieten noisy third-party loggers
    logging.getLogger("chromadb").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("litellm").setLevel(logging.WARNING)
    logging.getLogger("google.adk").setLevel(logging.INFO)

    logger.info("=" * 60)
    logger.info("  EvoTrader v0.2.0 — Starting up (ADK + LiteLLM)")
    logger.info("=" * 60)
    logger.info("Ticker:     %s", config.settings.asset.primary_ticker)
    logger.info("MCP roles: %s", dict(config.settings.mcp.roles))
    logger.info("Dry Run:    %s", config.settings.dry_run.enabled)
    logger.info("Evolution Cron: %s", config.settings.schedule.evolution_cron)
    logger.info("Data Dir:   %s", config.data_dir)
    import os

    mock_time = os.environ.get("EVOTRADER_MOCK_TIME")
    if mock_time:
        logger.info("Mock Time:  %s (SIMULATION ACTIVE)", mock_time)
    logger.info("=" * 60)

    # ── Initialise database ─────────────────────────────────────
    db = Database(config.db_path)
    await db.initialize()

    # If sim_deposit is requested, deposit cash and exit immediately
    if sim_deposit is not None:
        from evotrader.sim import SimBroker

        sim_db_path = config.data_dir / "sim" / "db" / "sim_broker.db"
        sim_broker = SimBroker(sim_db_path)
        await sim_broker.initialize()
        await sim_broker.deposit(sim_deposit)
        logger.info(
            "Successfully deposited $%s to simulated trading account. Exiting.", sim_deposit
        )
        await db.close()
        await sim_broker.close()
        return

    try:
        # ── Setup signal handlers for graceful shutdown ────────
        import signal

        main_task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        registered_signals = []

        def _signal_handler(sig):
            logger.info("Received signal %s — initiating clean shutdown...", sig)
            if main_task is not None:
                main_task.cancel()
            else:
                for task in asyncio.all_tasks(loop):
                    task.cancel()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda s=sig: _signal_handler(s))
                registered_signals.append(sig)
            except NotImplementedError:
                pass

        # ── Initialise algorithm registry ───────────────────────
        from evotrader.algorithms.registry import AlgorithmRegistry

        algo_registry = AlgorithmRegistry(
            config.algorithms_dir,
            is_sim=(config.settings.mode.value == "sim"),
        )
        active_algo = algo_registry.get_active_version()
        logger.info("Active algorithm: %s", active_algo)

        # ── ENGINE BUILD PROVENANCE ────────────────────────────
        # The algorithm VERSION says which config is active; it says nothing
        # about which SOURCE is running. Those came apart on 2026-09-10: the
        # trading process was executing a composite.py that predated the
        # 2026-09-08 renormalization fix by two days, while the repo and the
        # version string both looked correct. It cost three sessions of
        # conclusions drawn against an engine nobody was looking at.
        #
        # Log the resolved module path and source hash next to the version so
        # the running build is identifiable from the log alone.
        from evotrader.algorithms import composite as _composite_mod

        logger.info(
            "Composite engine build: %s (source: %s)",
            _composite_mod.COMPOSITE_SOURCE_FINGERPRINT,
            _composite_mod.__file__,
        )

        # ── Initialise trade journal and metrics ────────────────
        journal = TradeJournal(db)
        metrics = MetricsStore(db)
        from evotrader.db.thought_log import ThoughtLogger

        thought_logger = ThoughtLogger(db)

        # ── Initialise semantic memory ──────────────────────────
        from evotrader.tools.memory import SemanticMemory

        memory = SemanticMemory(config.memory_dir)

        # Load user-provided notes from the configured notes directory
        notes_dir = config.notes_dir
        notes_count = memory.load_notes_from_directory(notes_dir)
        if notes_count > 0:
            logger.info("Loaded %d user notes from %s", notes_count, notes_dir)
        else:
            logger.info(
                "No user notes found. Add .md files to %s to guide the agent.",
                notes_dir,
            )

        # Prune stale memory entries (>90 days) to prevent unbounded growth
        pruned = memory.prune(max_age_days=90)
        if sum(pruned.values()) > 0:
            logger.info("Memory pruning: %s", pruned)

        # ── Create MCP toolsets ─────────────────────────────────
        from evotrader.agents.factory import create_mcp_toolsets

        mcp_toolsets = create_mcp_toolsets(config)
        mcp_toolset = (mcp_toolsets.get("trading") or [None])[0]

        # ── Initialize SimBroker & SimBrokerProxy if in Sim Mode ────
        from evotrader.models.config import TradingMode

        sim_proxy = None
        sim_broker = None
        if config.settings.mode == TradingMode.SIM:
            from evotrader.sim import SimBroker, SimBrokerProxy

            sim_db_path = config.data_dir / "sim" / "db" / "sim_broker.db"
            sim_broker = SimBroker(sim_db_path)
            await sim_broker.initialize()

            # Pass custom config if any
            if hasattr(config.settings, "sim_broker") and config.settings.sim_broker:
                sim_broker.slippage_model = getattr(
                    config.settings.sim_broker, "slippage_model", "spread"
                )
                sim_broker.slippage_bps = getattr(config.settings.sim_broker, "slippage_bps", 5.0)
                sim_broker.auto_close_expired_options = getattr(
                    config.settings.sim_broker, "auto_close_expired_options", True
                )

            sim_proxy = SimBrokerProxy(sim_broker, real_mcp_toolset=mcp_toolset)

            # Patch the MCP toolsets to intercept raw session calls
            if mcp_toolsets:
                for name, toolset_list in mcp_toolsets.items():
                    if name == "trading":
                        for toolset in toolset_list:
                            sim_proxy.attach_to_toolset(toolset)

        # ── Initialize strategy loader ──────────────────────────
        from evotrader.algorithms.loader import StrategyLoader

        strategy_loader = StrategyLoader(config.algorithms_dir / "strategy_manifest.yaml")

        # ── Bind dependencies for agent tools ───────────────────
        bind_dependencies(
            db=db,
            journal=journal,
            metrics=metrics,
            algo_registry=algo_registry,
            config=config,
            memory=memory,
            mcp_toolset=mcp_toolset,
            sim_proxy=sim_proxy,
            strategy_loader=strategy_loader,
        )

        # ── Bind the asset context ──────────────────────────────
        # One source of truth for "what are we trading". Modules that need the
        # ticker read it from here instead of each carrying its own literal
        # default, which is how stale symbols used to survive a switch.
        from evotrader.tools.asset_context import bind_asset_context
        from evotrader.tools.market_hours import bind_extended_hours_tickers

        bind_asset_context(config)
        bind_extended_hours_tickers(config.constitution.trading_rules.extended_hours_tickers)

        # ── Resolve the trading universe ────────────────────────
        # Which symbols can move what we trade. Resolved from live holdings data
        # keyed on the configured ticker(s) — no ETF or constituent list is
        # hardcoded, so changing `asset.primary_ticker` is sufficient.
        await _init_universe(config)

        # ── Initialise evolution engine ─────────────────────────
        from evotrader.db.evolution_log import EvolutionLogStore
        from evotrader.db.signal_attribution import SignalAttributionStore
        from evotrader.evolution.analyser import PerformanceAnalyser
        from evotrader.evolution.code_evolver import CodeEvolver
        from evotrader.evolution.instruction_evolver import InstructionEvolver
        from evotrader.evolution.proposals import ProposalManager
        from evotrader.evolution.tools import bind_evolution_dependencies

        # Ensure evolution state is always shared by pointing to the live DB
        evolution_db_path = config.data_dir / "db" / "evotrader.db"
        if evolution_db_path == db._db_path:
            evolution_db = db
        else:
            evolution_db = Database(evolution_db_path)
            await evolution_db.initialize()

        analyser = PerformanceAnalyser(journal, metrics)
        code_evolver = CodeEvolver(config.project_root, config.algorithms_dir)
        instruction_evolver = InstructionEvolver(
            config.instructions_dir,
            is_sim=(config.settings.mode.value == "sim"),
        )
        proposal_manager = ProposalManager(config.data_dir / "evolution" / "proposals")
        evolution_store = EvolutionLogStore(evolution_db)

        bind_evolution_dependencies(
            analyser=analyser,
            code_evolver=code_evolver,
            instruction_evolver=instruction_evolver,
            algo_registry=algo_registry,
            memory=memory,
            config=config,
            thought_logger=thought_logger,
            proposal_manager=proposal_manager,
            evolution_store=evolution_store,
            # The scored live record: what each witness said, and what price
            # then did. This is the calibration evidence — the backtest only
            # verifies that the engine still works.
            attribution_store=SignalAttributionStore(db),
        )
        logger.info("Evolution engine initialised")

        # ── Create ADK agents ──────────────────────────────────

        # ── Create ADK Runner ───────────────────────────────────
        session_service = InMemorySessionService()
        orchestrator = create_orchestrator_agent(config, mcp_toolsets=mcp_toolsets)
        logger.info(
            "Orchestrator agent created with %d sub-agents",
            len(orchestrator.sub_agents) if orchestrator.sub_agents else 0,
        )
        # Note: market data gathering is now done by the deterministic
        # gather_market_data tool, not the former market_intelligence sub-agent.

        # The factory has already populated mcp_toolset (from mcp_toolsets['trading']).
        # This is the exact instance that ADK will initialise with OAuth, and that
        # sim_proxy is attached to (if in SIM mode).

        # Update the tools module with the active (OAuth-ready) toolset
        from evotrader.agents import tools as tools_module

        tools_module._mcp_toolset = mcp_toolset

        # Create the evolution service (runs separately after market close).
        # Two interchangeable backends — see settings.yaml `evolution.backend`.
        evolution_service = _create_evolution_service(
            config=config,
            session_service=session_service,
            thought_logger=thought_logger,
        )

        adk_app = App(
            name="evotrader",
            root_agent=orchestrator,
            context_cache_config=ContextCacheConfig(ttl_seconds=3600),
        )

        runner = Runner(
            app=adk_app,
            session_service=session_service,
        )

        logger.info("ADK Runner initialised — system ready")
        logger.info("=" * 60)

        # Initial prompt to start the trading cycle
        from google.genai import types

        initial_prompt = types.Content(
            role="user",
            parts=[
                types.Part(
                    text=(
                        "Start a trading cycle. Check market status, then use "
                        "gather_market_data to fetch market data and compute the algo "
                        f"signal for {config.settings.asset.primary_ticker}. In parallel, "
                        "delegate to news_sentiment for sentiment analysis. "
                        "If the buying power is too low for a meaningful equity position "
                        "(e.g., buying_power < current price), call gather_option_chain to "
                        "fetch available option contracts. Then pass ALL of the market data, "
                        "algo signal breakdown, sentiment report, and option chain (if fetched) "
                        "to the strategy agent for a trade decision. "
                        "If a trade is proposed, validate with risk_manager and execute "
                        "if appropriate. Report your findings."
                    )
                )
            ],
        )

        async def run_cycle_task() -> None:
            # Create a session for this trading run
            session = await session_service.create_session(
                app_name="evotrader",
                user_id="trader",
            )
            logger.info("Starting trading cycle...")

            # Link all trades recorded during this cycle to this session
            set_current_session_id(session.id)

            # Reconcile pending limit orders before agent runs
            try:
                fill_result = await check_and_journal_pending_fills()
                if fill_result.get("fills_count"):
                    logger.info(
                        "Cycle start: journaled %d pending fills, cancelled %d stale orders",
                        fill_result["fills_count"],
                        fill_result.get("cancelled_count", 0),
                    )
            except Exception as e:
                logger.warning("Failed to check pending fills at cycle start: %s", e)

            # Reconcile live Robinhood orders (rejected, cancelled, filled)
            try:
                recon_result = await reconcile_pending_orders()
                if recon_result.get("reconciled_count"):
                    logger.info(
                        "Cycle start: reconciled %d live orders (still pending: %d)",
                        recon_result["reconciled_count"],
                        recon_result.get("still_pending_count", 0),
                    )
            except Exception as e:
                logger.warning("Failed to reconcile pending orders at cycle start: %s", e)

            # Insert a running state record for this trading cycle
            try:
                await thought_logger.record_run_start(session.id, "TRADING")
            except Exception as db_err:
                logger.warning("Could not insert RUNNING status into cycle_runs: %s", db_err)

            # Clear thoughts list in web app state if running
            from evotrader.web.server import _active_app, broadcast_sse_event

            if _active_app:
                _active_app.state.thoughts = []
                broadcast_sse_event("clear_thoughts", {})

            cycle_start_time = time.monotonic()
            stages_seen = set()
            last_strategy_text = ""
            cycle_error = None
            was_cancelled = False

            # ── One resume on a transient provider fault ─────────────
            # See agents/provider_retry.py. These three are what decide whether
            # resuming is safe, so they are tracked from the event stream.
            open_calls: Counter[str] = Counter()  # orchestrator calls awaiting a response
            last_author: str | None = None
            provider_resume: dict | None = None

            def _resume_blocked_reason() -> str | None:
                if _ORDER_PATH_STAGES & stages_seen:
                    return "the order path had started, so an order may already exist"
                if +open_calls:
                    return f"tool call(s) never answered: {sorted(+open_calls)}"
                if last_author not in (None, "orchestrator"):
                    return f"the failure was inside {last_author}"
                return None

            def _resume_prompt(exc: BaseException) -> types.Content:
                keep = (
                    "The market data above is the snapshot this cycle is scored "
                    "on; do not gather it again. "
                    if "gather_market_data" in stages_seen
                    else ""
                )
                return types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            text=(
                                "Resuming this cycle. The previous step stopped on a temporary "
                                f"model-provider error ({short_error(exc)}) and has been "
                                "retried after a pause. Everything already gathered in this "
                                f"session is still current. {keep}Continue from where the "
                                "cycle stopped: if the strategy agent has not run yet, "
                                "delegate to it now with the complete data above."
                            )
                        )
                    ],
                )

            async def _record_resume(exc: BaseException, delay: float) -> None:
                nonlocal provider_resume
                provider_resume = {
                    "error": short_error(exc),
                    "delay_seconds": delay,
                    "stages_completed_before": [s for s in _PIPELINE_STAGES if s in stages_seen],
                }
                try:
                    await thought_logger.record_event(
                        session_id=session.id,
                        agent_name="orchestrator",
                        event_type="runtime",
                        content=(
                            f"[runtime] model provider unavailable ({short_error(exc)}); "
                            f"resuming this cycle once in {delay:.0f} s"
                        ),
                        meta=provider_resume,
                    )
                except Exception as db_err:
                    logger.warning("Could not persist provider-resume row: %s", db_err)

            event_count = 0
            try:
                async for event in run_with_one_resume(
                    lambda message: runner.run_async(
                        user_id="trader",
                        session_id=session.id,
                        new_message=message,
                    ),
                    initial_prompt,
                    delay_seconds=config.settings.schedule.provider_retry_delay_seconds,
                    blocked_reason=_resume_blocked_reason,
                    resume_message=_resume_prompt,
                    on_resume=_record_resume,
                ):
                    event_count += 1
                    max_allowed = config.settings.schedule.max_cycle_events
                    if event_count > max_allowed:
                        logger.error(
                            "❌ Event limit exceeded: %d events (max allowed: %d). Systematically terminating cycle.",
                            event_count,
                            max_allowed,
                        )
                        raise RuntimeError(
                            f"Systematic termination: Cycle exceeded the maximum allowed event limit of {max_allowed} events."
                        )
                    author = getattr(event, "author", "unknown")
                    last_author = author

                    # 1. Extract text thoughts
                    text_content = ""
                    if hasattr(event, "content") and event.content and event.content.parts:
                        parts_text = []
                        for part in event.content.parts:
                            if hasattr(part, "text") and part.text:
                                parts_text.append(part.text)
                        if parts_text:
                            text_content = "\n".join(parts_text)

                    if text_content:
                        logger.info("Agent [%s]: %s", author, text_content)
                        if author == "strategy":
                            # API runtime: strategy authors its own events.
                            last_strategy_text = text_content

                        thought = {
                            "agent": author,
                            "type": "thought",
                            "content": text_content,
                            "timestamp": datetime.now(UTC).isoformat(),
                            "session_id": session.id,
                        }
                        if _active_app:
                            if not hasattr(_active_app.state, "thoughts"):
                                _active_app.state.thoughts = []
                            _active_app.state.thoughts.append(thought)
                            broadcast_sse_event("thought", thought)

                        # Save to SQLite db
                        try:
                            await thought_logger.record_event(
                                session_id=session.id,
                                agent_name=author,
                                event_type="thought",
                                content=text_content,
                            )
                        except Exception as db_err:
                            logger.warning("Could not persist agent thought to DB: %s", db_err)

                    # 2. Extract function calls
                    func_calls = (
                        event.get_function_calls() if hasattr(event, "get_function_calls") else []
                    )
                    for fc in func_calls:
                        fc_name = getattr(fc, "name", "unknown")
                        fc_args = getattr(fc, "args", {})

                        logger.info("Agent [%s] calling tool: %s", author, fc_name)

                        # Only orchestrator-authored calls register a stage.
                        # Correct today — every pipeline stage is reached by an
                        # orchestrator tool call or a transfer_to_agent it
                        # issues. But it is a silent assumption: a stage reached
                        # by any other route would never register, and the cycle
                        # would report `partial` while having done the work.
                        # That is exactly how the two 2026-09-15 `partial` rows
                        # arose before `_stage_for_call` learned to recognise
                        # transfer_to_agent. If a new routing path is added,
                        # this is the line that must learn about it.
                        if author == "orchestrator":
                            stage = _stage_for_call(fc_name, fc_args)
                            if stage:
                                stages_seen.add(stage)
                            open_calls[getattr(fc, "id", None) or fc_name] += 1

                        tool_meta = get_tool_metadata(fc_name, config)
                        tool_thought = {
                            "agent": author,
                            "type": "tool_call",
                            "tool_name": fc_name,
                            "args": fc_args,
                            "tool_info": tool_meta,
                            "timestamp": datetime.now(UTC).isoformat(),
                            "session_id": session.id,
                        }
                        if _active_app:
                            if not hasattr(_active_app.state, "thoughts"):
                                _active_app.state.thoughts = []
                            _active_app.state.thoughts.append(tool_thought)
                            broadcast_sse_event("thought", tool_thought)

                        # Save to SQLite db
                        try:
                            await thought_logger.record_event(
                                session_id=session.id,
                                agent_name=author,
                                event_type="tool_call",
                                content=fc_name,
                                meta={"args": fc_args, "tool_info": tool_meta},
                            )
                        except Exception as db_err:
                            logger.warning("Could not persist tool call to DB: %s", db_err)

                    # 3. Extract function responses
                    func_responses = (
                        event.get_function_responses()
                        if hasattr(event, "get_function_responses")
                        else []
                    )
                    for fr in func_responses:
                        fr_name = getattr(fr, "name", "unknown")
                        fr_resp = getattr(fr, "response", {})

                        # CLI runtime: the proposal comes back as the `strategy`
                        # TOOL's response, not as a strategy-authored event.
                        # Both shapes must feed the stall detector or it would
                        # go blind on exactly the runtime that broke.
                        if fr_name == "strategy":
                            last_strategy_text = str(fr_resp)
                        if author == "orchestrator":
                            open_calls[getattr(fr, "id", None) or fr_name] -= 1

                        tool_meta = get_tool_metadata(fr_name, config)
                        resp_thought = {
                            "agent": author,
                            "type": "tool_response",
                            "tool_name": fr_name,
                            "response": fr_resp,
                            "tool_info": tool_meta,
                            "timestamp": datetime.now(UTC).isoformat(),
                            "session_id": session.id,
                        }
                        if _active_app:
                            if not hasattr(_active_app.state, "thoughts"):
                                _active_app.state.thoughts = []
                            _active_app.state.thoughts.append(resp_thought)
                            broadcast_sse_event("thought", resp_thought)

                        # Save to SQLite db
                        try:
                            await thought_logger.record_event(
                                session_id=session.id,
                                agent_name=author,
                                event_type="tool_response",
                                content=fr_name,
                                meta={"response": fr_resp, "tool_info": tool_meta},
                            )
                        except Exception as db_err:
                            logger.warning("Could not persist tool response to DB: %s", db_err)

            except asyncio.CancelledError:
                was_cancelled = True
                logger.info("Trading cycle cancelled (shutdown in progress).")
                try:
                    await thought_logger.record_run_completion(
                        session_id=session.id, status="FAILED", error="Trading cycle cancelled."
                    )
                except Exception as db_err:
                    logger.warning("Could not update CANCELLED status in cycle_runs: %s", db_err)
                raise
            except Exception as e:
                cycle_error = str(e)
                raise
            finally:
                # Always reset session linkage
                set_current_session_id(None)
                if not was_cancelled:
                    duration_ms = int((time.monotonic() - cycle_start_time) * 1000)

                    if cycle_error:
                        status = "error"
                    elif _MANDATORY_STAGES.issubset(stages_seen):
                        status = "complete"
                    else:
                        status = "partial"

                    completed_list = [s for s in _PIPELINE_STAGES if s in stages_seen]
                    skipped_list = [s for s in _PIPELINE_STAGES if s not in stages_seen]

                    # ── STALL DETECTOR ────────────────────────────────
                    # `complete` stays correct for a genuine reasoned no-trade —
                    # ending at `strategy` is legitimate. But if strategy NAMED
                    # orders and the order path was skipped, no order could have
                    # been placed regardless of intent. That is an
                    # infrastructure fault wearing a healthy status, and it must
                    # be queryable: a diagnostic that only reaches the
                    # application log does not exist.
                    _missing_order_path = _ORDER_PATH_STAGES - stages_seen
                    if (
                        "strategy" in stages_seen
                        and _missing_order_path
                        and _ORDER_INTENT_RE.search(last_strategy_text or "")
                    ):
                        _missing = ", ".join(sorted(_missing_order_path))
                        logger.error(
                            "PIPELINE STALL: strategy named orders but the cycle "
                            "never reached %s. No order could have been placed "
                            "this cycle.",
                            _missing,
                        )
                        try:
                            await thought_logger.record_event(
                                session_id=session.id,
                                agent_name="orchestrator",
                                event_type="pipeline_stall",
                                content=(
                                    "Strategy output named one or more orders, but "
                                    f"the cycle never reached {_missing}. This is "
                                    "an INFRASTRUCTURE FAULT, not a decision to "
                                    "stay flat."
                                ),
                                meta={
                                    "stages_completed": completed_list,
                                    "stages_skipped": skipped_list,
                                },
                            )
                        except Exception as db_err:
                            logger.warning("Could not persist pipeline stall row: %s", db_err)

                    # Update status in cycle_runs
                    try:
                        db_status = "SUCCESS" if status in ("complete", "partial") else "FAILED"
                        await thought_logger.record_run_completion(
                            session_id=session.id,
                            status=db_status,
                            error=cycle_error,
                            summary=f"Stages completed: {', '.join(completed_list)}"
                            if completed_list
                            else "No stages completed.",
                        )
                    except Exception as db_err:
                        logger.warning("Could not update status in cycle_runs: %s", db_err)

                    complete_event = {
                        "agent": "orchestrator",
                        "type": "cycle_complete",
                        "status": status,
                        "stages_completed": completed_list,
                        "stages_skipped": skipped_list,
                        "duration_ms": duration_ms,
                        "error": cycle_error,
                        "provider_resume": provider_resume,
                        "timestamp": datetime.now(UTC).isoformat(),
                        "session_id": session.id,
                    }

                    if _active_app:
                        if not hasattr(_active_app.state, "thoughts"):
                            _active_app.state.thoughts = []
                        _active_app.state.thoughts.append(complete_event)
                        broadcast_sse_event("thought", complete_event)

                    try:
                        await thought_logger.record_event(
                            session_id=session.id,
                            agent_name="orchestrator",
                            event_type="cycle_complete",
                            content=f"Cycle finished with status: {status}",
                            meta={
                                "status": status,
                                "stages_completed": completed_list,
                                "stages_skipped": skipped_list,
                                "duration_ms": duration_ms,
                                "error": cycle_error,
                                # Set only when the cycle was resumed after a
                                # provider fault: what failed and what had run.
                                "provider_resume": provider_resume,
                            },
                        )
                    except Exception as db_err:
                        logger.warning("Could not persist cycle completion to DB: %s", db_err)

                    logger.info("Trading cycle complete. Status: %s", status)

                    # ── Post-cycle reconciliation ────────────────────────
                    # Orders placed during this cycle may have already
                    # filled/been rejected by the time the cycle ends.
                    # Reconcile now so the journal reflects reality
                    # immediately instead of waiting for the next cycle.
                    try:
                        from evotrader.models.config import TradingMode

                        if config.settings.mode == TradingMode.SIM:
                            post_fills = await check_and_journal_pending_fills()
                            if post_fills.get("fills_count"):
                                logger.info(
                                    "Post-cycle: journaled %d pending fills",
                                    post_fills["fills_count"],
                                )
                        else:
                            post_recon = await reconcile_pending_orders()
                            if post_recon.get("reconciled_count"):
                                logger.info(
                                    "Post-cycle: reconciled %d orders",
                                    post_recon["reconciled_count"],
                                )
                    except Exception as e:
                        logger.warning("Post-cycle reconciliation failed: %s", e)

                    # ── Is every held position actually covered? ─────────
                    # A refused protective order leaves no trace in the book.
                    # On 2026-09-21 the take-profit was rejected, the cycle
                    # reported "protective stop-loss placed", and the position
                    # ran all day with no target. Nothing compared intent
                    # against what was resting. This observes only — the repair
                    # is the next cycle's, which the instructions already cover.
                    # Attach any protective order that was journaled before its
                    # entry filled — FIRST, so the audit reads the corrected
                    # journal rather than the one it is about to misread.
                    try:
                        await journal.relink_unmatched_protective_orders()
                    except Exception as e:
                        logger.warning("Protective-order relink failed: %s", e)
                    try:
                        from evotrader.db.protection_audit import audit_protection

                        await audit_protection(journal, thought_logger, session.id)
                    except Exception as e:
                        logger.warning("Protection audit failed: %s", e)

                    # ── Score the attribution record ─────────────────────
                    # Every cycle writes what each witness said; this is what
                    # writes back what the market then did. Without it the
                    # table is a diary, not evidence — which is exactly what it
                    # was until 2026-09-18: 74 rows, none scored, because
                    # SignalAttributionStore.score() had no caller anywhere.
                    #
                    # Cheap by construction: it only touches rows whose horizon
                    # has elapsed, so it is a no-op on almost every cycle, and a
                    # data outage leaves rows pending rather than failing.
                    try:
                        from evotrader.agents.tools import _fetch_daily_candles
                        from evotrader.db.signal_attribution import (
                            SignalAttributionStore,
                        )
                        from evotrader.evolution.attribution_scorer import (
                            AttributionScorer,
                        )

                        store = SignalAttributionStore(db)
                        # A cycle that never reached the strategy agent (a
                        # provider outage, a crash) still computed and stored
                        # the algorithm's call. Record it, flagged agent_absent,
                        # so the control group has no infra-shaped holes.
                        await store.backfill_algo_only_rows()
                        # Every channel's vote, from the snapshot this cycle
                        # stored — so the calibration report can score each
                        # channel on its own calls, not only the one that led.
                        await store.backfill_channel_votes()
                        scored = await AttributionScorer(store, _fetch_daily_candles).run()
                        if scored.get("scored") or scored.get("backfilled_5d"):
                            logger.info(
                                "Post-cycle: scored %d attribution row(s), "
                                "backfilled the 5-day horizon on %d",
                                scored["scored"],
                                scored["backfilled_5d"],
                            )
                    except Exception as e:
                        logger.warning("Attribution scoring failed: %s", e)

        # ── Web Dashboard Launch OR Direct Run ───────────────────
        if dashboard:
            import uvicorn

            from evotrader.web.server import create_app

            app = create_app(
                db=db,
                journal=journal,
                metrics=metrics,
                mcp_toolset=mcp_toolset,
                config=config,
                runner_fn=run_cycle_task,
                memory=memory,
                evolution_service=evolution_service,
            )

            port = int(os.environ.get("EVOTRADER_PORT", "8080"))
            server = uvicorn.Server(
                uvicorn.Config(
                    app=app,
                    host=console_host(),
                    port=port,
                    log_level="warning",
                )
            )
            # Patch uvicorn's signal capturing so it doesn't overwrite our loop signal handlers
            import contextlib

            server.capture_signals = contextlib.nullcontext

            logger.info("=" * 60)
            logger.info("  Web Console running at http://127.0.0.1:%d", port)
            logger.info("=" * 60)
            # After a broker sign-in, send the browser back here.
            from evotrader.mcp.oauth import set_return_url

            set_return_url(f"http://127.0.0.1:{port}/")
            await server.serve()
        else:
            await run_cycle_task()

    finally:
        # Remove registered signal handlers
        if "registered_signals" in locals() and "loop" in locals():
            for sig in registered_signals:
                try:
                    loop.remove_signal_handler(sig)
                except Exception:
                    pass

        if "sim_broker" in locals() and sim_broker:
            logger.info("Closing SimBroker...")
            try:
                await sim_broker.close()
            except Exception as e:
                logger.warning("Error closing SimBroker: %s", e)

        if "mcp_toolsets" in locals() and mcp_toolsets:
            logger.info("Closing MCP toolsets...")
            for name, toolset_list in mcp_toolsets.items():
                for toolset in toolset_list:
                    try:
                        await toolset.close()
                    except Exception as e:
                        logger.warning("Error closing MCP toolset '%s': %s", name, e)
        if "evolution_db" in locals() and evolution_db and evolution_db is not db:
            await evolution_db.close()
        await db.close()
        logger.info("EvoTrader shut down cleanly.")


_THIS_COMPUTER = {"127.0.0.1", "localhost", "::1"}


def console_host() -> str:
    """Where the web console listens: only this computer, unless EVOTRADER_HOST says otherwise.

    Set EVOTRADER_HOST=0.0.0.0 to reach the console from other devices (a
    phone, a laptop over Tailscale). The console can approve trades, so that
    refuses to start without DASHBOARD_PASSWORD.
    """
    import os

    host = os.environ.get("EVOTRADER_HOST", "").strip() or "127.0.0.1"
    if host not in _THIS_COMPUTER and not os.environ.get("DASHBOARD_PASSWORD"):
        raise SystemExit(
            f"EVOTRADER_HOST={host} would let other devices reach the console, but "
            "DASHBOARD_PASSWORD is not set. Set a console password (./run.sh setup) "
            "or leave EVOTRADER_HOST unset to keep the console on this computer."
        )
    return host


def run() -> None:
    """Synchronous wrapper for the async entry point."""
    import argparse
    import os

    parser = argparse.ArgumentParser(description="EvoTrader Trading System")
    parser.add_argument(
        "--mode",
        choices=["live", "sim"],
        help="Trading execution mode (live or sim). Overrides settings.yaml.",
    )
    parser.add_argument(
        "--mock-time",
        nargs="?",
        const="2026-06-17T10:00:00-04:00",
        help="Simulate a custom system time (ISO format). If passed without value, defaults to Wednesday 10:00 AM ET.",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Start the web console dashboard interface",
    )
    parser.add_argument(
        "--sim-deposit",
        type=float,
        help="Deposit funds into the simulated brokerage account (USD).",
    )
    parser.add_argument(
        "--data-dir",
        help="Data folder to use (settings, instructions, journal, keys). "
        "Overrides EVOTRADER_DATA_DIR; default: data/ in the project.",
    )

    args, _ = parser.parse_known_args()

    if args.data_dir:
        from pathlib import Path

        from evotrader import paths

        # Through the environment, so every module and any child process
        # resolves the same folder.
        os.environ[paths.DATA_DIR_ENV] = str(Path(args.data_dir).expanduser().resolve())

    # A data folder that was never set up has no settings, no instructions and no
    # algorithm: the app would run on built-in defaults with instruction-less
    # agents. Setup seeds the folder from starter_data/ and asks for the keys.
    from evotrader import paths as _paths

    data_folder = _paths.data_dir()
    if not (data_folder / "settings.yaml").is_file():
        print(
            f"No settings in {data_folder} yet.\n"
            "Run ./run.sh setup first: it creates your data folder from the starter "
            "data and asks for your AI provider and keys (about a minute).",
            file=sys.stderr,
        )
        sys.exit(1)

    # Safety validation: cannot run live trading with a mocked/simulated clock
    if args.mode == "live" and args.mock_time:
        parser.error(
            "Safety Check: Cannot run in live trading mode with a mocked/simulated clock (--mock-time)."
        )

    if args.mock_time:
        os.environ["EVOTRADER_MOCK_TIME"] = args.mock_time

    mode_override = None
    if args.mode:
        mode_override = args.mode
    elif args.mock_time:
        mode_override = "sim"

    # If depositing, force mode to sim
    if args.sim_deposit is not None:
        mode_override = "sim"

    try:
        asyncio.run(
            start(
                dashboard=args.dashboard,
                mode_override=mode_override,
                sim_deposit=args.sim_deposit,
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Interrupted by user — shutting down.")
        sys.exit(0)


if __name__ == "__main__":
    run()
