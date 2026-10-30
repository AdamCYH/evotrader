"""Earnings report dates for the event context, from every source that has them.

The event-window channel reads how many hours until, or since, the nearest
earnings report of what is traded (``hours_to_event``, ``hours_since_event``).
Its one source was the broker's market-wide earnings calendar, and every way
that can fail (a rate-limit payload, an empty list, no row for the symbol, a
date that does not parse) ended the same silent way: the channel read
``event_context_not_available``. A market-wide calendar can list no row for
the traded stock through the very days before its report, while the news
agent's tools return the date every cycle: the broker's own earnings results
(the report date, its timing and whether it is verified), a research
provider's earnings calendar and an events tool. The channel was then blind
through the window it was written for.

So the dates are gathered here:

* ``dates_from_rows`` and ``dates_from_reply`` read report dates out of the
  replies that carry them: the broker's earnings calendar and earnings
  results, a research earnings calendar (CSV) and an events tool
  (``earnings.next_date``);
* ``remember`` keeps them in a small file and ``recall`` reads them back, so a
  date once seen still resolves through an outage, and after the print, when
  a forward calendar has dropped it;
* ``known_events`` reads the operator's ``asset.known_events``;
* ``nearest`` picks the next report and the latest one past.

A report with a date and no time is placed at 16:00 ET, after the close, or at
08:00 ET when its timing says it comes before the open.

Which sources the event context uses is the active algorithm version's choice
(``composite.event_dates_every_source``); the operator's ``known_events`` always
count. ``note_reply`` records the dates the agents' tools return whatever the
version, so a version that turns the sources on starts with them.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STATE_FILENAME = "event_dates.json"
#: A date stays this long after it passes: the post-event window is 48 hours,
#: and a weekend sits inside a week.
KEEP_AFTER = timedelta(days=7)
#: The broker's earnings results are asked again for a symbol after this long.
RECHECK_AFTER = timedelta(hours=12)
#: At most this many symbols' earnings results are read per cycle, in the
#: order the universe lists them.
MAX_RESULTS_SYMBOLS = 15
#: Timings that put a report before the open.
_BEFORE_OPEN = {"am", "bmo", "pre-market", "pre_market", "premarket", "before market open"}


def _state_path(data_dir: Path) -> Path:
    return Path(data_dir) / "trading" / STATE_FILENAME


def _now() -> datetime:
    return datetime.now(UTC)


def report_time(day: date, timing: str | None = None) -> datetime:
    """When a report on ``day`` lands: 16:00 ET, or 08:00 ET before the open."""
    from evotrader.tools.market_hours import ET

    hour = 8 if str(timing or "").strip().lower() in _BEFORE_OPEN else 16
    return datetime.combine(day, time(hour), tzinfo=ET).astimezone(UTC)


def _when(raw: Any, timing: str | None = None) -> datetime | None:
    """A report time from an ISO datetime or a date, in UTC; None if it does not parse."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if "T" in text:
            when = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return (when if when.tzinfo else when.replace(tzinfo=UTC)).astimezone(UTC)
        return report_time(date.fromisoformat(text[:10]), timing)
    except ValueError:
        return None


def dates_from_rows(rows: list[Any], source: str) -> tuple[list[dict[str, Any]], int]:
    """Report dates from earnings rows: ``(dates, rows_whose_date_did_not_parse)``.

    Reads the broker's calendar rows (``report_date``, ``expected_report_date``
    or ``date``) and its earnings results (``report: {date, timing}``). A row
    with an actual EPS is a report already made (``reported`` true).
    """
    dates: list[dict[str, Any]] = []
    unparsed = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or row.get("ticker") or "").upper()
        if not symbol:
            continue
        report = row.get("report") if isinstance(row.get("report"), dict) else {}
        timing = report.get("timing") or row.get("timing") or row.get("time")
        raw = next(
            (
                v
                for v in (
                    row.get("report_date"),
                    row.get("expected_report_date"),
                    report.get("date"),
                    row.get("date"),
                )
                if v
            ),
            None,
        )
        when = _when(raw, timing)
        if when is None:
            unparsed += 1
            continue
        eps = row.get("eps") if isinstance(row.get("eps"), dict) else {}
        actual = eps.get("actual") if eps else row.get("eps_actual")
        dates.append(
            {
                "symbol": symbol,
                "report_dt": when,
                "reported": actual is not None,
                "source": source,
            }
        )
    return dates, unparsed


