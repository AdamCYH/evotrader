"""Scenario definition, prompt rendering, and rule checking."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class RuleCheck:
    """One instruction rule, and how to detect a violation in a response.

    ``forbid`` and ``require`` are regexes applied to the agent's response
    text, case-insensitively. A rule passes when no ``forbid`` pattern matches
    and every ``require`` pattern does.

    ``applies_when`` gates the rule on the scenario's ``facts`` so a single
    rule set can cover scenarios where it is and is not relevant.
    """

    name: str
    description: str
    forbid: list[str] = field(default_factory=list)
    require: list[str] = field(default_factory=list)
    applies_when: dict[str, Any] = field(default_factory=dict)
    severity: str = "error"  # "error" | "warning"

    def applicable(self, facts: dict[str, Any]) -> bool:
        return all(facts.get(k) == v for k, v in self.applies_when.items())

    def evaluate(self, response: str) -> tuple[bool, str]:
        """Return (passed, detail)."""
        text = response or ""
        for pat in self.forbid:
            m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
            if m:
                snippet = m.group(0)[:90].replace("\n", " ")
                return False, f"forbidden pattern matched: /{pat}/ → “{snippet}”"
        for pat in self.require:
            if not re.search(pat, text, re.IGNORECASE | re.DOTALL):
                return False, f"required pattern absent: /{pat}/"
        return True, "ok"


@dataclass
class Scenario:
    """Market state plus the behaviour the instructions demand of it."""

    name: str
    description: str
    market: str
    facts: dict[str, Any] = field(default_factory=dict)
    rules: list[RuleCheck] = field(default_factory=list)
    notes: str = ""
    # Values for the instruction template's ``{{PLACEHOLDERS}}``. The runner
    # renders the live configuration by default; a scenario built from another
    # instrument's history sets its own (a QQQ snapshot must not be told its
    # bearish vehicle is an inverse MSTR fund).
    placeholders: dict[str, str] = field(default_factory=dict)

    @property
    def applicable_rules(self) -> list[RuleCheck]:
        return [r for r in self.rules if r.applicable(self.facts)]


@dataclass
class ScenarioResult:
    """Outcome of checking one response against one scenario."""

    scenario: str
    passed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str, str]] = field(default_factory=list)  # name, severity, detail

    @property
    def ok(self) -> bool:
        return not any(sev == "error" for _, sev, _ in self.failed)

    @property
    def warnings(self) -> list[tuple[str, str, str]]:
        return [f for f in self.failed if f[1] == "warning"]

    def __str__(self) -> str:
        head = f"{self.scenario}: {'PASS' if self.ok else 'FAIL'}"
        lines = [head, "-" * len(head)]
        for n in self.passed:
            lines.append(f"  [ok]   {n}")
        for name, sev, detail in self.failed:
            tag = "WARN" if sev == "warning" else "FAIL"
            lines.append(f"  [{tag}] {name} — {detail}")
        return "\n".join(lines)


def load_scenario(path: Path) -> Scenario:
    """Load one scenario from YAML."""
    raw = yaml.safe_load(Path(path).read_text()) or {}
    missing = {"name", "market"} - set(raw)
    if missing:
        raise ValueError(f"{path}: scenario missing required field(s): {sorted(missing)}")
    return Scenario(
        name=raw["name"],
        description=raw.get("description", ""),
        market=raw["market"],
        facts=raw.get("facts", {}) or {},
        rules=[RuleCheck(**r) for r in (raw.get("rules") or [])],
        notes=raw.get("notes", ""),
        placeholders={str(k): str(v) for k, v in (raw.get("placeholders") or {}).items()},
    )


def load_scenarios(directory: Path) -> list[Scenario]:
    """Load every ``*.yaml`` scenario in a directory, sorted by filename."""
    d = Path(directory)
    if not d.is_dir():
        raise FileNotFoundError(f"scenario directory not found: {d}")
    return [load_scenario(p) for p in sorted(d.glob("*.yaml"))]


def render_prompt(scenario: Scenario, instructions: str) -> str:
    """Build the exact text an agent should be given for this scenario.

    Deterministic: the same scenario and instructions always render the same
    string, so prompt construction itself is testable.
    """
    return (
        "You are the Strategy Agent of an automated trading system. "
        "Act exactly as that agent would.\n\n"
        "=== SYSTEM INSTRUCTIONS ===\n"
        f"{instructions.strip()}\n\n"
        "=== CYCLE INPUT ===\n"
        f"{scenario.market.strip()}\n\n"
        "=== YOUR TASK ===\n"
        "Produce your decision for this cycle exactly as your instructions "
        "require: your reasoning, then your final decision (direction, "
        "conviction, size, and protective order if entering), then the exact "
        "JSON you would pass to log_signal_attribution.\n"
    )


def check_response(scenario: Scenario, response: str) -> ScenarioResult:
    """Check one agent response against a scenario's applicable rules."""
    result = ScenarioResult(scenario=scenario.name)
    for rule in scenario.applicable_rules:
        ok, detail = rule.evaluate(response)
        if ok:
            result.passed.append(rule.name)
        else:
            result.failed.append((rule.name, rule.severity, detail))
    if not result.ok:
        logger.warning(
            "Scenario '%s' failed %d rule(s)",
            scenario.name,
            sum(1 for _, s, _ in result.failed if s == "error"),
        )
    return result


