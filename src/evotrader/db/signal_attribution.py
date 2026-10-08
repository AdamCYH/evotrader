"""Storage for per-witness signal attribution.

Each cycle records what every witness said *before* the decision was combined,
plus the decision itself. Forward returns are backfilled later, which turns the
log into a scoreable record: which witness actually predicts, and whether stated
conviction matches realised accuracy.

Cycles that did not trade are recorded too. They are the control group — without
them the record only contains situations the agent already liked, and any
measured edge is selection rather than skill.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from evotrader.db.connection import Database

logger = logging.getLogger(__name__)


class SignalAttributionStore:
    """Repository for the ``signal_attribution`` table."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(
        self,
        ticker: str,
        *,
        session_id: str | None = None,
        algo_direction: float | None = None,
        algo_strength: float | None = None,
        algo_author: str | None = None,
        participation_ratio: float | None = None,
        news_direction: float | None = None,
        news_strength: float | None = None,
        catalyst: str | None = None,
        llm_direction: float | None = None,
        llm_conviction: float | None = None,
        mechanism: str | None = None,
        priced_in_check: dict[str, Any] | None = None,
        final_direction: float | None = None,
        final_conviction: float | None = None,
        risk_budget: float | None = None,
        position_size: float | None = None,
        traded: bool = False,
        price_at_decision: float | None = None,
        deviation_atr: float | None = None,
        trigger_atr: float | None = None,
        composite_unattenuated: float | None = None,
        participation_scale: float | None = None,
        participation_numerator: int | None = None,
        participation_denominator: int | None = None,
        timestamp: str | None = None,
        agent_absent: bool = False,
    ) -> int | None:
        """Record one cycle's witnesses and decision. Returns the row id.

        ``timestamp`` defaults to now; a row written after the fact carries the
        decision's own time, because forward returns are measured from the
        decision's date. ``agent_absent`` marks a row the strategy agent did
        not write (see :meth:`backfill_algo_only_rows`).
        """
        try:
            async with self._db.transaction() as conn:
                cur = await conn.execute(
                    """
                    INSERT INTO signal_attribution (
                        timestamp, session_id, ticker,
                        algo_direction, algo_strength, algo_author, participation_ratio,
                        news_direction, news_strength, catalyst,
                        llm_direction, llm_conviction, mechanism, priced_in_check,
                        final_direction, final_conviction, risk_budget, position_size,
                        traded, price_at_decision,
                        deviation_atr, trigger_atr, agent_absent,
                        composite_unattenuated, participation_scale,
                        participation_numerator, participation_denominator
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        timestamp or datetime.now(UTC).isoformat(),
                        session_id,
                        ticker,
                        algo_direction,
                        algo_strength,
                        algo_author,
                        participation_ratio,
                        news_direction,
                        news_strength,
                        catalyst,
                        llm_direction,
                        llm_conviction,
                        mechanism,
                        json.dumps(priced_in_check) if priced_in_check else None,
                        final_direction,
                        final_conviction,
                        risk_budget,
                        position_size,
                        1 if traded else 0,
                        price_at_decision,
                        deviation_atr,
                        trigger_atr,
                        1 if agent_absent else 0,
                        composite_unattenuated,
                        participation_scale,
                        participation_numerator,
                        participation_denominator,
                    ),
                )
                return cur.lastrowid
        except Exception as e:
            logger.error("Failed to record signal attribution: %s", e)
            return None

    async def pending_scoring(
        self, horizon_days: int = 5, limit: int = 500, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Rows past their horizon that still have no forward return.

        ``now`` overrides the clock so a caller can score a fixed point in time
        (and so this is testable without sleeping through a horizon).
        """
        try:
            async with self._db.connection() as conn:
                # The shared connection uses dict_factory; never mutate row_factory
                # here or every other caller gets tuples back.
                # Compare against a cutoff computed here rather than with
                # julianday('now'): that truncates to whole seconds while stored
                # timestamps carry microseconds, so a freshly written row can
                # evaluate as having negative age and be skipped. ISO-8601 UTC
                # strings sort lexicographically, and this store always writes
                # them in the same format.
                cutoff = ((now or datetime.now(UTC)) - timedelta(days=horizon_days)).isoformat()
                cur = await conn.execute(
                    """
                    SELECT id, timestamp, ticker, price_at_decision
                    FROM signal_attribution
                    WHERE scored_at IS NULL
                      AND price_at_decision IS NOT NULL
                      AND timestamp <= ?
                    ORDER BY timestamp
                    LIMIT ?
                    """,
                    (cutoff, limit),
                )
                return [dict(r) for r in await cur.fetchall()]
        except Exception as e:
            logger.error("Failed to fetch pending attribution rows: %s", e)
            return []

    async def pending_long_horizon(
        self, horizon_days: int = 5, limit: int = 500, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Already-scored rows whose LONG horizon has only now elapsed.

        A row is stamped ``scored_at`` as soon as its 1-day horizon resolves —
        that is a complete measurement of the 1-day question, and waiting for
        the 5-day one would hide the most recent week from evolution entirely.
        These are the rows that then need their 5-day column filled in.
        """
        try:
            async with self._db.connection() as conn:
                cutoff = ((now or datetime.now(UTC)) - timedelta(days=horizon_days)).isoformat()
                cur = await conn.execute(
                    """
                    SELECT id, timestamp, ticker, price_at_decision
                    FROM signal_attribution
                    WHERE scored_at IS NOT NULL
                      AND forward_return_5d IS NULL
                      AND price_at_decision IS NOT NULL
                      AND timestamp <= ?
                    ORDER BY timestamp
                    LIMIT ?
                    """,
                    (cutoff, limit),
                )
                return [dict(r) for r in await cur.fetchall()]
        except Exception as e:
            logger.error("Failed to fetch long-horizon attribution rows: %s", e)
            return []

    async def score(
        self, row_id: int, forward_return_1d: float | None, forward_return_5d: float | None
    ) -> None:
        """Backfill realised forward returns for one row.

        A ``None`` leaves that column alone rather than blanking it: the 1-day
        and 5-day horizons resolve on different days, so the later pass must be
        able to fill one without erasing the other. Units are percent.
        """
        assignments = ["scored_at = ?"]
        params: list[Any] = [datetime.now(UTC).isoformat()]
        if forward_return_1d is not None:
            assignments.insert(0, "forward_return_1d = ?")
            params.insert(0, forward_return_1d)
        if forward_return_5d is not None:
            assignments.insert(-1, "forward_return_5d = ?")
            params.insert(-1, forward_return_5d)
        try:
            async with self._db.transaction() as conn:
                await conn.execute(
                    f"UPDATE signal_attribution SET {', '.join(assignments)} WHERE id = ?",
                    (*params, row_id),
                )
        except Exception as e:
            logger.error("Failed to score attribution row %s: %s", row_id, e)

    async def backfill_channel_votes(self, limit: int = 5000) -> int:
        """Fill ``channel_votes`` from the market snapshot of the same session.

        The composite's per-channel values are already stored in
        ``market_snapshots.sub_signals_json`` for every cycle. Taking them from
        there — rather than asking the strategy agent to copy ten numbers into
        its attribution JSON — means the record cannot be mistranscribed, and
        every past row can be filled in, not just new ones.

        Picks the latest snapshot at or before the attribution row (a session
        can run the market-data tool more than once), falling back to the
        session's latest if clocks disagree. Only the row's own instrument's
        snapshot: a session that also read another one (an inverse fund) holds
        that one's channels too, read on its own prices, roughly the mirror
        image. Idempotent: only rows still NULL are touched. Returns the number
        of rows filled.
        """
        try:
            async with self._db.transaction() as conn:
                cur = await conn.execute(
                    "SELECT id, timestamp, session_id, ticker FROM signal_attribution "
                    "WHERE channel_votes IS NULL AND session_id IS NOT NULL "
                    "ORDER BY timestamp LIMIT ?",
                    (limit,),
                )
                rows = [dict(r) for r in await cur.fetchall()]
                filled = 0
                for row in rows:
                    snap = await conn.execute(
                        "SELECT timestamp, sub_signals_json FROM market_snapshots "
                        "WHERE session_id = ? AND sub_signals_json IS NOT NULL "
                        "AND (? IS NULL OR UPPER(ticker) = UPPER(?)) "
                        "ORDER BY (timestamp <= ?) DESC, timestamp DESC LIMIT 1",
                        (row["session_id"], row["ticker"], row["ticker"], row["timestamp"]),
                    )
                    found = await snap.fetchone()
                    if not found:
                        continue
                    votes = channel_votes_from_sub_signals(found["sub_signals_json"])
                    if not votes:
                        continue
                    await conn.execute(
                        "UPDATE signal_attribution SET channel_votes = ? WHERE id = ?",
                        (json.dumps(votes), row["id"]),
                    )
                    filled += 1
                return filled
        except Exception as e:
            logger.error("Failed to backfill channel votes: %s", e)
            return 0

    async def backfill_participation(self, limit: int = 5000) -> int:
        """Fill the participation scale and its counts from the cycle's own record.

        ``composite_unattenuated``, ``participation_scale``,
        ``participation_numerator`` and ``participation_denominator`` are in
        the market-data tool's response every cycle, and that response is kept
        in ``agent_thought_log``. The first two had columns from 2026-09-28,
        but only the strategy agent wrote them and it was never asked to, so
        every row had them empty. Taking them from the record, as
        ``backfill_channel_votes`` does, needs no transcription and fills past
        rows too (the counts exist only from 2026-10-05).

        Uses the session's market-data response at or before the row (the
        latest one if clocks disagree), for the row's own instrument: a session
        that also read an inverse fund has that one's response too. Fills only
        rows whose scale is still empty and never overwrites a value already
        written. Returns the number of rows filled.
        """
        path = "$.response.algo_signal."
        try:
            async with self._db.transaction() as conn:
                cur = await conn.execute(
                    "SELECT id, timestamp, session_id, ticker FROM signal_attribution "
                    "WHERE participation_scale IS NULL AND session_id IS NOT NULL "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                )
                rows = [dict(r) for r in await cur.fetchall()]
                filled = 0
                for row in rows:
                    # One lookup per row with the row's values as parameters, as
                    # backfill_channel_votes does. A single joined query needs the
                    # row's timestamp inside a subquery's ORDER BY, and SQLite
                    # before 3.50 (Ubuntu 24.04 ships 3.45) rejects that with
                    # "no such column".
                    found = await (
                        await conn.execute(
                            f"""
                            SELECT json_extract(meta, '{path}composite_unattenuated') AS unattenuated,
                                   json_extract(meta, '{path}participation_scale') AS scale,
                                   json_extract(meta, '{path}participation_numerator') AS numerator,
                                   json_extract(meta, '{path}participation_denominator')
                                       AS denominator
                            FROM agent_thought_log
                            WHERE session_id = ?
                              AND event_type = 'tool_response'
                              AND content = 'gather_market_data'
                              AND json_valid(meta)
                              AND (? IS NULL
                                   OR json_extract(meta, '$.response.ticker') IS NULL
                                   OR UPPER(json_extract(meta, '$.response.ticker')) = UPPER(?))
                            ORDER BY (timestamp <= ?) DESC, timestamp DESC
                            LIMIT 1
                            """,
                            (row["session_id"], row["ticker"], row["ticker"], row["timestamp"]),
                        )
                    ).fetchone()
                    if not found or found["scale"] is None:
                        continue
                    await conn.execute(
                        "UPDATE signal_attribution SET "
                        "composite_unattenuated = COALESCE(composite_unattenuated, ?), "
                        "participation_scale = ?, "
                        "participation_numerator = COALESCE(participation_numerator, ?), "
                        "participation_denominator = COALESCE(participation_denominator, ?) "
                        "WHERE id = ? AND participation_scale IS NULL",
                        (
                            found["unattenuated"],
                            found["scale"],
                            found["numerator"],
                            found["denominator"],
                            row["id"],
                        ),
                    )
                    filled += 1
                return filled
        except Exception as e:
            logger.error("Failed to backfill participation: %s", e)
            return 0

    async def backfill_algo_only_rows(self, limit: int = 200, primary: str | None = None) -> int:
        """Write the algorithm's call for trading cycles that left no row.

        The strategy agent writes each cycle's row, so a cycle that never
        reached it — a provider outage, a crash, an agent that skipped the
        call — dropped out of the record entirely, and with it the algorithm's
        call, which the system had already computed and stored. That is a hole
        in the control group, and it is not random: it falls on bad-infra
        cycles.

        Every such row is taken from the ``market_snapshots`` row of the same
        session: composite value and direction, the authoring channel, the
        price. News, LLM and final fields stay NULL and ``agent_absent`` is 1,
        so the calibration scores the algorithm on it and leaves the agent-side
        witnesses out instead of counting a flat call nobody made.

        Scope: TRADING cycles from ``cycle_runs`` (scripts and tests that store
        a snapshot are not cycles), no earlier than the first row the agent
        wrote — cycles before the record existed were never part of it.
        Idempotent. Returns the number of rows written.

        A session that read two instruments (the primary and its inverse fund)
        has a snapshot of each; ``primary``'s is the call, else the session's
        latest, as before there was a choice.
        """
        try:
            async with self._db.transaction() as conn:
                cur = await conn.execute(
                    """
                    SELECT c.session_id,
                           COALESCE(
                               (SELECT MAX(t.id) FROM market_snapshots t
                                 WHERE t.session_id = c.session_id
                                   AND t.composite_signal IS NOT NULL
                                   AND UPPER(t.ticker) = UPPER(?)),
                               (SELECT MAX(t.id) FROM market_snapshots t
                                 WHERE t.session_id = c.session_id
                                   AND t.composite_signal IS NOT NULL)
                           ) AS snapshot_id
                    FROM cycle_runs c
                    WHERE c.cycle_type = 'TRADING'
                      AND c.timestamp >= (SELECT MIN(timestamp) FROM signal_attribution
                                          WHERE agent_absent = 0)
                      AND NOT EXISTS (SELECT 1 FROM signal_attribution a
                                      WHERE a.session_id = c.session_id)
                    ORDER BY c.timestamp
                    LIMIT ?
                    """,
                    (primary, limit),
                )
                todo = [dict(r) for r in await cur.fetchall() if r["snapshot_id"] is not None]
                written = 0
                for item in todo:
                    snap = await (
                        await conn.execute(
                            "SELECT timestamp, ticker, close_price, composite_signal, sub_signals_json "
                            "FROM market_snapshots WHERE id = ?",
                            (item["snapshot_id"],),
                        )
                    ).fetchone()
                    traded = await (
                        await conn.execute(
                            "SELECT 1 FROM trades WHERE session_id = ? "
                            "AND action IN ('OPEN', 'CLOSE') LIMIT 1",
                            (item["session_id"],),
                        )
                    ).fetchone()
                    direction, author = algo_call_from_snapshot(
                        snap["composite_signal"],
                        snap["sub_signals_json"],
                    )
                    await conn.execute(
                        """
                        INSERT INTO signal_attribution (
                            timestamp, session_id, ticker,
                            algo_direction, algo_strength, algo_author,
                            traded, price_at_decision, agent_absent
                        ) VALUES (?,?,?,?,?,?,?,?,1)
                        """,
                        (
                            snap["timestamp"],
                            item["session_id"],
                            snap["ticker"],
                            direction,
                            snap["composite_signal"],
                            author,
                            1 if traded else 0,
                            snap["close_price"],
                        ),
                    )
                    written += 1
                if written:
                    logger.info(
                        "Attribution: recorded the algorithm's call for %d cycle(s) "
                        "the strategy agent did not log",
                        written,
                    )
                return written
        except Exception as e:
            logger.error("Failed to backfill algo-only attribution rows: %s", e)
            return 0

    async def scored_rows(self, limit: int = 5000) -> list[dict[str, Any]]:
        """Scored rows, newest first, ready for :mod:`evotrader.backtest.attribution`."""
        cols = [
            "id",
            "timestamp",
            "ticker",
            "algo_direction",
            "algo_strength",
            "algo_author",
            "participation_ratio",
            "news_direction",
            "news_strength",
            "catalyst",
            "llm_direction",
            "llm_conviction",
            "mechanism",
            "final_direction",
            "final_conviction",
            "risk_budget",
            "position_size",
            "traded",
            "forward_return_1d",
            "forward_return_5d",
            # Dislocation depth (migration 0017). Selected here so realised
            # forward returns can be bucketed by how far price actually sat
            # from VWAP, in ATRs, rather than by a z-score whose units move
            # with min_std. Writing these columns without selecting them would
            # leave the measurement loop open: the rows would fill up and the
            # scorer would never see them.
            "deviation_atr",
            "trigger_atr",
            # Every channel's vote on this cycle (migration 0020), so the scorer
            # can judge each channel on its own calls rather than only the one
            # that happened to lead the weighted sum.
            "channel_votes",
            # Written from the stored snapshot because the agent never logged
            # the cycle (migration 0021): algorithm only, agent fields NULL.
            "agent_absent",
        ]
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {', '.join(cols)} FROM signal_attribution "
                    "WHERE scored_at IS NOT NULL ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                )
                return [dict(r) for r in await cur.fetchall()]
        except Exception as e:
            logger.error("Failed to fetch scored attribution rows: %s", e)
            return []


def algo_call_from_snapshot(composite: Any, sub_signals: Any) -> tuple[float, str | None]:
    """``(direction, authoring channel)`` of a stored composite.

    Direction is the composite's sign, 0 when it is below the composite's own
    silence threshold — the same line under which a channel counts as not
    having voted. The author is chosen exactly as the composite chooses
    ``authoring_signal``: the applicable, additive channel that voted with the
    largest weighted contribution.
    """
    from evotrader.algorithms.composite import SIGNAL_EPSILON

    try:
        value = float(composite)
    except (TypeError, ValueError):
        return 0.0, None
    direction = 0.0 if abs(value) < SIGNAL_EPSILON else (1.0 if value > 0 else -1.0)

    if isinstance(sub_signals, str):
        try:
            sub_signals = json.loads(sub_signals)
        except (ValueError, TypeError):
            sub_signals = []
    best: tuple[float, str] | None = None
    for s in sub_signals or []:
        if not isinstance(s, dict) or not s.get("name"):
            continue
        meta = s.get("metadata") or {}
        if meta.get("role") == "multiplier" or not meta.get("applicable", True):
            continue
        try:
            v, w = float(s.get("value") or 0.0), float(s.get("weight") or 0.0)
        except (TypeError, ValueError):
            continue
        if abs(v) < SIGNAL_EPSILON:
            continue
        if best is None or abs(v) * w > best[0]:
            best = (abs(v) * w, str(s["name"]))
    return direction, (best[1] if best else None)


#: Metadata flags kept with a channel's vote when the channel reports them, so
#: the calibration record can split one channel's calls into cohorts: the
#: momentum vote with its stack intact, decayed for a day against it, or under
#: a fast dissent.
_COHORT_FLAGS = ("divergence_applied", "fast_dissent", "fast_dissent_applied")


def channel_votes_from_sub_signals(sub_signals: Any) -> dict[str, dict[str, Any]]:
    """``{channel: {value, weight, applicable, reason}}`` from a snapshot's
    stored sub-signals, plus any of ``_COHORT_FLAGS`` the channel reports. A
    channel that reports no ``applicable`` flag emitted a value without
    qualification, so it counts as applicable."""
    if isinstance(sub_signals, str):
        try:
            sub_signals = json.loads(sub_signals)
        except (ValueError, TypeError):
            return {}
    votes: dict[str, dict[str, Any]] = {}
    for s in sub_signals or []:
        if not isinstance(s, dict) or not s.get("name"):
            continue
        meta = s.get("metadata") or {}
        votes[str(s["name"])] = {
            "value": s.get("value"),
            "weight": s.get("weight"),
            "applicable": bool(meta.get("applicable", True)),
            "reason": meta.get("reason"),
            **{flag: bool(meta[flag]) for flag in _COHORT_FLAGS if flag in meta},
        }
    return votes
