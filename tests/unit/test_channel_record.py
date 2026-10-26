"""The strategy agent reads the live channel record, not a count copied into its instructions.

Found by the evolution agent's code review. The strategy agent had no read of
the calibration record, so every base rate it reasoned from was typed into its
instructions by hand, and one such count vetoed every call of the channel the
live record scored as its best. Each cycle now renders the record (by author,
by channel and the final call by side, to the session close and to the next
session's close) from the same aggregation as get_signal_calibration, and the
prompt carries it beside the handoff. A row on too few independent days, or
with too small a t, is marked as not a base rate. Each attribution row also
records the algorithm version that made the call, so the record can be split
by version. Made-up ticker, numbers and times.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from evotrader.db.signal_attribution import SignalAttributionStore, algo_version_name
from evotrader.evolution.channel_record import (
    _cell,
    build_channel_record,
    render_channel_record,
)
from evotrader.evolution.tools import calibration_report


def _entry(n: int, days: int, hit: float, mean: float, t: float | None) -> dict:
    return {
        "n_calls": n,
        "n_independent_days": days,
        "hit_rate": hit,
        "mean_signed_return_pct": mean,
        "t_stat": t,
    }


class TestTheCell:
    def test_an_informative_row_is_unmarked(self) -> None:
        assert _cell(_entry(40, 12, 0.7, 1.2, 2.4)) == "40/12d 70% +1.20% t+2.4"

    def test_too_few_days_is_marked(self) -> None:
        assert _cell(_entry(9, 3, 1.0, 4.0, 5.0)).endswith("(days)")

    def test_a_small_t_is_marked(self) -> None:
        assert _cell(_entry(40, 12, 0.55, 0.3, 0.9)).endswith("(chance)")
        assert _cell(_entry(40, 12, 0.55, 0.3, None)).endswith("(chance)")

    def test_under_three_calls_there_is_nothing_to_read(self) -> None:
        assert _cell(_entry(2, 2, 1.0, 7.0, None)) is None
        assert _cell({"n_calls": 10, "hit_rate": None}) is None


def _report(n: int, channels: dict[str, dict], authors: dict[str, dict] | None = None) -> dict:
    return {
        "n_scored": n,
        "base_rate": {"share_of_cycles_price_rose": 0.4},
        "by_author": [{"author": a, **e} for a, e in (authors or {}).items()],
        "by_channel": [{"channel": c, **e} for c, e in channels.items()],
        "by_side": {
            "final": {
                "long": _entry(20, 8, 0.35, -1.0, -1.5),
                "short": _entry(6, 4, 0.5, 0.2, 0.1),
            }
        },
    }


class TestTheBlock:
    def test_nothing_scored_renders_nothing(self) -> None:
        assert render_channel_record({"1d": {"n_scored": 2}}, ticker="XYZ") == ""

    def test_the_layout(self) -> None:
        text = render_channel_record(
            {
                "close": _report(30, {"alpha": _entry(30, 10, 0.6, 0.5, 1.1)}),
                "1d": _report(
                    40,
                    {
                        "alpha": _entry(40, 12, 0.7, 1.2, 2.4),
                        "beta": _entry(12, 6, 0.4, -0.5, -0.8),
                    },
                    {"alpha": _entry(25, 9, 0.68, 1.0, 2.1)},
                ),
            },
            ticker="xyz",
            active_algo_version="v102_y",
            active_counts={"close": 3, "1d": 5},
        )
        lines = text.splitlines()
        assert lines[0] == "## Channel record (live: XYZ, last 30 days, scored calls only)"
        assert "### To the session close: 30 calls, price rose on 40%" in lines
        assert "### To the next session's close: 40 calls, price rose on 40%" in lines
        assert (
            "- channels: alpha 40/12d 70% +1.20% t+2.4; beta 12/6d 40% -0.50% t-0.8 (chance)"
            in lines
        )
        assert "- authors: alpha 25/9d 68% +1.00% t+2.1" in lines
        assert any(line.startswith("- final: long 20/8d 35%") for line in lines)
        assert lines[-1].startswith("Under the active algorithm (v102_y): 3 calls scored")

    def test_it_fits_by_leaving_out_the_smallest_rows(self) -> None:
        channels = {f"channel_{i:02d}": _entry(10 + i, 6, 0.5, 0.1, 0.5) for i in range(40)}
        text = render_channel_record({"1d": _report(400, channels)}, ticker="XYZ", max_chars=900)
        assert len(text) <= 900
        assert "channel_39" in text, "the largest stays"
        assert "channel_00" not in text, "the smallest goes first"
        assert "- final: long" in text, "the final call by side is never left out"
        assert "rows with the fewest calls left out to fit" in text


# ── From the store ───────────────────────────────────────────────────────

NOW = datetime.now(UTC).replace(microsecond=0)


async def _seed(db) -> SignalAttributionStore:
    """Twelve days, one cycle each: alpha always long, beta always short.

    Price rose the next session on nine of the twelve. The first six days ran
    on v101_x, the last six on v102_y.
    """
    store = SignalAttributionStore(db)
    rose = [True, True, False, True, True, True, False, True, True, True, False, True]
    for i, up in enumerate(rose):
        sid, ts = f"s{i:02d}", (NOW - timedelta(days=12 - i)).isoformat()
        subs = [
            {"name": "alpha", "value": 0.3, "weight": 0.5, "metadata": {}},
            {"name": "beta", "value": -0.2, "weight": 0.5, "metadata": {}},
        ]
        async with db.connection() as conn:
            await conn.execute(
                "INSERT INTO market_snapshots (timestamp, session_id, ticker, close_price, "
                "composite_signal, regime, sub_signals_json, algo_version) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    ts,
                    sid,
                    "XYZ",
                    100.0,
                    0.05,
                    "range_bound",
                    json.dumps(subs),
                    "composite_v101_x" if i < 6 else "composite_v102_y",
                ),
            )
            await conn.commit()
        rid = await store.record(
            "XYZ",
            session_id=sid,
            algo_direction=1.0,
            algo_author="alpha",
            final_direction=1.0,
            price_at_decision=100.0,
            timestamp=ts,
        )
        await store.score(rid, forward_return_1d=1.0 if up else -1.0, forward_return_5d=None)
    await store.backfill_channel_votes()
    return store


class TestFromTheStore:
    def test_the_version_name(self) -> None:
        assert algo_version_name("composite_v101_x") == "v101_x"
        assert algo_version_name("v101_x") == "v101_x"
        assert algo_version_name("") is None

    async def test_each_row_takes_its_snapshots_version(self, db) -> None:
        store = await _seed(db)
        assert await store.backfill_algo_version() == 12
        assert await store.backfill_algo_version() == 0, "idempotent"
        rows = await store.scored_rows()
        assert {r["algo_version"] for r in rows} == {"v101_x", "v102_y"}

    async def test_the_record_can_be_split_by_version(self, db) -> None:
        store = await _seed(db)
        await store.backfill_algo_version()
        pooled = await calibration_report(store, lookback_days=30, ticker="XYZ")
        own = await calibration_report(
            store, lookback_days=30, ticker="XYZ", algo_version="composite_v102_y"
        )
        assert (pooled["n_scored"], own["n_scored"]) == (12, 6)
        assert own["algo_version"] == "v102_y"
        author = next(a for a in pooled["by_author"] if a["author"] == "alpha")
        assert (author["n_calls"], author["n_independent_days"]) == (12, 12)
        assert author["t_stat"] is not None

    async def test_the_block_from_the_store(self, db) -> None:
        store = await _seed(db)
        await store.backfill_algo_version()
        text = await build_channel_record(store, "XYZ", active_algo_version="v102_y")
        assert "### To the next session's close: 12 calls, price rose on 75%" in text
        assert "alpha 12/12d 75% +0.50%" in text
        assert "beta 12/12d 25% -0.50%" in text
        assert "Under the active algorithm (v102_y): 0 calls scored to the close, 6 to" in text
        assert "To the session close" not in text, "no close-horizon rows were scored"

    async def test_nothing_scored_renders_nothing(self, db) -> None:
        assert await build_channel_record(SignalAttributionStore(db), "XYZ") == ""

    async def test_building_it_every_cycle_logs_nothing(self, db, caplog) -> None:
        """It skips the witnesses' agreement, whose warning would repeat every cycle."""
        store = await _seed(db)
        with caplog.at_level("WARNING"):
            assert await build_channel_record(store, "XYZ")
        assert "Agreement" not in caplog.text
        full = await calibration_report(store, lookback_days=30, ticker="XYZ")
        assert "agreement" in full, "the evolution agent's report keeps it"


# ── In the prompt ────────────────────────────────────────────────────────


class TestThePrompt:
    def test_the_record_is_in_the_strategy_agents_context(self, starter_data_dir) -> None:
        from evotrader.agents import factory
        from evotrader.config import AppConfig

        config = AppConfig(data_dir=starter_data_dir)
        try:
            factory.set_channel_record("## Channel record (live: XYZ)\n- channels: alpha")
            assert "## Channel record (live: XYZ)" in factory._strategy_dynamic_context(config)
            factory.set_channel_record("")
            assert "Channel record" not in factory._strategy_dynamic_context(config)
        finally:
            factory.set_channel_record("")

    def test_the_cycle_builds_it_before_the_agents_run(self) -> None:
        from pathlib import Path

        import evotrader.main as main

        src = Path(main.__file__).read_text()
        build = src.index("build_channel_record(")
        assert src.index("refresh_instructions(orchestrator, config)") < build
        assert build < src.index("cycle_start_time = time.monotonic()")
        assert "await store.backfill_algo_version()" in src


@pytest.fixture(autouse=True)
def _no_record_left_behind():
    yield
    from evotrader.agents import factory

    factory.set_channel_record("")
