#!/usr/bin/env python3
"""Preflight check for running agents on Claude Code (a Claude subscription).

Verifies every prerequisite for the agents ``agent_runtime`` in settings.yaml
puts on a Claude Code runtime (the strategy and evolution agents can be) and prints
the exact command to fix whatever is missing. Safe to run repeatedly — it only
inspects, never changes anything.

Usage::

    uv run python scripts/setup_claude_code.py

Full documentation: docs/claude_code_evolution.md
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent

OK = "✓"
FAIL = "✗"
WARN = "⚠"


class Check:
    """One preflight check and its outcome."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.status = FAIL
        self.detail = ""
        self.fix: list[str] = []

    def passed(self, detail: str = "") -> Check:
        self.status, self.detail = OK, detail
        return self

    def warned(self, detail: str, *fix: str) -> Check:
        self.status, self.detail, self.fix = WARN, detail, list(fix)
        return self

    def failed(self, detail: str, *fix: str) -> Check:
        self.status, self.detail, self.fix = FAIL, detail, list(fix)
        return self


def check_sdk() -> Check:
    check = Check("claude-agent-sdk (Python)")
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return check.failed(
            "not installed",
            "uv sync --extra claude-code       # or: uv sync --all-extras",
        )

    version = "unknown"
    try:
        from importlib import metadata

        version = metadata.version("claude-agent-sdk")
    except Exception:
        pass
    return check.passed(f"v{version}")


def check_cli() -> Check:
    check = Check("claude CLI")
    path = shutil.which("claude")
    if path is None:
        return check.failed(
            "not on PATH",
            "npm install -g @anthropic-ai/claude-code",
        )
    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        version = (result.stdout or result.stderr).strip().splitlines()[0]
    except Exception as exc:  # pragma: no cover - environment dependent
        return check.warned(f"found at {path} but `--version` failed: {exc}")
    return check.passed(f"{version} ({path})")


def check_node() -> Check:
    """Claude Code needs Node 18+; an old nvm default is a common snag."""
    check = Check("Node.js (>= 18)")
    node = shutil.which("node")
    if node is None:
        return check.warned(
            "not found — needed only to install the CLI via npm",
            "Install Node 18 or newer, then: npm install -g @anthropic-ai/claude-code",
        )
    try:
        result = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
        raw = (result.stdout or "").strip()
        major = int(raw.lstrip("v").split(".")[0])
    except Exception as exc:  # pragma: no cover - environment dependent
        return check.warned(f"found at {node} but version check failed: {exc}")

    if major < 18:
        return check.failed(
            f"{raw} is too old — Claude Code requires Node 18+",
            "nvm install 22 && nvm alias default 22    # if you use nvm",
            "then: npm install -g @anthropic-ai/claude-code",
        )
    return check.passed(raw)


def _claude_runtimes() -> tuple[dict[str, str], dict[str, object]] | None:
    """(agent → runtime name, runtime name → config) for the Claude Code runtimes in use."""
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))
    from evotrader.config import AppConfig

    settings = AppConfig().settings
    runtimes = {
        name: runtime
        for name, runtime in settings.cli_runtimes.items()
        if getattr(runtime, "driver", "") == "claude_code"
    }
    agents = {agent: name for agent, name in settings.agent_runtime.items() if name in runtimes}
    return agents, runtimes


def check_auth() -> Check:
    """Confirm the CLI is logged in, and say which account will be billed.

    With ``billing: "subscription"`` the app withholds ANTHROPIC_API_KEY from
    Claude Code, so a key in the environment is harmless: the run bills the
    subscription or fails loudly. With ``"api"`` or ``"auto"`` the key can be
    used, and billed per token.

    Note this cannot be fully verified from outside the CLI — on macOS the CLI
    stores credentials in the login Keychain, which this script does not read.
    So a missing credentials file is reported as a warning, not a failure.
    """
    check = Check("authentication")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    # Only an actual credentials file counts. ~/.claude/sessions exists from
    # other Claude tooling and is not evidence that the CLI is logged in.
    credentials_file = next(
        (
            path
            for path in (
                Path.home() / ".claude" / ".credentials.json",
                Path.home() / ".claude" / "credentials.json",
            )
            if path.exists()
        ),
        None,
    )

    try:
        found = _claude_runtimes()
    except Exception:
        found = None
    billed_by_api = sorted(
        name
        for name, runtime in (found[1] if found else {}).items()
        if str(getattr(runtime, "billing", "subscription")) != "subscription"
    )
    if api_key and billed_by_api:
        return check.warned(
            f"ANTHROPIC_API_KEY is set and runtime(s) {', '.join(billed_by_api)} may bill it",
            'Set `billing: "subscription"` on those runtimes in settings.yaml to bill',
            "the subscription only (the key is then withheld from Claude Code).",
        )

    if oauth_token:
        return check.passed("CLAUDE_CODE_OAUTH_TOKEN is set (subscription)")

    if credentials_file:
        return check.passed(f"CLI login found ({credentials_file.name})")

    return check.warned(
        "no credentials file found — on macOS the CLI may be using the Keychain, "
        "which this script cannot inspect",
        "Confirm with:  claude          # should open a session, not a login prompt",
        "If it asks you to log in, do so once and then /exit.",
        "On a headless host: run `claude setup-token` elsewhere and set",
        "   CLAUDE_CODE_OAUTH_TOKEN=<token> in .env",
    )


