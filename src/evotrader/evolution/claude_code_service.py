"""Evolution service backed by Claude Code instead of the ADK runner.

Why this exists: the evolution agent's job is to read source code, reason about
strategy logic, and write new Python. That is what Claude Code is built for, it
runs weekly rather than on the trading path, and a stall costs nothing — so it
is the one part of the system that can bill against a Claude subscription
instead of per-token API usage.

**Everything the web console shows keeps working.** This service is a drop-in
replacement for :class:`~evotrader.evolution.evolution_service.EvolutionService`
and exposes the same surface (``trigger``, ``continue_session``, ``is_running``,
``get_last_run``, ``mark_timeout``, ``thought_logger``). It records the same
``cycle_runs`` rows and the same ``agent_thought_log`` event types
(``thought`` / ``tool_call`` / ``tool_response``) and broadcasts the same SSE
events, because:

* the evolution *tools* run in-process (see
  :mod:`evotrader.evolution.claude_code_tools`), so proposals, code reviews,
  carry-forward notes and algorithm/instruction versions are written by the
  exact same code as the ADK backend; and
* Claude Code's message stream maps one-to-one onto the events the ADK loop
  records — ``TextBlock`` → ``thought``, ``ToolUseBlock`` → ``tool_call``,
  tool result → ``tool_response``.

Differences from the ADK backend, all deliberate:

* ``continue_session`` resumes Claude Code's own session by id rather than
  replaying DB rows into a reconstructed conversation. Claude Code keeps the
  transcript, so this is both simpler and lossless.
* Token usage is reported by ``ResultMessage`` rather than ADK's OTEL
  ``call_llm`` spans, so these runs do not appear in the telemetry token views.
  The per-run totals are logged and stored on the run's completion summary.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from evotrader.evolution.evolution_service import _stamp_carry_forward

logger = logging.getLogger(__name__)

# Claude Code's own system prompt is written for interactive coding. The
# evolution agent has its own instructions, so replace rather than append.
_SYSTEM_PROMPT_SUFFIX = """

## Operating context

You are running unattended as the EvoTrader evolution agent — there is no
human to answer questions mid-run, so do not ask any. Work through your
checklist and finish by filing your findings with the tools available to you.

All of your durable output must go through the `mcp__evotrader__*` tools.
Writing files directly does not register a proposal, and anything you only put
in prose is lost when this run ends. In particular:

- parameter changes  → `propose_parameter_change`
- new strategies     → `propose_new_strategy`
- weight changes     → `propose_composition_change`
- removals           → `propose_deprecation`
- infrastructure     → `submit_code_review`
- agent prompts      → `propose_instruction_change`
- cross-cycle notes  → `update_carry_forward`

