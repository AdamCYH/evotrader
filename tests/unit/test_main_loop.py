from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from evotrader.config import AppConfig


class MockEvent:
    def __init__(self, author="orchestrator", content_text=""):
        self.author = author
        # mock parts
        part = MagicMock()
        part.text = content_text
        content = MagicMock()
        content.parts = [part]
        self.content = content

    def get_function_calls(self):
        return []


class MockRunner:
    def __init__(self, *args, **kwargs):
        pass

    async def run_async(self, *args, **kwargs):
        # Yield 5 events
        for i in range(5):
            yield MockEvent(author="orchestrator", content_text=f"Thought {i}")


@pytest.mark.asyncio
async def test_main_loop_event_limit_termination(tmp_path):
    # Setup a temp data directory
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # Pre-seed instructions and algorithms
    (data_dir / "instructions").mkdir()
    (data_dir / "instructions" / "orchestrator").mkdir()
    (data_dir / "instructions" / "orchestrator" / "active.txt").write_text("v001")
    (data_dir / "instructions" / "orchestrator" / "v001.md").write_text("# Mock Orchestrator")

    (data_dir / "algorithms").mkdir()
    (data_dir / "algorithms" / "active.yaml").write_text("active_version: v001_initial")
    (data_dir / "algorithms" / "registry.yaml").write_text(
        "versions:\n  - version: v001_initial\n    name: Initial\n"
    )

    # Write settings.yaml with max_cycle_events = 3
    settings_yaml = """
mode: sim
schedule:
  max_cycle_events: 3
"""
    (data_dir / "settings.yaml").write_text(settings_yaml)
    (data_dir / "constitution.yaml").write_text("risk_limits:\n  max_order_value_usd: 1000\n")

    config = AppConfig(data_dir=data_dir)
    assert config.settings.schedule.max_cycle_events == 3

    with (
        patch("evotrader.main.AppConfig", return_value=config),
        patch("evotrader.agents.factory.create_mcp_toolsets", return_value={}),
        patch("evotrader.main.create_orchestrator_agent") as mock_create_orch,
        patch("evotrader.main.App"),
        patch("evotrader.main.create_evolution_agent"),
        patch("evotrader.main.Runner", new=MockRunner),
        patch("evotrader.tools.memory.SemanticMemory") as mock_memory,
    ):
        # Mock orchestrator agent
        mock_orch = MagicMock()
        mock_orch.sub_agents = []
        mock_orch.tools = []
        mock_create_orch.return_value = mock_orch

        # Mock memory
        mock_mem = MagicMock()
        mock_mem.load_notes_from_directory.return_value = 0
        mock_mem.prune.return_value = {}
        mock_memory.return_value = mock_mem

        from evotrader.main import start

        with pytest.raises(RuntimeError) as exc_info:
            await start(dashboard=False)

        assert "Systematic termination: Cycle exceeded the maximum allowed event limit" in str(
            exc_info.value
        )
