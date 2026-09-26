"""Anthropic prompt-caching for LiteLLM-backed ADK agents.

Anthropic prompt caching is a **prefix match** over the rendered request. The
render order is::

    tools  →  system  →  messages

A ``cache_control`` breakpoint caches everything from the start of the request
up to and including the block it sits on. The next request that shares that
exact prefix reads it back at ~10% of the input price instead of 100%.

ADK is stateless towards the model: it resends the *entire* conversation on
every agentic turn. So a breakpoint on the system message alone (which is all
this project used to do) caches ``tools`` + ``system`` and nothing else — every
tool call and tool result in the growing history is re-processed at full price
on every subsequent turn. On a 21-turn evolution run that meant ~2.6M tokens of
input billed at full rate for what was, in effect, the same conversation read
over and over.

This module therefore places breakpoints in two places:

1. **System** — at the end of the invariant part of the system prompt. Because
   ``tools`` render before ``system``, this single breakpoint covers the tool
   schemas too. The volatile tail (temporal context) is split off *after* the
   breakpoint so a changing clock doesn't invalidate the prefix.
2. **Messages** — at the tail of ``messages``, so turn *N* writes a cache that
   turn *N+1* reads. Two breakpoints are used by default: one on the newest
   turn and one on the previous turn. The second is not redundant — Anthropic
   only looks back a limited number of blocks from a breakpoint when matching,
   and a turn with many parallel tool calls can exceed that window.

Where ``cache_control`` goes depends on the message shape, because LiteLLM
reads it from different places (verified against
``litellm/litellm_core_utils/prompt_templates/factory.py``):

===========================  ===================================
Message                      ``cache_control`` location
===========================  ===================================
``role="tool"``/``function`` message level
``role="user"``, str content message level
``role="user"``, list        last content block
===========================  ===================================

Assistant messages are never used as breakpoints: LiteLLM routes their content
through the ``tool_use`` conversion path, and in an agentic loop the final
message of a request is always a user or tool message anyway.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Marks the start of the volatile tail of a system prompt. Everything before it
# is stable across a session and therefore cacheable; everything after it
# changes every call and must sit *after* the breakpoint.
_SYSTEM_VOLATILE_DELIMITER = "## Current Temporal Context"

# Message roles that can legally carry a cache breakpoint. Assistant messages
# are excluded — see the module docstring.
_CACHEABLE_ROLES = frozenset({"user", "tool", "function"})

# Content block types that accept cache_control per the Anthropic API.
_CACHEABLE_BLOCK_TYPES = frozenset({"text", "image", "document", "file"})

# Anthropic's prompt-caching beta header. Caching is GA and the header is a
# no-op on current models, but it is what this project has always sent and
# removing it is a separate change from fixing breakpoint placement.
_CACHE_BETA = "prompt-caching-2024-07-31"

# Required to request a 1-hour cache TTL instead of the 5-minute default.
_EXTENDED_TTL_BETA = "extended-cache-ttl-2025-04-11"


def is_anthropic_model(model: str | None) -> bool:
    """Return True when *model* routes to Anthropic through LiteLLM."""
    return bool(model) and "anthropic" in model.lower()


def _cache_control(ttl: str | None) -> dict[str, str]:
    """Build a ``cache_control`` value, including *ttl* when it isn't the default."""
    control = {"type": "ephemeral"}
    if ttl and ttl != "5m":
        control["ttl"] = ttl
    return control


def _strip_cache_control(messages: list[Any]) -> None:
    """Remove every ``cache_control`` key from *messages*, in place.

    ADK may hand us the same message dicts on consecutive turns. Without this,
    breakpoints from earlier turns accumulate and blow the 4-breakpoint-per-
    request limit. Nothing else in the pipeline sets ``cache_control``, so
    stripping unconditionally is safe and makes this module idempotent.
    """
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        msg.pop("cache_control", None)
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)


def _mark_message(msg: dict, ttl: str | None) -> bool:
    """Place a breakpoint on *msg*. Returns True when one was placed."""
    role = msg.get("role")
    if role not in _CACHEABLE_ROLES:
        return False

    content = msg.get("content")

    if isinstance(content, list):
        # Block-level: the last block LiteLLM will forward with cache_control.
        for block in reversed(content):
            if isinstance(block, dict) and block.get("type") in _CACHEABLE_BLOCK_TYPES:
                block["cache_control"] = _cache_control(ttl)
                return True
        return False

    if isinstance(content, str) and content:
        # Message-level: LiteLLM reads cache_control off the message dict for
        # string-content user messages and for tool/function results.
        msg["cache_control"] = _cache_control(ttl)
        return True

    if role in ("tool", "function"):
        # Tool results with non-string content still take a message-level key.
        msg["cache_control"] = _cache_control(ttl)
        return True

    return False


def _tail_breakpoint_indices(messages: list[Any], limit: int) -> list[int]:
    """Pick up to *limit* message indices at the tail, one per conversation turn.

    Walks backwards, taking the last message of each contiguous run of
    cacheable messages and skipping the assistant turn in between. This spaces
    the breakpoints one turn apart rather than clustering them inside a single
    batch of parallel tool results.
    """
    indices: list[int] = []
    i = len(messages) - 1

    while i >= 0 and len(indices) < limit:
        # Skip back to the end of the next cacheable run.
        while i >= 0 and _role_of(messages, i) not in _CACHEABLE_ROLES:
            i -= 1
        if i < 0:
            break
        indices.append(i)
        # Step over this run, then over the assistant turn preceding it.
        while i >= 0 and _role_of(messages, i) in _CACHEABLE_ROLES:
            i -= 1
        while i >= 0 and _role_of(messages, i) not in _CACHEABLE_ROLES:
            i -= 1

    return indices


