"""Unit tests for evotrader.backtest pipeline."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from evotrader.algorithms.base import TradingAlgorithm
from evotrader.algorithms.loader import StrategyLoader
from evotrader.backtest.data import HistoricalDataFetcher
from evotrader.backtest.engine import BacktestEngine, BacktestTrade, PositionSide
from evotrader.backtest.metrics import calculate_tear_sheet, infer_bars_per_day
from evotrader.backtest.snapshot_builder import SnapshotBuilder
from evotrader.models.market import (
    OHLCV,
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)
from evotrader.models.signals import AlgoSignal

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def sample_daily_df() -> pd.DataFrame:
    """Create 70 days of mock daily bars."""
    base_date = datetime(2025, 1, 1, 0, 0, tzinfo=UTC)
    dates = [base_date + timedelta(days=i) for i in range(70)]
    prices = [400.0 + i * 0.5 + (np.sin(i) * 2.0) for i in range(70)]
    return pd.DataFrame(
        {
            "open": prices,
            "high": [p + 2.0 for p in prices],
            "low": [p - 2.0 for p in prices],
            "close": prices,
            "volume": [50000000.0] * 70,
        },
        index=pd.DatetimeIndex(dates),
    )


@pytest.fixture
def sample_intraday_df() -> pd.DataFrame:
    """Create 2 days of mock intraday (hourly) bars."""
    dates = []
    prices = []
    base_price = 430.0
    for day in (50, 51):
        for hour in range(9, 16):
            dates.append(datetime(2025, 1, 1, hour, 30, tzinfo=UTC) + timedelta(days=day))
            base_price += 0.4
            prices.append(base_price)

    return pd.DataFrame(
        {
            "open": prices,
            "high": [p + 0.5 for p in prices],
            "low": [p - 0.5 for p in prices],
            "close": prices,
            "volume": [1000000.0] * len(prices),
        },
        index=pd.DatetimeIndex(dates),
    )


# ── Tests ────────────────────────────────────────────────────────────


class TestHistoricalDataFetcher:
    def test_normalize_columns(self) -> None:
        df = pd.DataFrame(
            {
                "Open": [100.0],
                "High": [105.0],
                "Low": [95.0],
                "Close": [102.0],
                "Volume": [1000.0],
            }
        )
        normalized = HistoricalDataFetcher._normalize_columns(df)
        assert list(normalized.columns) == ["open", "high", "low", "close", "volume"]

    def test_cache_round_trip(self, tmp_path: Path) -> None:
        fetcher = HistoricalDataFetcher(cache_dir=tmp_path)
        sample = pd.DataFrame(
            {"open": [100.0], "high": [105.0], "low": [95.0], "close": [102.0], "volume": [1000.0]},
            index=pd.DatetimeIndex([datetime(2025, 1, 1, tzinfo=UTC)]),
        )
        cache_file = tmp_path / "TEST_1h_1y.csv"
        sample.to_csv(cache_file)

        loaded = fetcher.fetch_intraday(ticker="TEST", period="1y", interval="1h")
        assert len(loaded) == 1
        assert loaded.iloc[0]["close"] == 102.0


class TestSnapshotBuilder:
    def test_iter_snapshots_generates_valid_snapshots(
        self, sample_intraday_df: pd.DataFrame, sample_daily_df: pd.DataFrame
    ) -> None:
        builder = SnapshotBuilder(
            df_intraday=sample_intraday_df,
            df_daily=sample_daily_df,
            ticker="QQQ",
        )
        snapshots = list(builder.iter_snapshots())
        assert len(snapshots) > 0

        first_snap = snapshots[0]
        assert isinstance(first_snap, MarketSnapshot)
        assert first_snap.ticker == "QQQ"
        assert first_snap.indicators.vwap is not None
        assert first_snap.indicators.vwap_anchor == "current_session"
        assert first_snap.indicators.rsi_14 is not None
        assert first_snap.indicators.atr_14 is not None
        assert first_snap.regime.regime in (
            MarketRegime.TRENDING_BULL,
            MarketRegime.TRENDING_BEAR,
            MarketRegime.RANGE_BOUND,
            MarketRegime.HIGH_VOLATILITY,
        )

    def test_recent_candles_never_cross_sessions(
        self, sample_intraday_df: pd.DataFrame, sample_daily_df: pd.DataFrame
    ) -> None:
        """Session VWAP is the reference, so the window must be session-scoped.

        The builder used to slice the full intraday frame, so 99.8% of
        snapshots reached into prior sessions while ``indicators.vwap``
        stayed anchored to the current one.
        """
        snapshots = list(
            SnapshotBuilder(sample_intraday_df, sample_daily_df, "QQQ").iter_snapshots()
        )
        assert snapshots
        for snap in snapshots:
            assert snap.recent_candles, "current bar must always be present"
            session_date = snap.timestamp.date()
            assert all(c.timestamp.date() == session_date for c in snap.recent_candles)
            # Last candle is the current bar, and never a future one.
            assert snap.recent_candles[-1].timestamp == snap.timestamp
            assert max(c.timestamp for c in snap.recent_candles) <= snap.timestamp

    def test_recent_candles_accumulate_within_session(
        self, sample_intraday_df: pd.DataFrame, sample_daily_df: pd.DataFrame
    ) -> None:
        snapshots = list(
            SnapshotBuilder(sample_intraday_df, sample_daily_df, "QQQ").iter_snapshots()
        )
        by_session: dict[object, list[MarketSnapshot]] = {}
        for snap in snapshots:
            by_session.setdefault(snap.timestamp.date(), []).append(snap)
        for day_snaps in by_session.values():
            counts = [len(s.recent_candles) for s in day_snaps]
            assert counts == list(range(1, len(day_snaps) + 1))

    def test_daily_candles_populated_and_strictly_prior(
        self, sample_intraday_df: pd.DataFrame, sample_daily_df: pd.DataFrame
    ) -> None:
        """TrendPersistenceStrategy abstained on 100% of bars without these."""
        snapshots = list(
            SnapshotBuilder(sample_intraday_df, sample_daily_df, "QQQ").iter_snapshots()
        )
        assert snapshots
        for snap in snapshots:
            assert snap.daily_candles, "daily_candles must be populated"
            # Strictly completed sessions — never today's bar.
            assert all(c.timestamp.date() < snap.timestamp.date() for c in snap.daily_candles)

    def test_daily_candles_respect_lookback(
        self, sample_intraday_df: pd.DataFrame, sample_daily_df: pd.DataFrame
    ) -> None:
        builder = SnapshotBuilder(
            sample_intraday_df, sample_daily_df, "QQQ", daily_candles_lookback=5
        )
        snapshots = list(builder.iter_snapshots())
        assert snapshots
        assert all(len(s.daily_candles) <= 5 for s in snapshots)

    def test_recent_candles_respect_lookback(
        self, sample_intraday_df: pd.DataFrame, sample_daily_df: pd.DataFrame
    ) -> None:
        builder = SnapshotBuilder(
            sample_intraday_df, sample_daily_df, "QQQ", recent_candles_lookback=3
        )
        snapshots = list(builder.iter_snapshots())
        assert snapshots
        assert all(len(s.recent_candles) <= 3 for s in snapshots)


class TestBacktestEngine:
    def _make_dummy_snapshot(
        self,
        price: float,
        atr: float = 3.0,
        timestamp: datetime | None = None,
        bar: tuple[float, float, float, float] | None = None,
    ) -> MarketSnapshot:
        """Snapshot at ``price``. ``bar`` supplies explicit OHLC for the current bar."""
        ts = timestamp or datetime.now(UTC)
        candles = []
        if bar is not None:
            o, h, low, c = bar
            candles = [OHLCV(timestamp=ts, open=o, high=h, low=low, close=c, volume=1000.0)]
        return MarketSnapshot(
            ticker="QQQ",
            timestamp=ts,
            quote=Quote(
                ticker="QQQ",
                bid=price - 0.01,
                ask=price + 0.01,
                last=price,
                volume=1000000.0,
                timestamp=ts,
            ),
            indicators=TechnicalIndicators(
                atr_14=atr,
                vwap=price,
                vwap_anchor="current_session",
            ),
            regime=RegimeClassification(
                regime=MarketRegime.RANGE_BOUND,
                confidence=0.8,
                reasoning="test",
            ),
            recent_candles=candles,
        )

    def test_entry_long_and_take_profit_legacy_fills(self) -> None:
        engine = BacktestEngine(
            initial_cash=10000.0,
            entry_threshold_range_bound=0.05,
            take_profit_atr_mult=2.0,
            slippage_bps=0.0,
            max_position_pct=0.50,
            next_bar_open_fill=False,
            intrabar_stops=False,
        )
        snap1 = self._make_dummy_snapshot(100.0, atr=2.0)
        sig_buy = AlgoSignal(name="composite", value=0.08, weight=1.0)

        # Step 1: Open LONG
        engine.step(snap1, sig_buy)
        assert engine.position is not None
        assert engine.position.side == PositionSide.LONG
        assert engine.position.shares == 50  # 50% of 10k = 5000 / 100 = 50 shares

        # Step 2: Price hits Take Profit (100 + 2*2 = 104)
        snap2 = self._make_dummy_snapshot(105.0, atr=2.0)
        engine.step(snap2, AlgoSignal(name="composite", value=0.08, weight=1.0))

        assert engine.position is None
        assert len(engine.closed_trades) == 1
        trade = engine.closed_trades[0]
        assert trade.exit_reason == "TAKE_PROFIT"
        assert trade.realized_pnl == pytest.approx(50 * (105.0 - 100.0))
        assert engine.cash > 10000.0

    def test_stop_loss_short_legacy_fills(self) -> None:
        engine = BacktestEngine(
            initial_cash=10000.0,
            entry_threshold_range_bound=0.05,
            stop_loss_atr_mult=1.5,
            slippage_bps=0.0,
            max_position_pct=0.50,
            next_bar_open_fill=False,
            intrabar_stops=False,
        )
        snap1 = self._make_dummy_snapshot(100.0, atr=2.0)
        sig_sell = AlgoSignal(name="composite", value=-0.08, weight=1.0)

        # Step 1: Open SHORT
        engine.step(snap1, sig_sell)
        assert engine.position is not None
        assert engine.position.side == PositionSide.SHORT

        # Step 2: Price rises against short to 104 (Stop Loss at 100 + 1.5*2 = 103)
        snap2 = self._make_dummy_snapshot(104.0, atr=2.0)
        engine.step(snap2, AlgoSignal(name="composite", value=-0.08, weight=1.0))

        assert engine.position is None
        assert len(engine.closed_trades) == 1
        assert engine.closed_trades[0].exit_reason == "STOP_LOSS"
        assert engine.closed_trades[0].realized_pnl < 0.0

    def test_default_sizing_matches_live_config(self) -> None:
        """The engine must not silently run at 5x the live position size."""
        assert BacktestEngine().max_position_pct == pytest.approx(0.10)

    def test_signal_entry_fills_at_next_bar_open(self) -> None:
        """A signal read off a bar's close cannot fill at that same close."""
        engine = BacktestEngine(
            initial_cash=10000.0,
            entry_threshold_range_bound=0.05,
            slippage_bps=0.0,
            max_position_pct=1.0,
        )
        t0 = datetime(2025, 6, 2, 14, 30, tzinfo=UTC)
        sig = AlgoSignal(name="composite", value=0.08, weight=1.0)

        # Bar 1 generates the signal but must NOT fill.
        engine.step(
            self._make_dummy_snapshot(100.0, bar=(99.0, 101.0, 98.0, 100.0), timestamp=t0),
            sig,
        )
        assert engine.position is None

        # Bar 2 fills at its OPEN (103.0), not its close (110.0).
        snap2 = self._make_dummy_snapshot(
            110.0, bar=(103.0, 111.0, 102.0, 110.0), timestamp=t0 + timedelta(hours=1)
        )
        engine.step(snap2, sig)
        assert engine.position is not None
        assert engine.position.entry_price == pytest.approx(103.0)

    def test_intrabar_stop_triggers_on_low_not_close(self) -> None:
        """A stop is a resting order: the bar's low touching it is a fill."""
        engine = BacktestEngine(
            initial_cash=10000.0,
            entry_threshold_range_bound=0.05,
            stop_loss_atr_mult=1.0,
            slippage_bps=0.0,
            max_position_pct=1.0,
            next_bar_open_fill=False,
        )
        t0 = datetime(2025, 6, 2, 14, 30, tzinfo=UTC)
        sig = AlgoSignal(name="composite", value=0.08, weight=1.0)
        flat_bar = (100.0, 100.0, 100.0, 100.0)
        engine.step(
            self._make_dummy_snapshot(100.0, atr=2.0, bar=flat_bar, timestamp=t0),
            sig,
        )
        assert engine.position is not None  # stop sits at 98.0

        # Close (99.5) never crosses 98.0, but the low (97.0) does.
        engine.step(
            self._make_dummy_snapshot(
                99.5, atr=2.0, bar=(99.8, 100.0, 97.0, 99.5), timestamp=t0 + timedelta(hours=1)
            ),
            sig,
        )
        assert engine.position is None
        assert engine.closed_trades[0].exit_reason == "STOP_LOSS"
        assert engine.closed_trades[0].exit_price == pytest.approx(98.0)

    def test_gap_through_stop_fills_at_open(self) -> None:
        """A gap below the stop fills at the open, not at the stop price."""
        engine = BacktestEngine(
            initial_cash=10000.0,
            entry_threshold_range_bound=0.05,
            stop_loss_atr_mult=1.0,
            slippage_bps=0.0,
            max_position_pct=1.0,
            next_bar_open_fill=False,
        )
        t0 = datetime(2025, 6, 2, 14, 30, tzinfo=UTC)
        sig = AlgoSignal(name="composite", value=0.08, weight=1.0)
        flat_bar = (100.0, 100.0, 100.0, 100.0)
        engine.step(
            self._make_dummy_snapshot(100.0, atr=2.0, bar=flat_bar, timestamp=t0),
            sig,
        )
        # Opens at 94.0, far below the 98.0 stop.
        engine.step(
            self._make_dummy_snapshot(
                93.0, atr=2.0, bar=(94.0, 94.5, 92.0, 93.0), timestamp=t0 + timedelta(hours=1)
            ),
            sig,
        )
        assert engine.closed_trades[0].exit_price == pytest.approx(94.0)

    def test_allow_shorts_false_blocks_short_entries(self) -> None:
        engine = BacktestEngine(
            initial_cash=10000.0,
            entry_threshold_range_bound=0.05,
            allow_shorts=False,
            next_bar_open_fill=False,
        )
        snap = self._make_dummy_snapshot(100.0)
        engine.step(snap, AlgoSignal(name="composite", value=-0.50, weight=1.0))
        assert engine.position is None

    def test_execution_assumptions_are_reported(self) -> None:
        ea = BacktestEngine().execution_assumptions
        assert ea["next_bar_open_fill"] is True
        assert ea["intrabar_stops"] is True
        assert ea["max_position_pct"] == pytest.approx(0.10)


