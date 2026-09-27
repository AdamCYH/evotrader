"""Code evolver — generates and validates new strategy code.

The Evolution Agent can propose new strategy implementations by:
1. Analysing current performance
2. Reviewing existing strategy source code
3. Generating improved Python code
4. Validating syntax + running tests
5. Writing the new code to a versioned directory

Code is NEVER executed until it passes all safety checks and is
explicitly promoted via the algorithm registry.

Safety layers:
- **Syntax check** — AST parse before writing to disk
- **Import check** — Only whitelisted imports allowed
- **Sandbox test** — Run unit tests against the new code
- **Backtest gate** — Must pass minimum performance criteria
- **Human review** — Infrastructure code always needs approval
"""

from __future__ import annotations

import ast
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Imports that evolved strategy code is allowed to use.
#
# This stays an ALLOWLIST. A review suggested inverting it to a deny-set of
# {os, sys, subprocess, socket, ...}, which would fix the false positives below
# but silently permit urllib, http, asyncio, threading, multiprocessing,
# tempfile and everything else nobody thought to name. This validator gates
# LLM-generated code that is then written to disk and imported, so "unknown
# means denied" is the property worth keeping.
#
# The real defect was narrower: the house style of every shipped strategy was
# not on the list. Measured 2026-09-17 — all NINE shipped strategies failed
# validation, so the validator could not approve the library it guards.
_ALLOWED_IMPORTS = frozenset(
    {
        "math",
        "statistics",
        "collections",
        "functools",
        "numpy",
        "pandas",
        # House style. Every shipped strategy opens with these two.
        "__future__",
        "typing",
        "evotrader.models.market",
        "evotrader.models.signals",
        "evotrader.algorithms.base",
        # Pure arithmetic: thresholds on a price move in ATR units, shared so
        # every strategy applies the same rule (see algorithms/units.py).
        "evotrader.algorithms.units",
        "evotrader.indicators",
        # Read-only session helpers (e.g. market_hours.minutes_since_open), used by
        # the shipped `gap` strategy.
        "evotrader.tools",
    }
)

# Dangerous patterns that must never appear in generated code
# Dual-use reflection. Flagged as warnings, never errors — see the note at the
# call site for why rejecting these made the validator useless.
_REFLECTION_PATTERNS = [
    r"\bgetattr\s*\(",
    r"\bsetattr\s*\(",
]

# NOTE the word boundaries. Without them `open\s*\(` matches the tail of
# `minutes_since_open(` and `import\s+os` matches `import osmium` — the first
# of those was live, rejecting the shipped `gap` strategy for calling a
# read-only market-hours helper.
_FORBIDDEN_PATTERNS = [
    r"\bimport\s+os\b",
    r"\bimport\s+sys\b",
    r"\bimport\s+subprocess\b",
    r"\bimport\s+shutil\b",
    r"__import__",
    r"\beval\s*\(",
    r"\bexec\s*\(",
    r"\bopen\s*\(",
    r"\bcompile\s*\(",
    r"\bdelattr\s*\(",
    r"globals\s*\(",
    r"locals\s*\(",
]


