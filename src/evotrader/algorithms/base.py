"""Abstract base class for trading algorithms.

All trading algorithms in EvoTrader implement this interface. The
design enables hot-swapping, versioning, parameter evolution, and
ensemble composition via the registry system.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from itertools import pairwise
from typing import Any

from evotrader.models.market import OHLCV, MarketSnapshot
from evotrader.models.signals import AlgoSignal

#: Minutes in a full regular session, 9:30 to 16:00 ET.
REGULAR_SESSION_MINUTES = 390.0


def warming_up(candles: Sequence[OHLCV], bars_needed: int) -> bool:
    """Whether a channel short of session bars will have enough before the close.

    A channel that needs ``bars_needed`` bars of the current session and has
    fewer is in one of two states, and the composite's participation scale
    treats them differently:

    * **warming up**: the session is producing bars at a pace that reaches
      ``bars_needed`` before the close. The channel is on duty, only early, and
      it is counted in the participation denominator, so the same votes are
      scaled alike at 10:30 and at 14:30. A 20-bar channel has 12 five-minute
      bars at 10:30 ET and 24 at 11:30, so without this the denominator grew
      by one between those two cycles for no market reason.
    * **unreachable**: a full session never holds that many bars at this pace
      (20 one-hour bars do not fit in 6.5 hours). That channel cannot speak
      today at all and stays out, like an off-duty one.

    The pace is the smallest positive gap between consecutive bars (a missing
    bar can only widen a gap). Two bars are needed to measure it, so with
    fewer the answer is False: the pre-market and opening-print cycles keep the
    denominator they had.
    """
    n = len(candles)
    if n < 2 or n >= bars_needed:
        return False
    gaps = [
        (later.timestamp - earlier.timestamp).total_seconds() / 60.0
        for earlier, later in pairwise(candles)
    ]
    pace = min((g for g in gaps if g > 0), default=None)
    if pace is None:
        return False
    return REGULAR_SESSION_MINUTES / pace >= bars_needed


class TradingAlgorithm(ABC):
    """Base class for all trading algorithms.

    Subclasses implement signal computation from market data. Algorithms
    are stateless — all state is carried in the ``MarketSnapshot`` input
    and the algorithm's ``parameters`` dict.

    Example::

        class MomentumStrategy(TradingAlgorithm):
            @property
            def name(self) -> str:
                return "momentum"

            @property
            def version(self) -> str:
                return "v001"

            def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
                # ... compute signal from snapshot.indicators ...
                return AlgoSignal(name="momentum", value=0.6, weight=1.0)
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique algorithm name (e.g., 'momentum', 'mean_reversion')."""
        ...

    @property
    @abstractmethod
    def version(self) -> str:
        """Version identifier (e.g., 'v001'). Incremented on parameter changes."""
        ...

    @property
    def description(self) -> str:
        """Human-readable description of the algorithm's approach."""
        return ""

    @property
    def resolution(self) -> str:
        """How often this channel's INPUTS can actually change.

        One of ``"daily"``, ``"intraday"`` or ``"event"``. Defaults to
        ``"daily"`` — the conservative answer, since most channels are built
        from daily MAs, daily MACD and daily RSI.

        This exists because the composite cannot currently tell a fresh reading
        from a daily value re-emitted for the eighth time today. Measured
        2026-09-17: ``momentum`` and ``mean_reversion`` produced a combined
        numerator of 0.0247, 0.0298, 0.0313, 0.0307, 0.0312, 0.0311 across six
        hourly cycles — one witness counted six times, holding 0.31 of
        trending_bull weight against ``intraday_vwap_zscore``'s 0.18. The
        constant pair outweighed the only channel carrying live intraday
        information.

        **Emitted only. Nothing attenuates on it yet, deliberately.** The point
        is to make the resolution mix COUNTABLE, so the implied claim that
        ``pool_sizes`` counts independent witnesses can be audited against real
        cycles. After ~20 sessions of census, consider a repeated-value
        de-rating analogous to the vwap_z staleness decay.
        """
        return "daily"

    @abstractmethod
    def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
        """Compute a trading signal from the current market state.

        Args:
            snapshot: Complete market snapshot including quote, indicators,
                and regime classification.

        Returns:
            An ``AlgoSignal`` with ``value`` in [-1.0, +1.0] where:
            - +1.0 = maximum bullish conviction
            - -1.0 = maximum bearish conviction
            -  0.0 = no signal / neutral
        """
        ...

    def get_parameters(self) -> dict[str, Any]:
        """Return the current tunable parameters as a dictionary.

        Used by the Evolution Agent to inspect and propose changes.
        Override in subclasses to expose algorithm-specific parameters.
        """
        return {}

    def set_parameters(self, params: dict[str, Any]) -> None:  # noqa: B027 (optional hook)
        """Apply new parameter values.

        Used by the Algorithm Registry to apply evolved parameters.
        Override in subclasses to accept algorithm-specific parameters.

        Args:
            params: Dictionary of parameter names to new values.
                    Unknown keys are silently ignored.
        """

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        """Validate proposed parameter values before applying them.

        Returns a list of validation error messages (empty if valid).
        Override in subclasses to add algorithm-specific validation.
        """
        return []

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r}, version={self.version!r})"
