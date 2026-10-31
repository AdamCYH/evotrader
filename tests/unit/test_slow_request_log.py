"""The console logs each request that was slow to answer.

A console page that feels slow can be the server working out the answer, a
large answer being sent, many requests at once, or a trading cycle busy in the
same process. ``SlowRequestLog`` writes one line for each request over a limit
with which of those it was. The clock here is a stand-in: an endpoint moves it
forward by the time it "takes".
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from evotrader.web import slow_requests
from evotrader.web.slow_requests import SlowRequestLog


@pytest.fixture
def console():
    now = [0.0]
    state = {"status": "idle"}
    app = FastAPI()

    @app.get("/fast")
    async def fast() -> dict:
        now[0] += 0.1
        return {"ok": True}

    @app.get("/slow")
    async def slow() -> dict:
        now[0] += 2.0
        return {"rows": ["x" * 100] * 20}

    @app.get("/slow-during-a-cycle")
    async def slow_during_a_cycle() -> dict:
        now[0] += 1.5
        state["status"] = "idle"
        return {}

    @app.get("/fails")
    async def fails() -> dict:
        now[0] += 3.0
        raise RuntimeError("boom")

    @app.get("/events")
    async def events() -> StreamingResponse:
        async def stream():
            now[0] += 600.0
            yield "data: {}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    app.add_middleware(
        SlowRequestLog,
        limit_seconds=0.5,
        app_status=lambda: state["status"],
        clock=lambda: now[0],
    )
    return TestClient(app, raise_server_exceptions=False), state


def _lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == slow_requests.__name__]


class TestSlowRequestLog:
    def test_a_fast_request_is_not_logged(self, console, caplog) -> None:
        tc, _ = console
        with caplog.at_level(logging.INFO, logger=slow_requests.__name__):
            assert tc.get("/fast").status_code == 200
        assert _lines(caplog) == []

    def test_a_slow_request_says_how_long_how_big_and_what_the_app_was_doing(
        self, console, caplog
    ) -> None:
        tc, _ = console
        with caplog.at_level(logging.INFO, logger=slow_requests.__name__):
            tc.get("/slow?limit=500")
        (line,) = _lines(caplog)
        assert line.startswith("Slow console request: GET /slow?limit=500 200 took 2.00 s")
        assert "first byte at 2.00 s, 2 KB" in line
        assert "0 other requests open" in line
        assert line.endswith("app idle)")

    def test_a_change_of_state_while_it_waited_is_said(self, console, caplog) -> None:
        tc, state = console
        state["status"] = "running"
        with caplog.at_level(logging.INFO, logger=slow_requests.__name__):
            tc.get("/slow-during-a-cycle")
        (line,) = _lines(caplog)
        assert line.endswith("app running, then idle)")

    def test_a_request_that_failed_is_still_logged(self, console, caplog) -> None:
        tc, _ = console
        with caplog.at_level(logging.INFO, logger=slow_requests.__name__):
            assert tc.get("/fails").status_code == 500
        (line,) = _lines(caplog)
        assert "GET /fails - took 3.00 s (no answer sent" in line

    def test_the_event_stream_is_never_counted(self, console, caplog) -> None:
        tc, _ = console
        with caplog.at_level(logging.INFO, logger=slow_requests.__name__):
            assert tc.get("/events").status_code == 200
        assert _lines(caplog) == []

    def test_requests_open_at_once_are_counted(self, caplog) -> None:
        middleware = SlowRequestLog(None, limit_seconds=0.5)
        with caplog.at_level(logging.INFO, logger=slow_requests.__name__):
            middleware._log(
                {"method": "GET", "path": "/api/trades"},
                1.0,
                {"code": 200, "first_byte": 0.9, "bytes": 2_400_000, "stream": False},
                3,
                "",
            )
        (line,) = _lines(caplog)
        assert line == (
            "Slow console request: GET /api/trades 200 took 1.00 s "
            "(first byte at 0.90 s, 2.4 MB; 3 other requests open)"
        )


class TestTheLimit:
    @pytest.mark.parametrize(
        ("value", "limit"),
        [("", 0.5), ("2", 2.0), ("0.25", 0.25), ("0", 0.0), ("-1", 0.0), ("soon", 0.5)],
    )
    def test_from_the_environment(self, monkeypatch, value, limit) -> None:
        monkeypatch.setenv(slow_requests.ENV_VAR, value)
        assert slow_requests.limit_from_env() == limit

    def test_zero_turns_it_off(self, caplog) -> None:
        now = [0.0]
        app = FastAPI()

        @app.get("/slow")
        async def slow() -> dict:
            now[0] += 60.0
            return {}

        app.add_middleware(SlowRequestLog, limit_seconds=0, clock=lambda: now[0])
        with caplog.at_level(logging.INFO, logger=slow_requests.__name__):
            TestClient(app).get("/slow")
        assert _lines(caplog) == []

    def test_the_console_has_it(self, tmp_path) -> None:
        from unittest.mock import MagicMock

        from evotrader.config import AppConfig
        from evotrader.web.server import create_app

        app = create_app(
            db=MagicMock(),
            journal=MagicMock(),
            metrics=MagicMock(),
            mcp_toolset=MagicMock(),
            config=AppConfig(data_dir=tmp_path / "data"),
            runner_fn=MagicMock(),
            memory=MagicMock(),
        )
        assert any(m.cls is SlowRequestLog for m in app.user_middleware)