def check_settings() -> Check:
    check = Check("agents on Claude Code")
    try:
        found = _claude_runtimes()
    except Exception as exc:
        return check.warned(f"could not load config: {exc}")
    agents, runtimes = found
    if not agents:
        return check.warned(
            "no agent runs on a Claude Code runtime",
            "Run ./run.sh setup (choose Claude, or detailed mode) to put the",
            "strategy and evolution agents on your subscription, or set",
            '`agent_runtime: {strategy: "claude_code", evolution: "claude_code"}`',
            "in settings.yaml; restart the app afterwards.",
        )
    described = ", ".join(
        f"{agent} → {name} (model {getattr(runtimes[name], 'model', '') or 'CLI default'}, "
        f"billing {getattr(runtimes[name], 'billing', '?')})"
        for agent, name in sorted(agents.items())
    )
    return check.passed(described)


def check_tool_bridge() -> Check:
    """Confirm the evolution tools bridge cleanly onto MCP schemas."""
    check = Check("evolution tool bridge")
    try:
        sys.path.insert(0, str(_PROJECT_ROOT / "src"))
        from evotrader.evolution.claude_code_tools import (
            build_input_schema,
            evolution_tool_functions,
        )

        functions = evolution_tool_functions()
        for fn in functions:
            build_input_schema(fn)
    except Exception as exc:
        return check.failed(f"failed to build tool schemas: {exc}")
    return check.passed(f"{len(functions)} tools ready to expose over MCP")


def check_instructions() -> Check:
    check = Check("evolution agent instructions")
    try:
        sys.path.insert(0, str(_PROJECT_ROOT / "src"))
        from evotrader.agents import instructions
        from evotrader.config import AppConfig

        config = AppConfig()
        text = instructions.load(
            "evolution",
            config.instructions_dir,
            is_sim=(config.settings.mode.value == "sim"),
        )
    except Exception as exc:
        return check.failed(
            f"could not load: {exc}",
            "uv run python scripts/init_data.py",
        )
    if not text.strip():
        return check.failed("loaded but empty")
    return check.passed(f"{len(text.splitlines())} lines")


def main() -> int:
    print()
    print("═" * 68)
    print("  EvoTrader — Claude Code preflight (agents on a Claude subscription)")
    print("═" * 68)
    print()

    checks = [
        check_sdk(),
        check_node(),
        check_cli(),
        check_auth(),
        check_instructions(),
        check_tool_bridge(),
        check_settings(),
    ]

    for check in checks:
        detail = f"— {check.detail}" if check.detail else ""
        print(f"  {check.status} {check.label:<32s} {detail}")
        for line in check.fix:
            print(f"        {line}")
    print()

    failures = [c for c in checks if c.status == FAIL]
    warnings = [c for c in checks if c.status == WARN]

    print("═" * 68)
    if failures:
        print(f"  {FAIL} {len(failures)} blocking issue(s). Fix the commands above,")
        print("    then re-run this script.")
        print()
        print("  Until then each agent falls back to its API model (billed per")
        print("  token) rather than failing to run.")
    elif warnings:
        print(f"  {WARN} Ready, with {len(warnings)} thing(s) to look at above.")
    else:
        print(f"  {OK} All checks passed — those agents will run through Claude Code.")
        print()
        print("  Start a cycle (or an evolution run) from the web console and watch")
        print("  the thought stream.")
    print()
    print("  Docs: docs/claude_code_evolution.md")
    print("═" * 68)
    print()

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
