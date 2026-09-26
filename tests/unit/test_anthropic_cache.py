"""Tests for Anthropic prompt-cache breakpoint placement.

The placement rules here are dictated by where LiteLLM reads ``cache_control``
from when it transforms OpenAI-shaped messages into Anthropic ones:

* ``role="tool"`` / ``"function"``  → message level
* ``role="user"`` with str content  → message level
* ``role="user"`` with list content → the content block

The final test asserts this end-to-end against LiteLLM's real transformation
function, so it fails loudly if LiteLLM ever changes where it looks.
"""

from __future__ import annotations

import pytest

from evotrader.agents.anthropic_cache import (
    apply_prompt_caching,
    is_anthropic_model,
)

MODEL = "anthropic/claude-opus-5"

# Padding so histories clear `min_cacheable_chars` without each test
# hand-rolling a realistic-sized conversation.
BULK = "x" * 5000


def _system(text: str = "You are a trader.") -> dict:
    return {"role": "system", "content": text}


def _breakpoint_count(messages: list[dict]) -> int:
    """Count breakpoints the way Anthropic counts them: one per marked block."""
    count = 0
    for msg in messages:
        if "cache_control" in msg:
            count += 1
        content = msg.get("content")
        if isinstance(content, list):
            count += sum(1 for b in content if isinstance(b, dict) and "cache_control" in b)
    return count


def _marked_indices(messages: list[dict]) -> list[int]:
    marked = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "system":
            continue
        content = msg.get("content")
        block_marked = isinstance(content, list) and any(
            isinstance(b, dict) and "cache_control" in b for b in content
        )
        if "cache_control" in msg or block_marked:
            marked.append(i)
    return marked


# ── Model gating ──────────────────────────────────────────────────


def test_is_anthropic_model():
    assert is_anthropic_model("anthropic/claude-opus-5")
    assert not is_anthropic_model("gemini-3.7-flash")
    assert not is_anthropic_model(None)


def test_non_anthropic_model_is_untouched():
    messages = [_system(), {"role": "user", "content": BULK}]
    kwargs: dict = {}
    apply_prompt_caching("gemini-3.7-flash", messages, kwargs)
    assert _breakpoint_count(messages) == 0
    assert "extra_headers" not in kwargs


# ── System prompt ─────────────────────────────────────────────────


def test_system_prompt_split_at_temporal_delimiter():
    """The volatile tail must land *after* the breakpoint or it kills the cache."""
    system = "STATIC RULES\n\n## Current Temporal Context\nNow: 2026-09-05 10:00"
    messages = [_system(system), {"role": "user", "content": "go"}]
    apply_prompt_caching(MODEL, messages, {})

    blocks = messages[0]["content"]
    assert len(blocks) == 2
    assert blocks[0]["text"] == "STATIC RULES"
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[1]["text"].startswith("## Current Temporal Context")
    assert "cache_control" not in blocks[1]


def test_system_prompt_without_delimiter_is_cached_whole():
    messages = [_system("STATIC RULES"), {"role": "user", "content": "go"}]
    apply_prompt_caching(MODEL, messages, {})

    blocks = messages[0]["content"]
    assert len(blocks) == 1
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


# ── Message history: the actual fix ───────────────────────────────


def test_message_history_gets_breakpoints():
    """The regression this module exists for: history must be cached too."""
    messages = [
        _system(),
        {"role": "user", "content": "start a cycle"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": BULK},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c2"}]},
        {"role": "tool", "tool_call_id": "c2", "content": BULK},
    ]
    apply_prompt_caching(MODEL, messages, {}, message_breakpoints=2)

    # Newest tool result and the one from the previous turn.
    assert _marked_indices(messages) == [3, 5]
    assert messages[5]["cache_control"] == {"type": "ephemeral"}
    assert messages[3]["cache_control"] == {"type": "ephemeral"}