@dataclass
class DecisionScore:
    """An agent's decision on a historical case, priced against what happened."""

    scenario: str
    direction: float
    conviction: float
    position_pct: float
    forward_return_pct: float
    pnl_pct: float
    correct: bool | None
    traded: bool

    def __str__(self) -> str:
        if not self.traded:
            return (
                f"{self.scenario:<26} FLAT            "
                f"market {self.forward_return_pct:+6.2f}%   P&L  +0.00%   (declined)"
            )
        side = "LONG " if self.direction > 0 else "SHORT"
        mark = "right" if self.correct else "wrong"
        return (
            f"{self.scenario:<26} {side} {self.position_pct:>5.1%}  "
            f"market {self.forward_return_pct:+6.2f}%   P&L {self.pnl_pct:+6.2f}%   ({mark})"
        )


_DIRECTION_RE = re.compile(
    r'"?final_direction"?\s*[:=]\s*"?(-?[01](?:\.\d+)?|long|short|flat)"?', re.I
)
_CONVICTION_RE = re.compile(r'"?final_conviction"?\s*[:=]\s*([01](?:\.\d+)?)', re.I)
_TRADED_RE = re.compile(r'"?traded"?\s*[:=]\s*(true|false)', re.I)
_SIZE_RE = re.compile(r'"?position_size"?\s*[:=]\s*\$?([\d,]+(?:\.\d+)?)', re.I)


def parse_decision(response: str) -> dict[str, Any]:
    """Extract the decision fields an agent reports in its attribution block.

    Reads the structured log payload rather than the prose, because the prose
    is where an agent hedges and the payload is what the system acts on.
    """
    text = response or ""

    direction = 0.0
    m = _DIRECTION_RE.search(text)
    if m:
        raw = m.group(1).lower()
        words = {"long": 1.0, "short": -1.0, "flat": 0.0}
        direction = words[raw] if raw in words else float(raw)

    conviction = 0.0
    m = _CONVICTION_RE.search(text)
    if m:
        conviction = float(m.group(1))

    traded = False
    m = _TRADED_RE.search(text)
    if m:
        traded = m.group(1).lower() == "true"

    size = None
    m = _SIZE_RE.search(text)
    if m:
        size = float(m.group(1).replace(",", ""))

    return {
        "direction": direction,
        "conviction": conviction,
        "traded": traded,
        "position_size": size,
    }


def score_decision(
    scenario_name: str,
    response: str,
    forward_return_pct: float,
    capital: float = 5000.0,
) -> DecisionScore:
    """Price one decision against the realised forward return.

    P&L is expressed as a percentage of capital, so declining to trade scores
    exactly 0.00% — which is frequently the right answer and must not be
    penalised as if it were a loss.
    """
    d = parse_decision(response)
    direction, conviction = d["direction"], d["conviction"]
    traded = d["traded"] and abs(direction) > 0 and conviction > 0

    if d["position_size"] and capital > 0:
        position_pct = min(d["position_size"] / capital, 3.0)
    else:
        position_pct = abs(direction) * conviction

    pnl = position_pct * forward_return_pct * (1.0 if direction >= 0 else -1.0) if traded else 0.0
    correct = None if not traded else (direction * forward_return_pct > 0)

    return DecisionScore(
        scenario=scenario_name,
        direction=direction,
        conviction=conviction,
        position_pct=position_pct if traded else 0.0,
        forward_return_pct=forward_return_pct,
        pnl_pct=pnl,
        correct=correct,
        traded=traded,
    )


def load_outcome(path: Path) -> float | None:
    """Read the hidden forward return from a generated scenario file."""
    raw = yaml.safe_load(Path(path).read_text()) or {}
    outcome = raw.get("outcome") or {}
    val = outcome.get("forward_return_pct")
    return float(val) if val is not None else None
