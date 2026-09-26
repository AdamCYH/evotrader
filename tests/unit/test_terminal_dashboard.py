"""Regression test: ``./run.sh cli-dashboard`` runs.

Found 2026-09-26 by a first-time-user walkthrough: the terminal dashboard
listed in ``./run.sh --help`` crashed for everyone. It read ``model.default``,
which settings no longer have now that models come in live and practice
blocks; past that, it assumed the data folder sits inside the project, which
is not so for anyone using EVOTRADER_DATA_DIR.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from evotrader import paths


def test_it_runs_on_a_data_folder_outside_the_project(tmp_path: Path) -> None:
    data = tmp_path / "data"
    shutil.copytree(paths.project_root() / "starter_data", data)
    result = subprocess.run(
        [sys.executable, "scripts/dashboard.py"],
        cwd=paths.project_root(),
        env={**os.environ, "EVOTRADER_DATA_DIR": str(data), "HOME": str(tmp_path / "home")},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    assert "Mode" in result.stdout
