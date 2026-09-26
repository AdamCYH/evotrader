"""Unit tests for evolution proposals and associated tools."""

from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from evotrader.evolution.proposals import ProposalManager, ProposalType
from evotrader.evolution.tools import (
    bind_evolution_dependencies,
    get_strategy_manifest,
    list_proposals,
    propose_composition_change,
    propose_deprecation,
    propose_new_strategy,
)


@pytest.fixture
def temp_proposals_dir(tmp_path: Path) -> Path:
    return tmp_path / "proposals"


def test_proposal_manager_lifecycle(temp_proposals_dir: Path) -> None:
    manager = ProposalManager(temp_proposals_dir)

    frontmatter = {
        "proposal_id": "p_test_123",
        "type": ProposalType.NEW_STRATEGY.value,
        "status": "proposed",
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "target_strategy": "test_strat",
    }
    body = "## Rationale\nThis is a test proposal."

    # Test Create
    path = manager.create_proposal(frontmatter, body)
    assert path.is_file()
    assert path.name == "p_test_123.md"

    # Test Parse
    parsed_frontmatter, parsed_body = manager.parse_proposal(path)
    assert parsed_frontmatter["proposal_id"] == "p_test_123"
    assert parsed_frontmatter["type"] == "new_strategy"
    assert parsed_body == "## Rationale\nThis is a test proposal."

    # Test List
    listed = manager.list_proposals()
    assert len(listed) == 1
    assert listed[0]["proposal_id"] == "p_test_123"

    # Test Update Status
    manager.update_status("p_test_123", "implemented")
    updated_frontmatter, _ = manager.parse_proposal(path)
    assert updated_frontmatter["status"] == "implemented"


@pytest.mark.asyncio
async def test_evolution_proposal_tools(temp_proposals_dir: Path, tmp_path: Path) -> None:
    # Set up mocks
    analyser = MagicMock()
    code_evolver = MagicMock()
    instruction_evolver = MagicMock()
    algo_registry = MagicMock()
    memory = MagicMock()
    config = MagicMock()
    thought_logger = AsyncMock()

    config.data_dir = tmp_path
    config.algorithms_dir = tmp_path / "algorithms"
    config.algorithms_dir.mkdir(parents=True, exist_ok=True)

    # Bind dependencies
    bind_evolution_dependencies(
        analyser=analyser,
        code_evolver=code_evolver,
        instruction_evolver=instruction_evolver,
        algo_registry=algo_registry,
        memory=memory,
        config=config,
        thought_logger=thought_logger,
    )

    # Override internal proposal manager to use temp directory
    from evotrader.evolution import tools

    tools._proposal_manager = ProposalManager(temp_proposals_dir)

    # 1. Test get_strategy_manifest
    manifest_data = {"strategies": {"momentum": {"status": "active"}}}
    with open(config.algorithms_dir / "strategy_manifest.yaml", "w") as f:
        yaml.dump(manifest_data, f)

    manifest = get_strategy_manifest()
    assert "strategies" in manifest
    assert manifest["strategies"]["momentum"]["status"] == "active"

    # 2. Test propose_new_strategy
    new_res = await propose_new_strategy(
        name="vwap_scalper",
        description="Scalper around VWAP",
        signal_logic="def test(): pass",
        indicators=["vwap"],
        params={"threshold": 0.01},
        integration="Take 10% from gap",
    )
    assert new_res["status"] == "success"
    proposal_id = new_res["proposal_id"]

    # 3. Test list_proposals
    proposals_list = list_proposals()
    assert proposals_list["status"] == "success"
    assert len(proposals_list["proposals"]) == 1
    assert proposals_list["proposals"][0]["proposal_id"] == proposal_id

    # 4. Test propose_deprecation
    dep_res = await propose_deprecation(
        strategy_name="gap",
        reasoning="Underperforming",
        redistribute_weight_to={"momentum": 0.5, "mean_reversion": 0.5},
    )
    assert dep_res["status"] == "success"

    # 5. Test propose_composition_change
    comp_res = await propose_composition_change(
        weight_changes={"momentum": 0.6, "mean_reversion": 0.4},
        reasoning="Adapt to trend",
    )
    assert comp_res["status"] == "success"

    # Reset globals
    bind_evolution_dependencies(None, None, None, None, None, None, None)
