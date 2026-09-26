"""Regression tests: the web console is reachable only by whom it should be.

Found 2026-09-25 while documenting security for the public release:

- The console granted cross-origin access to every website (CORS ``*``). Its
  live event stream (``/api/events``) carries no password — a browser's
  EventSource cannot send one — so any web page the user happened to visit could
  read the running system's live log and trade proposals.
- The server listened on every network interface, so anything on the same
  network could reach a console that, without DASHBOARD_PASSWORD, has no login
  and can approve trades.

The console is served by the same app, so it needs no cross-origin access at
all; it now listens only on this computer unless EVOTRADER_HOST says
otherwise, and refuses to listen more widely without a password.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from evotrader import main
from evotrader.web.server import create_app


def _app(tmp_path):
    config = MagicMock()
    config.data_dir = tmp_path
    return create_app(
        db=MagicMock(),
        journal=MagicMock(),
        metrics=MagicMock(),
        mcp_toolset=MagicMock(),
        config=config,
        runner_fn=MagicMock(),
        memory=MagicMock(),
    )


def _preflight(client: TestClient, origin: str):
    return client.options(
        "/api/portfolio",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )


class TestOtherWebsites:
    def test_cannot_read_the_console(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("EVOTRADER_CORS_ORIGINS", raising=False)
        response = _preflight(TestClient(_app(tmp_path)), "https://some-website.example")
        assert "access-control-allow-origin" not in response.headers

    def test_a_named_front_end_can_opt_in(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("EVOTRADER_CORS_ORIGINS", "http://localhost:5173, http://127.0.0.1:5173")
        client = TestClient(_app(tmp_path))
        allowed = _preflight(client, "http://localhost:5173")
        assert allowed.headers.get("access-control-allow-origin") == "http://localhost:5173"
        other = _preflight(client, "https://some-website.example")
        assert "access-control-allow-origin" not in other.headers


class TestWhereTheConsoleListens:
    def test_only_this_computer_by_default(self, monkeypatch) -> None:
        monkeypatch.delenv("EVOTRADER_HOST", raising=False)
        assert main.console_host() == "127.0.0.1"

    def test_other_devices_with_a_password(self, monkeypatch) -> None:
        monkeypatch.setenv("EVOTRADER_HOST", "0.0.0.0")
        monkeypatch.setenv("DASHBOARD_PASSWORD", "a-long-password")
        assert main.console_host() == "0.0.0.0"

    @pytest.mark.parametrize("password", [None, ""])
    def test_other_devices_without_a_password_refuse_to_start(self, monkeypatch, password) -> None:
        monkeypatch.setenv("EVOTRADER_HOST", "0.0.0.0")
        if password is None:
            monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
        else:
            monkeypatch.setenv("DASHBOARD_PASSWORD", password)
        with pytest.raises(SystemExit, match="DASHBOARD_PASSWORD"):
            main.console_host()