class TestCalculateTearSheet:
    def test_metrics_calculation(self) -> None:
        initial = 10000.0
        equity_curve = [
            {"equity": 10000.0},
            {"equity": 10200.0},
            {"equity": 10500.0},
            {"equity": 10300.0},
            {"equity": 11000.0},
        ]
        closed_trades = [
            BacktestTrade(
                trade_id=1,
                ticker="QQQ",
                side=PositionSide.LONG,
                entry_time=datetime.now(UTC),
                entry_price=100.0,
                shares=10.0,
                exit_time=datetime.now(UTC),
                exit_price=105.0,
                exit_reason="TAKE_PROFIT",
                realized_pnl=50.0,
                return_pct=5.0,
                bars_held=3,
                authoring_signal="momentum",
                entry_regime="trending_bull",
            ),
            BacktestTrade(
                trade_id=2,
                ticker="QQQ",
                side=PositionSide.LONG,
                entry_time=datetime.now(UTC),
                entry_price=100.0,
                shares=10.0,
                exit_time=datetime.now(UTC),
                exit_price=98.0,
                exit_reason="STOP_LOSS",
                realized_pnl=-20.0,
                return_pct=-2.0,
                bars_held=2,
                authoring_signal="intraday_vwap_zscore",
                entry_regime="range_bound",
            ),
        ]
        metrics = calculate_tear_sheet(initial, closed_trades, equity_curve)

        assert metrics["final_equity"] == 11000.0
        assert metrics["total_pnl"] == 1000.0
        assert metrics["total_return_pct"] == 10.0
        assert metrics["trade_count"] == 2
        assert metrics["win_rate"] == 0.5
        assert metrics["profit_factor"] == pytest.approx(50.0 / 20.0)
        assert metrics["sharpe_ratio"] > 0.0
        assert "range_bound" in metrics["regime_breakdown"]
        assert "trending_bull" in metrics["regime_breakdown"]


