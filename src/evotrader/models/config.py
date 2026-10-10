"""Configuration schemas.

Pydantic models that validate and type-check the YAML configuration files
(``constitution.yaml`` and ``settings.yaml``). These are loaded once at
startup and can be hot-reloaded for ``settings.yaml``.

_pct field convention
~~~~~~~~~~~~~~~~~~~~~
All fields ending in ``_pct`` are written in **human-readable percent**
in the YAML files (e.g. ``5`` means 5%, ``100`` means 100%).  A shared
model validator (``_normalize_pct_fields``) automatically divides by 100
so that internal code always works with **decimal fractions** (0.05 for
5%).  This eliminates the ambiguity where ``1.0`` could be read as
either 1% or 100% — both humans and LLM agents found that confusing.
"""

from __future__ import annotations

from enum import Enum, StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TradingMode(str, Enum):
    """Trading execution mode."""

    LIVE = "live"
    SIM = "sim"


# ---------------------------------------------------------------------------
# Constitution (inviolable risk rules)
# ---------------------------------------------------------------------------


class RiskLimits(BaseModel):
    """Hard risk limits that the Evolution Agent can never modify.

    All ``_pct`` fields are written in human-readable percent in the YAML
    (e.g. ``5`` = 5%).  The ``_normalize_pct_fields`` validator converts
    them to decimal fractions for internal use (``5`` → ``0.05``).
    """

    max_single_position_pct: float = Field(
        10,
        ge=0.0,
        le=100.0,
        description="Max % of portfolio in a single long position (e.g. 100 = 100%).",
    )
    max_short_position_pct: float = Field(
        5,
        ge=0.0,
        le=100.0,
        description="Max % of portfolio in a single short position (e.g. 30 = 30%).",
    )
    max_total_exposure_pct: float = Field(
        80,
        ge=0.0,
        le=100.0,
        description="Max % of portfolio deployed (long + short notional).",
    )
    max_daily_loss_pct: float = Field(
        2,
        ge=0.0,
        le=100.0,
        description="Pause trading if daily loss exceeds this % (e.g. 5 = 5%).",
    )
    max_weekly_loss_pct: float = Field(
        5,
        ge=0.0,
        le=100.0,
        description="Pause trading if weekly loss exceeds this % (e.g. 10 = 10%).",
    )
    max_drawdown_pct: float = Field(
        10,
        ge=0.0,
        le=100.0,
        description="Full stop if drawdown from peak exceeds this % (e.g. 10 = 10%).",
    )
    require_stop_loss: bool = Field(
        True,
        description="Every position must have a stop-loss order.",
    )
    max_stop_loss_pct: float = Field(
        3,
        ge=0.0,
        le=100.0,
        description="Max stop-loss distance from entry as % (e.g. 5 = 5%).",
    )
    max_order_value_usd: float = Field(
        5000.0,
        ge=0.0,
        description="Maximum value of a single order in USD.",
    )

    @model_validator(mode="after")
    def _normalize_pct_fields(self) -> Self:
        """Convert human-readable percent (5 = 5%) to fraction (0.05).

        All ``_pct`` fields are divided by 100 so downstream code always
        works with decimal fractions.  This runs after Field validation
        (which accepts 0-100) and before any business logic.
        """
        for name in type(self).model_fields:
            if name.endswith("_pct"):
                raw = getattr(self, name)
                if isinstance(raw, (int, float)):
                    object.__setattr__(self, name, raw / 100.0)
        return self


class TradingRules(BaseModel):
    """Trading behavior rules enforced by the Risk Manager."""

    allowed_tickers: list[str] = Field(
        default_factory=lambda: ["SPY"],
        description="Tickers the system is allowed to trade.",
    )
    allow_extended_hours: bool = Field(
        True,
        description="Whether extended/overnight hours trading is permitted.",
    )
    extended_hours_tickers: list[str] | None = Field(
        None,
        description="Tickers allowed for 24-hour / extended hours trading. Defaults to allowed_tickers.",
    )
    allow_short_sell: bool = Field(
        True,
        description="Whether short selling is permitted.",
    )

    @model_validator(mode="after")
    def populate_extended_hours_tickers(self) -> TradingRules:
        if self.extended_hours_tickers is None:
            self.extended_hours_tickers = list(self.allowed_tickers)
        return self

    allow_margin: bool = Field(
        False,
        description="Whether margin trading is permitted.",
    )
    allow_options: bool = Field(
        True,
        description="Whether option trading (buying calls/puts) is permitted.",
    )
    allow_option_writing: bool = Field(
        False,
        description="Whether selling/writing options is permitted. Disabled = buy-only (debit strategies).",
    )
    max_option_premium_pct: float = Field(
        5,
        ge=0.0,
        le=100.0,
        description="Max % of portfolio value for a single option trade premium (e.g. 5 = 5%).",
    )
    max_total_option_exposure_pct: float = Field(
        20,
        ge=0.0,
        le=100.0,
        description="Max % of portfolio in total open option positions (e.g. 20 = 20%).",
    )

    @model_validator(mode="after")
    def _normalize_pct_fields(self) -> Self:
        """Convert human-readable percent to fraction — see RiskLimits."""
        for name in type(self).model_fields:
            if name.endswith("_pct"):
                raw = getattr(self, name)
                if isinstance(raw, (int, float)):
                    object.__setattr__(self, name, raw / 100.0)
        return self

    pdt_protection: bool = Field(
        True,
        description="Enforce pattern day trader awareness for sub-25k accounts.",
    )
    max_trades_per_day: int = Field(
        10,
        ge=1,
        description="Maximum number of trades per day to prevent overtrading.",
    )
    min_holding_period_seconds: int = Field(
        60,
        ge=0,
        description="Minimum holding period to prevent sub-minute flipping.",
    )


