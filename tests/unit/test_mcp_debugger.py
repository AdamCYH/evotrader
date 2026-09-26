"""Regression tests: the MCP debugger can't touch orders or share a running app's sign-in.

Found 2026-09-26 while tidying the skills: its `call` command ran any tool,
including those that place, change or cancel orders on a real account, with
none of the constitution, the risk gate or the approval step. It also used the
saved broker sign-in while the app might be running, and two programs
refreshing one sign-in can invalidate it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from evotrader import paths


def debugger() -> ModuleType:
    path = paths.project_root() / ".claude/skills/mcp-debugger/scripts/mcp_debugger.py"
    spec = importlib.util.spec_from_file_location("mcp_debugger", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def must_not_connect(_config: object) -> dict:
    pytest.fail("it connected to the broker")


@pytest.mark.parametrize(
    "tool",
    [
        "place_equity_order",
        "place_option_order",
        "cancel_equity_order",
        "cancel_option_order",
        "replace_equity_order",
        "place_trailing_stop_order",
    ],
)
def test_order_tools_are_recognised(tool: str) -> None:
    assert debugger().changes_orders(tool)


@pytest.mark.parametrize(
    "tool", ["get_equity_quotes", "get_option_chains", "get_accounts", "review_equity_order"]
)
def test_read_only_tools_are_not(tool: str) -> None:
    assert not debugger().changes_orders(tool)


async def test_calling_an_order_tool_stops_before_connecting(monkeypatch) -> None:
    module = debugger()
    monkeypatch.setattr(sys, "argv", ["mcp_debugger.py", "call", "place_equity_order", "{}"])
    monkeypatch.setattr(module, "create_mcp_toolsets", must_not_connect)
    with pytest.raises(SystemExit, match="never from this script"):
        await module.main()


async def test_it_does_not_share_a_running_apps_sign_in(monkeypatch, tmp_path: Path) -> None:
    fcntl = pytest.importorskip("fcntl")
    module = debugger()
    signin = tmp_path / "signin"
    signin.mkdir()
    monkeypatch.setenv(paths.SIGNIN_DIR_ENV, str(signin))
    app = open(signin / ".evotrader.lock", "a+")  # noqa: SIM115 - the running app's hold
    try:
        fcntl.flock(app, fcntl.LOCK_EX | fcntl.LOCK_NB)
        app.write("4242\n")
        app.flush()
        monkeypatch.setattr(sys, "argv", ["mcp_debugger.py", "list"])
        monkeypatch.setattr(module, "create_mcp_toolsets", must_not_connect)
        with pytest.raises(SystemExit, match=r"EvoTrader is running \(process 4242\)"):
            await module.main()
    finally:
        app.close()
