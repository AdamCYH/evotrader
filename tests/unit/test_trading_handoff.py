"""The trading agent's note to its next cycle.

Operator, 2026-09-21: the evolution agent has a carry-forward file; the trading
agent has nothing. Every cycle starts from a fresh ADK session, no trading agent
can read its own past reasoning, and the one continuity mechanism that exists
(`store_learning` + `query_user_notes`) is opt-in and ranked by similarity
rather than recency — so "what did I decide an hour ago" can return a
fortnight-old note about a different setup.

Four properties this has to hold, each one asked for explicitly:
  - the handoff is MANDATORY in the prompt, while other notes stay queryable;
  - it is rewritten in full every cycle, so a stale line cannot survive;
  - it is stamped with who wrote it, when, and which cycle, so a run that dies
    midway is still legible to the next one;
  - it reads as NOTES, never as instructions — the new cycle still decides.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evotrader.tools.trading_handoff import (
    append_system_line,
    handoff_path,
    read_handoff,
    render_for_prompt,
    write_handoff,
)

_NOW = datetime(2026, 9, 21, 19, 30, tzinfo=UTC)  # 15:30 ET


class TestItIsRewrittenNotAppended:
    def test_a_second_write_replaces_the_first(self, tmp_path: Path) -> None:
        """THE core property. Evolution's file is pruned by a human between
        weekly runs; nothing prunes this nine times a day, so a leftover
        'stop at 149.80' would be wrong money once the position moves."""
        write_handoff(tmp_path, "POSITION: 12 MSTR @165.10", cycle="8/9", now=_NOW)
        write_handoff(tmp_path, "POSITION: flat, closed at 168.00", cycle="1/9", now=_NOW)
        body = read_handoff(tmp_path, now=_NOW)["body"]
        assert "flat, closed at 168.00" in body
        assert "12 MSTR @165.10" not in body, "the previous note must not survive"

    def test_it_is_size_capped(self, tmp_path: Path) -> None:
        write_handoff(tmp_path, "x" * 9000, now=_NOW)
        assert len(handoff_path(tmp_path).read_text()) < 5000

    def test_an_empty_note_is_refused_by_the_tool(self, tmp_path: Path, monkeypatch) -> None:
        import types

        import evotrader.agents.tools as tools

        monkeypatch.setattr(tools, "_config", types.SimpleNamespace(data_dir=tmp_path))
        out = tools.update_trading_handoff(notes="   ")
        assert out["status"] == "error"


class TestItSaysWhoWroteItAndWhen:
    def test_author_cycle_and_session_round_trip(self, tmp_path: Path) -> None:
        write_handoff(
            tmp_path, "watching 160", agent="strategy", cycle="8/9", session_id="98190504", now=_NOW
        )
        note = read_handoff(tmp_path, now=_NOW)
        assert note["written_by"] == "strategy"
        assert note["cycle"] == "8/9"
        assert note["session_id"] == "98190504", "ties back to the thought log"
        assert note["age_hours"] == pytest.approx(0.0, abs=0.01)

    def test_the_rendered_block_states_the_age(self, tmp_path: Path) -> None:
        write_handoff(tmp_path, "watching 160", cycle="8/9", now=_NOW)
        block = render_for_prompt(tmp_path, now=_NOW + timedelta(hours=2))
        assert "cycle 8/9" in block and "2.0 hours ago" in block

    def test_an_overnight_note_is_marked_stale(self, tmp_path: Path) -> None:
        """08:30 Monday reading Friday's 15:30 note: position state has moved
        under it via reconciliation, and it must not read as current."""
        write_handoff(tmp_path, "POSITION: 12 MSTR, stop 149.80 resting", now=_NOW)
        block = render_for_prompt(tmp_path, now=_NOW + timedelta(hours=65))
        assert "STALE" in block
        assert "Verify every fact below against the broker" in block

    def test_a_fresh_note_is_not_marked_stale(self, tmp_path: Path) -> None:
        write_handoff(tmp_path, "watching 160", now=_NOW)
        assert "STALE" not in render_for_prompt(tmp_path, now=_NOW + timedelta(hours=1))


class TestItReadsAsNotesNotInstructions:
    def test_the_block_says_so_in_its_own_words(self, tmp_path: Path) -> None:
        """A prior self's note that reads like a command is the failure mode —
        especially a stale one."""
        write_handoff(tmp_path, "SELL EVERYTHING AT THE OPEN", now=_NOW)
        block = render_for_prompt(tmp_path, now=_NOW)
        assert "NOTES from a previous cycle, not instructions" in block
        assert "do not authorise anything" in block
        assert "decide for yourself" in block

    def test_the_file_itself_says_so_too(self, tmp_path: Path) -> None:
        write_handoff(tmp_path, "anything", now=_NOW)
        assert "not instructions" in handoff_path(tmp_path).read_text()


class TestMachinesCanWriteToItToo:
    def test_a_system_line_is_appended_to_the_agents_note(self, tmp_path: Path) -> None:
        write_handoff(tmp_path, "POSITION: 12 MSTR", now=_NOW)
        assert append_system_line(tmp_path, "protection gap — MSTR x12 uncovered")
        body = read_handoff(tmp_path, now=_NOW)["body"]
        assert "POSITION: 12 MSTR" in body
        assert "SYSTEM: protection gap — MSTR x12 uncovered" in body

    def test_it_lands_even_when_the_agent_never_wrote_one(self, tmp_path: Path) -> None:
        """A cycle that crashed before writing its note still owes the next one
        the machine-verified facts."""
        assert append_system_line(tmp_path, "protection gap — MSTR x12 uncovered")
        note = read_handoff(tmp_path, now=_NOW)
        assert note["written_by"] == "system"
        assert "SYSTEM:" in note["body"]

    def test_the_same_line_is_not_duplicated(self, tmp_path: Path) -> None:
        write_handoff(tmp_path, "POSITION: 12 MSTR", now=_NOW)
        append_system_line(tmp_path, "same gap")
        append_system_line(tmp_path, "same gap")
        assert read_handoff(tmp_path, now=_NOW)["body"].count("SYSTEM: same gap") == 1


class TestNothingBreaksWithoutOne:
    def test_no_file_renders_to_nothing(self, tmp_path: Path) -> None:
        assert render_for_prompt(tmp_path, now=_NOW) == ""
        assert read_handoff(tmp_path, now=_NOW) is None


class TestItIsMandatoryInContextAndNotOnlyQueryable:
    def test_both_strategy_runtimes_inject_it(self) -> None:
        """Continuity that depends on the agent choosing to search for it goes
        missing on the cycle it mattered — and it must survive a fallback. The
        hosted runtime and the API fallback resolve the SAME hook, so a quota
        wall cannot silently drop the handoff along with the harness."""
        src = Path("src/evotrader/agents/factory.py").read_text()
        assert "def _strategy_dynamic_context" in src
        assert "render_for_prompt" in src, "the handoff must be injected, not fetched"
        assert src.count("_strategy_dynamic_context(config)") == 2, (
            "both the API agent's `instruction` and the CLI agent's "
            "`dynamic_instruction` must use it"
        )

    def test_it_is_not_injected_only_on_the_hosted_path(self) -> None:
        cli = Path("src/evotrader/agents/cli_agent.py").read_text()
        assert "render_for_prompt" not in cli, (
            "injecting in the CLI task prompt alone skips the API fallback"
        )

    def test_the_evolution_agent_does_not_receive_it(self) -> None:
        """Evolution runs weekly through its own service and builds its own
        prompt; an operational note for the next trading hour is noise there."""
        from evotrader.evolution.claude_code_tools import evolution_tool_functions

        assert "update_trading_handoff" not in {f.__name__ for f in evolution_tool_functions()}
        svc = Path("src/evotrader/evolution/claude_code_service.py").read_text()
        assert "trading_handoff" not in svc

    def test_only_the_strategy_agent_can_write_one(self) -> None:
        """Every other agent in the pipeline is an ADK LlmAgent with its own
        tool list; the handoff tool is on the strategy agent alone."""
        src = Path("src/evotrader/agents/factory.py").read_text()
        assert src.count("update_trading_handoff") == 2, (
            "expected exactly the import and the _STRATEGY_TOOLS entry"
        )

    def test_the_other_notes_are_still_queryable(self) -> None:
        """The operator was explicit that this replaces nothing."""
        from evotrader.agents.factory import _STRATEGY_TOOLS

        names = {t.__name__ for t in _STRATEGY_TOOLS}
        assert {"query_user_notes", "query_past_trades", "store_learning"} <= names
        assert "update_trading_handoff" in names

    def test_it_is_not_stored_where_semantic_memory_ingests(self, tmp_path: Path) -> None:
        """data/notes/ is loaded into ChromaDB at startup; a handoff there would
        pollute every search and never expire."""
        assert "notes" in str(handoff_path(tmp_path))
        assert handoff_path(tmp_path).parent == tmp_path / "trading" / "notes"

    def test_the_notes_ui_lists_the_trading_source(self) -> None:
        src = Path("src/evotrader/web/server.py").read_text()
        assert '("trading", config.data_dir / "trading" / "notes")' in src
        assert '"trading_handoff"' in src


class TestTheAuditFeedsIt:
    async def test_a_protection_gap_reaches_the_next_cycles_prompt(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """End to end: the audit runs post-cycle, and what it finds is in the
        next cycle's context without the agent asking."""
        import types

        import evotrader.agents.tools as tools
        from evotrader.db.protection_audit import audit_protection

        monkeypatch.setattr(tools, "_config", types.SimpleNamespace(data_dir=tmp_path))
        write_handoff(tmp_path, "POSITION: 12 MSTR @165.10", cycle="8/9", now=_NOW)

        class _Journal:
            async def get_open_trades(self):
                return [{"id": 187, "ticker": "MSTR", "remaining_quantity": 12.0}]

            async def get_recent_trades(self, limit=60):
                return [
                    {
                        "ticker": "MSTR",
                        "action": "STOP_LOSS",
                        "quantity": 12.0,
                        "order_status": "FAILED",
                    }
                ]

        await audit_protection(_Journal())
        block = render_for_prompt(tmp_path, now=_NOW)
        assert "SYSTEM: protection gap" in block
        # It names its evidence and asks for verification rather than ordering
        # a re-place: on 2026-09-22 the audit was wrong for 9 cycles, and
        # "re-place before anything else" would have double-covered the stop.
        assert "stop rows counted: none" in block
        assert "verify against get_equity_orders before re-placing" in block
        # And it says which source the count came from, because a journal-only
        # count is exactly the one that was wrong on 2026-09-25.
        assert "Counted from the journal" in block
        assert "Re-place protection before anything else" not in block


