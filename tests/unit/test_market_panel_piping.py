"""Regression tests: every field the console's market panel shows survives a page reload.

The panel is drawn from two sources: the live market-data response during a
cycle, and ``market_snapshots`` (via /api/chart/tech) on every page load. Four
fields reached the first and never the second, so they vanished on reload
(found 2026-09-25, operator asked "are all signals correctly piped?"):

* ``vwap_dist`` — stored in its own column, never in ``indicators_json``, so
  "VWAP Dist" read "--" after every reload;
* ``algo_version`` — not stored at all, so the Strategies tab could not say
  which version produced the numbers;
* ``daily_change_pct`` / ``gap_pct`` — not stored (migration 0022 adds them).
"""

from __future__ import annotations

import pytest

from evotrader.db.thought_log import ThoughtLogger

# Shape of a real gather_market_data response (2026-09-24 15:30 ET, trimmed).
_LIVE_RESPONSE = {
    "ticker": "MSTR",
    "timestamp": "2026-09-24T19:30:05+00:00",
    "quote": {"ticker": "MSTR", "last": 163.165, "previous_close": 162.20},
    "indicators": {
        "rsi_14": 67.364559,
        "vwap": 161.71608481220696,
        "vwap_anchor": "current_session",
        "ibs": 0.7117,
        "atr_14": 9.213119,
        "relative_volume": 0.813607,
        "session_relative_volume": 0.7198,
        "session_relative_volume_sessions": 19,
    },
    "regime": {"regime": "trending_bull", "confidence": 0.85, "reasoning": "x"},
    "daily_change_pct": 0.5949,
    "gap_pct": -0.857,
    "algo_signal": {
        "composite_signal": 0.1815,
        "algo_version": "composite_v029_momentum_divergence_threshold_in_instrument_units",
        "sub_signals": [
            {"name": "momentum", "value": 0.5613, "weight": 0.2045, "metadata": {}},
            {"name": "mean_reversion", "value": -0.2328, "weight": 0.129, "metadata": {}},
        ],
    },
}


@pytest.fixture
async def stored(db):
    logger = ThoughtLogger(db)
    await logger.record_event(
        session_id="81ff74ee",
        agent_name="orchestrator",
        event_type="tool_response",
        content="gather_market_data",
        meta={"response": _LIVE_RESPONSE},
    )
    points = await logger.get_market_snapshots(limit=5)
    assert len(points) == 1
    return points[0]


class TestAReloadedPanelHasWhatTheLiveOneHad:
    async def test_vwap_distance(self, stored) -> None:
        assert stored["indicators"]["vwap_dist"] == pytest.approx(
            (163.165 - 161.71608481220696) / 161.71608481220696
        ), "a fraction, as the panel expects (it multiplies by 100)"

    async def test_algorithm_version(self, stored) -> None:
        assert stored["algo_version"].startswith("composite_v029_")

    async def test_day_change_and_gap_in_percent(self, stored) -> None:
        assert stored["daily_change_pct"] == pytest.approx(0.5949)
        assert stored["gap_pct"] == pytest.approx(-0.857)

    async def test_what_already_worked_still_does(self, stored) -> None:
        assert stored["composite_signal"] == pytest.approx(0.1815)
        assert stored["regime"] == "trending_bull"
        assert [s["name"] for s in stored["sub_signals"]] == ["momentum", "mean_reversion"]
        assert stored["indicators"]["vwap_anchor"] == "current_session"
        assert stored["indicators"]["session_relative_volume"] == pytest.approx(0.7198)
        assert stored["close"] == pytest.approx(163.165)


async def test_rows_written_before_the_migration_still_load(db) -> None:
    async with db.connection() as conn:
        await conn.execute(
            "INSERT INTO market_snapshots (timestamp, session_id, ticker, close_price, vwap, "
            "vwap_dist, composite_signal, regime, indicators_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "2026-09-20T14:30:00+00:00",
                "old",
                "MSTR",
                150.0,
                149.0,
                0.0067,
                0.1,
                "trending_bull",
                '{"rsi_14": 60.0}',
            ),
        )
        await conn.commit()
    point = (await ThoughtLogger(db).get_market_snapshots(limit=1))[0]
    assert point["algo_version"] is None and point["daily_change_pct"] is None
    assert point["indicators"]["vwap_dist"] == pytest.approx(0.0067)
