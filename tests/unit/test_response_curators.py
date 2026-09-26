"""Tests for MCP response curation.

Trade quality is the priority these tests defend. Two things matter more than
the size reduction:

1. Nothing decision-relevant is lost — the curated payload carries statistics
   computed from the *whole* history, so an agent can still say "this is a
   two-year high" after the old rows are gone.
2. Nothing is mislabelled. A summary that calls a five-month CPI move "1w"
   would be worse than no summary at all, because the agent would reason
   confidently from a number that means something else.
"""

from __future__ import annotations

import json

from evotrader.mcp.response_curators import (
    DEFAULT_TIMESERIES_ROWS,
    apply_backstop,
    curate_earnings_calendar,
    curate_news_feed,
    curate_response,
    curate_timeseries,
    dedupe_structured_content,
    effective_tool_name,
    has_curator,
)


def _daily_csv(days: int, start_value: float = 4.0) -> str:
    """Newest-first daily series, as these feeds return them."""
    from datetime import date, timedelta

    today = date(2026, 7, 24)
    rows = ["timestamp,value"]
    for i in range(days):
        rows.append(f"{today - timedelta(days=i)},{start_value + i * 0.001:.4f}")
    return "\r".join(rows)


def _monthly_csv(months: int) -> str:
    """Newest-first monthly series, like CPI."""
    rows = ["timestamp,value"]
    year, month = 2026, 5
    for i in range(months):
        rows.append(f"{year:04d}-{month:02d}-01,{300.0 + i * 0.5:.3f}")
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return "\r".join(rows)


def _mcp_result(text: str, structured: dict | None = None) -> dict:
    result: dict = {"content": [{"type": "text", "text": text}], "isError": False}
    if structured is not None:
        result["structuredContent"] = structured
    return result


def _curated(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])["curated"]


# ── Dispatch ──────────────────────────────────────────────────────


def test_registered_tools_are_recognised():
    assert has_curator("FEDERAL_FUNDS_RATE")
    assert has_curator("TREASURY_YIELD")
    assert has_curator("CPI")


def test_unregistered_tools_pass_through_untouched():
    """Curation is opt-in per tool — nothing is guessed at."""
    assert not has_curator("gather_option_chain")

    payload = _mcp_result(_daily_csv(5000))
    before = json.dumps(payload)
    curate_response("gather_option_chain", {}, payload)

    assert json.dumps(payload) == before


def test_alpha_vantage_tool_call_dispatcher_is_unwrapped():
    """The real function name hides in args["tool_name"] behind TOOL_CALL."""
    assert effective_tool_name("TOOL_CALL", {"tool_name": "FEDERAL_FUNDS_RATE"}) == (
        "FEDERAL_FUNDS_RATE"
    )
    assert effective_tool_name("TOOL_CALL", {}) == "TOOL_CALL"
    assert effective_tool_name("CPI", None) == "CPI"

    payload = _mcp_result(_daily_csv(3000))
    curate_response("TOOL_CALL", {"tool_name": "FEDERAL_FUNDS_RATE"}, payload)

    assert "curated" in payload["content"][0]["text"]


# ── Trimming ──────────────────────────────────────────────────────


def test_short_series_is_left_alone():
    text = _daily_csv(30)
    assert curate_timeseries(text) == text


def test_long_series_is_trimmed_to_the_newest_rows():
    payload = _mcp_result(_daily_csv(20_000))
    curate_response("FEDERAL_FUNDS_RATE", {}, payload)

    info = _curated(payload)
    assert info["observations_kept"] == DEFAULT_TIMESERIES_ROWS
    assert info["observations_available"] == 20_000
    # Newest row must survive — it is the current reading.
    assert "2026-07-24" in json.loads(payload["content"][0]["text"])["data"]


def test_trim_is_announced_so_the_agent_knows_it_is_a_window():
    payload = _mcp_result(_daily_csv(20_000))
    curate_response("CPI", {}, payload)

    note = _curated(payload)["note"]
    assert "most recent" in note
    assert "summary" in note.lower()


def test_reduction_is_large():
    payload = _mcp_result(_daily_csv(26_000))
    before = len(json.dumps(payload))
    curate_response("FEDERAL_FUNDS_RATE", {}, payload)

    assert len(json.dumps(payload)) < before / 50


