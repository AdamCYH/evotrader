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

import asyncio
import contextlib
import copy
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
    AgentResult,
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

# How long one stage may take, harness and API fallback together, unless the
# runtime sets ``max_run_seconds``: past it the stage reports no decision. A
# healthy run takes minutes; this leaves room for a slow one.
_DEFAULT_MAX_RUN_SECONDS = 1200.0

# Model calls the API fallback may make without an answer, unless the runtime
# sets ``fallback_max_model_calls``. A decision takes a handful; each is billed.
_DEFAULT_FALLBACK_MODEL_CALLS = 25


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


def _parts(event: Any) -> list[Any]:
    return list(getattr(getattr(event, "content", None), "parts", None) or [])


def _calls_a_tool(event: Any) -> bool:
    return any(getattr(p, "function_call", None) is not None for p in _parts(event))


def _is_final(event: Any) -> bool:
    """An answer, not a step: no tool call or tool result, not a fragment."""
    check = getattr(event, "is_final_response", None)
    if callable(check):
        return bool(check())
    return not getattr(event, "partial", False) and not any(
        getattr(p, "function_call", None) is not None
        or getattr(p, "function_response", None) is not None
        for p in _parts(event)
    )


def _answer_text(event: Any) -> str:
    """The plain text of an answer: no tool results, no thinking."""
    return "\n".join(
        p.text
        for p in _parts(event)
        if getattr(p, "text", None) and not getattr(p, "thought", False)
    ).strip()


def _with(obj: Any, **changes: Any) -> Any:
    """A shallow copy of *obj* with *changes*, for a pydantic model or a plain object."""
    if hasattr(obj, "model_copy"):
        return obj.model_copy(update=changes)
    clone = copy.copy(obj)
    for key, value in changes.items():
        setattr(clone, key, value)
    return clone


