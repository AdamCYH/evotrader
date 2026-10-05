"""The attribution row gets the participation scale and its counts from the cycle's record.

See: data/evolution/reviews/
20261005_211602_participation_attenuation_counts_zero_weight_shadow_channels.md

``composite_unattenuated`` and ``participation_scale`` had columns from
migration 0024, but only the strategy agent wrote the row and it was never
asked for them, so they stayed empty on every row. The market-data tool reports
both every cycle, now with the two counts beside them, and its response is kept
in ``agent_thought_log``. The backfill takes them from there, as the channel
votes are taken from the stored snapshot.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evotrader.db.signal_attribution import SignalAttributionStore

_T = datetime(2026, 3, 2, 16, 30, tzinfo=UTC)


async def _market_data_response(db, session: str, when: datetime, algo_signal: dict | str) -> None:
    meta = (
        algo_signal
        if isinstance(algo_signal, str)
        else json.dumps({"response": {"ticker": "XYZ", "algo_signal": algo_signal}})
    )
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO agent_thought_log (timestamp, session_id, agent_name, event_type, "
            "content, meta) VALUES (?,?,?,?,?,?)",
            (
                when.isoformat(),
                session,
                "orchestrator",
                "tool_response",
                "gather_market_data",
                meta,
            ),
        )


async def _row(db, session: str, when: datetime, **kwargs) -> tuple[SignalAttributionStore, int]:
    store = SignalAttributionStore(db)
    rid = await store.record(
        "XYZ", session_id=session, algo_direction=1.0, timestamp=when.isoformat(), **kwargs
    )
    assert rid is not None
    return store, rid


async def _get(db, rid: int) -> dict:
    async with db.connection() as conn:
        cur = await conn.execute(
            "SELECT composite_unattenuated, participation_scale, participation_numerator, "
            "participation_denominator FROM signal_attribution WHERE id = ?",
            (rid,),
        )
        return dict(await cur.fetchone())


_SCALED = {
    "composite_signal": 0.1,
    "composite_unattenuated": 0.2,
    "participation_scale": 0.5,
    "participation_numerator": 1,
    "participation_denominator": 5,
}


class TestTheRowIsFilledFromTheRecord:
    async def test_all_four_numbers(self, db) -> None:
        await _market_data_response(db, "s1", _T, _SCALED)
        store, rid = await _row(db, "s1", _T + timedelta(minutes=2))
        assert await store.backfill_participation() == 1
        assert await _get(db, rid) == {
            "composite_unattenuated": pytest.approx(0.2),
            "participation_scale": pytest.approx(0.5),
            "participation_numerator": 1,
            "participation_denominator": 5,
        }

    async def test_it_is_idempotent(self, db) -> None:
        await _market_data_response(db, "s1", _T, _SCALED)
        store, _ = await _row(db, "s1", _T + timedelta(minutes=2))
        assert await store.backfill_participation() == 1
        assert await store.backfill_participation() == 0

    async def test_a_record_from_before_the_counts_fills_the_scale(self, db) -> None:
        """Responses from 2026-09-28 on carry the scale; the counts come later."""
        old = {
            "composite_signal": 0.1,
            "composite_unattenuated": 0.16,
            "participation_scale": 0.625,
        }
        await _market_data_response(db, "s1", _T, old)
        store, rid = await _row(db, "s1", _T + timedelta(minutes=2))
        assert await store.backfill_participation() == 1
        got = await _get(db, rid)
        assert got["participation_scale"] == pytest.approx(0.625)
        assert got["participation_numerator"] is None

    async def test_the_response_before_the_row_is_used(self, db) -> None:
        """A session can run the market-data tool twice."""
        await _market_data_response(db, "s1", _T, _SCALED)
        later = {**_SCALED, "participation_scale": 1.0, "participation_numerator": 3}
        await _market_data_response(db, "s1", _T + timedelta(minutes=10), later)
        store, rid = await _row(db, "s1", _T + timedelta(minutes=5))
        await store.backfill_participation()
        assert (await _get(db, rid))["participation_scale"] == pytest.approx(0.5)


class TestWhatIsLeftAlone:
    async def test_a_row_without_a_record(self, db) -> None:
        store, rid = await _row(db, "orphan", _T)
        assert await store.backfill_participation() == 0
        assert (await _get(db, rid))["participation_scale"] is None

    async def test_a_record_without_a_scale(self, db) -> None:
        await _market_data_response(db, "s1", _T, {"composite_signal": 0.1})
        store, _ = await _row(db, "s1", _T + timedelta(minutes=2))
        assert await store.backfill_participation() == 0

    async def test_a_value_already_written(self, db) -> None:
        await _market_data_response(db, "s1", _T, _SCALED)
        store, rid = await _row(db, "s1", _T + timedelta(minutes=2), participation_scale=0.9)
        assert await store.backfill_participation() == 0
        assert (await _get(db, rid))["participation_scale"] == pytest.approx(0.9)

    async def test_an_unreadable_record_does_not_stop_the_rest(self, db) -> None:
        await _market_data_response(db, "bad", _T, "{not json")
        await _market_data_response(db, "good", _T, _SCALED)
        store, _ = await _row(db, "bad", _T + timedelta(minutes=2))
        store, good = await _row(db, "good", _T + timedelta(minutes=2))
        assert await store.backfill_participation() == 1
        assert (await _get(db, good))["participation_numerator"] == 1


class TestWiring:
    def test_the_cycle_runs_it(self) -> None:
        assert "backfill_participation" in Path("src/evotrader/main.py").read_text()

    def test_the_agent_is_not_asked_for_them(self) -> None:
        """Numbers copied by an LLM are the fragile form; the record has them."""
        from evotrader.agents.tools import log_signal_attribution

        doc = log_signal_attribution.__doc__ or ""
        assert "participation_scale" not in doc
        assert "participation_numerator" not in doc

    async def test_the_tool_stores_them_if_given(self, db, monkeypatch) -> None:
        from evotrader.agents import tools

        monkeypatch.setattr(tools, "_db", db)
        out = await tools.log_signal_attribution(
            json.dumps(
                {"ticker": "XYZ", "participation_numerator": 2, "participation_denominator": 5}
            )
        )
        assert out["status"] == "ok"
        got = await _get(db, out["id"])
        assert (got["participation_numerator"], got["participation_denominator"]) == (2, 5)
