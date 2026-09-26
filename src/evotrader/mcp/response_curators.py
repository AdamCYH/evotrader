"""Shape oversized MCP tool responses before they enter an agent's context.

Some research endpoints return unbounded data. Alpha Vantage's economic
indicators are the clearest case: ``FEDERAL_FUNDS_RATE`` and ``TREASURY_YIELD``
accept only an ``interval``, with **no date-range parameter**, so the entire
series since the 1950s comes back on every call. One real response in this
project's log was 1,038,424 characters (~260k tokens) of daily fed funds rates
going back decades, to inform an intraday decision about QQQ.

Because there is no way to ask for less, the trimming has to happen on receipt.
That is what this module does, and it does it *structurally* rather than by
truncating text: a blind cut through a payload leaves a fragment the model may
silently misread, whereas keeping the newest N rows of a newest-first series is
lossless for any decision that depends on recent data.

Design notes
------------
* **Curators are per-tool and opt-in.** A tool with no registered curator is
  passed through untouched. Nothing is guessed at.
* **Every trim is announced.** A curated payload carries a note stating how many
  observations were kept out of how many and over what dates, so the model knows
  it is looking at a window rather than assuming it saw everything.
* **Alpha Vantage's ``TOOL_CALL`` dispatcher is unwrapped.** That tool takes the
  real function name in ``args["tool_name"]``, so dispatch resolves through it.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# How many of the most recent observations to keep from a time series.
#
# 120 rows covers ~6 months of daily data or 10 years of monthly data — far more
# history than an intraday decision needs, while turning a 1,038,424-character
# response into roughly 2,500. Raise it if a strategy ever needs a longer
# lookback; the algorithmic indicators get their history from price data, not
# from these endpoints.
DEFAULT_TIMESERIES_ROWS = 120

# Alpha Vantage's own preview envelope, which it applies to large CSV payloads
# before we ever see them. It still leaves ~150k characters, so we curate the
# `sample_data` inside it.
_AV_PREVIEW_KEYS = ("preview", "data_type", "total_lines", "sample_lines", "sample_data")


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip()[:10])
    except (ValueError, AttributeError):
        return None


def _parse_rows(rows: list[str]) -> list[tuple[str, float]]:
    """Parse ``date,value`` rows, skipping missing-value markers.

    These feeds use ``.`` for a missing observation (e.g. ``2025-10-01,.``).
    """
    parsed: list[tuple[str, float]] = []
    for row in rows:
        parts = row.split(",")
        if len(parts) < 2:
            continue
        try:
            parsed.append((parts[0].strip(), float(parts[1].strip())))
        except ValueError:
            continue
    return parsed


def _summarise_history(parsed: list[tuple[str, float]]) -> dict[str, Any]:
    """Describe the *whole* series compactly, so trimming loses no context.

    This is the part that makes curation safe rather than merely smaller. An
    agent reading only a recent window could not say "the 10-year yield is at a
    two-year high"; these statistics let it, and in ~200 characters rather than
    150,000. It is computed from the full history *before* any trimming.

    Rows arrive newest-first.
    """
    if not parsed:
        return {}

    latest_date, latest_value = parsed[0]
    summary: dict[str, Any] = {
        "latest": {"date": latest_date, "value": latest_value},
        "observations": len(parsed),
        "full_range": f"{parsed[-1][0]} … {parsed[0][0]}",
    }

    # Changes are measured by CALENDAR distance, not by row offset. These feeds
    # differ in frequency — fed funds is daily, CPI is monthly — so a fixed row
    # offset would label a 5-month CPI move as "1w". Mislabelled macro data is
    # worse than absent macro data: the agent would reason confidently from a
    # number that means something else entirely.
    dated: list[tuple[date, float]] = []
    for raw_date, value in parsed:
        parsed_date = _parse_date(raw_date)
        if parsed_date is not None:
            dated.append((parsed_date, value))

    if dated:
        latest_dt = dated[0][0]
        changes: dict[str, float] = {}
        for label, days in (("1w", 7), ("1m", 30), ("3m", 91), ("1y", 365)):
            target = latest_dt - timedelta(days=days)
            # Nearest observation at or before the target date.
            past = next((v for d, v in dated if d <= target), None)
            if past is None:
                continue  # series doesn't reach back that far
            # Only report a lookback the series can actually resolve: skip it
            # when the nearest match is more than half the window away.
            nearest = next(d for d, _ in dated if d <= target)
            if abs((target - nearest).days) > days / 2:
                continue
            changes[label] = round(latest_value - past, 4)
        if changes:
            summary["change"] = changes

    # Extremes over the full history and over the last year, so the agent can
    # place the current reading in context ("a two-year high") without needing
    # the rows that were trimmed.
    def _extremes(window: list[tuple[str, float]]) -> dict[str, Any]:
        lo = min(window, key=lambda r: r[1])
        hi = max(window, key=lambda r: r[1])
        return {
            "min": {"date": lo[0], "value": lo[1]},
            "max": {"date": hi[0], "value": hi[1]},
        }

    summary["full_history_extremes"] = _extremes(parsed)
    if dated:
        year_ago = dated[0][0] - timedelta(days=365)
        last_year = [
            (raw_date, value)
            for raw_date, value in parsed
            if (d := _parse_date(raw_date)) is not None and d >= year_ago
        ]
        if last_year and len(last_year) < len(parsed):
            summary["last_1y_extremes"] = _extremes(last_year)

    return summary


def _trim_csv_timeseries(text: str, max_rows: int) -> tuple[str, dict[str, Any] | None]:
    """Keep the header and the newest *max_rows* rows of a CSV time series.

    Alpha Vantage returns these newest-first, so the newest rows are the ones
    immediately after the header. Returns ``(text, info)`` where ``info`` is
    None when nothing was trimmed.
    """
    # Rows are separated by \r (not \r\n) in these payloads.
    lines = [ln for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if ln.strip()]
    if len(lines) <= max_rows + 1:
        return text, None

    header, rows = lines[0], lines[1:]
    kept = rows[:max_rows]

    def _date_of(row: str) -> str:
        return row.split(",", 1)[0].strip()

    info: dict[str, Any] = {
        "observations_kept": len(kept),
        "observations_available": len(rows),
        "date_range_kept": f"{_date_of(kept[-1])} … {_date_of(kept[0])}",
        "oldest_available": _date_of(rows[-1]),
        "note": (
            f"Showing the {len(kept)} most recent observations of {len(rows)}. "
            "This endpoint has no date-range parameter, so the full history is "
            "trimmed on receipt. `summary` below is computed from the COMPLETE "
            "history, so use it for any long-horizon comparison (highs, lows, "
            "year-over-year change) rather than assuming the rows are all there is."
        ),
    }

    # Summarise the full history before discarding it, so nothing that could
    # matter for a decision is actually lost.
    summary = _summarise_history(_parse_rows(rows))
    if summary:
        info["summary"] = summary

    return "\n".join([header, *kept]), info


def curate_timeseries(text: str, max_rows: int = DEFAULT_TIMESERIES_ROWS) -> str:
    """Curate an economic-indicator payload, whatever envelope it arrives in."""
    stripped = text.lstrip()

    # Alpha Vantage sometimes wraps large CSV in its own preview envelope.
    if stripped.startswith("{"):
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict) and any(k in parsed for k in _AV_PREVIEW_KEYS):
            sample = parsed.get("sample_data")
            if isinstance(sample, str):
                trimmed, info = _trim_csv_timeseries(sample, max_rows)
                if info is None:
                    return text
                # Report against the true total when the envelope knows it.
                total = parsed.get("total_lines")
                if isinstance(total, int) and total > info["observations_available"]:
                    info["observations_available"] = total
                    info["note"] = (
                        f"Showing the {info['observations_kept']} most recent "
                        f"observations of ~{total}. This endpoint has no "
                        "date-range parameter, so the full history is trimmed "
                        "on receipt."
                    )
                return json.dumps(
                    {"data_type": "csv", "data": trimmed, "curated": info},
                    default=str,
                )
        return text

    if "timestamp,value" in stripped[:64] or stripped.startswith("timestamp,"):
        trimmed, info = _trim_csv_timeseries(text, max_rows)
        if info is None:
            return text
        return json.dumps({"data_type": "csv", "data": trimmed, "curated": info}, default=str)

    return text


# Per-article fields dropped from a news feed. Both are decoration: a URL to a
# banner image, and a source-internal category string. Neither reaches the
# agent's output contract (score, confidence, ≤4 sentences, catalysts, flag).
_NEWS_DROP_FIELDS = ("banner_image", "category_within_source")


def _relevant_tickers(config: Any = None) -> frozenset[str]:
    """Symbols that can move what we trade, resolved from live holdings data.

    Delegates to :mod:`evotrader.tools.universe`, so this follows whatever
    ticker(s) are configured — nothing about QQQ (or any ETF) is assumed here.

    Returns an empty set when the universe is not yet resolved. Callers must
    read that as "don't filter": carrying extra articles costs tokens, whereas
    dropping relevant ones costs signal.
    """
    try:
        from evotrader.tools.universe import cached_relevant_symbols

        return frozenset(cached_relevant_symbols(config))
    except Exception:  # pragma: no cover - defensive
        return frozenset()


def _article_time(article: Any) -> datetime | None:
    """Parse an article timestamp. These feeds use ``YYYYMMDDTHHMMSS``."""
    if not isinstance(article, dict):
        return None
    raw = article.get("time_published") or article.get("published") or ""
    for fmt in ("%Y%m%dT%H%M%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(raw).strip()[:19].replace("Z", ""), fmt)
        except (ValueError, TypeError):
            continue
    return None


def _trim_news_feed(
    feed: list[Any],
    max_age_days: int,
    max_articles: int,
    min_articles: int,
) -> tuple[list[Any], dict[str, Any] | None]:
    """Reduce a news feed by recency, never by position in the server's ranking.

    Age is the primary lever because it is the one the consuming agent actually
    reasons with — its instructions treat anything older than ~5 sessions as
    "largely priced". A fixed *count* means something different every week, and
    splits unevenly when several tickers are queried at once; an age window
    adapts to article density on its own.

    The feed is sorted newest-first here rather than trusting the server's
    ordering, so the reduction is a recency cut even if a ``sort=LATEST`` request
    was ignored. ``min_articles`` guarantees the agent is never left with almost
    nothing during a quiet stretch.
    """
    if max_age_days <= 0 and max_articles <= 0:
        return feed, None

    dated = [(article, _article_time(article)) for article in feed]
    if not any(when for _, when in dated):
        # No parseable timestamps — an age window is meaningless and a count cut
        # would be a blind slice of the server's ranking. Only honour a hard
        # ceiling, and say so.
        if max_articles and len(feed) > max_articles:
            return feed[:max_articles], {
                "trimmed_by": "count",
                "note_detail": "timestamps unparseable, so recency could not be used",
            }
        return feed, None

    # Undated articles sort last rather than being discarded outright.
    ordered = sorted(
        dated,
        key=lambda pair: pair[1] or datetime.min,
        reverse=True,
    )
    newest = ordered[0][1]
    reasons: list[str] = []

    kept = ordered
    if max_age_days > 0 and newest is not None:
        cutoff = newest - timedelta(days=max_age_days)
        within = [pair for pair in ordered if pair[1] is None or pair[1] >= cutoff]
        if len(within) < len(ordered):
            reasons.append(f"older than {max_age_days} days")
            kept = within

    if min_articles > 0 and len(kept) < min_articles:
        # Restore the next-newest articles up to the floor.
        kept = ordered[: min(min_articles, len(ordered))]
        reasons.append(f"floor of {min_articles} applied")

    if max_articles > 0 and len(kept) > max_articles:
        kept = kept[:max_articles]
        reasons.append(f"ceiling of {max_articles} applied")

    if len(kept) == len(ordered) and not reasons:
        return feed, None

    info = {
        "trimmed_by": "recency",
        "oldest_kept": (kept[-1][1].date().isoformat() if kept and kept[-1][1] else None),
        "newest": newest.date().isoformat() if newest else None,
        "note_detail": "; ".join(reasons) if reasons else "sorted newest-first",
    }
    return [article for article, _ in kept], info


def curate_news_feed(
    text: str,
    *,
    max_age_days: int = 0,
    max_articles: int = 0,
    min_articles: int = 0,
    filter_ticker_sentiment: bool = False,
    relevant_tickers: frozenset[str] | None = None,
) -> str:
    """Prune per-article noise from a news-sentiment payload.

    Three levers with genuinely different risk profiles, which is why they are
    separate switches rather than one "curate" flag:

    * **Decorative fields (always on, provably lossless).** ``banner_image`` is
      a URL to a picture and ``category_within_source`` is a source-internal
      string. Neither can reach the consuming agent's output contract — a score,
      a confidence, ≤4 sentences, catalyst lines and one flag.
    * **``filter_ticker_sentiment`` (off by default, *nearly* lossless).** Keeps
      the per-ticker sentiment breakdown only for the primary ticker and its
      major holdings. Saves roughly a further quarter of the payload, but it does
      drop information: an article mentioning e.g. MU loses that relevance row,
      and semis are something the news agent is told to watch. The article's own
      title, summary and overall sentiment are untouched either way.
    * **``max_age_days`` / ``max_articles`` / ``min_articles`` (recency window).**
      Reducing the feed decides what is relevant, so it is explicit and always
      announced. Age is the primary lever; the count is a ceiling; the floor
      protects against a quiet stretch leaving the agent with nothing.
    """
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return text
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text
    if not isinstance(parsed, dict):
        return text

    feed = parsed.get("feed")
    if not isinstance(feed, list) or not feed:
        return text

    info: dict[str, Any] = {
        "articles_returned": len(feed),
        "fields_dropped": list(_NEWS_DROP_FIELDS),
    }

    available = len(feed)
    feed, trim_info = _trim_news_feed(feed, max_age_days, max_articles, min_articles)
    if trim_info is not None:
        parsed["feed"] = feed
        info["articles_available"] = available
        info["articles_returned"] = len(feed)
        info.update(trim_info)

    keep_tickers = frozenset()
    if filter_ticker_sentiment:
        keep_tickers = relevant_tickers if relevant_tickers is not None else _relevant_tickers()
        if not keep_tickers:
            # Universe unresolved — filtering now would drop every row. Skip.
            logger.info(
                "News curation: trading universe unresolved, keeping all ticker_sentiment rows."
            )
            filter_ticker_sentiment = False
    dropped_ticker_rows = 0

    for article in feed:
        if not isinstance(article, dict):
            continue
        for field in _NEWS_DROP_FIELDS:
            article.pop(field, None)

        if not filter_ticker_sentiment:
            continue
        sentiments = article.get("ticker_sentiment")
        if isinstance(sentiments, list):
            relevant = [
                row
                for row in sentiments
                if isinstance(row, dict) and row.get("ticker") in keep_tickers
            ]
            dropped_ticker_rows += len(sentiments) - len(relevant)
            article["ticker_sentiment"] = relevant

    note = (
        "Decorative per-article fields (a banner-image URL and a source-internal "
        "category) were removed. Every article's title, timestamp, source, "
        "overall sentiment and summary are intact."
    )
    if filter_ticker_sentiment:
        info["ticker_sentiment_rows_dropped"] = dropped_ticker_rows
        info["ticker_sentiment_kept_for"] = sorted(keep_tickers)
        note += (
            " Per-ticker sentiment rows were kept only for the primary ticker "
            "and its major holdings."
        )
    if "articles_available" in info:
        detail = info.get("note_detail") or ""
        note += (
            f" Showing the {info['articles_returned']} most recent of "
            f"{info['articles_available']} articles"
            + (f" ({detail})" if detail else "")
            + ". Ask for a wider window if you need older coverage."
        )
    info["note"] = note

    parsed["curated"] = info
    return json.dumps(parsed, default=str)


# Keys under which the various earnings endpoints nest their rows. Checked in
# order; the first list found is treated as the result set.
_EARNINGS_ROW_PATHS = (
    ("data", "results"),
    ("results",),
    ("data", "earningsCalendar"),
    ("earningsCalendar"),
)


def _find_rows(payload: Any) -> tuple[list[Any] | None, Any, str | None]:
    """Locate the list of earnings rows in a payload.

    Returns ``(rows, container, key)`` so the caller can substitute a filtered
    list back in place. Shape-agnostic on purpose: these endpoints differ between
    providers and the filter should not care which one answered.
    """
    if isinstance(payload, list):
        return payload, None, None
    if not isinstance(payload, dict):
        return None, None, None

    for path in _EARNINGS_ROW_PATHS:
        keys = (path,) if isinstance(path, str) else path
        node: Any = payload
        ok = True
        for key in keys[:-1]:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                ok = False
                break
        if not ok:
            continue
        last = keys[-1]
        if isinstance(node, dict) and isinstance(node.get(last), list):
            return node[last], node, last

    # Fall back to the single largest list of dicts anywhere at the top level.
    best: tuple[list[Any] | None, Any, str | None] = (None, None, None)
    for key, value in payload.items():
        if (
            isinstance(value, list)
            and value
            and isinstance(value[0], dict)
            and (best[0] is None or len(value) > len(best[0]))
        ):
            best = (value, payload, key)
    return best


def curate_earnings_calendar(
    text: str,
    *,
    relevant_tickers: frozenset[str] | None = None,
) -> str:
    """Filter an earnings calendar to symbols that can move what we trade.

    These endpoints return the whole market: one real response held 1,814
    entries across 1,788 tickers, none of which was the traded instrument (a
    fund has no earnings of its own — its *holdings* do).

    The symbol set is supplied by the caller, resolved from live holdings data,
    so nothing about any particular ticker or index is assumed here. An empty or
    missing set leaves the payload untouched rather than filtering everything —
    dropping earnings the strategy needed would cost far more than the tokens.
    """
    if not relevant_tickers:
        return text

    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")):
        return text
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text

    rows, container, key = _find_rows(parsed)
    if not rows:
        return text

    kept = [
        row
        for row in rows
        if isinstance(row, dict)
        and str(row.get("symbol") or row.get("ticker") or "").upper() in relevant_tickers
    ]

    # If the filter matched nothing at all, the payload's shape probably isn't
    # what we assumed. Returning it unchanged is the safe reading.
    if not kept:
        logger.info(
            "Earnings calendar curation matched 0 of %d rows against %d relevant "
            "symbols — leaving the payload unchanged.",
            len(rows),
            len(relevant_tickers),
        )
        return text

    info = {
        "entries_kept": len(kept),
        "entries_available": len(rows),
        "filtered_to": sorted(relevant_tickers),
        "note": (
            f"Filtered from {len(rows)} market-wide entries to the {len(kept)} "
            "for symbols that can move the traded instrument (the instrument "
            "itself plus its largest holdings). Ask for a specific symbol if you "
            "need one outside this set."
        ),
    }

    if container is None:
        parsed = {"results": kept, "curated": info}
    else:
        container[key] = kept
        if isinstance(parsed, dict):
            parsed["curated"] = info

    return json.dumps(parsed, default=str)


# Tools whose responses get curated, and how.
#
# Keyed by the tool name the MCP server exposes. Alpha Vantage economic
# indicators all return the same newest-first `timestamp,value` CSV.
_CURATORS: dict[str, Callable[[str], str]] = {
    name: curate_timeseries
    for name in (
        "FEDERAL_FUNDS_RATE",
        "TREASURY_YIELD",
        "CPI",
        "INFLATION",
        "UNEMPLOYMENT",
        "NONFARM_PAYROLL",
        "REAL_GDP",
        "REAL_GDP_PER_CAPITA",
        "RETAIL_SALES",
        "DURABLES",
    )
}
_CURATORS["NEWS_SENTIMENT"] = curate_news_feed
for _name in ("get_earnings_calendar", "EARNINGS_CALENDAR"):
    _CURATORS[_name] = curate_earnings_calendar


def _measure(value: Any) -> int:
    try:
        return len(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def apply_backstop(text: str, max_chars: int) -> tuple[str, dict[str, Any] | None]:
    """Shrink an oversized payload on a structural boundary.

    A safety net, not the primary mechanism. The per-tool curators above only
    cover endpoints someone has looked at; this catches the *next* unfamiliar
    tool that returns a megabyte, so a surprise costs tokens rather than a whole
    context window.

    Three properties make it safe rather than crude:

    * **It never cuts mid-structure.** Whole list elements or whole dict keys are
      dropped. A blind slice through JSON leaves a fragment the model may read as
      complete, which is a worse failure than a large payload.
    * **It announces itself.** The result says what was dropped and how much
      remains, so the model can ask for a narrower query instead of assuming it
      saw everything.
    * **It reports non-JSON payloads rather than cutting them.** Truncating opaque
      text has no safe boundary to cut on, so the payload is left intact and the
      caller logs it — that is a signal the tool needs a real curator.

    Returns ``(text, info)``; ``info`` is None when nothing changed.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text, None

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # No structure to cut along — leave it alone and let the caller log it.
        return text, None

    # Prefer trimming the largest list: that is almost always the bulk.
    target_list: list[Any] | None = None
    container: Any = None
    key: Any = None

    if isinstance(parsed, list):
        target_list, container, key = parsed, None, None
    elif isinstance(parsed, dict):
        best = 0
        for k, v in parsed.items():
            if isinstance(v, list) and _measure(v) > best:
                target_list, container, key, best = v, parsed, k, _measure(v)
        if target_list is None:
            # No list: drop the largest keys until it fits.
            dropped_keys: list[str] = []
            ordered = sorted(parsed.items(), key=lambda kv: -_measure(kv[1]))
            for k, _ in ordered:
                if _measure(parsed) <= max_chars:
                    break
                if k in ("curated", "_backstop"):
                    continue
                del parsed[k]
                dropped_keys.append(str(k))
            if not dropped_keys:
                return text, None
            info = {
                "_truncated": True,
                "_keys_dropped": dropped_keys,
                "_note": (
                    "This response exceeded the size limit and the largest "
                    f"field(s) were removed: {', '.join(dropped_keys)}. Request a "
                    "narrower query if you need them."
                ),
            }
            parsed["_backstop"] = info
            return json.dumps(parsed, default=str), info

    if target_list is None:
        return text, None

    total = len(target_list)
    kept = list(target_list)
    # Halve until it fits — O(log n) re-serialisations rather than O(n).
    while kept and _measure(kept) > max_chars:
        kept = kept[: max(1, len(kept) // 2)]
        if len(kept) == 1 and _measure(kept) > max_chars:
            break

    if len(kept) >= total:
        return text, None

    info = {
        "_truncated": True,
        "_kept": len(kept),
        "_total": total,
        "_note": (
            f"This response exceeded the size limit. Showing {len(kept)} of "
            f"{total} entries, cut on a whole-entry boundary. The rest were not "
            "sent — narrow your query if you need them."
        ),
    }

    if container is None:
        return json.dumps({"results": kept, "_backstop": info}, default=str), info
    container[key] = kept
    if isinstance(parsed, dict):
        parsed["_backstop"] = info
    return json.dumps(parsed, default=str), info


def _structured_content(result: Any) -> Any:
    return (
        result.get("structuredContent")
        if isinstance(result, dict)
        else getattr(result, "structuredContent", None)
    )


def dedupe_structured_content(result: Any) -> int:
    """Drop ``structuredContent`` when it provably duplicates ``content``.

    MCP servers return a payload twice: once as text in ``content`` and once as
    parsed data in ``structuredContent``. ADK forwards the whole result object
    into the conversation, so the model reads both copies and is billed for both.

    Measured across this project's log: 32,256,061 of 70,226,654 characters
    (**46%**) of all MCP payload is this duplicate. Six tools from three
    different servers were checked and every one was byte-identical — either
    ``text == structuredContent`` or ``text == structuredContent["result"]``.

    The saving is unconditional but the *safety* is not assumed: equality is
    verified on every response, and anything that differs by even a byte is left
    untouched. That is why this runs for every tool including the trading path —
    the guarantee comes from the check, not from a judgement about the tool.

    Returns the number of characters removed.
    """
    structured = _structured_content(result)
    if structured is None or not isinstance(result, dict):
        return 0

    texts = [t for block in _text_blocks(result) if (t := _get_text(block))]
    if len(texts) != 1:
        # Only the single-text-block case is unambiguous.
        return 0
    text = texts[0]

    def _equivalent() -> bool:
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            parsed = None

        if parsed is not None and parsed == structured:
            return True
        # Servers that wrap a raw payload under a single key, e.g. {"result": "<csv>"}
        if isinstance(structured, dict) and len(structured) == 1:
            only = next(iter(structured.values()))
            if parsed is not None and only == parsed:
                return True
            if isinstance(only, str) and only.strip() == text.strip():
                return True
        return False

    try:
        if not _equivalent():
            return 0
    except Exception:  # pragma: no cover - defensive
        return 0

    removed = len(json.dumps(structured, default=str))
    result["structuredContent"] = {
        "_deduplicated": "Identical to the text content above; omitted to avoid "
        "sending the same payload twice."
    }
    return removed


def _curator_for(resolved: str, config: Any, app_config: Any = None) -> Callable[[str], str] | None:
    """Look up a curator, bound to *config* where it takes options."""
    curator = _CURATORS.get(resolved)
    if curator is None or config is None:
        return curator

    if curator is curate_timeseries:
        rows = getattr(config, "timeseries_max_rows", DEFAULT_TIMESERIES_ROWS)
        return lambda text: curate_timeseries(text, max_rows=rows)

    if curator is curate_earnings_calendar:
        universe = _relevant_tickers(app_config)
        return lambda text: curate_earnings_calendar(text, relevant_tickers=universe)

    if curator is curate_news_feed:
        max_age_days = getattr(config, "news_max_age_days", 0)
        max_articles = getattr(config, "news_max_articles", 0)
        min_articles = getattr(config, "news_min_articles", 0)
        filter_sentiment = getattr(config, "news_filter_ticker_sentiment", False)
        universe = _relevant_tickers(app_config) if filter_sentiment else None
        return lambda text: curate_news_feed(
            text,
            max_age_days=max_age_days,
            max_articles=max_articles,
            min_articles=min_articles,
            filter_ticker_sentiment=filter_sentiment,
            relevant_tickers=universe,
        )

    return curator


def effective_tool_name(tool_name: str, args: dict | None) -> str:
    """Resolve the real function behind Alpha Vantage's ``TOOL_CALL`` dispatcher.

    ``TOOL_CALL`` takes the function to invoke in ``args["tool_name"]``, so
    without this every economic indicator would arrive under one opaque name.
    """
    if tool_name == "TOOL_CALL" and isinstance(args, dict):
        inner = args.get("tool_name")
        if isinstance(inner, str) and inner:
            return inner
    return tool_name


def has_curator(tool_name: str, args: dict | None = None) -> bool:
    return effective_tool_name(tool_name, args) in _CURATORS


def _text_blocks(result: Any) -> list[Any]:
    """Return the content blocks of an MCP result, whatever shape it has."""
    content = (
        result.get("content") if isinstance(result, dict) else getattr(result, "content", None)
    )
    return list(content) if isinstance(content, list) else []


def _get_text(block: Any) -> str | None:
    if isinstance(block, dict):
        return block.get("text") if block.get("type", "text") == "text" else None
    if getattr(block, "type", "text") == "text":
        text = getattr(block, "text", None)
        return text if isinstance(text, str) else None
    return None


def _set_text(block: Any, text: str) -> bool:
    if isinstance(block, dict):
        block["text"] = text
        return True
    try:
        block.text = text
        return True
    except Exception:  # pragma: no cover - frozen/immutable block types
        return False


def curate_response(
    tool_name: str,
    args: dict | None,
    result: Any,
    config: Any = None,
    app_config: Any = None,
    roles: tuple[str, ...] = (),
) -> Any:
    """Apply the registered curator to *result*, in place where possible.

    Returns the result either way so callers can treat this as a pipeline step.
    Never raises: a curator failure must not break a tool call, so it logs and
    returns the payload untouched.

    Args:
        tool_name: The MCP tool's name as the server exposes it.
        args: The arguments the tool was called with (used to resolve the
            ``TOOL_CALL`` dispatcher).
        result: The MCP result, mutated in place where possible.
        config: A ``CurationConfig``. ``None`` uses module defaults.
        app_config: The ``AppConfig``, used to resolve which symbols are
            relevant to what is being traded. ``None`` disables that filtering
            rather than guessing at a universe.
        roles: The MCP roles this tool's provider serves (e.g. ``("research",)``).
            Gates the size backstop, which stays away from the order path.
    """
    if config is not None and not getattr(config, "enabled", True):
        return result

    resolved = effective_tool_name(tool_name, args)

    # Deduplication runs for every tool, curated or not: it is verified byte-for
    # byte on each response, so it cannot lose information.
    if config is None or getattr(config, "dedupe_structured_content", True):
        removed = dedupe_structured_content(result)
        if removed:
            logger.info(
                "Dropped duplicate structuredContent from %s: %d chars",
                resolved,
                removed,
            )

    curator = _curator_for(resolved, config, app_config)
    if curator is None:
        _maybe_backstop(resolved, result, config, roles)
        return result

    try:
        curated_any = False
        for block in _text_blocks(result):
            original = _get_text(block)
            if not original:
                continue
            curated = curator(original)
            shrank = curated is not original and len(curated) < len(original)
            if shrank and _set_text(block, curated):
                curated_any = True
                logger.info(
                    "Curated %s response: %d → %d chars (%.1f%% smaller)",
                    resolved,
                    len(original),
                    len(curated),
                    (1 - len(curated) / len(original)) * 100,
                )

        # MCP results carry `structuredContent` as a machine-readable copy of
        # the same payload. Curating only `content` would leave the full series
        # sitting in the duplicate — which is exactly half of a 1MB response.
        # Curating the copy too keeps the two consistent; a curated series that
        # disagreed with its own structured form would be worse than either.
        if curated_any:
            _curate_structured_content(result, curator)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Curator for %s failed, passing response through unchanged: %s",
            resolved,
            exc,
        )

    return result


def _curate_structured_content(result: Any, curator: Callable[[str], str]) -> None:
    """Curate the ``structuredContent`` mirror of a payload, in place."""
    structured = (
        result.get("structuredContent")
        if isinstance(result, dict)
        else getattr(result, "structuredContent", None)
    )
    if not isinstance(structured, dict):
        return

    # Shape A: {"result": "<the whole csv>"}
    payload = structured.get("result")
    if isinstance(payload, str) and len(payload) > 4000:
        curated = curator(payload)
        if len(curated) < len(payload):
            structured["result"] = curated
        return

    # Shape B: Alpha Vantage's own preview envelope, mirrored field by field.
    sample = structured.get("sample_data")
    if isinstance(sample, str) and len(sample) > 4000:
        trimmed, info = _trim_csv_timeseries(sample, DEFAULT_TIMESERIES_ROWS)
        if info is not None:
            structured["sample_data"] = trimmed
            structured["curated"] = info


def _maybe_backstop(
    tool_name: str,
    result: Any,
    config: Any,
    roles: tuple[str, ...],
) -> None:
    """Apply the size backstop to an uncurated response, if it is in scope.

    Scoped by MCP role rather than by tool name so it generalises: whichever
    providers serve the configured roles are covered, with no per-tool list to
    maintain. Trading-role tools are excluded by default — they return positions,
    orders and quotes, which are bounded by nature and where a partial answer is
    far more dangerous than a large one.
    """
    max_chars = getattr(config, "backstop_max_chars", 0) if config is not None else 0
    if max_chars <= 0:
        return

    allowed = set(getattr(config, "backstop_roles", ()) if config is not None else ())
    if not allowed or not (set(roles) & allowed):
        return

    for block in _text_blocks(result):
        text = _get_text(block)
        if not text or len(text) <= max_chars:
            continue
        trimmed, info = apply_backstop(text, max_chars)
        if info is None:
            logger.warning(
                "Backstop: %s returned %d chars with no safe structural boundary "
                "to cut on and no curator registered. Consider writing one.",
                tool_name,
                len(text),
            )
            continue
        if _set_text(block, trimmed):
            logger.warning(
                "Backstop trimmed %s: %d → %d chars (%s). This tool has no "
                "curator — consider adding one.",
                tool_name,
                len(text),
                len(trimmed),
                info.get("_note", ""),
            )
