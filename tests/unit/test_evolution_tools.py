from unittest.mock import AsyncMock, MagicMock

import pytest

from evotrader.evolution.tools import (
    bind_evolution_dependencies,
    get_cycle_digest,
    propose_parameter_change,
    query_cycle_thoughts,
    submit_code_review,
)


@pytest.fixture(autouse=True)
def setup_evolution_dependencies():
    # Setup mocks
    analyser = MagicMock()
    code_evolver = MagicMock()
    instruction_evolver = MagicMock()
    algo_registry = MagicMock()
    memory = MagicMock()
    config = MagicMock()
    thought_logger = AsyncMock()

    # Bind mocks
    bind_evolution_dependencies(
        analyser=analyser,
        code_evolver=code_evolver,
        instruction_evolver=instruction_evolver,
        algo_registry=algo_registry,
        memory=memory,
        config=config,
        thought_logger=thought_logger,
    )

    yield {
        "analyser": analyser,
        "code_evolver": code_evolver,
        "instruction_evolver": instruction_evolver,
        "algo_registry": algo_registry,
        "memory": memory,
        "config": config,
        "thought_logger": thought_logger,
    }

    # Reset/cleanup globals to none
    bind_evolution_dependencies(None, None, None, None, None, None, None)


@pytest.mark.asyncio
async def test_propose_parameter_change_safe_parsing(setup_evolution_dependencies):
    registry = setup_evolution_dependencies["algo_registry"]
    registry.save_version.return_value = "/path/to/version"
    registry.get_active_version.return_value = "v001"

    # Malformed JSON with markdown blocks and trailing characters
    malformed_param_json = (
        '```json\n{\n  "rsi_period": 14,\n  "rsi_overbought": 70\n}\n```\nExtra garbage'
    )

    result = await propose_parameter_change(
        version_name="v002",
        description="New RSI thresholds",
        reasoning="Improve performance",
        parameters_json=malformed_param_json,
    )

    assert "error" not in result
    assert result["status"] == "saved"

    # Verify the parameters dict was parsed cleanly and passed to the registry
    registry.save_version.assert_called_once_with(
        "v002",
        {"rsi_period": 14, "rsi_overbought": 70},
        {
            "name": "New RSI thresholds",
            "version": "v002",
            "description": "Improve performance",
            "created_by": "evolution_agent",
            "change_type": "ALGORITHM_PARAMS",
            "status": "proposed",
        },
    )


@pytest.mark.asyncio
async def test_submit_code_review_safe_parsing(setup_evolution_dependencies):
    evolver = setup_evolution_dependencies["code_evolver"]
    evolver.save_code_review.return_value = MagicMock()

    # Malformed arrays with markdown backticks or extra elements
    malformed_files_json = '```json\n["src/evotrader/main.py"]\n```'
    malformed_findings_json = '```json\n[{"severity": "info", "file": "main.py", "issue": "Clean", "suggestion": "None"}]\n```'
    malformed_diffs_json = (
        '```\n[{"file": "main.py", "description": "Formatting", "diff": ""}]\n```'
    )

    result = await submit_code_review(
        review_title="clean_main",
        files_reviewed_json=malformed_files_json,
        findings_json=malformed_findings_json,
        proposed_diffs_json=malformed_diffs_json,
    )

    assert "error" not in result

    # Verify code evolver received cleaned and parsed lists/dicts
    evolver.save_code_review.assert_called_once()
    _args, kwargs = evolver.save_code_review.call_args
    assert kwargs["files_reviewed"] == ["src/evotrader/main.py"]
    assert kwargs["findings"] == [
        {"severity": "info", "file": "main.py", "issue": "Clean", "suggestion": "None"}
    ]
    assert kwargs["proposed_diffs"] == [
        {"file": "main.py", "description": "Formatting", "diff": ""}
    ]


@pytest.mark.asyncio
async def test_activate_instruction_version_auto_promote(setup_evolution_dependencies):
    from evotrader.evolution.tools import activate_instruction_version

    config = setup_evolution_dependencies["config"]
    config.evolution.auto_promote = False

    # Try calling activate_instruction_version
    result = await activate_instruction_version("strategy", "v002")
    assert result["status"] == "pending_review"
    assert "Auto-promotion is disabled" in result["message"]

    # Now enable auto_promote and check it forwards call
    config.evolution.auto_promote = True
    evolver = setup_evolution_dependencies["instruction_evolver"]
    evolver.activate_version.return_value = {"status": "activated"}

    result = await activate_instruction_version("strategy", "v002")
    assert result["status"] == "activated"
    evolver.activate_version.assert_called_once_with("strategy", "v002")