You may read the repository directly with Read, Grep and Glob when that is
faster than `read_source_file`. You cannot edit files, run commands, or reach
the network: proposals are reviewed by a human before anything ships.
"""


class ClaudeCodeEvolutionService:
    """Runs the evolution cycle through the Claude Code agent loop."""

    def __init__(
        self,
        thought_logger: Any,
        config: Any,
        instructions: str,
        api_fallback: Callable[[], Any] | None = None,
    ) -> None:
        self.thought_logger = thought_logger
        self.config = config
        self._instructions = instructions
        # Builds the API-backed EvolutionService on demand, for
        # `quota.on_exhausted: "api"`. A FACTORY rather than an instance so the
        # ADK agent and its session service are only constructed if a quota wall
        # is actually hit — the common case never pays for them. Supplied by
        # main.py, which already owns the session service.
        self._api_fallback = api_fallback
        # Set when the harness reports the subscription window is spent. Read
        # after the stream ends and in the error path.
        self._quota: QuotaState | None = None
        self._is_running = False
        self._last_run: str | None = None
        self._timed_out = False
        self._mcp_server: Any = None
        # Maps our run session id → Claude Code's session id, so a timed-out
        # run can be resumed.
        self._claude_session_ids: dict[str, str] = {}
        # tool_use_id → tool name. Claude Code's tool_result blocks carry only
        # the id, but the web console labels responses by tool name.
        self._pending_tool_names: dict[str | None, str | None] = {}

    # ── Public surface (mirrors EvolutionService) ──────────────────

    def is_running(self) -> bool:
        return self._is_running

    def get_last_run(self) -> str | None:
        return self._last_run

    def mark_timeout(self) -> None:
        self._timed_out = True

    async def trigger(self, user_comment: str = "") -> dict[str, Any]:
        """Run a full evolution cycle through Claude Code."""
        if self._is_running:
            raise RuntimeError("Evolution is already running.")

        prompt = self._build_prompt(user_comment)
        session_id = f"cc-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
        return await self._run(session_id, prompt, resume=None)

    async def continue_session(self, session_id: str) -> dict[str, Any]:
        """Resume a timed-out or failed evolution cycle.

        Unlike the ADK backend there is no conversation to rebuild: Claude Code
        holds the transcript, so this resumes its session directly.
        """
        if self._is_running:
            raise RuntimeError("Evolution is already running.")

        claude_session = self._claude_session_ids.get(session_id)
        if not claude_session:
            claude_session = await self._recover_claude_session_id(session_id)
        if not claude_session:
            raise ValueError(
                f"No Claude Code session recorded for {session_id}; cannot resume. "
                "Trigger a fresh evolution run instead."
            )

        new_session_id = f"{session_id}-cont"
        prompt = (
            "Your previous evolution cycle was interrupted before you could file "
            "your findings. Your full analysis is still in this conversation.\n\n"
            "Continue where you left off — file your findings with the "
            "appropriate tools (submit_code_review, propose_parameter_change, "
            "update_carry_forward, and so on). Do NOT re-run your analysis; the "
            "earlier tool results are already above."
        )
        return await self._run(new_session_id, prompt, resume=claude_session)

    # ── Prompt construction (matches the ADK backend) ──────────────

    def _build_prompt(self, user_comment: str) -> str:
        prompt_text = (
            "Run a full self-evolution cycle.\n\n"
            "1. Start with `get_cycle_digest` to review recent trading cycles — "
            "it includes each agent's final reasoning, so you can spot patterns "
            "across cycles without needing raw logs.\n"
            "2. Run `analyse_performance` for quantitative metrics.\n"
            "3. If a specific cycle needs deeper investigation, use "
            "`query_cycle_thoughts` with agent_name/event_type filters to narrow "
            "down the data.\n"
            "4. Identify the weakest link and propose improvements "
            "(parameters, code, or system instructions).\n"
            "5. Update carry-forward notes using `update_carry_forward` — "
            "add new items, resolve completed ones, remove stale entries."
        )

        carry_forward_path = self.config.data_dir / "evolution" / "notes" / "carry_forward.md"
        if carry_forward_path.is_file():
            _stamp_carry_forward(carry_forward_path)
            carry_forward_text = carry_forward_path.read_text().strip()
            if carry_forward_text:
                prompt_text += (
                    "\n\n---\n\n"
                    "## Carry-Forward Notes (from prior evolution cycles)\n\n"
                    "Review these items. Implement any that overlap with your "
                    "current findings. Use `update_carry_forward` to add, "
                    "resolve, or remove items.\n\n"
                    f"{carry_forward_text}"
                )

        if user_comment:
            prompt_text += (
                "\n\n---\n\n"
                "## Operator Question / Comment\n"
                "The system operator has submitted the following input for this "
                "evolution cycle. Consider it during your analysis and address it "
                "in your final report:\n\n"
                f"> {user_comment}"
            )
            logger.info(
                "📝 Operator comment included in evolution prompt: %s",
                user_comment[:200],
            )

        return prompt_text

    # ── Options ───────────────────────────────────────────────────

    def _build_options(self, resume: str | None) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        from evotrader.evolution.claude_code_tools import (
            allowed_tool_patterns,
            build_evolution_mcp_server,
        )

        _, cc = self.config.settings.runtime_for("evolution")
        if cc is None:  # pragma: no cover - only reachable via direct construction
            from evotrader.models.config import CliRuntimeConfig

            cc = CliRuntimeConfig()

        if self._mcp_server is None:
            self._mcp_server = build_evolution_mcp_server()

        # Only the read-only built-ins, and only when enabled. Never Write,
        # Edit or Bash — every change must go through a proposal tool.
        builtin_tools = ["Read", "Grep", "Glob"] if cc.allow_file_tools else []

        options: dict[str, Any] = {
            "system_prompt": self._instructions + _SYSTEM_PROMPT_SUFFIX,
            "mcp_servers": {"evotrader": self._mcp_server},
            "allowed_tools": allowed_tool_patterns(cc.allow_file_tools),
            "tools": builtin_tools,
            "disallowed_tools": ["Write", "Edit", "Bash", "WebFetch", "WebSearch"],
            "permission_mode": cc.permission_mode,
            "max_turns": cc.max_turns,
            "cwd": str(self.config.project_root),
            # Don't inherit the host's CLAUDE.md, hooks, or project MCP servers:
            # an evolution run must behave the same on every machine.
            "setting_sources": [],
        }
        if cc.model:
            options["model"] = cc.model
        if resume:
            options["resume"] = resume
        if cc.effort:
            options["effort"] = cc.effort

        # Reasoning depth. `default` sends nothing, so the harness applies its
        # own — pinning a value we did not choose would let a future default
        # change rewrite behaviour silently, and on a subscription that shows up
        # as lost quota rather than a bill.
        thinking = _thinking_option(ThinkingSpec.parse(cc.thinking))
        if thinking is not None:
            options["thinking"] = thinking

        # Pin billing by withholding the credential we do not want used. Without
        # this the harness silently prefers ANTHROPIC_API_KEY from .env and bills
        # the API while the operator believes the subscription is in use — which
        # is exactly what happened here for a week, at ~$2.88 per run. Shares the
        # adapter's implementation so the policy exists in exactly one place.
        env = _billing_env(BillingMode(cc.billing))
        if env:
            options["env"] = env

        # Drop keys this SDK version doesn't know about rather than crashing.
        return _build_options_safely(ClaudeAgentOptions, options)

    # ── Core run loop ─────────────────────────────────────────────

    async def _run(
        self,
        session_id: str,
        prompt: str,
        resume: str | None,
    ) -> dict[str, Any]:
        from claude_agent_sdk import query

        from evotrader.web.server import _active_app, broadcast_sse_event

        self._is_running = True
        self._timed_out = False
        self._last_run = datetime.now(UTC).isoformat()

        if _active_app:
            _active_app.state.status = "evolving"
            _active_app.state.thoughts = []
            broadcast_sse_event("status", "evolving")
            broadcast_sse_event("clear_thoughts", {})

        logger.info(
            "🎬 Starting evolution cycle via Claude Code (session: %s%s)...",
            session_id,
            f", resuming {resume}" if resume else "",
        )

        try:
            await self.thought_logger.record_run_start(session_id, "EVOLUTION")
        except Exception as db_err:
            logger.warning("Could not insert RUNNING status into cycle_runs: %s", db_err)

        usage_summary: dict[str, Any] = {}

        try:
            options = self._build_options(resume)
            self._quota = None
            async for message in query(prompt=prompt, options=options):
                await self._handle_message(session_id, message, usage_summary)

            if self._quota is not None and self._quota.status is QuotaStatus.EXHAUSTED:
                return await self._on_quota_exhausted(session_id, prompt, usage_summary)

            summary = "Self-evolution completed successfully."
            if usage_summary:
                summary += f" {_format_usage(usage_summary)}"
            try:
                await self.thought_logger.record_run_completion(
                    session_id=session_id,
                    status="SUCCESS",
                    summary=summary,
                )
            except Exception as db_err:
                logger.warning("Could not update SUCCESS status in cycle_runs: %s", db_err)

        except asyncio.CancelledError:
            if self._timed_out:
                limit = self.config.settings.schedule.max_evolution_duration_seconds
                error_msg = f"Timed out after {limit}s"
                status = "TIMED_OUT"
            else:
                error_msg = "User cancelled."
                status = "CANCELLED"
            logger.info("Evolution cycle session %s: %s", session_id, error_msg)
            try:
                await self.thought_logger.record_run_completion(
                    session_id=session_id, status=status, error=error_msg
                )
            except Exception as db_err:
                logger.warning("Could not update %s in cycle_runs: %s", status, db_err)
            raise
        except Exception as e:
            # A 429 can surface as a raised exception rather than a result
            # message. Same reading as the adapter: treat it as the wall.
            if self._quota is None:
                probe = type("_P", (), {"errors": [str(e)], "result": ""})()
                self._quota = _quota_from_error(probe)
            if self._quota is not None and self._quota.status is QuotaStatus.EXHAUSTED:
                return await self._on_quota_exhausted(session_id, prompt, usage_summary)

            logger.error("Error during Claude Code evolution run: %s", e, exc_info=True)
            try:
                await self.thought_logger.record_run_completion(
                    session_id=session_id, status="FAILED", error=str(e)
                )
            except Exception as db_err:
                logger.warning("Could not update FAILED status in cycle_runs: %s", db_err)
            raise
        finally:
            self._is_running = False
            self._timed_out = False
            if _active_app:
                _active_app.state.status = "idle"
                broadcast_sse_event("status", "idle")
                logger.info("💤 Self-Evolution complete. Agent returned to idle state.")

        return {"status": "complete", "session_id": session_id, "usage": usage_summary}

    async def _handle_message(
        self,
        session_id: str,
        message: Any,
        usage_summary: dict[str, Any],
    ) -> None:
        """Translate one Claude Code stream message into DB + SSE events."""
        kind = type(message).__name__

        # ── QUOTA WALL ───────────────────────────────────────────────
        # Detected the same two ways the agent adapter does, so there is one
        # notion of "exhausted" in the codebase: an explicit RateLimitEvent, or
        # a failed ResultMessage whose text reads like a 429. Recorded here and
        # acted on after the stream ends — mid-stream is too late to do anything
        # useful, and the harness may still emit a clean result.
        if kind == "RateLimitEvent":
            info = getattr(message, "rate_limit_info", None)
            if info is not None:
                self._quota = _quota_from_event(info)
        elif kind == "ResultMessage" and getattr(message, "is_error", False):
            if (inferred := _quota_from_error(message)) is not None:
                self._quota = inferred

        # Remember Claude Code's session id so continue_session() can resume it.
        claude_session = getattr(message, "session_id", None)
        if claude_session and session_id not in self._claude_session_ids:
            self._claude_session_ids[session_id] = claude_session
            await self._record(
                session_id,
                "system",
                "thought",
                f"Claude Code session: {claude_session}",
                meta={"claude_session_id": claude_session},
            )

        if kind == "ResultMessage":
            usage = getattr(message, "usage", None)
            if usage:
                usage_summary["usage"] = usage if isinstance(usage, dict) else _as_dict(usage)
            cost = getattr(message, "total_cost_usd", None) or getattr(message, "cost", None)
            if cost is not None:
                usage_summary["estimated_cost_usd"] = cost
            # Record how the run was actually billed, established by which
            # credential the subprocess was given rather than assumed from
            # config. Without this the cost figure is ambiguous: on a
            # subscription the harness still reports what the run WOULD have
            # cost on the API, and an unlabelled "$2.88" reads as real spend.
            _, _runtime = self.config.settings.runtime_for("evolution")
            usage_summary["billing"] = getattr(_runtime, "billing", "api")
            logger.info("Claude Code evolution finished: %s", _format_usage(usage_summary))
            return

        content = getattr(message, "content", None)
        if not content:
            return
        if isinstance(content, str):
            await self._emit_thought(session_id, "evolution", content)
            return

        for block in content:
            block_kind = type(block).__name__

            if block_kind == "TextBlock":
                text = getattr(block, "text", "") or ""
                if text.strip():
                    await self._emit_thought(session_id, "evolution", text)

            elif block_kind == "ToolUseBlock":
                await self._emit_tool_call(
                    session_id,
                    _strip_mcp_prefix(getattr(block, "name", "unknown")),
                    getattr(block, "input", {}) or {},
                    getattr(block, "id", None),
                )

            elif block_kind == "ToolResultBlock":
                await self._emit_tool_response(
                    session_id,
                    getattr(block, "content", None),
                    getattr(block, "tool_use_id", None),
                    bool(getattr(block, "is_error", False)),
                )

            elif block_kind == "ThinkingBlock":
                thinking = getattr(block, "thinking", "") or ""
                if thinking.strip():
                    await self._emit_thought(session_id, "evolution", thinking, kind="thought")

    # ── Quota wall ────────────────────────────────────────────────

    async def _on_quota_exhausted(
        self,
        session_id: str,
        prompt: str,
        usage_summary: dict[str, Any],
    ) -> dict[str, Any]:
        """Act on `cli_runtimes.<runtime>.quota.on_exhausted`.

        Until 2026-09-18 this setting was read by nothing: the subscription run
        simply failed and the operator was told, in a config comment, that it
        would fall back to the API. It did not.

        A mid-run wall CANNOT be resumed on the API. The Claude Code session
        lives in the harness and the ADK service rebuilds its own conversation,
        so `"api"` means **restart the cycle on the API model**, repeating
        whatever the subscription run already did. That is the honest cost of
        the option, and it is why it is opt-in rather than the default.
        """
        detail = (self._quota.detail if self._quota else None) or "usage window spent"
        policy = "fail"
        try:
            _, runtime = self.config.settings.runtime_for("evolution")
            if runtime is not None and runtime.quota is not None:
                policy = str(runtime.quota.on_exhausted)
        except Exception:  # never let config shape turn a wall into a crash
            logger.warning("Could not read quota.on_exhausted; defaulting to 'fail'.")

        await self._record(
            session_id,
            "system",
            "thought",
            f"[runtime] subscription quota exhausted ({detail}); on_exhausted={policy}",
            meta={"quota_detail": detail, "on_exhausted": policy},
        )

        if policy == "skip":
            logger.warning(
                "Evolution: subscription quota exhausted (%s). on_exhausted='skip' "
                "— abandoning this cycle. Nothing was billed.",
                detail,
            )
            await self.thought_logger.record_run_completion(
                session_id=session_id,
                status="SKIPPED",
                error=f"Subscription quota exhausted ({detail}).",
            )
            return {
                "status": "skipped",
                "session_id": session_id,
                "reason": "quota_exhausted",
                "usage": usage_summary,
            }

        if policy != "api" or self._api_fallback is None:
            if policy == "api":
                logger.error(
                    "Evolution: quota exhausted and on_exhausted='api', but no API "
                    "fallback was wired in. Failing rather than pretending."
                )
            await self.thought_logger.record_run_completion(
                session_id=session_id,
                status="FAILED",
                error=f"Subscription quota exhausted ({detail}).",
            )
            raise RuntimeError(f"Subscription quota exhausted ({detail}).")

        # ── policy == "api" ──────────────────────────────────────────
        logger.error(
            "COST WARNING: evolution hit the subscription quota wall (%s) and "
            "on_exhausted='api'. RESTARTING the cycle on the API model, which is "
            "BILLED PER TOKEN. Work already done on the subscription run is "
            "repeated. Set quota.on_exhausted to 'skip' to wait for the window "
            "to reset instead.",
            detail,
        )
        await self._record(
            session_id,
            "system",
            "thought",
            "[runtime] restarting this cycle on the API runtime — BILLED PER "
            "TOKEN. The subscription run's progress is not carried over.",
            meta={"billing": "api", "restarted_after_quota": True},
        )
        try:
            service = self._api_fallback()
        except Exception:
            logger.error("Could not build the API fallback service.", exc_info=True)
            await self.thought_logger.record_run_completion(
                session_id=session_id,
                status="FAILED",
                error=f"Quota exhausted ({detail}); API fallback unavailable.",
            )
            raise

        result = await service.trigger()
        if isinstance(result, dict):
            result.setdefault("fell_back_to_api", True)
            result.setdefault("quota_detail", detail)
        return result

    # ── Event emission (same shapes as the ADK backend) ───────────

    async def _emit_thought(
        self,
        session_id: str,
        agent: str,
        text: str,
        kind: str = "thought",
    ) -> None:
        logger.info("Agent [%s]: %s", agent, text)
        self._broadcast(
            {
                "agent": agent,
                "type": kind,
                "content": text,
                "timestamp": datetime.now(UTC).isoformat(),
                "session_id": session_id,
            }
        )
        await self._record(session_id, agent, kind, text)

    async def _emit_tool_call(
        self,
        session_id: str,
        tool_name: str,
        args: dict,
        call_id: str | None,
    ) -> None:
        logger.info("Agent [evolution] calling tool: %s", tool_name)
        self._broadcast(
            {
                "agent": "evolution",
                "type": "tool_call",
                "tool_name": tool_name,
                "args": args,
                "timestamp": datetime.now(UTC).isoformat(),
                "session_id": session_id,
            }
        )
        meta: dict[str, Any] = {"args": args}
        if call_id:
            meta["tool_call_id"] = call_id
            # Track the call so its result can be labelled with the tool name.
            self._pending_tool_names[call_id] = tool_name
        await self._record(session_id, "evolution", "tool_call", tool_name, meta=meta)

    async def _emit_tool_response(
        self,
        session_id: str,
        content: Any,
        call_id: str | None,
        is_error: bool,
    ) -> None:
        tool_name = self._pending_tool_names.pop(call_id, None) or "unknown"
        response = _parse_tool_result(content)
        self._broadcast(
            {
                "agent": "evolution",
                "type": "tool_response",
                "tool_name": tool_name,
                "response": response,
                "timestamp": datetime.now(UTC).isoformat(),
                "session_id": session_id,
            }
        )
        meta: dict[str, Any] = {"response": response}
        if call_id:
            meta["tool_call_id"] = call_id
        if is_error:
            meta["is_error"] = True
        await self._record(session_id, "evolution", "tool_response", tool_name, meta=meta)

    def _broadcast(self, thought: dict) -> None:
        from evotrader.web.server import _active_app, broadcast_sse_event

        if not _active_app:
            return
        if not hasattr(_active_app.state, "thoughts"):
            _active_app.state.thoughts = []
        _active_app.state.thoughts.append(thought)
        broadcast_sse_event("thought", thought)

    async def _record(
        self,
        session_id: str,
        agent: str,
        event_type: str,
        content: str,
        meta: dict | None = None,
    ) -> None:
        try:
            await self.thought_logger.record_event(
                session_id=session_id,
                agent_name=agent,
                event_type=event_type,
                content=content,
                **({"meta": meta} if meta is not None else {}),
            )
        except Exception as db_err:
            logger.warning("Could not persist %s to DB: %s", event_type, db_err)

    async def _recover_claude_session_id(self, session_id: str) -> str | None:
        """Look up a run's Claude Code session id from the thought log."""
        try:
            events = await self.thought_logger.get_session_events(session_id)
        except Exception as db_err:
            logger.warning("Could not read events for %s: %s", session_id, db_err)
            return None

        for row in events or []:
            meta = row.get("meta")
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, TypeError):
                    continue
            if isinstance(meta, dict) and meta.get("claude_session_id"):
                return str(meta["claude_session_id"])
        return None


