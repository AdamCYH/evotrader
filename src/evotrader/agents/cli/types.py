"""Vendor-neutral vocabulary for talking to a CLI-hosted agent.

A "CLI backend" is any coding-agent harness we drive as a subprocess instead of
calling a completion endpoint ourselves: Claude Code today, plausibly a Gemini
CLI or Codex CLI later. The appeal is billing — a subscription seat instead of
per-token API spend — but the shape is different enough from a chat completion
that it needs its own contract:

* the harness owns the agent loop, so we hand it a task and tools, not messages
* it runs many turns before answering, so results arrive with turn counts and
  usage rather than one response object
* it is metered by *plan quota*, not by invoice, so "you are nearly out" is a
  first-class signal we must be able to act on mid-day

Nothing in this module imports a vendor SDK. That is the point: everything above
the backend layer — the ADK agent wrapper, the evolution service, config — is
written against these types, so adding a second harness means adding one adapter
and one settings entry, not touching callers.

The neutral types deliberately do NOT expose every vendor knob. A field earns a
place here only if more than one plausible harness has the concept, or if
trading behaviour depends on it. Vendor-specific extras belong in that adapter's
own config, not in this vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

# ── Tools ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LocalTool:
    """A Python function we want the hosted agent to be able to call.

    Every harness has some mechanism for this (Claude Code uses an in-process
    MCP server; others use function declarations or a plugin manifest), so the
    neutral form is just "name, docs, JSON schema, coroutine".

    ``read_only`` is advisory metadata for harnesses that gate side-effecting
    tools behind a permission prompt. It is not a security boundary — the
    handler itself is responsible for refusing work it should not do.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    read_only: bool = True

    @classmethod
    def from_function(cls, fn: Any, *, read_only: bool = True) -> LocalTool:
        """Build a tool from a plain Python function.

        Name, description and parameter schema come from the signature and
        Google-style docstring — the same derivation ADK's ``FunctionTool``
        performs — so an agent moved between the API runtime and a CLI runtime is
        offered an identical tool surface.

        Sync functions are wrapped so every handler is awaitable, which keeps the
        backend adapters from having to care which kind they were given.
        """
        import inspect

        from evotrader.agents.cli.schema import _split_docstring, build_input_schema

        description, _ = _split_docstring(fn)
        is_async = inspect.iscoroutinefunction(fn)

        async def handler(**kwargs: Any) -> Any:
            result = fn(**kwargs)
            if is_async:
                result = await result
            return result

        return cls(
            name=fn.__name__,
            description=description,
            input_schema=build_input_schema(fn),
            handler=handler,
            read_only=read_only,
        )


# Built-in capabilities named in neutral terms. A backend maps these onto
# whatever its harness calls them (Claude Code: Read/Grep/Glob) and silently
# drops the ones it cannot offer — a missing built-in degrades the agent's
# convenience, never its correctness, because every tool that *matters* is
# supplied explicitly as a LocalTool.
BuiltinCapability = Literal["read_files", "search_files", "list_files"]


# ── Thinking ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ThinkingSpec:
    """How much private reasoning to ask for.

    ``mode="default"`` means *send nothing* and let the harness apply its own
    default. That is distinct from ``"adaptive"``, which explicitly requests
    model-chosen depth: they may coincide today, but pinning a value we did not
    choose makes a future default change silently rewrite our behaviour.
    """

    mode: Literal["default", "adaptive", "off", "budget"] = "default"
    budget_tokens: int | None = None

    @classmethod
    def parse(cls, value: Any) -> ThinkingSpec:
        """Build a spec from a settings value.

        Accepts ``"default"``, ``"adaptive"``, ``"off"``/``"none"``/``False``,
        or an integer token budget (as int or digit string). Raises on anything
        else rather than guessing — a typo in a cost-relevant setting should
        stop startup, not quietly pick a mode.
        """
        if value is None or value is True:
            return cls("default")
        if value is False:
            return cls("off")
        if isinstance(value, int):
            if value <= 0:
                return cls("off")
            return cls("budget", budget_tokens=value)
        text = str(value).strip().lower()
        if text in ("", "default"):
            return cls("default")
        if text == "adaptive":
            return cls("adaptive")
        if text in ("off", "none", "disabled", "false"):
            return cls("off")
        if text.isdigit():
            budget = int(text)
            return cls("budget", budget_tokens=budget) if budget > 0 else cls("off")
        raise ValueError(
            f"thinking must be 'default', 'adaptive', 'off' or a positive token "
            f"budget; got {value!r}"
        )


# Effort is a coarse dial that several harnesses now expose. None = unset.
EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]


# ── Billing ───────────────────────────────────────────────────────


