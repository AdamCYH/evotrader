"""Regression tests: which value wins when a setting is in the shell and in a .env file.

Found 2026-09-26 by a first-time-user walkthrough. The data folder's .env
overrode the shell, so ``EVOTRADER_PORT=8114 ./run.sh`` was silently ignored
when the file named a port, and a .env copied from .env.example — whose
``EVOTRADER_MOCK_TIME=`` line is empty — cancelled ``--mock-time``.

Now, as with most tools: the environment wins (the shell, or a flag the app
turned into a variable), then the data folder's .env, then the project's; an
empty value means "not set".
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from evotrader import config

NAMES = ("EVOTRADER_PORT", "EVOTRADER_MOCK_TIME", "SOME_KEY")


@pytest.fixture
def folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    for name in NAMES:  # registered, so whatever a load sets is removed afterwards
        monkeypatch.setenv(name, "before")
        monkeypatch.delenv(name)
    monkeypatch.setattr(config, "_from_env_files", {})
    monkeypatch.setattr(config, "_warned_shadowed", set())
    (tmp_path / "data").mkdir()
    (tmp_path / "project").mkdir()
    return tmp_path / "data", tmp_path / "project"


def test_the_shell_wins_and_says_so(folders, monkeypatch, caplog) -> None:
    data, project = folders
    (data / ".env").write_text("EVOTRADER_PORT=8113\n")
    monkeypatch.setenv("EVOTRADER_PORT", "8114")
    with caplog.at_level(logging.WARNING):
        config._load_env_files(data, project)
    assert os.environ["EVOTRADER_PORT"] == "8114"
    assert "EVOTRADER_PORT is set in your environment" in caplog.text
    assert "8113" not in caplog.text and "8114" not in caplog.text, "values are never logged"


def test_the_data_folder_wins_over_the_project(folders) -> None:
    data, project = folders
    (data / ".env").write_text("SOME_KEY=from-data\n")
    (project / ".env").write_text("SOME_KEY=from-project\n")
    config._load_env_files(data, project)
    assert os.environ["SOME_KEY"] == "from-data"


def test_an_empty_value_means_not_set(folders, monkeypatch) -> None:
    data, project = folders
    (data / ".env").write_text("EVOTRADER_MOCK_TIME=\nSOME_KEY=\n")
    (project / ".env").write_text("SOME_KEY=from-project\n")
    monkeypatch.setenv("EVOTRADER_MOCK_TIME", "2026-06-17T10:00:00-04:00")  # from --mock-time
    config._load_env_files(data, project)
    assert os.environ["EVOTRADER_MOCK_TIME"] == "2026-06-17T10:00:00-04:00"
    assert os.environ["SOME_KEY"] == "from-project"


def test_a_value_from_a_file_follows_the_file(folders) -> None:
    """A file changed while the app runs is read again, not mistaken for the shell."""
    data, project = folders
    (data / ".env").write_text("SOME_KEY=first\n")
    config._load_env_files(data, project)
    (data / ".env").write_text("SOME_KEY=second\n")
    config._load_env_files(data, project)
    assert os.environ["SOME_KEY"] == "second"