# ── Helpers ───────────────────────────────────────────────────────


def _strip_mcp_prefix(name: str) -> str:
    """``mcp__evotrader__analyse_performance`` → ``analyse_performance``.

    Keeps tool names in the web console identical between the two backends.
    """
    if name.startswith("mcp__"):
        return name.split("__")[-1]
    return name


def _parse_tool_result(content: Any) -> Any:
    """Normalise a tool_result payload into something JSON-serialisable.

    Our tools return JSON strings inside MCP text blocks, so unwrap those back
    into objects — that keeps the ``meta.response`` column shaped the way the
    ADK backend writes it, which the web console already renders.
    """
    if content is None:
        return {}

    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
            else:
                text = getattr(block, "text", None)
                if text:
                    texts.append(text)
        content = "\n".join(texts) if texts else content

    if isinstance(content, str):
        try:
            return json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return content

    if isinstance(content, dict):
        return content
    return str(content)


def _as_dict(obj: Any) -> dict:
    for attr in ("model_dump", "to_dict", "_asdict", "__dict__"):
        value = getattr(obj, attr, None)
        if callable(value):
            try:
                return dict(value())
            except Exception:
                continue
        elif isinstance(value, dict):
            return dict(value)
    return {}


def _format_usage(summary: dict[str, Any]) -> str:
    parts = []
    usage = summary.get("usage") or {}
    if isinstance(usage, dict):
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            if usage.get(key) is not None:
                parts.append(f"{key}={usage[key]}")
    billing = summary.get("billing")
    cost = summary.get("estimated_cost_usd")
    if cost is not None:
        # On a subscription the number is notional — what the run would have
        # cost on the API — so name it as such. Reporting it bare invites the
        # same misreading in reverse: a plan-billed run looking like a charge.
        label = "notional_cost" if billing == "subscription" else "est_cost"
        parts.append(f"{label}=${cost:.4f}")
    if billing:
        parts.append(f"billing={billing}")
    if not parts:
        return "usage not reported"
    return " ".join(parts)


# CLI discovery, availability and option building now live in the pluggable
# backend adapter, which is the single implementation shared by every agent that
# runs on a hosted harness. Re-exported here so existing callers and tests keep
# working against the names they already use.
from evotrader.agents.cli.claude_code import (  # noqa: E402
    ClaudeCodeBackend as _ClaudeCodeBackend,
)
from evotrader.agents.cli.claude_code import (  # noqa: E402
    _billing_env,
    _build_options_safely,
    _quota_from_error,
    _quota_from_event,
    _thinking_option,
)
from evotrader.agents.cli.types import (  # noqa: E402
    BillingMode,
    QuotaState,
    QuotaStatus,
    ThinkingSpec,
)


def is_available() -> tuple[bool, str]:
    """Whether the Claude Code harness can run here, and why not when it cannot."""
    return _ClaudeCodeBackend.is_available()
