"""The combined signal per trading day, for the account chart's signal overlay.

The account value is one point per Eastern date and the combined signal one
reading per cycle. ``web.signal_summary.daily_signal_summary`` rolls the
readings up per Eastern date, ``ThoughtLogger.get_composite_readings`` reads
them without the market panel's row limit, and ``/api/chart/signal-daily``
serves them cut at the same date as the equity curve.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from evotrader.web.signal_summary import daily_signal_summary


def _reading(ts: str, value: float | None, ticker: str = "XYZ", regime: str = "trending_bull"):
    return {"timestamp": ts, "composite_signal": value, "ticker": ticker, "regime": regime}


class TestDailySummary:
    def test_one_row_per_eastern_date(self) -> None:
        days = daily_signal_summary(
            [
                _reading("2026-03-02T14:30:00+00:00", 0.10),
                _reading("2026-03-02T15:30:00+00:00", 0.30),
                _reading("2026-03-02T19:30:00+00:00", -0.10),
                _reading("2026-03-03T14:30:00+00:00", 0.05),
            ]
        )
        assert [d["date"] for d in days] == ["2026-03-02", "2026-03-03"]
        first = days[0]
        assert first["count"] == 3
        assert first["mean"] == pytest.approx(0.1)
        assert (first["min"], first["max"], first["last"]) == (-0.1, 0.3, -0.1)
        assert first["regime"] == "trending_bull"
        assert first["ticker"] == "XYZ"

    def test_the_evening_reading_belongs_to_its_eastern_day(self) -> None:
        """21:00 ET is 01:00 UTC the next day; it is still the day it was read."""
        days = daily_signal_summary(
            [
                _reading("2026-03-02T20:00:00+00:00", 0.2),
                _reading("2026-03-03T02:00:00+00:00", 0.4),
            ]
        )
        assert len(days) == 1
        assert days[0]["date"] == "2026-03-02"
        assert days[0]["count"] == 2

    def test_on_a_two_instrument_day_the_main_one_counts(self) -> None:
        days = daily_signal_summary(
            [
                _reading("2026-03-02T14:30:00+00:00", 0.2, "MAIN"),
                _reading("2026-03-02T15:30:00+00:00", -0.6, "HEDGE"),
                _reading("2026-03-02T16:30:00+00:00", 0.4, "MAIN"),
            ]
        )
        assert days[0]["ticker"] == "MAIN"
        assert days[0]["mean"] == pytest.approx(0.3)

    def test_a_tie_goes_to_the_later_instrument(self) -> None:
        days = daily_signal_summary(
            [
                _reading("2026-03-02T14:30:00+00:00", 0.2, "OLD"),
                _reading("2026-03-02T15:30:00+00:00", -0.2, "NEW"),
            ]
        )
        assert days[0]["ticker"] == "NEW"

    def test_the_regime_most_readings_had(self) -> None:
        days = daily_signal_summary(
            [
                _reading("2026-03-02T14:30:00+00:00", 0.1, regime="range_bound"),
                _reading("2026-03-02T15:30:00+00:00", 0.1, regime="trending_bull"),
                _reading("2026-03-02T16:30:00+00:00", 0.1, regime="trending_bull"),
            ]
        )
        assert days[0]["regime"] == "trending_bull"

    def test_unusable_readings_are_skipped(self) -> None:
        days = daily_signal_summary(
            [
                _reading("2026-03-02T14:30:00+00:00", None),
                _reading("", 0.5),
                _reading("not a time", 0.5),
                _reading("2026-03-02T15:30:00+00:00", 0.25),
            ]
        )
        assert [(d["date"], d["count"], d["mean"]) for d in days] == [("2026-03-02", 1, 0.25)]

    def test_nothing_in_nothing_out(self) -> None:
        assert daily_signal_summary([]) == []


class TestTheJournalQuery:
    async def _snapshot(self, db, ts: str, value: float | None, ticker: str = "XYZ") -> None:
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO market_snapshots (timestamp, ticker, composite_signal, regime) "
                "VALUES (?, ?, ?, ?)",
                (ts, ticker, value, "trending_bull"),
            )

    async def test_every_reading_oldest_first(self, db) -> None:
        from evotrader.db.thought_log import ThoughtLogger

        await self._snapshot(db, "2026-03-03T14:30:00+00:00", 0.2)
        await self._snapshot(db, "2026-03-02T14:30:00+00:00", 0.1)
        await self._snapshot(db, "2026-03-02T15:30:00+00:00", None)
        rows = await ThoughtLogger(db).get_composite_readings()
        assert [r["composite_signal"] for r in rows] == [0.1, 0.2]
        assert set(rows[0]) == {"timestamp", "ticker", "composite_signal", "regime"}

    async def test_since(self, db) -> None:
        from evotrader.db.thought_log import ThoughtLogger

        await self._snapshot(db, "2026-03-02T14:30:00+00:00", 0.1)
        await self._snapshot(db, "2026-03-03T14:30:00+00:00", 0.2)
        rows = await ThoughtLogger(db).get_composite_readings("2026-03-03T00:00:00+00:00")
        assert [r["composite_signal"] for r in rows] == [0.2]

    async def test_no_row_limit(self, db) -> None:
        """The market panel's history stops at 500 readings; the overlay's does not."""
        from evotrader.db.thought_log import ThoughtLogger

        start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
        async with db.transaction() as conn:
            await conn.executemany(
                "INSERT INTO market_snapshots (timestamp, ticker, composite_signal) VALUES (?, ?, ?)",
                [((start + timedelta(hours=i)).isoformat(), "XYZ", 0.01) for i in range(620)],
            )
        assert len(await ThoughtLogger(db).get_composite_readings()) == 620


class TestTheEndpoint:
    @pytest.fixture
    def app_client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from evotrader.config import AppConfig
        from evotrader.web.server import create_app

        monkeypatch.setenv("DASHBOARD_PASSWORD", "test_password")
        app = create_app(
            db=MagicMock(),
            journal=MagicMock(),
            metrics=MagicMock(),
            mcp_toolset=MagicMock(),
            config=AppConfig(data_dir=tmp_path / "data"),
            runner_fn=MagicMock(),
            memory=MagicMock(),
        )
        client = TestClient(app)
        client.headers.update({"Authorization": "Bearer test_password"})
        return app, client

    def test_days_for_all_time(self, app_client) -> None:
        app, client = app_client
        app.state.thought_logger.get_composite_readings = AsyncMock(
            return_value=[
                _reading("2026-03-02T14:30:00+00:00", 0.1),
                _reading("2026-03-03T14:30:00+00:00", -0.2),
            ]
        )
        res = client.get("/api/chart/signal-daily?period=all")
        assert res.status_code == 200
        assert [d["date"] for d in res.json()["days"]] == ["2026-03-02", "2026-03-03"]
        app.state.thought_logger.get_composite_readings.assert_awaited_once_with(None)

    def test_a_period_is_cut_at_the_equity_curves_first_date(self, app_client) -> None:
        from evotrader.tools.market_hours import ET
        from evotrader.utils import get_period_cutoff_dt

        app, client = app_client
        first = get_period_cutoff_dt("week", ET)
        before = (first - timedelta(hours=2)).astimezone(UTC).isoformat()
        inside = (first + timedelta(hours=14)).astimezone(UTC).isoformat()
        app.state.thought_logger.get_composite_readings = AsyncMock(
            return_value=[_reading(before, 0.5), _reading(inside, 0.1)]
        )
        days = client.get("/api/chart/signal-daily?period=week").json()["days"]
        assert [d["date"] for d in days] == [first.strftime("%Y-%m-%d")]
        (since,) = app.state.thought_logger.get_composite_readings.await_args.args
        assert since < first.astimezone(UTC).isoformat(), "a day of margin for the ET date"

    def test_a_read_failure_is_an_empty_answer(self, app_client) -> None:
        app, client = app_client
        app.state.thought_logger.get_composite_readings = AsyncMock(side_effect=RuntimeError("x"))
        res = client.get("/api/chart/signal-daily?period=month")
        assert res.status_code == 200
        assert res.json()["days"] == []

    def test_it_needs_the_password(self, app_client) -> None:
        _, client = app_client
        client.headers.pop("Authorization")
        assert client.get("/api/chart/signal-daily").status_code == 401
