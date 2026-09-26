"""A cold volume profile fills itself from the broker, for any ticker.

Operator, 2026-09-23: "Can we backfill the volume from past 2 week's data?"

The time-of-day volume profile (tools/session_volume.py) needs 10 prior
sessions before it reads anything, so a fresh profile — today's MSTR one, or any
future instrument switch — would sit silent for two weeks.

Filled from the SAME broker call the live cycle uses for today's bars
(`get_equity_historicals`, 5-minute, regular session). Not from the backtest's
cached file: that comes from a different data vendor, and the profile compares
today's volume with past volume at the same minute — any difference in how two
vendors count volume would become a standing bias in every reading until those
sessions rolled out of the 20-session window. Same source, like for like.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evotrader.tools.market_hours import ET

_NOW = datetime(2026, 9, 24, 14, 30, tzinfo=UTC)  # 10:30 ET Thursday


def _trading_days_before(day: datetime, n: int) -> list[datetime]:
    out, d = [], day
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return sorted(out)


def _bars(
    days: list[datetime], per_bar: float = 1000.0, *, pre_and_post: bool = False
) -> list[dict]:
    bars = []
    for d in days:
        open_et = d.astimezone(ET).replace(hour=9, minute=30, second=0, microsecond=0)
        start = open_et - timedelta(minutes=60) if pre_and_post else open_et
        count = 78 + (24 if pre_and_post else 0)
        for i in range(count):
            ts = (start + timedelta(minutes=5 * i)).astimezone(UTC)
            bars.append(
                {
                    "timestamp": ts.isoformat(),
                    "open": 1,
                    "high": 1,
                    "low": 1,
                    "close": 1,
                    "volume": per_bar,
                }
            )
    return bars


class _Broker:
    def __init__(self, bars: list[dict] | Exception) -> None:
        self.bars, self.calls = bars, []

    async def __call__(self, ticker: str, start: datetime, end: datetime) -> list[dict]:
        self.calls.append((ticker, start, end))
        if isinstance(self.bars, Exception):
            raise self.bars
        return [b for b in self.bars if start <= datetime.fromisoformat(b["timestamp"]) < end]


class TestAColdProfileIsFilled:
    async def test_it_fills_the_prior_sessions_and_reads_immediately(self, tmp_path: Path) -> None:
        """THE CASE. Empty profile -> backfilled -> the very next read works,
        instead of abstaining for ten sessions."""
        from evotrader.tools.session_volume import (
            ensure_warm_profile,
            session_relative_volume,
            sessions_in_profile,
        )

        broker = _Broker(_bars(_trading_days_before(_NOW, 25)))
        out = await ensure_warm_profile(tmp_path, "MSTR", broker, now=_NOW)
        assert out["status"] == "backfilled"
        assert out["added"] == 20, "the profile keeps 20 sessions"
        assert sessions_in_profile(tmp_path, "MSTR") == 20

        today = _bars([_NOW])[:12]  # 09:30-10:25
        r = session_relative_volume(tmp_path, "MSTR", today, now=_NOW)
        assert r["value"] == pytest.approx(1.0) and r["sessions"] == 20

    async def test_it_asks_for_enough_history_and_stops_before_today(self, tmp_path: Path) -> None:
        from evotrader.tools.session_volume import ensure_warm_profile

        broker = _Broker(_bars(_trading_days_before(_NOW, 25)))
        await ensure_warm_profile(tmp_path, "MSTR", broker, now=_NOW)
        (_, start, end) = broker.calls[0]
        assert end == _NOW.astimezone(ET).replace(hour=9, minute=30, second=0, microsecond=0)
        assert (end - start).days >= 28, "20 sessions need ~4 calendar weeks plus holidays"

    async def test_today_is_never_backfilled(self, tmp_path: Path) -> None:
        from evotrader.tools.session_volume import _load_profile, ensure_warm_profile

        broker = _Broker(_bars(_trading_days_before(_NOW, 12) + [_NOW]))
        await ensure_warm_profile(tmp_path, "MSTR", broker, now=_NOW)
        assert (
            _NOW.astimezone(ET).date().isoformat()
            not in _load_profile(tmp_path, "MSTR")["sessions"]
        )

    async def test_sessions_already_recorded_live_are_kept(self, tmp_path: Path) -> None:
        from evotrader.tools.session_volume import (
            _load_profile,
            ensure_warm_profile,
            record_session_curve,
        )

        live_day = _trading_days_before(_NOW, 1)[0]
        record_session_curve(tmp_path, "MSTR", _bars([live_day], per_bar=7.0), now=live_day)
        broker = _Broker(_bars(_trading_days_before(_NOW, 25), per_bar=1000.0))
        await ensure_warm_profile(tmp_path, "MSTR", broker, now=_NOW)
        kept = _load_profile(tmp_path, "MSTR")["sessions"][
            live_day.astimezone(ET).date().isoformat()
        ]
        assert kept["570"] == 7.0, "a live-recorded session must not be overwritten"

    async def test_only_regular_session_bars_count(self, tmp_path: Path) -> None:
        """Pre- and after-market bars are not in today's live curve, so they must
        not be in the baseline either."""
        from evotrader.tools.session_volume import _load_profile, ensure_warm_profile

        broker = _Broker(_bars(_trading_days_before(_NOW, 12), pre_and_post=True))
        await ensure_warm_profile(tmp_path, "MSTR", broker, now=_NOW)
        curve = next(iter(_load_profile(tmp_path, "MSTR")["sessions"].values()))
        minutes = sorted(int(m) for m in curve)
        assert minutes[0] == 570 and minutes[-1] < 960  # 09:30 .. 15:55 ET


class TestItIsCheapAndSafe:
    async def test_a_warm_profile_makes_no_broker_call(self, tmp_path: Path) -> None:
        from evotrader.tools.session_volume import ensure_warm_profile

        await ensure_warm_profile(
            tmp_path, "MSTR", _Broker(_bars(_trading_days_before(_NOW, 25))), now=_NOW
        )
        second = _Broker([])
        out = await ensure_warm_profile(tmp_path, "MSTR", second, now=_NOW + timedelta(days=1))
        assert out["status"] == "warm" and second.calls == []

    async def test_at_most_one_attempt_per_day(self, tmp_path: Path) -> None:
        """If the broker returns little, do not hammer it every cycle."""
        from evotrader.tools.session_volume import ensure_warm_profile

        broker = _Broker([])
        await ensure_warm_profile(tmp_path, "MSTR", broker, now=_NOW)
        out = await ensure_warm_profile(tmp_path, "MSTR", broker, now=_NOW + timedelta(hours=1))
        assert out["status"] == "already_attempted_today" and len(broker.calls) == 1

    async def test_it_retries_the_next_day(self, tmp_path: Path) -> None:
        from evotrader.tools.session_volume import ensure_warm_profile

        broker = _Broker([])
        await ensure_warm_profile(tmp_path, "MSTR", broker, now=_NOW)
        await ensure_warm_profile(tmp_path, "MSTR", broker, now=_NOW + timedelta(days=1))
        assert len(broker.calls) == 2

    async def test_a_broker_failure_never_raises(self, tmp_path: Path) -> None:
        from evotrader.tools.session_volume import ensure_warm_profile

        out = await ensure_warm_profile(
            tmp_path, "MSTR", _Broker(RuntimeError("MCP down")), now=_NOW
        )
        assert out["added"] == 0

    async def test_the_attempt_marker_survives_a_live_recording(self, tmp_path: Path) -> None:
        """record_session_curve rewrites the file every cycle; it must keep the
        marker, or the once-a-day guard is silently lost."""
        from evotrader.tools.session_volume import (
            _load_profile,
            ensure_warm_profile,
            record_session_curve,
        )

        await ensure_warm_profile(tmp_path, "MSTR", _Broker([]), now=_NOW)
        record_session_curve(tmp_path, "MSTR", _bars([_NOW])[:12], now=_NOW)
        assert _load_profile(tmp_path, "MSTR").get("backfill_attempted") == "2026-09-24"


class TestItIsGeneric:
    async def test_each_ticker_has_its_own_profile(self, tmp_path: Path) -> None:
        """An instrument switch starts — and fills — a fresh profile rather than
        inheriting another instrument's volume scale."""
        from evotrader.tools.session_volume import ensure_warm_profile, sessions_in_profile

        await ensure_warm_profile(
            tmp_path, "MSTR", _Broker(_bars(_trading_days_before(_NOW, 25), 1000.0)), now=_NOW
        )
        await ensure_warm_profile(
            tmp_path, "QQQ", _Broker(_bars(_trading_days_before(_NOW, 25), 50_000.0)), now=_NOW
        )
        assert sessions_in_profile(tmp_path, "MSTR") == sessions_in_profile(tmp_path, "QQQ") == 20

    def test_the_market_data_tool_warms_the_profile_before_reading_it(self) -> None:
        src = Path("src/evotrader/agents/tools.py").read_text()
        assert "ensure_warm_profile(" in src
        assert src.index("ensure_warm_profile(") < src.index("srv = session_relative_volume("), (
            "the backfill must land before the first read, or that cycle abstains for nothing"
        )

    def test_it_uses_the_same_broker_call_as_the_live_bars(self) -> None:
        """Same tool, same interval, same (default regular) session bounds."""
        src = Path("src/evotrader/agents/tools.py").read_text()
        i = src.index("async def _fetch_session_bars")
        body = src[i : i + 1500]
        assert '"get_equity_historicals"' in body and '"interval": "5minute"' in body
        assert '"bounds"' not in body, "the live call uses the default regular bounds too"