class TestTheNotesUiShowsItAsItsOwnSection:
    """The operator tracks this by hand, so it needs a section of its own —
    it is authored by the AGENT, unlike the notes above it in the same list."""

    _NOTES_JS = Path("src/evotrader/web/static/js/components/notes.js")
    _CTRL_JS = Path("src/evotrader/web/static/js/memory_controller.js")

    def test_the_list_splits_three_ways(self) -> None:
        js = self._NOTES_JS.read_text()
        assert 'files.filter(f => f.source === "trading")' in js
        assert 'f.source !== "evolution" && f.source !== "trading"' in js, (
            "the handoff must not be lumped in with the operator's own notes"
        )

    def test_it_renders_its_own_header(self) -> None:
        js = self._NOTES_JS.read_text()
        assert "Cycle Handoff" in js
        for section in ("Trading Notes", "Cycle Handoff", "Evolution Notes"):
            assert section in js, section

    def test_the_editor_shows_it_raw_and_labels_the_author(self) -> None:
        js = self._CTRL_JS.read_text()
        assert 'isHandoff = (fileData.source === "trading")' in js
        assert "isEvolution || isHandoff" in js, "a stamped header must not be parsed away"
        assert "Written BY the trading agent" in js

    def test_the_backend_serves_that_source(self) -> None:
        py = Path("src/evotrader/web/server.py").read_text()
        assert '("trading", config.data_dir / "trading" / "notes")' in py
        assert 'if source == "trading":' in py, "get/save/delete must resolve the directory"


