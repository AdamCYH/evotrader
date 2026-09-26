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
    # run() writes --mock-time into the environment; registering it here makes
    # teardown remove it, or every later test would run on a fake clock.
    monkeypatch.setenv("EVOTRADER_MOCK_TIME", "set-by-the-test")
    monkeypatch.delenv("EVOTRADER_MOCK_TIME")
    return calls


def run_with(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["evotrader", *argv])
    main.run()


@pytest.mark.parametrize(
    ("typo", "said"),
    [
        (["--sim"], "unrecognized arguments: --sim (did you mean --mode sim?)"),
        (["--mock_time"], "unrecognized arguments: --mock_time (did you mean --mock-time?)"),
        (["--data_dir=x"], "(did you mean --data-dir?)"),
        (["--dashbord"], "(did you mean --dashboard?)"),
        (["--mode", "paper"], "invalid choice: 'paper'"),
        # Abbreviations are off: "--sim 5000" read as "--sim-deposit 5000"
        # would have deposited play money.
        (
            ["--sim", "5000"],
            "unrecognized arguments: --sim 5000 (did you mean --sim-deposit 5000?)",
        ),
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
    # A sign-in that timed out: the sign-in code has said so already.
    assert not keep(_record("Error on session runner task: unhandled errors in a TaskGroup (1)"))
    assert not keep(
        _record("OAuth flow error", TimeoutError("Authorization timed out. Please try again."))
    )
    assert keep(_record("OAuth flow error", ValueError("invalid_grant"))), "a real one stays"
    cut_short = "Traceback (most recent call last):\n  ...\nasyncio.exceptions.CancelledError\n"
    assert not keep(_record(cut_short))
    assert keep(_record("Traceback (most recent call last):\n  ...\nKeyError: 'x'\n"))


def test_a_broker_error_names_its_cause_in_one_line() -> None:
    """Found 2026-09-26: a sign-in that timed out printed ~100 lines of traceback."""
    from evotrader.db.reconciliation import root_cause

    try:
        try:
            raise ExceptionGroup("unhandled errors in a TaskGroup", [TimeoutError("timed out")])
        except ExceptionGroup as group:
            raise ConnectionError("Failed to create MCP session") from group
    except ConnectionError as outer:
        cause = root_cause(outer)
    assert isinstance(cause, TimeoutError) and str(cause) == "timed out"


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


@pytest.mark.parametrize("amount", ["inf", "1e400", "nan", "10000001", "0.5"])
def test_play_money_must_be_a_sensible_amount(amount, started, monkeypatch, capsys) -> None:
    """Found 2026-09-26: "inf" was accepted, and an infinite balance broke the console."""
    with pytest.raises(SystemExit):
        run_with(monkeypatch, "--sim-deposit", amount)
    assert not started
    assert "between $1 and $10,000,000" in capsys.readouterr().err or amount == "nan"


def test_a_mock_time_it_cannot_read_stops_it(started, monkeypatch, capsys) -> None:
    """Found 2026-09-26: "--mock-time garbage" ran, logging an error every few seconds."""
    with pytest.raises(SystemExit) as stopped:
        run_with(monkeypatch, "--mock-time", "garbage")
    assert stopped.value.code == 2 and not started
    assert "not a date and time: 'garbage'" in capsys.readouterr().err
    run_with(monkeypatch, "--mock-time", "2026-06-17T10:00:00")  # no offset: New York time
    assert started


def test_the_same_from_the_environment_stops_it() -> None:
    with pytest.raises(SystemExit, match="EVOTRADER_MOCK_TIME: not a date and time"):
        main._moment("next tuesday", "EVOTRADER_MOCK_TIME")


def test_settings_it_cannot_read_are_named(started, monkeypatch, capsys, tmp_path) -> None:
    """Found 2026-09-26: a broken settings.yaml printed a traceback of ~70 lines."""
    (tmp_path / "data" / "settings.yaml").write_text("mode: [live\n")
    with pytest.raises(SystemExit) as stopped:
        run_with(monkeypatch)
    assert stopped.value.code == 1 and not started
    error = capsys.readouterr().err
    assert "settings.yaml can't be used" in error and "Traceback" not in error


class TestOneAppPerFolder:
    """Found 2026-09-26: two copies could run on one data folder, both trading
    the account; the port-in-use advice even led there."""

    def test_a_folder_another_process_holds_is_refused(self, tmp_path) -> None:
        fcntl = pytest.importorskip("fcntl")
        folder = tmp_path / "held"
        folder.mkdir()
        other = open(folder / ".evotrader.lock", "a+")  # noqa: SIM115 - a second holder
        try:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            other.write("4242\n")
            other.flush()
            assert paths.hold(folder) == 4242
        finally:
            other.close()
        assert paths.hold(folder) is None, "free again once the other one has gone"

    def test_the_app_says_so_and_does_not_start(self, started, monkeypatch, capsys, tmp_path):
        fcntl = pytest.importorskip("fcntl")
        other = open(tmp_path / "data" / ".evotrader.lock", "a+")  # noqa: SIM115
        try:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(SystemExit) as stopped:
                run_with(monkeypatch)
        finally:
            other.close()
        assert stopped.value.code == 1 and not started
        assert "already running on this data folder" in capsys.readouterr().err
