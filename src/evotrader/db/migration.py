"""Lightweight SQLite database migration system."""

import logging
import re
import sqlite3
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# OperationalError messages that are safe to ignore during idempotent retries.
# These indicate that part of a migration already ran (e.g. a previous attempt
# created a table/column before crashing) and re-running is harmless.
_IGNORABLE_ERRORS: tuple[re.Pattern, ...] = (
    re.compile(r"duplicate column name", re.IGNORECASE),
    re.compile(r"table .+ already exists", re.IGNORECASE),
    re.compile(r"index .+ already exists", re.IGNORECASE),
)


def _is_ignorable(error: sqlite3.OperationalError) -> bool:
    """Return True if the error is safe to skip during migration replay."""
    msg = str(error)
    return any(pat.search(msg) for pat in _IGNORABLE_ERRORS)


async def apply_migrations(conn: aiosqlite.Connection) -> None:
    """Read .sql files from migrations/ and apply pending ones."""
    # Ensure schema_version table exists
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version TEXT PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # Get applied migrations
    cursor = await conn.execute("SELECT version FROM schema_version")
    applied = {row[0] for row in await cursor.fetchall()}

    # Get available migrations from directory
    if not MIGRATIONS_DIR.exists():
        logger.warning(f"Migrations directory not found: {MIGRATIONS_DIR}")
        return

    migration_files = sorted(f for f in MIGRATIONS_DIR.iterdir() if f.name.endswith(".sql"))

    pending = [f for f in migration_files if f.name not in applied]

    if not pending:
        logger.debug("Database schema is up to date.")
        return

    logger.info(f"Applying {len(pending)} pending migrations...")

    for file_path in pending:
        version = file_path.name
        logger.debug(f"Applying migration: {version}")

        with open(file_path, encoding="utf-8") as f:
            sql_script = f.read()

        try:
            # Execute statement by statement to gracefully handle idempotent
            # retries (e.g. ADD COLUMN on existing DBs, table recreation after
            # a previous partial run).
            statements = [s.strip() for s in sql_script.split(";") if s.strip()]
            for stmt in statements:
                try:
                    await conn.execute(stmt)
                except sqlite3.OperationalError as e:
                    if _is_ignorable(e):
                        logger.debug(f"Ignoring idempotent error in {version}: {e}")
                    else:
                        raise

            await conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            await conn.commit()
            logger.debug(f"Successfully applied {version}")
        except Exception as e:
            logger.error(f"Failed to apply migration {version}: {e}")
            await conn.rollback()
            raise

    logger.info("Database migrations complete.")