@pytest.mark.asyncio
async def test_promote_algorithm_version_auto_promote(setup_evolution_dependencies):
    from evotrader.evolution.tools import promote_algorithm_version

    config = setup_evolution_dependencies["config"]
    config.evolution.auto_promote = False

    # Try calling promote_algorithm_version
    result = await promote_algorithm_version("v002")
    assert result["status"] == "pending_review"
    assert "Auto-promotion is disabled" in result["message"]

    # Now enable auto_promote and check it forwards call
    config.evolution.auto_promote = True
    registry = setup_evolution_dependencies["algo_registry"]
    registry.get_active_version.return_value = "v001"

    result = await promote_algorithm_version("v002")
    assert result["status"] == "promoted"
    registry.set_active_version.assert_called_once_with("v002")


@pytest.mark.asyncio
async def test_get_cycle_digest(setup_evolution_dependencies):
    thought_logger = setup_evolution_dependencies["thought_logger"]

    # Mock unique sessions
    thought_logger.get_unique_sessions.return_value = [
        {"session_id": "sess-1", "start_time": "2026-06-23T12:00:00Z"},
        {"session_id": "sess-2", "start_time": "2026-06-23T13:00:00Z"},
    ]

    # Mock get_final_thoughts for each session
    async def mock_get_final_thoughts(session_id):
        if session_id == "sess-1":
            return [
                {"agent_name": "orchestrator", "content": "Sess 1 Orchestrator thought"},
                {"agent_name": "strategy", "content": "Sess 1 Strategy thought"},
            ]
        elif session_id == "sess-2":
            return [
                {"agent_name": "orchestrator", "content": "Sess 2 Orchestrator thought"},
                {"agent_name": "strategy", "content": "Sess 2 Strategy thought"},
                {"agent_name": "news_sentiment", "content": "Sess 2 News thought"},
            ]
        return []

    thought_logger.get_final_thoughts = mock_get_final_thoughts

    result = await get_cycle_digest(limit=2)

    assert "cycles" in result
    assert result["count"] == 2
    assert len(result["cycles"]) == 2

    # Assert session 1 contents
    s1 = result["cycles"][0]
    assert s1["session_id"] == "sess-1"
    assert s1["final_thoughts"]["orchestrator"] == "Sess 1 Orchestrator thought"
    assert s1["final_thoughts"]["strategy"] == "Sess 1 Strategy thought"
    assert "news_sentiment" not in s1["final_thoughts"]

    # Assert session 2 contents
    s2 = result["cycles"][1]
    assert s2["session_id"] == "sess-2"
    assert s2["final_thoughts"]["orchestrator"] == "Sess 2 Orchestrator thought"
    assert s2["final_thoughts"]["strategy"] == "Sess 2 Strategy thought"
    assert s2["final_thoughts"]["news_sentiment"] == "Sess 2 News thought"


@pytest.mark.asyncio
async def test_query_cycle_thoughts_filtered(setup_evolution_dependencies):
    thought_logger = setup_evolution_dependencies["thought_logger"]

    # Mock get_unique_sessions to return a session if query_cycle_thoughts defaults session_id
    thought_logger.get_unique_sessions.return_value = [{"session_id": "sess-default"}]

    # Mock get_recent_thoughts
    thought_logger.get_recent_thoughts.return_value = [
        {"id": 1, "agent_name": "strategy", "event_type": "thought", "content": "strategy thought"},
    ]

    # Test filtered by agent and type
    result = await query_cycle_thoughts(
        session_id="sess-xyz",
        agent_name="strategy",
        event_type="thought",
        limit=10,
    )

    assert result["session_id"] == "sess-xyz"
    assert result["count"] == 1
    assert result["events"][0]["content"] == "strategy thought"

    # Verify thought logger was queried with filters
    thought_logger.get_recent_thoughts.assert_called_once_with(
        limit=10,
        session_id="sess-xyz",
        agent_name="strategy",
        event_type="thought",
    )
