"""Telemetry is kept for a set number of days, not forever.

``telemetry.db`` stores the full text of every model call and nothing removed
it, so it grew without limit. At each start, records older than
``logging.telemetry_retention_days`` (default 30, 0 = keep all) are deleted.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from evotrader.db.telemetry import prune_spans
from evotrader.models.config import LoggingConfig

NOW = 1_800_000_000.0  # a fixed "now", in seconds
DAY_NS = 86400 * 10**9


def _telemetry(path: Path, ages_in_days: list[float]) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE spans (span_id TEXT PRIMARY KEY, name TEXT, "
        "start_time_unix_nano INTEGER, end_time_unix_nano INTEGER, attributes_json TEXT)"
    )
    now_ns = int(NOW * 1e9)
    for i, age in enumerate(ages_in_days):
        start = now_ns - int(age * DAY_NS)
        conn.execute(
            "INSERT INTO spans VALUES (?, 'call_llm', ?, ?, '{}')", (f"s{i}", start, start + 1)
        )
    conn.commit()
    conn.close()
    return path


def _ages_left(path: Path) -> list[float]:
    rows = sqlite3.connect(path).execute("SELECT start_time_unix_nano FROM spans").fetchall()
    return sorted(round((NOW * 1e9 - start) / DAY_NS, 1) for (start,) in rows)


def test_older_records_go_and_recent_ones_stay(tmp_path) -> None:
    db = _telemetry(tmp_path / "telemetry.db", [1, 10, 29.9, 30.1, 45, 100])
    assert prune_spans(db, 30, now=NOW) == 3
    assert _ages_left(db) == [1.0, 10.0, 29.9]


def test_zero_keeps_everything(tmp_path) -> None:
    db = _telemetry(tmp_path / "telemetry.db", [1, 100, 400])
    assert prune_spans(db, 0, now=NOW) == 0
    assert len(_ages_left(db)) == 3


def test_nothing_to_remove_is_not_an_error(tmp_path) -> None:
    assert prune_spans(tmp_path / "missing.db", 30, now=NOW) == 0
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    assert prune_spans(empty, 30, now=NOW) == 0, "a file with no spans table yet"


def test_the_setting_defaults_to_30_days_and_rejects_negatives() -> None:
    assert LoggingConfig().telemetry_retention_days == 30
    assert LoggingConfig(telemetry_retention_days=0).telemetry_retention_days == 0
    with pytest.raises(ValueError):
        LoggingConfig(telemetry_retention_days=-1)


def test_startup_applies_it(tmp_path, monkeypatch) -> None:
    """The cleanup runs where telemetry is set up, with the configured days."""
    import evotrader.main as main

    # Keep the real exporter and the global tracer out of it: they would outlive
    # this test. The two variables the function sets are restored afterwards.
    monkeypatch.setattr(
        "google.adk.telemetry.sqlite_span_exporter.SqliteSpanExporter", lambda db_path: None
    )
    monkeypatch.setattr("opentelemetry.trace.get_tracer_provider", lambda: object())
    monkeypatch.setenv("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "true")
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "SPAN_ONLY")
    calls: list[tuple[Path, int]] = []
    monkeypatch.setattr(
        "evotrader.db.telemetry.prune_spans",
        lambda path, days, now=None: calls.append((Path(path), days)) or 0,
    )
    main._init_telemetry(tmp_path, 7)
    assert calls == [(tmp_path / "telemetry.db", 7)]