class TestBarsWithoutATimestampCannotCorruptTheProfile:
    """_parse_hist_candles invents a timestamp for a bar that has none, one DAY
    apart per bar. Grouped by session, that would turn one day of 5-minute bars
    into hundreds of one-bar 'sessions' — the profile would count as warm and
    never read, silently, for four weeks."""

    def test_the_default_is_unchanged_for_existing_callers(self) -> None:
        from evotrader.agents.tools import _parse_hist_candles

        raw = {"data": {"results": [{"bars": [{"close_price": 1, "volume": 5}]}]}}
        out = _parse_hist_candles(raw, datetime(2026, 9, 24, tzinfo=UTC))
        assert len(out) == 1 and out[0]["timestamp"]

    def test_the_session_fetch_drops_untimestamped_bars(self) -> None:
        from evotrader.agents.tools import _parse_hist_candles

        raw = {
            "data": {
                "results": [
                    {
                        "bars": [
                            {"begins_at": "2026-09-23T13:30:00Z", "close_price": 1, "volume": 5},
                            {"close_price": 1, "volume": 5},
                        ]
                    }
                ]
            }
        }
        out = _parse_hist_candles(raw, datetime(2026, 9, 24, tzinfo=UTC), require_timestamp=True)
        assert [c["timestamp"] for c in out] == ["2026-09-23T13:30:00Z"]

    def test_the_backfill_fetch_requests_it(self) -> None:
        src = Path("src/evotrader/agents/tools.py").read_text()
        i = src.index("async def _fetch_session_bars")
        assert "require_timestamp=True" in src[i : i + 1500]