# ── The part that makes trimming safe: full-history summary ────────


def test_summary_is_computed_from_the_whole_history_not_the_window():
    """Without this, trimming would genuinely lose decision-relevant context."""
    from datetime import date, timedelta

    today = date(2026, 7, 24)
    rows = ["timestamp,value"]
    for i in range(5000):
        # A spike far outside the retained window.
        value = 22.36 if i == 4000 else 3.6
        rows.append(f"{today - timedelta(days=i)},{value}")
    payload = _mcp_result("\r".join(rows))

    curate_response("FEDERAL_FUNDS_RATE", {}, payload)
    summary = _curated(payload)["summary"]

    assert summary["observations"] == 5000
    assert summary["full_history_extremes"]["max"]["value"] == 22.36
    assert summary["latest"] == {"date": "2026-07-24", "value": 3.6}


def test_change_is_reported_for_a_daily_series():
    payload = _mcp_result(_daily_csv(2000))
    curate_response("FEDERAL_FUNDS_RATE", {}, payload)

    change = _curated(payload)["summary"]["change"]
    assert set(change) == {"1w", "1m", "3m", "1y"}


def test_monthly_series_does_not_claim_a_weekly_change():
    """The bug this guards: index-based lookbacks labelled 5 months as "1w"."""
    payload = _mcp_result(_monthly_csv(400))
    curate_response("CPI", {}, payload)

    change = _curated(payload)["summary"]["change"]
    assert "1w" not in change, "a monthly series cannot resolve a one-week change"
    assert "1m" in change
    assert "1y" in change


def test_change_is_measured_by_calendar_distance():
    payload = _mcp_result(_daily_csv(800, start_value=4.0))
    curate_response("TREASURY_YIELD", {}, payload)

    change = _curated(payload)["summary"]["change"]
    # Series ascends by 0.001 per day going back, so latest minus 365-days-ago
    # is about -0.365.
    assert change["1y"] == -0.365
    assert change["1w"] == -0.007


def test_lookbacks_beyond_the_series_are_omitted_not_guessed():
    payload = _mcp_result(_daily_csv(200))
    curate_response("FEDERAL_FUNDS_RATE", {}, payload)

    change = _curated(payload)["summary"]["change"]
    assert "1y" not in change
    assert "3m" in change


def test_last_year_extremes_are_reported_separately():
    payload = _mcp_result(_daily_csv(3000))
    curate_response("FEDERAL_FUNDS_RATE", {}, payload)

    summary = _curated(payload)["summary"]
    assert "last_1y_extremes" in summary
    assert summary["last_1y_extremes"] != summary["full_history_extremes"]


def test_missing_value_markers_do_not_break_the_summary():
    """These feeds use '.' for a missing observation."""
    rows = ["timestamp,value", "2026-05-01,335.123", "2026-04-01,.", "2026-03-01,330.213"]
    rows += [f"2020-{m:02d}-01,300.0" for m in range(1, 13)] * 40
    payload = _mcp_result("\r".join(rows))

    curate_response("CPI", {}, payload)
    summary = _curated(payload)["summary"]

    assert summary["latest"]["value"] == 335.123
    assert isinstance(summary["full_history_extremes"]["min"]["value"], float)


# ── Envelopes and mirrors ─────────────────────────────────────────


def test_alpha_vantage_preview_envelope_is_curated():
    envelope = json.dumps(
        {
            "preview": True,
            "data_type": "csv",
            "total_lines": 16829,
            "sample_lines": 3796,
            "sample_data": _daily_csv(3796),
        }
    )
    payload = _mcp_result(envelope)
    curate_response("TREASURY_YIELD", {}, payload)

    info = _curated(payload)
    # Reported against the envelope's true total, not just the sample.
    assert info["observations_available"] == 16829
    assert info["observations_kept"] == DEFAULT_TIMESERIES_ROWS


