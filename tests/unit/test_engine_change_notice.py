"""Regression tests: the next cycle is told when the composite's engine or version changed.

After the 2026-09-24 mean_reversion guard fix, an open lot's entry composite +0.3027
reads +0.3691 on the fixed engine, and the strategy agent's add rule compares
today's composite against the entry one. Nothing told it the ruler had moved,
and the engine fingerprint could not have: it hashed composite.py alone, and
the fix did not touch composite.py.

See: data/evolution/reviews/20260924_224217_mean_reversion_bull_stack_guard_unreachable_strategy_stage_503_skip.md
(operator note 2 of finding 1)
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from evotrader.algorithms import composite
from evotrader.tools.engine_provenance import note_engine
from evotrader.tools.market_hours import ET
from evotrader.tools.trading_handoff import read_handoff, write_handoff

_T0 = datetime(2026, 9, 25, 8, 30, tzinfo=ET)
_T1 = datetime(2026, 9, 25, 9, 30, tzinfo=ET)


class TestFingerprintCoversTheWholeEngine:
    def test_is_twelve_hex_chars(self) -> None:
        fp = composite.COMPOSITE_SOURCE_FINGERPRINT
        assert len(fp) == 12 and all(c in "0123456789abcdef" for c in fp)

    def test_every_strategy_and_indicator_module_is_covered(self) -> None:
        pkg = Path(composite.__file__).resolve().parents[1]
        covered = {
            f.relative_to(pkg).as_posix()
            for rel in composite._ENGINE_SOURCES
            for f in ((pkg / rel).rglob("*.py") if (pkg / rel).is_dir() else [pkg / rel])
        }
        assert "algorithms/strategies/mean_reversion.py" in covered
        assert "indicators/rsi.py" in covered
        assert "algorithms/composite.py" in covered

    def test_a_behaviour_change_moves_the_hash(self) -> None:
        a = composite._logic_dump("def f(x):\n    if x > 0:\n        return 1\n    return 0\n")
        b = composite._logic_dump("def f(x):\n    if x != 0:\n        return 1\n    return 0\n")
        assert a != b

    def test_comments_and_docstrings_do_not(self) -> None:
        a = composite._logic_dump(
            '"""Module."""\ndef f(x):\n    """Doc."""\n    return x  # note\n'
        )
        b = composite._logic_dump(
            '"""Other text."""\ndef f(x):\n    """Rewritten."""\n    # moved\n    return x\n'
        )
        assert a == b


class TestNoteEngine:
    def test_first_sighting_is_not_a_change(self, tmp_path: Path) -> None:
        seen = note_engine(tmp_path, algo_version="composite_v029", fingerprint="aaa", now=_T0)
        assert seen["changed"] is False and seen["notice"] is None
        state = json.loads((tmp_path / "trading" / "engine_provenance.json").read_text())
        assert state["fingerprint"] == "aaa"

    def test_same_engine_is_quiet_and_keeps_its_since(self, tmp_path: Path) -> None:
        note_engine(tmp_path, algo_version="composite_v029", fingerprint="aaa", now=_T0)
        seen = note_engine(tmp_path, algo_version="composite_v029", fingerprint="aaa", now=_T1)
        assert seen["changed"] is False
        assert seen["since"] == _T0.isoformat()

    def test_code_change_is_reported_once(self, tmp_path: Path) -> None:
        note_engine(tmp_path, algo_version="composite_v029", fingerprint="aaa", now=_T0)
        seen = note_engine(tmp_path, algo_version="composite_v029", fingerprint="bbb", now=_T1)
        assert seen["changed"] is True
        assert "signal code aaa → bbb" in seen["notice"]
        assert "algorithm version" not in seen["notice"]
        assert "2026-09-25 09:30 ET" in seen["notice"]
        again = note_engine(tmp_path, algo_version="composite_v029", fingerprint="bbb", now=_T1)
        assert again["changed"] is False

    def test_version_activation_is_reported(self, tmp_path: Path) -> None:
        note_engine(tmp_path, algo_version="composite_v029", fingerprint="aaa", now=_T0)
        seen = note_engine(tmp_path, algo_version="composite_v030", fingerprint="aaa", now=_T1)
        assert seen["changed"] is True
        assert "composite_v029 → composite_v030" in seen["notice"]

    def test_notice_is_a_note_not_an_instruction(self, tmp_path: Path) -> None:
        note_engine(tmp_path, algo_version="v1", fingerprint="aaa", now=_T0)
        notice = note_engine(tmp_path, algo_version="v2", fingerprint="aaa", now=_T1)["notice"]
        for word in ("must", "do not", "never", "should"):
            assert word not in notice.lower()

    def test_corrupt_state_does_not_raise(self, tmp_path: Path) -> None:
        state = tmp_path / "trading" / "engine_provenance.json"
        state.parent.mkdir(parents=True)
        state.write_text("{not json")
        seen = note_engine(tmp_path, algo_version="v1", fingerprint="aaa", now=_T0)
        assert seen["changed"] is False


class TestMarketDataToolWritesTheHandoffLine:
    def _cfg(self, monkeypatch, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from evotrader.agents import tools

        monkeypatch.setattr(tools, "_config", SimpleNamespace(data_dir=tmp_path))

    def test_changed_engine_lands_next_to_the_agents_note(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from evotrader.agents import tools

        self._cfg(monkeypatch, tmp_path)
        write_handoff(tmp_path, "Add-rule note: entry composite was 0.3027.", now=_T0)
        live = {"algo_version": "composite_v029", "composite_source_fingerprint": "aaaaaaaaaaaa"}
        assert tools._note_engine_change(live, _T0) is False  # first sighting

        fixed = {**live, "composite_source_fingerprint": "bbbbbbbbbbbb"}
        assert tools._note_engine_change(fixed, _T1) is True
        body = read_handoff(tmp_path, now=_T1)["body"]
        assert "SYSTEM:" in body and "may not be comparable" in body
        assert "entry composite was 0.3027" in body, "the agent's own note is kept"

        assert tools._note_engine_change(fixed, _T1) is False, "said once, not every cycle"

    def test_failed_composite_is_not_an_engine(self, tmp_path: Path, monkeypatch) -> None:
        from evotrader.agents import tools

        self._cfg(monkeypatch, tmp_path)
        err = {"composite_signal": 0.0, "algo_version": "error", "sub_signals": []}
        assert tools._note_engine_change(err, _T0) is False
        assert not (tmp_path / "trading" / "engine_provenance.json").exists()

    def test_gather_market_data_calls_it_after_the_composite(self) -> None:
        """Wiring check: the step must run inside the market-data tool, after the
        composite is computed, or the first affected cycle is not the one told."""
        import ast
        import inspect

        from evotrader.agents import tools

        tree = ast.parse(inspect.getsource(tools.gather_market_data))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        names = [getattr(c.func, "id", getattr(c.func, "attr", "")) for c in calls]
        assert "_note_engine_change" in names
        note_line = next(
            c.lineno for c in calls if getattr(c.func, "id", "") == "_note_engine_change"
        )
        composite_line = next(
            c.lineno for c in calls if getattr(c.func, "attr", "") == "compute_detailed_signal"
        )
        assert note_line > composite_line
