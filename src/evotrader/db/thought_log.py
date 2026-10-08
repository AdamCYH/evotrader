"""Db helper for recording structured agent thoughts and decisions."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from evotrader.db.connection import Database
from evotrader.indicators import normalize_snapshot_payload
from evotrader.tools.asset_context import primary_ticker

logger = logging.getLogger(__name__)


class ThoughtLogger:
    """Thought Logger for recording agent reasoning and tool executions in SQLite."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record_event(
        self,
        session_id: str | None,
        agent_name: str,
        event_type: str,
        content: str,
        meta: dict[str, Any] | None = None,
    ) -> int:
        """Record an event (thought, tool_call, tool_response) to the database."""
        meta_str = json.dumps(meta) if meta else None

        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO agent_thought_log (
                    timestamp, session_id, agent_name, event_type, content, meta
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(UTC).isoformat(),
                    session_id,
                    agent_name,
                    event_type,
                    content,
                    meta_str,
                ),
            )
            inserted_id = cursor.lastrowid

            # If this is a market data response, log a snapshot
            if event_type == "tool_response" and content == "gather_market_data" and meta:
                resp = meta.get("response", {})
                normalized = normalize_snapshot_payload(resp)
                indicators = normalized.get("indicators", {})
                if indicators:
                    ticker = normalized.get("ticker") or primary_ticker()
                    close_price = normalized.get("close")
                    vwap = indicators.get("vwap")
                    vwap_dist = indicators.get("vwap_dist")
                    if vwap and close_price and vwap_dist is None:
                        vwap_dist = (close_price - vwap) / vwap

                    sub_signals = normalized.get("sub_signals", [])
                    sub_signals_str = json.dumps(sub_signals) if sub_signals else None
                    indicators_str = json.dumps(indicators) if indicators else None

                    await conn.execute(
                        """
                        INSERT INTO market_snapshots (
                            timestamp, session_id, ticker, close_price, rsi_14,
                            bollinger_upper, bollinger_middle, bollinger_lower, bollinger_width, vwap, vwap_dist,
                            composite_signal, regime, regime_confidence, sub_signals_json, indicators_json,
                            algo_version, daily_change_pct, gap_pct
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            datetime.now(UTC).isoformat(),
                            session_id,
                            ticker,
                            close_price,
                            indicators.get("rsi_14"),
                            indicators.get("bollinger_upper"),
                            indicators.get("bollinger_middle"),
                            indicators.get("bollinger_lower"),
                            indicators.get("bollinger_width"),
                            vwap,
                            vwap_dist,
                            normalized.get("composite_signal"),
                            normalized.get("regime"),
                            normalized.get("regime_confidence"),
                            sub_signals_str,
                            indicators_str,
                            normalized.get("algo_version"),
                            normalized.get("daily_change_pct"),
                            normalized.get("gap_pct"),
                        ),
                    )

            assert inserted_id is not None
            return inserted_id

    async def get_recent_thoughts(
        self,
        limit: int = 100,
        session_id: str | None = None,
        agent_name: str | None = None,
        event_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch historical agent thoughts."""
        query = "SELECT * FROM agent_thought_log WHERE 1=1"
        params: list[Any] = []

        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)

        if agent_name:
            query += " AND agent_name = ?"
            params.append(agent_name)

        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        async with self._db.connection() as conn:
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return list(rows)

    async def get_event(self, event_id: int) -> dict[str, Any] | None:
        """Fetch a single event by id, including its full ``meta`` payload.

        Used by ``get_tool_response`` so an agent can drill into one payload that
        ``query_cycle_thoughts`` summarised.
        """
        async with self._db.connection() as conn:
            cursor = await conn.execute("SELECT * FROM agent_thought_log WHERE id = ?", (event_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_most_recent_session_id(self) -> str | None:
        """Get the session_id from the most recent thought log entry."""
        async with self._db.connection() as conn:
            cursor = await conn.execute(
                "SELECT session_id FROM agent_thought_log ORDER BY timestamp DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            if row:
                return row.get("session_id") if isinstance(row, dict) else row[0]
            return None

    async def get_final_thoughts(self, session_id: str) -> list[dict[str, Any]]:
        """Fetch only the final (last) thought from each agent in a session.

        Harness diagnostics are excluded in both shapes they exist in. New rows
        carry ``event_type='runtime'`` (migration 0019). The 97 rows written
        before that carry ``event_type='thought'`` with a ``[runtime]`` prefix,
        and history is not rewritten to make a query simpler — so the content
        filter stays. Without it the digest reported ``[runtime] quota ok`` as
        an agent's final reasoning, because the quota check is emitted after the
        final report and this ranks by id DESC.
        """
        query = """
            WITH ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY agent_name ORDER BY id DESC
                ) as rn
                FROM agent_thought_log
                WHERE session_id = ? AND event_type = 'thought'
                  AND content NOT LIKE '[runtime]%'
            )
            SELECT id, timestamp, session_id, agent_name, event_type, content
            FROM ranked WHERE rn = 1
            ORDER BY id ASC
        """
        async with self._db.connection() as conn:
            cursor = await conn.execute(query, (session_id,))
            rows = await cursor.fetchall()
            return list(rows)

    async def get_unique_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        """Fetch unique session runs from the thought logs."""
        query = """
            SELECT t.session_id,
                   MIN(t.timestamp) as start_time,
                   MAX(t.timestamp) as end_time,
                   COUNT(*) as event_count,
                   MAX(CASE WHEN t.event_type = 'tool_response' AND t.content = 'record_trade' THEN 1 ELSE 0 END) as has_trades,
                   MAX(CASE WHEN t.agent_name = 'evolution' THEN 1 ELSE 0 END) as is_evolution,
                   COALESCE(
                       MAX(CASE WHEN t.event_type = 'cycle_complete' THEN json_extract(t.meta, '$.status') ELSE NULL END),
                       MAX(CASE
                           WHEN r.status = 'SUCCESS' THEN 'complete'
                           WHEN r.status = 'FAILED' THEN 'error'
                           WHEN r.status = 'RUNNING' THEN 'running'
                           ELSE NULL
                       END)
                   ) as cycle_status,
                   MAX(CASE WHEN t.event_type = 'cycle_complete' THEN json_extract(t.meta, '$.stages_completed') ELSE NULL END) as stages_completed
            FROM agent_thought_log t
            LEFT JOIN cycle_runs r ON t.session_id = r.session_id
            WHERE t.session_id IS NOT NULL AND t.session_id != ''
            GROUP BY t.session_id
            ORDER BY start_time DESC
            LIMIT ?
        """
        async with self._db.connection() as conn:
            cursor = await conn.execute(query, (limit,))
            rows = await cursor.fetchall()
            results = []
            for row in rows:
                row_dict = dict(row)
                if row_dict.get("stages_completed"):
                    try:
                        row_dict["stages_completed"] = json.loads(row_dict["stages_completed"])
                    except Exception:
                        row_dict["stages_completed"] = []
                else:
                    row_dict["stages_completed"] = []
                results.append(row_dict)
            return results

    async def get_session_events(self, session_id: str) -> list[dict[str, Any]]:
        """Fetch all events for a session in chronological order.

        Used by ``EvolutionService.continue_session()`` to reconstruct a
        timed-out session's conversation history.

        Returns:
            List of dicts with keys: agent_name, event_type, content, meta, timestamp.
        """
        async with self._db.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT agent_name, event_type, content, meta, timestamp
                FROM agent_thought_log
                WHERE session_id = ?
                ORDER BY id ASC
                """,
                (session_id,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def record_run_start(self, session_id: str, cycle_type: str) -> None:
        """Record the start of a trading or self-evolution run."""
        async with self._db.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO cycle_runs (session_id, timestamp, cycle_type, status)
                VALUES (?, ?, ?, 'RUNNING')
                """,
                (session_id, datetime.now(UTC).isoformat(), cycle_type),
            )
        logger.info("Cycle run start recorded: session_id=%s, type=%s", session_id, cycle_type)

    async def record_run_completion(
        self,
        session_id: str,
        status: str,
        error: str | None = None,
        summary: str | None = None,
    ) -> None:
        """Record the outcome of a trading or self-evolution run."""
        async with self._db.transaction() as conn:
            await conn.execute(
                """
                UPDATE cycle_runs
                SET status = ?, error = ?, summary = ?
                WHERE session_id = ?
                """,
                (status, error, summary, session_id),
            )
        logger.info(
            "Cycle run completion recorded: session_id=%s, status=%s, error=%s",
            session_id,
            status,
            error,
        )

    async def get_evolution_runs(self) -> list[dict[str, Any]]:
        """Fetch all recorded self-evolution runs."""
        async with self._db.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT c.session_id, c.timestamp, c.status, c.error, c.summary,
                       COUNT(a.id) as llm_call_count
                FROM cycle_runs c
                LEFT JOIN agent_thought_log a ON a.session_id = c.session_id AND a.event_type = 'thought'
                WHERE c.cycle_type = 'EVOLUTION'
                GROUP BY c.session_id
                ORDER BY c.timestamp DESC
                """
            )
            rows = await cursor.fetchall()
            return list(rows)

    async def delete_cycle_logs(self, session_id: str) -> None:
        """Delete all database logs for a session from thought logs and cycle runs."""
        async with self._db.transaction() as conn:
            await conn.execute("DELETE FROM agent_thought_log WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM cycle_runs WHERE session_id = ?", (session_id,))
        logger.info("Deleted cycle logs from DB: session_id=%s", session_id)

    async def get_composite_readings(self, since: str | None = None) -> list[dict[str, Any]]:
        """Every stored combined-signal reading, oldest first.

        ``since`` is a UTC ISO timestamp; None returns them all. Only the four
        fields the account chart's signal overlay needs (timestamp, ticker,
        composite_signal, regime), with no row limit, unlike
        :meth:`get_market_snapshots`, which caps the market panel's history.
        """
        query = (
            "SELECT timestamp, ticker, composite_signal, regime FROM market_snapshots "
            "WHERE composite_signal IS NOT NULL"
        )
        params: tuple[str, ...] = ()
        if since:
            query += " AND timestamp >= ?"
            params = (since,)
        query += " ORDER BY timestamp"
        async with self._db.connection() as conn, conn.execute(query, params) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def snapshot_tickers(self) -> list[dict[str, Any]]:
        """Each instrument with stored snapshots: ticker, count, last timestamp.

        Newest first. The market panel offers these as its instrument choice.
        """
        query = (
            "SELECT ticker, COUNT(*) AS count, MAX(timestamp) AS last FROM market_snapshots "
            "WHERE ticker IS NOT NULL AND ticker != '' GROUP BY ticker ORDER BY last DESC"
        )
        async with self._db.connection() as conn, conn.execute(query) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def get_market_snapshots(
        self, limit: int = 20, ticker: str | None = None
    ) -> list[dict[str, Any]]:
        """The most recent snapshots (indicators and the algorithm's signal), newest first.

        ``ticker`` keeps one instrument's. Every snapshot carries its own
        instrument's signal, read on that instrument's prices: an inverse fund's
        reads roughly opposite to the stock it tracks, so a list holding both
        zigzags between the two.
        """
        where, params = "", (limit,)
        if ticker:
            where, params = "WHERE ticker = ?", (ticker, limit)
        points = []
        async with self._db.connection() as conn:
            async with conn.execute(
                f"""
                SELECT
                    timestamp, ticker, session_id, rsi_14, bollinger_upper, bollinger_middle,
                    bollinger_lower, bollinger_width, vwap, vwap_dist, close_price,
                    composite_signal, regime, regime_confidence, sub_signals_json, indicators_json,
                    algo_version, daily_change_pct, gap_pct
                FROM market_snapshots
                {where}
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                params,
            ) as cursor:
                rows = await cursor.fetchall()

            for r in rows:
                sub_signals = []
                sub_str = r["sub_signals_json"] if "sub_signals_json" in r.keys() else None
                if sub_str:
                    try:
                        sub_signals = json.loads(sub_str)
                    except Exception:
                        sub_signals = []

                indicators = {}
                ind_str = r["indicators_json"] if "indicators_json" in r.keys() else None
                if ind_str:
                    try:
                        indicators = json.loads(ind_str)
                    except Exception:
                        indicators = {}

                # If indicators_json was null (older snapshot), populate from columns
                if not indicators:
                    for key in [
                        "rsi_14",
                        "bollinger_upper",
                        "bollinger_middle",
                        "bollinger_lower",
                        "bollinger_width",
                        "vwap",
                        "vwap_dist",
                    ]:
                        if key in r.keys() and r[key] is not None:
                            indicators[key] = r[key]
                # vwap_dist lives in its own column (a fraction, computed at
                # write time) and was never in indicators_json, so the console's
                # "VWAP Dist" read blank after every reload.
                if indicators.get("vwap_dist") is None and r.get("vwap_dist") is not None:
                    indicators["vwap_dist"] = r["vwap_dist"]

                snap_dict = {
                    "timestamp": r["timestamp"],
                    "ticker": (r["ticker"] if "ticker" in r.keys() else primary_ticker()),
                    "session_id": r["session_id"] if "session_id" in r.keys() else None,
                    "close": r["close_price"],
                    "close_price": r["close_price"],
                    "composite_signal": r["composite_signal"]
                    if "composite_signal" in r.keys()
                    else None,
                    "regime": r["regime"] if "regime" in r.keys() else None,
                    "regime_confidence": r["regime_confidence"]
                    if "regime_confidence" in r.keys()
                    else None,
                    "sub_signals": sub_signals,
                    "indicators": indicators,
                    # Migration 0022; NULL on rows written before it.
                    "algo_version": r.get("algo_version"),
                    "daily_change_pct": r.get("daily_change_pct"),
                    "gap_pct": r.get("gap_pct"),
                }
                points.append(normalize_snapshot_payload(snap_dict))
        return points