def test_identical_mirror_is_deduplicated_before_curation():
    """Half of a 1MB response lives in the structuredContent duplicate."""
    csv = _daily_csv(20_000)
    payload = _mcp_result(csv, structured={"result": csv})
    before = len(json.dumps(payload))

    curate_response("FEDERAL_FUNDS_RATE", {}, payload)

    # Dedupe removes the mirror outright, so curation only has to shape `content`.
    assert "_deduplicated" in payload["structuredContent"]
    assert len(json.dumps(payload)) < before / 100


def test_differing_mirror_is_curated_rather_than_dropped():
    """When the mirror is not a byte-identical copy, dedupe declines — so the
    curator has to shape it too, or the full series survives in the duplicate."""
    csv = _daily_csv(20_000)
    payload = _mcp_result(csv, structured={"result": csv, "extra": "not in content"})

    curate_response("FEDERAL_FUNDS_RATE", {}, payload)

    assert "_deduplicated" not in payload["structuredContent"]
    assert payload["structuredContent"]["extra"] == "not in content"
    assert len(payload["structuredContent"]["result"]) < len(csv) / 50


def test_differing_preview_envelope_mirror_is_curated():
    sample = _daily_csv(3796)
    payload = _mcp_result(
        json.dumps({"preview": True, "sample_data": sample, "total_lines": 16829}),
        # `sample_lines` only in the mirror, so dedupe declines and the curator runs.
        structured={
            "preview": True,
            "sample_data": sample,
            "total_lines": 16829,
            "sample_lines": 3796,
        },
    )
    curate_response("TREASURY_YIELD", {}, payload)

    assert len(payload["structuredContent"]["sample_data"]) < len(sample) / 20
    assert "curated" in payload["structuredContent"]


# ── Robustness: never break a tool call ───────────────────────────


def test_a_curator_failure_returns_the_payload_unchanged(monkeypatch):
    import evotrader.mcp.response_curators as mod

    def boom(_text: str) -> str:
        raise RuntimeError("curator exploded")

    monkeypatch.setitem(mod._CURATORS, "CPI", boom)
    payload = _mcp_result(_daily_csv(5000))
    before = json.dumps(payload)

    result = curate_response("CPI", {}, payload)

    assert json.dumps(result) == before


def test_non_text_blocks_are_ignored():
    payload = {"content": [{"type": "image", "data": "abc"}]}
    assert curate_response("CPI", {}, payload) is payload


def test_empty_and_malformed_results_are_safe():
    for payload in ({}, {"content": []}, {"content": None}, {"content": [{}]}):
        curate_response("CPI", {}, payload)  # must not raise


def test_non_timeseries_text_passes_through():
    text = json.dumps({"some": "unrelated payload"})
    assert curate_timeseries(text) == text


# ── Deduplication: the biggest lossless win ───────────────────────


def test_identical_structured_content_is_dropped():
    """MCP servers send every payload twice; the model reads and pays for both."""
    data = {"data": {"results": [{"symbol": "AAPL"}] * 500}}
    text = json.dumps(data)
    payload = _mcp_result(text, structured=data)
    before = len(json.dumps(payload))

    removed = dedupe_structured_content(payload)

    assert removed > 0
    assert payload["structuredContent"] == {
        "_deduplicated": "Identical to the text content above; omitted to avoid "
        "sending the same payload twice."
    }
    # The text block itself is untouched — nothing is actually lost.
    assert payload["content"][0]["text"] == text
    assert len(json.dumps(payload)) < before / 1.8


def test_single_key_wrapper_is_recognised():
    """Some servers wrap a raw payload: {"result": "<the csv>"}."""
    csv = _daily_csv(50)
    payload = _mcp_result(csv, structured={"result": csv})

    assert dedupe_structured_content(payload) > 0
    assert "_deduplicated" in payload["structuredContent"]


def test_single_key_wrapper_of_parsed_json_is_recognised():
    data = {"a": [1, 2, 3]}
    payload = _mcp_result(json.dumps(data), structured={"result": data})

    assert dedupe_structured_content(payload) > 0


def test_differing_structured_content_is_never_dropped():
    """Safety comes from the equality check, not from trusting the tool."""
    payload = _mcp_result(json.dumps({"a": 1}), structured={"a": 1, "extra": "only here"})

    assert dedupe_structured_content(payload) == 0
    assert payload["structuredContent"] == {"a": 1, "extra": "only here"}


