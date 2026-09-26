from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from evotrader.config import AppConfig
from evotrader.web.server import create_app


@pytest.fixture(autouse=True)
def mock_dashboard_password():
    """Mock the DASHBOARD_PASSWORD env var in tests to keep it isolated."""
    with patch.dict(os.environ, {"DASHBOARD_PASSWORD": "test_password"}):
        yield


@pytest.fixture
def mock_app_dependencies(tmp_path):
    """Fixture to mock all app dependencies."""
    db = MagicMock()
    journal = MagicMock()
    journal.get_pnl = AsyncMock(return_value=0.0)
    journal.get_today_pnl = AsyncMock(return_value=0.0)
    journal.get_consecutive_losses = AsyncMock(return_value=0)
    journal.get_trade_count = AsyncMock(return_value=0)
    journal.get_period_win_loss_count = AsyncMock(return_value=(0, 0))
    journal.get_trade_count_today = AsyncMock(return_value=0)
    journal.get_period_metrics = AsyncMock(
        return_value={"trade_count": 0, "pnl": 0.0, "wins": 0, "losses": 0}
    )
    journal.get_consecutive_no_trades = AsyncMock(return_value=0)
    journal.get_open_trades = AsyncMock(return_value=[])
    journal.get_recent_trades = AsyncMock(return_value=[])
    journal.delete_trade = AsyncMock()
    metrics = MagicMock()
    metrics.get_cumulative_adjustments_by_date = AsyncMock(return_value={})
    metrics.get_cash_adjustments = AsyncMock(return_value=[])
    metrics.get_metrics_range = AsyncMock(return_value=[])
    mcp_toolset = MagicMock()
    mcp_toolset._mcp_session_manager = MagicMock()
    mcp_toolset._mcp_session_manager.create_session = AsyncMock(return_value=MagicMock())

    # Minimal config
    config = AppConfig(data_dir=tmp_path / "data")

    runner_fn = MagicMock()
    memory = MagicMock()

    # Setup memory mocks
    memory.stats.return_value = {"trade_experiences": 5, "market_patterns": 2, "user_notes": 10}
    memory.get_all_notes.return_value = [
        {
            "id": "note_1",
            "text": "Note text",
            "metadata": {"source": "file", "category": "instruction", "priority": "high"},
        }
    ]
    memory.get_all_trade_experiences.return_value = [
        {
            "id": "trade_1",
            "text": "Trade description",
            "metadata": {"trade_id": 1, "outcome_pnl": 150.0},
        }
    ]
    memory.prune.return_value = {"trade_experiences": 1}

    return db, journal, metrics, mcp_toolset, config, runner_fn, memory


@pytest.fixture
def client(mock_app_dependencies):
    db, journal, metrics, mcp_toolset, config, runner_fn, memory = mock_app_dependencies

    evolution_service = MagicMock()
    evolution_service.is_running.return_value = False
    evolution_service.get_last_run.return_value = "2026-06-21T10:00:00Z"

    app = create_app(
        db=db,
        journal=journal,
        metrics=metrics,
        mcp_toolset=mcp_toolset,
        config=config,
        runner_fn=runner_fn,
        memory=memory,
        evolution_service=evolution_service,
    )
    tc = TestClient(app)
    # Automatically add auth header
    tc.headers.update({"Authorization": "Bearer test_password"})
    return tc, config, memory


def test_memory_endpoints(client):
    tc, _, memory = client

    # 1. Stats
    resp = tc.get("/api/memory/stats")
    assert resp.status_code == 200
    assert resp.json() == {"trade_experiences": 5, "market_patterns": 2, "user_notes": 10}
    memory.stats.assert_called_once()

    # 2. Notes
    resp = tc.get("/api/memory/notes")
    assert resp.status_code == 200
    assert "notes" in resp.json()
    assert len(resp.json()["notes"]) == 1

    # 3. Trades
    resp = tc.get("/api/memory/trades")
    assert resp.status_code == 200
    assert "trades" in resp.json()
    assert len(resp.json()["trades"]) == 1

    # 4. Prune
    resp = tc.post("/api/memory/prune")
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    assert resp.json()["pruned"] == {"trade_experiences": 1}

    # 5. Clear
    resp = tc.post("/api/memory/clear")
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    memory.clear_all_memories.assert_called_once()
    memory.load_notes_from_directory.assert_called_once()