def test_breakpoints_skip_assistant_messages():
    messages = [
        _system(),
        {"role": "user", "content": BULK},
        {"role": "assistant", "content": "thinking out loud"},
    ]
    apply_prompt_caching(MODEL, messages, {}, message_breakpoints=2)

    assert "cache_control" not in messages[2]
    assert _marked_indices(messages) == [1]


def test_breakpoints_land_one_per_turn_not_within_a_parallel_batch():
    """A batch of parallel tool results is one turn — spend one breakpoint on it."""
    messages = [
        _system(),
        {"role": "user", "content": BULK},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "a"}, {"id": "b"}]},
        {"role": "tool", "tool_call_id": "a", "content": BULK},
        {"role": "tool", "tool_call_id": "b", "content": BULK},
    ]
    apply_prompt_caching(MODEL, messages, {}, message_breakpoints=2)

    # Last of the batch, then back past the assistant turn to the user message.
    assert _marked_indices(messages) == [1, 4]


def test_list_content_marks_last_cacheable_block():
    messages = [
        _system(),
        {
            "role": "user",
            "content": [
                {"type": "text", "text": BULK},
                {"type": "text", "text": "and this"},
            ],
        },
    ]
    # multi-turn gating is exercised separately; this test is about placement.
    apply_prompt_caching(MODEL, messages, {}, message_breakpoints=1, require_multi_turn=False)

    blocks = messages[1]["content"]
    assert "cache_control" not in blocks[0]
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in messages[1]  # block level, not message level


def test_zero_breakpoints_restores_system_only_behaviour():
    messages = [
        _system(),
        {"role": "user", "content": BULK},
        {"role": "tool", "tool_call_id": "c1", "content": BULK},
    ]
    apply_prompt_caching(MODEL, messages, {}, message_breakpoints=0)

    assert _marked_indices(messages) == []
    assert _breakpoint_count(messages) == 1  # the system one


def test_short_history_skips_message_breakpoints():
    """A cache write nothing will read back is pure overhead."""
    messages = [_system(), {"role": "user", "content": "hi"}]
    apply_prompt_caching(MODEL, messages, {}, min_cacheable_chars=4000)

    assert _marked_indices(messages) == []


def test_never_exceeds_anthropic_breakpoint_limit():
    messages = [_system()]
    for i in range(20):
        messages.append({"role": "user", "content": BULK})
        messages.append({"role": "assistant", "content": f"reply {i}"})
    apply_prompt_caching(MODEL, messages, {}, message_breakpoints=3)

    assert _breakpoint_count(messages) <= 4


# ── Idempotency ───────────────────────────────────────────────────


def test_repeated_application_does_not_accumulate_breakpoints():
    """ADK can hand back the same message dicts; stale breakpoints must not pile up."""
    messages = [
        _system(),
        {"role": "user", "content": BULK},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": BULK},
    ]
    for _ in range(5):
        apply_prompt_caching(MODEL, messages, {}, message_breakpoints=2)

    assert _breakpoint_count(messages) <= 4
    assert _marked_indices(messages) == [1, 3]


def test_growing_conversation_moves_breakpoints_to_the_tail():
    messages = [
        _system(),
        {"role": "user", "content": BULK},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": BULK},
    ]
    apply_prompt_caching(MODEL, messages, {}, message_breakpoints=2)
    assert _marked_indices(messages) == [1, 3]

    # Next agentic turn appends a call + result.
    messages.append({"role": "assistant", "content": None, "tool_calls": [{"id": "c2"}]})
    messages.append({"role": "tool", "tool_call_id": "c2", "content": BULK})
    apply_prompt_caching(MODEL, messages, {}, message_breakpoints=2)

    assert _marked_indices(messages) == [3, 5]
    assert _breakpoint_count(messages) == 3  # system + 2


# ── TTL and headers ───────────────────────────────────────────────