class TestStrategyLoaderBackCompat:
    """Archived configs must survive strategy signature changes.

    ``MeanReversionStrategy`` dropped ``vwap_weight``, which made every
    config from v005..v016 raise TypeError and vanish from ``--compare``.
    """

    class _Strat(TradingAlgorithm):
        def __init__(self, alpha: float = 1.0, beta: float = 2.0) -> None:
            self.alpha = alpha
            self.beta = beta

        @property
        def name(self) -> str:
            return "stub"

        @property
        def version(self) -> str:
            return "v1"

        @property
        def description(self) -> str:
            return "stub"

        def compute_signal(self, snapshot):
            raise NotImplementedError

    class _KwargsStrat(_Strat):
        def __init__(self, alpha: float = 1.0, **kwargs: object) -> None:
            super().__init__(alpha=alpha)
            self.extra = kwargs

    def test_stale_keys_are_dropped(self, caplog: pytest.LogCaptureFixture) -> None:
        params = {"alpha": 9.0, "vwap_weight": 0.3, "gone": 1}
        kept = StrategyLoader._filter_params("mean_reversion", self._Strat, params)
        assert kept == {"alpha": 9.0}
        self._Strat(**kept)  # must construct

    def test_accepted_keys_are_preserved(self) -> None:
        params = {"alpha": 9.0, "beta": 8.0}
        assert StrategyLoader._filter_params("s", self._Strat, params) == params

    def test_var_keyword_strategies_keep_everything(self) -> None:
        params = {"alpha": 1.0, "anything": 2.0}
        assert StrategyLoader._filter_params("s", self._KwargsStrat, params) == params

    def test_all_archived_versions_build(self, starter_data_dir: Path) -> None:
        """Every version on disk must remain benchmarkable."""
        algorithms_dir = starter_data_dir / "algorithms"
        manifest = algorithms_dir / "strategy_manifest.yaml"
        loader = StrategyLoader(manifest)
        versions = sorted(
            p for p in algorithms_dir.iterdir() if p.is_dir() and (p / "config.yaml").is_file()
        )
        assert versions, "expected archived version configs"
        import yaml

        failed = []
        for vdir in versions:
            params = yaml.safe_load((vdir / "config.yaml").read_text()) or {}
            try:
                loader.build_composite(params, active_version=vdir.name)
            except Exception as e:
                failed.append(f"{vdir.name}: {type(e).__name__}: {e}")
        assert not failed, "versions that no longer build:\n" + "\n".join(failed)


