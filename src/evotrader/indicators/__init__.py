"""Technical indicator computations and central indicator registry.

Pure-function modules that take OHLCV data and return indicator values,
along with a central registry for dynamic indicator discovery, formatting,
and snapshot normalization.
"""

from evotrader.indicators.registry import (
    INDICATOR_REGISTRY,
    IndicatorFormat,
    IndicatorSpec,
    format_indicator_value,
    get_indicator_spec,
    normalize_snapshot_payload,
)

__all__ = [
    "INDICATOR_REGISTRY",
    "IndicatorFormat",
    "IndicatorSpec",
    "format_indicator_value",
    "get_indicator_spec",
    "normalize_snapshot_payload",
]
