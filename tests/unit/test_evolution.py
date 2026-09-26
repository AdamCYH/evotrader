"""Tests for the evolution engine — analyser, code evolver, and instruction evolver."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from evotrader.evolution.analyser import PerformanceAnalyser
from evotrader.evolution.code_evolver import CodeEvolver, _is_allowed_import
from evotrader.evolution.instruction_evolver import InstructionEvolver

# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def instructions_dir(tmp_path: Path) -> Path:
    """Temp directory pre-seeded with a v001 instruction file."""
    agent_dir = tmp_path / "strategy"
    agent_dir.mkdir()
    (agent_dir / "v001.md").write_text("You are the Strategy Agent.")
    (agent_dir / "active.txt").write_text("v001")
    return tmp_path


@pytest.fixture
def code_evolver(tmp_path: Path) -> CodeEvolver:
    """Code evolver with temp project layout."""
    project_root = tmp_path / "project"
    src_root = project_root / "src" / "evotrader"
    algo_dir = src_root / "algorithms"
    algo_dir.mkdir(parents=True)

    # Seed a fake strategy file for read tests
    (algo_dir / "momentum.py").write_text(
        "class Momentum:\n    def compute_signal(self, s): pass\n"
    )

    algorithms_dir = project_root / "data" / "algorithms"
    algorithms_dir.mkdir(parents=True)

    return CodeEvolver(project_root, algorithms_dir)


@pytest.fixture
def instruction_evolver(instructions_dir: Path) -> InstructionEvolver:
    return InstructionEvolver(instructions_dir)


def _make_trades(
    *pnls: float,
    regime: str = "range_bound",
    algo_signal: float = 0.5,
    llm_signal: float = 0.3,
) -> list[dict]:
    """Helper to build mock trade dicts."""
    return [
        {
            "realized_pnl": pnl,
            "regime": regime,
            "algo_signal": algo_signal,
            "llm_signal": llm_signal,
        }
        for pnl in pnls
    ]


# ═══════════════════════════════════════════════════════════════════════
# PerformanceAnalyser
# ═══════════════════════════════════════════════════════════════════════


class TestPerformanceAnalyser:
    """Tests for PerformanceAnalyser."""

    @pytest_asyncio.fixture
    async def analyser(self) -> PerformanceAnalyser:
        journal = AsyncMock()
        journal.get_performance_summary.return_value = {
            "win_rate": 0.55,
            "total_trades": 20,
            "total_pnl": 150.0,
        }
        journal.get_recent_trades.return_value = _make_trades(
            10,
            -5,
            20,
            -3,
            15,
            -8,
            12,
            -2,
            7,
            -4,
        )
        metrics = AsyncMock()
        return PerformanceAnalyser(journal, metrics)

    @pytest.mark.asyncio
    async def test_full_analysis_returns_all_sections(self, analyser):
        result = await analyser.full_analysis(lookback_days=30)

        assert "period" in result
        assert result["period"]["lookback_days"] == 30
        assert "overall" in result
        assert "regime_breakdown" in result
        assert "signal_attribution" in result
        assert "streak_analysis" in result
        assert "recommendations" in result

    @pytest.mark.asyncio
    async def test_regime_breakdown(self, analyser):
        result = await analyser.full_analysis()

        rb = result["regime_breakdown"]
        assert "range_bound" in rb
        assert rb["range_bound"]["total_trades"] == 10
        assert rb["range_bound"]["win_rate"] == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_signal_attribution(self, analyser):
        result = await analyser.full_analysis()

        sa = result["signal_attribution"]
        # algo_signal (0.5) > llm_signal (0.3), so all trades are algo_dominant
        assert sa["algo_dominant"]["count"] == 10
        assert sa["llm_dominant"]["count"] == 0

    @pytest.mark.asyncio
    async def test_streak_analysis(self, analyser):
        result = await analyser.full_analysis()
        streaks = result["streak_analysis"]

        assert streaks["max_win_streak"] >= 1
        assert streaks["max_loss_streak"] >= 1
        assert streaks["current_streak_type"] in ("win", "loss")

    @pytest.mark.asyncio
    async def test_recommendations_low_win_rate(self):
        journal = AsyncMock()
        journal.get_performance_summary.return_value = {
            "win_rate": 0.30,
            "trade_count": 20,
        }
        journal.get_recent_trades.return_value = _make_trades(
            -5,
            -3,
            -1,
            10,
            -4,
            -2,
            -6,
            12,
            -3,
            -1,
        )
        metrics = AsyncMock()

        analyser = PerformanceAnalyser(journal, metrics)
        result = await analyser.full_analysis()

        assert any("Win rate" in r for r in result["recommendations"])


# ═══════════════════════════════════════════════════════════════════════
# CodeEvolver — Validation
# ═══════════════════════════════════════════════════════════════════════


class TestCodeEvolverValidation:
    """Tests for strategy code validation."""

    def test_valid_code_passes(self, code_evolver: CodeEvolver):
        code = textwrap.dedent("""\
            import numpy as np
            from evotrader.algorithms.base import TradingAlgorithm

            class MyStrategy(TradingAlgorithm):
                @property
                def name(self):
                    return "my_strategy"

                def compute_signal(self, snapshot):
                    return 0.5
        """)
        result = code_evolver.validate_strategy_code(code)
        assert result["valid"] is True
        assert result["errors"] == []

    def test_syntax_error_detected(self, code_evolver: CodeEvolver):
        code = "def broken(\n"
        result = code_evolver.validate_strategy_code(code)
        assert result["valid"] is False
        assert any("Syntax" in e for e in result["errors"])

    def test_forbidden_import_os(self, code_evolver: CodeEvolver):
        code = "import os\nclass X:\n    def compute_signal(self, s): pass\n"
        result = code_evolver.validate_strategy_code(code)
        assert result["valid"] is False
        assert any("Forbidden import" in e for e in result["errors"])

    def test_forbidden_import_subprocess(self, code_evolver: CodeEvolver):
        code = "import subprocess\nclass X:\n    def compute_signal(self, s): pass\n"
        result = code_evolver.validate_strategy_code(code)
        assert result["valid"] is False

    def test_forbidden_pattern_eval(self, code_evolver: CodeEvolver):
        code = textwrap.dedent("""\
            from evotrader.algorithms.base import TradingAlgorithm
            class X(TradingAlgorithm):
                def compute_signal(self, s):
                    return eval("1+1")
        """)
        result = code_evolver.validate_strategy_code(code)
        assert result["valid"] is False
        assert any("Forbidden pattern" in e for e in result["errors"])

    def test_forbidden_pattern_exec(self, code_evolver: CodeEvolver):
        code = "class X:\n    def compute_signal(self, s): exec('pass')\n"
        result = code_evolver.validate_strategy_code(code)
        assert result["valid"] is False

    def test_missing_compute_signal(self, code_evolver: CodeEvolver):
        code = "class X:\n    def run(self): pass\n"
        result = code_evolver.validate_strategy_code(code)
        assert result["valid"] is False
        assert any("compute_signal" in e for e in result["errors"])

    def test_missing_class(self, code_evolver: CodeEvolver):
        code = "def compute_signal(s): return 0\n"
        result = code_evolver.validate_strategy_code(code)
        assert result["valid"] is False
        assert any("No class" in e for e in result["errors"])


class TestAllowedImports:
    """Tests for the import allowlist checker."""

    def test_numpy_allowed(self):
        assert _is_allowed_import("numpy") is True

    def test_numpy_submodule_allowed(self):
        assert _is_allowed_import("numpy.linalg") is True

    def test_evotrader_models_allowed(self):
        assert _is_allowed_import("evotrader.models.market") is True

    def test_evotrader_indicators_submodule_allowed(self):
        assert _is_allowed_import("evotrader.indicators.rsi") is True

    def test_os_not_allowed(self):
        assert _is_allowed_import("os") is False

    def test_sys_not_allowed(self):
        assert _is_allowed_import("sys") is False

    def test_requests_not_allowed(self):
        assert _is_allowed_import("requests") is False


# ═══════════════════════════════════════════════════════════════════════
# CodeEvolver — Reading / Writing
# ═══════════════════════════════════════════════════════════════════════


class TestCodeEvolverReadWrite:
    """Tests for code reading and strategy saving."""

    def test_read_strategy_source(self, code_evolver: CodeEvolver):
        result = code_evolver.read_strategy_source("momentum")
        assert "source" in result
        assert "Momentum" in result["source"]

    def test_read_missing_strategy(self, code_evolver: CodeEvolver):
        result = code_evolver.read_strategy_source("nonexistent")
        assert "error" in result

    def test_read_source_file_security(self, code_evolver: CodeEvolver):
        result = code_evolver.read_source_file("../../etc/passwd")
        assert "error" in result

    def test_save_code_review(self, code_evolver: CodeEvolver):
        review_path = code_evolver.save_code_review(
            review_id="test_review_001",
            files_reviewed=["src/evotrader/indicators/rsi.py"],
            findings=[
                {
                    "severity": "warning",
                    "file": "rsi.py",
                    "issue": "No NaN handling",
                    "suggestion": "Add dropna()",
                }
            ],
            proposed_diffs=[
                {
                    "file": "rsi.py",
                    "description": "Add NaN guard",
                    "diff": "+  series = series.dropna()",
                }
            ],
        )
        assert review_path.is_file()
        content = review_path.read_text()
        assert "PENDING_REVIEW" in content
        assert "NaN handling" in content


# ═══════════════════════════════════════════════════════════════════════
# InstructionEvolver
# ═══════════════════════════════════════════════════════════════════════


class TestInstructionEvolver:
    """Tests for versioned instruction management."""

    def test_get_current_version(self, instruction_evolver: InstructionEvolver):
        assert instruction_evolver.get_current_version("strategy") == "v001"

    def test_get_current_version_defaults_to_v001(self, instruction_evolver):
        assert instruction_evolver.get_current_version("nonexistent") == "v001"

    def test_get_instructions(self, instruction_evolver: InstructionEvolver):
        text = instruction_evolver.get_instructions("strategy")
        assert "Strategy Agent" in text

    def test_get_instructions_missing_raises(self, instruction_evolver):
        with pytest.raises(FileNotFoundError):
            instruction_evolver.get_instructions("nonexistent")

    def test_list_versions(self, instruction_evolver: InstructionEvolver):
        versions = instruction_evolver.list_versions("strategy")
        assert len(versions) == 1
        assert versions[0]["version"] == "v001"
        assert versions[0]["is_active"] is True

    def test_propose_change(self, instruction_evolver: InstructionEvolver):
        result = instruction_evolver.propose_change(
            agent_name="strategy",
            new_instructions="You are the improved Strategy Agent v2. " * 5,
            reasoning="Improve clarity.",
        )
        assert result["status"] == "proposed"
        assert result["new_version"] == "v002"
        assert "diff" in result

    def test_propose_change_protected_agent(self, instruction_evolver):
        result = instruction_evolver.propose_change(
            agent_name="risk_manager",
            new_instructions="Override risk rules." * 5,
            reasoning="Testing",
        )
        assert result["status"] == "rejected"
        assert "protected" in result["reason"].lower()

    def test_propose_change_empty_rejected(self, instruction_evolver):
        result = instruction_evolver.propose_change(
            agent_name="strategy",
            new_instructions="   ",
            reasoning="Testing",
        )
        assert result["status"] == "rejected"

    def test_propose_change_too_short_rejected(self, instruction_evolver):
        result = instruction_evolver.propose_change(
            agent_name="strategy",
            new_instructions="Too short.",
            reasoning="Testing",
        )
        assert result["status"] == "rejected"

    def test_activate_version(self, instruction_evolver: InstructionEvolver):
        # First propose a new version
        instruction_evolver.propose_change(
            agent_name="strategy",
            new_instructions="Upgraded Strategy Agent instructions. " * 5,
            reasoning="Testing activation.",
        )

        # Activate it
        result = instruction_evolver.activate_version("strategy", "v002")
        assert result["status"] == "activated"
        assert instruction_evolver.get_current_version("strategy") == "v002"

    def test_activate_nonexistent_version(self, instruction_evolver):
        result = instruction_evolver.activate_version("strategy", "v999")
        assert result["status"] == "rejected"

    def test_activate_protected_agent(self, instruction_evolver):
        result = instruction_evolver.activate_version("risk_manager", "v001")
        assert result["status"] == "rejected"

    def test_rollback(self, instruction_evolver: InstructionEvolver):
        # Propose and activate v002
        instruction_evolver.propose_change(
            agent_name="strategy",
            new_instructions="V2 instructions for the Strategy Agent. " * 5,
            reasoning="Test rollback.",
        )
        instruction_evolver.activate_version("strategy", "v002")
        assert instruction_evolver.get_current_version("strategy") == "v002"

        # Rollback to v001
        result = instruction_evolver.rollback("strategy", "v001")
        assert result["status"] == "activated"
        assert instruction_evolver.get_current_version("strategy") == "v001"

    def test_next_version_increments(self, instruction_evolver):
        assert instruction_evolver._next_version("strategy") == "v002"
        # Propose another to create v002
        instruction_evolver.propose_change(
            agent_name="strategy",
            new_instructions="V2 strategy instructions content. " * 5,
            reasoning="Test increment.",
        )
        assert instruction_evolver._next_version("strategy") == "v003"

    def test_next_version_new_agent(self, instruction_evolver):
        assert instruction_evolver._next_version("brand_new_agent") == "v001"

    def test_sim_mode_behavior(self, instructions_dir: Path):
        sim_evolver = InstructionEvolver(instructions_dir, is_sim=True)
        assert sim_evolver.get_current_version("strategy") == "v001"

        # Now propose and activate
        res = sim_evolver.propose_change(
            agent_name="strategy",
            new_instructions="V2 instructions in simulation mode. " * 5,
            reasoning="Sim test",
        )
        assert res["new_version"] == "v002"
        sim_evolver.activate_version("strategy", "v002")

        # Now get_current_version returns v002 for sim_evolver
        assert sim_evolver.get_current_version("strategy") == "v002"

        # A live evolver also returns v002 because files are unified!
        live_evolver = InstructionEvolver(instructions_dir, is_sim=False)
        assert live_evolver.get_current_version("strategy") == "v002"

        # Both list the version
        sim_versions = [v["version"] for v in sim_evolver.list_versions("strategy")]
        assert "v002" in sim_versions

        live_versions = [v["version"] for v in live_evolver.list_versions("strategy")]
        assert "v002" in live_versions


# ═══════════════════════════════════════════════════════════════════════
# Instruction Loader (agents/instructions.py)
# ═══════════════════════════════════════════════════════════════════════


class TestInstructionLoader:
    """Tests for the file-based instruction loader module."""

    def test_load_from_disk(self, instructions_dir: Path):
        from evotrader.agents.instructions import load

        text = load("strategy", instructions_dir)
        assert "Strategy Agent" in text

    def test_load_missing_agent_raises(self, instructions_dir: Path):
        from evotrader.agents.instructions import load

        with pytest.raises(FileNotFoundError, match="No instruction directory"):
            load("nonexistent_agent", instructions_dir)

    def test_load_missing_active_txt_raises(self, tmp_path: Path):
        from evotrader.agents.instructions import load

        (tmp_path / "orphan_agent").mkdir()
        with pytest.raises(FileNotFoundError, match=r"No active\.txt pointer"):
            load("orphan_agent", tmp_path)

    def test_load_empty_active_txt_raises(self, tmp_path: Path):
        from evotrader.agents.instructions import load

        agent_dir = tmp_path / "empty_ptr"
        agent_dir.mkdir()
        (agent_dir / "active.txt").write_text("  ")
        with pytest.raises(ValueError, match="empty"):
            load("empty_ptr", tmp_path)

    def test_load_dangling_pointer_raises(self, tmp_path: Path):
        from evotrader.agents.instructions import load

        agent_dir = tmp_path / "dangling"
        agent_dir.mkdir()
        (agent_dir / "active.txt").write_text("v999")
        with pytest.raises(FileNotFoundError, match="v999"):
            load("dangling", tmp_path)

    def test_list_agents(self, instructions_dir: Path):
        from evotrader.agents.instructions import list_agents

        agents = list_agents(instructions_dir)
        assert "strategy" in agents

    def test_get_active_version(self, instructions_dir: Path):
        from evotrader.agents.instructions import get_active_version

        assert get_active_version("strategy", instructions_dir) == "v001"

    def test_load_sim_mode(self, instructions_dir: Path):
        from evotrader.agents.instructions import get_active_version, load

        # Test fallback
        assert load("strategy", instructions_dir, is_sim=True) == "You are the Strategy Agent."
        assert get_active_version("strategy", instructions_dir, is_sim=True) == "v001"

        # Test when active.txt is set
        (instructions_dir / "strategy" / "active.txt").write_text("v002")
        (instructions_dir / "strategy" / "v002.md").write_text("Simulation active instructions.")

        assert load("strategy", instructions_dir, is_sim=True) == "Simulation active instructions."
        assert get_active_version("strategy", instructions_dir, is_sim=True) == "v002"
        # Live loads v002 as well
        assert load("strategy", instructions_dir, is_sim=False) == "Simulation active instructions."
        assert get_active_version("strategy", instructions_dir, is_sim=False) == "v002"


# ═══════════════════════════════════════════════════════════════════════
# Algorithm Registry (algorithms/registry.py)
# ═══════════════════════════════════════════════════════════════════════


class TestAlgorithmRegistry:
    """Tests for AlgorithmRegistry in live and sim modes."""

    def test_live_vs_sim_registry(self, tmp_path: Path):
        from evotrader.algorithms.registry import AlgorithmRegistry

        algo_dir = tmp_path / "algorithms"

        live_reg = AlgorithmRegistry(algo_dir, is_sim=False)
        sim_reg = AlgorithmRegistry(algo_dir, is_sim=True)

        # Initially active version is v001_initial for both
        assert live_reg.get_active_version() == "v001_initial"
        assert sim_reg.get_active_version() == "v001_initial"

        # Save a live evolved version (accessible to both)
        params = {"rsi": 35}
        meta = {"name": "Evolved RSI"}
        live_reg.save_version("v002_rsi", params, meta)

        # Activate it in sim
        sim_reg.set_active_version("v002_rsi")

        # Sim active is now v002_rsi
        assert sim_reg.get_active_version() == "v002_rsi"
        # Live active is also v002_rsi since files are unified
        assert live_reg.get_active_version() == "v002_rsi"

        # Save a version (accessible to both)
        sim_reg.save_version("v003_rebound", params, {"name": "Sim Rebound"})

        # Sim registry lists both versions
        sim_versions = [v["version"] for v in sim_reg.list_versions()]
        assert "v002_rsi" in sim_versions
        assert "v003_rebound" in sim_versions

        # Live registry lists both as well
        live_versions = [v["version"] for v in live_reg.list_versions()]
        assert "v002_rsi" in live_versions
        assert "v003_rebound" in live_versions


class TestEvolutionStatusUpdates:
    """Regression cover for a silently-broken reject flow.

    The web UI rejects proposals via update_status(status="REJECTED"), but
    REJECTED was missing from the schema's CHECK constraint. The write raised,
    update_status swallowed the exception, and the endpoint still returned
    success — so rejections did nothing and proposals piled up in the queue.
    """

    @pytest_asyncio.fixture
    async def store(self, tmp_path):
        from evotrader.db.connection import Database
        from evotrader.db.evolution_log import EvolutionLogStore

        db = Database(tmp_path / "e.db")
        await db.initialize()
        yield EvolutionLogStore(db)
        await db.close()

    async def _propose(self, store, version: str = "v999") -> None:
        await store.insert(
            change_type="ALGORITHM_PARAMS",
            risk_level="LOW",
            target_component="strategy",
            old_version="v998",
            new_version=version,
            reasoning="test",
            status="PROPOSED",
        )

    async def test_rejected_is_an_allowed_status(self, store) -> None:
        await self._propose(store)
        assert await store.update_status("v999", "REJECTED") is True
        row = await store.get_by_version("v999")
        assert row is not None and row["status"] == "REJECTED"

    async def test_active_still_works(self, store) -> None:
        await self._propose(store)
        assert await store.update_status("v999", "ACTIVE") is True
        row = await store.get_by_version("v999")
        assert row is not None and row["status"] == "ACTIVE"

    async def test_unknown_version_reports_failure(self, store) -> None:
        """A no-op update must not report success — that is what hid the bug."""
        assert await store.update_status("does_not_exist", "REJECTED") is False

    async def test_invalid_status_reports_failure(self, store) -> None:
        await self._propose(store)
        assert await store.update_status("v999", "NOT_A_STATUS") is False
        row = await store.get_by_version("v999")
        assert row is not None and row["status"] == "PROPOSED"

    async def test_target_component_filter_is_respected(self, store) -> None:
        await self._propose(store)
        assert (
            await store.update_status("v999", "REJECTED", target_component="risk_manager") is False
        )
        assert await store.update_status("v999", "REJECTED", target_component="strategy") is True
