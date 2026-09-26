"""Db helper for recording and querying strategy evolution proposals and status."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from evotrader.db.connection import Database

logger = logging.getLogger(__name__)


class EvolutionLogStore:
    """Evolution log repository for the self-evolving agent."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Fetch recent evolution log entries."""
        async with self._db.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM evolution_log ORDER BY timestamp DESC LIMIT ?", (limit,)
            )
            rows = await cursor.fetchall()
            return list(rows)

    async def get_by_version(self, version: str) -> dict[str, Any] | None:
        """Fetch a specific evolution log entry by new_version."""
        async with self._db.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM evolution_log WHERE new_version = ? LIMIT 1", (version,)
            )
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None

    async def get_created_at(self, version: str) -> str | None:
        """Fetch the creation timestamp of a specific evolution version."""
        async with self._db.connection() as conn:
            cursor = await conn.execute(
                "SELECT created_at FROM evolution_log WHERE new_version = ? LIMIT 1", (version,)
            )
            row = await cursor.fetchone()
            if row:
                return row["created_at"]
            return None

    async def delete_by_version(self, version: str) -> None:
        """Delete an evolution log entry by its new_version."""
        try:
            async with self._db.transaction() as conn:
                await conn.execute("DELETE FROM evolution_log WHERE new_version = ?", (version,))
        except Exception as db_err:
            logger.warning("Failed to delete from evolution_log: %s", db_err)

    async def insert(
        self,
        change_type: str,
        risk_level: str,
        target_component: str,
        old_version: str,
        new_version: str,
        reasoning: str,
        expected_impact: str | None = None,
        metrics_before: dict[str, Any] | None = None,
        metrics_after: dict[str, Any] | None = None,
        code_diffs: list[dict[str, Any]] | None = None,
        status: str = "PROPOSED",
        session_id: str | None = None,
    ) -> None:
        """Insert a new evolution log entry."""
        metrics_before_str = json.dumps(metrics_before or {})
        metrics_after_str = json.dumps(metrics_after) if metrics_after else None
        code_diffs_str = json.dumps(code_diffs) if code_diffs else None

        try:
            async with self._db.transaction() as conn:
                await conn.execute(
                    """
                    INSERT INTO evolution_log (
                        session_id, timestamp, change_type, risk_level, target_component,
                        old_version, new_version, reasoning, expected_impact,
                        metrics_before, metrics_after, code_diffs, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        datetime.now(UTC).isoformat(),
                        change_type,
                        risk_level,
                        target_component,
                        old_version,
                        new_version,
                        reasoning,
                        expected_impact,
                        metrics_before_str,
                        metrics_after_str,
                        code_diffs_str,
                        status,
                    ),
                )
                logger.info(
                    "Logged evolution proposal to DB: %s (%s -> %s)",
                    change_type,
                    old_version,
                    new_version,
                )
        except Exception as e:
            logger.error("Failed to log evolution proposal to DB: %s", e)

    async def delete_by_session(self, session_id: str) -> None:
        """Delete all evolution log entries matching a session_id."""
        try:
            async with self._db.transaction() as conn:
                await conn.execute("DELETE FROM evolution_log WHERE session_id = ?", (session_id,))
        except Exception as e:
            logger.error("Failed to delete evolution logs by session: %s", e)

    async def update_status(
        self,
        version: str,
        status: str,
        target_component: str | None = None,
        change_types: tuple[str, ...] | None = None,
    ) -> bool:
        """Update the status of an evolution log entry by its new_version.

        Returns True when a row was actually updated.

        This used to return None and swallow every exception, so a write that
        violated the status CHECK constraint was logged and forgotten while the
        caller reported success. The web UI's reject endpoints hit exactly that:
        REJECTED was missing from the constraint, so rejections silently did
        nothing and proposals accumulated in the review queue forever. Callers
        must check the return value.
        """
        query = "UPDATE evolution_log SET status = ? WHERE new_version = ?"
        params = [status, version]

        if target_component:
            query += " AND target_component = ?"
            params.append(target_component)

        if change_types:
            placeholders = ", ".join(["?"] * len(change_types))
            query += f" AND change_type IN ({placeholders})"
            params.extend(change_types)

        try:
            async with self._db.transaction() as conn:
                cur = await conn.execute(query, params)
                if cur.rowcount == 0:
                    logger.warning(
                        "No evolution_log row matched version=%s (component=%s) — "
                        "status not changed to %s",
                        version,
                        target_component,
                        status,
                    )
                    return False
                logger.info(
                    "Updated evolution status to %s for version %s (%d row(s))",
                    status,
                    version,
                    cur.rowcount,
                )
                return True
        except Exception as e:
            logger.error("Failed to update evolution status for %s to %s: %s", version, status, e)
            return False