def calendar_rows(raw: Any) -> list[Any]:
    """The rows of a broker earnings reply: ``data.results``, or a list under ``data``."""
    if not isinstance(raw, dict):
        return []
    data = raw.get("data")
    if isinstance(data, dict):
        rows = data.get("results")
        return rows if isinstance(rows, list) else []
    if isinstance(data, list):
        return data
    rows = raw.get("results")  # get_earnings_history's own shape
    return rows if isinstance(rows, list) else []


def _csv_dates(text: str, source: str) -> list[dict[str, Any]]:
    """A research earnings calendar: ``symbol,...,reportDate,...,timeOfTheDay`` rows."""
    dates = []
    for row in csv.DictReader(io.StringIO(text.strip())):
        symbol = str(row.get("symbol") or "").upper()
        when = _when(row.get("reportDate"), row.get("timeOfTheDay"))
        if symbol and when is not None:
            dates.append({"symbol": symbol, "report_dt": when, "reported": False, "source": source})
    return dates


def _reply_texts(result: Any) -> list[str]:
    """The text blocks of an MCP reply, whatever shape it has."""
    content = (
        result.get("content") if isinstance(result, dict) else getattr(result, "content", None)
    )
    texts = []
    for block in content if isinstance(content, list) else []:
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if isinstance(text, str) and text.strip():
            texts.append(text)
    return texts


#: The tools whose replies carry report dates, by the name their server gives them.
_DATE_TOOLS = {
    "get_earnings_calendar": "broker_calendar",
    "get_earnings_results": "broker_earnings_results",
    "EARNINGS_CALENDAR": "research_calendar",
    "get_events": "events_tool",
}


def dates_from_reply(tool: str, args: dict | None, result: Any) -> list[dict[str, Any]]:
    """Report dates in one tool reply (an MCP result or a parsed payload). Never raises."""
    from evotrader.mcp.response_curators import effective_tool_name

    try:
        name = effective_tool_name(tool, args)
        source = _DATE_TOOLS.get(name)
        if source is None:
            return []
        payloads: list[Any] = []
        if isinstance(result, dict) and ("data" in result or "earnings" in result):
            payloads.append(result)
        for text in _reply_texts(result):
            stripped = text.lstrip()
            if stripped.startswith(("{", "[")):
                try:
                    payloads.append(json.loads(stripped))
                except ValueError:
                    continue
            elif name == "EARNINGS_CALENDAR":
                payloads.append(stripped)
        dates: list[dict[str, Any]] = []
        for payload in payloads:
            if isinstance(payload, str):
                dates += _csv_dates(payload, source)
            elif name == "get_events" and isinstance(payload, dict):
                earnings = payload.get("earnings") or {}
                when = _when(earnings.get("next_date")) if isinstance(earnings, dict) else None
                symbol = str(payload.get("symbol") or (args or {}).get("symbol") or "").upper()
                if when is not None and symbol:
                    dates.append(
                        {"symbol": symbol, "report_dt": when, "reported": False, "source": source}
                    )
            else:
                dates += dates_from_rows(calendar_rows(payload), source)[0]
        return dates
    except Exception as e:  # a date we could not read costs nothing; a broken tool call would
        logger.debug("Could not read report dates from %s: %s", tool, e)
        return []


def _load(data_dir: Path) -> dict[str, Any]:
    path = _state_path(data_dir)
    try:
        if path.is_file():
            state = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                return state
    except Exception as e:
        logger.warning("Event dates unreadable at %s: %s", path, e)
    return {}


def _save(data_dir: Path, state: dict[str, Any]) -> None:
    path = _state_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    except Exception as e:
        logger.warning("Could not record event dates: %s", e)


def remember(
    data_dir: Path,
    dates: list[dict[str, Any]],
    *,
    now: datetime,
    checked: tuple[str, ...] = (),
) -> None:
    """Keep report dates seen now; ``checked`` names symbols whose earnings results were read.

    One entry per symbol and report day: the latest sighting wins (its time,
    source and whether it was reported). Dates past ``KEEP_AFTER`` are dropped.
    """
    state = _load(data_dir)
    symbols = state.setdefault("symbols", {})
    for d in dates:
        entry = symbols.setdefault(d["symbol"], {"dates": {}})
        day = d["report_dt"].astimezone(UTC).date().isoformat()
        entry["dates"][day] = {
            "report_dt": d["report_dt"].astimezone(UTC).isoformat(),
            "source": d["source"],
            "reported": bool(d.get("reported")),
            "seen_at": now.astimezone(UTC).isoformat(),
        }
    for symbol in checked:
        symbols.setdefault(symbol, {"dates": {}})["checked_at"] = now.astimezone(UTC).isoformat()
    for entry in symbols.values():
        dates_kept = entry.get("dates") or {}
        for day in [
            k
            for k, v in dates_kept.items()
            if now - datetime.fromisoformat(v["report_dt"]) > KEEP_AFTER
        ]:
            del dates_kept[day]
    _save(data_dir, state)


