"""Channels that cannot fire, cannot be read, or mislabel themselves.

See: data/evolution/reviews/20260922_221300_20260922_protection_audit_false_positive_and_silent_channel_census.md
(findings 2-5) and data/algorithms/v028_swing_failure_reachable_on_hourly_cadence/.

The census behind these: of the ten weighted channels, three are daily and
always-on, two carry zero weight, two have NO FEED and have never voted on any
instrument, and two more are gated off by timing. Participation reads "3 of 9"
because most of the nine structurally cannot speak.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

# The swing_failure_reversal parameters of v027 and of v028, which made the
# channel reachable on an hourly cadence.
_V027_SWING_FAILURE = {
    "lookback_bars": 20,
    "min_confirm_bars": 2,
    "max_confirm_bars": 8,
    "min_stretch_atr": 1.2,
    "stretch_scale": 0.8,
    "confirm_decay": 0.75,
    "reclaim_scale": 0.5,
    "base_strength": 0.8,
    "require_current_session_anchor": True,
}
_V028_SWING_FAILURE = {
    **_V027_SWING_FAILURE,
    "max_confirm_bars": 14,
    "min_stretch_atr": 0.3,
    "stretch_scale": 0.3,
    "confirm_decay": 0.87,
}


# ═════════════════════════════════════════════════════════════════════════
# Finding 3 + 4 — channels that read 5-minute bars must say so
# ═════════════════════════════════════════════════════════════════════════


class TestResolutionIsHonest:
    """`resolution` tells the strategy agent which channels CAN change between
    hourly cycles. Both of these read intraday inputs and reported 'daily'."""

    def test_swing_failure_reads_five_minute_bars(self) -> None:
        from evotrader.algorithms.strategies.swing_failure_reversal import (
            SwingFailureReversalStrategy,
        )

        assert SwingFailureReversalStrategy().resolution == "intraday"

    def test_range_break_gates_on_session_vwap_and_ibs(self) -> None:
        from evotrader.algorithms.strategies.range_break_continuation import (
            RangeBreakContinuationStrategy,
        )

        assert RangeBreakContinuationStrategy().resolution == "intraday"


class TestRangeBreakStopsAdvertisingADecayItDoesNotHave:
    """Finding 4. `decay_hours` was stored, validated and returned — and never
    read by compute_signal. The carry-forward described the channel as 'decayed
    over 3 h'. It was not."""

    def test_the_parameter_is_gone(self) -> None:
        import inspect

        from evotrader.algorithms.strategies.range_break_continuation import (
            RangeBreakContinuationStrategy,
        )

        s = RangeBreakContinuationStrategy()
        assert "decay_hours" not in s.get_parameters()
        assert "decay_hours" not in inspect.signature(RangeBreakContinuationStrategy).parameters

    def test_the_shipped_config_no_longer_carries_it(self, active_algorithm_config) -> None:
        """Otherwise the loader logs 'will use its current defaults' at WARNING
        on every restart, for a behaviour that never existed."""
        assert "decay_hours" not in active_algorithm_config["range_break_continuation"]

    def test_an_old_config_that_still_has_it_still_loads(
        self, starter_data_dir, active_algorithm_config
    ) -> None:
        """Archived versions keep the key; the loader must drop it, not crash."""
        from evotrader.algorithms.loader import StrategyLoader

        loader = StrategyLoader(starter_data_dir / "algorithms" / "strategy_manifest.yaml")
        # Shaped like an archived version (v026) that still carries the key.
        params = {
            **active_algorithm_config,
            "range_break_continuation": {
                **active_algorithm_config["range_break_continuation"],
                "decay_hours": 3.0,
            },
        }
        assert "decay_hours" in params["range_break_continuation"]
        loader.build_composite(params)  # must not raise


# ═════════════════════════════════════════════════════════════════════════
# Finding 5 — a reclaim that has not happened has no age
# ═════════════════════════════════════════════════════════════════════════


