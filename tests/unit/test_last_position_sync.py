"""The latest position sync's outcome reaches every trading cycle.

Found 2026-09-29 by the evolution agent's code review. The daily sync runs
between cycles and its result went nowhere a cycle records, so its cost-basis
check (do the journal's lots agree with the broker's average cost?) could be
verified only by inference. The sync now keeps its latest result in the data
folder, and get_open_positions, which the agents call every cycle, shows it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from evotrader.agents import tools
from evotrader.db.journal import TradeJournal
from evotrader.db.reconciliation import read_last_sync, save_last_sync

CHECK = {"sources_disagree": False, "journal_weighted_cost": 96.0, "broker_average_cost": 96.0}
SYNC = {"ticker": "SPY", "status": "IN_SYNC", "cost_basis_check": CHECK, "message": "in sync"}


def test_the_sync_result_is_kept_in_the_data_folder(tmp_path) -> None:
    save_last_sync(tmp_path, SYNC, now=datetime(2026, 3, 3, 21, 0, tzinfo=UTC))

    assert read_last_sync(tmp_path) == {
        "at": "2026-03-03T21:00:00+00:00",
        "ticker": "SPY",
        "status": "IN_SYNC",
        "cost_basis_check": CHECK,
    }


def test_nothing_is_there_before_the_first_sync(tmp_path) -> None:
    assert read_last_sync(tmp_path) is None


async def test_every_cycle_sees_it_next_to_the_open_orders(db, tmp_path, monkeypatch) -> None:
    save_last_sync(tmp_path, SYNC)
    monkeypatch.setattr(tools, "_journal", TradeJournal(db))
    monkeypatch.setattr(tools, "_config", SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(tools, "_open_positions_cache", None)

    result = await tools.get_open_positions()

    assert result["last_position_sync"]["cost_basis_check"] == CHECK
    assert "pending_orders" in result