def test_subtly_differing_content_is_never_dropped():
    payload = _mcp_result(json.dumps({"value": 1.0}), structured={"value": 1.5})

    assert dedupe_structured_content(payload) == 0


def test_no_structured_content_is_a_noop():
    payload = _mcp_result(json.dumps({"a": 1}))
    assert dedupe_structured_content(payload) == 0


def test_multiple_text_blocks_are_left_alone():
    """Ambiguous which block the mirror corresponds to — don't guess."""
    payload = {
        "content": [
            {"type": "text", "text": json.dumps({"a": 1})},
            {"type": "text", "text": json.dumps({"b": 2})},
        ],
        "structuredContent": {"a": 1},
    }
    assert dedupe_structured_content(payload) == 0


def test_dedupe_runs_even_for_tools_with_no_curator():
    """It applies everywhere, including the trading path, because it is verified."""
    assert not has_curator("get_equity_orders")
    data = {"orders": [{"id": i} for i in range(300)]}
    payload = _mcp_result(json.dumps(data), structured=data)
    before = len(json.dumps(payload))

    curate_response("get_equity_orders", {}, payload)

    assert len(json.dumps(payload)) < before / 1.8


def test_dedupe_and_curation_stack():
    csv = _daily_csv(20_000)
    payload = _mcp_result(csv, structured={"result": csv})
    before = len(json.dumps(payload))

    curate_response("FEDERAL_FUNDS_RATE", {}, payload)

    assert len(json.dumps(payload)) < before / 100


# ── News feed pruning ─────────────────────────────────────────────


def _news(articles: int = 5, tickers: tuple[str, ...] = ("QQQ", "NVDA", "MU")) -> str:
    return json.dumps(
        {
            "items": str(articles),
            "sentiment_score_definition": "x <= -0.35: Bearish; …",
            "relevance_score_definition": "0 < x <= 1 …",
            "feed": [
                {
                    "title": f"Headline {i}",
                    "url": f"https://example.com/{i}",
                    "time_published": f"2026070{i % 9}T120000",
                    "authors": ["A"],
                    "summary": "Something happened. " * 20,
                    "banner_image": "https://example.com/img/" + "x" * 200,
                    "source": "Example",
                    "category_within_source": "News",
                    "source_domain": "example.com",
                    "topics": [{"topic": "Technology", "relevance_score": "0.9"}],
                    "overall_sentiment_score": 0.21,
                    "overall_sentiment_label": "Somewhat-Bullish",
                    "ticker_sentiment": [
                        {
                            "ticker": t,
                            "relevance_score": "0.5",
                            "ticker_sentiment_score": "0.2",
                            "ticker_sentiment_label": "Neutral",
                        }
                        for t in tickers
                    ],
                }
                for i in range(articles)
            ],
        }
    )


def test_news_curator_is_registered():
    assert has_curator("NEWS_SENTIMENT")


def test_decorative_fields_are_dropped_and_signal_is_kept():
    payload = _mcp_result(_news())
    curate_response("NEWS_SENTIMENT", {"tickers": "QQQ"}, payload)
    d = json.loads(payload["content"][0]["text"])

    article = d["feed"][0]
    assert "banner_image" not in article
    assert "category_within_source" not in article
    # Everything the consuming agent's output contract needs must survive.
    for field in (
        "title",
        "url",
        "time_published",
        "source",
        "summary",
        "topics",
        "overall_sentiment_score",
        "overall_sentiment_label",
    ):
        assert field in article, f"{field} must survive curation"
    # The score-interpretation preamble is cheap and genuinely informative.
    assert "sentiment_score_definition" in d


def test_all_articles_are_kept_by_default():
    payload = _mcp_result(_news(articles=50))
    curate_response("NEWS_SENTIMENT", {}, payload)
    d = json.loads(payload["content"][0]["text"])

    assert len(d["feed"]) == 50
    assert d["curated"]["articles_returned"] == 50


def test_ticker_sentiment_is_untouched_by_default():
    """Off by default: dropping an unrelated ticker's row is not provably lossless."""
    payload = _mcp_result(_news())
    curate_response("NEWS_SENTIMENT", {}, payload)
    d = json.loads(payload["content"][0]["text"])

    tickers = [r["ticker"] for r in d["feed"][0]["ticker_sentiment"]]
    assert tickers == ["QQQ", "NVDA", "MU"]