def recall(data_dir: Path, symbols: set[str] | frozenset[str]) -> list[dict[str, Any]]:
    """Every remembered report date of these symbols, marked ``remembered``."""
    state = _load(data_dir)
    out = []
    for symbol in symbols:
        for v in ((state.get("symbols") or {}).get(symbol) or {}).get("dates", {}).values():
            try:
                when = datetime.fromisoformat(v["report_dt"])
            except (KeyError, TypeError, ValueError):
                continue
            out.append(
                {
                    "symbol": symbol,
                    "report_dt": when,
                    "reported": bool(v.get("reported")),
                    "source": f"remembered:{v.get('source', '?')}",
                }
            )
    return out


def needs_check(data_dir: Path, symbol: str, now: datetime) -> bool:
    """Whether the broker's earnings results for ``symbol`` are due a read."""
    entry = ((_load(data_dir).get("symbols") or {}).get(symbol)) or {}
    try:
        checked = datetime.fromisoformat(entry["checked_at"])
    except (KeyError, TypeError, ValueError):
        return True
    return now - checked >= RECHECK_AFTER


def known_events(config: Any, symbols: set[str] | frozenset[str]) -> list[dict[str, Any]]:
    """The operator's ``asset.known_events`` for these symbols."""
    events = getattr(
        getattr(getattr(config, "settings", None), "asset", None), "known_events", None
    )
    out = []
    for ev in events or []:
        symbol = str(getattr(ev, "symbol", "") or "").upper()
        when = getattr(ev, "at", None)
        if symbol not in symbols or when is None:
            continue
        if when.tzinfo is None:
            from evotrader.tools.market_hours import ET

            when = when.replace(tzinfo=ET)
        out.append(
            {
                "symbol": symbol,
                "report_dt": when.astimezone(UTC),
                "reported": False,
                "source": "known_events",
                "type": getattr(ev, "type", "earnings") or "earnings",
            }
        )
    return out


def nearest(
    dates: list[dict[str, Any]], now: datetime
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The next report and the latest one past, over every symbol and source.

    A report time already passed is past, whatever its row said. A future date
    that carries an actual EPS is a row that cannot be read, and is skipped.
    On a tie a source read this cycle is preferred to a remembered one.
    """
    upcoming = [d for d in dates if d["report_dt"] > now and not d.get("reported")]
    past = [d for d in dates if d["report_dt"] <= now]

    def remembered(d: dict[str, Any]) -> bool:
        return str(d.get("source", "")).startswith("remembered")

    nxt = min(upcoming, key=lambda d: (d["report_dt"], remembered(d))) if upcoming else None
    last = max(past, key=lambda d: (d["report_dt"], not remembered(d))) if past else None
    return nxt, last


def note_reply(tool: str, args: dict | None, result: Any, config: Any) -> int:
    """Remember the report dates an agent's tool returned, for the traded symbols.

    Called on every MCP reply; returns how many dates it kept. Never raises.
    """
    if config is None or getattr(config, "data_dir", None) is None:
        return 0
    try:
        dates = dates_from_reply(tool, args, result)
        if not dates:
            return 0
        from evotrader.tools.universe import cached_relevant_symbols, trading_tickers

        relevant = set(cached_relevant_symbols(config)) | set(trading_tickers(config))
        kept = [d for d in dates if d["symbol"] in relevant]
        if kept:
            remember(config.data_dir, kept, now=_now())
            logger.info(
                "Event dates from %s: %s",
                tool,
                ", ".join(f"{d['symbol']} {d['report_dt']:%Y-%m-%d %H:%M}Z" for d in kept),
            )
        return len(kept)
    except Exception as e:
        logger.debug("Event dates from %s not recorded: %s", tool, e)
        return 0
