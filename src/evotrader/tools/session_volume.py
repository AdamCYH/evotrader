"""Today's relative volume, measured against the same time of day.

`indicators.relative_volume` is computed from the DAILY series, and the last
daily bar's volume does not update during the session — so it is the PRIOR
session's ratio and holds one constant all day (1.5236 on every cycle of
2026-09-22, 0.8959 on every cycle of 2026-09-23, from 08:30 pre-market to 17:00).
Three channels read it as "is today's trading busy right now". It cannot say.

This module answers that question with the instrument's own history:

    session_relative_volume = today's cumulative volume up to the latest bar
                              / mean cumulative volume at that same minute
                                over prior sessions

Matching the minute matters: volume is U-shaped through the day, heavy at the
open and the close. Dividing by "the fraction of the day elapsed" would read
every morning as unusually busy.

Why a stored profile rather than a fetch every cycle: each cycle already has
today's session bars (`recent_candles`), so recording the curve costs nothing,
whereas fetching multi-session five-minute history every cycle would add a
broker call each time. The profile needs `MIN_SESSIONS` before it reads
anything; a cold profile (first run, or a change of instrument) is filled ONCE
from the broker by `ensure_warm_profile` — the same call and the same session
bounds the live cycle uses for today's bars, so past and present are counted
the same way. Until the profile is warm the value is None and the caller keeps
the daily value, labelled as such.

Nothing here is specific to an instrument: the profile is per ticker and in the
ticker's own shares, so a switch of instrument starts a fresh, correct profile
rather than inheriting a wrong one.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Sessions a reading must be averaged over before it is reported.
MIN_SESSIONS = 10
#: Most recent sessions averaged over, and kept on disk.
LOOKBACK_SESSIONS = 20
#: Regular session in minutes after midnight ET: bars starting 09:30 up to 15:55.
_SESSION_OPEN_MIN, _SESSION_CLOSE_MIN = 9 * 60 + 30, 16 * 60

#: ``(ticker, start, end) -> 5-minute bars``, the broker call the live cycle uses.
BarFetcher = Callable[[str, datetime, datetime], Awaitable[list[dict[str, Any]]]]


def _profile_path(data_dir: Path, ticker: str) -> Path:
    return Path(data_dir) / "trading" / "session_volume" / f"{ticker.upper()}.json"


def _to_et(value: Any) -> datetime | None:
    from evotrader.tools.market_hours import ET

    if isinstance(value, datetime):
        ts = value
    elif value:
        try:
            ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(ET)


def _bar(candle: Any) -> tuple[datetime | None, float]:
    """(ET timestamp, volume) for a candle given as a dict or a model."""
    if isinstance(candle, dict):
        ts, vol = candle.get("timestamp"), candle.get("volume")
    else:
        ts, vol = getattr(candle, "timestamp", None), getattr(candle, "volume", None)
    try:
        v = float(vol) if vol is not None else 0.0
    except (TypeError, ValueError):
        v = 0.0
    return _to_et(ts), v


def cumulative_curve(candles: list[Any], session_date: str) -> dict[int, float]:
    """``{minute_of_day: cumulative volume through the bar starting then}``.

    Only regular-session bars dated ``session_date`` (ET) count: a stray bar
    from another session, or a pre/after-market bar, would put volume into the
    baseline that today's live curve never contains.
    """
    bars = []
    for c in candles or []:
        et, vol = _bar(c)
        if et is None or et.date().isoformat() != session_date:
            continue
        minute = et.hour * 60 + et.minute
        if _SESSION_OPEN_MIN <= minute < _SESSION_CLOSE_MIN:
            bars.append((minute, vol))
    bars.sort()
    curve: dict[int, float] = {}
    running = 0.0
    for minute, vol in bars:
        running += vol
        curve[minute] = running
    return curve


def _load_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"sessions": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        logger.warning("Session volume profile unreadable at %s: %s", path, e)
        return {"sessions": {}}
    data.setdefault("sessions", {})
    return data


def _load_profile(data_dir: Path, ticker: str) -> dict[str, Any]:
    """The whole profile file: ``sessions`` plus any bookkeeping keys."""
    return _load_file(_profile_path(data_dir, ticker))


def _load(path: Path) -> dict[str, dict[str, float]]:
    return _load_file(path)["sessions"]


def _save(path: Path, data: dict[str, Any]) -> None:
    sessions = data.get("sessions", {})
    # The lookback's prior sessions PLUS the session in progress. Keeping only
    # LOOKBACK_SESSIONS dates let recording today evict the oldest prior one,
    # so the day's first read averaged 20 sessions and every later read 19
    # (live 2026-09-24: `session_relative_volume_sessions: 19` all day).
    keep = sorted(sessions)[-(LOOKBACK_SESSIONS + 1) :]
    out = {**data, "sessions": {d: sessions[d] for d in keep}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out), encoding="utf-8")


def record_session_curve(
    data_dir: Path, ticker: str, candles: list[Any], now: datetime | None = None
) -> bool:
    """Store today's curve, replacing any earlier record of the same session.

    Called every cycle; each call carries a longer curve than the last, so the
    session's entry is overwritten rather than appended. Returns False, never
    raises, if it could not write.
    """
    from evotrader.tools.market_hours import ET

    session_date = (now or datetime.now(UTC)).astimezone(ET).date().isoformat()
    curve = cumulative_curve(candles, session_date)
    if not curve:
        return False
    try:
        path = _profile_path(data_dir, ticker)
        data = _load_file(path)
        data["sessions"][session_date] = {str(m): v for m, v in curve.items()}
        _save(path, data)
        return True
    except Exception as e:
        logger.warning("Could not record session volume for %s: %s", ticker, e)
        return False


def session_relative_volume(
    data_dir: Path,
    ticker: str,
    candles: list[Any],
    now: datetime | None = None,
    min_sessions: int = MIN_SESSIONS,
    lookback: int = LOOKBACK_SESSIONS,
) -> dict[str, Any]:
    """Today's cumulative volume against prior sessions at the same minute.

    Returns ``{"value", "sessions", "minute"}``; ``value`` is None while fewer
    than ``min_sessions`` prior sessions reached this minute, or when there are
    no regular-session bars yet (the 08:30 and 17:00 cycles).
    """
    from evotrader.tools.market_hours import ET

    session_date = (now or datetime.now(UTC)).astimezone(ET).date().isoformat()
    today = cumulative_curve(candles, session_date)
    if not today:
        return {"value": None, "sessions": 0, "minute": None}
    minute = max(today)
    key = str(minute)

    prior = [
        float(curve[key])
        for date, curve in sorted(_load(_profile_path(data_dir, ticker)).items())
        if date < session_date and key in curve and float(curve[key]) > 0
    ][-lookback:]
    if len(prior) < min_sessions:
        return {"value": None, "sessions": len(prior), "minute": minute}
    baseline = sum(prior) / len(prior)
    return {"value": round(today[minute] / baseline, 4), "sessions": len(prior), "minute": minute}


def sessions_in_profile(data_dir: Path, ticker: str) -> int:
    """How many sessions the profile holds for ``ticker``."""
    return len(_load(_profile_path(data_dir, ticker)))


async def ensure_warm_profile(
    data_dir: Path,
    ticker: str,
    fetch_bars: BarFetcher,
    now: datetime | None = None,
    min_sessions: int = MIN_SESSIONS,
    lookback: int = LOOKBACK_SESSIONS,
) -> dict[str, Any]:
    """Fill a cold profile with prior sessions from the broker.

    A fresh profile — the first run, or any change of instrument — would
    otherwise stay silent for ``min_sessions`` trading days. This fetches the
    prior ``lookback`` sessions of 5-minute bars through ``fetch_bars`` (the
    same broker call the live cycle uses for today's bars, so the baseline and
    today are counted the same way) and records each one.

    * No-op, and no broker call, once the profile has enough prior sessions.
    * At most one attempt per ticker per day, remembered in the profile file,
      so a broker that returns little is not asked again every cycle.
    * Never overwrites a session already recorded live, never records today,
      and never raises.
    """
    from evotrader.tools.market_hours import ET

    now = now or datetime.now(UTC)
    today = now.astimezone(ET).date().isoformat()
    path = _profile_path(data_dir, ticker)
    data = _load_file(path)
    sessions = data["sessions"]
    prior = [d for d in sessions if d < today]
    if len(prior) >= min_sessions:
        return {"status": "warm", "added": 0, "sessions": len(prior)}
    if data.get("backfill_attempted") == today:
        return {"status": "already_attempted_today", "added": 0, "sessions": len(prior)}

    end = now.astimezone(ET).replace(hour=9, minute=30, second=0, microsecond=0)
    # Five trading sessions per calendar week, plus a margin for holidays.
    start = end - timedelta(days=int(lookback * 7 / 5) + 7)
    try:
        bars = await fetch_bars(ticker, start, end) or []
    except Exception as e:
        logger.warning("Volume profile backfill for %s failed: %s", ticker, e)
        bars = []

    dates = sorted(
        {
            et.date().isoformat()
            for et, _ in (_bar(b) for b in bars)
            if et is not None and et.date().isoformat() < today
        }
    )
    added = 0
    for date in dates[-lookback:]:
        if date in sessions:
            continue
        curve = cumulative_curve(bars, date)
        if curve:
            sessions[date] = {str(m): v for m, v in curve.items()}
            added += 1

    data["backfill_attempted"] = today
    try:
        _save(path, data)
    except Exception as e:
        logger.warning("Could not save backfilled volume profile for %s: %s", ticker, e)
        return {"status": "save_failed", "added": 0, "sessions": len(prior)}
    total = len([d for d in data["sessions"] if d < today])
    logger.info(
        "Volume profile for %s: backfilled %d session(s) from the broker, %d prior now held",
        ticker,
        added,
        total,
    )
    return {"status": "backfilled", "added": added, "sessions": total}