def test_ticker_sentiment_filter_uses_the_resolved_universe():
    """The universe is passed in, never assumed — no ticker is hardcoded here."""
    curated = curate_news_feed(
        _news(tickers=("QQQ", "NVDA", "MU")),
        filter_ticker_sentiment=True,
        relevant_tickers=frozenset({"QQQ", "NVDA"}),
    )
    d = json.loads(curated)

    tickers = [r["ticker"] for r in d["feed"][0]["ticker_sentiment"]]
    assert tickers == ["QQQ", "NVDA"]
    assert d["curated"]["ticker_sentiment_rows_dropped"] > 0
    assert "major holdings" in d["curated"]["note"]


def test_filter_follows_a_different_ticker_without_code_changes():
    """Switching the traded instrument must need no change to the curator."""
    curated = curate_news_feed(
        _news(tickers=("IWM", "SMCI", "NVDA")),
        filter_ticker_sentiment=True,
        relevant_tickers=frozenset({"IWM", "SMCI"}),
    )
    d = json.loads(curated)

    tickers = [r["ticker"] for r in d["feed"][0]["ticker_sentiment"]]
    assert tickers == ["IWM", "SMCI"]


def test_unresolved_universe_keeps_every_ticker_row():
    """Losing signal is worse than carrying tokens — so an empty universe
    disables the filter rather than dropping everything."""
    curated = curate_news_feed(
        _news(tickers=("QQQ", "NVDA", "MU")),
        filter_ticker_sentiment=True,
        relevant_tickers=frozenset(),
    )
    d = json.loads(curated)

    tickers = [r["ticker"] for r in d["feed"][0]["ticker_sentiment"]]
    assert tickers == ["QQQ", "NVDA", "MU"]
    assert "ticker_sentiment_rows_dropped" not in d["curated"]


def test_filter_is_inert_without_an_app_config():
    """curate_response with no AppConfig cannot know the universe, so it must
    not guess one."""
    from evotrader.models.config import CurationConfig

    payload = _mcp_result(_news())
    curate_response(
        "NEWS_SENTIMENT", {}, payload, CurationConfig(news_filter_ticker_sentiment=True)
    )
    d = json.loads(payload["content"][0]["text"])

    tickers = [r["ticker"] for r in d["feed"][0]["ticker_sentiment"]]
    assert tickers == ["QQQ", "NVDA", "MU"]


def test_article_cap_when_configured_is_announced():
    from evotrader.models.config import CurationConfig

    payload = _mcp_result(_news(articles=50))
    curate_response("NEWS_SENTIMENT", {}, payload, CurationConfig(news_max_articles=20))
    d = json.loads(payload["content"][0]["text"])

    assert len(d["feed"]) == 20
    assert d["curated"]["articles_available"] == 50
    assert "Showing the 20 most recent of 50 articles" in d["curated"]["note"]


def test_non_news_json_passes_through():
    payload = _mcp_result(json.dumps({"unrelated": True}))
    before = json.dumps(payload)
    curate_response("NEWS_SENTIMENT", {}, payload)
    assert json.dumps(payload) == before


# ── Config plumbing ───────────────────────────────────────────────


def test_master_switch_disables_everything():
    from evotrader.models.config import CurationConfig

    csv = _daily_csv(20_000)
    payload = _mcp_result(csv, structured={"result": csv})
    before = json.dumps(payload)

    curate_response("FEDERAL_FUNDS_RATE", {}, payload, CurationConfig(enabled=False))

    assert json.dumps(payload) == before


def test_dedupe_can_be_disabled_independently():
    from evotrader.models.config import CurationConfig

    data = {"a": [1] * 500}
    payload = _mcp_result(json.dumps(data), structured=data)

    curate_response("get_accounts", {}, payload, CurationConfig(dedupe_structured_content=False))

    assert payload["structuredContent"] == data