class TestReclaimAgeLabel:
    def _snapshot(self, closes: list[float], *, stamped: bool = True):
        from evotrader.models.market import (
            OHLCV,
            MarketRegime,
            MarketSnapshot,
            Quote,
            RegimeClassification,
            TechnicalIndicators,
        )

        now = datetime(2026, 9, 22, 17, 30, tzinfo=UTC)
        candles = [
            OHLCV(
                timestamp=now - timedelta(minutes=5 * (len(closes) - i)),
                open=c,
                high=c + 0.3,
                low=c - 0.3,
                close=c,
                volume=40_000.0,
            )
            for i, c in enumerate(closes)
        ]
        if not stamped:
            # The model requires a timestamp at construction; the live failure
            # mode is a candle that arrives without one, so blank it after.
            candles = [c.model_copy(update={"timestamp": None}) for c in candles]
        px = closes[-1]
        return MarketSnapshot(
            ticker="MSTR",
            timestamp=now,
            quote=Quote(
                ticker="MSTR", bid=px - 0.02, ask=px + 0.02, last=px, volume=5e6, timestamp=now
            ),
            indicators=TechnicalIndicators(
                vwap=140.0,
                vwap_anchor="current_session",
                atr_14=9.5,
                relative_volume=0.7,
                ema_9=px - 3,
                ema_21=px - 6,
                sma_20=125.0,
                sma_50=110.0,
            ),
            regime=RegimeClassification(
                regime=MarketRegime.TRENDING_BULL, confidence=0.7, reasoning="t"
            ),
            recent_candles=candles,
        )

    def test_no_reclaim_bar_reads_as_such(self) -> None:
        """Live on every not_reclaimed cycle of 09-21/22: price still below VWAP,
        so no reclaim bar exists — and the label said the timestamp path failed."""
        from evotrader.algorithms.strategies.vwap_reclaim_continuation import (
            VwapReclaimContinuationStrategy,
        )

        closes = [141.0, 140.5, 139.8, 139.2, 138.9]  # dipping, never reclaimed
        sig = VwapReclaimContinuationStrategy().compute_signal(self._snapshot(closes))
        assert sig.metadata["age_source"] == "no_reclaim_bar"
        assert sig.metadata["age_min"] is None
        assert sig.value == 0.0

    def test_missing_timestamps_still_use_the_bar_count_fallback(self) -> None:
        """The genuine fallback keeps its label, so the census can tell them apart."""
        from evotrader.algorithms.strategies.vwap_reclaim_continuation import (
            VwapReclaimContinuationStrategy,
        )

        closes = [141.0, 139.5, 139.0, 140.6, 141.1, 141.4]
        sig = VwapReclaimContinuationStrategy().compute_signal(
            self._snapshot(closes, stamped=False)
        )
        assert sig.metadata["age_source"] == "bars_assumed_5min"


# ═════════════════════════════════════════════════════════════════════════
# Finding 2 (phase 1) — start the put/call history now
# ═════════════════════════════════════════════════════════════════════════

# 09-22 15:30 ET near-money chain totals, from the review.
_CHAIN = [
    {"type": "put", "volume": v, "open_interest": oi} for v, oi in ((1200, 900), (1122, 700))
] + [
    {"type": "call", "volume": v, "open_interest": oi}
    for v, oi in ((689, 459), (716, 785), (51, 40))
]


class TestPutCallReadingFromTheChain:
    def test_ratios_from_the_live_chain(self) -> None:
        from evotrader.tools.options_pcr import pcr_from_chain

        r = pcr_from_chain(_CHAIN)
        assert r["put_volume"] == 2322 and r["call_volume"] == 1456
        assert r["pc_volume_ratio"] == pytest.approx(2322 / 1456, abs=1e-4)  # 1.59
        assert r["pc_oi_ratio"] == pytest.approx(1600 / 1284, abs=1e-4)
        assert r["pcr_scope"] == "near_money_chain"
        assert r["contracts_counted"] == 5

    def test_a_one_sided_chain_has_no_ratio(self) -> None:
        """A ratio with an empty side is a division artefact, not a reading."""
        from evotrader.tools.options_pcr import pcr_from_chain

        r = pcr_from_chain([c for c in _CHAIN if c["type"] == "call"])
        assert r["pc_volume_ratio"] is None and r["pc_oi_ratio"] is None

    def test_missing_fields_do_not_raise(self) -> None:
        from evotrader.tools.options_pcr import pcr_from_chain

        r = pcr_from_chain([{"type": "put"}, {"type": "call", "volume": None}, {}])
        assert r["pc_volume_ratio"] is None


