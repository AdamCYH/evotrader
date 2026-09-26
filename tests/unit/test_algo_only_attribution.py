"""Regression tests: a cycle the strategy agent never logged still records the algorithm's call.

2026-09-24 11:30 ET (session ca7cec51): a provider 503 ended the cycle after the
market data was gathered and stored. The strategy agent is the one that writes
the attribution row, so the cycle left none: the algorithm's call (composite
+0.2050 long, swing_failure_reversal's second-ever firing inside it) dropped
out of the calibration record. The holes are not random — they fall on bad-infra
cycles — so the control group is biased by them.

See: data/evolution/reviews/20260924_224217_mean_reversion_bull_stack_guard_unreachable_strategy_stage_503_skip.md
(finding 2, second half)
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

import pytest

from evotrader.db.signal_attribution import SignalAttributionStore, algo_call_from_snapshot

# The stored votes of session ca7cec51 (metadata trimmed to the fields that matter).
_CA7_SUBS = [
    {"name": "momentum", "value": 0.553351, "weight": 0.2045, "metadata": {}},
    {"name": "mean_reversion", "value": -0.193011, "weight": 0.129, "metadata": {}},
    {"name": "gap", "value": 0.0, "weight": 0.0, "metadata": {"applicable": True}},
    {
        "name": "intraday_vwap_zscore",
        "value": 0.0,
        "weight": 0.1935,
        "metadata": {"applicable": True, "reason": "within_band"},
    },
    {
        "name": "event_window_timing",
        "value": 0.0,
        "weight": 0.0215,
        "metadata": {"applicable": False, "reason": "event_context_not_available"},
    },
    {
        "name": "range_break_continuation",
        "value": 0.0,
        "weight": 0.1075,
        "metadata": {"in_scope": False},
    },
    {
        "name": "options_positioning",
        "value": 0.0,
        "weight": 0.086,
        "metadata": {"applicable": False, "reason": "no_options_data"},
    },
    {
        "name": "swing_failure_reversal",
        "value": 0.050605,
        "weight": 0.129,
        "metadata": {"applicable": True, "reason": "confirmed_reversal"},
    },
    {"name": "trend_persistence", "value": 0.0, "weight": 0.129, "metadata": {}},
    {
        "name": "vwap_reclaim_continuation",
        "value": 0.0,
        "weight": 0.0,
        "metadata": {"applicable": True, "reason": "not_reclaimed"},
    },
]
_CA7_SNAPSHOT_TS = "2026-09-24T15:30:35.231256+00:00"


async def _cycle(db, sid: str, ts: str, *, status: str = "SUCCESS", kind: str = "TRADING") -> None:
    async with db.connection() as conn:
        await conn.execute(
            "INSERT INTO cycle_runs (session_id, timestamp, cycle_type, status) VALUES (?,?,?,?)",
            (sid, ts, kind, status),
        )
        await conn.commit()


async def _snapshot(
    db, sid: str, ts: str, composite: float, close: float, subs: list[dict] | None = None
) -> None:
    async with db.connection() as conn:
        await conn.execute(
            "INSERT INTO market_snapshots (timestamp, session_id, ticker, close_price, "
            "composite_signal, regime, sub_signals_json) VALUES (?,?,?,?,?,?,?)",
            (ts, sid, "MSTR", close, composite, "trending_bull", json.dumps(subs or _CA7_SUBS)),
        )
        await conn.commit()


async def _agent_row(store: SignalAttributionStore, sid: str, ts: str) -> int:
    rid = await store.record(
        "MSTR",
        session_id=sid,
        algo_direction=1.0,
        algo_strength=0.25,
        algo_author="momentum",
        llm_direction=1.0,
        final_direction=1.0,
        final_conviction=0.3,
        price_at_decision=160.0,
        timestamp=ts,
    )
    assert rid is not None
    return rid


async def _rows(db) -> list[dict]:
    async with db.connection() as conn:
        cur = await conn.execute("SELECT * FROM signal_attribution ORDER BY timestamp")
        return [dict(r) for r in await cur.fetchall()]


@pytest.fixture
async def seeded(db):
    """09-24 as it happened: logged cycles either side of the failed 11:30 one."""
    store = SignalAttributionStore(db)
    await _cycle(db, "d6a4ee96", "2026-09-24T12:30:11+00:00")
    await _snapshot(db, "d6a4ee96", "2026-09-24T12:30:26+00:00", 0.2788, 158.895)
    await _agent_row(store, "d6a4ee96", "2026-09-24T12:31:28+00:00")

    await _cycle(db, "ca7cec51", "2026-09-24T15:30:10+00:00", status="FAILED")
    await _snapshot(db, "ca7cec51", _CA7_SNAPSHOT_TS, 0.204951422758269, 160.455)

    await _cycle(db, "3633e647", "2026-09-24T16:30:02+00:00")
    await _snapshot(db, "3633e647", "2026-09-24T16:30:30+00:00", 0.2549, 162.99)
    await _agent_row(store, "3633e647", "2026-09-24T16:31:12+00:00")
    return store


class TestTheFailedCycleIsRecorded:
    async def test_the_0924_1130_cycle_gets_the_algorithms_call(self, db, seeded) -> None:
        assert await seeded.backfill_algo_only_rows() == 1
        row = next(r for r in await _rows(db) if r["session_id"] == "ca7cec51")
        assert row["agent_absent"] == 1
        assert row["algo_direction"] == 1.0
        assert row["algo_strength"] == pytest.approx(0.2050, abs=1e-4)
        assert row["algo_author"] == "momentum"
        assert row["price_at_decision"] == 160.455
        assert row["timestamp"] == _CA7_SNAPSHOT_TS, "scored from the decision's own time"
        for agent_field in (
            "news_direction",
            "llm_direction",
            "llm_conviction",
            "final_direction",
            "final_conviction",
        ):
            assert row[agent_field] is None, agent_field
        assert row["traded"] == 0

    async def test_logged_cycles_are_left_alone_and_it_is_idempotent(self, db, seeded) -> None:
        await seeded.backfill_algo_only_rows()
        assert await seeded.backfill_algo_only_rows() == 0
        rows = await _rows(db)
        assert len(rows) == 3
        assert sum(r["agent_absent"] for r in rows) == 1

    async def test_channel_votes_are_filled_in_for_it(self, db, seeded) -> None:
        await seeded.backfill_algo_only_rows()
        await seeded.backfill_channel_votes()
        row = next(r for r in await _rows(db) if r["session_id"] == "ca7cec51")
        votes = json.loads(row["channel_votes"])
        assert votes["swing_failure_reversal"]["value"] == pytest.approx(0.050605)
        assert votes["swing_failure_reversal"]["reason"] == "confirmed_reversal"


class TestScope:
    async def test_cycles_before_the_record_began_are_not_backfilled(self, db, seeded) -> None:
        await _cycle(db, "old00001", "2026-08-01T14:30:00+00:00")
        await _snapshot(db, "old00001", "2026-08-01T14:30:20+00:00", 0.1, 400.0)
        await seeded.backfill_algo_only_rows()
        assert "old00001" not in {r["session_id"] for r in await _rows(db)}

    async def test_a_snapshot_outside_a_trading_cycle_is_not_a_cycle(self, db, seeded) -> None:
        """A session at 03:26 ET on 2026-09-15 stored a snapshot with no cycle."""
        await _snapshot(db, "6a4cc111", "2026-09-24T17:00:00+00:00", 0.0439, 161.0)
        await _cycle(db, "evo00001", "2026-09-24T17:10:00+00:00", kind="EVOLUTION")
        await _snapshot(db, "evo00001", "2026-09-24T17:10:30+00:00", 0.1, 161.0)
        await seeded.backfill_algo_only_rows()
        sids = {r["session_id"] for r in await _rows(db)}
        assert "6a4cc111" not in sids and "evo00001" not in sids

    async def test_a_cycle_that_died_before_gathering_has_nothing_to_record(
        self, db, seeded
    ) -> None:
        await _cycle(db, "nodata01", "2026-09-24T18:30:00+00:00", status="FAILED")
        await seeded.backfill_algo_only_rows()
        assert "nodata01" not in {r["session_id"] for r in await _rows(db)}

    async def test_an_empty_record_is_not_started_by_the_system(self, db) -> None:
        store = SignalAttributionStore(db)
        await _cycle(db, "ca7cec51", "2026-09-24T15:30:10+00:00", status="FAILED")
        await _snapshot(db, "ca7cec51", _CA7_SNAPSHOT_TS, 0.205, 160.455)
        assert await store.backfill_algo_only_rows() == 0

    async def test_traded_counts_entries_and_exits_not_protective_placements(
        self, db, seeded
    ) -> None:
        async with db.connection() as conn:
            for sid, action in (("t_open01", "OPEN"), ("t_stop01", "STOP_LOSS")):
                await conn.execute(
                    "INSERT INTO trades (timestamp, ticker, direction, action, quantity, price, "
                    "order_type, algo_version, regime, confidence, reasoning, market_snapshot, "
                    "session_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "2026-09-24T19:00:00+00:00",
                        "MSTR",
                        "LONG",
                        action,
                        1,
                        160.0,
                        "limit",
                        "v029",
                        "trending_bull",
                        0.5,
                        "x",
                        "{}",
                        sid,
                    ),
                )
            await conn.commit()
        for sid in ("t_open01", "t_stop01"):
            await _cycle(db, sid, "2026-09-24T19:00:00+00:00")
            await _snapshot(db, sid, "2026-09-24T19:00:20+00:00", 0.2, 160.0)
        await seeded.backfill_algo_only_rows()
        traded = {r["session_id"]: r["traded"] for r in await _rows(db)}
        assert traded["t_open01"] == 1 and traded["t_stop01"] == 0


class TestTheCallItself:
    def test_direction_is_the_sign_above_the_composites_silence_line(self) -> None:
        assert algo_call_from_snapshot(0.0009, _CA7_SUBS)[0] == 0.0
        assert algo_call_from_snapshot(-0.0089, _CA7_SUBS)[0] == -1.0
        assert algo_call_from_snapshot(0.2050, _CA7_SUBS)[0] == 1.0
        assert algo_call_from_snapshot(None, _CA7_SUBS) == (0.0, None)

    def test_author_is_chosen_as_the_composite_chooses_it(self) -> None:
        """Largest |value| x weight among applicable additive channels that voted."""
        assert algo_call_from_snapshot(0.2, _CA7_SUBS)[1] == "momentum"
        subs = [
            {
                "name": "loud_but_inapplicable",
                "value": 0.9,
                "weight": 0.5,
                "metadata": {"applicable": False},
            },
            {"name": "multiplier", "value": 0.9, "weight": 0.5, "metadata": {"role": "multiplier"}},
            {"name": "whisper", "value": 0.0005, "weight": 0.9, "metadata": {}},
            {"name": "dissent", "value": -0.3, "weight": 0.2, "metadata": {}},
            {"name": "lead", "value": 0.2, "weight": 0.2, "metadata": {}},
        ]
        assert algo_call_from_snapshot(-0.01, json.dumps(subs))[1] == "dissent"

    def test_nobody_voted_means_no_author(self) -> None:
        assert algo_call_from_snapshot(0.0, [{"name": "a", "value": 0.0, "weight": 1.0}])[1] is None


class TestCalibrationCountsItForTheAlgorithmOnly:
    async def test_agent_witnesses_do_not_see_a_flat_call_nobody_made(
        self, db, monkeypatch
    ) -> None:
        import evotrader.evolution.tools as evo

        store = SignalAttributionStore(db)
        monkeypatch.setattr(evo, "_attribution_store", store)
        base = datetime(2026, 7, 1, 14, 30, tzinfo=UTC)
        for i in range(12):
            absent = i % 3 == 0
            ts = (base + timedelta(days=i)).isoformat()
            if absent:
                rid = await store.record(
                    "MSTR",
                    algo_direction=1.0,
                    algo_strength=0.2,
                    algo_author="momentum",
                    price_at_decision=100.0,
                    timestamp=ts,
                    agent_absent=True,
                )
            else:
                rid = await store.record(
                    "MSTR",
                    algo_direction=1.0,
                    algo_strength=0.2,
                    algo_author="momentum",
                    news_direction=1.0,
                    llm_direction=1.0,
                    final_direction=1.0,
                    final_conviction=0.5,
                    price_at_decision=100.0,
                    timestamp=ts,
                )
            await store.score(rid, forward_return_1d=1.0 if i % 2 else -1.0, forward_return_5d=None)

        out = await evo.get_signal_calibration(lookback_days=36500, ticker="MSTR")
        w = {x["source"]: x for x in out["witnesses"]}
        assert out["n_scored"] == 12
        assert out["rows_without_agent"] == 4
        assert w["algo"]["n_calls"] == 12, "the algorithm's call counts on every row"
        assert w["llm"]["n_calls"] == 8 and w["final"]["n_calls"] == 8
        assert out["agreement"]["n_calls"] == 8, "no consensus is claimed for an absent agent"
        assert sum(b["n"] for b in out["conviction_calibration"]) == 8
        assert "NaN" not in json.dumps(out)

    def test_nan_really_is_what_the_scorer_drops(self) -> None:
        from evotrader.backtest.attribution import score_source

        now = datetime(2026, 7, 1, tzinfo=UTC)
        sc = score_source(
            "x",
            [1.0, math.nan, -1.0, 1.0],
            [1.0, 5.0, -1.0, 1.0],
            [now + timedelta(days=i) for i in range(4)],
        )
        assert sc.n_calls == 3