def test_timeseries_row_count_is_configurable():
    from evotrader.models.config import CurationConfig

    payload = _mcp_result(_daily_csv(20_000))
    curate_response("CPI", {}, payload, CurationConfig(timeseries_max_rows=30))

    assert _curated(payload)["observations_kept"] == 30


# ── Earnings calendar: generalized, universe-driven ───────────────


def _calendar(symbols: list[str]) -> str:
    return json.dumps(
        {
            "data": {
                "results": [
                    {
                        "symbol": s,
                        "year": 2026,
                        "quarter": 3,
                        "eps": {"estimate": "1.00", "actual": None},
                        "report": {"date": "2026-07-30", "timing": "pm"},
                    }
                    for s in symbols
                ]
            }
        }
    )


def test_earnings_calendar_curators_are_registered():
    assert has_curator("get_earnings_calendar")
    assert has_curator("EARNINGS_CALENDAR")


def test_calendar_is_filtered_to_the_supplied_universe():
    market = _calendar(["AAPL", "RANDOMCO", "MSFT", "TINYCAP"])
    curated = curate_earnings_calendar(market, relevant_tickers=frozenset({"QQQ", "AAPL", "MSFT"}))
    d = json.loads(curated)

    kept = [r["symbol"] for r in d["data"]["results"]]
    assert kept == ["AAPL", "MSFT"]
    assert d["curated"]["entries_available"] == 4
    assert d["curated"]["entries_kept"] == 2


def test_calendar_filter_follows_a_different_universe():
    """Switch the traded instrument and the filter follows — no code change."""
    market = _calendar(["XOM", "CVX", "AAPL"])
    curated = curate_earnings_calendar(market, relevant_tickers=frozenset({"XLE", "XOM", "CVX"}))

    kept = [r["symbol"] for r in json.loads(curated)["data"]["results"]]
    assert kept == ["XOM", "CVX"]


def test_calendar_with_no_universe_is_untouched():
    """Dropping earnings the strategy needed costs far more than the tokens."""
    market = _calendar(["AAPL", "MSFT"])
    assert curate_earnings_calendar(market, relevant_tickers=frozenset()) is market
    assert curate_earnings_calendar(market, relevant_tickers=None) is market


def test_calendar_with_zero_matches_is_untouched():
    """0 of N matching suggests the shape isn't what we assumed — don't guess."""
    market = _calendar(["AAA", "BBB"])
    out = curate_earnings_calendar(market, relevant_tickers=frozenset({"QQQ", "NVDA"}))
    assert out is market


def test_calendar_handles_a_bare_list_payload():
    market = json.dumps([{"symbol": "AAPL"}, {"symbol": "NOPE"}])
    d = json.loads(curate_earnings_calendar(market, relevant_tickers=frozenset({"AAPL"})))
    assert [r["symbol"] for r in d["results"]] == ["AAPL"]


def test_calendar_handles_a_ticker_key_instead_of_symbol():
    market = json.dumps({"results": [{"ticker": "AAPL"}, {"ticker": "NOPE"}]})
    d = json.loads(curate_earnings_calendar(market, relevant_tickers=frozenset({"AAPL"})))
    assert [r["ticker"] for r in d["results"]] == ["AAPL"]


def test_calendar_non_json_passes_through():
    assert curate_earnings_calendar("not json", relevant_tickers=frozenset({"A"})) == ("not json")


# ── Backstop ──────────────────────────────────────────────────────


def test_backstop_cuts_on_whole_entries():
    payload = json.dumps({"rows": [{"i": i, "pad": "x" * 200} for i in range(500)]})
    trimmed, info = apply_backstop(payload, 5_000)

    assert info is not None
    d = json.loads(trimmed)  # must still be valid JSON
    assert 0 < len(d["rows"]) < 500
    # Every surviving entry is whole, never a fragment.
    assert all(set(row) == {"i", "pad"} for row in d["rows"])
    assert d["_backstop"]["_truncated"] is True
    assert d["_backstop"]["_total"] == 500


def test_backstop_announces_itself():
    payload = json.dumps({"rows": [{"pad": "x" * 200} for _ in range(500)]})
    trimmed, _ = apply_backstop(payload, 5_000)

    note = json.loads(trimmed)["_backstop"]["_note"]
    assert "of 500" in note
    assert "narrow your query" in note