def test_notes_file_crud_endpoints(client):
    tc, config, _memory = client
    notes_dir = config.notes_dir

    # Create a dummy note file
    note_file = notes_dir / "test_rules.md"
    note_content = "---\ncategory: instruction\npriority: critical\n---\nDon't lose money."
    note_file.write_text(note_content)

    # 1. List note files
    resp = tc.get("/api/notes/files")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["filename"] == "test_rules.md"
    assert resp.json()[0]["category"] == "instruction"
    assert resp.json()[0]["priority"] == "critical"

    # 2. Get specific file details
    resp = tc.get("/api/notes/files/test_rules.md")
    assert resp.status_code == 200
    assert resp.json()["filename"] == "test_rules.md"
    assert resp.json()["content"] == "Don't lose money."
    assert resp.json()["category"] == "instruction"
    assert resp.json()["priority"] == "critical"

    # 3. Save / POST file details
    payload = {"category": "market_insight", "priority": "high", "content": "Updated rule content."}
    resp = tc.post("/api/notes/files/new_note.md", json=payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"

    saved_file = notes_dir / "new_note.md"
    assert saved_file.is_file()
    saved_text = saved_file.read_text()
    assert "category: market_insight" in saved_text
    assert "priority: high" in saved_text
    assert "Updated rule content." in saved_text

    # 4. Delete file
    resp = tc.delete("/api/notes/files/new_note.md")
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    assert not saved_file.is_file()


def test_instructions_endpoints(client):
    tc, config, _ = client
    instructions_dir = config.instructions_dir

    # Set up dummy agent instructions
    agent_dir = instructions_dir / "strategy"
    agent_dir.mkdir()
    (agent_dir / "active.txt").write_text("v002")
    (agent_dir / "v001.md").write_text("# Old rules")
    (agent_dir / "v002.md").write_text("# New strategy instructions content")

    # 1. List instructions
    resp = tc.get("/api/instructions")
    assert resp.status_code == 200
    agents = resp.json()["agents"]
    assert len(agents) == 1
    assert agents[0]["name"] == "strategy"
    assert agents[0]["active_version"] == "v002"
    assert set(agents[0]["versions"]) == {"v001", "v002"}

    # 2. Get active version content
    resp = tc.get("/api/instructions/strategy/v002")
    assert resp.status_code == 200
    assert resp.json()["agent_name"] == "strategy"
    assert resp.json()["version"] == "v002"
    assert resp.json()["content"] == "# New strategy instructions content"
    assert resp.json()["is_active"] is True

    # 3. Get old version content
    resp = tc.get("/api/instructions/strategy/v001")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False
    assert resp.json()["content"] == "# Old rules"

    # 4. Activate version v001
    resp = tc.post("/api/instructions/strategy/v001/activate")
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    assert (agent_dir / "active.txt").read_text().strip() == "v001"


def test_evolution_endpoints(client):
    tc, _, _ = client
    evolution_service = tc.app.state.evolution_service

    # 1. Check status
    resp = tc.get("/api/evolution/status")
    assert resp.status_code == 200
    assert resp.json() == {"running": False, "last_run": "2026-06-21T10:00:00Z"}

    # 2. Trigger evolution
    resp = tc.post("/api/evolution/trigger")
    assert resp.status_code == 200
    assert resp.json()["status"] == "triggered"

    # 3. Trigger when already running should raise 409
    evolution_service.is_running.return_value = True
    resp = tc.post("/api/evolution/trigger")
    assert resp.status_code == 409


def test_portfolio_endpoint(client):
    tc, config, _ = client

    resp = tc.get("/api/portfolio")
    assert resp.status_code == 200
    data = resp.json()
    assert "broker" in data
    assert "local" in data
    assert "status" in data
    expected_db_path = (
        str(config.db_path.relative_to(config.project_root))
        if config.db_path.is_relative_to(config.project_root)
        else str(config.db_path)
    )
    assert data["local"]["db_path"] == expected_db_path


def test_get_evolution_runs_endpoint(client):
    tc, _, _ = client

    with patch("evotrader.db.thought_log.ThoughtLogger.get_evolution_runs") as mock_runs:
        mock_runs.return_value = [
            {
                "session_id": "sess_1",
                "timestamp": "2026-06-21T12:00:00Z",
                "status": "SUCCESS",
                "error": None,
                "summary": "Done",
            }
        ]

        resp = tc.get("/api/evolution/runs")
        assert resp.status_code == 200
        assert resp.json() == [
            {
                "session_id": "sess_1",
                "timestamp": "2026-06-21T12:00:00Z",
                "status": "SUCCESS",
                "error": None,
                "summary": "Done",
                "llm_call_count": 0,
            }
        ]
        mock_runs.assert_called_once()


def test_delete_cycle_endpoint(client):
    tc, _, memory = client
    journal = tc.app.state.journal

    # We will simulate events that deletion reverts
    thoughts = [
        {"content": "store_learning", "meta": json.dumps({"response": {"note_id": "note_123"}})},
        {"content": "record_trade", "meta": json.dumps({"response": {"trade_id": 456}})},
        {
            "content": "propose_instruction_change",
            "meta": json.dumps(
                {
                    "response": {
                        "agent": "strategy",
                        "new_version": "v002",
                        "previous_version": "v001",
                        "file_path": "dummy_path.md",
                    }
                }
            ),
        },
    ]

    with (
        patch(
            "evotrader.db.thought_log.ThoughtLogger.get_recent_thoughts", return_value=thoughts
        ) as mock_get_thoughts,
        patch("evotrader.db.thought_log.ThoughtLogger.delete_cycle_logs") as mock_del_logs,
        patch("pathlib.Path.unlink"),
        patch("pathlib.Path.is_file", return_value=True),
        patch("pathlib.Path.read_text", return_value="v002") as mock_read,
        patch("pathlib.Path.write_text") as mock_write,
    ):
        resp = tc.delete("/api/cycles/sess_abc")
        assert resp.status_code == 200
        assert resp.json() == {
            "status": "success",
            "message": "Cycle sess_abc successfully deleted and reverted.",
        }

        # Verify side effects called
        memory.delete_note.assert_called_once_with("note_123")
        journal.delete_trade.assert_called_once_with(456)
        mock_get_thoughts.assert_called_once()
        mock_del_logs.assert_called_once_with("sess_abc")
        mock_read.assert_called()
        mock_write.assert_called_once_with("v001")


def test_algorithms_endpoints(client):
    tc, config, _ = client
    algo_dir = config.algorithms_dir

    # 1. Create dummy version directories & configurations
    v1_dir = algo_dir / "v001_initial"
    v2_dir = algo_dir / "v002_relaxed_rsi"
    v1_dir.mkdir(parents=True, exist_ok=True)
    v2_dir.mkdir(parents=True, exist_ok=True)

    (v1_dir / "config.yaml").write_text("rsi_oversold: 30\nrsi_overbought: 70")
    (v1_dir / "metadata.yaml").write_text(
        "name: Initial Strategy\nversion: v001_initial\ncreated_by: system"
    )

    (v2_dir / "config.yaml").write_text("rsi_oversold: 35\nrsi_overbought: 65")
    (v2_dir / "metadata.yaml").write_text(
        "name: Relaxed Strategy\nversion: v002_relaxed_rsi\ncreated_by: evolution"
    )

    # Create registry.yaml
    import yaml

    registry_yaml = {
        "versions": [
            {"version": "v001_initial", "name": "Initial Strategy", "created_by": "system"},
            {"version": "v002_relaxed_rsi", "name": "Relaxed Strategy", "created_by": "evolution"},
        ]
    }
    with open(algo_dir / "registry.yaml", "w") as f:
        yaml.dump(registry_yaml, f)

    (algo_dir / "active.yaml").write_text("active_version: v001_initial")

    # 2. Test list algorithms endpoint
    resp = tc.get("/api/algorithms")
    assert resp.status_code == 200
    data = resp.json()
    assert data["active_version"] == "v001_initial"
    assert len(data["versions"]) == 2
    assert data["versions"][0]["version"] == "v001_initial"
    assert data["versions"][1]["version"] == "v002_relaxed_rsi"

    # 3. Test get specific algorithm version
    resp = tc.get("/api/algorithms/v002_relaxed_rsi")
    assert resp.status_code == 200
    data = resp.json()
    assert data["version"] == "v002_relaxed_rsi"
    assert "rsi_oversold: 35" in data["content"]
    assert data["metadata"]["created_by"] == "evolution"
    assert data["is_active"] is False
    assert "rsi_oversold" in data["diff"]  # check unified diff is computed

    # 4. Test activate algorithm
    resp = tc.post("/api/algorithms/v002_relaxed_rsi/activate")
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"

    # Check registry active version is updated
    resp = tc.get("/api/algorithms")
    assert resp.json()["active_version"] == "v002_relaxed_rsi"


def test_get_thoughts_endpoint(client):
    tc, _, _ = client

    db_thoughts = [
        {
            "id": 1,
            "timestamp": "2026-06-21T12:00:00Z",
            "session_id": "sess_123",
            "agent_name": "market_intelligence",
            "event_type": "thought",
            "content": "RSI is high",
            "meta": None,
        },
        {
            "id": 2,
            "timestamp": "2026-06-21T12:01:00Z",
            "session_id": "sess_123",
            "agent_name": "market_intelligence",
            "event_type": "tool_call",
            "content": "compute_indicators",
            "meta": json.dumps({"args": {}, "tool_info": {"source": "local"}}),
        },
        {
            "id": 3,
            "timestamp": "2026-06-21T12:02:00Z",
            "session_id": "sess_123",
            "agent_name": "market_intelligence",
            "event_type": "tool_response",
            "content": "compute_indicators",
            "meta": json.dumps({"response": {"rsi": 75}, "tool_info": {"source": "local"}}),
        },
    ]

    with patch(
        "evotrader.db.thought_log.ThoughtLogger.get_recent_thoughts", return_value=db_thoughts
    ) as mock_get_thoughts:
        resp = tc.get("/api/thoughts?session_id=sess_123")
        assert resp.status_code == 200
        thoughts = resp.json()["thoughts"]
        assert len(thoughts) == 3

        # Chronological order check (get_thoughts reverses the database list)
        assert thoughts[0]["type"] == "tool_response"  # db_thoughts is reversed
        assert thoughts[0]["tool_info"]["source"] == "local"

        assert thoughts[1]["type"] == "tool_call"
        assert thoughts[1]["tool_info"]["source"] == "local"

        assert thoughts[2]["type"] == "thought"
        assert thoughts[2]["content"] == "RSI is high"

        mock_get_thoughts.assert_called_once()


def test_cron_scheduler_endpoints(client):
    tc, config, _ = client

    # Configure cron settings
    config.settings.schedule.cycle_cron_enabled = False
    config.settings.schedule.evolution_cron_enabled = False
    config.settings.schedule.cycle_cron = "30 8-15 * * 1-5"
    config.settings.schedule.evolution_cron = "0 17 * * 1-5"

    # 1. Test status endpoint when disabled
    resp = tc.get("/api/cron/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cycle_cron_enabled"] is False
    assert data["evolution_cron_enabled"] is False
    assert data["cron_expression"] == "30 8-15 * * 1-5"
    assert data["evolution_cron_expression"] == "0 17 * * 1-5"
    assert data["seconds_until_next_cycle"] is not None
    assert data["seconds_until_next_evolution"] is not None

    # 2. Test toggle ON for cycle
    resp = tc.post("/api/cron/toggle", json={"type": "cycle", "enabled": True})
    assert resp.status_code == 200
    assert resp.json()["cycle_cron_enabled"] is True
    assert resp.json()["evolution_cron_enabled"] is False

    # 3. Test status endpoint when cycle enabled
    resp = tc.get("/api/cron/status")
    assert resp.status_code == 200
    assert resp.json()["cycle_cron_enabled"] is True
    assert resp.json()["evolution_cron_enabled"] is False

    # 4. Test toggle ON for evolution
    resp = tc.post("/api/cron/toggle", json={"type": "evolution", "enabled": True})
    assert resp.status_code == 200
    assert resp.json()["evolution_cron_enabled"] is True
    assert resp.json()["cycle_cron_enabled"] is True

    # 5. Test status endpoint when both enabled
    resp = tc.get("/api/cron/status")
    assert resp.status_code == 200
    assert resp.json()["cycle_cron_enabled"] is True
    assert resp.json()["evolution_cron_enabled"] is True


def test_cancel_active_cycle_endpoint(client):
    tc, _, _ = client
    app = tc.app

    # Case 1: No active cycle task running
    app.state.active_cycle_task = None
    resp = tc.post("/api/cycles/cancel")
    assert resp.status_code == 400
    assert "No active trading cycle is currently running." in resp.json()["detail"]

    # Case 2: Active cycle task running
    mock_task = MagicMock()
    app.state.active_cycle_task = mock_task

    resp = tc.post("/api/cycles/cancel")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "cancelled",
        "message": "Trading cycle task cancellation initiated.",
    }
    mock_task.cancel.assert_called_once()


