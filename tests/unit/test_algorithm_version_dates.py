"""Every algorithm version shows when it was proposed, and what it changed from.

The console read the date from the version's metadata and, failing that, from
the evolution log. Neither worked. The evolution tool saved versions without a
date, and the fallback called ``get_by_new_version``, a method the evolution-log
store does not have. The call raised, a bare ``except: pass`` swallowed it, and
every proposal read "Proposed: Unknown date". The same lookup supplies the
version a proposal was made against, so the "changed from" diff silently fell
back to whichever folder happened to sort before it.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from evotrader.algorithms.registry import AlgorithmRegistry
from evotrader.config import AppConfig
from evotrader.db.connection import Database
from evotrader.db.evolution_log import EvolutionLogStore
from evotrader.web.server import create_app


@pytest.fixture(autouse=True)
def _console_password(monkeypatch) -> None:
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test_password")


def _write_version(algo_dir: Path, version: str, rsi: int, **meta) -> None:
    d = algo_dir / version
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(f"mean_reversion:\n  rsi_oversold: {rsi}\n")
    (d / "metadata.yaml").write_text(yaml.safe_dump({"name": version, "version": version, **meta}))


@pytest.fixture
def console(tmp_path):
    """A console over a real evolution log and three versions.

    Folder order is v001 < v002 < v003, but v003 was proposed against v001:
    a diff "against the folder before it" would compare it with v002.
    """
    config = AppConfig(data_dir=tmp_path / "data")
    algo_dir = config.algorithms_dir
    _write_version(algo_dir, "v001_initial", 30, created_by="system")
    _write_version(algo_dir, "v002_sibling", 33, created_by="evolution_agent")
    _write_version(algo_dir, "v003_from_base", 35, created_by="evolution_agent", status="proposed")
    _write_version(
        algo_dir,
        "v004_no_log_row",
        36,
        created_by="evolution_agent",
        status="proposed",
        parent_version="v001_initial",
    )
    (algo_dir / "registry.yaml").write_text(
        yaml.safe_dump(
            {
                "versions": [
                    {"version": v, "name": v}
                    for v in ("v001_initial", "v002_sibling", "v003_from_base", "v004_no_log_row")
                ]
            }
        )
    )
    (algo_dir / "active.yaml").write_text("active_version: v001_initial")

    db_path = config.data_dir / "db" / "evotrader.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    async def _log() -> None:
        db = Database(db_path)
        await db.initialize()
        await EvolutionLogStore(db).insert(
            change_type="ALGORITHM_PARAMS",
            risk_level="LOW",
            target_component="strategy",
            old_version="v001_initial",
            new_version="v003_from_base",
            reasoning="r",
        )
        await db.close()

    asyncio.run(_log())
    logged_at = (
        sqlite3.connect(db_path)
        .execute("SELECT timestamp FROM evolution_log WHERE new_version = 'v003_from_base'")
        .fetchone()[0]
    )

    db = MagicMock()
    db._db_path = tmp_path / "elsewhere.db"  # so the console opens the log itself
    memory = MagicMock()
    memory.sync_experiences_from_journal = AsyncMock()
    app = create_app(
        db=db,
        journal=MagicMock(),
        metrics=MagicMock(),
        mcp_toolset=MagicMock(),
        config=config,
        runner_fn=MagicMock(),
        memory=memory,
        evolution_service=MagicMock(is_running=MagicMock(return_value=False)),
    )
    with TestClient(app) as tc:
        tc.headers.update({"Authorization": "Bearer test_password"})
        yield tc, config, logged_at


class TestTheDate:
    def test_a_logged_version_shows_its_log_date(self, console) -> None:
        tc, _, logged_at = console
        meta = tc.get("/api/algorithms/v003_from_base").json()["metadata"]
        assert logged_at and meta["created_at"] == str(logged_at)

    def test_the_version_list_carries_it_too(self, console) -> None:
        tc, _, logged_at = console
        versions = {v["version"]: v for v in tc.get("/api/algorithms").json()["versions"]}
        assert versions["v003_from_base"]["created_at"] == str(logged_at)

    def test_saving_a_version_records_its_date(self, tmp_path) -> None:
        registry = AlgorithmRegistry(tmp_path / "algorithms")
        saved = registry.save_version(
            "v002_a", {"x": 1}, {"name": "a", "created_by": "evolution_agent"}
        ).name
        meta = registry.load_metadata(saved)
        assert meta["created_at"], "a version saved today must not read 'Unknown date'"
        entry = next(v for v in registry.list_versions() if v["version"] == saved)
        assert entry["created_at"] == meta["created_at"]

    def test_a_date_the_caller_gives_is_kept(self, tmp_path) -> None:
        registry = AlgorithmRegistry(tmp_path / "algorithms")
        saved = registry.save_version(
            "v002_a", {"x": 1}, {"name": "a", "created_at": "2026-06-18"}
        ).name
        assert registry.load_metadata(saved)["created_at"] == "2026-06-18"


class TestTheDateReadsTheSameInEveryBrowser:
    """SQLite's own ``datetime('now')`` has no zone; browsers disagree on it."""

    def test_a_log_date_carries_its_zone(self, console) -> None:
        tc, _, _ = console
        created = tc.get("/api/algorithms/v003_from_base").json()["metadata"]["created_at"]
        assert "T" in created and created.endswith("+00:00")

    def test_a_zone_less_sqlite_time_is_read_as_utc(self) -> None:
        from evotrader.web.server import _log_time_iso

        assert _log_time_iso({"created_at": "2026-10-02 21:06:15"}) == "2026-10-02T21:06:15+00:00"
        assert _log_time_iso({"timestamp": "2026-10-02T21:06:15+00:00", "created_at": "x"}) == (
            "2026-10-02T21:06:15+00:00"
        )
        assert _log_time_iso({}) is None


class TestWhatItChangedFrom:
    def test_the_diff_is_against_the_version_it_was_proposed_from(self, console) -> None:
        tc, _, _ = console
        data = tc.get("/api/algorithms/v003_from_base").json()
        assert data["diff_against"] == "v001_initial", "not v002_sibling, the folder before it"
        assert "rsi_oversold: 30" in data["diff"] and "rsi_oversold: 35" in data["diff"]

    def test_without_a_log_row_the_recorded_parent_is_used(self, console) -> None:
        tc, _, _ = console
        assert tc.get("/api/algorithms/v004_no_log_row").json()["diff_against"] == "v001_initial"