def test_backstop_drops_largest_keys_when_there_is_no_list():
    payload = json.dumps({"small": "a", "huge": "x" * 100_000})
    trimmed, info = apply_backstop(payload, 5_000)

    d = json.loads(trimmed)
    assert info["_keys_dropped"] == ["huge"]
    assert d["small"] == "a"


def test_backstop_leaves_small_payloads_alone():
    payload = json.dumps({"rows": [1, 2, 3]})
    assert apply_backstop(payload, 5_000) == (payload, None)


def test_backstop_will_not_cut_opaque_text():
    """No safe boundary exists in unstructured text, so leave it and log."""
    payload = "x" * 100_000
    trimmed, info = apply_backstop(payload, 5_000)

    assert trimmed == payload
    assert info is None


def test_backstop_disabled_at_zero():
    payload = json.dumps({"rows": [{"pad": "x" * 200} for _ in range(500)]})
    assert apply_backstop(payload, 0) == (payload, None)


def test_backstop_applies_to_research_role_only():
    from evotrader.models.config import CurationConfig

    big = json.dumps({"rows": [{"pad": "x" * 200} for _ in range(500)]})
    config = CurationConfig(backstop_max_chars=5_000, backstop_roles=["research"])

    research = _mcp_result(big)
    curate_response("some_new_tool", {}, research, config, roles=("research",))
    assert "_backstop" in research["content"][0]["text"]

    # The order path is deliberately excluded: a partial position or order list
    # is more dangerous than a large one.
    trading = _mcp_result(big)
    curate_response("get_equity_orders", {}, trading, config, roles=("trading",))
    assert "_backstop" not in trading["content"][0]["text"]


def test_backstop_can_be_opted_into_for_trading():
    from evotrader.models.config import CurationConfig

    big = json.dumps({"rows": [{"pad": "x" * 200} for _ in range(500)]})
    payload = _mcp_result(big)
    curate_response(
        "get_equity_orders",
        {},
        payload,
        CurationConfig(backstop_max_chars=5_000, backstop_roles=["research", "trading"]),
        roles=("trading",),
    )
    assert "_backstop" in payload["content"][0]["text"]


def test_backstop_does_not_touch_a_curated_tool():
    """A tool with a real curator must not also be blindly trimmed."""
    from evotrader.models.config import CurationConfig

    payload = _mcp_result(_daily_csv(20_000))
    curate_response(
        "FEDERAL_FUNDS_RATE",
        {},
        payload,
        CurationConfig(backstop_max_chars=5_000),
        roles=("research",),
    )
    text = payload["content"][0]["text"]
    assert "_backstop" not in text
    assert "curated" in text


# ── News recency window ───────────────────────────────────────────


def _aged_news(ages_in_days: list[int]) -> str:
    """A feed whose articles are the given ages, deliberately out of order."""
    from datetime import datetime, timedelta

    newest = datetime(2026, 7, 24, 12, 0)
    articles = []
    for i, age in enumerate(ages_in_days):
        when = newest - timedelta(days=age)
        articles.append(
            {
                "title": f"Article {i} ({age}d old)",
                "time_published": when.strftime("%Y%m%dT%H%M%S"),
                "summary": "Something happened. " * 20,
                "banner_image": "https://example.com/" + "x" * 200,
                "overall_sentiment_score": 0.1,
                "ticker_sentiment": [{"ticker": "QQQ", "relevance_score": "0.5"}],
            }
        )
    return json.dumps({"feed": articles})


def _titles(curated: str) -> list[str]:
    return [a["title"] for a in json.loads(curated)["feed"]]


def test_age_window_keeps_recent_articles_only():
    curated = curate_news_feed(_aged_news([0, 5, 20, 40, 46]), max_age_days=21)
    kept = _titles(curated)

    assert kept == ["Article 0 (0d old)", "Article 1 (5d old)", "Article 2 (20d old)"]
    d = json.loads(curated)["curated"]
    assert d["articles_available"] == 5
    assert d["articles_returned"] == 3
    assert "older than 21 days" in d["note_detail"]


