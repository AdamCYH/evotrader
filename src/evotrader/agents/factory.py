"""Agent factory — creates and configures all EvoTrader agents.

Uses Google ADK's ``LlmAgent`` with LiteLLM for multi-model support.
Each specialist agent is configured with:
- A specific model (Gemini, Claude, or GPT — configurable per agent)
- Custom tools relevant to its role
- System instructions loaded from versioned markdown files
- Appropriate MCP server access

The factory also handles dependency injection and lifecycle management.
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.models import Gemini
from google.adk.models.lite_llm import LiteLlm, LiteLLMClient
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.mcp_tool import (
    McpToolset,
    SseConnectionParams,
    StdioConnectionParams,
    StreamableHTTPConnectionParams,
)
from google.adk.tools.mcp_tool.mcp_tool import McpTool
from google.genai import types

from evotrader.agents import instructions
from evotrader.agents.anthropic_cache import (
    apply_prompt_caching,
    is_anthropic_model,
    log_cache_usage,
)
from evotrader.agents.instruction_context import (
    build_instruction_context,
    render_instructions,
)
from evotrader.agents.temporal_context import build_temporal_context
from evotrader.agents.tools import (
    assess_order_book,
    check_option_risk_limits,
    check_risk_limits,
    compute_risk_budget,
    gather_market_data,
    gather_option_chain,
    get_active_algorithm,
    get_earnings_history,
    get_market_status,
    get_memory_stats,
    get_open_positions,
    get_performance_summary,
    get_ticker_snapshot,
    get_trade_history,
    list_algorithm_versions,
    log_signal_attribution,
    query_past_trades,
    query_user_notes,
    reconcile_broker_pnl,
    reconcile_pending_orders,
    record_trade,
    store_learning,
    update_trading_handoff,
)
from evotrader.config import AppConfig
from evotrader.mcp import get_mcp_provider
from evotrader.mcp.response_curators import curate_response

logger = logging.getLogger(__name__)


class CachingLiteLLMClient(LiteLLMClient):
    """LiteLLM client that inserts Anthropic prompt-cache breakpoints.

    ADK exposes ``LiteLlm.llm_client`` as a swappable field, which is a far
    sturdier hook than patching ``litellm.acompletion`` globally: ADK imports
    that symbol into its own module namespace lazily, so a global patch only
    lands if it happens to run before the first completion call.

    Breakpoint placement lives in :mod:`evotrader.agents.anthropic_cache`.
    """

    def __init__(self, caching: Any) -> None:
        super().__init__()
        self._caching = caching

    async def acompletion(self, model, messages, tools, **kwargs):  # type: ignore[no-untyped-def]
        cfg = self._caching
        if getattr(cfg, "enabled", True):
            apply_prompt_caching(
                model,
                messages,
                kwargs,
                message_breakpoints=getattr(cfg, "message_breakpoints", 2),
                min_cacheable_chars=getattr(cfg, "min_cacheable_chars", 4000),
                require_multi_turn=getattr(cfg, "require_multi_turn", True),
                ttl=getattr(cfg, "ttl", "5m"),
            )
        response = await super().acompletion(model=model, messages=messages, tools=tools, **kwargs)
        log_cache_usage(model, response)
        return response


# ADK's default model identifier
_DEFAULT_MODEL = "gemini-2.5-flash"


def _resolve_model(config: AppConfig, agent_name: str) -> Gemini | LiteLlm:
    """Resolve the model for a given agent.

    If the model string starts with 'openai/' or 'anthropic/', wraps it
    in a ``LiteLlm`` adapter for multi-provider support. Otherwise,
    returns a ``Gemini`` instance configured with automatic retry.

    Args:
        config: Application configuration.
        agent_name: The agent name matching a field in ``ModelConfig``.

    Returns:
        ``Gemini`` or ``LiteLlm`` instance.
    """
    model_id = config.settings.active_model.for_agent(agent_name) or _DEFAULT_MODEL

    # If the model uses a provider prefix, use LiteLLM
    if "/" in model_id and not model_id.startswith("gemini"):
        if is_anthropic_model(model_id):
            return LiteLlm(
                model=model_id,
                llm_client=CachingLiteLLMClient(config.settings.caching),
            )
        return LiteLlm(model=model_id)

    return Gemini(
        model=model_id,
        retry_options=types.HttpRetryOptions(
            attempts=4,
            initial_delay=1.0,
            max_delay=10.0,
        ),
    )


def _load_instructions(agent_name: str, config: AppConfig) -> str:
    """Load an agent's instructions and fill in the configuration placeholders.

    Reads from ``instructions_dir/{agent_name}/active.txt`` (or ``active_sim.txt``)
    → ``{version}.md``. All instructions are file-based; there are no inline
    fallbacks.

    Placeholders (``{{PRIMARY_TICKER}}``, ``{{BEARISH_VEHICLE}}``, …) are
    documented in :mod:`evotrader.agents.instruction_context`. Instruction
    files must not name a ticker literally — that is what makes switching
    instrument a settings change rather than a prose rewrite.

    Args:
        agent_name: Agent identifier (e.g., 'orchestrator', 'strategy').
        config: Centralised application configuration.

    Returns:
        System instruction string with configuration substituted.
    """
    text = instructions.load(
        agent_name,
        config.instructions_dir,
        is_sim=(config.settings.mode.value == "sim"),
    )
    return render_instructions(text, build_instruction_context(config))


class SanitizedMcpTool(McpTool):
    """McpTool subclass that sanitizes arguments before MCP dispatch.

    Anthropic models (via LiteLLM) sometimes wrap tool call arguments in
    a spurious ``{"arguments": {...}}`` envelope.  The MCP server rejects
    this because its JSON schema has ``additionalProperties: false``.

    Additionally, LLM models sometimes hallucinate extra parameters that
    the tool doesn't accept (e.g. ``horizon`` on ``FEDERAL_FUNDS_RATE``).
    This subclass strips any properties not declared in the tool's JSON
    schema when ``additionalProperties`` is false.

    See: https://github.com/BerriAI/litellm/issues/7305
    """

    # Injected by the factory. Defaults keep a directly-constructed
    # SanitizedMcpTool behaving sensibly (no curation, no backstop).
    _curation_config: Any = None
    _app_config: Any = None
    _mcp_roles: tuple[str, ...] = ()

    async def run_async(self, *, args: dict, tool_context) -> Any:
        args = _sanitize_mcp_args(args)
        args = _strip_unknown_params(args, self.raw_mcp_tool)
        args = _prefer_latest_sort(args, self.raw_mcp_tool, self._curation_config)
        result = await super().run_async(args=args, tool_context=tool_context)
        # Shape unbounded payloads on receipt. Some endpoints (Alpha Vantage's
        # economic indicators) have no date-range parameter, so asking for less
        # is not an option — see evotrader.mcp.response_curators.
        return curate_response(
            getattr(self, "name", ""),
            args,
            result,
            self._curation_config,
            self._app_config,
            self._mcp_roles,
        )


def _prefer_latest_sort(args: dict, raw_mcp_tool: Any, curation: Any) -> dict:
    """Default a news-style tool to recency ordering when it supports it.

    These feeds rank by *relevance* by default, which in practice returned 50
    articles spanning 47 days when only 6 were inside the ~5-session window the
    news agent's instructions treat as unpriced. Sorting by recency is a strict
    improvement: the same number of articles, far more of them current.

    Driven entirely by the tool's own JSON schema — a ``sort`` parameter whose
    enum offers ``LATEST`` — so it self-disables on any server that doesn't
    support it and nothing provider-specific is assumed. An explicit ``sort``
    from the model always wins.
    """
    if curation is not None and not getattr(curation, "news_prefer_latest", True):
        return args
    if not isinstance(args, dict) or "sort" in args or not raw_mcp_tool:
        return args

    try:
        schema = raw_mcp_tool.inputSchema
        options = schema["properties"]["sort"]["enum"]
    except (AttributeError, KeyError, TypeError):
        return args

    if not isinstance(options, list):
        return args
    for option in options:
        if isinstance(option, str) and option.upper() == "LATEST":
            args = {**args, "sort": option}
            logger.debug(
                "Defaulting %s to sort=%s for recency",
                getattr(raw_mcp_tool, "name", "?"),
                option,
            )
            break
    return args


def _sanitize_mcp_args(args: dict) -> dict:
    """Unwrap the spurious ``{"arguments": {...}}`` envelope.

    Anthropic models sometimes echo the OpenAI tool-call schema structure
    back into the arguments payload, producing::

        {"arguments": {"symbol": "QQQ"}}   # wrong

    instead of::

        {"symbol": "QQQ"}                  # correct

    This only triggers when ``arguments`` is the *sole* top-level key and
    its value is a dict, so it won't collide with a legitimate parameter
    named ``arguments``.
    """
    if (
        isinstance(args, dict)
        and len(args) == 1
        and "arguments" in args
        and isinstance(args["arguments"], dict)
    ):
        logger.debug("Sanitizing spurious 'arguments' wrapper from MCP tool args")
        return args["arguments"]
    return args


def _strip_unknown_params(args: dict, raw_mcp_tool) -> dict:
    """Strip parameters not declared in the tool's JSON schema.

    When ``additionalProperties`` is false in the tool schema, the MCP
    server rejects unexpected keys.  LLM agents sometimes hallucinate
    extra params (e.g. ``horizon`` for ``FEDERAL_FUNDS_RATE``).  Rather
    than failing, we silently drop them and log a warning.
    """
    if not args or not raw_mcp_tool:
        return args

    try:
        schema = raw_mcp_tool.inputSchema
        if not schema or not isinstance(schema, dict):
            return args

        # Only strip when additionalProperties is explicitly false
        if schema.get("additionalProperties") is not False:
            return args

        allowed = set(schema.get("properties", {}).keys())
        if not allowed:
            return args

        extra = set(args.keys()) - allowed
        if extra:
            logger.warning(
                "Stripping hallucinated params %s from MCP tool '%s' (allowed: %s)",
                extra,
                getattr(raw_mcp_tool, "name", "?"),
                allowed,
            )
            return {k: v for k, v in args.items() if k in allowed}
    except Exception:
        pass  # Don't crash on schema introspection failures

    return args


def _provider_roles(config: AppConfig, provider_name: str) -> tuple[str, ...]:
    """Which logical roles a provider serves, from ``mcp.roles``.

    Inverts the configured mapping rather than hardcoding which provider is
    "research", so adding or renaming a provider needs no code change.
    """
    roles: list[str] = []
    mapping = getattr(getattr(config.settings, "mcp", None), "roles", None) or {}
    for role, providers in mapping.items():
        names = [providers] if isinstance(providers, str) else list(providers or [])
        if provider_name in names:
            roles.append(role)
    return tuple(roles)


class LoggingMcpToolset(McpToolset):
    """McpToolset subclass that uses SanitizedMcpTool and logs connection errors to the DB and UI."""

    def __init__(self, *args, provider_name: str, config: AppConfig, **kwargs):
        super().__init__(*args, **kwargs)
        self.provider_name = provider_name
        self.config = config

    async def get_tools(self, readonly_context=None):
        try:
            tools = await super().get_tools(readonly_context)
            # Replace vanilla McpTool instances with SanitizedMcpTool
            # so every MCP tool on every agent auto-fixes argument nesting.
            sanitized = []
            for tool in tools:
                if isinstance(tool, McpTool) and not isinstance(tool, SanitizedMcpTool):
                    wrapped = SanitizedMcpTool(
                        mcp_tool=tool.raw_mcp_tool,
                        mcp_session_manager=tool._mcp_session_manager,
                    )
                    wrapped._curation_config = self.config.settings.mcp.curation
                    wrapped._app_config = self.config
                    wrapped._mcp_roles = _provider_roles(self.config, self.provider_name)
                    sanitized.append(wrapped)
                else:
                    sanitized.append(tool)
            return sanitized
        except Exception as e:
            session_id = None
            agent_name = "system"
            if readonly_context:
                try:
                    session_id = readonly_context.session.id
                    agent_name = readonly_context.agent_name
                except Exception:
                    pass

            logger.warning(
                "LoggingMcpToolset: get_tools failed for provider '%s': %s",
                self.provider_name,
                e,
            )

            try:
                from datetime import UTC, datetime

                from evotrader.db.connection import Database
                from evotrader.db.thought_log import ThoughtLogger
                from evotrader.web.server import _active_app, broadcast_sse_event

                tool_name = f"connect_{self.provider_name}"
                args = {"url": getattr(self._connection_params, "url", None) or "stdio"}

                address = getattr(self._connection_params, "url", None) or "stdio"
                tool_info = {
                    "source": "mcp",
                    "provider": self.provider_name,
                    "provider_label": self.provider_name.replace("_", " ").title(),
                    "address": address,
                    "transport": self._connection_params.__class__.__name__.replace(
                        "ConnectionParams", ""
                    ).lower()
                    if hasattr(self, "_connection_params")
                    else "unknown",
                    "is_write": False,
                }

                # 1. Record tool_call
                tool_call_event = {
                    "agent": agent_name,
                    "type": "tool_call",
                    "tool_name": tool_name,
                    "args": args,
                    "tool_info": tool_info,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "session_id": session_id,
                }

                if _active_app:
                    if not hasattr(_active_app.state, "thoughts"):
                        _active_app.state.thoughts = []
                    _active_app.state.thoughts.append(tool_call_event)
                    broadcast_sse_event("thought", tool_call_event)

                # 2. Record tool_response with error details
                error_msg = str(e)
                response = {"error": error_msg, "isError": True, "success": 0}

                tool_response_event = {
                    "agent": agent_name,
                    "type": "tool_response",
                    "tool_name": tool_name,
                    "response": response,
                    "tool_info": tool_info,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "session_id": session_id,
                }

                if _active_app:
                    _active_app.state.thoughts.append(tool_response_event)
                    broadcast_sse_event("thought", tool_response_event)

                # Persist to database
                db = None
                should_close_db = False
                if (
                    _active_app
                    and hasattr(_active_app.state, "db")
                    and _active_app.state.db._db_path == self.config.db_path
                ):
                    db = _active_app.state.db
                else:
                    db = Database(self.config.db_path)
                    await db.initialize()
                    should_close_db = True

                try:
                    logger_db = ThoughtLogger(db)
                    await logger_db.record_event(
                        session_id=session_id,
                        agent_name=agent_name,
                        event_type="tool_call",
                        content=tool_name,
                        meta={"args": args, "tool_info": tool_info},
                    )
                    await logger_db.record_event(
                        session_id=session_id,
                        agent_name=agent_name,
                        event_type="tool_response",
                        content=tool_name,
                        meta={"response": response, "success": 0, "tool_info": tool_info},
                    )
                finally:
                    if should_close_db and db:
                        await db.close()

            except Exception as inner_err:
                logger.error(
                    "LoggingMcpToolset: Failed to write connection error thoughts to DB: %s",
                    inner_err,
                )

            # Return empty tool list instead of crashing the agent.
            # This allows the agent to continue with tools from other
            # providers that connected successfully.
            return []


class FilteredMcpToolset(BaseToolset):
    """Toolset wrapper that delegates to an underlying toolset but filters exposed tools for the LLM.

    Used by the Orchestrator to ensure the MCP session is initialized without exposing
    dozens of unused trading tools to the orchestrator's LLM context window.
    """

    def __init__(self, wrapped_toolset: Any, allowed_tool_names: set[str]) -> None:
        super().__init__()
        self._wrapped_toolset = wrapped_toolset
        self.allowed_tool_names = allowed_tool_names

    async def get_tools(self, readonly_context=None):
        tools = await self._wrapped_toolset.get_tools(readonly_context)
        return [t for t in tools if getattr(t, "name", None) in self.allowed_tool_names]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped_toolset, name)

    async def close(self) -> None:
        # Wrapped toolset lifecycle managed externally
        pass


class AgentToolFilter(BaseToolset):
    """Hides tools an agent cannot use, by configured name patterns.

    Sibling of :class:`FilteredMcpToolset`, which takes an explicit allowlist and
    suits the orchestrator's two-tool case. This one is exclusion-based because
    it is applied to agents on the order path, where the cost of hiding too much
    (an agent unable to place a trade) is far worse than the cost of hiding too
    little (a larger prompt). A tool matching no pattern stays visible.

    Logs what it hid, once per toolset, so the saving is verifiable rather than
    assumed — and so a pattern that suddenly matches more than intended is
    visible in the logs instead of silently shrinking an agent's capability.
    """

    def __init__(self, wrapped_toolset: Any, agent: str, tool_filter: Any) -> None:
        super().__init__()
        self._wrapped_toolset = wrapped_toolset
        self._agent = agent
        self._tool_filter = tool_filter
        self._logged = False

    async def get_tools(self, readonly_context=None):
        tools = await self._wrapped_toolset.get_tools(readonly_context)
        names = [getattr(t, "name", "") for t in tools]
        hidden = self._tool_filter.hidden_for(self._agent, names)
        if not hidden:
            return tools
        kept = [t for t in tools if getattr(t, "name", "") not in hidden]
        if not self._logged:
            self._logged = True
            logger.info(
                "Tool filter (%s): hiding %d of %d MCP tools — %s",
                self._agent,
                len(hidden),
                len(names),
                ", ".join(sorted(hidden)),
            )
        return kept

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped_toolset, name)

    async def close(self) -> None:
        # Wrapped toolset lifecycle managed externally.
        pass


def _filtered_for_agent(config: AppConfig, agent: str, toolsets: list[Any]) -> list[Any]:
    """Wrap *toolsets* so *agent* only sees the tools it can use."""
    tool_filter = getattr(config.settings.mcp, "tool_filter", None)
    if tool_filter is None or not getattr(tool_filter, "enabled", False):
        return toolsets
    if not tool_filter.exclude.get(agent):
        return toolsets
    return [AgentToolFilter(ts, agent, tool_filter) for ts in toolsets]


def _create_single_mcp_toolset(
    provider_name: str,
    config: AppConfig,
) -> McpToolset | None:
    """Create an MCP toolset for a single provider.

    Args:
        provider_name: Name of the provider in the config.
        config: The application configuration.

    Returns:
        The configured McpToolset instance, or None on failure.
    """
    try:
        provider_entry = config.settings.mcp.provider_by_name(provider_name)
    except Exception as e:
        logger.warning("Could not resolve MCP provider '%s': %s", provider_name, e)
        return None

    try:
        provider = get_mcp_provider(provider_name, provider_entry)
        server_config = provider.get_server_config()

        logger.info(
            "Initializing MCP toolset for provider: %s (transport: %s)",
            provider.name,
            server_config.transport,
        )

        if server_config.transport == "stdio":
            if not server_config.command:
                logger.error("Stdio provider '%s' has no command configured", provider_name)
                return None

            import shutil

            from mcp import StdioServerParameters

            # Resolve bare command names to absolute paths for subprocess.
            command = server_config.command
            if not command.startswith("/"):
                if command in ("python", "python3"):
                    # Always use the current interpreter — guaranteed correct
                    # and avoids all PATH issues with venv/system Python.
                    import sys

                    command = sys.executable
                else:
                    resolved = shutil.which(command)
                    if resolved:
                        command = resolved
                    else:
                        logger.warning(
                            "Could not resolve command '%s' to absolute path; subprocess may fail",
                            command,
                        )
                logger.info("Stdio command resolved: '%s' → '%s'", server_config.command, command)

            connection_params = StdioConnectionParams(
                server_params=StdioServerParameters(
                    command=command,
                    args=server_config.args,
                )
            )
        elif server_config.transport == "streamable_http":
            kwargs: dict[str, Any] = {}
            if server_config.auth == "oauth":
                from evotrader.mcp.oauth import create_oauth_httpx_factory

                logger.info("Configuring browser-based OAuth for streamable HTTP transport")
                kwargs["httpx_client_factory"] = create_oauth_httpx_factory(server_config.url)
                kwargs["timeout"] = 300.0
            elif provider_name == "alpha_vantage":
                import os

                from evotrader.mcp.alpha_vantage import create_alpha_vantage_httpx_factory

                api_keys_str = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
                api_keys = [k.strip() for k in api_keys_str.split(",") if k.strip()]
                if not api_keys:
                    api_keys = [""]
                logger.info(
                    "Configuring Alpha Vantage key rotation factory with %d keys",
                    len(api_keys),
                )
                kwargs["httpx_client_factory"] = create_alpha_vantage_httpx_factory(api_keys)

            connection_params = StreamableHTTPConnectionParams(
                url=server_config.url,
                headers=server_config.headers or None,
                **kwargs,
            )
        elif server_config.transport == "sse":
            connection_params = SseConnectionParams(
                url=server_config.url,
                headers=server_config.headers or None,
            )
        else:
            logger.error("Unsupported MCP transport type: %s", server_config.transport)
            return None

        return LoggingMcpToolset(
            connection_params=connection_params,
            provider_name=provider_name,
            config=config,
        )
    except Exception as e:
        logger.error("Failed to create MCP toolset for '%s': %s", provider_name, e, exc_info=True)
        return None


def create_mcp_toolsets(config: AppConfig) -> dict[str, list[McpToolset]]:
    """Create MCP toolsets for all configured roles.

    Returns a dict mapping role names (e.g. 'trading', 'research') to
    ordered lists of ``McpToolset`` instances.  When a role maps to
    multiple providers, all are created and returned in priority order.

    Roles whose providers fail to initialise are silently omitted.
    """
    mcp_config = config.settings.mcp
    toolsets: dict[str, list[McpToolset]] = {}

    for role in mcp_config.roles:
        providers = mcp_config.providers_for_role(role)
        role_toolsets: list[McpToolset] = []

        for provider_name, _entry in providers:
            toolset = _create_single_mcp_toolset(provider_name, config)
            if toolset is not None:
                role_toolsets.append(toolset)
                logger.info("MCP role '%s' → provider '%s' ✓", role, provider_name)
            else:
                logger.warning(
                    "MCP role '%s' → provider '%s' — failed to initialise",
                    role,
                    provider_name,
                )

        if role_toolsets:
            toolsets[role] = role_toolsets

    return toolsets


def _tools_for_roles(
    mcp_toolsets: dict[str, list[McpToolset]],
    roles: list[str],
) -> list[McpToolset]:
    """Collect MCP toolsets for the given roles.

    Flattens multi-provider role lists so agents receive all
    available toolsets for their assigned roles.
    """
    result: list[McpToolset] = []
    for role in roles:
        if role in mcp_toolsets:
            result.extend(mcp_toolsets[role])
    return result


def create_news_sentiment_agent(
    config: AppConfig,
    mcp_toolsets: dict[str, list[McpToolset]] | None = None,
) -> LlmAgent:
    """Create the News & Sentiment agent.

    Analyses news for sentiment and identifies catalysts.

    MCP roles: ``research`` (for news/sentiment data).
    """
    tools: list = [get_trade_history, query_user_notes, get_earnings_history]
    if mcp_toolsets:
        tools.extend(
            _filtered_for_agent(
                config, "news_sentiment", _tools_for_roles(mcp_toolsets, ["research"])
            )
        )

    return LlmAgent(
        model=_resolve_model(config, "news_sentiment_agent"),
        name="news_sentiment",
        instruction=_load_instructions("news_sentiment", config),
        tools=tools,
        mode="single_turn",
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


# The strategy agent's tool set, in one place so the API and CLI runtimes cannot
# drift apart. None of these can place or cancel an order — that is what makes
# this agent safe to host on an external harness.
#
# `gather_market_data` and `get_open_positions` are READ-ONLY, and they are here
# because a hosted agent starts COLD: on the API runtime an LlmAgent sub-agent
# inherits the session history automatically, but on a CLI runtime its entire
# view of the market is the reconstruction in `CliBackedAgent._task_prompt`.
# When that reconstruction drops a payload the agent has no way to notice or
# recover — and on 2026-09-15 15:30Z it did exactly that, leaving the agent
# unable to distinguish "the market is quiet" from "I was not shown the market".
#
# This does not widen the blast radius. Strategy still only PROPOSES,
# risk_manager still approves, and the risk gate still lives on the executor.
# An agent that can re-read the snapshot it was supposed to be handed is
# strictly safer than one that must trust the pipe.
_STRATEGY_TOOLS = (
    compute_risk_budget,
    gather_market_data,
    get_open_positions,
    get_ticker_snapshot,
    log_signal_attribution,
    query_past_trades,
    query_user_notes,
    store_learning,
    update_trading_handoff,
)


class _LazyThoughtLogger:
    """Writes timeline rows without holding a database handle open.

    The factory builds agents long before a cycle runs, and the web server owns
    the live connection. So the database is resolved per write, exactly as the
    existing tool callbacks do, rather than captured at construction time.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    async def record_event(self, **kwargs: Any) -> None:
        from evotrader.db.connection import Database
        from evotrader.db.thought_log import ThoughtLogger
        from evotrader.web.server import _active_app

        db = None
        should_close = False
        if (
            _active_app
            and hasattr(_active_app.state, "db")
            and _active_app.state.db._db_path == self._config.db_path
        ):
            db = _active_app.state.db
        else:
            db = Database(self._config.db_path)
            await db.initialize()
            should_close = True
        try:
            await ThoughtLogger(db).record_event(**kwargs)
        finally:
            if should_close and db:
                await db.close()


