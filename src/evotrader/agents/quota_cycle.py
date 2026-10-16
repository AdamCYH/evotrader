"""A trading cycle with no agents, while the strategy agent's plan is spent.

With ``quota.on_exhausted`` 'skip' or 'fail', a spent subscription plan means
the strategy agent makes no decision until its window resets. Running the
orchestrator and the news agent anyway spends API calls on a cycle that cannot
act. So the cycle runs only what needs no agent: the reconciliation of the
book at its start (main.py, before this), and the market data with the
algorithm's call, recorded as the orchestrator's tool call records them. The
snapshot is then stored, and the post-cycle backfill keeps the algorithm's
call in the attribution record, flagged agent_absent, so the control group has
no hole where the plan ran out.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


async def record_cycle_without_agents(
    thought_logger: Any,
    session_id: str,
    ticker: str,
    why: str,
    on_exhausted: str,
    gather: Callable[[str], Awaitable[dict[str, Any]]],
) -> str:
    """Record the cycle's market data with no agent run; returns the cycle's error text."""
    reason = (
        f"strategy agent out of subscription quota ({why}); "
        f"quota.on_exhausted={on_exhausted!r}: no agent ran this cycle"
    )
    await thought_logger.record_event(
        session_id=session_id,
        agent_name="orchestrator",
        event_type="runtime",
        content=(
            f"[runtime] {reason}. The book was reconciled at the start; the market "
            "data and the algorithm's call are recorded."
        ),
        meta={"no_agents": True, "reason": why, "on_exhausted": on_exhausted},
    )
    tool_info = {"source": "local", "called_by": "cycle_without_agents"}
    await thought_logger.record_event(
        session_id=session_id,
        agent_name="orchestrator",
        event_type="tool_call",
        content="gather_market_data",
        meta={"args": {"ticker": ticker}, "tool_info": tool_info},
    )
    result = await gather(ticker)
    await thought_logger.record_event(
        session_id=session_id,
        agent_name="orchestrator",
        event_type="tool_response",
        content="gather_market_data",
        meta={"response": result, "tool_info": tool_info},
    )
    return reason
