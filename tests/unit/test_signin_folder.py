"""Regression tests: the broker sign-in folder comes from one setting, and tests never use the real one.

Found 2026-09-26 while moving the live system onto the public code. The sign-in
code named ``~/.evotrader`` in three places, so every test run wrote into the
user's real home folder: each test that built the broker connection created
``~/.evotrader/oauth/<server>/`` there, and every token store logged to
``~/.evotrader/oauth_forensic.log`` even when the test gave it a temporary
folder. A test could also have read the user's real token.

Now :func:`paths.signin_dir` names the folder (``EVOTRADER_SIGNIN_DIR``, else
``~/.evotrader``), and the test session points it at a temporary folder.
"""

from __future__ import annotations

import atexit
from pathlib import Path

import pytest

from evotrader import paths
from evotrader.mcp.oauth import FileTokenStorage, create_oauth_httpx_factory, hashlib_url


def test_the_default_is_evotrader_in_the_home_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(paths.SIGNIN_DIR_ENV, raising=False)
    assert paths.signin_dir() == Path.home() / ".evotrader"


def test_the_setting_moves_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(paths.SIGNIN_DIR_ENV, str(tmp_path / "signin"))
    assert paths.signin_dir() == (tmp_path / "signin").resolve()


def test_tests_never_use_the_real_one() -> None:
    assert paths.signin_dir() != Path.home() / ".evotrader"
    assert not paths.signin_dir().is_relative_to(Path.home() / ".evotrader")


def test_the_broker_connection_keeps_its_token_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"  # a stand-in, so a regression can't reach the real one
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(paths.SIGNIN_DIR_ENV, str(tmp_path / "signin"))
    exit_checks: list = []
    monkeypatch.setattr(atexit, "register", exit_checks.append)

    create_oauth_httpx_factory("https://broker.example/mcp")

    assert (tmp_path / "signin" / "oauth" / hashlib_url("https://broker.example/mcp")).is_dir()
    for check in exit_checks:
        check()
    assert (tmp_path / "signin" / "oauth_forensic.log").is_file()
    assert not home.exists()


def test_a_token_store_elsewhere_still_logs_to_the_sign_in_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(paths.SIGNIN_DIR_ENV, str(tmp_path / "signin"))
    exit_checks: list = []
    monkeypatch.setattr(atexit, "register", exit_checks.append)

    FileTokenStorage(tmp_path / "somewhere" / "cache")
    for check in exit_checks:
        check()

    assert (tmp_path / "signin" / "oauth_forensic.log").is_file()
