"""Evolution models.

Schemas for self-evolution proposals, backtest results, and change tracking.
Used by the Evolution Agent to propose, validate, and log system changes.

Supports five evolution layers:
1. ALGORITHM_PARAMS — Tuning existing strategy thresholds/weights
2. ALGORITHM_NEW — Creating entirely new strategy code
3. INSTRUCTION_UPDATE — Modifying agent system prompts
4. REGIME_WEIGHTS — Adjusting regime-based algo/LLM weighting
5. CODE_REVIEW — Proposing improvements to infrastructure code
6. KNOWLEDGE_UPDATE — Evolving the semantic knowledge base
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class EvolutionChangeType(str, Enum):
    """Category of self-evolution change."""

    ALGORITHM_PARAMS = "ALGORITHM_PARAMS"
    ALGORITHM_NEW = "ALGORITHM_NEW"
    INSTRUCTION_UPDATE = "INSTRUCTION_UPDATE"
    REGIME_WEIGHTS = "REGIME_WEIGHTS"
    CODE_REVIEW = "CODE_REVIEW"
    KNOWLEDGE_UPDATE = "KNOWLEDGE_UPDATE"


class EvolutionStatus(str, Enum):
    """Lifecycle status of an evolution proposal."""

    PROPOSED = "PROPOSED"
    BACKTESTED = "BACKTESTED"
    VALIDATED = "VALIDATED"
    ACTIVE = "ACTIVE"
    ROLLED_BACK = "ROLLED_BACK"
    ARCHIVED = "ARCHIVED"
    PENDING_REVIEW = "PENDING_REVIEW"  # For code changes requiring human review


class RiskLevel(str, Enum):
    """Risk level of the evolution change."""

    LOW = "LOW"  # Parameter tweaks, knowledge updates
    MEDIUM = "MEDIUM"  # Instruction updates, regime weights
    HIGH = "HIGH"  # New algorithm code
    CRITICAL = "CRITICAL"  # Infrastructure code changes (human-only)


class BacktestResult(BaseModel):
    """Result of backtesting an evolution proposal against historical data."""

    start_date: str
    end_date: str
    total_trades: int
    win_rate: float
    profit_factor: float | None = None
    total_return_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    avg_trade_pnl: float
    avg_holding_period_hours: float | None = None

    @property
    def passes_minimum_criteria(self) -> bool:
        """Whether this backtest meets the minimum promotion criteria."""
        return (
            self.total_trades >= 10
            and self.win_rate >= 0.40
            and self.total_return_pct > 0
            and (self.sharpe_ratio is None or self.sharpe_ratio > 0)
        )


class PerformanceMetrics(BaseModel):
    """Current performance metrics snapshot for comparison."""

    period_start: str
    period_end: str
    total_return_pct: float
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    win_rate: float
    profit_factor: float | None = None
    max_drawdown_pct: float
    total_trades: int
    avg_trade_pnl: float


class CodeDiff(BaseModel):
    """Structured representation of a proposed code change."""

    file_path: str = Field(description="Relative path to the file being modified.")
    change_type: str = Field(description="Type of change: 'modify', 'create', or 'delete'.")
    description: str = Field(description="What this change does and why.")
    diff_content: str = Field(description="Unified diff format of the proposed change.")
    risk_assessment: str = Field(description="Assessment of risks this change introduces.")
    test_plan: str = Field(default="", description="How to verify this change works correctly.")


class EvolutionProposal(BaseModel):
    """A proposed change from the Evolution Agent.

    Every evolution must include reasoning, metrics comparison, and
    (for algorithm changes) backtest results before it can be promoted.
    """

    change_type: EvolutionChangeType
    risk_level: RiskLevel = RiskLevel.LOW
    target_component: str = Field(
        description="Which component to modify (e.g., 'strategy', 'orchestrator')."
    )
    old_version: str = Field(description="Current active version identifier.")
    new_version: str = Field(description="Proposed new version identifier.")

    reasoning: str = Field(description="Detailed explanation of why this change is proposed.")
    expected_impact: str = Field(
        default="", description="Expected impact on performance (e.g., '+5% win rate')."
    )
    metrics_before: PerformanceMetrics = Field(
        description="Performance metrics before the proposed change."
    )
    backtest_result: BacktestResult | None = Field(
        None,
        description="Backtest results for algorithm changes (required before promotion).",
    )

    # Parameter diff (for ALGORITHM_PARAMS / REGIME_WEIGHTS)
    parameter_changes: dict[str, dict[str, float]] = Field(
        default_factory=dict,
        description='Parameter changes: {"param_name": {"old": x, "new": y}}.',
    )

    # Code diffs (for ALGORITHM_NEW / CODE_REVIEW)
    code_diffs: list[CodeDiff] = Field(
        default_factory=list,
        description="Proposed code changes as unified diffs.",
    )

    # Instruction diff (for INSTRUCTION_UPDATE)
    instruction_diff_summary: str | None = Field(
        None, description="Human-readable summary of instruction changes."
    )

    # Knowledge updates (for KNOWLEDGE_UPDATE)
    knowledge_entries: list[str] = Field(
        default_factory=list,
        description="New knowledge entries to add to semantic memory.",
    )

    status: EvolutionStatus = EvolutionStatus.PROPOSED
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_promotable(self) -> bool:
        """Whether this proposal is ready to be promoted to active."""
        # Code reviews always need human approval
        if self.change_type == EvolutionChangeType.CODE_REVIEW:
            return False

        # Algorithm changes need backtesting
        if self.change_type in (
            EvolutionChangeType.ALGORITHM_PARAMS,
            EvolutionChangeType.ALGORITHM_NEW,
            EvolutionChangeType.REGIME_WEIGHTS,
        ):
            return self.backtest_result is not None and self.backtest_result.passes_minimum_criteria

        # Knowledge updates are always safe to auto-promote
        if self.change_type == EvolutionChangeType.KNOWLEDGE_UPDATE:
            return True

        # Instruction updates don't require backtesting
        return self.status == EvolutionStatus.PROPOSED

    @property
    def requires_human_review(self) -> bool:
        """Whether this change must be reviewed by a human."""
        return self.risk_level == RiskLevel.CRITICAL


class EvolutionRateLimits(BaseModel):
    """Rate limits for different evolution change types."""

    algo_params_per_day: int = 1
    algo_new_per_week: int = 1
    instructions_per_agent_per_week: int = 1
    code_reviews_per_day: int = 3
    knowledge_updates_per_day: int = 10
