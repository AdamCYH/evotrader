"""Decoupled historical backtesting package for evotrader."""

from evotrader.backtest.attribution import (
    AttributionReport,
    CalibrationBucket,
    SourceScore,
    attribute,
    score_calibration,
)
from evotrader.backtest.data import HistoricalDataFetcher
from evotrader.backtest.engine import BacktestEngine, BacktestTrade, PositionSide
from evotrader.backtest.metrics import calculate_tear_sheet
from evotrader.backtest.snapshot_builder import SnapshotBuilder
from evotrader.backtest.validation import (
    ValidationReport,
    ValidationResult,
    circular_shift_indices,
    minimum_detectable_effect,
    placebo_test,
    validate,
)

__all__ = [
    "AttributionReport",
    "BacktestEngine",
    "BacktestTrade",
    "CalibrationBucket",
    "HistoricalDataFetcher",
    "PositionSide",
    "SnapshotBuilder",
    "SourceScore",
    "ValidationReport",
    "ValidationResult",
    "attribute",
    "calculate_tear_sheet",
    "circular_shift_indices",
    "minimum_detectable_effect",
    "placebo_test",
    "score_calibration",
    "validate",
]