class CircuitBreakers(BaseModel):
    """Automatic trading halts triggered by adverse conditions."""

    consecutive_losses_pause: int = Field(
        5,
        ge=1,
        description="Pause trading after this many consecutive losses.",
    )
    pause_duration_minutes: int = Field(
        60,
        ge=1,
        description="Duration of trading pause in minutes.",
    )
    flash_crash_detection: bool = Field(
        True,
        description="Halt if asset drops >3%% in 5 minutes.",
    )


class WashSaleGuard(BaseModel):
    """Configurable wash sale detection to avoid IRS tax traps.

    When enabled, checks tax lot data before re-entering a position
    that was sold at a loss within the lookback window.
    """

    enabled: bool = Field(
        True,
        description="Whether to check tax lots for wash sale risk before trades.",
    )
    mode: str = Field(
        "warn",
        pattern="^(block|warn)$",
        description="'block' to reject the trade, 'warn' to allow with advisory warning.",
    )
    lookback_days: int = Field(
        30,
        ge=1,
        le=90,
        description="IRS wash sale lookback window in calendar days.",
    )


class LevelIIConfig(BaseModel):
    """Configuration for Level II (order book depth) data."""

    enabled: bool = Field(
        True,
        description="Whether to fetch Level II order book depth (via get_equity_price_book).",
    )
    depth_levels: int = Field(
        5,
        ge=1,
        le=20,
        description="Number of price levels to fetch each side (bid/ask).",
    )
    thin_book_warn_pct: float = Field(
        50,
        ge=0.0,
        le=100.0,
        description="Warn if order size > this % of visible depth (e.g. 50 = 50%).",
    )

    @model_validator(mode="after")
    def _normalize_pct_fields(self) -> Self:
        """Convert human-readable percent to fraction — see RiskLimits."""
        for name in type(self).model_fields:
            if name.endswith("_pct"):
                raw = getattr(self, name)
                if isinstance(raw, (int, float)):
                    object.__setattr__(self, name, raw / 100.0)
        return self


class Constitution(BaseModel):
    """Inviolable risk rules. Only human operators may modify this.

    The Evolution Agent is explicitly forbidden from altering any field
    in this model. Changes require manual editing of ``constitution.yaml``.
    """

    risk_limits: RiskLimits = Field(default_factory=RiskLimits)
    trading_rules: TradingRules = Field(default_factory=TradingRules)
    circuit_breakers: CircuitBreakers = Field(default_factory=CircuitBreakers)
    wash_sale_guard: WashSaleGuard = Field(default_factory=WashSaleGuard)
    level_ii: LevelIIConfig = Field(default_factory=LevelIIConfig)

    def to_llm_display_dict(self) -> dict[str, Any]:
        """Return a dict with ``_pct`` fields in human-readable percent.

        Internally, all ``_pct`` fields are stored as decimal fractions
        (0.05 = 5%) for code convenience.  LLM agents, however, misread
        ``1.0`` as "1%" instead of "100%".  This method reverses the
        normalisation so the LLM sees unambiguous values::

            max_single_position_pct: "100%"
            max_daily_loss_pct: "5%"

        Use this for ANY serialisation that an LLM will read — prompts,
        tool return values, logs shown to models.  For internal code,
        keep using the raw model attributes (which are fractions).
        """
        raw = self.model_dump()
        _denormalize_pct_recursive(raw)
        return raw


def _denormalize_pct_recursive(d: dict[str, Any]) -> None:
    """Walk a dict tree and convert ``_pct`` fraction values to ``"N%"`` strings.

    Mutates *d* in place.  Nested dicts are walked recursively.
    The string format (``"5%"``) is intentional — it is unambiguous to
    both humans and LLMs, unlike a bare ``5`` or ``0.05``.
    """
    for key, val in list(d.items()):
        if isinstance(val, dict):
            _denormalize_pct_recursive(val)
        elif key.endswith("_pct") and isinstance(val, (int, float)):
            pct = val * 100
            # Use integer display if whole number (100%, 5%), else one decimal (2.5%)
            d[key] = f"{pct:.0f}%" if pct == int(pct) else f"{pct:.1f}%"


# ---------------------------------------------------------------------------
# Settings (hot-reloadable system configuration)
# ---------------------------------------------------------------------------


class UniverseConfig(BaseModel):
    """How the traded ticker's constituent universe is resolved.

    Nothing here names a ticker or an ETF: holdings are resolved from live data
    keyed on whatever ``primary_ticker`` (and the constitution's
    ``allowed_tickers``) point at, so changing instrument needs no code or
    config change beyond the ticker itself.
    """

    top_n: int = Field(
        15,
        ge=1,
        description=(
            "Largest holdings to treat as relevant. Combined with "
            "min_weight_pct as a union, so either rule can include a symbol."
        ),
    )
    min_weight_pct: float = Field(
        1.0,
        ge=0.0,
        le=100.0,
        description=(
            "Any holding at or above this percentage of the fund is relevant, even beyond top_n."
        ),
    )
    cache_ttl_days: int = Field(
        7,
        ge=0,
        description=(
            "How long a resolved holdings list stays fresh. Holdings change "
            "slowly; a stale cache is still preferred over no data if a refresh "
            "fails."
        ),
    )


