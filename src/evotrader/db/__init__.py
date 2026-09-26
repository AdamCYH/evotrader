"""Database layer for trade journal, metrics, and configuration snapshots.

Uses SQLite via ``aiosqlite`` for async access. The schema is defined in
``schema.sql`` and applied automatically on first run.
"""

from .evolution_log import EvolutionLogStore
from .telemetry import TelemetryReader

__all__ = [
    "EvolutionLogStore",
    "TelemetryReader",
]
