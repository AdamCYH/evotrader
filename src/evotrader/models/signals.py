"""Signal models.

Schemas for algorithmic signals, LLM sentiment reports, and the hybrid
score that combines them. These flow between the Strategy Agent,
News & Sentiment Agent, and the Risk Manager.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SignalDirection(str, Enum):
    """Directional bias of a signal."""

    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class AlgoSignal(BaseModel):
    """Output of a single algorithmic indicator or composite strategy.

    The ``value`` field is always in [-1.0, +1.0] where:
    - +1.0 = maximum bullish signal
    - -1.0 = maximum bearish signal
    -  0.0 = neutral / no signal
    """

    name: str = Field(description="Indicator or strategy name.")
    value: float = Field(ge=-1.0, le=1.0, description="Signal strength and direction.")
    weight: float = Field(ge=0.0, le=1.0, description="Weight of this signal in the composite.")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Indicator-specific metadata (e.g., RSI value, gap type).",
    )

    @property
    def direction(self) -> SignalDirection:
        if self.value > 0.05:
            return SignalDirection.BULLISH
        elif self.value < -0.05:
            return SignalDirection.BEARISH
        return SignalDirection.NEUTRAL


class CompositeAlgoSignal(BaseModel):
    """Weighted combination of multiple algorithmic signals."""

    signals: list[AlgoSignal] = Field(description="Individual indicator signals.")
    composite_value: float = Field(ge=-1.0, le=1.0, description="Weighted composite signal.")
    algo_version: str = Field(description="Algorithm version that produced this signal.")
    renormalized: bool = Field(
        default=False,
        description="True when weights were renormalized over applicable-only sub-signals.",
    )
    # Pool sizes as ACTUALLY used by the ensemble arithmetic. Reported rather
    # than recomputed downstream: compute_signal previously rebuilt these with
    # its own comprehensions, so editing one filter and not the other would
    # have silently desynchronised the reported participation_ratio from the
    # one applied to the composite.
    n_additive: int = Field(
        default=0, ge=0, description="Additive (non-overlay) sub-signals considered."
    )
    n_applicable: int = Field(
        default=0, ge=0, description="Additive sub-signals able to vote this cycle."
    )
    n_voting: int = Field(
        default=0,
        ge=0,
        description="Applicable sub-signals that actually emitted beyond SIGNAL_EPSILON.",
    )
    # Channels whose preconditions are absent in this regime — off-duty rather
    # than silent. Counting an off-duty channel as an absent witness under-
    # states participation and triggers an unearned attenuation haircut.
    n_in_scope: int = Field(
        default=0,
        ge=0,
        description="Applicable sub-signals whose preconditions exist this regime.",
    )
    # ── THE COMPOSITE IS A FUNCTION OF THE HOUR, NOT ONLY OF INFORMATION ──
    # The low-participation attenuation scales the composite by how many
    # channels could speak, and that count changes with the clock: intraday
    # channels are out of scope pre-market and after-hours. On 2026-09-25 MSTR
    # held identical channel votes all day and read +0.3800 at 08:30 ET (no
    # attenuation), +0.2297 from 11:30 to 15:30 (scaled by (2/7)/0.4 = 0.714)
    # and +0.3231 at 17:00. The strategy add rule compares "composite now"
    # against "composite at entry", so with a pre-market entry it could not pass
    # during regular hours no matter what the market did — eight refusals the
    # agent explained as a price echo when the cause was arithmetic.
    #
    # `composite_value` is unchanged and is still the traded number. These two
    # record what it was before the hour was applied, so a comparison across
    # hours can be made on like terms.
    composite_unattenuated: float | None = Field(
        default=None,
        description="Composite before the low-participation scale. None when not computed.",
    )
    participation_scale: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Factor the participation attenuation applied. 1.0 = none.",
    )
    # The two counts the scale was computed from, so it can be checked from the
    # cycle's own record: scale = (numerator / denominator) / 0.4 when that
    # ratio is under 0.4, else 1.0. Both count only channels with weight in this
    # regime; the denominator also counts channels still collecting their
    # session bars. 0 and 0 when there were no additive channels.
    participation_numerator: int = Field(
        default=0,
        ge=0,
        description="Weighted channels that voted, as counted by the attenuation.",
    )
    participation_denominator: int = Field(
        default=0,
        ge=0,
        description="Weighted channels on duty (in scope or warming up), as counted.",
    )
    # Names of survivors that hit MAX_AMPLIFICATION during renormalization.
    # When the cap binds on EVERY survivor it divides out of the subsequent
    # normalisation and has no effect at all, so its real influence is only
    # visible by recording where it bound.
    amplification_capped: list[str] = Field(
        default_factory=list,
        description="Pool members whose effective weight hit MAX_AMPLIFICATION.",
    )

    @property
    def direction(self) -> SignalDirection:
        if self.composite_value > 0.05:
            return SignalDirection.BULLISH
        elif self.composite_value < -0.05:
            return SignalDirection.BEARISH
        return SignalDirection.NEUTRAL


class ExposureTarget(BaseModel):
    """Target portfolio exposure as a fraction of capital.

    This is the exposure-semantics counterpart to :class:`AlgoSignal`.
    Where an ``AlgoSignal`` answers "which way?", an ``ExposureTarget``
    answers "how much?" — and unlike direction, that question has a
    measurable answer.

    The distinction matters because volatility is autocorrelated and
    returns are not: turbulent days cluster together, while up days do
    not. Sizing against forecastable volatility is therefore a different
    activity from betting on unforecastable direction.

    ``value`` is a multiple of capital:
    - 0.0 = flat, no market exposure
    - 1.0 = fully invested
    - 1.5 = 150% invested (levered)
    """

    value: float = Field(
        ge=0.0,
        le=3.0,
        description="Target exposure as a multiple of capital (1.0 = fully invested).",
    )
    target_volatility: float = Field(
        gt=0.0,
        description="Annualised volatility the sizing aims to hold, as a fraction (0.20 = 20%).",
    )
    realized_volatility: float | None = Field(
        None,
        description="Trailing annualised volatility measured from recent data, as a fraction.",
    )
    reason: str = Field(
        default="",
        description="Human-readable explanation of how this exposure was derived.",
    )
    capped_by: str | None = Field(
        None,
        description=(
            "Name of the limit that bound this target, if any — e.g. 'max_leverage', "
            "'rebalance_band', 'insufficient_history'. None when the raw target was used."
        ),
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Strategy-specific diagnostics.",
    )

    @property
    def is_flat(self) -> bool:
        """True when this target implies holding no market exposure."""
        return self.value <= 1e-9

    @property
    def is_levered(self) -> bool:
        """True when this target implies borrowing."""
        return self.value > 1.0


class SentimentItem(BaseModel):
    """Sentiment analysis of a single news article or data point."""

    source: str = Field(description="Source of the information (e.g., 'Reuters').")
    headline: str = Field(description="Article headline or summary.")
    sentiment_score: float = Field(
        ge=-1.0, le=1.0, description="Sentiment from -1 (bearish) to +1 (bullish)."
    )
    relevance: float = Field(ge=0.0, le=1.0, description="Relevance to the target asset.")
    timestamp: datetime | None = None


class SentimentReport(BaseModel):
    """Output of the News & Sentiment Agent.

    Aggregates multiple sentiment items into a single directional signal
    with confidence and reasoning.
    """

    ticker: str
    timestamp: datetime
    items: list[SentimentItem] = Field(
        default_factory=list, description="Individual sentiment analyses."
    )
    aggregate_score: float = Field(ge=-1.0, le=1.0, description="Overall sentiment score.")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in the sentiment assessment.")
    key_events: list[str] = Field(
        default_factory=list, description="Key upcoming or recent events."
    )
    recommendation: SignalDirection = Field(description="Directional recommendation.")
    reasoning: str = Field(description="LLM's natural language reasoning.")


class HybridScore(BaseModel):
    """Combined algorithmic + LLM signal with regime-dependent weighting.

    This is the final decision signal used to generate trade proposals.
    """

    algo_signal: float = Field(ge=-1.0, le=1.0, description="Algorithmic composite signal.")
    llm_signal: float = Field(ge=-1.0, le=1.0, description="LLM sentiment signal.")
    algo_weight: float = Field(ge=0.0, le=1.0, description="Regime-determined algo weight.")
    llm_weight: float = Field(ge=0.0, le=1.0, description="Regime-determined LLM weight.")
    weighted_score: float = Field(ge=-1.0, le=1.0, description="Final weighted score.")
    confidence: float = Field(ge=0.0, le=1.0, description="Signal agreement confidence.")
    regime: str = Field(description="Market regime at decision time.")

    @property
    def direction(self) -> SignalDirection:
        if self.weighted_score > 0.05:
            return SignalDirection.BULLISH
        elif self.weighted_score < -0.05:
            return SignalDirection.BEARISH
        return SignalDirection.NEUTRAL