class CodeEvolver:
    """Generates, validates, and stores evolved strategy code.

    The Evolution Agent calls this to:
    1. Read existing strategy source code for analysis
    2. Write new strategy code (after validation)
    3. Read infrastructure code for review (read-only)
    """

    def __init__(self, project_root: Path, algorithms_dir: Path) -> None:
        self._project_root = project_root
        self._algorithms_dir = algorithms_dir
        self._src_root = project_root / "src" / "evotrader"

    # ═══════════════════════════════════════════════════════════════
    # Code Reading (for analysis / review)
    # ═══════════════════════════════════════════════════════════════

    def read_strategy_source(self, strategy_name: str) -> dict[str, str]:
        """Read the source code of an existing strategy.

        Args:
            strategy_name: Name like 'momentum', 'mean_reversion', 'gap',
                'composite', 'base', 'event_window_timing', etc.

        Returns:
            Dict with 'file_path' and 'source' keys.
        """
        # Check both locations: top-level algorithms/ (base, composite)
        # and algorithms/strategies/ (actual strategy implementations)
        candidates = [
            self._src_root / "algorithms" / "strategies" / f"{strategy_name}.py",
            self._src_root / "algorithms" / f"{strategy_name}.py",
        ]
        file_path = None
        for candidate in candidates:
            if candidate.is_file():
                file_path = candidate
                break

        if file_path is None:
            searched = ", ".join(str(c) for c in candidates)
            return {"error": f"Strategy file not found. Searched: {searched}"}

        return {
            "file_path": str(file_path.relative_to(self._project_root)),
            "source": file_path.read_text(),
            "lines": file_path.read_text().count("\n") + 1,
        }

    def read_source_file(self, relative_path: str) -> dict[str, str]:
        """Read any source file from the project (for code review).

        Only allows reading from within the ``src/evotrader/`` directory
        for security.

        Args:
            relative_path: Path relative to project root
                (e.g., 'src/evotrader/indicators/rsi.py').
        """
        file_path = self._project_root / relative_path
        resolved = file_path.resolve()

        # Security: only allow reading within the project
        if not str(resolved).startswith(str(self._project_root.resolve())):
            return {"error": "Access denied: path outside project"}

        if not resolved.is_file():
            return {"error": f"File not found: {relative_path}"}

        return {
            "file_path": relative_path,
            "source": resolved.read_text(),
            "lines": resolved.read_text().count("\n") + 1,
        }

    def list_source_files(self, subdirectory: str = "") -> list[dict[str, Any]]:
        """List source files in a project subdirectory.

        Args:
            subdirectory: Relative path like 'algorithms' or 'indicators'.
        """
        search_dir = self._src_root / subdirectory if subdirectory else self._src_root
        if not search_dir.is_dir():
            return []

        files = []
        for f in sorted(search_dir.rglob("*.py")):
            rel = f.relative_to(self._project_root)
            files.append(
                {
                    "path": str(rel),
                    "name": f.name,
                    "size_bytes": f.stat().st_size,
                    "lines": f.read_text().count("\n") + 1,
                }
            )
        return files

    # ═══════════════════════════════════════════════════════════════
    # Code Validation
    # ═══════════════════════════════════════════════════════════════

    def validate_strategy_code(self, code: str) -> dict[str, Any]:
        """Validate proposed strategy code before writing to disk.

        Checks:
        1. Python syntax (AST parse)
        2. Forbidden imports (os, sys, subprocess, etc.)
        3. Forbidden patterns (eval, exec, open, etc.)
        4. Extends TradingAlgorithm base class
        5. Implements required methods (name, version, compute_signal)

        Returns:
            Dict with 'valid' (bool), 'errors' (list), 'warnings' (list).
        """
        errors: list[str] = []
        warnings: list[str] = []

        # 1. Syntax check
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            errors.append(f"Syntax error at line {e.lineno}: {e.msg}")
            return {"valid": False, "errors": errors, "warnings": warnings}

        # 2. Forbidden import check
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = ""
                if isinstance(node, ast.ImportFrom) and node.module:
                    module = node.module
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        module = alias.name

                if module and not _is_allowed_import(module):
                    errors.append(f"Forbidden import: '{module}'")

        # 3. Forbidden pattern check
        for pattern in _FORBIDDEN_PATTERNS:
            matches = re.findall(pattern, code)
            if matches:
                errors.append(f"Forbidden pattern detected: '{pattern}'")

        # 3b. Dual-use reflection: WARN, do not reject.
        # `setattr(self, f"_{key}", ...)` is the shipped `set_parameters` idiom
        # in five strategies and `getattr(snapshot.quote, "previous_close",
        # None)` is a defensive read in a sixth. Treating these as errors made
        # the validator unable to approve conforming code, which is how the
        # whole library came to fail its own guard. The escalation path they
        # could enable — `getattr(__builtins__, "__import__")` — is already
        # blocked outright by the `__import__` pattern above.
        for pattern in _REFLECTION_PATTERNS:
            if re.findall(pattern, code):
                warnings.append(
                    f"Reflection used ({pattern}). Permitted — it is the house "
                    f"set_parameters idiom — but it must never be used to reach "
                    f"builtins or module attributes."
                )

        # 4. Class structure check
        classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
        if not classes:
            errors.append("No class definition found. Strategy must define a class.")

        # 5. Required methods check (best-effort)
        has_compute_signal = "compute_signal" in code
        has_name = "def name" in code or "@property" in code
        if not has_compute_signal:
            errors.append("Missing 'compute_signal' method.")
        if not has_name:
            warnings.append("Consider defining a 'name' property.")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
        }

    # ═══════════════════════════════════════════════════════════════
    # Code Review (infrastructure analysis)
    # ═══════════════════════════════════════════════════════════════

    def save_code_review(
        self,
        review_id: str,
        files_reviewed: list[str],
        findings: list[dict[str, str]],
        proposed_diffs: list[dict[str, str]],
    ) -> Path:
        """Save a code review proposal for human review.

        Code reviews of infrastructure are NEVER auto-applied.
        They are saved as markdown files in ``data/evolution/reviews/``.

        Args:
            review_id: Unique review identifier.
            files_reviewed: List of file paths that were analysed.
            findings: List of dicts with 'severity', 'file', 'issue', 'suggestion'.
            proposed_diffs: List of dicts with 'file', 'description', 'diff'.

        Returns:
            Path to the saved review file.
        """
        reviews_dir = self._algorithms_dir.parent / "evolution" / "reviews"
        reviews_dir.mkdir(parents=True, exist_ok=True)

        import re

        safe_filename = re.sub(r"[^a-zA-Z0-9_\-]", "_", review_id)
        review_path = reviews_dir / f"{safe_filename}.md"

        lines = [
            f"# Code Review: {review_id}",
            "",
            f"**Generated**: {datetime.now(UTC).isoformat()}",
            "**Status**: PENDING_REVIEW",
            "",
            "## Files Reviewed",
            "",
        ]
        for f in files_reviewed:
            lines.append(f"- `{f}`")

        lines.extend(["", "## Findings", ""])
        for i, finding in enumerate(findings, 1):
            severity = finding.get("severity", "INFO").upper()
            lines.append(f"### {i}. [{severity}] {finding.get('file', '')}")
            lines.append("")
            lines.append(f"**Issue**: {finding.get('issue', '')}")
            lines.append("")
            lines.append(f"**Suggestion**: {finding.get('suggestion', '')}")
            lines.append("")

        if proposed_diffs:
            lines.extend(["## Proposed Changes", ""])
            for diff in proposed_diffs:
                lines.append(f"### {diff.get('file', '')}")
                lines.append("")
                lines.append(f"{diff.get('description', '')}")
                lines.append("")
                lines.append("```diff")
                lines.append(diff.get("diff", ""))
                lines.append("```")
                lines.append("")

        review_path.write_text("\n".join(lines))
        logger.info("Code review saved: %s", review_path)
        return review_path


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def _is_allowed_import(module: str) -> bool:
    """Check if an import is in the allowlist."""
    for allowed in _ALLOWED_IMPORTS:
        if module == allowed or module.startswith(allowed + "."):
            return True
    return False
