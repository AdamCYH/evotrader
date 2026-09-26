"""Tests for the agent-instruction scenario harness."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from evotrader.scenarios import (
    RuleCheck,
    Scenario,
    check_response,
    load_scenario,
    load_scenarios,
    render_prompt,
)


def _write(tmp_path: Path, data: dict, name: str = "s.yaml") -> Path:
    p = tmp_path / name
    p.write_text(yaml.safe_dump(data))
    return p


MINIMAL = {"name": "s1", "market": "price: 100"}


class TestLoading:
    def test_loads_minimal_scenario(self, tmp_path: Path) -> None:
        s = load_scenario(_write(tmp_path, MINIMAL))
        assert s.name == "s1"
        assert s.rules == []

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="missing required"):
            load_scenario(_write(tmp_path, {"name": "s1"}))

    def test_loads_directory_sorted(self, tmp_path: Path) -> None:
        _write(tmp_path, {**MINIMAL, "name": "b"}, "02.yaml")
        _write(tmp_path, {**MINIMAL, "name": "a"}, "01.yaml")
        assert [s.name for s in load_scenarios(tmp_path)] == ["a", "b"]

    def test_missing_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_scenarios(tmp_path / "nope")


class TestRuleEvaluation:
    def test_forbid_pattern_fails(self) -> None:
        r = RuleCheck(name="r", description="d", forbid=[r"\bBUY\b"])
        ok, detail = r.evaluate("Decision: BUY 6 shares")
        assert not ok
        assert "forbidden" in detail

    def test_require_pattern_missing_fails(self) -> None:
        r = RuleCheck(name="r", description="d", require=[r"log_signal_attribution"])
        ok, detail = r.evaluate("I have decided to hold.")
        assert not ok
        assert "required" in detail

    def test_clean_response_passes(self) -> None:
        r = RuleCheck(
            name="r", description="d", forbid=[r"\bBUY\b"], require=[r"log_signal_attribution"]
        )
        ok, _ = r.evaluate("Holding flat. Calling log_signal_attribution now.")
        assert ok

    def test_matching_is_case_insensitive(self) -> None:
        r = RuleCheck(name="r", description="d", forbid=[r"\bbuy\b"])
        assert not r.evaluate("BUY now")[0]

    def test_applies_when_gates_the_rule(self) -> None:
        r = RuleCheck(name="r", description="d", applies_when={"in_drawdown": True})
        assert r.applicable({"in_drawdown": True})
        assert not r.applicable({"in_drawdown": False})
        assert not r.applicable({})


class TestCheckResponse:
    def _scenario(self) -> Scenario:
        return Scenario(
            name="s",
            description="",
            market="m",
            facts={"in_drawdown": True},
            rules=[
                RuleCheck(name="hard", description="", forbid=[r"\bSELL\b"], severity="error"),
                RuleCheck(name="soft", description="", require=[r"priced"], severity="warning"),
                RuleCheck(
                    name="skipped",
                    description="",
                    forbid=[r"."],
                    applies_when={"in_drawdown": False},
                ),
            ],
        )

    def test_error_violation_fails_the_run(self) -> None:
        res = check_response(self._scenario(), "I will SELL everything")
        assert not res.ok
        assert any(n == "hard" for n, _, _ in res.failed)

    def test_warning_alone_still_passes(self) -> None:
        res = check_response(self._scenario(), "Holding steady.")
        assert res.ok
        assert len(res.warnings) == 1

    def test_inapplicable_rules_are_skipped(self) -> None:
        res = check_response(self._scenario(), "priced in, holding")
        assert "skipped" not in res.passed
        assert res.ok

    def test_report_renders(self) -> None:
        text = str(check_response(self._scenario(), "SELL"))
        assert "FAIL" in text and "hard" in text


class TestPromptRendering:
    def test_prompt_contains_both_parts(self) -> None:
        s = Scenario(name="s", description="", market="price: 100")
        out = render_prompt(s, "RULE ONE")
        assert "RULE ONE" in out
        assert "price: 100" in out
        assert "log_signal_attribution" in out

    def test_rendering_is_deterministic(self) -> None:
        s = Scenario(name="s", description="", market="m")
        assert render_prompt(s, "i") == render_prompt(s, "i")


class TestShippedScenarios:
    """The scenarios shipped in starter_data/ must stay loadable and well-formed."""

    def test_repo_scenarios_load(self, starter_data_dir: Path) -> None:
        d = starter_data_dir / "scenarios"
        scenarios = load_scenarios(d)
        assert scenarios
        for s in scenarios:
            assert s.market.strip(), f"{s.name} has empty market data"
            assert s.rules, f"{s.name} defines no rules"
            for r in s.rules:
                assert r.forbid or r.require, f"{s.name}/{r.name} checks nothing"
                assert r.severity in ("error", "warning")

    def test_flat_response_does_not_trip_the_no_trade_rule(self, starter_data_dir: Path) -> None:
        """A correctly-flat response must PASS the algorithm-alone rule.

        The forbid pattern once read ``\\d+\\s*shares`` with no lower bound, so the
        phrase "No open positions ... = 0 shares" — written by an agent that
        had correctly declined to trade — matched it. The rule reported a
        violation for perfect compliance, which is worse than no rule at all.
        """
        d = starter_data_dir / "scenarios"
        scenario = next(s for s in load_scenarios(d) if s.name == "algo_only_no_news")
        flat = (
            "No open positions and $5,000 cash: (total_value - cash)/mark = 0 shares.\n"
            "News feed returned empty, so the news witness is silent.\n"
            "FINAL DECISION: NO_TRADE - FLAT. direction 0, conviction 0.0,\n"
            "position_size $0 / 0 shares.\n"
            "log_signal_attribution({...})\n"
        )
        assert check_response(scenario, flat).ok

    def test_real_entry_still_trips_the_no_trade_rule(self, starter_data_dir: Path) -> None:
        """The relaxation must not blind the rule to an actual trade."""
        d = starter_data_dir / "scenarios"
        scenario = next(s for s in load_scenarios(d) if s.name == "algo_only_no_news")
        traded = (
            "News feed returned empty, so the news witness is silent.\n"
            "DECISION: BUY 12 shares of QQQ.\n"
            "log_signal_attribution({...})\n"
        )
        result = check_response(scenario, traded)
        assert not result.ok
        assert any(name == "no_trade_on_algorithm_alone" for name, _, _ in result.failed)

    def test_repo_scenario_patterns_compile(self, starter_data_dir: Path) -> None:
        import re

        d = starter_data_dir / "scenarios"
        for s in load_scenarios(d):
            for r in s.rules:
                for pat in [*r.forbid, *r.require]:
                    re.compile(pat)


class TestDecisionParsing:
    """The attribution payload is what the system acts on — parse that, not prose."""

    def test_parses_long_decision(self) -> None:
        from evotrader.scenarios import parse_decision

        d = parse_decision('"final_direction": "long", "final_conviction": 0.7, "traded": true')
        assert d["direction"] == pytest.approx(1.0)
        assert d["conviction"] == pytest.approx(0.7)
        assert d["traded"] is True

    def test_parses_short_and_numeric_direction(self) -> None:
        from evotrader.scenarios import parse_decision

        assert parse_decision('"final_direction": "short"')["direction"] == pytest.approx(-1.0)
        assert parse_decision('"final_direction": -1')["direction"] == pytest.approx(-1.0)

    def test_flat_is_not_traded(self) -> None:
        from evotrader.scenarios import parse_decision

        d = parse_decision('"final_direction": "flat", "final_conviction": 0.0, "traded": false')
        assert d["direction"] == pytest.approx(0.0)
        assert d["traded"] is False

    def test_parses_position_size_with_commas(self) -> None:
        from evotrader.scenarios import parse_decision

        assert parse_decision('"position_size": 1,435.80')["position_size"] == pytest.approx(
            1435.80
        )

    def test_missing_fields_default_to_flat(self) -> None:
        from evotrader.scenarios import parse_decision

        d = parse_decision("I have decided to wait.")
        assert d["direction"] == pytest.approx(0.0)
        assert d["traded"] is False


class TestDecisionScoring:
    def test_correct_long_earns(self) -> None:
        from evotrader.scenarios import score_decision

        s = score_decision(
            "x", '"final_direction": 1, "final_conviction": 0.5, "traded": true', 4.0
        )
        assert s.correct is True
        assert s.pnl_pct == pytest.approx(2.0)

    def test_wrong_long_loses(self) -> None:
        from evotrader.scenarios import score_decision

        s = score_decision(
            "x", '"final_direction": 1, "final_conviction": 0.5, "traded": true', -4.0
        )
        assert s.correct is False
        assert s.pnl_pct == pytest.approx(-2.0)

    def test_correct_short_earns_on_a_decline(self) -> None:
        from evotrader.scenarios import score_decision

        s = score_decision(
            "x", '"final_direction": -1, "final_conviction": 0.5, "traded": true', -4.0
        )
        assert s.correct is True
        assert s.pnl_pct == pytest.approx(2.0)

    def test_declining_scores_exactly_zero(self) -> None:
        """Standing aside is frequently right and must not be scored as a loss."""
        from evotrader.scenarios import score_decision

        s = score_decision("x", '"final_direction": "flat", "traded": false', -9.0)
        assert s.pnl_pct == pytest.approx(0.0)
        assert s.correct is None
        assert "declined" in str(s)

    def test_position_size_overrides_conviction_for_sizing(self) -> None:
        from evotrader.scenarios import score_decision

        s = score_decision(
            "x",
            '"final_direction": 1, "final_conviction": 0.2, "position_size": 2500, "traded": true',
            10.0,
            capital=5000.0,
        )
        assert s.position_pct == pytest.approx(0.5)
        assert s.pnl_pct == pytest.approx(5.0)


class TestOutcomeLoading:
    def test_reads_hidden_outcome(self, tmp_path: Path) -> None:
        from evotrader.scenarios import load_outcome

        p = _write(tmp_path, {**MINIMAL, "outcome": {"forward_return_pct": -3.25}})
        assert load_outcome(p) == pytest.approx(-3.25)

    def test_missing_outcome_returns_none(self, tmp_path: Path) -> None:
        from evotrader.scenarios import load_outcome

        assert load_outcome(_write(tmp_path, MINIMAL)) is None

    def test_generated_scenarios_never_leak_outcome_into_prompt(
        self, starter_data_dir: Path
    ) -> None:
        """The forward return must never reach the agent."""
        from evotrader.scenarios import load_scenarios, render_prompt

        d = starter_data_dir / "scenarios"
        for s in load_scenarios(d):
            prompt = render_prompt(s, "instructions")
            assert "forward_return" not in prompt
            assert "HIDDEN" not in prompt
