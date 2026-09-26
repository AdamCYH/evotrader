"""An ADK agent whose reasoning runs on a CLI-hosted harness instead of an API.

The rest of the pipeline should not be able to tell the difference. The
orchestrator delegates to this agent exactly as it delegates to an ``LlmAgent``;
the risk manager receives the same proposal text; the web console shows the same
timeline. Only the billing and the loop owner change.

**Why the strategy agent is a safe one to host this way.** It cannot reach the
broker. Its tools compute a risk budget, log attribution, and query memory —
strategy *proposes*, risk_manager approves, executor places, and the risk gate
lives on the executor. The worst case from a hosted strategy agent is a worse
proposal, which still has to clear both. That is not true of the executor, which
is why this is not applied there.

**Falling back is a first-class path, not an error path.** A subscription seat
has a hard quota wall, and hitting it mid-session would cost a trading decision.
So the agent drains early: once the harness reports it is near the limit, the
*next* invocation goes to the API-backed fallback while the current one finishes
where it started. A decision is never abandoned half-made.

    **The return-to-orchestrator guarantee comes from the TOOL BOUNDARY, not
    from this class.** This agent is a custom ``BaseAgent``; putting it in an
    orchestrator's ``sub_agents`` makes ADK reach it via ``transfer_to_agent``,
    which is a one-way handoff that ends the cycle — ``risk_manager`` and
    ``execution`` then never run. It must be wired with
    :class:`SessionSharingAgentTool`. Do not "simplify" it back into
    ``sub_agents``; that cost sixteen cycles and two trading days.

    The same applies to the fallback path below: it delegates to
    ``fallback_agent.run_async(ctx)`` from inside whatever frame invoked this
    agent, so the fallback protects the DECISION, never the return path. Under
    the tool wiring that is fine, because the whole invocation happens inside
    the tool call.
"""

from __future__ import annotations

import logging
import re as _re
import time
from typing import TYPE_CHECKING, Any

from google.adk.agents import BaseAgent
from google.adk.tools.agent_tool import AgentTool
from google.genai import types as genai_types
from pydantic import ConfigDict, Field, PrivateAttr