class InstrumentInfo(BaseModel):
    """What a symbol *is*, in terms an agent can act on.

    Without this the agent sees a bare ticker and has to infer the instrument
    from price behaviour — which is how a leveraged inverse fund gets sized like
    ordinary stock. ``leverage`` is the part that changes arithmetic, not just
    understanding: a -2x fund carries twice the exposure per dollar, so the same
    position size is twice the risk.
    """

    description: str = Field(
        "",
        description=(
            "One short clause naming the instrument and its role. Rendered "
            "inline beside the ticker, so keep it to a few words."
        ),
    )
    leverage: float = Field(
        1.0,
        description=(
            "Daily move relative to the primary ticker. 1.0 = moves with it, "
            "-1.0 = plain inverse, -2.0 = twice the move in the opposite "
            "direction. Used to derive position sizing, so a wrong value "
            "silently doubles or halves risk."
        ),
    )
    role: str = Field(
        "",
        description="Free text: 'primary', 'bearish vehicle', 'hedge', etc.",
    )


class AssetConfig(BaseModel):
    """Target asset configuration."""

    # Validate on assignment as well as on load. Settings are hot-reloaded and
    # the evolution agent can propose changes, so a field set at runtime must be
    # coerced and checked the same way a loaded one is — otherwise a plain dict
    # assigned to `instruments` survives until something reads `.leverage` deep
    # in a trading path and raises there instead of here.
    model_config = ConfigDict(validate_assignment=True)

    primary_ticker: str = "SPY"
    inverse_ticker: str | None = Field(
        None,
        description=(
            "Inverse ETF used to express a bearish view on primary_ticker "
            "without margin or expiry (e.g. PSQ for QQQ, SH for SPY). Leave "
            "null when none exists — most single stocks have no inverse "
            "product, and the strategy instructions then fall back to puts or "
            "to standing aside. Never invent one: a wrong inverse ETF tracks a "
            "different index."
        ),
    )
    instruments: dict[str, InstrumentInfo] = Field(
        default_factory=dict,
        description=(
            "Per-symbol descriptions and leverage, keyed by ticker. Optional: a "
            "symbol with no entry renders as a bare ticker, exactly as before."
        ),
    )
    universe: UniverseConfig = Field(default_factory=UniverseConfig)

    def describe(self, ticker: str) -> str:
        """Ticker with its short description, e.g. ``MSTR (traded instrument)``."""
        info = self.instruments.get(ticker)
        text = (info.description if info else "") or (info.role if info else "")
        return f"{ticker} ({text})" if text else ticker

    def leverage_of(self, ticker: str) -> float:
        info = self.instruments.get(ticker)
        return info.leverage if info else 1.0


class McpProviderEntry(BaseModel):
    """Configuration for a single MCP provider.

    Supports both HTTP-based (streamable_http / sse) and stdio-based
    MCP servers.  For HTTP servers, set ``url``.  For stdio servers,
    set ``command`` and ``args``.
    """

    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    auth: str | None = None
    transport: str = Field(
        "streamable_http",
        description="Transport type: 'streamable_http', 'sse', or 'stdio'.",
    )
    command: str | None = Field(
        None,
        description="Command to run for stdio transport (e.g. 'uvx').",
    )
    args: list[str] = Field(
        default_factory=list,
        description="Arguments for the stdio command.",
    )


class CurationConfig(BaseModel):
    """Shaping of oversized MCP tool responses.

    Trade quality outranks cost here, so the switches are separated by how much
    judgement each one requires rather than bundled into one flag. See
    ``evotrader.mcp.response_curators``.
    """

    enabled: bool = Field(
        True,
        description=(
            "Master switch. When off, MCP responses reach agents exactly as the "
            "server returned them."
        ),
    )
    dedupe_structured_content: bool = Field(
        True,
        description=(
            "Drop `structuredContent` when it is byte-identical to the text "
            "content. ~46% of all MCP payload is this duplicate. Equality is "
            "verified per response, so it cannot lose information — which is why "
            "it applies to trading tools too."
        ),
    )
    timeseries_max_rows: int = Field(
        120,
        ge=10,
        description=(
            "Recent observations kept from an economic time series. The full "
            "history is summarised into statistics alongside them (latest, "
            "changes, all-time and 1-year extremes), so long-horizon comparisons "
            "still work. ~6 months of daily data at the default."
        ),
    )
    news_max_age_days: int = Field(
        21,
        ge=0,
        description=(
            "Primary news lever: keep articles within this many days of the "
            "feed's newest item. 0 disables it. This is the shape the consuming "
            "agent actually reasons in — its instructions treat anything older "
            "than ~5 sessions as 'largely priced' — and unlike a fixed count it "
            "adapts to how busy the news week was. 21 days gives roughly 3x that "
            "relevance window."
        ),
    )
    news_max_articles: int = Field(
        30,
        ge=0,
        description=(
            "Hard ceiling on articles, applied after the age window. 0 = no "
            "ceiling. Guards against an unusually busy period; the age window "
            "does the ordinary work."
        ),
    )
    news_min_articles: int = Field(
        8,
        ge=0,
        description=(
            "Floor kept regardless of age, so a quiet stretch never leaves the "
            "news agent with almost nothing to reason from. Applied before the "
            "ceiling."
        ),
    )
    backstop_max_chars: int = Field(
        40_000,
        ge=0,
        description=(
            "Safety net for tools with no curator: responses above this size are "
            "cut on a whole-entry boundary and told so. 0 disables it. Catches "
            "the next unfamiliar endpoint that returns a megabyte."
        ),
    )
    backstop_roles: list[str] = Field(
        default_factory=lambda: ["research"],
        description=(
            "MCP roles the backstop applies to. Research only by default: "
            "trading tools return positions, orders and quotes, which are "
            "bounded by nature and where a partial answer is more dangerous "
            "than a large one. Add 'trading' to opt in."
        ),
    )
    news_prefer_latest: bool = Field(
        True,
        description=(
            "Ask news endpoints to sort by recency. These feeds rank by "
            "relevance by default, which in practice returned 50 articles "
            "spanning 47 days when only 6 were inside the ~5-session window the "
            "news agent treats as unpriced. This improves relevance at the same "
            "cost rather than trading one for the other."
        ),
    )
    news_filter_ticker_sentiment: bool = Field(
        True,
        description=(
            "Keep per-article `ticker_sentiment` rows only for symbols in the "
            "resolved trading universe. Measured saving is modest (~4 points on "
            "a real payload) and it is not strictly lossless — an unrelated "
            "ticker's relevance row is dropped — but the universe is resolved "
            "from live holdings, so what it drops really is unrelated. Each "
            "article's title, summary and overall sentiment are untouched, and "
            "the filter self-disables if the universe is unresolved."
        ),
    )