def test_default_ttl_omits_the_ttl_field():
    messages = [_system(), {"role": "user", "content": BULK}]
    kwargs: dict = {}
    apply_prompt_caching(MODEL, messages, kwargs, ttl="5m")

    assert messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "extended-cache-ttl" not in kwargs["extra_headers"]["anthropic-beta"]


def test_one_hour_ttl_sets_field_and_beta_header():
    messages = [_system(), {"role": "user", "content": BULK}]
    kwargs: dict = {}
    apply_prompt_caching(MODEL, messages, kwargs, ttl="1h")

    assert messages[0]["content"][0]["cache_control"] == {
        "type": "ephemeral",
        "ttl": "1h",
    }
    assert "extended-cache-ttl-2025-04-11" in kwargs["extra_headers"]["anthropic-beta"]


def test_existing_beta_headers_are_preserved():
    messages = [_system(), {"role": "user", "content": BULK}]
    kwargs: dict = {"extra_headers": {"anthropic-beta": "some-other-beta"}}
    apply_prompt_caching(MODEL, messages, kwargs)

    betas = kwargs["extra_headers"]["anthropic-beta"]
    assert "some-other-beta" in betas
    assert "prompt-caching-2024-07-31" in betas


def test_unrelated_headers_are_preserved():
    messages = [_system(), {"role": "user", "content": BULK}]
    kwargs: dict = {"extra_headers": {"x-trace-id": "abc"}}
    apply_prompt_caching(MODEL, messages, kwargs)

    assert kwargs["extra_headers"]["x-trace-id"] == "abc"


# ── Contract test against LiteLLM itself ──────────────────────────


def test_litellm_actually_forwards_our_breakpoints_to_anthropic():
    """Guards against LiteLLM changing where it reads ``cache_control`` from.

    This is the test that matters: everything above only proves we wrote the
    key somewhere. This proves Anthropic will see it.
    """
    litellm_factory = pytest.importorskip("litellm.litellm_core_utils.prompt_templates.factory")

    messages = [
        _system("STATIC RULES\n\n## Current Temporal Context\nNow: 10:00"),
        {"role": "user", "content": "start a cycle " + BULK},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "gather", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": BULK},
    ]
    apply_prompt_caching(MODEL, messages, {}, message_breakpoints=2)

    translated = litellm_factory.anthropic_messages_pt(
        model="claude-opus-5",
        messages=[m for m in messages if m["role"] != "system"],
        llm_provider="anthropic",
    )

    # Walk the Anthropic-shaped output and find surviving breakpoints.
    survived = []
    for msg in translated:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("cache_control"):
                    survived.append(block.get("type"))

    assert "tool_result" in survived, (
        "cache_control on a tool-result message did not survive LiteLLM's "
        f"Anthropic transformation; got {survived}"
    )
    assert "text" in survived, (
        "cache_control on a string-content user message did not survive "
        f"LiteLLM's Anthropic transformation; got {survived}"
    )


# ── Multi-turn gating ─────────────────────────────────────────────


def test_first_call_of_a_session_skips_message_breakpoints():
    """messages == [system, user]: nothing will ever read this cache back."""
    messages = [_system(), {"role": "user", "content": BULK}]
    apply_prompt_caching(MODEL, messages, {}, require_multi_turn=True)

    assert _marked_indices(messages) == []
    assert _breakpoint_count(messages) == 1  # system only


def test_second_call_of_a_session_gets_message_breakpoints():
    messages = [
        _system(),
        {"role": "user", "content": BULK},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": BULK},
    ]
    apply_prompt_caching(MODEL, messages, {}, require_multi_turn=True)

    assert _marked_indices(messages) == [1, 3]


def test_multi_turn_gate_can_be_disabled():
    messages = [_system(), {"role": "user", "content": BULK}]
    apply_prompt_caching(MODEL, messages, {}, require_multi_turn=False)

    assert _marked_indices(messages) == [1]