class TestAnnualizationInference:
    def test_infers_bars_per_day_from_timestamps(self) -> None:
        curve = [
            {"timestamp": f"2025-01-0{d}T1{h}:30:00+00:00", "equity": 1.0}
            for d in (1, 2, 3)
            for h in range(7)
        ]
        assert infer_bars_per_day(curve) == pytest.approx(7.0)

    def test_five_minute_data_infers_higher_frequency(self) -> None:
        curve = [
            {"timestamp": f"2025-01-0{d}T14:{m:02d}:00+00:00", "equity": 1.0}
            for d in (1, 2)
            for m in range(0, 60, 5)
        ]
        assert infer_bars_per_day(curve) == pytest.approx(12.0)

    def test_falls_back_without_timestamps(self) -> None:
        assert infer_bars_per_day([{"equity": 1.0}]) == pytest.approx(7.0)

    def test_tear_sheet_records_assumptions(self) -> None:
        metrics = calculate_tear_sheet(
            initial_cash=1000.0,
            closed_trades=[],
            equity_curve=[{"timestamp": "2025-01-01T14:30:00+00:00", "equity": 1000.0}],
            execution_assumptions={"next_bar_open_fill": True},
        )
        assert metrics["execution_assumptions"] == {"next_bar_open_fill": True}
        assert "bars_per_day" in metrics


