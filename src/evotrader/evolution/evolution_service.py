"""Service layer for triggering and monitoring agent self-evolution."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.adk.events.event import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

logger = logging.getLogger(__name__)

_TIMESTAMP_RE = re.compile(
    r"^\*\*Last self-evolve run\*\*:\s*\S+",
    re.MULTILINE,
)


def _stamp_carry_forward(path: Path) -> None:
    """Update the ``Last self-evolve run`` timestamp in *path*.

    Idempotent — creates the line if missing, updates it if present.
    """
    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")
    stamp_line = f"**Last self-evolve run**: {now_str}"

    try:
        text = path.read_text()
    except FileNotFoundError:
        return

    if _TIMESTAMP_RE.search(text):
        text = _TIMESTAMP_RE.sub(stamp_line, text, count=1)
    else:
        # Insert after the first heading line
        lines = text.split("\n", 1)
        text = f"{lines[0]}\n\n{stamp_line}\n" + (lines[1] if len(lines) > 1 else "")

    path.write_text(text)


class EvolutionService:
    """Service to execute the Self-Evolution agent pipeline.

    Runs as a standalone ADK Runner session, recording events to the DB
    and broadcasting them via SSE. Mutually exclusive with the trading cycle.
    """

    def __init__(
        self,
        agent: Any,
        session_service: InMemorySessionService,
        thought_logger: Any,
        config: Any,
    ) -> None:
        self.agent = agent
        self.session_service = session_service
        self.thought_logger = thought_logger
        self.config = config
        self._is_running = False
        self._last_run: str | None = None
        self._timed_out = False

        # Build runner specifically for the evolution agent
        self.runner = Runner(
            agent=self.agent,
            app_name="evotrader",
            session_service=self.session_service,
        )

    def is_running(self) -> bool:
        """Check if an evolution run is currently active."""
        return self._is_running

    def get_last_run(self) -> str | None:
        """Get the timestamp of the last completed evolution run."""
        return self._last_run

    def mark_timeout(self) -> None:
        """Mark the current run as timed out (called by the server layer)."""
        self._timed_out = True

    async def trigger(self, user_comment: str = "") -> dict[str, Any]:
        """Run a full evolution cycle.

        Args:
            user_comment: Optional question or comment from the operator to
                address during this evolution cycle.

        Returns:
            A status summary of the complete execution.
        """
        if self._is_running:
            raise RuntimeError("Evolution is already running.")

        self._is_running = True
        self._timed_out = False
        self._last_run = datetime.now(UTC).isoformat()

        # Update status and broadcast to clients
        from evotrader.web.server import _active_app, broadcast_sse_event

        if _active_app:
            _active_app.state.status = "evolving"
            _active_app.state.thoughts = []
            broadcast_sse_event("status", "evolving")
            broadcast_sse_event("clear_thoughts", {})

        # Create session
        session = await self.session_service.create_session(
            app_name="evotrader",
            user_id="evolution_engine",
        )
        session_id = session.id
        logger.info("🎬 Starting evolution cycle (session: %s)...", session_id)

        # Insert a running state record for this evolution cycle
        try:
            await self.thought_logger.record_run_start(session_id, "EVOLUTION")
        except Exception as db_err:
            logger.warning("Could not insert RUNNING status into cycle_runs: %s", db_err)

        # Prompt the evolution agent to run its core checklist workflow
        prompt_text = (
            "Run a full self-evolution cycle.\n\n"
            "1. Start with `get_cycle_digest` to review recent trading cycles — "
            "it includes each agent's final reasoning, so you can spot patterns "
            "across cycles without needing raw logs.\n"
            "2. Run `analyse_performance` for quantitative metrics.\n"
            "3. If a specific cycle needs deeper investigation, use "
            "`query_cycle_thoughts` with agent_name/event_type filters to narrow down the data.\n"
            "4. Identify the weakest link and propose improvements "
            "(parameters, code, or system instructions).\n"
            "5. Update carry-forward notes using `update_carry_forward` — "
            "add new items, resolve completed ones, remove stale entries."
        )

        # ── Inject carry-forward notes ────────────────────────────
        # Read the carry_forward.md and append its content to the prompt
        # so the agent has cross-cycle context without a tool call.
        carry_forward_path = self.config.data_dir / "evolution" / "notes" / "carry_forward.md"
        if carry_forward_path.is_file():
            # Programmatically update the timestamp before injecting
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

        # Append the operator's comment/question if provided
        if user_comment:
            prompt_text += (
                "\n\n---\n\n"
                "## Operator Question / Comment\n"
                "The system operator has submitted the following input for this "
                "evolution cycle. Consider it during your analysis and address it "
                "in your final report:\n\n"
                f"> {user_comment}"
            )
            logger.info("📝 Operator comment included in evolution prompt: %s", user_comment[:200])

        prompt = types.Content(
            role="user",
            parts=[types.Part(text=prompt_text)],
        )

        return await self._run_session(session_id, prompt)

    async def continue_session(self, session_id: str) -> dict[str, Any]:
        """Continue a previously timed-out or failed evolution cycle.

        Reconstructs the ADK session from the ``agent_thought_log`` DB records
        and sends a "continue" prompt so the agent can resume where it left off.

        Args:
            session_id: The session ID of the failed/timed-out evolution cycle.

        Returns:
            A status summary of the continued execution.
        """
        if self._is_running:
            raise RuntimeError("Evolution is already running.")

        # ── Load historical events from DB ────────────────────────
        events = await self.thought_logger.get_session_events(session_id)
        if not events:
            raise ValueError(f"No events found for session {session_id}")

        logger.info(
            "🔄 Reconstructing session %s from %d DB events...",
            session_id,
            len(events),
        )

        self._is_running = True
        self._timed_out = False
        self._last_run = datetime.now(UTC).isoformat()

        from evotrader.web.server import _active_app, broadcast_sse_event

        if _active_app:
            _active_app.state.status = "evolving"
            _active_app.state.thoughts = []
            broadcast_sse_event("status", "evolving")
            broadcast_sse_event("clear_thoughts", {})

        # ── Create a fresh session and seed with history ──────────
        session = await self.session_service.create_session(
            app_name="evotrader",
            user_id="evolution_engine",
        )
        new_session_id = session.id

        # Reconstruct and append ADK events from DB records
        adk_events = _reconstruct_adk_events(events)
        for adk_event in adk_events:
            await self.session_service.append_event(session, adk_event)

        logger.info(
            "✅ Session reconstructed: %d DB events → %d ADK events. Continuing as session %s...",
            len(events),
            len(adk_events),
            new_session_id,
        )

        # Insert a running state record linked to the ORIGINAL session
        # so the timeline stays together.
        try:
            await self.thought_logger.record_run_start(new_session_id, "EVOLUTION")
            # Record a link event so we know this is a continuation
            await self.thought_logger.record_event(
                session_id=new_session_id,
                agent_name="system",
                event_type="thought",
                content=f"Continuation of session {session_id}",
                meta={"original_session_id": session_id, "is_continuation": True},
            )
        except Exception as db_err:
            logger.warning("Could not record continuation metadata: %s", db_err)

        # ── Send the "continue" prompt ────────────────────────────
        continue_prompt = types.Content(
            role="user",
            parts=[
                types.Part(
                    text=(
                        "Your previous evolution cycle was interrupted (timed out) before "
                        "you could file your findings. Your full analysis up to that point "
                        "has been restored in this conversation.\n\n"
                        "Continue where you left off — file your findings using the "
                        "appropriate tools (submit_code_review, propose_parameter_change, "
                        "update_carry_forward, etc.). Do NOT re-run your analysis; your "
                        "previous tool call results are already in this conversation."
                    )
                )
            ],
        )

        return await self._run_session(new_session_id, continue_prompt)

    async def _run_session(
        self,
        session_id: str,
        prompt: types.Content,
    ) -> dict[str, Any]:
        """Run the evolution agent and process events.

        This is the core event loop shared by both ``trigger()`` and
        ``continue_session()``.
        """
        from evotrader.web.server import _active_app, broadcast_sse_event

        try:
            async for event in self.runner.run_async(
                user_id="evolution_engine",
                session_id=session_id,
                new_message=prompt,
            ):
                author = getattr(event, "author", "unknown")

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

                    thought = {
                        "agent": author,
                        "type": "thought",
                        "content": text_content,
                        "timestamp": datetime.now(UTC).isoformat(),
                        "session_id": session_id,
                    }
                    if _active_app:
                        if not hasattr(_active_app.state, "thoughts"):
                            _active_app.state.thoughts = []
                        _active_app.state.thoughts.append(thought)
                        broadcast_sse_event("thought", thought)

                    try:
                        await self.thought_logger.record_event(
                            session_id=session_id,
                            agent_name=author,
                            event_type="thought",
                            content=text_content,
                        )
                    except Exception as db_err:
                        logger.warning("Could not persist evolution agent thought: %s", db_err)

                # 2. Extract function calls
                func_calls = (
                    event.get_function_calls() if hasattr(event, "get_function_calls") else []
                )
                for fc in func_calls:
                    fc_name = getattr(fc, "name", "unknown")
                    fc_args = getattr(fc, "args", {})
                    fc_id = getattr(fc, "id", None)

                    logger.info("Agent [%s] calling tool: %s", author, fc_name)

                    tool_thought = {
                        "agent": author,
                        "type": "tool_call",
                        "tool_name": fc_name,
                        "args": fc_args,
                        "timestamp": datetime.now(UTC).isoformat(),
                        "session_id": session_id,
                    }
                    if _active_app:
                        if not hasattr(_active_app.state, "thoughts"):
                            _active_app.state.thoughts = []
                        _active_app.state.thoughts.append(tool_thought)
                        broadcast_sse_event("thought", tool_thought)

                    try:
                        meta: dict[str, Any] = {"args": fc_args}
                        if fc_id:
                            meta["tool_call_id"] = fc_id
                        await self.thought_logger.record_event(
                            session_id=session_id,
                            agent_name=author,
                            event_type="tool_call",
                            content=fc_name,
                            meta=meta,
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
                    fr_id = getattr(fr, "id", None)

                    resp_thought = {
                        "agent": author,
                        "type": "tool_response",
                        "tool_name": fr_name,
                        "response": fr_resp,
                        "timestamp": datetime.now(UTC).isoformat(),
                        "session_id": session_id,
                    }
                    if _active_app:
                        if not hasattr(_active_app.state, "thoughts"):
                            _active_app.state.thoughts = []
                        _active_app.state.thoughts.append(resp_thought)
                        broadcast_sse_event("thought", resp_thought)

                    try:
                        meta_resp: dict[str, Any] = {"response": fr_resp}
                        if fr_id:
                            meta_resp["tool_call_id"] = fr_id
                        await self.thought_logger.record_event(
                            session_id=session_id,
                            agent_name=author,
                            event_type="tool_response",
                            content=fr_name,
                            meta=meta_resp,
                        )
                    except Exception as db_err:
                        logger.warning("Could not persist tool response to DB: %s", db_err)

            # Mark the run as successful in cycle_runs
            try:
                await self.thought_logger.record_run_completion(
                    session_id=session_id,
                    status="SUCCESS",
                    summary="Self-evolution completed successfully.",
                )
            except Exception as db_err:
                logger.warning("Could not update SUCCESS status in cycle_runs: %s", db_err)

        except asyncio.CancelledError:
            if self._timed_out:
                error_msg = f"Timed out after {self.config.settings.schedule.max_evolution_duration_seconds}s"
                status = "TIMED_OUT"
            else:
                error_msg = "User cancelled."
                status = "CANCELLED"
            logger.info("Evolution cycle session %s: %s", session_id, error_msg)
            try:
                await self.thought_logger.record_run_completion(
                    session_id=session_id,
                    status=status,
                    error=error_msg,
                )
            except Exception as db_err:
                logger.warning("Could not update %s status in cycle_runs: %s", status, db_err)
            raise
        except Exception as e:
            logger.error("Error during evolution run: %s", e, exc_info=True)
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

        return {"status": "complete", "session_id": session_id}


# ── Session reconstruction helpers ────────────────────────────────


def _reconstruct_adk_events(db_events: list[dict]) -> list[Event]:
    """Convert ``agent_thought_log`` rows into ADK ``Event`` objects.

    The DB stores events individually (one row per thought, tool_call, or
    tool_response). The ADK groups concurrent tool calls/responses into a
    single ``Content`` with multiple ``Part`` objects. This function
    re-groups them accordingly.

    Grouping rules:
    - Consecutive ``tool_call`` rows → single model Content with multiple
      FunctionCall parts.
    - Consecutive ``tool_response`` rows → single user Content with multiple
      FunctionResponse parts.
    - ``thought`` rows → standalone model Content.

    Anthropic/OpenAI require ``id`` fields on FunctionCall/FunctionResponse
    pairs. The DB stores the real ``tool_call_id`` in the ``meta`` column
    when available. For legacy data without stored IDs, synthetic IDs are
    generated and paired positionally as a fallback.
    """
    adk_events: list[Event] = []
    invocation_id = "reconstructed"
    fallback_counter = 0
    # Pending call IDs to pair with upcoming responses (fallback only)
    pending_call_ids: list[str] = []
    i = 0

    while i < len(db_events):
        row = db_events[i]
        event_type = row["event_type"]

        if event_type == "thought":
            adk_events.append(
                Event(
                    invocation_id=invocation_id,
                    author=row["agent_name"],
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text=row["content"])],
                    ),
                )
            )
            i += 1

        elif event_type == "tool_call":
            # Group consecutive tool_call rows
            parts: list[types.Part] = []
            author = row["agent_name"]
            group_ids: list[str] = []
            while i < len(db_events) and db_events[i]["event_type"] == "tool_call":
                r = db_events[i]
                meta = _parse_meta(r.get("meta"))
                args = meta.get("args", {}) if meta else {}
                # Use stored ID if available, otherwise generate synthetic
                call_id = (
                    meta.get("tool_call_id") if meta else None
                ) or f"recon_{fallback_counter}"
                if not (meta and meta.get("tool_call_id")):
                    fallback_counter += 1
                group_ids.append(call_id)
                parts.append(
                    types.Part(
                        function_call=types.FunctionCall(
                            id=call_id,
                            name=r["content"],
                            args=args,
                        )
                    )
                )
                i += 1
            # Store IDs so the next response group can reference them
            pending_call_ids = group_ids
            adk_events.append(
                Event(
                    invocation_id=invocation_id,
                    author=author,
                    content=types.Content(role="model", parts=parts),
                )
            )

        elif event_type == "tool_response":
            # Group consecutive tool_response rows
            parts_fr: list[types.Part] = []
            author = row["agent_name"]
            resp_idx = 0
            while i < len(db_events) and db_events[i]["event_type"] == "tool_response":
                r = db_events[i]
                meta = _parse_meta(r.get("meta"))
                response = meta.get("response", {}) if meta else {}
                # Use stored ID if available, else match with pending call ID
                resp_id = meta.get("tool_call_id") if meta else None
                if not resp_id:
                    resp_id = (
                        pending_call_ids[resp_idx]
                        if resp_idx < len(pending_call_ids)
                        else f"recon_{fallback_counter + resp_idx}"
                    )
                parts_fr.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=resp_id,
                            name=r["content"],
                            response=response,
                        )
                    )
                )
                resp_idx += 1
                i += 1
            pending_call_ids = []
            adk_events.append(
                Event(
                    invocation_id=invocation_id,
                    author=author,
                    content=types.Content(role="user", parts=parts_fr),
                )
            )

        else:
            # Skip unknown event types (e.g., "continuation")
            i += 1

    return adk_events


def _parse_meta(meta_val: Any) -> dict | None:
    """Parse the ``meta`` column which may be a JSON string or already a dict."""
    if meta_val is None:
        return None
    if isinstance(meta_val, dict):
        return meta_val
    try:
        return json.loads(meta_val)
    except (json.JSONDecodeError, TypeError):
        return None