class ToolFilterConfig(BaseModel):
    """Hides MCP tool schemas an agent cannot use from its context.

    The executor was measured carrying **114,338 tokens of tool descriptions**
    (77 tools) on every single call — 3.09M tokens in one day, 58% of every token
    the system moved. ``place_crypto_order`` alone is ~9,850 tokens, on an agent
    trading MSTR equity and options.

    **Exclusion-only, by design.** There is no allowlist here. A tool that
    matches nothing stays *visible*, so a pattern that fails to classify
    something costs tokens rather than removing a tool the order path needs.
    Getting this backwards would mean an agent silently unable to place a trade,
    which is far worse than a large prompt.
    """

    enabled: bool = Field(True, description="Whether to filter tool schemas at all.")
    exclude: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Per-agent case-insensitive substrings matched against tool names. "
            "Keyed by agent name; an agent absent from this map is unfiltered."
        ),
    )
    always_keep: list[str] = Field(
        default_factory=list,
        description=(
            "Exact tool names never hidden, whatever the patterns match. A "
            "backstop for the order path — belt and braces against a pattern "
            "that turns out to be broader than intended."
        ),
    )

    def hidden_for(self, agent: str, tool_names: list[str]) -> set[str]:
        """Which of *tool_names* should be hidden from *agent*."""
        if not self.enabled:
            return set()
        patterns = [p.lower() for p in self.exclude.get(agent, []) if p]
        if not patterns:
            return set()
        keep = set(self.always_keep)
        return {
            name
            for name in tool_names
            if name not in keep and any(p in name.lower() for p in patterns)
        }


class McpConfig(BaseModel):
    """MCP connection configuration.

    Multiple providers are mapped to logical **roles** (e.g.
    ``trading``, ``research``).  Each role can be bound to a single
    provider (str) or an ordered list (list[str]) for fallback.
    """

    providers: dict[str, McpProviderEntry] = Field(
        default_factory=lambda: {
            "robinhood_official": McpProviderEntry(
                url="https://agent.robinhood.com/mcp/trading",
            ),
        },
    )
    curation: CurationConfig = Field(default_factory=CurationConfig)
    tool_filter: ToolFilterConfig = Field(default_factory=ToolFilterConfig)
    roles: dict[str, str | list[str]] = Field(
        default_factory=dict,
        description=(
            "Maps logical roles to provider names (str) or ordered lists "
            "of provider names (list[str]) for priority-based fallback.  "
            "Example: {'trading': 'robinhood_official', "
            "'research': ['alpha_vantage', 'stock_analysis']}"
        ),
    )

    def active_provider(self) -> McpProviderEntry:
        """Return the primary trading MCP provider."""
        value = self.roles.get("trading")
        if value is None:
            raise ValueError("No 'trading' role configured in mcp.roles")
        name = value[0] if isinstance(value, list) else value
        return self.provider_by_name(name)

    def provider_by_name(self, name: str) -> McpProviderEntry:
        """Look up a provider by name."""
        if name not in self.providers:
            raise ValueError(
                f"MCP provider '{name}' not found in providers: {list(self.providers.keys())}"
            )
        return self.providers[name]

    def providers_for_role(self, role: str) -> list[tuple[str, McpProviderEntry]]:
        """Return (name, entry) pairs for a given role.

        Supports both single-provider (str) and multi-provider (list[str])
        role mappings.  For list values, providers are returned in the
        configured priority order (primary first, fallback last).

        Returns an empty list if the role is not configured.
        """
        value = self.roles.get(role)
        if value is None:
            return []
        if isinstance(value, list):
            return [(name, self.provider_by_name(name)) for name in value]
        return [(value, self.provider_by_name(value))]


