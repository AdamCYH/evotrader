"""The strategy validator must accept the library it guards.

See: data/evolution/reviews/20260917_200126_vwap_zscore_unconditional_countertrend_fade.md
(finding 9)

Measured 2026-09-17: ALL NINE shipped strategies failed `validate_strategy_code`.
The evolution agent hit this filing the vwap_reclaim_continuation proposal and had
to strip every type annotation to get a passing artefact — so the thing that was
validated diverged from the thing that would land. Sixth instance of the standing
rule that a guard must run the path the real system runs.

Causes found, only the first of which the review identified:

* ``__future__`` and ``typing`` are house style and were not on the allowlist.
* ``open\\s*\\(`` has no word boundary, so it matched the tail of
  ``minutes_since_open(`` in the shipped ``gap`` strategy.
* ``setattr(self, f"_{key}", ...)`` is the shipped ``set_parameters`` idiom in
  five strategies, and was a hard error.
* ``evotrader.tools.market_hours`` is a read-only helper and was not allowed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evotrader.evolution.code_evolver import CodeEvolver

_STRATEGY_DIR = Path("src/evotrader/algorithms/strategies")
_STRATEGY_FILES = sorted(f for f in _STRATEGY_DIR.glob("*.py") if f.name != "__init__.py")


def _validator() -> CodeEvolver:
    return CodeEvolver.__new__(CodeEvolver)


def _strategy(body: str = "        return 0.0", header: str = "") -> str:
    """A STRUCTURALLY COMPLETE strategy, so rejection can only be about `body`.

    Written out because minimal snippets are rejected for missing
    `compute_signal` — which would make every "dangerous code is blocked" case
    below pass for the wrong reason, the exact trap this file exists to close.
    """
    return (
        f"{header}"
        "class X:\n"
        "    @property\n"
        "    def name(self): return 'x'\n"
        "    def compute_signal(self, snapshot):\n"
        f"{body}\n"
    )


@pytest.mark.parametrize("path", _STRATEGY_FILES, ids=lambda p: p.name)
def test_every_shipped_strategy_passes_its_own_validator(path: Path) -> None:
    """Fails today on every file, which is why it is the right guard."""
    result = _validator().validate_strategy_code(path.read_text())
    assert result["valid"], f"{path.name}: {result['errors']}"


def test_there_are_strategies_to_check(self=None) -> None:
    """Guard the guard: an empty glob would make the above vacuously green."""
    assert len(_STRATEGY_FILES) >= 8


class TestTheSandboxStillBites:
    """Relaxing the validator must not turn it into a rubber stamp.

    The review proposed inverting the allowlist into a deny-set of
    {os, sys, subprocess, socket, shutil, importlib, ctypes, pickle, pathlib,
    requests}. That was NOT done: it would fix the false positives and silently
    admit urllib, http, asyncio, threading, multiprocessing and tempfile.
    "Unknown means denied" is the property worth keeping for code that is
    written to disk and imported.
    """

    @pytest.mark.parametrize(
        "code",
        [
            _strategy(header="import os\n"),
            _strategy(header="import sys\n"),
            _strategy(header="import subprocess\n"),
            # Not in the deny-set the review proposed — caught by the allowlist.
            _strategy(header="import urllib.request\n"),
            _strategy(header="import asyncio\n"),
            _strategy(header="import tempfile\n"),
            # A near-miss on a denied name must not slip through on a prefix.
            _strategy(header="import osmium\n"),
            _strategy("        return eval('1')"),
            _strategy("        return exec('x=1')"),
            _strategy("        return __import__('os')"),
            _strategy("        return open('/etc/passwd')"),
        ],
        ids=[
            "import-os",
            "import-sys",
            "import-subprocess",
            "import-urllib",
            "import-asyncio",
            "import-tempfile",
            "import-osmium",
            "eval",
            "exec",
            "dunder-import",
            "open",
        ],
    )
    def test_dangerous_code_is_still_rejected(self, code: str) -> None:
        assert _validator().validate_strategy_code(code)["valid"] is False


class TestReflectionIsWarnedNotBlocked:
    """`setattr` is the house set_parameters idiom; blocking it made the
    validator unable to approve conforming code."""

    def test_setattr_is_allowed_but_flagged(self) -> None:
        code = _strategy(
            "        setattr(self, '_a', 1)\n        return 0.0",
            header="from __future__ import annotations\n",
        )
        result = _validator().validate_strategy_code(code)
        assert result["valid"] is True
        assert any("Reflection used" in w for w in result["warnings"])

    def test_house_style_imports_are_clean(self) -> None:
        code = _strategy(header="from __future__ import annotations\nfrom typing import Any\n")
        result = _validator().validate_strategy_code(code)
        assert result["valid"] is True
        assert not any("Forbidden import" in e for e in result["errors"])
