"""Regression tests: Ctrl+C stops the app, even with a console tab open.

Found 2026-09-26 while moving the live system onto the public code. Ctrl+C
cancelled the whole app at once, which skipped the web console's own shutdown.
The console had opened its own connection to the live journal, that connection
was never closed, and its worker thread kept the process alive after "shut down
cleanly" — only a hard kill stopped it. An open console tab made it worse: its
live event stream held the web server open.

Now the first Ctrl+C asks the web server to stop (open event streams end at
once), the console reuses the app's own connection to the live journal instead
of opening a second one, and a second Ctrl+C forces the stop.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from evotrader import paths
from evotrader.web.server import create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_console(port: int, proc: subprocess.Popen, seconds: float = 90) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"the app exited during start-up:\n{proc.stdout.read()}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2):
                return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.5)
    raise AssertionError(f"the console did not answer within {seconds}s")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_ctrl_c_stops_the_app_with_a_console_tab_open(tmp_path: Path) -> None:
    data = tmp_path / "data"
    shutil.copytree(paths.project_root() / "starter_data", data)
    shutil.rmtree(data / "notes")  # nothing to embed, so start-up needs no model download
    port = _free_port()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "home"),  # never the real broker sign-in
        "EVOTRADER_DATA_DIR": str(data),
        "EVOTRADER_PORT": str(port),
        "PYTHONUNBUFFERED": "1",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "evotrader", "--dashboard", "--mode", "sim"],
        cwd=paths.project_root(),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_console(port, proc)
        # A console tab: its live event stream stays open until the server ends it.
        tab = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/events", timeout=10)
        assert tab.readline().startswith(b"data:")

        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pytest.fail("the app was still running 15s after Ctrl+C")
        output = proc.stdout.read()
        tab.close()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    assert "shut down cleanly" in output
    assert "Traceback" not in output
    assert "ERROR" not in output


def _app(tmp_path: Path, **extra) -> object:
    config = MagicMock()
    config.data_dir = tmp_path
    config.db_dir = tmp_path / "db"
    db = MagicMock()
    db._db_path = tmp_path / "sim" / "db" / "evotrader_sim.db"  # practice journal
    return create_app(
        db=db,
        journal=MagicMock(),
        metrics=MagicMock(),
        mcp_toolset=None,
        config=config,
        runner_fn=MagicMock(),
        memory=MagicMock(),
        **extra,
    )


def test_the_console_reuses_the_apps_connection_to_the_live_journal(tmp_path: Path) -> None:
    live = MagicMock()
    app = _app(tmp_path, evolution_db=live)
    assert app.state.evolution_db is live
    assert app.state.owns_evolution_db is False  # the app opens and closes it


def test_on_its_own_the_console_opens_and_closes_its_own(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert app.state.evolution_db._db_path == tmp_path / "db" / "evotrader.db"
    assert app.state.owns_evolution_db is True
