"""Tests for thought-log event summarising.

The contract this protects: agent reasoning is never summarised, only bulk tool
payloads are, and the agent is always told when something was omitted.
"""

from __future__ import annotations

import json

from evotrader.evolution.event_summariser import (
    DEFAULT_MAX_META_CHARS,
    summarise_events,
    summarise_meta,
)


def _event(event_id: int, event_type: str, agent: str, content: str, meta) -> dict:
    return {
        "id": event_id,
        "event_type": event_type,
        "agent_name": agent,
        "content": content,
        "meta": json.dumps(meta) if not isinstance(meta, str) and meta is not None else meta,
    }


def _big_payload(chars: int) -> dict:
    return {"response": {"results": [{"symbol": "AAA", "pad": "x" * chars}]}}


# ── The core guarantee: reasoning survives ────────────────────────


def test_reasoning_content_is_never_touched():
    long_reasoning = "The composite signal is negative because " + "detail. " * 5000
    events = [_event(1, "thought", "strategy", long_reasoning, None)]

    out, stats = summarise_events(events)

    assert out[0]["content"] == long_reasoning
    assert stats["payloads_omitted"] == 0


def test_small_payloads_pass_through_unchanged():
    payload = {"response": {"score": 0.4, "confidence": 0.8}}
    events = [_event(1, "tool_response", "news_sentiment", "get_news", payload)]

    out, _ = summarise_events(events)

    assert out[0]["meta"] == payload


def test_sub_agent_reasoning_transfers_survive():
    """Trade reasoning arrives as a tool_response, not a `thought` row.

    An earlier draft used a 2,000-char threshold and summarised away the
    strategy agent's actual trade decision. This is the regression guard.
    """
    decision = {"response": {"result": "## Trade Decision: NO_TRADE\n" + "reasoning. " * 250}}
    size = len(json.dumps(decision))
    assert 2_000 < size < DEFAULT_MAX_META_CHARS, "fixture should sit in the danger band"

    out, stats = summarise_events(
        [_event(1, "tool_response", "orchestrator", "strategy", decision)]
    )

    assert out[0]["meta"] == decision
    assert stats["payloads_omitted"] == 0


# ── Oversized payloads ────────────────────────────────────────────


def test_oversized_payload_is_summarised_not_silently_cut():
    events = [
        _event(42, "tool_response", "news_sentiment", "get_earnings_calendar", _big_payload(60_000))
    ]

    out, stats = summarise_events(events)
    meta = out[0]["meta"]

    assert meta["_omitted"] is True
    assert meta["_size_chars"] > DEFAULT_MAX_META_CHARS
    assert "_preview" in meta
    assert "_shape" in meta
    # The agent must be told how to get the rest — silent truncation is the
    # failure mode that lets a model reason from a fragment as if complete.
    assert "get_tool_response(event_id=42)" in meta["_note"]
    assert stats["payloads_omitted"] == 1


def test_summary_is_much_smaller_than_the_original():
    events = [_event(1, "tool_response", "news_sentiment", "NEWS_SENTIMENT", _big_payload(200_000))]

    _, stats = summarise_events(events)

    assert stats["meta_chars_after"] < stats["meta_chars_before"] / 20


def test_tool_call_args_are_kept_when_small():
    """Arguments are how the evolution agent understands what a step did."""
    meta = {"args": {"tickers": "QQQ", "limit": 10}, "response": {"pad": "x" * 60_000}}
    out, _ = summarise_events(
        [_event(1, "tool_response", "news_sentiment", "NEWS_SENTIMENT", meta)]
    )

    assert out[0]["meta"]["args"] == {"tickers": "QQQ", "limit": 10}


def test_error_flag_is_kept():
    meta = {"is_error": True, "response": {"pad": "x" * 60_000}}
    out, _ = summarise_events([_event(1, "tool_response", "evolution", "promote", meta)])

    assert out[0]["meta"]["is_error"] is True


def test_shape_describes_a_list_payload():
    # Mirrors the real case: a market-wide earnings calendar of ~1,800 entries.
    meta = {
        "response": [
            {"symbol": f"S{i}", "eps": {"estimate": "0.51", "actual": None}} for i in range(1814)
        ]
    }
    assert len(json.dumps(meta)) > DEFAULT_MAX_META_CHARS
    out, _ = summarise_events([_event(1, "tool_response", "n", "get_earnings_calendar", meta)])

    assert out[0]["meta"]["_shape"] == "list of 1814 items"


# ── Mixed batch: the realistic case ───────────────────────────────


def test_mixed_batch_keeps_signal_and_drops_bulk():
    events = [
        _event(1, "thought", "strategy", "Decided NO_TRADE because of event risk.", None),
        _event(
            2, "tool_response", "orchestrator", "strategy", {"response": {"result": "decision"}}
        ),
        _event(3, "tool_response", "news_sentiment", "TREASURY_YIELD", _big_payload(150_000)),
        _event(4, "tool_response", "news_sentiment", "NEWS_SENTIMENT", _big_payload(190_000)),
        _event(5, "tool_response", "strategy", "query_past_trades", {"response": {"trades": []}}),
    ]

    out, stats = summarise_events(events)

    assert stats["payloads_omitted"] == 2
    assert out[0]["content"] == "Decided NO_TRADE because of event risk."
    assert out[1]["meta"] == {"response": {"result": "decision"}}
    assert out[4]["meta"] == {"response": {"trades": []}}
    assert out[2]["meta"]["_omitted"] is True
    assert out[3]["meta"]["_omitted"] is True
    assert "get_tool_response" in stats["note"]


def test_stats_absent_when_nothing_was_omitted():
    _, stats = summarise_events([_event(1, "thought", "strategy", "short", None)])

    assert stats["payloads_omitted"] == 0
    assert "note" not in stats


# ── Robustness ────────────────────────────────────────────────────


def test_none_meta_is_preserved():
    out, _ = summarise_events([_event(1, "thought", "strategy", "hi", None)])
    assert out[0]["meta"] is None


def test_unparseable_meta_string_is_handled():
    events = [
        {
            "id": 1,
            "event_type": "tool_response",
            "agent_name": "a",
            "content": "t",
            "meta": "not json at all " * 5000,
        }
    ]
    out, stats = summarise_events(events)

    assert stats["payloads_omitted"] == 1
    assert out[0]["meta"]["_omitted"] is True


def test_input_events_are_not_mutated():
    original = _event(1, "tool_response", "n", "NEWS_SENTIMENT", _big_payload(60_000))
    before = original["meta"]

    summarise_events([original])

    assert original["meta"] is before, "caller's rows must not be modified in place"


def test_summarise_meta_without_event_id_still_explains_itself():
    meta = summarise_meta(json.dumps(_big_payload(60_000)), event_id=None)
    assert meta["_omitted"] is True
    assert "retained in the database" in meta["_note"]


def test_threshold_is_configurable():
    meta = {"response": {"pad": "x" * 3000}}
    events = [_event(1, "tool_response", "n", "t", meta)]

    kept, _ = summarise_events(events, max_chars=100_000)
    assert kept[0]["meta"] == meta

    trimmed, _ = summarise_events(events, max_chars=500)
    assert trimmed[0]["meta"]["_omitted"] is True
