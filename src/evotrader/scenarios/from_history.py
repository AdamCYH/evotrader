"""Build scenarios from the system's own recorded history.

The generated scenarios in :mod:`evotrader.scenarios.generate` stub the news
witness as empty, because historical news is not in the market data feed. That
disables the strongest witness by construction and forces every decision to
flat — the test then measures the fixture rather than the strategy.

This module pairs recorded market snapshots with the news report the system
actually produced at that time, so all three witnesses are present and the
agreement gate can genuinely unlock.

Two rules keep it honest:

- News is matched **backwards only**. A report filed after the snapshot is
  information the agent could not have had, and pairing it would be lookahead.
- The forward return is computed from price history and stored separately from
  the prompt, so it can score a decision but never reach the agent.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_SCORE_RE = re.compile(r"Sentiment Score:\s*(-?[\d.]+)")
_CONF_RE = re.compile(r"Confidence:\s*(-?[\d.]+)")
_REASON_RE = re.compile(r"Reasoning:\s*(.+)", re.S)


@dataclass
class NewsReport:
    """One recorded news-sentiment output."""

    timestamp: pd.Timestamp
    score: float
    confidence: float
    reasoning: str

    def render(self) -> str:
        """Format as the Strategy Agent receives it."""
        body = " ".join(self.reasoning.split())
        return "\n".join(
            [
                "### News & Sentiment Agent report",
                f"  aggregate_score: {self.score:+.2f}",
                f"  confidence: {self.confidence:.2f}",
                f"  filed_at: {self.timestamp:%Y-%m-%d %H:%M} UTC",
                f'  reasoning: "{body[:900]}"',
            ]
        )


def load_news(db_path: str) -> pd.DataFrame:
    """Read parsed news-sentiment reports from the thought log."""
    conn = sqlite3.connect(db_path)
    try:
        raw = pd.read_sql(
            """
            SELECT timestamp, content FROM agent_thought_log
            WHERE agent_name = 'news_sentiment' AND event_type = 'thought'
              AND content LIKE '%Sentiment Score%'
            ORDER BY timestamp
            """,
            conn,
        )
    finally:
        conn.close()

    rows: list[dict[str, Any]] = []
    for ts, content in raw.itertuples(index=False):
        s, c = _SCORE_RE.search(content or ""), _CONF_RE.search(content or "")
        if not (s and c):
            continue
        r = _REASON_RE.search(content or "")
        rows.append(
            {
                "t": pd.to_datetime(ts, utc=True, format="mixed"),
                "score": float(s.group(1)),
                "confidence": float(c.group(1)),
                "reasoning": (r.group(1).strip() if r else ""),
            }
        )
    df = pd.DataFrame(rows)
    logger.info("Loaded %d parsed news reports from %s", len(df), db_path)
    return df


def load_snapshots(db_path: str, ticker: str | None = None) -> pd.DataFrame:
    """Read recorded market snapshots, of one instrument when ``ticker`` is given.

    :func:`attach_forward_returns` scores every row against one price series,
    so a frame holding two instruments (a stock and its inverse fund) scores
    one of them against the other's prices: pass the instrument.
    """
    where, params = ("WHERE ticker = ?", (ticker,)) if ticker else ("", ())
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql(
            f"""
            SELECT timestamp, ticker, close_price, composite_signal, regime,
                   regime_confidence, sub_signals_json, indicators_json
            FROM market_snapshots {where} ORDER BY timestamp
            """,
            conn,
            params=params,
        )
    finally:
        conn.close()
    df["t"] = pd.to_datetime(df["timestamp"], utc=True, format="mixed")
    return df


def pair(
    snapshots: pd.DataFrame,
    news: pd.DataFrame,
    max_age_minutes: int = 90,
) -> pd.DataFrame:
    """Attach to each snapshot the most recent news report *at or before* it.

    ``direction="backward"`` is the load-bearing argument. Matching to the
    nearest report in either direction would pair a snapshot with news filed
    afterwards — the agent would be reasoning from information it could not
    have had, and any measured edge would be an artefact.
    """
    if snapshots.empty or news.empty:
        return pd.DataFrame()
    merged = pd.merge_asof(
        snapshots.sort_values("t"),
        news.sort_values("t").rename(columns={"t": "news_t"}),
        left_on="t",
        right_on="news_t",
        direction="backward",
        tolerance=pd.Timedelta(minutes=max_age_minutes),
    )
    out = merged.dropna(subset=["score"]).copy()
    out["news_age_min"] = (out["t"] - out["news_t"]).dt.total_seconds() / 60.0
    logger.info("Paired %d of %d snapshots with prior news", len(out), len(snapshots))
    return out


def attach_forward_returns(
    paired: pd.DataFrame,
    prices: pd.Series,
    horizon_days: int = 5,
) -> pd.DataFrame:
    """Add the realised forward return for each paired snapshot.

    ``prices`` is a datetime-indexed close series covering the window. Rows
    whose horizon extends past the available data are dropped rather than
    filled, so no decision is scored against a return that does not exist.
    """
    if paired.empty:
        return paired
    px = prices.copy()
    px.index = pd.to_datetime(px.index, utc=True)
    px = px.sort_index()

    fwd: list[float | None] = []
    for t in paired["t"]:
        target = t + timedelta(days=horizon_days)
        if target > px.index[-1]:
            fwd.append(None)
            continue
        now_i = px.index.get_indexer(pd.Index([t]), method="ffill")[0]
        late_i = px.index.get_indexer(pd.Index([target]), method="ffill")[0]
        if now_i < 0 or late_i < 0 or px.iloc[now_i] <= 0:
            fwd.append(None)
            continue
        fwd.append(float((px.iloc[late_i] / px.iloc[now_i] - 1.0) * 100.0))

    out = paired.copy()
    out["forward_return_pct"] = fwd
    dropped = out["forward_return_pct"].isna().sum()
    if dropped:
        logger.info("Dropped %d rows whose %dd horizon exceeds price data", dropped, horizon_days)
    return out.dropna(subset=["forward_return_pct"])


def strong_cases(
    paired: pd.DataFrame, min_abs_score: float = 0.2, min_confidence: float = 0.7
) -> pd.DataFrame:
    """Rows where the news witness made a confident, directional call.

    These are the only cases where a multi-witness design can differ from an
    algorithm-only one, so they are where the thesis is actually testable.
    """
    if paired.empty:
        return paired
    return paired[
        (paired["score"].abs() >= min_abs_score) & (paired["confidence"] >= min_confidence)
    ].copy()
