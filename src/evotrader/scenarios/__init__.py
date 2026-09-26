"""Behavioural testing for agent instructions.

The backtest measures whether the *algorithm* makes money. This measures
whether the *agent* follows its instructions, and — for scenarios generated
from real history — whether following them would have made money.
"""

from evotrader.scenarios.model import (
    DecisionScore,
    RuleCheck,
    Scenario,
    ScenarioResult,
    check_response,
    load_outcome,
    load_scenario,
    load_scenarios,
    parse_decision,
    render_prompt,
    score_decision,
)

__all__ = [
    "DecisionScore",
    "RuleCheck",
    "Scenario",
    "ScenarioResult",
    "check_response",
    "load_outcome",
    "load_scenario",
    "load_scenarios",
    "parse_decision",
    "render_prompt",
    "score_decision",
]