from evotrader.agents.cli import (
    AgentEvent,
    AgentRequest,
    BillingMode,
    QuotaStatus,
    ThinkingSpec,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.events import Event


logger = logging.getLogger(__name__)

# How much of the conversation tail to hand the hosted agent as its task.
_PROMPT_EVENT_LIMIT = 14

# No single tool result may crowd out the others. The option chain is an order of
# magnitude larger than the market snapshot and is fetched *after* it, so without
# a per-result cap a newest-first walk would spend the whole budget on the chain
# and drop the quote and indicators the decision is actually sized against.
_MAX_TOOL_RESULT_CHARS = 30_000

# Overall ceiling on the assembled task prompt.
_MAX_PROMPT_CHARS = 120_000

# No single *event* may consume the whole prompt budget either.
# _MAX_TOOL_RESULT_CHARS is charged per function_response PART, but the budget
# below is charged per EVENT — and the orchestrator issues its data-gathering
# calls in parallel, so get_market_status, get_open_positions,
# gather_market_data and gather_option_chain all come back as parts of ONE
# event (confirmed session 336dc34b: four tool_call timestamps inside 4ms).
# Without this cap that single event can legitimately carry 4 x 30,000 chars
# and swallow the entire budget on its own.
_MAX_EVENT_BLOCK_CHARS = 60_000

# Tool results the agent cannot make a decision without. These are emitted
# EARLIEST in the cycle, so a newest-first walk reaches them LAST — exactly
# backwards from their importance. Their absence is an error, not a detail.
_CRITICAL_TOOL_RESULTS = ("gather_market_data",)


def _render_part(part: Any) -> str:
    """Render one ADK content part as text the hosted agent can read.

    Tool *results* matter as much as prose here. The orchestrator gathers market
    data, the option chain and news by calling tools, and those land in the
    session as ``function_response`` parts. An ``LlmAgent`` sub-agent receives
    them automatically; a hosted harness starts cold, so dropping them means
    asking the agent to size a position against data it was never shown — which
    is exactly what happened on the first live cycle, and the agent correctly
    refused to trade rather than guess.
    """
    import json

    if getattr(part, "text", None):
        return str(part.text)

    response = getattr(part, "function_response", None)
    if response is not None:
        name = getattr(response, "name", "") or "tool"
        payload = getattr(response, "response", None)
        try:
            body = json.dumps(payload, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            body = str(payload)
        if len(body) > _MAX_TOOL_RESULT_CHARS:
            body = (
                body[:_MAX_TOOL_RESULT_CHARS]
                + f"\n…[truncated at {_MAX_TOOL_RESULT_CHARS:,} chars]"
            )
        return f'<tool_result name="{name}">\n{body}\n</tool_result>'

    return ""


def _event_text(event: Any) -> str:
    """Extract everything readable from an ADK event, tool results included."""
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) or []
    chunks = [rendered for p in parts if (rendered := _render_part(p))]
    return "\n".join(chunks).strip()


class CliBackedAgent(BaseAgent):
    """Runs one agent's turn on a hosted harness, with an API-backed fallback."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    backend: Any
    """A :class:`~evotrader.agents.cli.CliAgentBackend`."""

    runtime: Any
    """The ``CliRuntimeConfig`` selected for this agent."""

    local_tools: list[Any] = Field(default_factory=list)
    instruction_text: str = ""
    dynamic_instruction: Any = None
    """Optional ``callable(ctx) -> str`` appended to the system prompt."""

    fallback_agent: Any = None
    """An ``LlmAgent`` used when the harness is unusable. Without one, a quota
    wall means no decision at all, so this should normally be supplied."""

    thought_logger: Any = None

    _backoff_until: float = PrivateAttr(default=0.0)
    _backoff_reason: str = PrivateAttr(default="")

    # ── Prompt assembly ───────────────────────────────────────────

    def _system_prompt(self, ctx: InvocationContext) -> str:
        parts = [self.instruction_text]
        if self.dynamic_instruction is not None:
            try:
                extra = self.dynamic_instruction(ctx)
            except TypeError:
                extra = self.dynamic_instruction()
            if extra:
                parts.append(str(extra))
        return "\n\n".join(p for p in parts if p)

    def _task_prompt(self, ctx: InvocationContext) -> str:
        """Reconstruct what the orchestrator is asking for.

        An ``LlmAgent`` sub-agent receives the session history automatically. A
        hosted harness starts cold, so the tail of the conversation is passed
        explicitly — otherwise the agent would be asked to decide a trade with no
        market data in front of it.
        """
        events: Sequence[Any] = getattr(getattr(ctx, "session", None), "events", []) or []
        chunks: list[str] = []
        budget = _MAX_PROMPT_CHARS
        for event in reversed(list(events)):
            text = _event_text(event)
            if not text:
                continue
            author = getattr(event, "author", "") or "system"
            if author == self.name:
                continue  # our own previous output
            block = f"[{author}]\n{text}"
            if len(block) > _MAX_EVENT_BLOCK_CHARS:
                block = (
                    block[:_MAX_EVENT_BLOCK_CHARS]
                    + f"\n…[event truncated at {_MAX_EVENT_BLOCK_CHARS:,} chars]"
                )
            if len(block) > budget:
                # Do NOT stop the walk here. The market snapshot is the OLDEST
                # event in a cycle, so `break` discarded precisely the payload
                # the decision is sized against while keeping the news summary
                # that happened to be newer. Observed live 2026-09-15 15:30Z:
                # gather_market_data ran, and the strategy agent was handed only
                # the news report. Skip this block and keep looking for smaller,
                # older, more decision-critical ones.
                continue
            budget -= len(block)
            chunks.append(block)
            if len(chunks) >= _PROMPT_EVENT_LIMIT:
                break

        if not chunks:
            text = _event_text(getattr(ctx, "user_content", None))
            if text:
                chunks.append(text)

        if not chunks:
            logger.error(
                "%s: assembled an EMPTY task prompt from %d session event(s). The "
                "hosted agent will be asked to decide with no market data.",
                self.name,
                len(events),
            )
            return "Produce your analysis for the current cycle."

        prompt = "\n\n".join(reversed(chunks))

        # Logged every cycle: a hosted agent that silently loses its market data
        # still answers, it just answers badly. Naming the tool results actually
        # present makes that failure visible in one line instead of in a trade.
        tools_present = _re.findall(r'<tool_result name="([^"]+)"', prompt)
        logger.info(
            "%s: task prompt assembled — %d chars from %d event(s); tool results: %s",
            self.name,
            len(prompt),
            len(chunks),
            ", ".join(dict.fromkeys(tools_present)) or "NONE",
        )
        missing = [t for t in _CRITICAL_TOOL_RESULTS if t not in tools_present]
        if missing:
            logger.error(
                "%s: task prompt is MISSING critical tool result(s): %s. The "
                "hosted agent is being asked to decide without them and cannot "
                "tell the difference between a quiet market and an absent one.",
                self.name,
                ", ".join(missing),
            )
        return prompt

    # ── Quota policy ──────────────────────────────────────────────

    def _in_backoff(self) -> bool:
        return bool(self._backoff_until) and time.time() < self._backoff_until

    def _note_quota(self, quota: Any) -> None:
        """Decide whether the NEXT invocation should avoid the harness.

        Acting before the wall is what keeps a quota limit from costing a trading
        cycle — but *only* on our own configured threshold, not on the harness's
        advisory status. The two are not the same thing, and conflating them is
        expensive: a ``seven_day`` window reporting ``allowed_warning`` at 76%
        used is a perfectly healthy plan with days of headroom, yet it would
        divert every remaining cycle to the metered API. Observed on the first
        sim run, which backed off at 76% against a 0.85 threshold.

        So: back off when the plan is genuinely exhausted, or when *we* judge it
        near the limit. Log an advisory warning below that and carry on.
        """
        threshold = getattr(getattr(self.runtime, "quota", None), "warn_utilization", 0.85)
        utilization = getattr(quota, "utilization", None)
        near_limit = utilization is not None and utilization >= threshold
        window = getattr(quota, "window", None) or "quota"

        if quota.status is not QuotaStatus.EXHAUSTED and not near_limit:
            if quota.status is not QuotaStatus.OK:
                logger.info(
                    "%s: harness reports %s on the %s window at %s — below the "
                    "%.0f%% threshold, continuing on the harness.",
                    self.name,
                    quota.status.value,
                    window,
                    f"{utilization:.0%}" if utilization is not None else "unknown",
                    threshold * 100,
                )
            return

        resets_at = getattr(quota, "resets_at", None)
        now = time.time()
        if quota.status is QuotaStatus.EXHAUSTED and resets_at and resets_at > now:
            # Genuinely out of capacity: wait for the real reset, but re-check at
            # least daily so a long window (seven_day) cannot silently route a
            # week of trading onto the paid API without another look.
            self._backoff_until = min(float(resets_at), now + 86_400)
        else:
            # Near the limit, or exhausted with no usable reset time. Back off one
            # cycle and re-check — utilisation may roll over, and a stale backoff
            # keeps paying for the API long after capacity returned.
            self._backoff_until = now + 3600

        self._backoff_reason = f"quota {quota.status.value} on the {window} window" + (
            f" at {utilization:.0%} utilisation" if utilization is not None else ""
        )
        logger.warning(
            "%s: %s — routing invocations to the %s until %s. THIS COSTS MONEY: "
            "the fallback is billed per token.",
            self.name,
            self._backoff_reason,
            "API fallback" if self.fallback_agent else "(no fallback configured)",
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._backoff_until)),
        )

    # ── Run ───────────────────────────────────────────────────────

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        from google.adk.events import Event

        if self._in_backoff() and self.fallback_agent is not None:
            logger.info(
                "%s: still in quota backoff (%s) — using the API fallback.",
                self.name,
                self._backoff_reason,
            )
            async for event in self.fallback_agent.run_async(ctx):
                yield event
            return

        request = AgentRequest(
            system_prompt=self._system_prompt(ctx),
            prompt=self._task_prompt(ctx),
            tools=list(self.local_tools),
            model=self.runtime.model or None,
            thinking=ThinkingSpec.parse(self.runtime.thinking),
            effort=self.runtime.effort,
            max_turns=self.runtime.max_turns,
            billing=BillingMode(self.runtime.billing),
            metadata={
                "server_name": "evotrader",
                "permission_mode": self.runtime.permission_mode,
                "agent": self.name,
            },
            on_event=self._make_event_sink(
                str(getattr(getattr(ctx, "session", None), "id", "") or ctx.invocation_id)
            ),
        )

        # A hosted agent that silently lost its market data still answers, it
        # just answers badly. Record what it was ACTUALLY shown so the cycle can
        # be graded afterwards: the app-log line above is invisible to
        # query_cycle_thoughts, to the console timeline and to the evolution
        # agent, so the 15:30 outage was reconstructable only because the
        # strategy agent happened to narrate it in prose.
        if request.on_event is not None:
            try:
                seen = list(
                    dict.fromkeys(_re.findall(r'<tool_result name="([^"]+)"', request.prompt))
                )
                missing = [t for t in _CRITICAL_TOOL_RESULTS if t not in seen]
                await request.on_event(
                    AgentEvent(
                        # Renders as a [runtime] note, same as the quota events.
                        kind="quota",
                        content=(
                            f"prompt: {len(request.prompt):,} chars; "
                            f"tool results: {', '.join(seen) or 'NONE'}"
                            + (f"; MISSING CRITICAL: {', '.join(missing)}" if missing else "")
                        ),
                        payload={"tool_results": seen, "missing_critical": missing},
                    )
                )
            except Exception:
                # Diagnostics must never take down a cycle.
                logger.warning(
                    "%s: could not record the prompt-composition diagnostic.",
                    self.name,
                    exc_info=True,
                )

        result = await self.backend.run(request)
        self._note_quota(result.quota)

        if result.ok and result.text:
            logger.info(
                "%s: hosted run complete — turns=%s billing=%s notional_cost=%s",
                self.name,
                result.turns,
                result.billing_used.value if result.billing_used else "?",
                f"${result.cost_usd:.4f}" if result.cost_usd is not None else "n/a",
            )
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                branch=getattr(ctx, "branch", None),
                content=genai_types.Content(
                    role="model", parts=[genai_types.Part(text=result.text)]
                ),
            )
            return

        # Failure. Fall back rather than returning nothing: a cycle with no
        # strategy output is a cycle that cannot trade.
        reason = result.error or "the hosted run produced no output"
        if self.fallback_agent is None:
            logger.error(
                "%s: hosted run failed (%s) and no fallback is configured.",
                self.name,
                reason,
            )
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                branch=getattr(ctx, "branch", None),
                error_message=f"{self.name} failed: {reason}",
                content=genai_types.Content(
                    role="model",
                    parts=[genai_types.Part(text=f"{self.name} could not run: {reason}")],
                ),
            )
            return

        logger.warning(
            "%s: hosted run failed (%s) — falling back to the API runtime for "
            "this cycle so the decision still happens.",
            self.name,
            reason,
        )
        async for event in self.fallback_agent.run_async(ctx):
            yield event

    # ── Console timeline ──────────────────────────────────────────

    def _make_event_sink(self, session_id: str) -> Any:
        """Mirror harness progress into the thought log, as the API path does.

        Uses the same ``record_event`` rows the ADK callbacks write, so the web
        console timeline renders a hosted run and an API run identically — the
        operator should not have to know which runtime produced a cycle.
        """
        if self.thought_logger is None:
            return None

        agent_name = self.name
        thought_logger = self.thought_logger

        async def sink(event: AgentEvent) -> None:
            if event.kind == "thought" and event.content:
                event_type, content, meta = "thought", event.content, None
            elif event.kind == "tool_call" and event.name:
                event_type, content = "tool_call", event.name
                meta = {"args": dict(event.payload or {})}
            elif event.kind == "quota":
                # A harness diagnostic is NOT reasoning. Logged as a 'thought'
                # it became the agent's LAST thought row — the backend emits the
                # quota check after the final report — and get_cycle_digest
                # showed '[runtime] quota ok' where the strategy's reasoning
                # should be, on 4 of 5 cycles on 2026-09-18. Its own event_type
                # means no consumer has to match on a string prefix.
                event_type, content = "runtime", f"[runtime] {event.content}"
                meta = dict(event.payload or {})
            else:
                return
            try:
                await thought_logger.record_event(
                    session_id=session_id,
                    agent_name=agent_name,
                    event_type=event_type,
                    content=content,
                    **({"meta": meta} if meta is not None else {}),
                )
            except Exception:  # telemetry must never break a trading cycle
                logger.debug("thought logging failed", exc_info=True)

        return sink


class SessionSharingAgentTool(AgentTool):
    """Run a hosted agent as a TOOL, inside the caller's own session.

    Two properties are needed at once, and no stock ADK construct has both.

    1. **Control must come BACK to the orchestrator**, so ``risk_manager`` and
       ``execution`` can still run. Adding a custom ``BaseAgent`` to
       ``sub_agents`` does NOT give this: ADK reaches it via
       ``transfer_to_agent``, which is a one-way handoff — ``base_llm_flow.py``
       runs the target, yields its events, and the parent generator simply ends.
       Measured cost of getting this wrong: sixteen consecutive cycles across
       2026-09-15/16 with zero orders placed, three of them carrying an explicit
       CLOSE proposal for a position that then sat unprotected through FOMC.

    2. **The agent must see the REAL cycle history**, because
       :meth:`CliBackedAgent._task_prompt` rebuilds its entire market briefing
       from ``ctx.session.events``. Stock ``AgentTool`` does NOT give this: it
       spins up an isolated ``InMemorySessionService`` whose only event is the
       ``request`` string, which would silently re-create the 2026-09-15 15:30Z
       outage where the agent was asked to size a trade with no market data.

    So: invoke the agent directly against the parent's ``InvocationContext``,
    swapping only the active agent, and return its text as the function
    response.
    """

    async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
        parent_ctx = tool_context._invocation_context
        # Same session, same events, same branch — only the active agent
        # changes. This is the line that preserves the briefing.
        child_ctx = parent_ctx.model_copy(update={"agent": self.agent})

        texts: list[str] = []
        async for event in self.agent.run_async(child_ctx):
            actions = getattr(event, "actions", None)
            if actions is not None and getattr(actions, "state_delta", None):
                tool_context.state.update(actions.state_delta)
            if text := _event_text(event):
                texts.append(text)

        result = "\n".join(texts).strip()
        if not result:
            # Never return silence: an empty proposal is indistinguishable from
            # "no trade" to the orchestrator, and that ambiguity is exactly the
            # failure mode this file exists to avoid.
            logger.error(
                "%s: hosted agent returned no text through the tool boundary.",
                self.agent.name,
            )
            return (
                f"ERROR: sub-agent '{self.agent.name}' produced no output. This is an "
                f"INFRASTRUCTURE FAULT, not a decision to stay flat. Do not read it "
                f"as a no-trade. Report it in the cycle summary."
            )
        return result