class TestRiskParams:
    """Risk settings must be explicit and layered, not hardcoded in the engine."""

    def test_defaults_match_live_config(self) -> None:
        from evotrader.backtest.runner import RiskParams

        rp = RiskParams()
        assert rp.max_position_pct == pytest.approx(0.10)
        assert rp.stop_loss_atr_mult == pytest.approx(2.0)
        assert rp.next_bar_open_fill is True
        assert rp.intrabar_stops is True

    def test_config_block_overrides_defaults(self) -> None:
        from evotrader.backtest.runner import RiskParams

        rp = RiskParams.from_sources({"max_position_pct": 0.25, "slippage_bps": 5.0}, {})
        assert rp.max_position_pct == pytest.approx(0.25)
        assert rp.slippage_bps == pytest.approx(5.0)

    def test_cli_overrides_win_over_config(self) -> None:
        from evotrader.backtest.runner import RiskParams

        rp = RiskParams.from_sources({"max_position_pct": 0.25}, {"max_position_pct": 0.40})
        assert rp.max_position_pct == pytest.approx(0.40)

    def test_none_overrides_are_ignored(self) -> None:
        from evotrader.backtest.runner import RiskParams

        rp = RiskParams.from_sources({"slippage_bps": 5.0}, {"slippage_bps": None})
        assert rp.slippage_bps == pytest.approx(5.0)

    def test_unknown_keys_are_ignored(self) -> None:
        from evotrader.backtest.runner import RiskParams

        rp = RiskParams.from_sources({"not_a_field": 1}, {})
        assert rp.max_position_pct == pytest.approx(0.10)

    def test_to_engine_propagates_settings(self) -> None:
        from evotrader.backtest.runner import RiskParams

        engine = RiskParams(max_position_pct=0.3, allow_shorts=False).to_engine()
        assert engine.max_position_pct == pytest.approx(0.3)
        assert engine.allow_shorts is False


