"""The calibration splits a vote-scaled channel's record by how deep its read was.

Found by the evolution agent's code review. A vote scale enlarges a channel's
emission before the vote, so a read once too shallow to matter can author the
composite, and the pooled channel row, which mixes shallow reads with deep
ones, could not say whether the shallow ones carry the record.
``get_signal_calibration`` now emits ``by_depth``: each vote-scaled channel's
own votes and the calls it authored, in buckets of its emission before the
scale (under 0.02, 0.02 to 0.05, 0.05 and over). The emission is kept with the
vote from now on
(``raw_value``); for older rows it is the stored value times the version's
scale, and a value clipped at the scale is counted as unknown when that does
not settle its bucket.

Momentum also reports ``ema9_side``, price against EMA 9 alone, beside
``fast_read``, the pair of price and the MACD histogram, which reads 0 while
the histogram lags a turn. Made-up ticker, numbers and times.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from evotrader.algorithms.strategies.momentum import MomentumStrategy, ema9_side
from evotrader.db.signal_attribution import SignalAttributionStore, channel_votes_from_sub_signals
from evotrader.evolution import tools as evolution_tools
from evotrader.evolution.tools import _depth_bucket, _depth_of, calibration_report
from evotrader.models.market import (
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)

NOW = datetime.now(UTC).replace(microsecond=0)


class TestTheDepth:
    def test_the_kept_emission_wins(self) -> None:
        assert _depth_of({"value": 0.5, "raw_value": -0.013}, 0.1) == (0.013, True)

    def test_else_the_value_times_the_scale(self) -> None:
        depth, exact = _depth_of({"value": -0.63}, 0.1)
        assert (round(depth, 4), exact) == (0.063, True)

    def test_a_clipped_value_gives_only_a_lower_bound(self) -> None:
        assert _depth_of({"value": 1.0}, 0.1) == (0.1, False)

    def test_no_scale_is_the_emission_itself(self) -> None:
        assert _depth_of({"value": 0.03}, None) == (0.03, True)

    @pytest.mark.parametrize(
        ("depth", "exact", "bucket"),
        [
            (0.013, True, "under_0.02"),
            (0.02, True, "0.02_to_0.05"),
            (0.0499, True, "0.02_to_0.05"),
            (0.05, True, "0.05_and_over"),
            (0.1, False, "0.05_and_over"),  # clipped at a scale above the top edge
            (0.03, False, None),  # clipped under it: unknown
            (None, True, None),
        ],
    )
    def test_the_buckets(self, depth, exact, bucket) -> None:
        assert _depth_bucket(depth, exact) == bucket

    def test_the_vote_keeps_the_emission_and_its_scale(self) -> None:
        votes = channel_votes_from_sub_signals(
            [
                {
                    "name": "mean_reversion",
                    "value": 0.13,
                    "weight": 0.2,
                    "metadata": {"vote_scale": 0.1, "raw_value": 0.013},
                },
                {"name": "momentum", "value": -0.3, "weight": 0.2, "metadata": {"ema9_side": 1}},
            ]
        )
        assert votes["mean_reversion"]["raw_value"] == 0.013
        assert votes["mean_reversion"]["vote_scale"] == 0.1
        assert votes["momentum"]["ema9_side"] == 1


async def _seed(db) -> SignalAttributionStore:
    """Twelve days, one call a day, all authored by mean_reversion (long).

    The first six on an older version whose votes kept only the scaled value
    (scale 0.1 in its config); the last six with the emission kept. Shallow
    reads (under 0.02) missed, deep ones (0.05 and over) hit.
    """
    store = SignalAttributionStore(db)
    plan = [
        # (scaled value, emission kept, next session up)
        (0.14, None, False),
        (0.11, None, False),
        (0.62, None, True),
        (0.93, None, True),
        (1.0, None, True),  # clipped: 0.1 or more, so 0.05 and over
        (0.27, None, False),
        (0.13, 0.013, False),
        (0.18, 0.018, True),
        (0.33, 0.033, True),
        (0.61, 0.061, True),
        (0.72, 0.072, True),
        (0.97, 0.097, False),
    ]
    for i, (value, raw, up) in enumerate(plan):
        sid, ts = f"s{i:02d}", (NOW - timedelta(days=12 - i)).isoformat()
        meta = {} if raw is None else {"vote_scale": 0.1, "raw_value": raw}
        subs = [
            {"name": "mean_reversion", "value": value, "weight": 0.2, "metadata": meta},
            {"name": "momentum", "value": -0.1, "weight": 0.2, "metadata": {}},
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
                    "composite_v101_old" if raw is None else "composite_v102_new",
                ),
            )
            await conn.commit()
        rid = await store.record(
            "XYZ",
            session_id=sid,
            algo_direction=1.0,
            algo_author="mean_reversion",
            final_direction=1.0,
            price_at_decision=100.0,
            timestamp=ts,
        )
        await store.score(rid, forward_return_1d=1.0 if up else -1.0, forward_return_5d=None)
    await store.backfill_channel_votes()
    await store.backfill_algo_version()
    return store


@pytest.fixture
def registry(monkeypatch):
    configs = {
        "v101_old": {"composite": {"vote_scales": {"mean_reversion": 0.1}}},
        "v102_new": {"composite": {"vote_scales": {"mean_reversion": 0.1}}},
    }
    monkeypatch.setattr(
        evolution_tools,
        "_algo_registry",
        SimpleNamespace(load_parameters=lambda version: configs.get(version, {})),
    )
    monkeypatch.setattr(evolution_tools, "_known_channel_names", lambda: ["mean_reversion"])


def _buckets(rows: list[dict]) -> dict[str, tuple]:
    return {b["bucket"]: (b["n_calls"], b["hit_rate"]) for b in rows}


class TestTheReport:
    async def test_each_bucket_is_scored(self, db, registry) -> None:
        report = await calibration_report(await _seed(db), lookback_days=30, ticker="XYZ")
        depth = report["by_depth"]["channels"]
        assert set(depth) == {"mean_reversion"}, "momentum has no scale"
        mr = depth["mean_reversion"]
        assert _buckets(mr["channel"]) == {
            "under_0.02": (4, 0.25),
            "0.02_to_0.05": (2, None),  # fewer than three calls: not scored
            "0.05_and_over": (6, pytest.approx(0.8333, abs=1e-4)),
        }
        assert _buckets(mr["author"]) == _buckets(mr["channel"]), "it authored every call"
        assert mr["depth_unknown"] == 0
        assert "before its vote scale" in report["by_depth"]["note"]

    async def test_without_a_registry_only_kept_emissions_count(self, db, monkeypatch) -> None:
        store = await _seed(db)
        monkeypatch.setattr(evolution_tools, "_algo_registry", None)
        report = await calibration_report(store, lookback_days=30, ticker="XYZ")
        mr = report["by_depth"]["channels"]["mean_reversion"]
        counts = {b["bucket"]: b["n_calls"] for b in mr["channel"]}
        # Older rows read as unscaled emissions: 0.11 and up are all "deep".
        assert counts == {"under_0.02": 2, "0.02_to_0.05": 1, "0.05_and_over": 9}

    async def test_nothing_scaled_means_no_split(self, db, monkeypatch) -> None:
        store = await _seed(db)
        monkeypatch.setattr(
            evolution_tools,
            "_algo_registry",
            SimpleNamespace(load_parameters=lambda version: {}),
        )
        async with db.connection() as conn:  # drop the kept emissions too
            await conn.execute("UPDATE signal_attribution SET channel_votes = NULL")
            await conn.execute(
                "UPDATE market_snapshots SET sub_signals_json = "
                "replace(sub_signals_json, '\"vote_scale\": 0.1, ', '')"
            )
            await conn.commit()
        await store.backfill_channel_votes()
        report = await calibration_report(store, lookback_days=30, ticker="XYZ")
        assert report["by_depth"] is None


def _snapshot(price: float, ema_9: float, hist: float) -> MarketSnapshot:
    return MarketSnapshot(
        ticker="XYZ",
        timestamp=NOW,
        quote=Quote(
            ticker="XYZ",
            bid=price - 0.01,
            ask=price + 0.01,
            last=price,
            volume=1e6,
            timestamp=NOW,
            previous_close=price,
        ),
        indicators=TechnicalIndicators(
            atr_14=4.0,
            macd_line=hist,
            macd_signal=0.0,
            macd_histogram=hist,
            ema_9=ema_9,
            ema_21=ema_9 + 1.0,
            sma_20=ema_9 + 1.5,
            sma_50=ema_9 + 3.0,
        ),
        regime=RegimeClassification(
            regime=MarketRegime.RANGE_BOUND, confidence=0.5, reasoning="fixture"
        ),
        daily_change_pct=1.0,
    )


class TestEma9Side:
    @pytest.mark.parametrize(
        ("p", "side"),
        [(0.5, 1), (-0.5, -1), (0.25, 1), (0.2499, 0), (0.0, 0), (None, 0)],
    )
    def test_its_reading(self, p, side) -> None:
        assert ema9_side(p, 0.25) == side

    def test_it_reads_where_the_pair_does_not(self) -> None:
        """Price half an ATR over EMA 9 while the MACD histogram still points down."""
        meta = MomentumStrategy().compute_signal(_snapshot(102.0, 100.0, -0.5)).metadata
        assert meta["price_vs_ema9_atr"] == 0.5
        assert meta["fast_read"] == 0
        assert meta["ema9_side"] == 1
