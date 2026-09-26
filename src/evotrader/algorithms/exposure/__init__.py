"""Exposure-sizing strategies.

These answer "how much capital should be at risk?" rather than "which
direction?". They are kept separate from ``algorithms.strategies`` because
the two families answer different questions and are validated differently.
"""

from evotrader.algorithms.exposure.volatility_target import VolatilityTargetStrategy

__all__ = ["VolatilityTargetStrategy"]
