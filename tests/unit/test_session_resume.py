"""Tests for evolution session reconstruction and continue functionality."""

from __future__ import annotations

from evotrader.evolution.evolution_service import (
    _parse_meta,
    _reconstruct_adk_events,
)


class TestParseMetaHelper:
    """Tests for _parse_meta utility."""

    def test_none(self) -> None:
        assert _parse_meta(None) is None

    def test_dict_passthrough(self) -> None:
        d = {"args": {"limit": 5}}
        assert _parse_meta(d) is d

    def test_json_string(self) -> None:
        assert _parse_meta('{"args": {"limit": 5}}') == {"args": {"limit": 5}}

    def test_invalid_json(self) -> None:
        assert _parse_meta("not json") is None


class TestReconstructADKEvents:
    """Tests for _reconstruct_adk_events."""

    def test_empty(self) -> None:
        assert _reconstruct_adk_events([]) == []

    def test_single_thought(self) -> None:
        events = [
            {
                "agent_name": "evolution",
                "event_type": "thought",
                "content": "I'll start...",
                "meta": None,
            },
        ]
        adk_events = _reconstruct_adk_events(events)
        assert len(adk_events) == 1
        assert adk_events[0].content.role == "model"
        assert adk_events[0].content.parts[0].text == "I'll start..."

    def test_single_tool_call(self) -> None:
        events = [
            {
                "agent_name": "evolution",
                "event_type": "tool_call",
                "content": "get_cycle_digest",
                "meta": '{"args": {"limit": 10}}',
            },
        ]
        adk_events = _reconstruct_adk_events(events)
        assert len(adk_events) == 1
        assert adk_events[0].content.role == "model"
        fc = adk_events[0].content.parts[0].function_call
        assert fc.name == "get_cycle_digest"
        assert fc.args == {"limit": 10}

    def test_single_tool_response(self) -> None:
        events = [
            {
                "agent_name": "evolution",
                "event_type": "tool_response",
                "content": "get_cycle_digest",
                "meta": '{"response": {"cycles": []}}',
            },
        ]
        adk_events = _reconstruct_adk_events(events)
        assert len(adk_events) == 1
        assert adk_events[0].content.role == "user"
        fr = adk_events[0].content.parts[0].function_response
        assert fr.name == "get_cycle_digest"
        assert fr.response == {"cycles": []}

    def test_concurrent_tool_calls_grouped(self) -> None:
        """Consecutive tool_call events should be grouped into one Content."""
        events = [
            {
                "agent_name": "evolution",
                "event_type": "tool_call",
                "content": "get_cycle_digest",
                "meta": '{"args": {"limit": 10}}',
            },
            {
                "agent_name": "evolution",
                "event_type": "tool_call",
                "content": "analyse_performance",
                "meta": '{"args": {"lookback_days": 30}}',
            },
        ]
        adk_events = _reconstruct_adk_events(events)
        assert len(adk_events) == 1
        assert len(adk_events[0].content.parts) == 2
        assert adk_events[0].content.parts[0].function_call.name == "get_cycle_digest"
        assert adk_events[0].content.parts[1].function_call.name == "analyse_performance"

    def test_concurrent_tool_responses_grouped(self) -> None:
        """Consecutive tool_response events should be grouped into one Content."""
        events = [
            {
                "agent_name": "evolution",
                "event_type": "tool_response",
                "content": "get_cycle_digest",
                "meta": '{"response": {"cycles": []}}',
            },
            {
                "agent_name": "evolution",
                "event_type": "tool_response",
                "content": "analyse_performance",
                "meta": '{"response": {"overall": {}}}',
            },
        ]
        adk_events = _reconstruct_adk_events(events)
        assert len(adk_events) == 1
        assert len(adk_events[0].content.parts) == 2
        assert adk_events[0].content.parts[0].function_response.name == "get_cycle_digest"
        assert adk_events[0].content.parts[1].function_response.name == "analyse_performance"

    def test_full_conversation_round_trip(self) -> None:
        """Reconstruct a realistic mini-conversation: thought → calls → responses → thought."""
        events = [
            {
                "agent_name": "evolution",
                "event_type": "thought",
                "content": "I'll start by reviewing.",
                "meta": None,
            },
            {
                "agent_name": "evolution",
                "event_type": "tool_call",
                "content": "get_cycle_digest",
                "meta": '{"args": {"limit": 10}}',
            },
            {
                "agent_name": "evolution",
                "event_type": "tool_call",
                "content": "analyse_performance",
                "meta": '{"args": {"lookback_days": 30}}',
            },
            {
                "agent_name": "evolution",
                "event_type": "tool_response",
                "content": "get_cycle_digest",
                "meta": '{"response": {"cycles": [{"id": "abc"}]}}',
            },
            {
                "agent_name": "evolution",
                "event_type": "tool_response",
                "content": "analyse_performance",
                "meta": '{"response": {"overall": {"win_rate": 0.5}}}',
            },
            {
                "agent_name": "evolution",
                "event_type": "thought",
                "content": "Now let me dig deeper.",
                "meta": None,
            },
        ]

        adk_events = _reconstruct_adk_events(events)

        # Expected: thought, grouped-calls, grouped-responses, thought
        assert len(adk_events) == 4

        # 1. First thought
        assert adk_events[0].content.role == "model"
        assert adk_events[0].content.parts[0].text == "I'll start by reviewing."

        # 2. Grouped tool calls
        assert adk_events[1].content.role == "model"
        assert len(adk_events[1].content.parts) == 2
        assert adk_events[1].content.parts[0].function_call.name == "get_cycle_digest"
        assert adk_events[1].content.parts[1].function_call.name == "analyse_performance"

        # 3. Grouped tool responses
        assert adk_events[2].content.role == "user"
        assert len(adk_events[2].content.parts) == 2
        assert adk_events[2].content.parts[0].function_response.name == "get_cycle_digest"
        assert adk_events[2].content.parts[1].function_response.name == "analyse_performance"

        # 4. Second thought
        assert adk_events[3].content.role == "model"
        assert adk_events[3].content.parts[0].text == "Now let me dig deeper."

    def test_unknown_event_types_skipped(self) -> None:
        """Unknown event types (e.g., 'continuation') should be silently skipped."""
        events = [
            {
                "agent_name": "system",
                "event_type": "continuation",
                "content": "Continuation of session abc",
                "meta": None,
            },
            {
                "agent_name": "evolution",
                "event_type": "thought",
                "content": "Continuing...",
                "meta": None,
            },
        ]
        adk_events = _reconstruct_adk_events(events)
        assert len(adk_events) == 1
        assert adk_events[0].content.parts[0].text == "Continuing..."

    def test_missing_meta_handled(self) -> None:
        """Tool calls with missing meta should use empty args."""
        events = [
            {
                "agent_name": "evolution",
                "event_type": "tool_call",
                "content": "get_strategy_manifest",
                "meta": None,
            },
        ]
        adk_events = _reconstruct_adk_events(events)
        assert adk_events[0].content.parts[0].function_call.args == {}

    def test_stored_tool_call_ids_used(self) -> None:
        """When meta contains tool_call_id, reconstruction should use it."""
        events = [
            {
                "agent_name": "evolution",
                "event_type": "tool_call",
                "content": "get_cycle_digest",
                "meta": '{"args": {"limit": 10}, "tool_call_id": "toolu_abc123"}',
            },
            {
                "agent_name": "evolution",
                "event_type": "tool_response",
                "content": "get_cycle_digest",
                "meta": '{"response": {"cycles": []}, "tool_call_id": "toolu_abc123"}',
            },
        ]
        adk_events = _reconstruct_adk_events(events)

        fc = adk_events[0].content.parts[0].function_call
        fr = adk_events[1].content.parts[0].function_response
        assert fc.id == "toolu_abc123"
        assert fr.id == "toolu_abc123"

    def test_stored_ids_with_concurrent_calls(self) -> None:
        """Stored IDs should pair correctly for concurrent tool calls."""
        events = [
            {
                "agent_name": "evolution",
                "event_type": "tool_call",
                "content": "get_cycle_digest",
                "meta": '{"args": {}, "tool_call_id": "toolu_001"}',
            },
            {
                "agent_name": "evolution",
                "event_type": "tool_call",
                "content": "analyse_performance",
                "meta": '{"args": {}, "tool_call_id": "toolu_002"}',
            },
            {
                "agent_name": "evolution",
                "event_type": "tool_response",
                "content": "get_cycle_digest",
                "meta": '{"response": {}, "tool_call_id": "toolu_001"}',
            },
            {
                "agent_name": "evolution",
                "event_type": "tool_response",
                "content": "analyse_performance",
                "meta": '{"response": {}, "tool_call_id": "toolu_002"}',
            },
        ]
        adk_events = _reconstruct_adk_events(events)

        call_ids = [p.function_call.id for p in adk_events[0].content.parts]
        resp_ids = [p.function_response.id for p in adk_events[1].content.parts]
        assert call_ids == ["toolu_001", "toolu_002"]
        assert resp_ids == ["toolu_001", "toolu_002"]

    def test_legacy_fallback_ids_generated(self) -> None:
        """Legacy data without tool_call_id should get synthetic IDs."""
        events = [
            {
                "agent_name": "evolution",
                "event_type": "tool_call",
                "content": "get_cycle_digest",
                "meta": '{"args": {"limit": 10}}',
            },
        ]
        adk_events = _reconstruct_adk_events(events)
        fc = adk_events[0].content.parts[0].function_call
        assert fc.id is not None
        assert fc.id.startswith("recon_")

    def test_legacy_fallback_ids_paired(self) -> None:
        """Legacy call/response pairs should get matching synthetic IDs."""
        events = [
            {
                "agent_name": "evolution",
                "event_type": "tool_call",
                "content": "get_cycle_digest",
                "meta": '{"args": {}}',
            },
            {
                "agent_name": "evolution",
                "event_type": "tool_call",
                "content": "analyse_performance",
                "meta": '{"args": {}}',
            },
            {
                "agent_name": "evolution",
                "event_type": "tool_response",
                "content": "get_cycle_digest",
                "meta": '{"response": {}}',
            },
            {
                "agent_name": "evolution",
                "event_type": "tool_response",
                "content": "analyse_performance",
                "meta": '{"response": {}}',
            },
        ]
        adk_events = _reconstruct_adk_events(events)

        call_ids = [p.function_call.id for p in adk_events[0].content.parts]
        resp_ids = [p.function_response.id for p in adk_events[1].content.parts]
        assert call_ids == resp_ids
        assert len(set(call_ids)) == 2  # All unique

    def test_multiple_rounds_have_unique_ids(self) -> None:
        """IDs across multiple call/response rounds should all be unique."""
        events = [
            {
                "agent_name": "evolution",
                "event_type": "tool_call",
                "content": "tool_a",
                "meta": '{"args": {}, "tool_call_id": "toolu_round1"}',
            },
            {
                "agent_name": "evolution",
                "event_type": "tool_response",
                "content": "tool_a",
                "meta": '{"response": {}, "tool_call_id": "toolu_round1"}',
            },
            {
                "agent_name": "evolution",
                "event_type": "thought",
                "content": "Thinking...",
                "meta": None,
            },
            {
                "agent_name": "evolution",
                "event_type": "tool_call",
                "content": "tool_b",
                "meta": '{"args": {}, "tool_call_id": "toolu_round2"}',
            },
            {
                "agent_name": "evolution",
                "event_type": "tool_response",
                "content": "tool_b",
                "meta": '{"response": {}, "tool_call_id": "toolu_round2"}',
            },
        ]
        adk_events = _reconstruct_adk_events(events)

        id_round1_call = adk_events[0].content.parts[0].function_call.id
        id_round1_resp = adk_events[1].content.parts[0].function_response.id
        id_round2_call = adk_events[3].content.parts[0].function_call.id
        id_round2_resp = adk_events[4].content.parts[0].function_response.id

        assert id_round1_call == "toolu_round1"
        assert id_round1_resp == "toolu_round1"
        assert id_round2_call == "toolu_round2"
        assert id_round2_resp == "toolu_round2"