class TestTheHistoryIsRecorded:
    def test_each_cycle_appends_one_reading(self, tmp_path: Path) -> None:
        from evotrader.tools.options_pcr import pcr_from_chain, record_pcr, sessions_recorded

        t0 = datetime(2026, 9, 22, 19, 30, tzinfo=UTC)
        record_pcr(tmp_path, "MSTR", pcr_from_chain(_CHAIN), now=t0)
        record_pcr(tmp_path, "MSTR", pcr_from_chain(_CHAIN), now=t0 + timedelta(hours=1))
        record_pcr(tmp_path, "MSTR", pcr_from_chain(_CHAIN), now=t0 + timedelta(days=1))
        lines = (tmp_path / "trading" / "options_pcr" / "MSTR.jsonl").read_text().splitlines()
        assert len(lines) == 3
        first = json.loads(lines[0])
        assert first["pc_volume_ratio"] == pytest.approx(1.5948, abs=1e-3)
        assert first["session_date"] == "2026-09-22", "dated in ET, the session's own calendar"
        assert sessions_recorded(tmp_path, "MSTR") == 2

    def test_it_is_observation_only(self) -> None:
        """Phase 1 must not change any signal: the channel still abstains."""
        src = Path("src/evotrader/agents/tools.py").read_text()
        assert "record_pcr" in src, "the option chain tool must record each reading"
        assert '"options_context": {' not in src, "wiring into the composite is phase 2"

    def test_a_write_failure_cannot_break_the_chain_tool(self, tmp_path: Path) -> None:
        from evotrader.tools.options_pcr import record_pcr

        blocker = tmp_path / "trading"
        blocker.write_text("not a directory")
        assert record_pcr(tmp_path, "MSTR", {"pc_volume_ratio": 1.0}) is False


# ═════════════════════════════════════════════════════════════════════════
# v028 — reachable on the hourly cadence, without losing the 11:30 cycle
# ═════════════════════════════════════════════════════════════════════════


class TestTheLookbackDoesNotCostACycle:
    """v028 as proposed raised lookback_bars 20 -> 30. Candles arrive 12 per hour
    from the 09:30 open, so the 11:30 cycle always has exactly 24 — and a
    30-bar lookback turns it into insufficient_data every day. It also broke
    the proposal's own fixture: that reconstruction used the lookback-20 flush.
    A longer window can additionally HIDE a fresh flush behind an older, deeper,
    already-stale low. 20 bars (100 min) already covers the 14-bar (70 min)
    confirm window, so the increase bought nothing. The version a new user
    starts with keeps the 20."""

    def test_the_shipped_version_keeps_lookback_at_20(self, active_algorithm_config) -> None:
        sf = active_algorithm_config["swing_failure_reversal"]
        assert sf["lookback_bars"] == 20
        assert sf["lookback_bars"] > sf["max_confirm_bars"], "a 14-bar-old flush must be findable"

    def test_the_1130_cycle_has_enough_bars(self, active_algorithm_config) -> None:
        sf = active_algorithm_config["swing_failure_reversal"]
        bars_at_1130 = 12 * 2  # two hours after the 09:30 open
        assert bars_at_1130 >= sf["lookback_bars"]


