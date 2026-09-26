"""Async SQLite connection management.

Provides a context-managed database connection that automatically applies
the schema on first use and enables WAL mode for concurrent read performance.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from .migration import apply_migrations

logger = logging.getLogger(__name__)


class WriteTurns:
    """Hand out one transaction at a time on a shared connection.

    SQLite allows one open transaction per connection, and this app shares one
    connection between every task. The agent framework runs an agent's tool calls
    side by side, so two writes can arrive together: on 2026-09-25 the executor
    recorded three trades at once, the second BEGIN failed with "cannot start a
    transaction within a transaction", and a trade row was lost. Writers now wait
    their turn instead.

    A transaction is rolled back on ANY exit that is not a clean finish,
    cancellation included. Rolling back only on ``Exception`` let a cancelled
    write leave its transaction open, and every later write failed.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._holder: asyncio.Task | None = None

    @asynccontextmanager
    async def transaction(self, conn: aiosqlite.Connection) -> AsyncIterator[aiosqlite.Connection]:
        me = asyncio.current_task()
        if me is not None and self._holder is me:
            # Waiting would never end: the turn belongs to this same task.
            raise RuntimeError(
                "This task already has a transaction open on this connection, and "
                "SQLite has no nested transactions. Do the inner writes on the "
                "outer transaction's connection instead."
            )
        async with self._lock:
            self._holder = me
            try:
                await conn.execute("BEGIN")
                try:
                    yield conn
                    await conn.commit()
                except BaseException:
                    # Shielded so a second cancellation cannot stop the rollback
                    # half way; the next writer's BEGIN queues behind it either way.
                    await asyncio.shield(conn.rollback())
                    raise
            finally:
                self._holder = None


class Database:
    """Async SQLite database wrapper.

    Usage::

        db = Database(path)
        await db.initialize()

        async with db.connection() as conn:
            await conn.execute("SELECT ...")

        await db.close()
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._connection: aiosqlite.Connection | None = None
        self._turns = WriteTurns()

    async def initialize(self) -> None:
        """Open the database, enable WAL mode, and apply schema."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(str(self._db_path))

        # Enable WAL mode for better concurrent read performance
        await self._connection.execute("PRAGMA journal_mode=WAL")
        # Enable foreign keys
        await self._connection.execute("PRAGMA foreign_keys=ON")

        await apply_migrations(self._connection)

        # Return rows as dicts everywhere so downstream `.get()` access is safe.
        # Must be set on the connection (not post-execute on a cursor).
        # Set AFTER _apply_schema() so PRAGMA table_info index-based access still works.
        self._connection.row_factory = dict_factory

        logger.info("Database initialized at %s", self._db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None
            logger.info("Database connection closed")

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """Yield an active database connection.

        Raises:
            RuntimeError: If the database has not been initialized.
        """
        if self._connection is None:
            raise RuntimeError("Database not initialized. Call `await db.initialize()` first.")
        yield self._connection

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Yield a connection inside an explicit transaction — the way to write.

        Transactions take turns (see :class:`WriteTurns`). Commits on a clean
        finish and rolls back on any other exit, cancellation included. Every
        write goes through here: a write that commits on its own can land in the
        middle of another task's transaction and commit half of it.
        """
        async with self.connection() as conn, self._turns.transaction(conn) as tx:
            yield tx


def dict_factory(cursor: aiosqlite.Cursor, row: tuple) -> dict:  # type: ignore[type-arg]
    """Row factory that converts SQLite rows to dictionaries."""
    if cursor.description:
        return {col[0]: value for col, value in zip(cursor.description, row, strict=False)}
    return dict(enumerate(row))
