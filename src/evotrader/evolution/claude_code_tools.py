"""Bridge the evolution tools onto Claude Code as in-process MCP tools.

The Claude Code backend reuses the *same* Python functions the ADK evolution
agent calls — ``evotrader.evolution.tools`` and the shared
``evotrader.agents.tools``. Nothing is reimplemented, so proposals, code
reviews, carry-forward notes, algorithm promotions and instruction versions all
land in the same database rows and on-disk files as before, and the web console
sees them identically.

That reuse is only possible because :func:`create_sdk_mcp_server` runs the MCP
server **in this process**. The tools keep the dependencies
``bind_evolution_dependencies()`` already wired up (analyser, proposal manager,
algorithm registry, thought logger, semantic memory). A subprocess MCP server
would need to rebuild all of that against a second SQLite connection.

Schemas are generated from each function's signature and Google-style docstring,
mirroring what ADK's ``FunctionTool`` derives automatically, so the two backends
present the model with the same tool surface.
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from evotrader.agents.cli.schema import (
    _split_docstring,
    build_input_schema,
)

logger = logging.getLogger(__name__)

# Name of the in-process MCP server. Tools are addressed as
# ``mcp__evotrader__<tool_name>``.
SERVER_NAME = "evotrader"

# Tools that only read state. Marking them lets Claude batch them in parallel,
# which matters because an evolution run opens with several analysis calls.
_READ_ONLY_TOOLS = frozenset(
    {
        "analyse_performance",
        "query_cycle_thoughts",
        "get_tool_response",
        "get_cycle_digest",
        "get_cycle_summary",
        "get_signal_calibration",
        "read_strategy_code",
        "read_source_file",
        "list_project_files",
        "validate_strategy_code",
        "list_instruction_versions",
        "get_instruction_text",
        "get_strategy_manifest",
        "list_proposals",
        "get_performance_summary",
        "get_trade_history",
        "get_active_algorithm",
        "list_algorithm_versions",
        "get_memory_stats",
        "query_past_trades",
    }
)


def _result_text(value: Any) -> str:
    """Serialise a tool return value for the model."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def wrap_tool(fn: Callable) -> Any:
    """Wrap a plain evolution tool function as an SDK MCP tool."""
    from claude_agent_sdk import ToolAnnotations, tool

    name = fn.__name__
    description, _ = _split_docstring(fn)
    schema = build_input_schema(fn)
    is_async = inspect.iscoroutinefunction(fn)

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            call_args = {k: v for k, v in (args or {}).items() if k != "_meta"}
            result = fn(**call_args)
            if is_async:
                result = await result
            return {"content": [{"type": "text", "text": _result_text(result)}]}
        except Exception as exc:
            # Compose the message rather than letting the raw exception through,
            # so the model can tell a bad argument from a genuine failure.
            logger.warning("Evolution tool %s failed: %s", name, exc, exc_info=True)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"{name} failed: {type(exc).__name__}: {exc}",
                    }
                ],
                "is_error": True,
            }

    handler.__name__ = f"{name}_mcp"

    return tool(
        name,
        description,
        schema,
        annotations=ToolAnnotations(readOnlyHint=name in _READ_ONLY_TOOLS),
    )(handler)


def evolution_tool_functions() -> list[Callable]:
    """The tool set the evolution agent gets, matching the ADK backend.

    Kept deliberately parallel to the ``tools=[...]`` list in
    ``create_evolution_agent`` so the two backends stay in step.
    """
    from evotrader.agents.tools import (
        get_active_algorithm,
        get_memory_stats,
        get_performance_summary,
        get_trade_history,
        list_algorithm_versions,
        query_past_trades,
        store_learning,
    )
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

    return [
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
        # Code evolution
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
        # Memory
        query_past_trades,
        store_learning,
        get_memory_stats,
        # Carry-forward notes
        update_carry_forward,
    ]


def build_evolution_mcp_server() -> Any:
    """Create the in-process MCP server exposing the evolution tools."""
    from claude_agent_sdk import create_sdk_mcp_server

    functions = evolution_tool_functions()
    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[wrap_tool(fn) for fn in functions],
    )
    logger.info(
        "Claude Code evolution: exposed %d evotrader tools as in-process MCP",
        len(functions),
    )
    return server


def allowed_tool_patterns(allow_file_tools: bool) -> list[str]:
    """Tool permissions for an evolution run.

    Every evotrader tool is pre-approved — the safety gates that matter live
    inside the tools themselves (validation, rate limits, ``auto_promote``,
    human review of proposals), not in Claude Code's permission layer.

    Args:
        allow_file_tools: Also allow Claude Code's own read-only file tools, so
            it can navigate the repo directly instead of going through
            ``read_source_file``. Read/Grep/Glob only — never Write, Edit or
            Bash: code changes must go through ``submit_code_review`` or
            ``propose_*`` so they stay human-gated and auditable.
    """
    patterns = [f"mcp__{SERVER_NAME}__*"]
    if allow_file_tools:
        patterns += ["Read", "Grep", "Glob"]
    return patterns