class ScheduleConfig(BaseModel):
    """Execution schedule — how often the trading cycle runs.

    The ``description`` is injected into agent prompts so the LLM can
    calibrate its trading horizon (intraday scalps vs. swing trades).
    """

    description: str = Field(
        "Manual trigger only (on-demand via UI)",
        description="Human-readable schedule description. Injected into agent prompts.",
    )
    cycle_cron_enabled: bool = Field(
        False,
        description="Whether automated cycle scheduling is enabled on web server startup.",
    )
    evolution_cron_enabled: bool = Field(
        False,
        description="Whether automated self-evolution scheduling is enabled on web server startup.",
    )
    metrics_cron: str | None = Field(
        "0 16,20 * * *",
        description="Optional cron expression (5-field) for automated EOD metrics syncing.",
    )
    cycle_cron: str | list[str] | None = Field(
        None,
        description=(
            "Cron expression(s) (5-field, US/Eastern) for automated trading "
            "cycles. A list fires a cycle when ANY entry matches, which is the "
            "only way to express 'hourly through the session PLUS once after "
            "the close' — those differ in both the minute and hour field."
        ),
    )
    evolution_cron: str | None = Field(
        None,
        description="Optional cron expression (5-field) for automated self-evolution.",
    )
    overnight_interval_seconds: int = Field(
        3600, ge=60, description="Trading loop interval during overnight hours."
    )
    max_cycle_events: int = Field(
        100,
        gt=0,
        description="Maximum events (tool calls/thoughts/etc.) allowed in a single cycle run before systematic termination.",
    )
    max_cycle_duration_seconds: int = Field(
        600,
        gt=0,
        description=(
            "Wall-clock timeout (in seconds) for a single trading cycle. "
            "If the cycle exceeds this duration (e.g. due to a hung LLM call), "
            "it is forcefully cancelled. Default: 600s (10 minutes)."
        ),
    )
    provider_retry_delay_seconds: int = Field(
        90,
        ge=0,
        le=300,
        description=(
            "When a cycle stops on a temporary model-provider error (503, 429, "
            "timeout) before any order could have been placed, wait this long "
            "and resume the same cycle once. The SDK's own retry spans ~20 s; "
            "demand spikes last minutes, and the next cycle is an hour away. "
            "0 disables."
        ),
    )
    max_evolution_duration_seconds: int = Field(
        2400,
        gt=0,
        description=(
            "Wall-clock timeout (in seconds) for a single evolution cycle. "
            "Evolution cycles read source code and perform deep analysis, "
            "so they need more time than trading cycles. Default: 2400s (40 minutes)."
        ),
    )

    @model_validator(mode="after")
    def _warn_on_shared_minute(self) -> Self:
        """Say when the evolution run and a trading cycle can fall on one minute.

        The scheduler starts the cycle and holds the evolution run until it
        ends, but a minute of its own keeps the evolution run on time.
        """
        import logging as _logging

        from evotrader.cron import crons_can_coincide, iter_cron_expressions

        for evolution in iter_cron_expressions(self.evolution_cron):
            for cycle in iter_cron_expressions(self.cycle_cron):
                if crons_can_coincide(cycle, evolution):
                    _logging.getLogger(__name__).warning(
                        "schedule: evolution_cron %r and cycle_cron %r can fall on the same "
                        "minute; the cycle runs first and the evolution run waits for it. "
                        "Give the evolution run a minute of its own (e.g. 15 past).",
                        evolution,
                        cycle,
                    )
        return self


class ModelConfig(BaseModel):
    """LLM model selection per agent. ``None`` means use SDK default."""

    default: str | None = None
    orchestrator: str | None = None
    strategy_agent: str | None = None
    news_sentiment_agent: str | None = None
    risk_manager_agent: str | None = None
    executor_agent: str | None = None
    evolution_agent: str | None = None

    def for_agent(self, agent_name: str) -> str | None:
        """Return the model identifier for a given agent, falling back to default."""
        agent_model = getattr(self, agent_name, None)
        return agent_model or self.default


class DualModelConfig(BaseModel):
    """Dual model configuration for sim and live environments."""

    sim: ModelConfig = Field(default_factory=ModelConfig)
    live: ModelConfig = Field(default_factory=ModelConfig)


class RegimeWeightEntry(BaseModel):
    """Algo vs. LLM weighting for a specific market regime."""

    algo: float = Field(ge=0.0, le=1.0)
    llm: float = Field(ge=0.0, le=1.0)

    @field_validator("llm")
    @classmethod
    def weights_sum_to_one(cls, v: float, info: Any) -> float:
        algo = info.data.get("algo", 0.0)
        if abs(algo + v - 1.0) > 0.01:
            raise ValueError(f"algo ({algo}) + llm ({v}) must sum to 1.0")
        return v


class RegimeDetectionMethod(str, Enum):
    """Supported regime detection algorithms."""

    RULE_BASED = "rule_based"
    HMM = "hmm"
    CLUSTERING = "clustering"


class StrategyConfig(BaseModel):
    """Strategy parameters — evolvable by the Evolution Agent."""

    entry_threshold: float = Field(
        0.3, ge=0.0, le=1.0, description="Minimum hybrid score to enter a trade."
    )
    min_confidence: float = Field(
        0.4, ge=0.0, le=1.0, description="Minimum confidence to allow a trade."
    )
    regime_detection: RegimeDetectionMethod = RegimeDetectionMethod.RULE_BASED

    algo_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "rsi": 0.20,
            "macd": 0.15,
            "bollinger": 0.15,
            "vwap": 0.15,
            "ibs": 0.15,
            "moving_average": 0.10,
            "volume": 0.10,
        },
        description="Weights for individual algo indicators (must sum to 1.0).",
    )

    regime_weights: dict[str, RegimeWeightEntry] = Field(
        default_factory=lambda: {
            "trending_bull": RegimeWeightEntry(algo=0.4, llm=0.6),
            "trending_bear": RegimeWeightEntry(algo=0.3, llm=0.7),
            "range_bound": RegimeWeightEntry(algo=0.7, llm=0.3),
            "high_volatility": RegimeWeightEntry(algo=0.5, llm=0.5),
        },
    )

    @field_validator("algo_weights")
    @classmethod
    def algo_weights_sum_to_one(cls, v: dict[str, float]) -> dict[str, float]:
        total = sum(v.values())
        if abs(total - 1.0) > 0.01:
            raise ValueError(f"algo_weights must sum to 1.0, got {total:.4f}")
        return v


