"""Runs the console's JavaScript display-rule tests under Node.

The market panel's rules — what counts as a vote, what a quiet channel says,
which thresholds label the lean, which clock the chart uses — live in
web/static/js/components/signal_display.js. Skipped when Node is not installed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_TESTS = _ROOT / "tests" / "js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_signal_display_rules() -> None:
    files = sorted(str(p) for p in _TESTS.glob("*.test.mjs"))
    assert files, "no JavaScript tests found"
    run = subprocess.run(
        ["node", "--test", *files],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-2000:]
