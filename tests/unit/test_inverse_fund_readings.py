"""An inverse fund's readings stay apart from the primary's.

When the strategy agent trades the primary's inverse fund, it runs the
market-data tool on the fund as well. That stores a snapshot with the fund's
own signal, read on the fund's prices, which sits roughly opposite to the
primary's. Nothing in a trading decision reads stored snapshots (each cycle
computes the primary's signal from its own prices), but everything that read
them back did so without asking which instrument they were for:

- the market panel's chart drew both in one line, zigzagging between a reading
  and its mirror image, and its "latest" reading was whichever came last;
- the account chart's daily signal took the instrument with most readings;
- the attribution backfills took a session's latest snapshot or market-data
  response, which could be the fund's;
- the agent could log the cycle's attribution row under the fund, where the
  fund's price and next-day move score a right bearish call as wrong.

Made-up instruments throughout: XYZ is the primary, ZYX its -2x inverse fund.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from evotrader.db.signal_attribution import SignalAttributionStore
from evotrader.db.thought_log import ThoughtLogger
from evotrader.models.config import AssetConfig, InstrumentInfo
from evotrader.web.signal_summary import daily_signal_summary

_T = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)


async def _snapshot(
    db,
    ticker: str,
    when: datetime,
    composite: float,
    *,
    session: str = "s1",
    close: float = 100.0,
    subs: list[dict] | None = None,
) -> None:
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO market_snapshots (timestamp, session_id, ticker, close_price, "
            "composite_signal, regime, sub_signals_json) VALUES (?,?,?,?,?,?,?)",
            (
                when.isoformat(),
                session,
                ticker,
                close,
                composite,
                "trending_bull",
                json.dumps(subs or [{"name": "momentum", "value": composite, "weight": 1.0}]),
            ),
        )


def _app(tmp_path, db=None, *, primary: str = "XYZ"):
    from fastapi.testclient import TestClient

    from evotrader.config import AppConfig
    from evotrader.web.server import create_app

    config = AppConfig(data_dir=tmp_path / "data")
    config.settings.asset.primary_ticker = primary
    app = create_app(
        db=db or MagicMock(),
        journal=MagicMock(),
        metrics=MagicMock(),
        mcp_toolset=MagicMock(),
        config=config,
        runner_fn=AsyncMock(),
        memory=MagicMock(),
    )
    client = TestClient(app)
    client.headers.update({"Authorization": "Bearer test_password"})
    return app, client


@pytest.fixture
def password(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test_password")


class TestTheSnapshotQuery:
    async def test_one_instruments_snapshots(self, db) -> None:
        await _snapshot(db, "XYZ", _T, 0.2)
        await _snapshot(db, "ZYX", _T + timedelta(minutes=5), -0.3)
        await _snapshot(db, "XYZ", _T + timedelta(hours=1), 0.1)
        logger = ThoughtLogger(db)
        assert [p["composite_signal"] for p in await logger.get_market_snapshots(ticker="XYZ")] == [
            0.1,
            0.2,
        ]
        assert [p["ticker"] for p in await logger.get_market_snapshots(ticker="ZYX")] == ["ZYX"]
        assert len(await logger.get_market_snapshots()) == 3, "no ticker: every instrument"

    async def test_the_limit_counts_the_instruments_own_rows(self, db) -> None:
        for i in range(5):
            await _snapshot(db, "ZYX", _T + timedelta(minutes=i), -0.1)
        await _snapshot(db, "XYZ", _T - timedelta(days=1), 0.2)
        points = await ThoughtLogger(db).get_market_snapshots(limit=2, ticker="XYZ")
        assert [p["ticker"] for p in points] == ["XYZ"]

    async def test_the_instruments_with_snapshots_newest_first(self, db) -> None:
        await _snapshot(db, "XYZ", _T, 0.2)
        await _snapshot(db, "XYZ", _T + timedelta(hours=2), 0.2)
        await _snapshot(db, "ZYX", _T + timedelta(hours=1), -0.3)
        await _snapshot(db, "", _T + timedelta(hours=3), 0.0)
        rows = await ThoughtLogger(db).snapshot_tickers()
        assert [(r["ticker"], r["count"]) for r in rows] == [("XYZ", 2), ("ZYX", 1)]


class TestTheMarketPanel:
    async def test_the_primary_by_default_and_the_choice_on_offer(
        self, db, tmp_path, password
    ) -> None:
        await _snapshot(db, "XYZ", _T, 0.2)
        await _snapshot(db, "ZYX", _T + timedelta(minutes=5), -0.3)
        _, client = _app(tmp_path, db)
        body = client.get("/api/chart/tech?period=all").json()
        assert body["ticker"] == "XYZ"
        assert body["tickers"] == ["XYZ", "ZYX"]
        assert [p["ticker"] for p in body["points"]] == ["XYZ"]
        assert body["latest"]["ticker"] == "XYZ", "not the fund's later reading"
        assert body["latest"]["composite_signal"] == 0.2

    async def test_another_instrument_on_request(self, db, tmp_path, password) -> None:
        await _snapshot(db, "XYZ", _T, 0.2)
        await _snapshot(db, "ZYX", _T + timedelta(minutes=5), -0.3)
        _, client = _app(tmp_path, db)
        body = client.get("/api/chart/tech?period=all&ticker=zyx").json()
        assert body["ticker"] == "ZYX"
        assert [p["composite_signal"] for p in body["points"]] == [-0.3]
        assert body["latest"]["ticker"] == "ZYX"

    async def test_the_primary_is_offered_before_it_has_a_reading(
        self, db, tmp_path, password
    ) -> None:
        await _snapshot(db, "OLD", _T, 0.2)
        _, client = _app(tmp_path, db)
        body = client.get("/api/chart/tech").json()
        assert (body["ticker"], body["tickers"]) == ("XYZ", ["XYZ", "OLD"])
        assert body["points"] == []

    async def test_the_journals_fallback_reading_must_be_the_chosen_instrument(
        self, db, tmp_path, password
    ) -> None:
        app, client = _app(tmp_path, db)
        fund_trade = {
            "ticker": "ZYX",
            "timestamp": _T.isoformat(),
            "market_snapshot": json.dumps({"ticker": "ZYX", "composite_signal": -0.3}),
        }
        app.state.journal.get_recent_trades = AsyncMock(return_value=[fund_trade])
        assert client.get("/api/chart/tech").json()["latest"] is None
        latest = client.get("/api/chart/tech?ticker=ZYX").json()["latest"]
        assert latest["ticker"] == "ZYX"


def _reading(ts: datetime, value: float, ticker: str) -> dict:
    return {
        "timestamp": ts.isoformat(),
        "composite_signal": value,
        "ticker": ticker,
        "regime": "trending_bull",
    }


class TestTheAccountChartsDailySignal:
    def test_the_primary_counts_even_when_the_fund_was_read_more(self) -> None:
        readings = [
            _reading(_T, 0.2, "XYZ"),
            _reading(_T + timedelta(minutes=5), -0.4, "ZYX"),
            _reading(_T + timedelta(minutes=65), -0.4, "ZYX"),
        ]
        (day,) = daily_signal_summary(readings, preferred="XYZ")
        assert (day["ticker"], day["mean"], day["count"]) == ("XYZ", 0.2, 1)
        assert daily_signal_summary(readings)[0]["ticker"] == "ZYX", "the old majority rule"

    def test_a_day_without_the_primary_keeps_the_majority_rule(self) -> None:
        """Days from before the primary was the primary."""
        readings = [
            _reading(_T, 0.2, "OLD"),
            _reading(_T + timedelta(hours=1), 0.4, "OLD"),
            _reading(_T + timedelta(hours=2), -0.1, "OLDFUND"),
        ]
        (day,) = daily_signal_summary(readings, preferred="XYZ")
        assert day["ticker"] == "OLD"

    def test_the_endpoint_prefers_the_configured_primary(self, tmp_path, password) -> None:
        app, client = _app(tmp_path)
        app.state.thought_logger.get_composite_readings = AsyncMock(
            return_value=[
                _reading(_T, 0.2, "XYZ"),
                _reading(_T + timedelta(minutes=5), -0.4, "ZYX"),
                _reading(_T + timedelta(minutes=65), -0.4, "ZYX"),
            ]
        )
        (day,) = client.get("/api/chart/signal-daily?period=all").json()["days"]
        assert (day["ticker"], day["mean"]) == ("XYZ", 0.2)


async def _cycle(db, session: str, when: datetime) -> None:
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO cycle_runs (session_id, timestamp, cycle_type, status) VALUES (?,?,?,?)",
            (session, when.isoformat(), "TRADING", "SUCCESS"),
        )


async def _rows(db) -> dict[str, dict]:
    async with db.connection() as conn:
        cur = await conn.execute("SELECT * FROM signal_attribution")
        return {r["session_id"]: dict(r) for r in await cur.fetchall()}


class TestTheAttributionBackfills:
    async def test_channel_votes_come_from_the_rows_own_instrument(self, db) -> None:
        await _snapshot(db, "XYZ", _T, 0.2)
        await _snapshot(db, "ZYX", _T + timedelta(minutes=1), -0.3)
        store = SignalAttributionStore(db)
        await store.record(
            "XYZ", session_id="s1", timestamp=(_T + timedelta(minutes=2)).isoformat()
        )
        assert await store.backfill_channel_votes() == 1
        votes = json.loads((await _rows(db))["s1"]["channel_votes"])
        assert votes["momentum"]["value"] == pytest.approx(0.2), "not the fund's later -0.3"

    async def test_no_snapshot_of_the_rows_instrument_leaves_it_empty(self, db) -> None:
        await _snapshot(db, "ZYX", _T, -0.3)
        store = SignalAttributionStore(db)
        await store.record(
            "XYZ", session_id="s1", timestamp=(_T + timedelta(minutes=2)).isoformat()
        )
        assert await store.backfill_channel_votes() == 0

    async def test_participation_comes_from_the_rows_own_instrument(self, db) -> None:
        async def response(ticker: str, when: datetime, scale: float) -> None:
            meta = {
                "response": {
                    "ticker": ticker,
                    "algo_signal": {"participation_scale": scale, "composite_unattenuated": 0.2},
                }
            }
            async with db.transaction() as conn:
                await conn.execute(
                    "INSERT INTO agent_thought_log (timestamp, session_id, agent_name, "
                    "event_type, content, meta) VALUES (?,?,?,?,?,?)",
                    (
                        when.isoformat(),
                        "s1",
                        "strategy",
                        "tool_response",
                        "gather_market_data",
                        json.dumps(meta),
                    ),
                )

        await response("XYZ", _T, 0.5)
        await response("ZYX", _T + timedelta(minutes=1), 0.9)
        store = SignalAttributionStore(db)
        await store.record(
            "XYZ", session_id="s1", timestamp=(_T + timedelta(minutes=2)).isoformat()
        )
        assert await store.backfill_participation() == 1
        assert (await _rows(db))["s1"]["participation_scale"] == pytest.approx(0.5)

    async def test_the_algorithms_call_is_the_primarys(self, db) -> None:
        store = SignalAttributionStore(db)
        await store.record("XYZ", session_id="s0", timestamp=_T.isoformat())
        later = _T + timedelta(hours=1)
        await _cycle(db, "s2", later)
        await _snapshot(db, "XYZ", later, 0.2, session="s2", close=100.0)
        await _snapshot(db, "ZYX", later + timedelta(minutes=1), -0.3, session="s2", close=20.0)
        assert await store.backfill_algo_only_rows(primary="XYZ") == 1
        row = (await _rows(db))["s2"]
        assert (row["ticker"], row["price_at_decision"]) == ("XYZ", 100.0)
        assert row["algo_direction"] == 1.0

    async def test_without_the_primarys_snapshot_the_sessions_latest(self, db) -> None:
        """A session from before the primary was the primary."""
        store = SignalAttributionStore(db)
        await store.record("OLD", session_id="s0", timestamp=_T.isoformat())
        later = _T + timedelta(hours=1)
        await _cycle(db, "s2", later)
        await _snapshot(db, "OLD", later, 0.2, session="s2", close=50.0)
        assert await store.backfill_algo_only_rows(primary="XYZ") == 1
        assert (await _rows(db))["s2"]["ticker"] == "OLD"


def _asset(**kwargs) -> SimpleNamespace:
    return SimpleNamespace(settings=SimpleNamespace(asset=AssetConfig(**kwargs)))


class TestTheAgentsAttributionRow:
    @pytest.fixture
    def tools(self, db, monkeypatch):
        from evotrader.agents import tools

        monkeypatch.setattr(tools, "_db", db)
        monkeypatch.setattr(
            tools,
            "_config",
            _asset(
                primary_ticker="XYZ",
                inverse_ticker="ZYX",
                instruments={"ZYX": InstrumentInfo(leverage=-2.0, role="bearish vehicle")},
            ),
        )
        return tools

    async def _log(self, tools, ticker: str) -> dict:
        return await tools.log_signal_attribution(
            json.dumps(
                {
                    "ticker": ticker,
                    "algo_direction": -1,
                    "final_direction": -1,
                    "traded": True,
                    "price_at_decision": 20.0,
                }
            )
        )

    async def test_a_row_under_the_inverse_fund_is_refused(self, db, tools) -> None:
        res = await self._log(tools, "zyx")
        assert res["status"] == "error"
        assert "under ticker XYZ" in res["error"]
        assert "bearish XYZ call, -1" in res["error"]
        assert await _rows(db) == {}, "nothing written"

    async def test_the_primary_is_recorded(self, db, tools) -> None:
        assert (await self._log(tools, "XYZ"))["status"] == "ok"

    async def test_negative_leverage_alone_marks_an_inverse_fund(
        self, db, tools, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            tools,
            "_config",
            _asset(primary_ticker="XYZ", instruments={"ZYX": InstrumentInfo(leverage=-1.0)}),
        )
        assert (await self._log(tools, "ZYX"))["status"] == "error"

    async def test_another_instrument_is_its_own_record(self, db, tools) -> None:
        """A second instrument with its own thesis keeps its own row."""
        assert (await self._log(tools, "ABC"))["status"] == "ok"

    async def test_with_no_settings_bound_nothing_is_refused(self, db, tools, monkeypatch) -> None:
        monkeypatch.setattr(tools, "_config", None)
        assert (await self._log(tools, "ZYX"))["status"] == "ok"


def test_history_scenarios_load_one_instrument(tmp_path) -> None:
    from evotrader.scenarios.from_history import load_snapshots

    path = tmp_path / "history.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE market_snapshots (timestamp TEXT, ticker TEXT, close_price REAL, "
            "composite_signal REAL, regime TEXT, regime_confidence REAL, "
            "sub_signals_json TEXT, indicators_json TEXT)"
        )
        conn.executemany(
            "INSERT INTO market_snapshots (timestamp, ticker, close_price, composite_signal) "
            "VALUES (?,?,?,?)",
            [
                (_T.isoformat(), "XYZ", 100.0, 0.2),
                ((_T + timedelta(minutes=5)).isoformat(), "ZYX", 20.0, -0.3),
            ],
        )
    assert list(load_snapshots(str(path), "XYZ")["ticker"]) == ["XYZ"]
    assert len(load_snapshots(str(path))) == 2