class PositionSizingMethod(str, Enum):
    """Supported position sizing methods."""

    FIXED = "fixed"
    VOLATILITY_TARGET = "volatility_target"
    KELLY = "kelly"


class PositionSizingConfig(BaseModel):
    """Position sizing parameters."""

    method: PositionSizingMethod = PositionSizingMethod.VOLATILITY_TARGET
    volatility_target_annual: float = Field(
        0.15, ge=0.01, le=1.0, description="Target annualized volatility."
    )
    max_position_pct: float = Field(
        0.10, ge=0.0, le=1.0, description="Maximum position as portfolio percentage."
    )
    default_stop_loss_atr_multiplier: float = Field(
        2.0, ge=0.5, le=10.0, description="Stop-loss distance as ATR multiplier."
    )
    target_atr_multiplier: float | None = Field(
        None,
        ge=0.1,
        le=10.0,
        description=(
            "The first profit target (T1) as a multiple of the daily ATR above each "
            "lot's entry: the number the strategy instruction states. When set, every "
            "held lot in the market-data snapshot reports how far it is from that "
            "target (t1_distance_atr). Unset, nothing is reported."
        ),
    )


class DryRunConfig(BaseModel):
    """Paper-trading / dry-run configuration."""

    enabled: bool = Field(True, description="When True, orders are logged but not executed.")
    log_would_be_trades: bool = Field(True, description="Log hypothetical trades in dry-run mode.")


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: str = "INFO"
    trade_journal_verbose: bool = True


class CachingConfig(BaseModel):
    """Anthropic prompt-caching configuration.

    Caching is a prefix match over ``tools`` → ``system`` → ``messages``. A
    breakpoint on the system prompt alone leaves the (growing) conversation
    history uncached, which is where most of the input spend lives in an
    agentic loop. See ``evotrader.agents.anthropic_cache``.
    """

    enabled: bool = Field(True, description="Insert Anthropic cache_control breakpoints.")
    message_breakpoints: int = Field(
        2,
        ge=0,
        le=3,
        description=(
            "Breakpoints placed in the message history, newest turn first. "
            "0 caches only tools+system (the old behaviour). Anthropic allows "
            "4 per request and the system prompt uses one."
        ),
    )
    min_cacheable_chars: int = Field(
        4000,
        ge=0,
        description=(
            "Skip message breakpoints while the serialised history is smaller "
            "than this, so a short exchange doesn't pay for a cache write that "
            "nothing will read."
        ),
    )
    require_multi_turn: bool = Field(
        True,
        description=(
            "Only cache the message history once an agent turn has happened. "
            "A breakpoint on the very first call writes a cache nothing reads "
            "back, which costs 1.25x on the delta instead of 1.0x."
        ),
    )
    ttl: str = Field(
        "5m",
        description=(
            "Cache lifetime: '5m' (default, 1.25x write cost) or '1h' "
            "(2x write cost). '1h' only pays off when calls are more than five "
            "minutes apart but less than an hour."
        ),
    )

    @field_validator("ttl")
    @classmethod
    def _validate_ttl(cls, v: str) -> str:
        if v not in ("5m", "1h"):
            raise ValueError("caching.ttl must be '5m' or '1h'")
        return v


# Agents whose factory actually READS ``agent_runtime`` and can be hosted on a
# CLI harness. Everything else builds an ``LlmAgent`` unconditionally, so naming
# it here-but-not-in-code would be worse than useless: the setting would look
# honoured and be ignored.
#
# Adding an agent to this set is NOT what enables hosting. The factory must call
# ``Settings.runtime_for(<name>)`` and return a ``CliBackedAgent``. Two further
# blockers apply before any agent can move:
#
#   * ``local_tools`` accepts plain functions only — an agent carrying an
#     ``McpToolset`` (news_sentiment: research, execution: trading) needs a
#     bridge that does not exist yet.
#   * The constitution risk gate is an ADK ``before_tool_callback`` on the
#     execution ``LlmAgent``. ``CliBackedAgent`` has no equivalent hook, so
#     hosting execution would place orders with NO gate. Do not.
CLI_CAPABLE_AGENTS: frozenset[str] = frozenset({"evolution", "strategy"})


class AgentRuntimeKind(StrEnum):
    """How an agent's model is reached.

    ``API`` is the ADK/LiteLLM path: a completion endpoint, billed per token,
    fully instrumented in telemetry. ``CLI`` is a hosted-agent harness driven as
    a subprocess — billable to a subscription seat, but the harness owns the loop
    so we see turns and usage rather than individual calls.
    """

    API = "api"
    CLI = "cli"


# Retained so existing settings files and pickled configs keep loading. The
# values map onto the generic runtime selection: "adk" -> the API runtime,
# "claude_code" -> the CLI runtime of the same name.
class EvolutionBackend(StrEnum):
    """Deprecated. Use ``agent_runtime.evolution`` instead."""

    ADK = "adk"
    CLAUDE_CODE = "claude_code"


class QuotaPolicy(BaseModel):
    """What to do as a CLI runtime's plan quota runs out.

    Acting on the *warning* is the point. A subscription seat has a hard wall,
    and hitting it mid-cycle costs a trading decision — so the policy drains
    early rather than failing late.
    """

    warn_utilization: float = Field(
        0.85,
        ge=0.0,
        le=1.0,
        description=(
            "Route the NEXT task to the fallback once the harness reports this "
            "fraction of the window consumed. The current task always finishes "
            "where it started, so no decision is ever abandoned half-made."
        ),
    )
    on_exhausted: str = Field(
        "api",
        description=(
            "When the wall is hit: 'api' re-runs the task on the metered API "
            "(costs money, keeps trading), 'skip' abandons the task (free, "
            "loses the cycle), 'fail' raises so a human notices."
        ),
    )

    @field_validator("on_exhausted")
    @classmethod
    def _validate_on_exhausted(cls, v: str) -> str:
        allowed = {"api", "skip", "fail"}
        if v not in allowed:
            raise ValueError(f"quota.on_exhausted must be one of {sorted(allowed)}")
        return v