def _clock(epoch: float) -> str:
    """An epoch time as US Eastern wall-clock time, e.g. ``10:05 ET``."""
    from datetime import datetime

    from evotrader.tools.market_hours import ET

    return datetime.fromtimestamp(epoch, ET).strftime("%H:%M ET")


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:.0f}-minute"


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

    on_exhausted: str = "api"
    """``quota.on_exhausted`` of the runtime. ``api``: while the plan is spent,
    the fallback decides, billed per token. ``skip`` and ``fail``: no decision
    until the window resets, and the cycle's record says so."""

    thought_logger: Any = None

    _backoff_until: float = PrivateAttr(default=0.0)
    _backoff_reason: str = PrivateAttr(default="")
    _backoff_exhausted: bool = PrivateAttr(default=False)
    _outcome: dict[str, Any] = PrivateAttr(default_factory=dict)

    # ── What the cycle reads ──────────────────────────────────────

    def out_of_quota(self) -> str | None:
        """Why the plan cannot run this agent now, or None.

        Only a spent plan still inside its window: one near its threshold can
        still answer on the harness.
        """
        if self._in_backoff() and self._backoff_exhausted:
            return f"{self._backoff_reason}, until {_clock(self._backoff_until)}"
        return None

    def decides_without_quota(self) -> bool:
        """Whether a spent plan still gets a decision: from the API fallback."""
        return self.on_exhausted == "api" and self.fallback_agent is not None

    def outcome(self, session_id: str) -> dict[str, Any] | None:
        """How this agent's run in *session_id* ended, or None if it did not run there.

        ``decided`` is whether it answered; ``via`` says where (``harness`` or
        ``api_fallback``); a run that did not answer says why in ``reason``,
        and a spent plan names ``on_exhausted``.
        """
        if self._outcome.get("session_id") != session_id:
            return None
        return dict(self._outcome)

    def _max_run_seconds(self) -> float:
        value = getattr(self.runtime, "max_run_seconds", None)
        return float(value) if value else _DEFAULT_MAX_RUN_SECONDS

    def _fallback_model_calls(self) -> int:
        value = getattr(self.runtime, "fallback_max_model_calls", None)
        return int(value) if value else _DEFAULT_FALLBACK_MODEL_CALLS

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
        near the limit. Log an advisory warning below that and carry on. Near
        the limit is only worth acting on when there is somewhere to go: with
        ``on_exhausted`` 'skip' or 'fail' the harness keeps answering until the
        plan is actually spent.
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
        exhausted = quota.status is QuotaStatus.EXHAUSTED
        if not exhausted and not self.decides_without_quota():
            logger.info(
                "%s: harness near its limit on the %s window; quota.on_exhausted=%r, "
                "so it keeps answering on the harness until the plan is spent.",
                self.name,
                window,
                self.on_exhausted,
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

        self._backoff_exhausted = exhausted
        self._backoff_reason = f"quota {quota.status.value} on the {window} window" + (
            f" at {utilization:.0%} utilisation" if utilization is not None else ""
        )
        if self.decides_without_quota():
            logger.warning(
                "%s: %s — the API fallback decides until %s. THIS COSTS MONEY: "
                "the fallback is billed per token.",
                self.name,
                self._backoff_reason,
                _clock(self._backoff_until),
            )
        else:
            logger.warning(
                "%s: %s — no decision until %s (quota.on_exhausted=%r%s).",
                self.name,
                self._backoff_reason,
                _clock(self._backoff_until),
                self.on_exhausted,
                "" if self.fallback_agent is not None else ", no fallback configured",
            )

    # ── Run ───────────────────────────────────────────────────────

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        from google.adk.events import Event

        session_id = str(getattr(getattr(ctx, "session", None), "id", "") or ctx.invocation_id)
        self._outcome = {"session_id": session_id, "decided": False}
        sink = self._make_event_sink(session_id)
        limit = self._max_run_seconds()
        deadline = time.monotonic() + limit

        if self._in_backoff():
            if self.decides_without_quota():
                logger.info(
                    "%s: still in quota backoff (%s) — using the API fallback.",
                    self.name,
                    self._backoff_reason,
                )
                reason = f"subscription {self._backoff_reason}, until {_clock(self._backoff_until)}"
                async for event in self._run_fallback(
                    ctx, session_id, sink, reason, deadline, limit
                ):
                    yield event
            else:
                reason = f"subscription {self.out_of_quota() or self._backoff_reason}"
                yield await self._no_decision(ctx, sink, reason, on_exhausted=self.on_exhausted)
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
            on_event=sink,
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

        result = await self._run_harness(request, deadline, limit)
        self._note_quota(result.quota)

        if result.ok and result.text:
            logger.info(
                "%s: hosted run complete — turns=%s billing=%s notional_cost=%s",
                self.name,
                result.turns,
                result.billing_used.value if result.billing_used else "?",
                f"${result.cost_usd:.4f}" if result.cost_usd is not None else "n/a",
            )
            self._outcome.update(decided=True, via="harness")
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
        # strategy output is a cycle that cannot trade. A spent plan falls back
        # only when quota.on_exhausted says to spend money on it.
        reason = result.error or "the hosted run produced no output"
        if result.quota_exhausted:
            reason = f"subscription {self.out_of_quota() or 'quota exhausted'}"
        if self.fallback_agent is None or (
            result.quota_exhausted and not self.decides_without_quota()
        ):
            yield await self._no_decision(
                ctx,
                sink,
                reason,
                on_exhausted=self.on_exhausted if result.quota_exhausted else None,
            )
            return

        logger.warning(
            "%s: hosted run failed (%s) — falling back to the API runtime for "
            "this cycle so the decision still happens.",
            self.name,
            reason,
        )
        async for event in self._run_fallback(ctx, session_id, sink, reason, deadline, limit):
            yield event

    async def _run_harness(self, request: AgentRequest, deadline: float, limit: float) -> Any:
        """The hosted run, stopped at the stage's deadline."""
        try:
            return await asyncio.wait_for(
                self.backend.run(request), timeout=max(1.0, deadline - time.monotonic())
            )
        except TimeoutError:
            logger.error("%s: the hosted run passed its %.0f s limit; stopped.", self.name, limit)
            return AgentResult(
                ok=False,
                error=f"the hosted run did not finish within its {_minutes(limit)} limit",
            )

    async def _run_fallback(
        self,
        ctx: InvocationContext,
        session_id: str,
        sink: Any,
        reason: str,
        deadline: float,
        limit: float,
    ) -> AsyncGenerator[Event, None]:
        """Let the API agent decide, with its own steps in front of it, and bounded.

        An ``LlmAgent`` builds every model request from the session's events,
        and the Runner adds each event the agent yields to the session before
        the agent's next step. Run here, inside a tool call, nothing added
        them: every request was the first one again, so the fallback called
        its first tools over and over and never answered, billed per token,
        until the app was stopped or the cycle cancelled, and none of it
        reached the thought log.

        So it gets a private copy of the cycle's history, and each of its steps
        is added to that copy before its next one. The shared session, and so
        the orchestrator's own context, is left as it was. Its thoughts and
        tool calls reach the thought log as a hosted run's do. It stops at the
        stage's deadline, or after ``fallback_max_model_calls`` model calls
        without an answer, and then reports no decision.
        """
        cap = self._fallback_model_calls()
        logger.warning(
            "%s: %s — the API fallback decides this cycle. THIS COSTS MONEY: it is "
            "billed per token (at most %d model calls).",
            self.name,
            reason,
            cap,
        )
        await self._runtime_note(
            sink,
            f"{reason}: the API fallback decides this cycle, billed per token "
            f"(at most {cap} model calls)",
            {"fallback": "api", "reason": reason, "max_model_calls": cap},
        )

        history = list(getattr(getattr(ctx, "session", None), "events", None) or [])
        fallback_ctx = _with(ctx, session=_with(ctx.session, events=history))
        name = getattr(self.fallback_agent, "name", None)
        calls = 0
        answer = ""
        stopped: str | None = None
        stream = self.fallback_agent.run_async(fallback_ctx)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stopped = f"it reached the stage's {_minutes(limit)} limit"
                    break
                try:
                    event = await asyncio.wait_for(anext(stream), timeout=remaining)
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    stopped = f"it reached the stage's {_minutes(limit)} limit"
                    break
                except Exception as exc:  # a provider error ends the stage, not the cycle
                    logger.error("%s: the API fallback failed: %s", self.name, exc, exc_info=True)
                    stopped = f"it failed: {exc}"
                    break
                if not getattr(event, "partial", False):
                    history.append(event)
                    content = getattr(event, "content", None)
                    if (
                        getattr(event, "author", None) == name
                        and getattr(content, "role", None) == "model"
                    ):
                        calls += 1
                    await self._mirror(session_id, event)
                    if _is_final(event):
                        answer = _answer_text(event) or answer
                yield event
                if calls >= cap and not answer and _calls_a_tool(event):
                    stopped = f"it reached {cap} model calls without an answer"
                    break
        finally:
            with contextlib.suppress(Exception):
                await stream.aclose()

        if answer:
            logger.info("%s: the API fallback decided in %d model call(s).", self.name, calls)
            self._outcome.update(decided=True, via="api_fallback", model_calls=calls)
            return
        why = stopped or "it ended without an answer"
        self._outcome["model_calls"] = calls
        yield await self._no_decision(
            ctx, sink, f"the API fallback made no decision: {why} ({calls} model call(s))"
        )

    async def _no_decision(
        self,
        ctx: InvocationContext,
        sink: Any,
        reason: str,
        *,
        on_exhausted: str | None = None,
    ) -> Event:
        """The stage's answer when it made no decision: says so, never silence."""
        from google.adk.events import Event

        self._outcome.update(decided=False, reason=reason, on_exhausted=on_exhausted)
        note = f"no decision this cycle: {reason}" + (
            f" (quota.on_exhausted={on_exhausted!r})" if on_exhausted else ""
        )
        logger.error("%s: %s", self.name, note)
        await self._runtime_note(
            sink, note, {"decided": False, "reason": reason, "on_exhausted": on_exhausted}
        )
        return Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            branch=getattr(ctx, "branch", None),
            error_message=f"{self.name} failed: {reason}",
            content=genai_types.Content(
                role="model",
                parts=[
                    genai_types.Part(
                        text=(
                            f"{self.name} could not run: {reason}. No decision was made "
                            "this cycle; this is not a decision to stay flat."
                        )
                    )
                ],
            ),
        )

    async def _runtime_note(self, sink: Any, content: str, payload: dict[str, Any]) -> None:
        """A ``[runtime]`` row in the thought log, the way the harness's quota notes are."""
        if sink is None:
            return
        with contextlib.suppress(Exception):
            await sink(AgentEvent(kind="quota", content=content, payload=payload))

    async def _mirror(self, session_id: str, event: Any) -> None:
        """The fallback's thoughts and tool calls, as rows like a hosted run's."""
        if self.thought_logger is None:
            return
        for part in _parts(event):
            if getattr(part, "thought", False):
                continue
            call = getattr(part, "function_call", None)
            if text := getattr(part, "text", None):
                row = ("thought", text, {"runtime": "api_fallback"})
            elif call is not None:
                args = dict(getattr(call, "args", None) or {})
                row = ("tool_call", call.name, {"args": args, "runtime": "api_fallback"})
            else:
                continue
            try:
                await self.thought_logger.record_event(
                    session_id=session_id,
                    agent_name=self.name,
                    event_type=row[0],
                    content=row[1],
                    meta=row[2],
                )
            except Exception:  # telemetry must never break a trading cycle
                logger.debug("thought logging failed", exc_info=True)

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
    swapping only the active agent, and return its answer as the function
    response: the text of its final events, not its intermediate steps (an
    API fallback's tool calls and tool results are steps, not the proposal).
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
            if _is_final(event) and (text := _event_text(event)):
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


def hosted_agents(root: Any) -> dict[str, CliBackedAgent]:
    """The hosted agents an orchestrator reaches as tools, by name."""
    found: dict[str, CliBackedAgent] = {}
    for tool in getattr(root, "tools", None) or []:
        agent = getattr(tool, "agent", None)
        if isinstance(tool, SessionSharingAgentTool) and isinstance(agent, CliBackedAgent):
            found[agent.name] = agent
    return found