def _strategy_dynamic_context(config: AppConfig) -> str:
    """Per-cycle context for the strategy agent, on EITHER runtime.

    The cycle-to-cycle handoff belongs here rather than in the CLI agent's task
    prompt: both the hosted agent and the API fallback resolve this, so a quota
    wall that drops the cycle onto the fallback does not silently drop the
    continuity with it. Injected rather than offered as a tool, because
    continuity the agent has to remember to look for goes missing on the cycle
    it mattered.
    """
    parts = [build_temporal_context(config.settings.schedule)]
    try:
        from evotrader.tools.trading_handoff import render_for_prompt

        block = render_for_prompt(config.data_dir)
        if block:
            parts.append(block)
    except Exception:  # continuity is valuable, a cycle is more so
        logger.debug("Could not attach the trading handoff", exc_info=True)
    return "\n\n".join(p for p in parts if p)


def _create_api_strategy_agent(config: AppConfig) -> LlmAgent:
    """The strategy agent on the metered API runtime (the proven path)."""

    return LlmAgent(
        model=_resolve_model(config, "strategy_agent"),
        name="strategy",
        static_instruction=_load_instructions("strategy", config),
        instruction=lambda ctx: _strategy_dynamic_context(config),
        tools=list(_STRATEGY_TOOLS),
        mode="single_turn",
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


def create_strategy_agent(config: AppConfig) -> Any:
    """Create the Strategy agent (Hybrid Brain).

    Combines pre-computed algorithmic signals and LLM reasoning to generate trade
    proposals. The algo signal and market data are injected into its prompt by the
    orchestrator — this agent focuses purely on reasoning about the trade decision.

    ``agent_runtime.strategy`` decides where that reasoning runs. On ``api`` this
    is an ordinary ``LlmAgent``. On a CLI runtime it is hosted on an external
    harness billed to a subscription seat, with the API agent retained as the
    fallback — so a quota wall or a harness failure costs nothing but a slightly
    different model, never a missed decision.
    """
    from evotrader.models.config import AgentRuntimeKind

    api_agent = _create_api_strategy_agent(config)
    kind, runtime = config.settings.runtime_for("strategy")
    if kind is AgentRuntimeKind.API or runtime is None:
        return api_agent

    from evotrader.agents.cli import LocalTool, load_backend
    from evotrader.agents.cli_agent import CliBackedAgent

    backend, reason = load_backend(runtime.driver, runtime)
    if backend is None:
        logger.error(
            "COST/RUNTIME WARNING: agent_runtime.strategy requests the %r runtime "
            "but it is unavailable — %s Falling back to the API runtime, which is "
            "BILLED PER TOKEN.",
            runtime.driver,
            reason,
        )
        return api_agent

    logger.info(
        "Strategy runtime: %s model=%s billing=%s thinking=%s effort=%s "
        "(requested; actual billing is logged after each run)",
        runtime.driver,
        runtime.model or "harness default",
        runtime.billing,
        runtime.thinking,
        runtime.effort or "unset",
    )
    return CliBackedAgent(
        name="strategy",
        # Becomes the function declaration the orchestrator sees when this agent
        # is wired as a tool, so keep it imperative and accurate.
        description=(
            "Hybrid-brain strategy agent. Reads the market snapshot, algo composite "
            "and news sentiment already gathered this cycle and returns either a "
            "trade proposal (including cancel/place of protective orders) or a "
            "reasoned no-trade. Proposes only — never places orders."
        ),
        backend=backend,
        runtime=runtime,
        instruction_text=_load_instructions("strategy", config),
        dynamic_instruction=lambda ctx: _strategy_dynamic_context(config),
        local_tools=[LocalTool.from_function(fn) for fn in _STRATEGY_TOOLS],
        fallback_agent=api_agent,
        thought_logger=_LazyThoughtLogger(config),
    )


def create_risk_manager_agent(config: AppConfig) -> LlmAgent:
    """Create the Risk Manager agent.

    Validates trade proposals against the constitution and risk limits.
    """
    # Inject constitution into instructions for the Risk Manager.
    # Use to_llm_display_dict() so _pct fields show as "100%" not "1.0".
    # model_dump_json() exposes post-normalization fractions that models
    # misread (1.0 → "1% hard cap" instead of "100% = no cap").
    import json

    base_instructions = _load_instructions("risk_manager", config)
    constitution_display = json.dumps(config.constitution.to_llm_display_dict(), indent=2)
    full_instructions = (
        f"{base_instructions}\n\n"
        f"## Active Constitution (INVIOLABLE)\n\n"
        f'All `_pct` fields are in human-readable percent (e.g. `"100%"` = 100%, '
        f'`"5%"` = 5%). Do NOT reinterpret these values.\n\n'
        f"```json\n{constitution_display}\n```"
    )

    return LlmAgent(
        model=_resolve_model(config, "risk_manager_agent"),
        name="risk_manager",
        instruction=full_instructions,
        tools=[
            check_risk_limits,
            check_option_risk_limits,
            get_open_positions,
            get_performance_summary,
        ],
        mode="single_turn",
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


def create_execution_agent(
    config: AppConfig,
    mcp_toolsets: dict[str, list[McpToolset]] | None = None,
) -> LlmAgent:
    """Create the Execution agent.

    Translates approved trade proposals into orders and monitors fills.

    MCP roles: ``trading`` (for placing orders via broker).
    """
    tools: list = [record_trade, get_open_positions, get_market_status, assess_order_book]
    if mcp_toolsets:
        tools.extend(
            _filtered_for_agent(config, "execution", _tools_for_roles(mcp_toolsets, ["trading"]))
        )

    from evotrader.callbacks.risk_gate import (
        _GATED_TOOLS,
        create_risk_gate,
        is_unchecked_order_tool,
    )

    gate = create_risk_gate(config.constitution, dry_run=config.settings.dry_run.enabled)

    # Track option_ids that have been reviewed to detect mismatches at placement time.
    # Populated by after_tool_callback (review_option_order responses),
    # checked by before_tool_callback (place_option_order calls).
    _reviewed_option_ids: set[str] = set()

    async def _broker_held_quantity(ticker: str | None, option_id: str | None) -> float | None:
        """Held units according to the BROKER, or None if it could not say.

        ``get_broker_positions`` collapses every failure into an empty list, so
        an empty result is ambiguous — it means "flat" or "the call failed" and
        there is no way to tell them apart from here. This returns None for
        that case rather than 0.0, because conflating unknown with zero is the
        exact defect this whole review is about. A NON-empty list, by contrast,
        is authoritative: if the account holds things and this instrument is
        not among them, the position really is flat.
        """
        if not mcp_toolsets:
            return None
        trading = _tools_for_roles(mcp_toolsets, ["trading"])
        if not trading:
            return None
        from evotrader.agents import tools as _tools_mod
        from evotrader.db.reconciliation import ReconciliationService

        journal = getattr(_tools_mod, "_journal", None)
        if journal is None:
            return None
        recon = ReconciliationService(journal, trading[0], dry_run=config.settings.dry_run.enabled)
        positions = await recon.get_broker_positions()
        if not positions:
            return None  # ambiguous — see docstring
        total = 0.0
        for pos in positions:
            if option_id:
                if pos.get("option_id") == option_id:
                    total += float(pos.get("quantity") or 0.0)
            elif ticker and (pos.get("symbol") or "").upper() == ticker.upper():
                if not pos.get("option_id"):
                    total += float(pos.get("quantity") or 0.0)
        return total

    async def _journal_held_quantity(ticker: str | None, option_id: str | None) -> float | None:
        """Held units according to the local journal, or None if unavailable."""
        from evotrader.agents import tools as _tools_mod

        journal = getattr(_tools_mod, "_journal", None)
        if journal is None:
            return None
        rows = await journal.get_open_trades()
        if not rows:
            return None
        total = 0.0
        matched = False
        for row in rows:
            if option_id:
                if row.get("option_id") != option_id:
                    continue
            elif not ticker or (row.get("ticker") or "").upper() != ticker.upper():
                continue
            matched = True
            total += float(row.get("remaining_quantity") or row.get("quantity") or 0.0)
        return total if matched else None

    async def _resolve_held_quantity(order_args: dict) -> float | None:
        """Units of the order's instrument currently held, or None if unknown.

        Broker state is the authority; the journal is the fallback for when the
        broker cannot answer. **None is meaningful and must NOT be conflated
        with 0.0** — see the permissive branch in ``_check_constitution``.
        Returning 0.0 for an unverified instrument would re-create the bug this
        resolver exists to fix, just one layer further out.
        """
        try:
            option_id = None
            if "legs" in order_args:
                option_id = (order_args.get("legs") or [{}])[0].get("option_id")
                if not option_id:
                    return None
            ticker = order_args.get("ticker") or order_args.get("symbol")
            if not ticker and not option_id:
                return None

            broker_qty = await _broker_held_quantity(ticker, option_id)
            if broker_qty is not None:
                return broker_qty
            # Broker could not answer. The journal is a weaker source but a
            # real one, and it catches the case where the broker call silently
            # failed while a position genuinely exists.
            return await _journal_held_quantity(ticker, option_id)
        except Exception:
            logger.warning(
                "[RISK GATE] held-quantity lookup failed for %s; the gate will "
                "treat the quantity as unknown and allow a sell rather than "
                "trap the position.",
                order_args.get("ticker") or order_args.get("symbol"),
                exc_info=True,
            )
            return None

    async def before_tool_callback(tool, args, tool_context):
        if is_unchecked_order_tool(tool.name):
            # Fail closed: see risk_gate._ORDER_TOOL_NAME.
            logger.error(
                "[RISK GATE] BLOCKED %s: it places or changes orders, but the gate "
                "has no check for it.",
                tool.name,
            )
            return {
                "allowed": False,
                "action": "UNCHECKED_ORDER_TOOL",
                "violations": [
                    f"'{tool.name}' places or changes orders, but the risk gate has "
                    "no check for it yet. Use place_equity_order or "
                    "place_option_order; a developer must add this tool to "
                    "_GATED_TOOLS with a check before it can be used."
                ],
            }
        if tool.name in _GATED_TOOLS:
            # ── Option-ID consistency check ──────────────────────────────
            # If the agent is placing an option order, verify the option_id
            # was previously reviewed. A mismatch typically means the agent
            # re-looked up instruments and picked a different expiration's
            # contract while keeping the price from the reviewed contract.
            if tool.name == "place_option_order" and _reviewed_option_ids:
                legs = args.get("legs") or []
                for leg in legs:
                    oid = leg.get("option_id", "")
                    if oid and oid not in _reviewed_option_ids:
                        logger.error(
                            "[RISK GATE] OPTION_ID MISMATCH: place_option_order "
                            "uses option_id '%s' which was NOT reviewed. "
                            "Reviewed IDs: %s. This likely means the agent "
                            "re-looked up instruments and picked a different "
                            "expiration's contract. BLOCKING order.",
                            oid,
                            _reviewed_option_ids,
                        )
                        return {
                            "allowed": False,
                            "action": "OPTION_ID_MISMATCH",
                            "violations": [
                                f"option_id '{oid}' was not reviewed via "
                                f"review_option_order. The agent must use the "
                                f"exact option_id from the approved proposal. "
                                f"Reviewed option_ids: {sorted(_reviewed_option_ids)}"
                            ],
                        }

            # 0. Protective orders are good-till-cancelled, enforced here rather
            #    than left to the model. Mutates args (injects gtc) or refuses.
            from evotrader.callbacks.risk_gate import enforce_protective_time_in_force

            tif_violation = enforce_protective_time_in_force(args)
            if tif_violation:
                logger.error(
                    "[RISK GATE] BLOCKED: %s — %s", gate.summarise_order(args), tif_violation
                )
                return {
                    "allowed": False,
                    "action": "PROTECTIVE_ORDER_NOT_GTC",
                    "violations": [tif_violation],
                    "retryable": True,
                    "error_class": "POLICY",
                    "fix": "Resubmit the identical order with time_in_force='gtc'.",
                }

            # 1. Check constitution violations
            # The held quantity is what lets the gate tell a position-reducing
            # sell from a short sale. Without it every exit reads as a short.
            held_qty = await _resolve_held_quantity(args)
            violations = gate.check_violations(args, held_quantity=held_qty)
            if violations:
                logger.error(
                    "[RISK GATE] BLOCKED: %s (held=%s) — violations: %s",
                    gate.summarise_order(args),
                    "unknown" if held_qty is None else f"{held_qty:g}",
                    "; ".join(violations),
                )
                return {
                    "allowed": False,
                    "action": "CONSTITUTION_BLOCKED",
                    "violations": violations,
                    # A deterministic policy decision, NOT a transient fault.
                    # Retrying the identical order will fail identically. On
                    # 2026-09-11 this block surfaced upstream as "system/network
                    # error", so the orchestrator retried it across four cycles
                    # and proposed a broker-sync remedy for what was a purely
                    # local, unconditional rejection.
                    "retryable": False,
                    "error_class": "POLICY",
                }

            # 2. If risk check passed, prompt web console for interactive user approval (if enabled)
            if config.settings.require_trade_approval:
                from evotrader.web.server import check_interactive_approval

                interactive_res = await check_interactive_approval(args)
                if interactive_res is not None:
                    return interactive_res

            # 3. If user approved (or server is not running / bypassed), check dry_run
            if config.settings.dry_run.enabled:
                from evotrader.agents.tools import _sim_proxy

                if _sim_proxy is not None:
                    logger.info("[RISK GATE] APPROVED (sim mode): %s", gate.summarise_order(args))
                    return None  # Let it proceed to the tool -> which proxy intercepts

                import uuid

                # Extract order details
                ticker = args.get("ticker", args.get("symbol", ""))
                # Handle option contract legs
                if not ticker and "legs" in args:
                    from evotrader.agents.tools import OPTION_ID_TO_TICKER

                    legs = args.get("legs", [])
                    if legs:
                        option_id = legs[0].get("option_id", "")
                        ticker = OPTION_ID_TO_TICKER.get(option_id, "OPTION")

                side = args.get("side", "buy").lower()
                quantity = float(args.get("quantity", 1))
                price = float(args.get("price", args.get("limit_price", 0.0)))

                logger.warning(
                    "[RISK GATE] SIMULATION MODE — simulating successful execution of order: %s",
                    gate.summarise_order(args),
                )

                return {
                    "allowed": True,
                    "action": "APPROVED",
                    "status": "filled",
                    "order_id": f"sim_{uuid.uuid4().hex[:8]}",
                    "price": price,
                    "quantity": quantity,
                    "ticker": ticker,
                    "side": side,
                    "simulated": True,
                    "message": "Simulated fill (dry_run=true)",
                }

            logger.info("[RISK GATE] APPROVED: %s", gate.summarise_order(args))
        return None

    async def after_tool_callback(tool, args, tool_context, tool_response):
        """Capture reviewed option_ids from review_option_order responses."""
        if tool.name == "review_option_order":
            # Extract option_ids from the review request legs so we can
            # validate that the subsequent place_option_order uses the same ones.
            legs = args.get("legs") or []
            for leg in legs:
                oid = leg.get("option_id", "")
                if oid:
                    _reviewed_option_ids.add(oid)
                    logger.info("[RISK GATE] Recorded reviewed option_id: %s", oid)
        return tool_response

    return LlmAgent(
        model=_resolve_model(config, "executor_agent"),
        name="execution",
        instruction=_load_instructions("executor", config),
        tools=tools,
        before_tool_callback=before_tool_callback,
        after_tool_callback=after_tool_callback,
        mode="single_turn",
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


def create_orchestrator_agent(
    config: AppConfig,
    mcp_toolsets: dict[str, list[McpToolset]] | None = None,
) -> LlmAgent:
    """Create the Orchestrator agent.

    Central coordinator that runs the trading loop, delegates to
    specialist agents, and manages the overall pipeline.

    Market data gathering is handled by the deterministic
    ``gather_market_data`` tool (replaces the market_intelligence
    sub-agent). This eliminates the lossy LLM-to-LLM data handoff
    and ensures indicators / algo signals are always computed.

    Sub-agents: news_sentiment, strategy, risk_manager, execution.
    """
    if mcp_toolsets is None:
        mcp_toolsets = create_mcp_toolsets(config)

    # Create sub-agents with role-appropriate MCP toolsets
    # Built in pipeline order; each is routed below by its TYPE, not its name.
    _stage_agents = [
        create_news_sentiment_agent(config, mcp_toolsets),
        create_strategy_agent(config),
        create_risk_manager_agent(config),
        create_execution_agent(config, mcp_toolsets),
    ]
    sub_agents: list = []

    tools: list = [
        gather_market_data,
        gather_option_chain,
        get_market_status,
        get_performance_summary,
        get_trade_history,
        get_open_positions,
        reconcile_pending_orders,
        reconcile_broker_pnl,
    ]

    # ── The strategy stage must be RE-ENTRANT ─────────────────────────────
    # An LlmAgent sub-agent hands control back when it is done. A custom
    # BaseAgent sub-agent — which the CLI-hosted strategy agent is — is reached
    # through `transfer_to_agent`, and ADK treats that as a ONE-WAY handoff: it
    # runs the target, yields its events, and the orchestrator's generator ends.
    # risk_manager and execution then become structurally unreachable.
    #
    # Measured: 2026-09-15 and 2026-09-16, sixteen consecutive cycles, every one
    # `stages_skipped: [risk_manager, execution]`, zero orders placed. The last
    # agent-placed order was at 2026-09-14 18:33Z, the day before the CLI
    # migration. Three of those cycles carried an explicit CLOSE proposal for a
    # position that then sat unprotected through FOMC.
    #
    # As a tool the agent is invoked, answers, and the orchestrator resumes
    # holding the proposal text — which is also what makes the v007 "route on
    # content" instructions reachable at all.
    # Routed on TYPE so this holds for EVERY stage, present and future. Any agent
    # that is not an LlmAgent — today only a CLI-hosted one, tomorrow whatever
    # else — is wired as a tool automatically. Nobody has to remember, which is
    # the point: the original defect was a single agent quietly changing type
    # and keeping its old registration.
    from evotrader.agents.cli_agent import SessionSharingAgentTool

    for _agent in _stage_agents:
        if isinstance(_agent, LlmAgent):
            # An ordinary LlmAgent sub-agent already returns control.
            sub_agents.append(_agent)
        else:
            tools.append(SessionSharingAgentTool(_agent, skip_summarization=True))
    # Trading MCP toolset must be registered on the orchestrator so ADK
    # initialises the OAuth session. We wrap it in FilteredMcpToolset to expose
    # only order cancellation tools, preventing 30+ unused trading tool schemas
    # from bloating every turn of the orchestrator's context window.
    if mcp_toolsets:
        trading_toolsets = _tools_for_roles(mcp_toolsets, ["trading"])
        filtered_trading = [
            FilteredMcpToolset(ts, {"cancel_option_order", "cancel_equity_order"})
            for ts in trading_toolsets
        ]
        tools.extend(filtered_trading)

    tool_call_cache = {}

    async def orchestrator_before_tool_callback(
        tool,
        args,
        tool_context,
    ):
        """Deduplicate parallel tool calls to prevent stuttering loops."""
        import json
        import time

        try:
            args_str = json.dumps(args, sort_keys=True) if args else ""
        except Exception:
            args_str = str(args)

        cache_key = (tool.name, args_str)
        now_time = time.time()

        if cache_key in tool_call_cache:
            prev_time, prev_response = tool_call_cache[cache_key]
            if now_time - prev_time < 2.0:
                logger.warning(
                    "⚠️ Deduplicating parallel tool call stuttering: '%s' with args %s. Skipping duplicate run.",
                    tool.name,
                    args,
                )
                return (
                    prev_response
                    or f"Skipped duplicate parallel tool call to {tool.name} for safety."
                )

        tool_call_cache[cache_key] = (now_time, None)
        return None

    async def orchestrator_after_tool_callback(
        tool,
        args,
        tool_context,
        tool_response,
    ):
        """Handle sub-agent execution failures gracefully by providing safety-constrained error messages."""
        import json

        try:
            args_str = json.dumps(args, sort_keys=True) if args else ""
        except Exception:
            args_str = str(args)

        cache_key = (tool.name, args_str)
        if cache_key in tool_call_cache:
            tool_call_cache[cache_key] = (tool_call_cache[cache_key][0], tool_response)

        if isinstance(tool_response, str) and tool_response.startswith("Error running sub-agent:"):
            # ── POLICY BLOCK ≠ INFRASTRUCTURE FAILURE ────────────────────
            # A constitution block is a deterministic local decision. Labelling
            # it a "system/network error" tells the orchestrator to treat it as
            # transient, and on 2026-09-11 that is exactly what happened: four
            # cycles retried the identical rejected order, and cycle 8's summary
            # attributed the failure to "journal/broker drift" and proposed a
            # broker reconciliation that could not possibly have helped.
            #
            # Surface the reason verbatim and say plainly that retrying is
            # pointless, so the agent reasons about the policy instead of the
            # network.
            _policy_markers = (
                "CONSTITUTION_BLOCKED",
                "OPTION_ID_MISMATCH",
                "not in allowed list",
                "disabled in constitution",
                "would open a short position",
            )
            if any(m in tool_response for m in _policy_markers):
                return (
                    f"Error: Sub-agent '{tool.name}' was BLOCKED BY POLICY, not by "
                    f"a system or network fault. This is a deterministic local "
                    f"decision and retrying the identical order will fail "
                    f"identically — do NOT re-attempt it. Reason, verbatim:\n"
                    f"{tool_response}\n"
                    f"Either adjust the order so it complies, or report the "
                    f"block and move on. Broker reconciliation or position "
                    f"syncing will not resolve a policy block."
                )
            if tool.name == "risk_manager":
                return (
                    "Error: Sub-agent 'risk_manager' failed due to a system/network error. "
                    "Trade validation could not be completed. As a safety constraint, you MUST NOT "
                    "proceed with executing this trade."
                )
            elif tool.name == "strategy":
                return "Error: Sub-agent 'strategy' failed due to a system/network error. Trading proposal could not be generated."
            elif tool.name == "news_sentiment":
                return "Error: Sub-agent 'news_sentiment' failed due to a system/network error. News sentiment analysis is unavailable."
            elif tool.name == "execution":
                return "Error: Sub-agent 'execution' failed due to a system/network error. Order placement could not be completed."
            else:
                return f"Error: Sub-agent '{tool.name}' failed due to a system/network error."
        return None

    return LlmAgent(
        model=_resolve_model(config, "orchestrator"),
        name="orchestrator",
        static_instruction=_load_instructions("orchestrator", config),
        instruction=lambda ctx: build_temporal_context(config.settings.schedule),
        tools=tools,
        sub_agents=sub_agents,
        before_tool_callback=orchestrator_before_tool_callback,
        after_tool_callback=orchestrator_after_tool_callback,
    )


def create_evolution_agent(config: AppConfig) -> LlmAgent:
    """Create the Evolution agent.

    Runs after market close to analyse performance and propose
    algorithm/instruction/code improvements. Has access to:
    - Performance analysis tools
    - Source code reading (all project files)
    - Algorithm parameter evolution
    - Strategy code generation (sandboxed)
    - Agent instruction versioning
    - Infrastructure code review (human-gated)
    - Semantic memory for learnings
    """
    from evotrader.evolution.tools import (
        activate_instruction_version,
        analyse_performance,
        get_cycle_digest,
        get_cycle_summary,
        get_instruction_text,
        get_signal_calibration,
        get_strategy_manifest,
        get_tool_response,
        list_instruction_versions,
        list_project_files,
        list_proposals,
        promote_algorithm_version,
        propose_composition_change,
        propose_deprecation,
        propose_instruction_change,
        propose_new_strategy,
        propose_parameter_change,
        query_cycle_thoughts,
        read_source_file,
        read_strategy_code,
        rollback_algorithm,
        rollback_instructions,
        submit_code_review,
        update_carry_forward,
        validate_strategy_code,
    )

    return LlmAgent(
        model=_resolve_model(config, "evolution_agent"),
        name="evolution",
        instruction=_load_instructions("evolution", config),
        tools=[
            # Performance analysis
            analyse_performance,
            get_performance_summary,
            get_trade_history,
            get_signal_calibration,
            # Cycle thought analysis (summary-first, drill-down)
            get_cycle_digest,
            get_cycle_summary,
            query_cycle_thoughts,
            get_tool_response,
            # Algorithm management
            get_active_algorithm,
            list_algorithm_versions,
            propose_parameter_change,
            promote_algorithm_version,
            rollback_algorithm,
            # Code evolution (strategy generation)
            read_strategy_code,
            validate_strategy_code,
            # Proposal-based evolution
            get_strategy_manifest,
            propose_new_strategy,
            propose_deprecation,
            propose_composition_change,
            list_proposals,
            # Code review (infrastructure — human-gated)
            read_source_file,
            list_project_files,
            submit_code_review,
            # Instruction evolution
            propose_instruction_change,
            activate_instruction_version,
            list_instruction_versions,
            get_instruction_text,
            rollback_instructions,
            # Memory / knowledge
            query_past_trades,
            store_learning,
            get_memory_stats,
            # Cross-cycle carry-forward notes
            update_carry_forward,
        ],
    )


def get_tool_metadata(tool_name: str, config: AppConfig) -> dict[str, Any]:
    """Dynamically resolve tool information (local vs MCP provider details)."""
    # Set of all local tools (including standard and evolution tools)
    local_tools = {
        # Standard tools
        "check_option_risk_limits",
        "check_risk_limits",
        "compute_indicators",
        "gather_market_data",
        "gather_option_chain",
        "get_active_algorithm",
        "get_market_status",
        "get_memory_stats",
        "get_open_positions",
        "get_performance_summary",
        "get_trade_history",
        "list_algorithm_versions",
        "query_past_trades",
        "query_user_notes",
        "record_trade",
        "run_composite_strategy",
        "store_learning",
        # Evolution tools
        "analyse_performance",
        "get_cycle_digest",
        "query_cycle_thoughts",
        "get_cycle_summary",
        "read_strategy_code",
        "read_source_file",
        "list_project_files",
        "propose_parameter_change",
        "promote_algorithm_version",
        "rollback_algorithm",
        "validate_strategy_code",
        "submit_code_review",
        "propose_instruction_change",
        "activate_instruction_version",
        "list_instruction_versions",
        "get_instruction_text",
        "rollback_instructions",
        "get_strategy_manifest",
        "get_tool_response",
        "propose_new_strategy",
        "propose_deprecation",
        "propose_composition_change",
        "list_proposals",
        "update_carry_forward",
    }

    if tool_name in local_tools:
        return {"source": "local"}

    # Check MCP providers
    if config and config.settings and config.settings.mcp:
        for name, entry in config.settings.mcp.providers.items():
            try:
                provider = get_mcp_provider(name, entry)
                if tool_name in provider.get_all_tool_names():
                    address = entry.url or ""
                    if not address and entry.command:
                        address = f"stdio ({entry.command} {' '.join(entry.args)})".strip()
                    return {
                        "source": "mcp",
                        "provider": name,
                        "provider_label": provider.name,
                        "address": address,
                        "transport": entry.transport,
                        "is_write": tool_name in provider.get_write_tools(),
                    }
            except Exception as e:
                logger.warning("Error resolving provider %s: %s", name, e)

    return {"source": "unknown"}
