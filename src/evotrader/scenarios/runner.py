"""CLI for scenario-based instruction testing.

Two modes:

``--render`` prints the exact prompt for a scenario, so it can be piped to any
agent runner. ``--check`` scores a saved response against the scenario's rules.

Splitting them keeps the expensive part (an LLM call) outside the test suite
while leaving both prompt construction and rule checking deterministic and
unit-testable.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from evotrader import paths
from evotrader.agents import instructions as instruction_loader
from evotrader.agents.instruction_context import (
    build_instruction_context,
    render_instructions,
)
from evotrader.config import AppConfig
from evotrader.scenarios.model import (
    Scenario,
    check_response,
    load_scenarios,
    render_prompt,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scenarios")


def _instruction_dir() -> Path:
    return paths.data_dir() / "instructions"


def _find(scenarios: list[Scenario], name: str) -> Scenario:
    for s in scenarios:
        if s.name == name:
            return s
    raise SystemExit(
        f"scenario '{name}' not found. Available: {', '.join(s.name for s in scenarios)}"
    )


def _load_instructions(
    agent: str, version: str | None, placeholders: dict[str, str] | None = None
) -> str:
    """Instruction text with its placeholders filled as the live factory fills them.

    The live path renders ``{{PRIMARY_TICKER}}``, ``{{BEARISH_VEHICLE}}``,
    ``{{DIRECTIONS_AVAILABLE}}`` … from configuration. Until 2026-09-18 this
    runner handed the agent the RAW file, so every scenario ran with the
    bearish vehicle and the permitted directions unstated — the agent flagged
    the ``{{...}}`` as a defect and declined every short as unplaceable.

    ``placeholders`` (a scenario's own values) override the live configuration,
    so a snapshot from another instrument's history is not told that its
    bearish vehicle is today's inverse fund.
    """
    if version:
        path = _instruction_dir() / agent / f"{version}.md"
        if not path.is_file():
            raise SystemExit(f"instruction file not found: {path}")
        text = path.read_text()
    else:
        text = instruction_loader.load(agent, _instruction_dir())
    context = build_instruction_context(AppConfig())
    context.update(placeholders or {})
    return render_instructions(text, context)


def main() -> None:
    p = argparse.ArgumentParser(description="Agent instruction scenario tests")
    p.add_argument("--scenario", help="Scenario name. Omit to list them.")
    p.add_argument("--agent", default="strategy", help="Agent whose instructions to load")
    p.add_argument("--version", help="Instruction version (e.g. v013). Default: active.")
    p.add_argument("--dir", default=str(paths.data_dir() / "scenarios"), help="Scenario directory")
    p.add_argument("--render", action="store_true", help="Print the prompt and exit")
    p.add_argument("--check", metavar="FILE", help="Score a saved agent response")
    args = p.parse_args()

    scenarios = load_scenarios(Path(args.dir))

    if not args.scenario:
        print(f"{len(scenarios)} scenario(s) in {args.dir}:\n")
        for s in scenarios:
            print(f"  {s.name:<24} {s.description}")
            for r in s.rules:
                flag = "!" if r.severity == "error" else "~"
                print(f"      {flag} {r.name}: {r.description}")
        return

    scenario = _find(scenarios, args.scenario)

    if args.render:
        print(
            render_prompt(
                scenario, _load_instructions(args.agent, args.version, scenario.placeholders)
            )
        )
        return

    if args.check:
        path = Path(args.check)
        if not path.is_file():
            raise SystemExit(f"response file not found: {path}")
        result = check_response(scenario, path.read_text())
        print(result)
        if result.warnings:
            print(f"\n{len(result.warnings)} warning(s) — review, not necessarily a defect.")
        sys.exit(0 if result.ok else 1)

    raise SystemExit("choose --render or --check FILE")


if __name__ == "__main__":
    main()
