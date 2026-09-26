"""Claude Code adapter for the CLI agent backend contract.

Translates the vendor-neutral request/result vocabulary onto ``claude_agent_sdk``
and back. Everything Anthropic-specific in the hosted-agent path should live in
this file — if a concept leaks into the neutral types, adding a second harness
gets harder.

Two translations carry real weight:

**Quota.** The CLI emits ``RateLimitEvent`` whenever plan state changes, with a
status of ``allowed``/``allowed_warning``/``rejected``. We map those onto
:class:`QuotaStatus` so a caller can act on the *warning* rather than only on the
wall. That distinction is the whole reason a quota-limited backend is safe to put
on the trading path: a warning lets the next cycle route elsewhere before
anything fails.

**Structured output.** The strategy agent's answer feeds a downstream agent, so
it needs a schema, not prose. ``output_format`` gives us that, but the SDK
version may not support it — hence ``_build_options_safely``, which drops
unknown options with a warning instead of failing the run.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from evotrader.agents.cli.base import CliAgentBackend, register_backend
from evotrader.agents.cli.types import (
    AgentEvent,
    AgentRequest,
    AgentResult,
    BillingMode,
    LocalTool,
    QuotaState,
    QuotaStatus,
    ThinkingSpec,
)

logger = logging.getLogger(__name__)

# Neutral capability names -> Claude Code's own built-in tool names. Anything we
# cannot map is dropped: a missing built-in costs convenience, never
# correctness, because every tool that matters arrives as a LocalTool.
_BUILTIN_MAP = {
    "read_files": "Read",
    "search_files": "Grep",
    "list_files": "Glob",
}

# Never available to a hosted agent on the trading path, regardless of config.
# The agent's job is to decide and report; anything that mutates the repo or
# reaches the network must go through an explicit tool we wrote.
_ALWAYS_BLOCKED = ("Write", "Edit", "Bash", "WebFetch", "WebSearch", "NotebookEdit")


# ── Billing enforcement ───────────────────────────────────────────

_API_KEY_ENV = "ANTHROPIC_API_KEY"
_OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"

# Phrases the CLI uses when the credential it was given is missing or rejected.
# Matched on text because the SDK surfaces these as a generic result error, and
# the caller's response differs sharply from a quota wall: authenticate, rather
# than wait for a window to reset or retry on another plan.
_AUTH_FAILURE_MARKERS = (
    "not logged in",
    "please run /login",
    "api key is invalid",
    "failed to authenticate",
    "invalid bearer token",
    "oauth token has expired",
    "authentication_error",
)


def subscription_credential_source() -> str | None:
    """Where a subscription credential could come from, or None if nowhere.

    Deliberately does not consult the macOS Keychain. An interactive login
    stores its token somewhere we cannot read portably or without risking a
    prompt, so a negative result here is "not provably present", not "absent".
    That is why this is only used for a friendlier *early* message — the real
    guarantee comes from withholding the API key and letting the run prove it.
    """
    import os

    if os.environ.get(_OAUTH_TOKEN_ENV, "").strip():
        return _OAUTH_TOKEN_ENV
    if (Path.home() / ".claude" / ".credentials.json").is_file():
        return "~/.claude/.credentials.json"
    return None


def _billing_env(mode: BillingMode) -> dict[str, str]:
    """Environment overrides that pin billing to *mode*.

    ``options.env`` merges over ``os.environ`` rather than replacing it, so a
    variable cannot be deleted through it — but the CLI treats an empty value as
    unset (verified against the installed CLI), so blanking achieves the same
    thing without mutating this process's environment. That matters: the ADK
    fallback path in the same process still needs the real API key, and a
    trading cycle may be running while an evolution cycle is not yet finished.
    """
    if mode is BillingMode.API:
        return {}
    # SUBSCRIPTION and AUTO both start by making API billing impossible.
    return {_API_KEY_ENV: ""}


def _looks_like_auth_failure(*texts: Any) -> bool:
    haystack = " ".join(str(t or "") for t in texts).lower()
    return any(marker in haystack for marker in _AUTH_FAILURE_MARKERS)


# ── CLI discovery ─────────────────────────────────────────────────


def _candidate_cli_paths() -> list[Path]:
    """Well-known install locations for the ``claude`` CLI, best first.

    PATH alone is not reliable here. nvm puts the binary under a specific Node
    version and exports PATH from shell startup, so a server launched from an
    IDE, a supervisor, or a shell that never sourced nvm will not see it — even
    though the same binary runs fine in an interactive terminal.
    """
    home = Path.home()
    candidates: list[Path] = []

    # nvm: prefer the aliased default version, then any installed version.
    nvm_versions = home / ".nvm" / "versions" / "node"
    default_alias = home / ".nvm" / "alias" / "default"
    preferred: str | None = None
    if default_alias.is_file():
        try:
            preferred = default_alias.read_text().strip()
        except OSError:
            preferred = None
    if nvm_versions.is_dir():
        versions = sorted(nvm_versions.iterdir(), reverse=True)
        if preferred:
            # The alias may be a major ("20") rather than a full version.
            versions.sort(key=lambda d: not d.name.lstrip("v").startswith(preferred.lstrip("v")))
        candidates += [d / "bin" / "claude" for d in versions]

    candidates += [
        home / ".claude" / "local" / "claude",
        home / ".local" / "bin" / "claude",
        home / ".npm-global" / "bin" / "claude",
        home / ".bun" / "bin" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ]
    return candidates


def resolve_claude_cli() -> Path | None:
    """Locate the ``claude`` executable, searching beyond PATH.

    When found outside PATH, its directory is prepended to ``os.environ['PATH']``
    so the SDK's subprocess inherits it. Without that the check would pass and
    the actual run would still fail.
    """
    import os
    import shutil

    found = shutil.which("claude")
    if found:
        return Path(found)

    for candidate in _candidate_cli_paths():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            bin_dir = str(candidate.parent)
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
            logger.info(
                "Found 'claude' CLI outside PATH at %s — prepended %s to PATH for this process.",
                candidate,
                bin_dir,
            )
            return candidate
    return None


def _build_options_safely(options_cls: Any, values: dict[str, Any]) -> Any:
    """Construct ``ClaudeAgentOptions``, dropping fields this SDK lacks.

    The SDK moves faster than this project; an unknown option should degrade to
    a warning rather than breaking every run.
    """
    import dataclasses

    try:
        known = {f.name for f in dataclasses.fields(options_cls)}
    except TypeError:
        known = set()

    if known:
        unknown = set(values) - known
        if unknown:
            logger.warning(
                "claude_agent_sdk does not support option(s) %s — ignoring. "
                "Check the installed SDK version.",
                sorted(unknown),
            )
        values = {k: v for k, v in values.items() if k in known}

    return options_cls(**values)


# ── Translation helpers ───────────────────────────────────────────


def _thinking_option(spec: ThinkingSpec) -> dict[str, Any] | None:
    """Map a neutral thinking spec onto the SDK's ``thinking`` config.

    ``default`` returns None so nothing is sent and the harness applies its own
    default. Pinning a value we did not choose would let a future default change
    silently rewrite our behaviour, and on a subscription that shows up as
    reduced quota rather than a bill, which is harder to notice.
    """
    if spec.mode == "default":
        return None
    if spec.mode == "adaptive":
        return {"type": "adaptive"}
    if spec.mode == "off":
        return {"type": "disabled"}
    if spec.mode == "budget":
        return {"type": "enabled", "budget_tokens": spec.budget_tokens}
    return None


def _to_sdk_tool(local: LocalTool) -> Any:
    """Wrap a :class:`LocalTool` as an in-process MCP tool.

    The handler is adapted rather than called directly: MCP wants
    ``{"content": [...]}`` and our tools return plain Python. An exception inside
    a tool is returned to the agent as text, not raised — a hosted agent that
    learns one lookup failed can carry on and say so, whereas an exception kills
    the whole run and loses the reasoning already done.
    """
    from claude_agent_sdk import SdkMcpTool

    async def _handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            value = await local.handler(**(args or {}))
        except Exception as exc:  # surfaced to the agent as text
            logger.warning("Hosted tool %s failed: %s", local.name, exc, exc_info=True)
            return {
                "content": [{"type": "text", "text": f"ERROR from {local.name}: {exc}"}],
                "isError": True,
            }
        return {"content": [{"type": "text", "text": _as_text(value)}]}

    return SdkMcpTool(
        name=local.name,
        description=local.description,
        input_schema=local.input_schema,
        handler=_handler,
    )


def _as_text(value: Any) -> str:
    """Render a tool result as text without losing structure."""
    import json

    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _quota_from_event(info: Any) -> QuotaState:
    """Normalise the SDK's ``RateLimitInfo`` onto :class:`QuotaState`.

    ``overage`` matters: a plan with pay-as-you-go still spends real money, so an
    exhausted primary window whose overage is *allowed* is reported as a WARNING,
    not OK. The caller then moves on rather than quietly billing the card.
    """
    status_text = str(getattr(info, "status", "") or "").lower()
    if status_text == "rejected":
        overage = str(getattr(info, "overage_status", "") or "").lower()
        status = QuotaStatus.WARNING if overage == "allowed" else QuotaStatus.EXHAUSTED
    elif status_text == "allowed_warning":
        status = QuotaStatus.WARNING
    else:
        status = QuotaStatus.OK

    return QuotaState(
        status=status,
        utilization=getattr(info, "utilization", None),
        resets_at=getattr(info, "resets_at", None),
        window=getattr(info, "rate_limit_type", None),
        detail=getattr(info, "overage_disabled_reason", None),
    )


def _quota_from_error(result: Any) -> QuotaState | None:
    """Infer quota exhaustion from a failed result when no event arrived.

    A run can fail on a 429 without the CLI having emitted a rate-limit
    transition first. Treating that as exhausted is the safe reading: the cost of
    being wrong is one unnecessary fallback, whereas missing it means a dropped
    trading cycle.
    """
    if getattr(result, "api_error_status", None) == 429:
        return QuotaState(QuotaStatus.EXHAUSTED, detail="HTTP 429 from the API")

    parts = list(getattr(result, "errors", None) or [])
    parts.append(getattr(result, "result", "") or "")
    haystack = " ".join(str(x) for x in parts).lower()
    for needle in ("rate limit", "usage limit", "quota", "too many requests"):
        if needle in haystack:
            return QuotaState(QuotaStatus.EXHAUSTED, detail=f"reported {needle!r}")
    return None


# ── The backend ───────────────────────────────────────────────────


@register_backend
class ClaudeCodeBackend(CliAgentBackend):
    """Runs a hosted agent through the Claude Code CLI on a Claude subscription."""

    driver = "claude_code"
    display_name = "Claude Code"

    @classmethod
    def is_available(cls) -> tuple[bool, str]:
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError:
            return False, (
                "The 'claude-agent-sdk' package is not installed. Install the "
                "extra with: uv sync --extra claude-code"
            )

        cli = resolve_claude_cli()
        if cli is None:
            return False, (
                "The 'claude' CLI was not found on PATH or in any known install "
                "location. Install it with 'npm install -g "
                "@anthropic-ai/claude-code' (requires Node 18+), then "
                "authenticate with 'claude' (interactive login) or set "
                "CLAUDE_CODE_OAUTH_TOKEN from 'claude setup-token'."
            )
        return True, f"Claude Code backend available ({cli})."

    # ── Options ───────────────────────────────────────────────────

    def _options(self, request: AgentRequest, billing: BillingMode) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server

        builtins = [_BUILTIN_MAP[c] for c in request.builtin_tools if c in _BUILTIN_MAP]
        unmapped = [c for c in request.builtin_tools if c not in _BUILTIN_MAP]
        if unmapped:
            logger.debug("Claude Code has no built-in for capability %s — skipping.", unmapped)

        values: dict[str, Any] = {
            "system_prompt": request.system_prompt,
            "tools": builtins,
            "disallowed_tools": list(_ALWAYS_BLOCKED),
            # Don't inherit the host's CLAUDE.md, hooks, or project MCP servers:
            # a run must behave the same on every machine.
            "setting_sources": [],
        }

        if request.tools:
            server_name = str(request.metadata.get("server_name") or "evotrader")
            values["mcp_servers"] = {
                server_name: create_sdk_mcp_server(
                    name=server_name,
                    tools=[_to_sdk_tool(t) for t in request.tools],
                )
            }
            values["allowed_tools"] = [
                f"mcp__{server_name}__{t.name}" for t in request.tools
            ] + builtins

        if request.model:
            values["model"] = request.model
        if request.max_turns:
            values["max_turns"] = request.max_turns
        if request.effort:
            values["effort"] = request.effort
        if request.workspace:
            values["cwd"] = str(request.workspace)
        if request.resume_token:
            values["resume"] = request.resume_token
        if permission_mode := request.metadata.get("permission_mode"):
            values["permission_mode"] = permission_mode

        # Pin billing by withholding the credential we do not want used. See
        # _billing_env: this is the enforcement, not a hint.
        env = _billing_env(billing)
        if env:
            values["env"] = env

        thinking = _thinking_option(request.thinking)
        if thinking is not None:
            values["thinking"] = thinking

        if request.output_schema:
            values["output_format"] = {
                "type": "json_schema",
                "schema": request.output_schema,
            }

        return _build_options_safely(ClaudeAgentOptions, values)

    # ── Run ───────────────────────────────────────────────────────

    async def run(self, request: AgentRequest) -> AgentResult:
        """Run *request*, honouring its billing mode.

        ``AUTO`` makes at most two attempts: subscription first, then the API if
        — and only if — the first failed specifically on authentication. A quota
        wall is NOT retried here, because the caller's fallback policy decides
        whether spending money is the right answer to running out of plan; this
        layer must not make that choice silently.
        """
        attempts: list[BillingMode] = (
            [BillingMode.SUBSCRIPTION, BillingMode.API]
            if request.billing is BillingMode.AUTO
            else [request.billing]
        )

        result = AgentResult(ok=False, error="no attempt was made")
        for index, billing in enumerate(attempts):
            result = await self._run_once(request, billing)
            if result.ok or not result.auth_failed:
                return result
            if index + 1 < len(attempts):
                logger.warning(
                    "Claude Code %s auth failed (%s) — falling back to %s billing for this run.",
                    billing.value,
                    result.error,
                    attempts[index + 1].value,
                )
        return result

    async def _run_once(self, request: AgentRequest, billing: BillingMode) -> AgentResult:
        try:
            from claude_agent_sdk import (
                AssistantMessage,
                RateLimitEvent,
                ResultMessage,
                TextBlock,
                ThinkingBlock,
                ToolUseBlock,
                query,
            )
        except ImportError as exc:
            return AgentResult(ok=False, error=f"claude-agent-sdk unavailable: {exc}")

        if billing is BillingMode.SUBSCRIPTION and subscription_credential_source() is None:
            logger.info(
                "No subscription credential found in %s or ~/.claude/.credentials.json. "
                "Attempting the run anyway — an interactive login may hold one where "
                "this process cannot see it.",
                _OAUTH_TOKEN_ENV,
            )

        options = self._options(request, billing)
        texts: list[str] = []
        quota = QuotaState()
        result_msg: Any = None

        async def emit(event: AgentEvent) -> None:
            if request.on_event is not None:
                try:
                    await request.on_event(event)
                except Exception:  # telemetry must not kill a run
                    logger.debug("on_event handler raised", exc_info=True)

        try:
            async for message in query(prompt=request.prompt, options=options):
                if isinstance(message, RateLimitEvent):
                    quota = _quota_from_event(message.rate_limit_info)
                    logger.info(
                        "Claude Code quota: status=%s utilization=%s window=%s",
                        quota.status.value,
                        quota.utilization,
                        quota.window,
                    )
                    await emit(
                        AgentEvent(
                            kind="quota",
                            name=quota.status.value,
                            content=f"quota {quota.status.value}",
                            payload={
                                "utilization": quota.utilization,
                                "resets_at": quota.resets_at,
                                "window": quota.window,
                            },
                        )
                    )
                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            texts.append(block.text)
                            await emit(AgentEvent(kind="thought", content=block.text))
                        elif isinstance(block, ThinkingBlock):
                            # Recorded for the console timeline, never re-sent.
                            await emit(
                                AgentEvent(
                                    kind="thought",
                                    name="thinking",
                                    content=getattr(block, "thinking", "") or "",
                                )
                            )
                        elif isinstance(block, ToolUseBlock):
                            await emit(
                                AgentEvent(
                                    kind="tool_call",
                                    name=block.name,
                                    payload={"input": block.input},
                                )
                            )
                elif isinstance(message, ResultMessage):
                    result_msg = message
        except Exception as exc:  # operational failure, not a bug
            logger.error("Claude Code run failed: %s", exc, exc_info=True)
            await emit(AgentEvent(kind="error", content=str(exc)))
            return AgentResult(
                ok=False,
                text="\n".join(texts),
                quota=quota,
                error=str(exc),
                billing_used=billing,
                auth_failed=_looks_like_auth_failure(exc, *texts),
            )

        if result_msg is None:
            return AgentResult(
                ok=False,
                text="\n".join(texts),
                quota=quota,
                error="the harness produced no result message",
                billing_used=billing,
            )

        if result_msg.is_error and (inferred := _quota_from_error(result_msg)):
            quota = inferred

        auth_failed = bool(result_msg.is_error) and _looks_like_auth_failure(
            result_msg.result, *(result_msg.errors or ())
        )

        return AgentResult(
            ok=not result_msg.is_error,
            text=(result_msg.result or "\n".join(texts)),
            structured=getattr(result_msg, "structured_output", None),
            session_token=result_msg.session_id,
            turns=result_msg.num_turns,
            cost_usd=result_msg.total_cost_usd,
            usage=result_msg.usage or {},
            quota=quota,
            error="; ".join(result_msg.errors) if result_msg.errors else None,
            stop_reason=getattr(result_msg, "terminal_reason", None) or result_msg.stop_reason,
            billing_used=billing,
            auth_failed=auth_failed,
        )