class TestRunnerIntegration:
    """End-to-end checks on the assembled harness."""

    @pytest.fixture
    def _algorithms_dir(self, starter_data_dir: Path) -> Path:
        """The data folder's algorithms, where the runner looks up a version by name."""
        return starter_data_dir / "algorithms"

    @pytest.fixture
    def _snapshots(
        self, sample_intraday_df: pd.DataFrame, sample_daily_df: pd.DataFrame
    ) -> list[MarketSnapshot]:
        from evotrader.backtest.runner import build_snapshots

        return build_snapshots(sample_intraday_df, sample_daily_df, ticker="TEST")

    def test_run_reports_participation_and_reasons(
        self, _algorithms_dir: Path, _snapshots: list[MarketSnapshot]
    ) -> None:
        from evotrader.backtest.runner import run_single_backtest

        version = next(
            p.name
            for p in sorted(_algorithms_dir.iterdir())
            if p.is_dir() and (p / "config.yaml").is_file()
        )
        m = run_single_backtest(version, _snapshots)

        assert m["total_bars"] == len(_snapshots)
        assert m["participation"], "participation must be reported"
        for stats in m["participation"].values():
            assert {"abstain_pct", "emit_pct", "inert", "top_abstain_reason"} <= set(stats)
        # An inert strategy must always carry a diagnosable reason.
        for name in m["inert_strategies"]:
            assert m["participation"][name]["top_abstain_reason"]
        assert "execution_assumptions" in m
        assert m["benchmark"]["buy_hold_return_pct"] is not None

    def test_walk_forward_splits_window_without_overlap(
        self, _algorithms_dir: Path, sample_daily_df: pd.DataFrame
    ) -> None:
        from evotrader.backtest.runner import RiskParams, build_snapshots, walk_forward

        # 16 sessions so each fold clears the default 50-bar minimum.
        rows, prices, price = [], [], 430.0
        for day in range(40, 56):
            for hour in range(9, 16):
                rows.append(datetime(2025, 1, 1, hour, 30, tzinfo=UTC) + timedelta(days=day))
                price += 0.4
                prices.append(price)
        intraday = pd.DataFrame(
            {
                "open": prices,
                "high": [p + 0.5 for p in prices],
                "low": [p - 0.5 for p in prices],
                "close": prices,
                "volume": [1_000_000.0] * len(prices),
            },
            index=pd.DatetimeIndex(rows),
        )
        snapshots = build_snapshots(intraday, sample_daily_df, ticker="TEST")
        version = next(
            p.name
            for p in sorted(_algorithms_dir.iterdir())
            if p.is_dir() and (p / "config.yaml").is_file()
        )
        folds = walk_forward(version, snapshots, folds=2, risk=RiskParams())

        assert len(folds) == 2
        assert [f["fold"] for f in folds] == [1, 2]
        # Folds must be sequential and disjoint.
        assert folds[0]["window_end"] <= folds[1]["window_start"]
        assert sum(f["total_bars"] for f in folds) == len(snapshots)

    def test_walk_forward_rejects_impossible_split(
        self, _algorithms_dir: Path, _snapshots: list[MarketSnapshot]
    ) -> None:
        from evotrader.backtest.runner import RiskParams, walk_forward

        version = next(
            p.name
            for p in sorted(_algorithms_dir.iterdir())
            if p.is_dir() and (p / "config.yaml").is_file()
        )
        with pytest.raises(ValueError):
            walk_forward(version, _snapshots, folds=len(_snapshots) + 1, risk=RiskParams())

    def test_walk_forward_skips_short_fold_loudly(
        self,
        _algorithms_dir: Path,
        _snapshots: list[MarketSnapshot],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from evotrader.backtest.runner import RiskParams, walk_forward

        version = next(
            p.name
            for p in sorted(_algorithms_dir.iterdir())
            if p.is_dir() and (p / "config.yaml").is_file()
        )
        with caplog.at_level("WARNING"):
            folds = walk_forward(
                version,
                _snapshots,
                folds=2,
                risk=RiskParams(),
                min_fold_bars=len(_snapshots),
            )
        assert folds == []
        assert "skipped" in caplog.text

    def test_long_only_run_opens_no_shorts(
        self, _algorithms_dir: Path, _snapshots: list[MarketSnapshot]
    ) -> None:
        from evotrader.backtest.runner import RiskParams, run_single_backtest

        version = next(
            p.name
            for p in sorted(_algorithms_dir.iterdir())
            if p.is_dir() and (p / "config.yaml").is_file()
        )
        m = run_single_backtest(version, _snapshots, risk=RiskParams(allow_shorts=False))
        assert "SHORT" not in m.get("side_breakdown", {})
