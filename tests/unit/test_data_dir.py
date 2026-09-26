"""The data folder can live anywhere, and one setting moves every path into it.

Open-source prep (P1.1, 2026-09-25): the code becomes public while each user's
data (settings, instructions, journal, keys) stays private, so the data folder
must be movable. Before this, ``data/`` was spelled out in a dozen places —
the backtest cache and reports, the scenario runner, the strategy manifest —
and those would have kept reading the project's own folder whatever the
configuration said.
"""

from __future__ import annotations

import sys

import pytest

from evotrader import main, paths
from evotrader.agents import tools
from evotrader.backtest.data import HistoricalDataFetcher


@pytest.fixture
def elsewhere(tmp_path, monkeypatch):
    folder = tmp_path / "my-data"
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(folder))
    return folder


def test_default_is_data_in_the_project(monkeypatch) -> None:
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    assert paths.data_dir() == paths.project_root() / "data"
    assert (paths.project_root() / "pyproject.toml").is_file()


def test_the_setting_moves_it(elsewhere) -> None:
    assert paths.data_dir() == elsewhere.resolve()


def test_a_home_relative_setting_is_expanded(monkeypatch) -> None:
    monkeypatch.setenv(paths.DATA_DIR_ENV, "~/some-data")
    assert paths.data_dir().is_absolute() and "~" not in str(paths.data_dir())


def test_the_backtest_cache_follows_it(elsewhere) -> None:
    assert HistoricalDataFetcher().cache_dir == elsewhere.resolve() / "backtest" / "cache"


def test_the_strategy_manifest_follows_it(elsewhere, monkeypatch) -> None:
    monkeypatch.setattr(tools, "_strategy_loader", None)
    loader = tools._get_strategy_loader()
    assert loader.manifest_path == elsewhere.resolve() / "algorithms" / "strategy_manifest.yaml"


def test_the_command_line_flag_sets_it_for_everything(tmp_path, monkeypatch) -> None:
    # run() writes the setting into the process environment; registering it
    # with monkeypatch first makes teardown remove it again, so later tests
    # don't inherit a data folder that doesn't exist.
    monkeypatch.setenv(paths.DATA_DIR_ENV, "restored-after-the-test")
    monkeypatch.delenv(paths.DATA_DIR_ENV)
    seen = {}

    async def fake_start(**_kwargs) -> None:
        seen["data_dir"] = paths.data_dir()

    monkeypatch.setattr(main, "start", fake_start)
    (tmp_path / "flag").mkdir()
    (tmp_path / "flag" / "settings.yaml").write_text("{}\n")  # a set-up data folder
    monkeypatch.setattr(sys, "argv", ["evotrader", "--data-dir", str(tmp_path / "flag")])
    main.run()
    assert seen["data_dir"] == (tmp_path / "flag").resolve()


def test_starting_before_setup_stops_with_directions(tmp_path, monkeypatch, capsys) -> None:
    """Found 2026-09-26: with no data folder the app fell back to built-in
    defaults and empty instruction folders, so the agents ran with no
    instructions. It now stops and says to run setup, which seeds the folder
    from starter_data/ and asks for the keys."""
    monkeypatch.setenv(paths.DATA_DIR_ENV, "restored-after-the-test")
    monkeypatch.delenv(paths.DATA_DIR_ENV)
    started = []

    async def fake_start(**_kwargs) -> None:
        started.append(True)

    monkeypatch.setattr(main, "start", fake_start)
    monkeypatch.setattr(sys, "argv", ["evotrader", "--data-dir", str(tmp_path / "empty")])
    with pytest.raises(SystemExit) as stopped:
        main.run()
    assert stopped.value.code == 1
    assert not started, "the app must not start on an empty data folder"
    assert "./run.sh setup" in capsys.readouterr().err
