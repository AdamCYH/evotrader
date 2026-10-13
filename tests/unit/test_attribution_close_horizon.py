"""Intraday calls are scored at the close of their own session.

Found by the evolution agent's code review. The scorer measured every call at
the next session's close and the fifth's. Half the channels are intraday:
their targets resolve, or fail, by the same day's close, and measured a day
later they read the next session's move instead. Each evolution session
counted the intraday calls "to the close" by hand, and none of it reached the
calibration record.

``forward_return_close`` is the return from ``price_at_decision`` to the close
of the decision's own session (US Eastern). A decision at or after that close,
or on a day the market is shut, has none. It resolves once the session's daily
bar is out, a session before the 1-day horizon, and leaves ``scored_at`` alone.
``get_signal_calibration(horizon="close")`` scores it.

Made-up ticker, dates and prices.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from evotrader.db.signal_attribution import SignalAttributionStore
from evotrader.evolution import tools as evo
from evotrader.evolution.attribution_scorer import (
    AttributionScorer,
    close_horizon_session,
    same_session_close,
)
from evotrader.tools.market_hours import ET

WEDNESDAY = date(2026, 3, 4)  # US Eastern standard time: UTC-5


def _et(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)


def _bars(*closes: tuple[date, float]) -> list[dict]:
    return [{"timestamp": f"{d.isoformat()}T05:00:00+00:00", "close": c} for d, c in closes]


BARS = _bars((date(2026, 3, 3), 102.0), (WEDNESDAY, 98.0))


class TestWhichSessionACallIsMeasuredTo:
    @pytest.mark.parametrize(
        ("when", "session"),
        [
            (_et(WEDNESDAY, 8, 30), WEDNESDAY),  # pre-market: the close is still ahead
            (_et(WEDNESDAY, 11, 30), WEDNESDAY),
            (_et(WEDNESDAY, 15, 59), WEDNESDAY),
            (_et(WEDNESDAY, 16, 0), None),  # at the close: nothing ahead
            (_et(WEDNESDAY, 17, 0), None),  # the after-hours cycle
            (_et(date(2026, 6, 10), 15, 59), date(2026, 6, 10)),  # daylight time: UTC-4
            (_et(date(2026, 6, 10), 16, 0), None),
            (_et(date(2026, 11, 27), 12, 30), date(2026, 11, 27)),  # a 1 pm close
            (_et(date(2026, 11, 27), 13, 30), None),
            (_et(date(2026, 3, 7), 11, 0), None),  # a Saturday
            (_et(date(2026, 7, 3), 11, 0), None),  # a market holiday
        ],
    )
    def test_its_own_session_or_none(self, when, session) -> None:
        assert close_horizon_session(when) == session
        assert close_horizon_session(when.astimezone(UTC).isoformat()) == session

    def test_the_close_is_read_only_once_the_session_is_over(self) -> None:
        assert same_session_close(BARS, WEDNESDAY, _et(WEDNESDAY, 18)) == 98.0
        assert same_session_close(BARS, WEDNESDAY, _et(WEDNESDAY, 16, 30)) is None, (
            "a bar still forming is not the close"
        )
        assert same_session_close(BARS[:1], WEDNESDAY, _et(WEDNESDAY, 18)) is None


class TestTheScorerWritesIt:
    @pytest.fixture
    def store(self, db) -> SignalAttributionStore:
        return SignalAttributionStore(db)

    async def _rows(self, store: SignalAttributionStore) -> dict[str, int]:
        ids = {}
        for label, when, price in (
            ("08:30", _et(WEDNESDAY, 8, 30), 101.0),
            ("11:30", _et(WEDNESDAY, 11, 30), 100.0),
            ("15:30", _et(WEDNESDAY, 15, 30), 99.0),
            ("17:00", _et(WEDNESDAY, 17, 0), 98.5),
        ):
            ids[label] = await store.record(
                "XYZ",
                algo_direction=1.0,
                price_at_decision=price,
                timestamp=when.astimezone(UTC).isoformat(),
            )
        return ids

    async def _columns(self, store, row_id: int) -> dict:
        async with store._db.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT forward_return_close, forward_return_1d, scored_at "
                    "FROM signal_attribution WHERE id = ?",
                    (row_id,),
                )
            ).fetchone()
        return dict(row)

    async def test_each_call_to_its_own_close(self, store) -> None:
        ids = await self._rows(store)

        async def fetch(ticker, now):
            return BARS

        scorer = AttributionScorer(store, fetch)
        out = await scorer.backfill_close_horizon(now=_et(date(2026, 3, 5), 9))
        assert out == {"examined": 4, "filled": 3, "waiting": 0, "no_close": 1}
        got = {k: (await self._columns(store, v))["forward_return_close"] for k, v in ids.items()}
        assert got["08:30"] == pytest.approx((98 / 101 - 1) * 100, abs=1e-4)
        assert got["11:30"] == pytest.approx(-2.0)
        assert got["15:30"] == pytest.approx((98 / 99 - 1) * 100, abs=1e-4)
        assert got["17:00"] is None, "decided after the close: no close ahead of it"
        assert (await self._columns(store, ids["11:30"]))["scored_at"] is None, (
            "the 1-day horizon is not touched"
        )

    async def test_before_the_bar_is_out_it_waits(self, store) -> None:
        await self._rows(store)

        async def fetch(ticker, now):
            return BARS[:1]  # the daily series still ends at the prior session

        out = await AttributionScorer(store, fetch).backfill_close_horizon(
            now=_et(date(2026, 3, 5), 9)
        )
        assert (out["filled"], out["waiting"], out["no_close"]) == (0, 3, 1)

    async def test_the_next_session_still_scores_the_one_day_horizon(self, store) -> None:
        ids = await self._rows(store)

        async def fetch(ticker, now):
            return [*BARS, *_bars((date(2026, 3, 5), 103.0))]

        out = await AttributionScorer(store, fetch).run(now=_et(date(2026, 3, 6), 9))
        assert (out["close_filled"], out["scored"]) == (3, 4)
        row = await self._columns(store, ids["11:30"])
        assert row["forward_return_close"] == pytest.approx(-2.0)
        assert row["forward_return_1d"] == pytest.approx(3.0)


class TestTheCalibrationReadsIt:
    async def test_close_rows_count_before_the_one_day_horizon(self, db, monkeypatch) -> None:
        store = SignalAttributionStore(db)
        for i, minute in enumerate((30, 31, 32)):
            row_id = await store.record(
                "XYZ",
                algo_direction=1.0,
                price_at_decision=100.0,
                timestamp=_et(WEDNESDAY, 11, minute).astimezone(UTC).isoformat(),
            )
            await store.score_close(row_id, -1.0 - i)
        monkeypatch.setattr(evo, "_attribution_store", store)
        close = await evo.get_signal_calibration(lookback_days=3650, horizon="close")
        assert (close["horizon"], close["n_scored"]) == ("forward_return_close", 3)
        assert close["base_rate"]["mean_return_pct"] == pytest.approx(-2.0)
        one_day = await evo.get_signal_calibration(lookback_days=3650, horizon="1d")
        assert one_day["n_scored"] == 0, "none of them has its next session yet"

    async def test_waiting_counts_only_calls_that_will_get_a_close(self, db, monkeypatch) -> None:
        store = SignalAttributionStore(db)

        async def pending(limit=500, now=None, window_days=120):
            return [
                {"timestamp": _et(WEDNESDAY, 11, 30).astimezone(UTC).isoformat(), "ticker": "XYZ"},
                {"timestamp": _et(WEDNESDAY, 17, 0).astimezone(UTC).isoformat(), "ticker": "XYZ"},
            ]

        monkeypatch.setattr(store, "pending_close_horizon", pending)
        monkeypatch.setattr(evo, "_attribution_store", store)
        out = await evo.get_signal_calibration(lookback_days=30, horizon="close")
        assert out["n_waiting_on_horizon"] == 1


async def test_the_column_exists(db) -> None:
    async with db.connection() as conn:
        cursor = await conn.execute("PRAGMA table_info(signal_attribution)")
        assert "forward_return_close" in {r["name"] for r in await cursor.fetchall()}