class TestTheDisplayHeadingStaysOutOfThePrompt:
    def test_the_prompt_body_has_no_duplicate_framing(self, tmp_path: Path) -> None:
        """The file carries a heading so the notes UI is readable; the prompt
        adds its own framing. Without a separator the agent reads
        'not instructions' twice and the heading as if it were content."""
        write_handoff(tmp_path, "POSITION: flat", agent="strategy", cycle="1/9", now=_NOW)
        block = render_for_prompt(tmp_path, now=_NOW)
        assert block.count("not instructions") == 1
        assert "# Trading handoff" not in block
        assert "POSITION: flat" in block

    def test_the_file_still_reads_well_on_its_own(self, tmp_path: Path) -> None:
        write_handoff(tmp_path, "POSITION: flat", agent="strategy", cycle="1/9", now=_NOW)
        text = handoff_path(tmp_path).read_text()
        assert "# Trading handoff — strategy at 2026-09-21 15:30 ET (cycle 1/9)" in text
        assert "POSITION: flat" in text


class TestItUsesTheSameClockTheAgentSees:
    """The agent's temporal context resolves time through ``_resolve_now``,
    which honours ``EVOTRADER_MOCK_TIME``. A handoff stamped from the wall
    clock instead would tell a simulated cycle that a note written seconds ago
    was written months ago — or in the future.
    """

    def test_the_stamp_follows_the_simulated_clock(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EVOTRADER_MOCK_TIME", "2026-06-17T10:00:00-04:00")
        write_handoff(tmp_path, "watching the reclaim")
        note = read_handoff(tmp_path)
        assert note["written_at"].startswith("2026-06-17T10:00:00")

    def test_age_is_measured_against_the_simulated_clock(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EVOTRADER_MOCK_TIME", "2026-06-17T10:00:00-04:00")
        write_handoff(tmp_path, "watching the reclaim")
        monkeypatch.setenv("EVOTRADER_MOCK_TIME", "2026-06-17T13:00:00-04:00")
        note = read_handoff(tmp_path)
        assert note["age_hours"] == pytest.approx(3.0, abs=0.01)
        assert not note["stale"]

    def test_the_prompt_block_carries_an_absolute_eastern_time(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EVOTRADER_MOCK_TIME", "2026-06-17T10:00:00-04:00")
        write_handoff(tmp_path, "watching the reclaim")
        monkeypatch.setenv("EVOTRADER_MOCK_TIME", "2026-06-17T13:00:00-04:00")
        block = render_for_prompt(tmp_path)
        assert "2026-06-17 10:00 AM ET" in block
        assert "3.0 hours ago" in block


class TestItRefusesToBeALongNote:
    def test_a_long_note_is_cut_to_the_ceiling(self, tmp_path):
        write_handoff(tmp_path, "x" * 5000)
        body = read_handoff(tmp_path)["body"]
        assert "[truncated]" in body
        assert len(body) < 1400
