"""The event context finds an earnings date wherever one is, and says where.

Found by the evolution agent's code review. The event-window channel's one
source was the broker's market-wide earnings calendar, and every way it can
fail (a rate-limit payload, an empty list, no row for the symbol, a date that
does not parse) ended silently as "no event data": the calendar can list
nothing for the traded stock in the days before its report, while the
broker's earnings results, a research calendar and an events tool all have
the date. Now:

* ``event_context`` on the indicators says what the calendar gave and which
  date, from which source, was used;
* an algorithm version that sets ``composite.event_dates_every_source`` also
  reads the broker's earnings results (at most twice a day per symbol) and
  every date seen before, here or in an agent's tool reply, so a date
  resolves through an outage and after the print;
* the operator's ``asset.known_events`` count whatever the version.

Made-up ticker, dates and payloads.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from evotrader.agents import tools
from evotrader.config import AppConfig
from evotrader.models.config import KnownEvent
from evotrader.tools import event_calendar

# 2026-03-05 is a Thursday in US Eastern standard time: 16:00 ET = 21:00 UTC.
REPORT = datetime(2026, 3, 5, 21, 0, tzinfo=UTC)
BEFORE = datetime(2026, 3, 4, 15, 0, tzinfo=UTC)  # 30 hours ahead
MORNING_AFTER = datetime(2026, 3, 6, 13, 30, tzinfo=UTC)  # 16.5 hours after

CALENDAR = {"data": {"results": [{"symbol": "OTHER", "report_date": "2026-03-05"}]}}
RESULTS = {
    "data": {
        "results": [
            {
                "symbol": "XYZ",
                "eps": {"estimate": "0.90", "actual": "1.10"},
                "report": {"date": "2025-12-04", "timing": "pm", "verified": True},
            },
            {
                "symbol": "XYZ",
                "eps": {"estimate": "1.00", "actual": None},
                "report": {"date": "2026-03-05", "timing": "pm", "verified": True},
            },
            {
                "symbol": "XYZ",
                "eps": {"estimate": "1.20", "actual": None},
                "report": {"date": "2026-06-04", "timing": "pm", "verified": False},
            },
        ]
    }
}


class Registry:
    def __init__(self, composite: dict) -> None:
        self._params = {"composite": composite}

    def load_parameters(self) -> dict:
        return self._params


@pytest.fixture
def broker(tmp_path, monkeypatch):
    """The event context bound to a scratch data folder and a stand-in broker."""
    config = AppConfig(data_dir=tmp_path / "data")
    config.settings.asset.primary_ticker = "XYZ"
    calls: list[str] = []
    replies = {"get_earnings_calendar": CALENDAR, "get_earnings_results": RESULTS}

    async def call_mcp(tool: str, args: dict):
        calls.append(tool)
        return replies.get(tool)

    async def universe(_config=None):
        return ("XYZ",)

    monkeypatch.setattr(tools, "_config", config)
    monkeypatch.setattr(tools, "_algo_registry", Registry({"event_dates_every_source": True}))
    monkeypatch.setattr(tools, "_call_mcp_tool", call_mcp)
    monkeypatch.setattr(tools, "relevant_symbols", universe)
    monkeypatch.setattr(event_calendar, "_now", lambda: BEFORE)  # an agent's reply, now
    return config, calls, replies


async def _enrich(now: datetime) -> dict:
    indicators: dict = {}
    await tools._enrich_event_context(indicators, "XYZ", now)
    return indicators


class TestTheSources:
    async def test_off_the_calendar_alone_and_it_says_why(self, broker, monkeypatch) -> None:
        _, calls, _ = broker
        monkeypatch.setattr(tools, "_algo_registry", Registry({}))
        ind = await _enrich(BEFORE)
        assert "hours_to_event" not in ind
        diag = ind["event_context"]
        assert (diag["calendar"], diag["calendar_rows"], diag["calendar_matched"]) == (
            "no_symbol_match",
            1,
            0,
        )
        assert diag["every_source"] is False and diag["source"] is None
        assert "get_earnings_results" not in calls

    async def test_on_the_earnings_results_give_the_date(self, broker) -> None:
        ind = await _enrich(BEFORE)
        assert ind["hours_to_event"] == pytest.approx(30.0)
        assert ind["event_type"] == "earnings"
        assert ind["hours_since_event"] > 24 * 80, "the December report, long past"
        diag = ind["event_context"]
        assert diag["source"] == "broker_earnings_results"
        assert diag["nearest_report_dt"] == REPORT.isoformat()

    async def test_a_date_once_seen_survives_an_outage_and_the_print(self, broker) -> None:
        _, _, replies = broker
        await _enrich(BEFORE)
        replies.clear()  # the broker answers nothing at all
        ind = await _enrich(MORNING_AFTER)
        assert ind["hours_since_event"] == pytest.approx(16.5)
        assert ind["hours_to_event"] > 24 * 80, "the June date, unverified, still next"
        assert ind["event_context"]["calendar"] == "calendar_empty"
        assert ind["event_context"]["source"].startswith("remembered:")

    async def test_the_results_are_read_twice_a_day_at_most(self, broker) -> None:
        _, calls, _ = broker
        await _enrich(BEFORE)
        await _enrich(BEFORE + timedelta(hours=1))
        assert calls.count("get_earnings_results") == 1
        await _enrich(BEFORE + event_calendar.RECHECK_AFTER)
        assert calls.count("get_earnings_results") == 2

    async def test_a_failed_read_is_asked_again(self, broker) -> None:
        _, calls, replies = broker
        replies.pop("get_earnings_results")
        await _enrich(BEFORE)
        await _enrich(BEFORE + timedelta(hours=1))
        assert calls.count("get_earnings_results") == 2

    async def test_known_events_count_whatever_the_version(self, broker, monkeypatch) -> None:
        config, _, _ = broker
        monkeypatch.setattr(tools, "_algo_registry", Registry({}))
        config.settings.asset.known_events = [
            KnownEvent(symbol="XYZ", at=datetime(2026, 3, 5, 16, 0)),
            KnownEvent(symbol="ELSE", at=datetime(2026, 3, 4, 16, 0)),
        ]
        ind = await _enrich(BEFORE)
        assert ind["hours_to_event"] == pytest.approx(30.0)
        assert ind["event_context"]["source"] == "known_events"
        assert ind["event_context"]["known_events"] == 1


class TestTheAgentsReplies:
    def test_a_research_calendar_and_an_events_tool_are_remembered(self, broker) -> None:
        config, _, _ = broker
        csv_reply = {
            "content": [
                {
                    "type": "text",
                    "text": "symbol,name,reportDate,fiscalDateEnding,estimate,currency,"
                    "timeOfTheDay\r\nXYZ,XYZ CORP,2026-03-05,2025-12-31,1.0,USD,post-market\r\n"
                    "ABC,ABC CORP,2026-03-05,2025-12-31,1.0,USD,pre-market\r\n",
                }
            ]
        }
        assert event_calendar.note_reply("EARNINGS_CALENDAR", {}, csv_reply, config) == 1
        events_reply = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"symbol": "XYZ", "earnings": {"next_date": "2026-06-04"}}),
                }
            ]
        }
        assert event_calendar.note_reply("get_events", {"symbol": "XYZ"}, events_reply, config) == 1
        assert event_calendar.note_reply("get_news", {}, csv_reply, config) == 0
        dates = sorted(d["report_dt"] for d in event_calendar.recall(config.data_dir, {"XYZ"}))
        assert dates == [REPORT, datetime(2026, 6, 4, 20, 0, tzinfo=UTC)]  # June: daylight time

    def test_the_dispatcher_name_is_resolved(self, broker) -> None:
        config, _, _ = broker
        reply = {
            "content": [
                {"type": "text", "text": "symbol,reportDate,timeOfTheDay\nXYZ,2026-03-05,\n"}
            ]
        }
        args = {"tool_name": "EARNINGS_CALENDAR", "arguments": {}}
        assert event_calendar.note_reply("TOOL_CALL", args, reply, config) == 1

    def test_a_reply_it_cannot_read_costs_nothing(self, broker) -> None:
        config, _, _ = broker
        assert (
            event_calendar.note_reply("get_events", {}, {"content": [{"text": "{oops"}]}, config)
            == 0
        )
        assert event_calendar.note_reply("get_events", {}, None, config) == 0
        assert event_calendar.note_reply("get_events", {}, {}, None) == 0

    async def test_the_mcp_wrapper_hands_every_reply_over(self, broker, monkeypatch) -> None:
        from google.adk.tools.mcp_tool.mcp_tool import McpTool

        from evotrader.agents.factory import SanitizedMcpTool

        config, _, _ = broker
        reply = {"content": [{"type": "text", "text": json.dumps(RESULTS)}]}

        async def parent_run(self, *, args, tool_context):
            return reply

        monkeypatch.setattr(McpTool, "run_async", parent_run)
        tool = object.__new__(SanitizedMcpTool)
        tool.name = "get_earnings_results"
        tool._mcp_tool = None
        tool._app_config = config
        assert await tool.run_async(args={"symbol": "XYZ"}, tool_context=None) is reply
        assert {d["report_dt"] for d in event_calendar.recall(config.data_dir, {"XYZ"})} >= {REPORT}


class TestTheDates:
    def test_timing_places_a_date_only_report(self) -> None:
        rows = [
            {"symbol": "xyz", "report": {"date": "2026-03-05", "timing": "am"}},
            {"symbol": "XYZ", "report_date": "2026-03-05"},
            {"symbol": "XYZ", "date": "2026-03-05T18:30:00Z"},
            {"symbol": "XYZ", "report_date": "soon"},
            {"symbol": "", "report_date": "2026-03-05"},
        ]
        dates, unparsed = event_calendar.dates_from_rows(rows, "test")
        assert [d["report_dt"].strftime("%H:%M") for d in dates] == ["13:00", "21:00", "18:30"]
        assert {d["symbol"] for d in dates} == {"XYZ"}
        assert unparsed == 1

    def test_the_next_is_the_earliest_unreported_date(self) -> None:
        def d(day: int, reported: bool = False, source: str = "a") -> dict:
            when = datetime(2026, 3, day, 21, tzinfo=UTC)
            return {"symbol": "XYZ", "report_dt": when, "reported": reported, "source": source}

        now = datetime(2026, 3, 10, tzinfo=UTC)
        nxt, last = event_calendar.nearest(
            [d(20), d(12), d(11, reported=True), d(5), d(9, source="remembered:a"), d(9)], now
        )
        assert nxt["report_dt"].day == 12
        assert (last["report_dt"].day, last["source"]) == (9, "a")
        assert event_calendar.nearest([], now) == (None, None)

    def test_old_dates_are_dropped(self, tmp_path) -> None:
        when = {"symbol": "XYZ", "report_dt": REPORT, "reported": False, "source": "a"}
        event_calendar.remember(tmp_path, [when], now=REPORT)
        assert event_calendar.recall(tmp_path, {"XYZ"})
        event_calendar.remember(
            tmp_path, [], now=REPORT + event_calendar.KEEP_AFTER + timedelta(hours=1)
        )
        assert event_calendar.recall(tmp_path, {"XYZ"}) == []

    def test_an_unreadable_state_file_reads_as_empty(self, tmp_path) -> None:
        path = tmp_path / "trading" / event_calendar.STATE_FILENAME
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        assert event_calendar.recall(tmp_path, {"XYZ"}) == []
        assert event_calendar.needs_check(tmp_path, "XYZ", REPORT) is True

    def test_report_time(self) -> None:
        assert event_calendar.report_time(date(2026, 3, 5)).hour == 21
        assert event_calendar.report_time(date(2026, 3, 5), "pre-market").hour == 13