class TestTheBaselineDoesNotShrinkWhenTodayIsRecorded:
    """Live, 2026-09-24: 20 sessions were backfilled, then every cycle read
    `session_relative_volume_sessions: 19`. Recording today's curve trimmed the
    file to 20 dates INCLUDING today, dropping the oldest prior session, so the
    first read of the day averaged 20 sessions and every later read 19 — the
    baseline moved mid-session for no market reason."""

    async def test_every_read_of_the_day_averages_the_full_lookback(self, tmp_path: Path) -> None:
        from evotrader.tools.session_volume import (
            LOOKBACK_SESSIONS,
            ensure_warm_profile,
            record_session_curve,
            session_relative_volume,
        )

        await ensure_warm_profile(
            tmp_path, "MSTR", _Broker(_bars(_trading_days_before(_NOW, 25))), now=_NOW
        )
        today = _bars([_NOW])
        seen = []
        for n_bars, hour in ((12, 14), (24, 15), (48, 17)):  # 10:30, 11:30, 13:30 ET
            now = _NOW.replace(hour=hour)
            seen.append(
                session_relative_volume(tmp_path, "MSTR", today[:n_bars], now=now)["sessions"]
            )
            record_session_curve(tmp_path, "MSTR", today[:n_bars], now=now)
        assert seen == [LOOKBACK_SESSIONS] * 3

    async def test_the_file_still_keeps_only_the_window(self, tmp_path: Path) -> None:
        from evotrader.tools.session_volume import (
            LOOKBACK_SESSIONS,
            record_session_curve,
            sessions_in_profile,
        )

        days = _trading_days_before(_NOW, 30) + [_NOW]
        for d in days:
            record_session_curve(tmp_path, "MSTR", _bars([d]), now=d)
        assert sessions_in_profile(tmp_path, "MSTR") == LOOKBACK_SESSIONS + 1, (
            "the lookback's prior sessions plus the session in progress"
        )