class CliRuntimeConfig(BaseModel):
    """One configured CLI-hosted agent runtime.

    ``driver`` names the adapter; the config key names *this* runtime. Keeping
    them separate is what makes the layer pluggable: two runtimes can share a
    driver with different models, and a future ``gemini_cli`` driver needs no
    change here beyond a new entry.
    """

    driver: str = Field(
        "claude_code",
        description=(
            "Registered adapter that implements this runtime. See "
            "evotrader.agents.cli.registered_drivers() for what is available."
        ),
    )
    billing: str = Field(
        "subscription",
        description=(
            "'subscription' withholds the API credential from the subprocess so "
            "API billing is impossible — a run that succeeds is proof of how it "
            "was billed. 'api' bills per token. 'auto' tries the subscription "
            "then falls back to the API on an auth failure, logging which."
        ),
    )
    model: str = Field(
        "",
        description=(
            "Harness model alias ('opus', 'sonnet', 'haiku') or a full model id. "
            "Empty uses the harness default."
        ),
    )
    thinking: str = Field(
        "default",
        description=(
            "'default' sends nothing and lets the harness decide (recommended). "
            "'adaptive' explicitly requests model-chosen depth, 'off' disables "
            "it, or give a positive integer token budget."
        ),
    )
    effort: str | None = Field(
        None,
        description="Coarse reasoning dial: low, medium, high, xhigh, max. Null = unset.",
    )
    max_turns: int = Field(
        80,
        ge=1,
        description=(
            "Hard cap on agentic turns — stops a runaway loop consuming the plan's usage window."
        ),
    )
    permission_mode: str = Field(
        "dontAsk",
        description=(
            "'dontAsk' denies anything not explicitly allowed, which is what an "
            "unattended run wants."
        ),
    )
    allow_file_tools: bool = Field(
        True,
        description=(
            "Let the harness read the repo with its own file tools. Write, Edit "
            "and Bash are blocked unconditionally by the adapter."
        ),
    )
    quota: QuotaPolicy = Field(default_factory=QuotaPolicy)

    @field_validator("billing")
    @classmethod
    def _validate_billing(cls, v: str) -> str:
        allowed = {"subscription", "api", "auto"}
        if v not in allowed:
            raise ValueError(f"cli_runtimes.*.billing must be one of {sorted(allowed)}")
        return v

    @field_validator("effort")
    @classmethod
    def _validate_effort(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        allowed = {"low", "medium", "high", "xhigh", "max"}
        if v not in allowed:
            raise ValueError(f"cli_runtimes.*.effort must be null or one of {sorted(allowed)}")
        return v

    @field_validator("thinking")
    @classmethod
    def _validate_thinking(cls, v: str) -> str:
        # Delegate to the same parser the backend uses, so a value that passes
        # config validation cannot fail later at request-build time.
        from evotrader.agents.cli.types import ThinkingSpec

        ThinkingSpec.parse(v)
        return v

    @field_validator("permission_mode")
    @classmethod
    def _validate_permission_mode(cls, v: str) -> str:
        allowed = {"default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"}
        if v not in allowed:
            raise ValueError(f"cli_runtimes.*.permission_mode must be one of {sorted(allowed)}")
        return v


# Deprecated alias kept so an older settings.yaml still loads.
ClaudeCodeConfig = CliRuntimeConfig


class EvolutionConfig(BaseModel):
    """Self-evolution feature toggles and rate limits."""

    backend: EvolutionBackend | None = Field(
        None,
        description=(
            "DEPRECATED — use `agent_runtime.evolution` instead. Kept so an "
            "older settings.yaml still loads; it is migrated forward on load."
        ),
    )
    claude_code: CliRuntimeConfig | None = Field(
        None,
        description="DEPRECATED — use the `cli_runtimes` section instead.",
    )
    auto_promote: bool = Field(
        False, description="Auto-promote evolved algos without human review."
    )
    backtest_min_days: int = Field(
        60, ge=7, description="Minimum backtest window for evolution proposals."
    )
    max_algo_changes_per_day: int = Field(1, ge=1)
    max_instruction_changes_per_week: int = Field(1, ge=1)
    rollback_threshold_sharpe_diff: float = Field(
        1.0,
        ge=0.0,
        description="Auto-rollback if new version Sharpe is this much worse.",
    )


class SimBrokerConfig(BaseModel):
    """Configuration for simulated trading."""

    slippage_model: str = Field(
        "spread", description="Slippage model: 'none', 'spread', or 'fixed'"
    )
    slippage_bps: float = Field(
        5.0, ge=0.0, description="Slippage basis points for fixed slippage model"
    )
    auto_close_expired_options: bool = Field(
        True, description="Whether to auto-close options at expiration"
    )


class Settings(BaseModel):
    """System configuration — hot-reloadable at runtime.

    Loaded from ``settings.yaml``. The system watches this file and
    reloads on change via a file-change trigger.
    """

    asset: AssetConfig = Field(default_factory=AssetConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    model: ModelConfig | DualModelConfig = Field(default_factory=ModelConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    position_sizing: PositionSizingConfig = Field(default_factory=PositionSizingConfig)
    mode: TradingMode = Field(
        TradingMode.SIM, description="Trading execution mode: 'live' or 'sim'"
    )
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    caching: CachingConfig = Field(default_factory=CachingConfig)
    evolution: EvolutionConfig = Field(default_factory=EvolutionConfig)
    cli_runtimes: dict[str, CliRuntimeConfig] = Field(
        default_factory=dict,
        description=(
            "Named CLI-hosted agent runtimes. The key names the runtime, its "
            "`driver` names the adapter that implements it — so two runtimes can "
            "share a driver with different models, and adding a new harness is a "
            "new entry plus a new adapter, nothing else."
        ),
    )
    agent_runtime: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Which runtime each agent uses: 'api' for the metered ADK path, or "
            "the name of a `cli_runtimes` entry. Agents not listed use 'api'."
        ),
    )
    sim_broker: SimBrokerConfig = Field(default_factory=SimBrokerConfig)
    portfolio_cache_seconds: int = Field(
        1800,
        ge=0,
        description="Web API portfolio cache duration in seconds (0 to disable).",
    )
    dashboard_poll_interval_seconds: int = Field(
        10,
        ge=1,
        description="Dashboard UI polling interval in seconds.",
    )
    require_trade_approval: bool = Field(
        True,
        description="Require manual trade approval in the web console UI before executing trades.",
    )

    @model_validator(mode="after")
    def _migrate_and_validate_runtimes(self) -> Settings:
        """Carry the old evolution-only backend keys forward, then check refs.

        The previous shape hard-coded one agent and one vendor
        (``evolution.backend: claude_code``). Rather than break a settings file
        pulled from an older commit, it is migrated here and a warning names the
        replacement — a silently ignored setting is how the billing defect went
        unnoticed for a week, so a stale key must never be quietly dropped.
        """
        import logging as _logging

        log = _logging.getLogger(__name__)
        legacy_backend = self.evolution.backend
        legacy_runtime = self.evolution.claude_code

        if legacy_runtime is not None and "claude_code" not in self.cli_runtimes:
            self.cli_runtimes["claude_code"] = legacy_runtime
            log.warning(
                "settings: `evolution.claude_code` is deprecated — migrated to "
                "`cli_runtimes.claude_code`. Move it in settings.yaml."
            )
        if legacy_backend is not None and "evolution" not in self.agent_runtime:
            target = "api" if legacy_backend is EvolutionBackend.ADK else legacy_backend.value
            self.agent_runtime["evolution"] = target
            log.warning(
                "settings: `evolution.backend: %s` is deprecated — migrated to "
                "`agent_runtime.evolution: %s`. Move it in settings.yaml.",
                legacy_backend.value,
                target,
            )

        for agent, runtime in self.agent_runtime.items():
            if runtime == AgentRuntimeKind.API.value:
                continue
            if runtime not in self.cli_runtimes:
                raise ValueError(
                    f"agent_runtime.{agent} = {runtime!r} names no runtime. Use "
                    f"'api' or one of the cli_runtimes keys: "
                    f"{sorted(self.cli_runtimes) or '(none defined)'}"
                )
            if agent not in CLI_CAPABLE_AGENTS:
                # NOT raised: this is a valid config that simply does nothing,
                # and refusing to start a live trading system over a stale key
                # would be worse than the silence it replaces. But it must be
                # impossible to miss — the setting looks like it works, reads
                # like it works in settings.yaml, and is ignored end to end.
                log.error(
                    "settings: agent_runtime.%s = %r is IGNORED. Only %s read "
                    "this setting; every other agent is built as an LlmAgent "
                    "unconditionally, so %s still runs on the API runtime. "
                    "Hosting it would need its factory to call runtime_for(), "
                    "and — for any agent holding MCP toolsets or a "
                    "before_tool_callback risk gate — new plumbing first.",
                    agent,
                    runtime,
                    sorted(CLI_CAPABLE_AGENTS),
                    agent,
                )
        return self

    def runtime_for(self, agent: str) -> tuple[AgentRuntimeKind, CliRuntimeConfig | None]:
        """Resolve how *agent* should be run.

        Returns ``(API, None)`` for the metered path, or ``(CLI, config)`` for a
        hosted harness. Unlisted agents default to the API path: a typo in an
        agent name must not silently move a real agent onto a different runtime.

        Only the agents in :data:`CLI_CAPABLE_AGENTS` actually CALL this — every
        other factory builds an ``LlmAgent`` unconditionally. Configuring a CLI
        runtime for anything else is a silent no-op, so the settings validator
        logs an error for it at load time.
        """
        name = self.agent_runtime.get(agent, AgentRuntimeKind.API.value)
        if name == AgentRuntimeKind.API.value:
            return AgentRuntimeKind.API, None
        runtime = self.cli_runtimes.get(name)
        if runtime is None:  # pragma: no cover - blocked by the validator above
            return AgentRuntimeKind.API, None
        return AgentRuntimeKind.CLI, runtime

    @field_validator("model", mode="before")
    @classmethod
    def validate_model_config(cls, v: Any) -> Any:
        if isinstance(v, dict):
            if "sim" in v or "live" in v:
                return DualModelConfig.model_validate(v)
            return ModelConfig.model_validate(v)
        return v

    @property
    def active_model(self) -> ModelConfig:
        """Return the active ModelConfig based on the current trading mode."""
        if isinstance(self.model, DualModelConfig):
            if self.mode == TradingMode.SIM:
                return self.model.sim
            return self.model.live
        return self.model

    @property
    def dry_run(self) -> DryRunConfig:
        """Dynamic backward compatibility getter for dry_run."""
        return DryRunConfig(enabled=(self.mode == TradingMode.SIM))
