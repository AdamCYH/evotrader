"""Regression tests for starting the app: its options, its port and its output.

Found 2026-09-26 by a first-time-user walkthrough:

* Unknown options were ignored, so a typo (``--mock_time``, ``--sim``) started
  the app in whatever mode the settings say — possibly live.
* ``--sim-deposit -9000`` crashed with a traceback, then hung: the databases
  opened for it were never closed.
* A console port already in use was reported as "running", then failed with a
  bare "address already in use".
* ``./run.sh --data-dir mydata``, typed outside the project, looked for
  ``mydata`` inside the project, because run.sh moves into its own folder.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from evotrader import main, paths


@pytest.fixture
def started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """``main.start`` replaced by a recorder, on a data folder that is set up."""
    calls: list[dict] = []

    async def fake_start(**kwargs) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(main, "start", fake_start)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "settings.yaml").write_text("{}\n")
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "data"))
    return calls


def run_with(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["evotrader", *argv])
    main.run()


@pytest.mark.parametrize(
    ("typo", "said"),
    [
        (["--sim"], "unrecognized arguments: --sim (did you mean --mode sim?)"),
        (["--mock_time"], "unrecognized arguments: --mock_time"),
        (["--mode", "paper"], "invalid choice: 'paper'"),
        # Abbreviations are off: "--sim 5000" read as "--sim-deposit 5000"
        # would have deposited play money.
        (["--sim", "5000"], "unrecognized arguments: --sim 5000"),
    ],
)
def test_an_unknown_option_stops_it(typo, said, started, monkeypatch, capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        run_with(monkeypatch, *typo)
    assert stopped.value.code == 2
    assert not started, "nothing may start on a mistyped option"
    error = capsys.readouterr().err
    assert "usage: ./run.sh" in error and said in error


@pytest.mark.parametrize("amount", ["-9000", "0", "lots"])
def test_play_money_must_be_an_amount_above_zero(amount, started, monkeypatch, capsys) -> None:
    with pytest.raises(SystemExit):
        run_with(monkeypatch, f"--sim-deposit={amount}")
    assert not started
    assert "--sim-deposit" in capsys.readouterr().err


def test_play_money_may_be_written_like_money(started, monkeypatch) -> None:
    run_with(monkeypatch, "--sim-deposit", "$5,000")
    assert started[0]["sim_deposit"] == 5000.0


def test_a_port_in_use_is_seen_before_starting() -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        assert not main._port_is_free("127.0.0.1", port)
    assert main._port_is_free("127.0.0.1", port)


def test_a_port_that_is_not_a_number_is_named(monkeypatch) -> None:
    monkeypatch.setenv("EVOTRADER_PORT", "80a")
    with pytest.raises(SystemExit, match="EVOTRADER_PORT='80a' is not a port number"):
        main._console_port()


def _record(message: str, exc: BaseException | None = None) -> logging.LogRecord:
    exc_info = (type(exc), exc, None) if exc else None
    return logging.LogRecord("x", logging.ERROR, __file__, 1, message, (), exc_info)


def test_noise_that_only_alarms_is_dropped_or_calmed() -> None:
    keep = main._drop_alarming_noise
    assert not keep(_record("Exception in ASGI application\n", asyncio.CancelledError()))
    assert not keep(_record("Failed to configure mTLS using AsyncAuthorizedSession: ..."))
    assert not keep(_record("Error on session runner task: "))
    assert keep(_record("Error on session runner task: connection refused")), "a real error"
    assert keep(_record("Exception in ASGI application\n", ValueError("real")))
    cancel = _record("Cancel 4 running task(s), timeout graceful shutdown exceeded")
    assert keep(cancel) and cancel.levelname == "INFO"


def test_run_sh_finds_a_relative_data_folder_where_you_are(tmp_path: Path) -> None:
    name = "relative-data-folder-from-a-test"
    wrong_place = paths.project_root() / name
    assert not wrong_place.exists()
    try:
        result = subprocess.run(
            [str(paths.project_root() / "run.sh"), "init", "--data-dir", name],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert (tmp_path / name / "settings.yaml").is_file()
        assert not wrong_place.exists(), "made inside the project instead"
    finally:
        shutil.rmtree(wrong_place, ignore_errors=True)