class BillingMode(StrEnum):
    """Which account a hosted run should be charged to.

    The distinction is not cosmetic. Most harnesses accept *both* a
    subscription login and an API key, and silently prefer the key when one is
    present in the environment — so a project that loads an API key from
    ``.env`` for its other agents will bill every "subscription" run to the API
    without any error, log line, or visible difference in behaviour. That
    happened here, unnoticed, for a week.

    So this is enforced rather than detected: for ``SUBSCRIPTION`` the adapter
    removes the API credential from the subprocess environment, which makes API
    billing *impossible* for that run. A successful run is then proof of how it
    was billed, rather than an assumption about credential precedence.

    ``AUTO`` prefers the subscription and falls back to the API on an auth
    failure — logging which one was used, never silently.
    """

    SUBSCRIPTION = "subscription"
    API = "api"
    AUTO = "auto"


# ── Quota ─────────────────────────────────────────────────────────


class QuotaStatus(StrEnum):
    """Plan-quota state, normalised across vendors.

    Three states, because that is what a caller can actually act on:

    ``OK``        proceed.
    ``WARNING``   this call succeeded, but the next one might not. Finish the
                  current task here and route the *next* one elsewhere — acting
                  on the warning is what keeps a quota wall from costing a
                  trading cycle.
    ``EXHAUSTED`` this call did not land. Retry elsewhere now.
    """

    OK = "ok"
    WARNING = "warning"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class QuotaState:
    """What the harness told us about remaining plan capacity."""

    status: QuotaStatus = QuotaStatus.OK
    utilization: float | None = None
    """Fraction of the window consumed, 0.0-1.0, when the vendor reports it."""

    resets_at: float | None = None
    """Unix seconds when the window rolls over, if known."""

    window: str | None = None
    """Vendor's label for the window (e.g. "five_hour"). Opaque; for logs."""

    detail: str | None = None

    @property
    def usable(self) -> bool:
        """Whether another call to this backend is worth attempting."""
        return self.status is not QuotaStatus.EXHAUSTED


# ── Requests and results ──────────────────────────────────────────


@dataclass
class AgentRequest:
    """One unit of work for a hosted agent.

    ``system_prompt`` is the agent's standing instructions and ``prompt`` is the
    task; keeping them separate lets a harness cache the stable half.
    """

    system_prompt: str
    prompt: str
    tools: Sequence[LocalTool] = ()
    builtin_tools: Sequence[BuiltinCapability] = ()
    output_schema: dict[str, Any] | None = None
    """JSON schema for a structured answer. Backends that cannot enforce a
    schema must say so via ``AgentResult.structured is None`` and leave the
    text intact, so the caller can fall back to parsing it."""

    model: str | None = None
    thinking: ThinkingSpec = field(default_factory=ThinkingSpec)
    effort: EffortLevel | None = None
    max_turns: int | None = None
    workspace: Path | None = None
    resume_token: str | None = None
    """Opaque continuation handle from a previous AgentResult, if resuming."""

    billing: BillingMode = BillingMode.AUTO
    """Which account to charge. See :class:`BillingMode` — this is enforced by
    withholding credentials, not by trusting precedence rules."""

    metadata: Mapping[str, Any] = field(default_factory=dict)
    """Free-form labels for telemetry. Never interpreted as instructions."""

    on_event: Callable[[AgentEvent], Awaitable[None]] | None = None
    """Called as the run progresses, for live streaming to the web console."""


@dataclass(frozen=True)
class AgentEvent:
    """A progress notification during a run.

    ``kind`` is deliberately a small neutral set rather than a vendor enum, so
    the web-console timeline renders identically whichever harness produced it.
    """

    kind: Literal["thought", "tool_call", "tool_result", "quota", "error"]
    name: str | None = None
    content: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    """What came back.

    ``ok`` is about the *run*, not the answer: a run that completed but proposed
    nothing is still ok. A run that hit the quota wall, crashed, or exceeded its
    turn cap is not, and ``quota``/``error`` say which.
    """

    ok: bool
    text: str = ""
    structured: Any = None
    session_token: str | None = None
    turns: int = 0
    cost_usd: float | None = None
    """Vendor-reported equivalent cost. On a subscription this is what the run
    *would* have cost on the API — useful for telemetry, not an invoice."""

    usage: Mapping[str, Any] = field(default_factory=dict)
    quota: QuotaState = field(default_factory=QuotaState)
    error: str | None = None
    stop_reason: str | None = None

    billing_used: BillingMode | None = None
    """How the run was actually billed, as established by which credential the
    subprocess was given. Recorded so "are we really on the subscription?" is a
    question the system can answer, not one that needs a database dig."""

    auth_failed: bool = False
    """True when the run failed specifically because the chosen credential was
    absent or rejected. Distinct from a quota wall: the fix is to authenticate,
    not to wait or fall back to another plan."""

    @property
    def quota_exhausted(self) -> bool:
        return self.quota.status is QuotaStatus.EXHAUSTED