def test_reviews_endpoints(client):
    tc, config, _ = client
    reviews_dir = config.data_dir / "evolution" / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)

    # 1. Create a dummy code review file
    review_id = "20260625_120000_test_review_title"
    review_file = reviews_dir / f"{review_id}.md"
    review_content = (
        "# Code Review: 20260625_120000_test_review_title\n\n"
        "**Generated**: 2026-06-25T12:00:00.000000+00:00\n"
        "**Status**: PENDING_REVIEW\n\n"
        "## Files Reviewed\n"
        "- `src/evotrader/mcp/robinhood.py`\n"
    )
    review_file.write_text(review_content)

    # 2. Test list reviews
    resp = tc.get("/api/reviews")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["reviews"]) >= 1
    review_item = next(r for r in data["reviews"] if r["id"] == review_id)
    assert review_item["title"] == "20260625_120000_test_review_title"
    assert review_item["status"] == "PENDING_REVIEW"
    assert review_item["generated"] == "2026-06-25T12:00:00.000000+00:00"

    # 3. Test get specific review
    resp = tc.get(f"/api/reviews/{review_id}")
    assert resp.status_code == 200
    details = resp.json()
    assert details["id"] == review_id
    assert details["status"] == "PENDING_REVIEW"
    assert "## Files Reviewed" in details["content"]

    # 4. Test apply code review
    resp = tc.post(f"/api/reviews/{review_id}/apply")
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"

    # Check the file content has been updated
    updated_content = review_file.read_text()
    assert "**Status**: APPLIED" in updated_content

    # Clean up review file
    review_file.unlink()