class TestV028LiveFixtures:
    """The anchor cycles, reproduced from recorded VWAP / ATR / flush. Under
    v027 each was rejected on TIMING (stale_flush); under v028 each is rejected
    on DEPTH (stretch_too_shallow) — an honest answer, and the composite is
    unchanged because the value is 0 either way. None of these was a real flush:
    the deepest dip across both sessions was 0.21 of a daily ATR."""

    # (label, vwap, atr, flush_low, bars_since_flush)
    _CYCLES = [
        ("09-21 11:30", 166.77535335536277, 10.161432, 164.69, 14),
        ("09-22 11:30", 169.33144705234432, 9.80295, 167.729, 10),
        ("09-22 13:30", 169.21970109411512, 9.829021, 167.4, 12),
        ("09-22 15:30", 169.22528751078002, 9.80295, 168.4, 11),
    ]

    def _snapshot(self, vwap: float, atr: float, flush_low: float, bars_since: int):
        from evotrader.models.market import (
            OHLCV,
            MarketRegime,
            MarketSnapshot,
            Quote,
            RegimeClassification,
            TechnicalIndicators,
        )

        now = datetime(2026, 9, 22, 19, 30, tzinfo=UTC)
        n = 24
        flush_idx = n - 1 - bars_since
        candles = []
        for i in range(n):
            low = flush_low if i == flush_idx else flush_low + 0.5 + 0.02 * i
            close = low + 0.4
            candles.append(
                OHLCV(
                    timestamp=now - timedelta(minutes=5 * (n - i)),
                    open=close,
                    high=close + 0.3,
                    low=low,
                    close=close,
                    volume=40_000.0,
                )
            )
        px = candles[-1].close
        return MarketSnapshot(
            ticker="MSTR",
            timestamp=now,
            quote=Quote(
                ticker="MSTR", bid=px - 0.02, ask=px + 0.02, last=px, volume=5e6, timestamp=now
            ),
            indicators=TechnicalIndicators(vwap=vwap, vwap_anchor="current_session", atr_14=atr),
            regime=RegimeClassification(
                regime=MarketRegime.TRENDING_BULL, confidence=0.7, reasoning="t"
            ),
            recent_candles=candles,
        )

    def _strategy(self, params: dict):
        from evotrader.algorithms.strategies.swing_failure_reversal import (
            SwingFailureReversalStrategy,
        )

        return SwingFailureReversalStrategy(**params)

    @pytest.mark.parametrize("label,vwap,atr,flush_low,bars", _CYCLES)
    def test_v027_rejected_it_on_timing(self, label, vwap, atr, flush_low, bars) -> None:
        sig = self._strategy(_V027_SWING_FAILURE).compute_signal(
            self._snapshot(vwap, atr, flush_low, bars)
        )
        assert sig.metadata["reason"] == "stale_flush", label

    @pytest.mark.parametrize("label,vwap,atr,flush_low,bars", _CYCLES)
    def test_v028_rejects_it_on_depth(self, label, vwap, atr, flush_low, bars) -> None:
        sig = self._strategy(_V028_SWING_FAILURE).compute_signal(
            self._snapshot(vwap, atr, flush_low, bars)
        )
        assert sig.metadata["reason"] == "stretch_too_shallow", label
        assert sig.metadata["stretch_atr"] == pytest.approx((vwap - flush_low) / atr, abs=1e-3)
        assert sig.value == 0.0, "composite unchanged on these cycles"

    def test_v028_fires_on_a_genuine_flush(self) -> None:
        """Reachable, not merely re-labelled: a 0.5-ATR flush that held and was
        reclaimed 40 minutes ago produces a positive, long-only vote."""
        vwap, atr = 169.0, 9.8
        flush_low = vwap - 0.5 * atr
        snap = self._snapshot(vwap, atr, flush_low, 8)
        sig = self._strategy(_V028_SWING_FAILURE).compute_signal(snap)
        assert sig.metadata["reason"] == "confirmed_reversal", sig.metadata
        assert sig.value > 0
        v027 = self._strategy(_V027_SWING_FAILURE).compute_signal(snap)
        assert v027.value == 0.0, "the same flush is below v027's 1.2-ATR gate"
