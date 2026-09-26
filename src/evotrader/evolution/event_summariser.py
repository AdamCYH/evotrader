"""Compact thought-log events before they re-enter an agent's context.

``query_cycle_thoughts`` exists so the evolution agent can read *why* a trading
cycle did what it did. But every row it returns carries the raw ``meta`` column,
which for a ``tool_response`` event holds the entire tool payload. Since some
research tools return unbounded data (a market-wide earnings calendar, a
full-history interest-rate series), the reasoning the agent asked for arrives
buried in data it never wanted.

Measured on the worst real case in this project's own database: 15 events,
576,235 characters of ``meta``, and **229 characters** of reasoning — 99.96% of
a 163k-token response was payload the agent had no use for.

So this module keeps ``content`` (the reasoning) intact and always in full, and
replaces oversized ``meta`` with a descriptor that says what the payload was,
how big it was, and how to fetch it. Nothing is deleted from the database:
:func:`evotrader.evolution.tools.get_tool_response` retrieves any payload in
full by event id.

The effect is *more* usable history per call, not less — 50 events of reasoning
fit in the space 15 events of raw payload used to occupy.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Payloads at or below this size pass through untouched.
#
# 40,000 chars (~10k tokens) was chosen from the size distribution of this
# project's own thought log, not by instinct. Above that line sit 430 events
# holding 70.8M of the 96.0M total payload chars (74%), and every one is a bulk
# data dump — market-wide earnings calendars, full-history rate series, news
# archives. Below it sit the payloads that carry signal the evolution agent
# needs, including the sub-agent reasoning transfers that arrive as tool
# results rather than as `thought` rows: `strategy` peaks at 7.1k chars,
# `risk_manager` at 9.1k, `query_past_trades` at 19.6k, `read_source_file` at
# 38.3k. A lower threshold would summarise away the trade reasoning that
# evolution exists to study — which an earlier draft of this module did.
DEFAULT_MAX_META_CHARS = 40_000

# How much of an oversized payload to show inline, so the agent can tell what it
# is (and often answer its question) without a second call.
DEFAULT_PREVIEW_CHARS = 1000


def _shorten(text: str, limit: int) -> str:
    """Cut *text* to *limit* chars on a whitespace boundary where possible."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.7:
        cut = cut[:space]
    return cut


def _describe(value: Any) -> str:
    """One-line description of a payload's shape, e.g. 'list of 1814 items'."""
    if isinstance(value, list):
        return f"list of {len(value)} items"
    if isinstance(value, dict):
        keys = list(value.keys())
        shown = ", ".join(keys[:8])
        if len(keys) > 8:
            shown += f", … (+{len(keys) - 8} more)"
        return f"object with keys: {shown}"
    return type(value).__name__


def summarise_meta(
    meta: Any,
    *,
    event_id: int | None = None,
    max_chars: int = DEFAULT_MAX_META_CHARS,
    preview_chars: int = DEFAULT_PREVIEW_CHARS,
) -> Any:
    """Return *meta* unchanged when small, or a descriptor when oversized.

    The descriptor deliberately states that content was omitted and how to get
    it. Silent truncation is the dangerous case: an agent that doesn't know it
    is looking at a fragment may reason from it as though it were complete.

    Args:
        meta: The raw ``meta`` column value (dict, JSON string, or None).
        event_id: Row id, so the descriptor can name how to fetch the payload.
        max_chars: Payloads at or below this size pass through untouched.
        preview_chars: How much of an oversized payload to show inline.

    Returns:
        Either the original ``meta``, or a dict describing what was omitted.
    """
    if meta is None:
        return None

    parsed = meta
    if isinstance(meta, str):
        try:
            parsed = json.loads(meta)
        except (json.JSONDecodeError, ValueError):
            parsed = meta

    try:
        serialised = parsed if isinstance(parsed, str) else json.dumps(parsed, default=str)
    except (TypeError, ValueError):
        serialised = str(parsed)

    if len(serialised) <= max_chars:
        return parsed

    descriptor: dict[str, Any] = {
        "_omitted": True,
        "_size_chars": len(serialised),
        "_note": (
            "Payload omitted to keep this response readable. "
            + (
                f"Call get_tool_response(event_id={event_id}) for the full payload."
                if event_id is not None
                else "Full payload is retained in the database."
            )
        ),
    }

    # Keep the small, high-signal parts of the envelope: the arguments a tool
    # was called with are what the agent usually needs to understand a step.
    if isinstance(parsed, dict):
        for key in ("args", "tool_call_id", "is_error", "tool_info"):
            if key in parsed:
                value = parsed[key]
                try:
                    encoded = json.dumps(value, default=str)
                except (TypeError, ValueError):
                    encoded = str(value)
                if len(encoded) <= max_chars:
                    descriptor[key] = value

        payload = parsed.get("response")
        if payload is not None:
            descriptor["_shape"] = _describe(payload)
            try:
                payload_text = (
                    payload if isinstance(payload, str) else json.dumps(payload, default=str)
                )
            except (TypeError, ValueError):
                payload_text = str(payload)
            descriptor["_preview"] = _shorten(payload_text, preview_chars)
            return descriptor

    descriptor["_shape"] = _describe(parsed)
    descriptor["_preview"] = _shorten(serialised, preview_chars)
    return descriptor


def summarise_events(
    events: list[dict[str, Any]],
    *,
    max_chars: int = DEFAULT_MAX_META_CHARS,
    preview_chars: int = DEFAULT_PREVIEW_CHARS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Compact the ``meta`` of each event, leaving ``content`` untouched.

    Returns ``(events, stats)``. ``stats`` reports what was compacted so the
    caller can tell the agent plainly rather than leaving it to guess.
    """
    compacted: list[dict[str, Any]] = []
    omitted = 0
    chars_before = 0
    chars_after = 0

    for event in events:
        row = dict(event)
        raw = row.get("meta")
        if raw is not None:
            before = len(raw if isinstance(raw, str) else str(raw))
            summary = summarise_meta(
                raw,
                event_id=row.get("id"),
                max_chars=max_chars,
                preview_chars=preview_chars,
            )
            if isinstance(summary, dict) and summary.get("_omitted"):
                omitted += 1
            try:
                after = len(json.dumps(summary, default=str))
            except (TypeError, ValueError):
                after = len(str(summary))
            chars_before += before
            chars_after += after
            row["meta"] = summary
        compacted.append(row)

    stats = {
        "events": len(events),
        "payloads_omitted": omitted,
        "meta_chars_before": chars_before,
        "meta_chars_after": chars_after,
    }
    if omitted:
        stats["note"] = (
            f"{omitted} oversized tool payload(s) were summarised. Agent "
            "reasoning (the `content` field) is always complete. Use "
            "get_tool_response(event_id=…) to retrieve any full payload."
        )
        logger.debug(
            "query_cycle_thoughts: summarised %d payload(s), %d → %d meta chars",
            omitted,
            chars_before,
            chars_after,
        )
    return compacted, stats