def test_portfolio_metrics_endpoint(client, mock_app_dependencies):
    tc, _, _ = client
    _db, journal, metrics, _mcp_toolset, _config, _runner_fn, _memory = mock_app_dependencies

    from evotrader.models.portfolio import DailyMetrics

    metrics.get_latest_metrics = AsyncMock(
        return_value=[
            DailyMetrics(
                date="2026-06-25",
                portfolio_value=10500.0,
                cash_balance=500.0,
                positions_value=10000.0,
                daily_pnl=500.0,
                daily_return_pct=0.05,
                cumulative_return=0.05,
                active_positions={},
            )
        ]
    )
    journal.get_first_trade_timestamp = AsyncMock(return_value="2026-06-25T08:00:00")
    journal.get_realized_pnl_history = AsyncMock(return_value=[])

    resp = tc.get("/api/metrics/portfolio")
    assert resp.status_code == 200
    data = resp.json()
    assert "history" in data
    assert "source" in data
    assert len(data["history"]) >= 1


def test_portfolio_metrics_fallback_dict(client, mock_app_dependencies):
    """Portfolio metrics reconstructs history from trades + deposits."""
    tc, _, _ = client
    _db, journal, metrics, _mcp_toolset, _config, _runner_fn, _memory = mock_app_dependencies

    # Mock journal methods
    journal.get_first_trade_timestamp = AsyncMock(return_value="2026-06-24T08:00:00")
    journal.get_realized_pnl_history = AsyncMock(
        return_value=[
            {"timestamp": "2026-06-25T08:05:00", "realized_pnl": 150.0},
            {"timestamp": "2026-06-26T08:10:00", "realized_pnl": -50.0},
        ]
    )
    metrics.get_cash_adjustments = AsyncMock(return_value=[])

    resp = tc.get("/api/metrics/portfolio")
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "broker_snapshots"
    history = data["history"]
    assert len(history) >= 3
    # All deposits = $0, so portfolio_value = 0 + cumulative P&L
    assert history[0]["portfolio_value"] == 0.0  # 06/24: no P&L yet
    day_25 = next(h for h in history if h["date"] == "2026-06-25")
    assert day_25["portfolio_value"] == 150.0
    day_26 = next(h for h in history if h["date"] == "2026-06-26")
    assert day_26["portfolio_value"] == 100.0


def test_portfolio_metrics_fallback_tuple(client, mock_app_dependencies):
    """Portfolio metrics returns empty when no trades or adjustments exist."""
    tc, _, _ = client
    _db, journal, metrics, _mcp_toolset, _config, _runner_fn, _memory = mock_app_dependencies

    journal.get_first_trade_timestamp = AsyncMock(return_value=None)
    journal.get_realized_pnl_history = AsyncMock(return_value=[])
    metrics.get_cash_adjustments = AsyncMock(return_value=[])

    resp = tc.get("/api/metrics/portfolio")
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "empty"
    assert data["history"] == []
    assert data["starting_value"] == 0.0
