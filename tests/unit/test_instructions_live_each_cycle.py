"""An activated instruction version is live from the next cycle, and the record says which ran.

The agents were built once, at start-up, with the text of the versions active
then. Activating a version (the Web Console, the evolution agent) only rewrote
``active.txt``, so the running process kept the old text until a restart while
the console reported the new version active. Once a strategy and an executor
version activated during the day ran on none of that day's cycles, and nothing
recorded which version a cycle had run on. ``refresh_instructions`` re-reads
every agent's active version at the start of each cycle; the cycle's runtime
row, each attribution row, the handoff and each stored learning carry the
version.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest

from evotrader.agents import factory
from evotrader.config import AppConfig


@pytest.fixture(autouse=True)
def _claude_code_installed(monkeypatch) -> None:
    monkeypatch.setattr(
        "evotrader.agents.cli.claude_code.ClaudeCodeBackend.is_available",
        classmethod(lambda cls: (True, "ok")),
    )


def _activate(config: AppConfig, folder: str, version: str, text: str) -> None:
    agent_dir = config.instructions_dir / folder
    (agent_dir / f"{version}.md").write_text(text)
    (agent_dir / "active.txt").write_text(version)


def _agent(root: Any, name: str) -> Any:
    return next(a for a in factory._pipeline_agents(root) if getattr(a, "name", "") == name)


def _all_named(root: Any, name: str) -> list[Any]:
    return [a for a in factory._pipeline_agents(root) if getattr(a, "name", "") == name]


class TestAnActivationReachesTheRunningAgents:
    def test_the_api_strategy_agent_takes_the_new_text(self) -> None:
        config = AppConfig()
        orchestrator = factory.create_orchestrator_agent(config)
        old = factory._LOADED_VERSIONS["strategy"]
        _activate(config, "strategy", "v999", "You are the strategy agent, version 999.")

        versions, changed = factory.refresh_instructions(orchestrator, config)

        strategy = _agent(orchestrator, "strategy")
        assert "version 999" in strategy.static_instruction
        assert versions["strategy"] == "v999"
        assert changed == {"strategy": f"{old} -> v999"}

    def test_a_second_refresh_changes_nothing(self) -> None:
        config = AppConfig()
        orchestrator = factory.create_orchestrator_agent(config)
        _activate(config, "strategy", "v999", "You are the strategy agent, version 999.")
        factory.refresh_instructions(orchestrator, config)
        before = _agent(orchestrator, "strategy").static_instruction

        _, changed = factory.refresh_instructions(orchestrator, config)

        assert changed == {}
        assert _agent(orchestrator, "strategy").static_instruction is before

    def test_the_hosted_strategy_agent_and_its_fallback_both_take_it(self) -> None:
        config = AppConfig()
        config.settings.agent_runtime["strategy"] = "claude_code"
        orchestrator = factory.create_orchestrator_agent(config)
        _activate(config, "strategy", "v999", "You are the strategy agent, version 999.")

        factory.refresh_instructions(orchestrator, config)

        hosted, fallback = sorted(
            _all_named(orchestrator, "strategy"), key=lambda a: type(a).__name__
        )
        assert type(hosted).__name__ == "CliBackedAgent"
        assert "version 999" in hosted.instruction_text
        assert "version 999" in fallback.static_instruction

    def test_the_executor_folder_feeds_the_execution_agent(self) -> None:
        config = AppConfig()
        orchestrator = factory.create_orchestrator_agent(config)
        _activate(config, "executor", "v999", "You are the executor, version 999.")

        versions, _ = factory.refresh_instructions(orchestrator, config)

        assert "version 999" in _agent(orchestrator, "execution").static_instruction
        assert versions["executor"] == "v999"

    def test_the_risk_manager_keeps_its_constitution(self) -> None:
        config = AppConfig()
        orchestrator = factory.create_orchestrator_agent(config)
        _activate(config, "risk_manager", "v999", "You are the risk manager, version 999.")

        factory.refresh_instructions(orchestrator, config)

        text = _agent(orchestrator, "risk_manager").instruction
        assert text.startswith("You are the risk manager, version 999.")
        assert "## Active Constitution (INVIOLABLE)" in text

    def test_every_trading_agent_is_reported(self) -> None:
        config = AppConfig()
        orchestrator = factory.create_orchestrator_agent(config)
        versions, _ = factory.refresh_instructions(orchestrator, config)
        assert set(versions) == {
            "orchestrator",
            "news_sentiment",
            "strategy",
            "risk_manager",
            "executor",
        }


def test_the_cycle_refreshes_before_its_agents_run() -> None:
    from evotrader import main

    src = inspect.getsource(main)
    block = src[src.index("async def run_cycle_task") :]
    assert block.index("refresh_instructions(orchestrator, config)") < block.index(
        "run_with_one_resume("
    )
    assert "set_cycle_instruction_versions(None)" in block
    tree = ast.parse(src)
    called = {
        getattr(n.func, "id", getattr(n.func, "attr", ""))
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
    }
    assert "set_cycle_instruction_versions" in called


def test_an_activation_says_when_it_is_live(tmp_path: Path) -> None:
    from evotrader.evolution.instruction_evolver import InstructionEvolver

    (tmp_path / "strategy").mkdir()
    (tmp_path / "strategy" / "v001.md").write_text("one")
    (tmp_path / "strategy" / "v002.md").write_text("two")
    (tmp_path / "strategy" / "active.txt").write_text("v001")

    result = InstructionEvolver(tmp_path).activate_version("strategy", "v002")

    assert result["live_from"] == "the next trading cycle"


# ── The handoff ───────────────────────────────────────────────────────────


class TestTheHandoffSaysWhichVersionWroteIt:
    def test_a_note_from_another_version_is_flagged(self, tmp_path: Path) -> None:
        from evotrader.tools.trading_handoff import render_for_prompt, write_handoff

        write_handoff(tmp_path, "Watch the open.", written_under="v101")

        block = render_for_prompt(tmp_path, active_version="v102")

        assert "Written under strategy v101; you are running v102." in block
        assert "void where it differs from your instructions" in block

    @pytest.mark.parametrize(("written", "active"), [("v102", "v102"), ("", "v102"), ("v101", "")])
    def test_otherwise_it_is_not(self, tmp_path: Path, written: str, active: str) -> None:
        from evotrader.tools.trading_handoff import read_handoff, render_for_prompt, write_handoff

        write_handoff(tmp_path, "Watch the open.", written_under=written)

        assert "Written under strategy" not in render_for_prompt(tmp_path, active_version=active)
        assert read_handoff(tmp_path)["written_under"] == written


# ── Learnings ─────────────────────────────────────────────────────────────


class _Memory:
    def __init__(self) -> None:
        self.stored: list[dict[str, Any]] = []
        self.results: list[dict[str, Any]] = []

    def store_note(self, **kwargs: Any) -> None:
        self.stored.append(kwargs)

    def query_notes(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.results


@pytest.fixture
def tools_on(monkeypatch):
    from evotrader.agents import tools

    memory = _Memory()
    monkeypatch.setattr(tools, "_memory", memory)
    tools.set_cycle_instruction_versions({"strategy": "v102", "executor": "v201"})
    yield tools, memory
    tools.set_cycle_instruction_versions(None)


class TestLearnings:
    def test_a_learning_is_stamped_and_short(self, tools_on) -> None:
        tools, memory = tools_on

        result = tools.store_learning("x" * 2000, category="pattern")

        [stored] = memory.stored
        assert len(stored["text"]) == 800 + len("\n…[truncated]")
        assert stored["metadata"] == {"agent": "strategy", "instruction_version": "v102"}
        assert "Cut to 800 characters" in result["note"]

    def test_a_short_learning_is_kept_whole(self, tools_on) -> None:
        tools, memory = tools_on
        result = tools.store_learning("Dips held at VWAP twice.", category="observation")
        assert memory.stored[0]["text"] == "Dips held at VWAP twice."
        assert "note" not in result

    def test_a_learning_from_another_version_is_read_with_its_stamp(self, tools_on) -> None:
        tools, memory = tools_on
        memory.results = [
            {"document": "old rule", "metadata": {"source": "agent_learning"}},
            {
                "document": "new rule",
                "metadata": {"source": "agent_learning", "instruction_version": "v102"},
            },
            {"document": "owner note", "metadata": {"source": "file"}},
        ]

        old, new, owner = tools.query_user_notes("rules")["results"]

        assert old["written_under"] == "unknown"
        assert "you are running v102" in old["version_note"]
        assert new["written_under"] == "v102" and "version_note" not in new
        assert "written_under" not in owner


# ── The attribution record ────────────────────────────────────────────────


async def test_an_attribution_row_carries_the_version_and_can_be_filtered(db, monkeypatch) -> None:
    import evotrader.evolution.tools as evo
    from evotrader.agents import tools
    from evotrader.db.signal_attribution import SignalAttributionStore

    monkeypatch.setattr(tools, "_db", db)
    tools.set_cycle_instruction_versions({"strategy": "v102"})
    try:
        result = await tools.log_signal_attribution('{"ticker": "XYZ", "final_direction": 1}')
    finally:
        tools.set_cycle_instruction_versions(None)
    async with db.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT strategy_instruction_version FROM signal_attribution WHERE id = ?",
                (result["id"],),
            )
        ).fetchone()
    assert row["strategy_instruction_version"] == "v102"

    store = SignalAttributionStore(db)
    for version in ("v101", "v101", "v101", "v102") * 2:
        await store.record(
            "XYZ",
            final_direction=1,
            llm_direction=1,
            strategy_instruction_version=version,
            timestamp="2026-03-10T15:30:00+00:00",
        )
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE signal_attribution SET forward_return_1d = 1.0, "
            "scored_at = '2026-03-12T00:00:00+00:00' WHERE id > ?",
            (result["id"],),
        )
    monkeypatch.setattr(evo, "_attribution_store", store)

    v101 = await evo.get_signal_calibration(lookback_days=3650, instruction_version="v101")
    every = await evo.get_signal_calibration(lookback_days=3650)

    assert (v101["n_scored"], v101["instruction_version"]) == (6, "v101")
    assert every["n_scored"] == 8