def _role_of(messages: list[Any], i: int) -> str | None:
    msg = messages[i]
    return msg.get("role") if isinstance(msg, dict) else None


def _apply_system_breakpoint(msg: dict, ttl: str | None) -> None:
    """Cache ``tools`` + the invariant part of the system prompt.

    When the system prompt contains the temporal-context delimiter, it is split
    so the volatile tail lands *after* the breakpoint. Otherwise the whole
    prompt is cached.
    """
    content = msg.get("content")

    if isinstance(content, str):
        if _SYSTEM_VOLATILE_DELIMITER in content:
            static_part, volatile_part = content.split(_SYSTEM_VOLATILE_DELIMITER, 1)
            msg["content"] = [
                {
                    "type": "text",
                    "text": static_part.rstrip(),
                    "cache_control": _cache_control(ttl),
                },
                {
                    "type": "text",
                    "text": _SYSTEM_VOLATILE_DELIMITER + volatile_part,
                },
            ]
        else:
            msg["content"] = [
                {
                    "type": "text",
                    "text": content,
                    "cache_control": _cache_control(ttl),
                }
            ]
        return

    if isinstance(content, list) and content:
        first_block = content[0]
        if isinstance(first_block, dict):
            first_block["cache_control"] = _cache_control(ttl)


def _is_multi_turn(messages: list[Any]) -> bool:
    """True once at least one agent turn has already happened.

    On the first call of a session ``messages`` is just ``[system, user]``. A
    breakpoint there writes a cache that nothing will ever read back — it costs
    1.25x on the delta instead of 1.0x. Waiting for the first assistant/tool
    message makes single-call agents strictly cheaper and costs a multi-turn
    agent only the caching of its opening user message.
    """
    return any(
        isinstance(m, dict) and m.get("role") in ("assistant", "tool", "function") for m in messages
    )


def _approx_chars(messages: list[Any]) -> int:
    """Cheap size estimate for the message history (~4 chars per token)."""
    try:
        return len(json.dumps(messages, default=str))
    except (TypeError, ValueError):
        return sum(len(str(m)) for m in messages)


def apply_prompt_caching(
    model: str,
    messages: list[Any] | None,
    kwargs: dict[str, Any],
    *,
    message_breakpoints: int = 2,
    min_cacheable_chars: int = 4000,
    require_multi_turn: bool = True,
    ttl: str | None = None,
) -> None:
    """Insert Anthropic cache breakpoints into *messages*, in place.

    No-op for non-Anthropic models. Also sets the beta header(s) the requested
    configuration needs on ``kwargs["extra_headers"]``.

    Args:
        model: The LiteLLM model string (e.g. ``anthropic/claude-opus-5``).
        messages: The OpenAI-shaped message list LiteLLM will transform.
        kwargs: The completion kwargs, mutated to carry the beta header.
        message_breakpoints: How many breakpoints to place in the message
            history. ``0`` restores the old system-only behaviour.
        min_cacheable_chars: Skip message breakpoints while the history is
            smaller than this, so a trivially short exchange doesn't pay a
            cache write nothing will ever read.
        require_multi_turn: Only place message breakpoints once at least one
            agent turn has happened. Protects single-call agents from paying
            the cache-write premium for nothing.
        ttl: ``"5m"`` (default) or ``"1h"``.
    """
    if not is_anthropic_model(model) or not messages:
        return

    _strip_cache_control(messages)

    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            _apply_system_breakpoint(msg, ttl)

    placed = 0
    eligible = (
        message_breakpoints > 0
        and (not require_multi_turn or _is_multi_turn(messages))
        and _approx_chars(messages) >= min_cacheable_chars
    )
    if eligible:
        for idx in _tail_breakpoint_indices(messages, message_breakpoints):
            if _mark_message(messages[idx], ttl):
                placed += 1

    headers = dict(kwargs.get("extra_headers") or {})
    betas = [_CACHE_BETA]
    if ttl and ttl != "5m":
        betas.append(_EXTENDED_TTL_BETA)
    existing = headers.get("anthropic-beta")
    if existing:
        betas.extend(b.strip() for b in existing.split(",") if b.strip() not in betas)
    headers["anthropic-beta"] = ",".join(dict.fromkeys(betas))
    kwargs["extra_headers"] = headers

    logger.debug(
        "Anthropic caching: %d message breakpoint(s) placed across %d messages (ttl=%s)",
        placed,
        len(messages),
        ttl or "5m",
    )


def log_cache_usage(model: str, response: Any) -> None:
    """Log cache hit/write counts from a completion *response* at DEBUG level.

    Tolerant of streaming wrappers and providers that report no cache fields.
    """
    if not logger.isEnabledFor(logging.DEBUG):
        return
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    read = getattr(usage, "cache_read_input_tokens", None)
    written = getattr(usage, "cache_creation_input_tokens", None)
    prompt = getattr(usage, "prompt_tokens", None)
    if read is None and written is None:
        return
    logger.debug(
        "Anthropic caching [%s]: prompt=%s cache_read=%s cache_write=%s",
        model,
        prompt,
        read,
        written,
    )