def test_feed_is_sorted_by_recency_before_trimming():
    """Robust even if the server ignored a sort=LATEST request."""
    curated = curate_news_feed(_aged_news([40, 1, 30, 0, 10]), max_age_days=21)
    kept = _titles(curated)

    assert kept == [
        "Article 3 (0d old)",
        "Article 1 (1d old)",
        "Article 4 (10d old)",
    ]


def test_floor_protects_a_quiet_week():
    """Every article old, but the agent must not be left with nothing."""
    curated = curate_news_feed(_aged_news([30, 35, 40, 45, 50]), max_age_days=7, min_articles=3)
    kept = _titles(curated)

    assert len(kept) == 3
    assert kept[0] == "Article 0 (30d old)", "the newest survive"
    assert "floor of 3" in json.loads(curated)["curated"]["note_detail"]


def test_ceiling_caps_a_busy_week():
    curated = curate_news_feed(_aged_news([0] * 40), max_age_days=21, max_articles=10)
    assert len(_titles(curated)) == 10
    assert "ceiling of 10" in json.loads(curated)["curated"]["note_detail"]


def test_nothing_is_trimmed_when_everything_is_recent():
    curated = curate_news_feed(_aged_news([0, 1, 2]), max_age_days=21)
    d = json.loads(curated)
    assert len(d["feed"]) == 3
    assert "articles_available" not in d["curated"]


def test_reduction_is_announced_with_dates():
    curated = curate_news_feed(_aged_news([0, 5, 40]), max_age_days=21)
    d = json.loads(curated)["curated"]

    assert d["trimmed_by"] == "recency"
    assert d["newest"] == "2026-07-24"
    assert d["oldest_kept"] == "2026-07-19"
    assert "most recent" in d["note"]
    assert "wider window" in d["note"]


def test_unparseable_timestamps_fall_back_to_a_count_ceiling_only():
    """No timestamps means an age window is meaningless — say so, don't guess."""
    feed = json.dumps({"feed": [{"title": f"A{i}", "time_published": "??"} for i in range(20)]})
    curated = curate_news_feed(feed, max_age_days=21, max_articles=5)
    d = json.loads(curated)

    assert len(d["feed"]) == 5
    assert d["curated"]["trimmed_by"] == "count"
    assert "unparseable" in d["curated"]["note_detail"]


def test_undated_articles_are_kept_not_discarded():
    from datetime import datetime, timedelta

    newest = datetime(2026, 7, 24, 12, 0)
    feed = json.dumps(
        {
            "feed": [
                {"title": "dated", "time_published": newest.strftime("%Y%m%dT%H%M%S")},
                {"title": "undated"},
                {
                    "title": "old",
                    "time_published": (newest - timedelta(days=40)).strftime("%Y%m%dT%H%M%S"),
                },
            ]
        }
    )
    kept = _titles(curate_news_feed(feed, max_age_days=21))
    assert "undated" in kept
    assert "old" not in kept


def test_zero_disables_the_recency_window():
    curated = curate_news_feed(_aged_news([0, 40, 90]), max_age_days=0, max_articles=0)
    assert len(json.loads(curated)["feed"]) == 3


def test_configured_defaults_reach_the_curator():
    """The shipped settings must actually take effect through curate_response."""
    from evotrader.models.config import CurationConfig

    # Enough recent articles that the min_articles floor doesn't restore the
    # old ones: 10 inside the 21-day window, 3 outside it.
    payload = _mcp_result(_aged_news([*range(10), 40, 45, 50]))
    curate_response("NEWS_SENTIMENT", {}, payload, CurationConfig())
    d = json.loads(payload["content"][0]["text"])

    assert d["curated"]["articles_available"] == 13
    assert d["curated"]["articles_returned"] == 10
    assert d["curated"]["trimmed_by"] == "recency"


def test_floor_wins_over_the_age_window_on_a_small_feed():
    """A short feed must not be reduced below the floor by the age window."""
    from evotrader.models.config import CurationConfig

    payload = _mcp_result(_aged_news([0, 5, 40, 46]))
    curate_response("NEWS_SENTIMENT", {}, payload, CurationConfig())
    d = json.loads(payload["content"][0]["text"])

    # Only 2 are inside 21 days, but the floor of 8 keeps all 4 available.
    assert len(d["feed"]) == 4
