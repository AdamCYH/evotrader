---
name: self-evolve
description: "Implement changes from the Evolution Agent — strategy proposals (new strategies, weight changes, parameter tuning) and code reviews (bug fixes, anti-patterns, infrastructure improvements). ACTIVATE this skill when you need to implement any evolution note from data/evolution/."
---

# Self-Evolve — Implementation Skill

This skill guides a coding agent through implementing changes proposed by the
EvoTrader Evolution Agent. The Evolution Agent produces three types of notes:

| Type | Location | Purpose |
|------|----------|---------|
| **Strategy Proposals** | `data/evolution/proposals/` | New strategies, weight changes, param tuning, deprecations |
| **Code Reviews** | `data/evolution/reviews/` | Bug fixes, anti-patterns, infrastructure improvements |
| **Carry-Forward Notes** | `data/evolution/notes/carry_forward.md` | Cross-cycle continuity items the previous evolve agent deferred |

Both follow the same core workflow: read the note → understand the change →
modify code → test → verify algorithms still work.

---

### Pre-Implementation: Session Context Readout

> [!IMPORTANT]
> Before implementing any evolution change, **read the evolution agent's
> session transcript** to understand its full reasoning, not just the
> proposal/review output.
>
> The evolution agent's session ID is typically provided by the user when
> invoking this skill. Read the session's final thoughts and artifacts:
>
> ```bash
> # Read the session's transcript for context
> # (adjust the conversation ID to the evolution session)
> grep '"type":"PLANNER_RESPONSE"' \
>   <appDataDir>/brain/<session-id>/.system_generated/logs/transcript.jsonl \
>   | tail -5
> ```
>
> This gives you the agent's reasoning about WHY each change was proposed,
> what trade-offs it considered, and what it expected the impact to be.
> This context is critical for making correct implementation decisions.

### Pre-Implementation: Cross-Cycle Carry-Forward Notes

> [!IMPORTANT]
> At the start of every self-evolve cycle, **read the carry-forward notes**:
>
> ```
> data/evolution/notes/carry_forward.md
> ```
>
> This file contains items the previous evolution agent deferred to future
> cycles. For each pending item:
> 1. Read and understand the context
> 2. Implement if it's in scope for the current cycle
> 3. **Remove the item** from the file once completed
> 4. Leave items that are out of scope for the current cycle
>
> The file uses a single rolling format — no stale file cleanup needed.
> The evolution agent appends new items; you remove completed ones.
>
> **When writing carry-forward notes** (if you are the evolution agent):
> - Complete as much work as possible in the current cycle
> - Only defer items that genuinely cannot be done now (e.g. need data
>   from a future trading session, depend on a human-gated change)
> - Write specific, actionable notes with enough context for a fresh agent
> - Do NOT defer work just because you're running long — finish it now
> - **Always update the `Last self-evolve run` timestamp** at the top of
>   `carry_forward.md` to the current UTC time (format: `YYYY-MM-DDTHH:MMZ`)

---

### Units Convention — `_pct` means PERCENT

> [!IMPORTANT]
> All `_pct` parameters and snapshot fields use **PERCENT** units.
> `1.0 = 1%`, `-2.38 = -2.38%`.
>
> | Value | Meaning |
> |-------|---------|
> | `1.2` | **1.2%** |
> | `0.3` | **0.3%** |
> | `-2.38` | **-2.38%** |
>
> This applies to:
> - `MarketSnapshot.daily_change_pct` and `gap_pct`
> - Config params: `min_break_pct`, `min_gap_pct`, `gap_fade_threshold`,
>   `divergence_day_change_pct`, `min_overextension`
> - Any new `_pct` param you propose
>
> **Do NOT use fraction values** (e.g. `0.012` for 1.2%) — that is WRONG.
> Validators reject `_pct` values above 10 (=10%) to catch confusion.
> Always add an inline comment confirming the human-readable meaning:
> ```yaml
> min_break_pct: 1.2    # 1.2%
> ```
>
> **Prefer the instrument's own units for a new threshold on a price move.**
> A percent means different things on different instruments: 0.6% was ~0.5
> daily ATR on QQQ and ~0.1 ATR on MSTR, so a percent threshold chosen for one
> ticker silently breaks on the next. For "how big is this move", express it
> as a multiple of `indicators.atr_14` (pattern:
> `momentum.divergence_day_change_atr`, 2026-09-23), with the percent kept only
> as a fallback when ATR is missing. Existing percent thresholds with this
> defect: `range_break_continuation.min_break_pct`,
> `trend_persistence.counter_day_pct`, `gap.min_gap_pct`.
>
> **Say which day a number describes.** A value computed from the daily series
> is the PRIOR session's during the day (its last bar does not update intraday).
> Label it the way `vwap_anchor` and `relative_volume_source` do, and never read
> it as today's tape.

---

### Backtests Verify — Live Data Calibrates

> [!IMPORTANT]
> Operator's standing rule (2026-09-18): **the backtest is a regression check,
> never the objective.** Calibration comes from what the system actually did —
> `signal_attribution` rows scored against realised forward returns, cycle
> digests, the trade journal — because only that data includes the agent
> layer, the real hourly cadence, real fills and the current instrument.
>
> What the backtest is:
> - **Algorithm only.** `BacktestEngine` runs mechanical rules (entry when the
>   composite crosses 0.03 trending / 0.05 otherwise, exit at 0.0, 2.0-ATR
>   stop, 3.5-ATR take-profit, fixed-fraction sizing). No strategy agent, no
>   news, no conviction sizing, no discretionary exits.
> - **Noisy.** On the standard run (primary ticker, 1h, 2y) the placebo
>   spread is **sd 1.86pp, MDE 3.72pp**. A delta inside that band is not
>   evidence in either direction; every parameter change to date has landed
>   inside it.
> - **A proxy on the short leg.** It shorts the underlying at 1x; live goes
>   through `asset.inverse_ticker` (-2x, tracking noise, its own spread).
> - **Blind to channels that need many intraday bars.** `recent_candles` is
>   session-scoped, and a session has at most 7 hourly bars — so on the
>   standard 1h run any channel needing more (e.g. `swing_failure_reversal`,
>   `lookback_bars` 20) abstains on 100% of bars under EVERY parameter set.
>   Identical headline numbers between two versions then prove nothing.
>   Always read the participation table; if the changed channel is inert, run
>   `--period 60d --interval 5m` (the data source's 5-minute limit) instead.
>   Found 2026-09-22: v027 and v028 matched to the cent on 1h/2y while the
>   channel went 0.0% -> 4.0% of bars voting on 5-minute data.
>
> Therefore, when implementing an evolution note:
> 1. **Do** run the before/after backtest (Verification step 5) to catch a
>    broken engine, a silenced channel, a sign flip or a participation
>    collapse. Report the delta **with** the placebo band and say plainly when
>    it is inside it.
> 2. **Do not** choose a parameter value, weight or threshold by what moves
>    the backtest number, and do not add a backtest improvement as a
>    justification of your own. If a proposal's only evidence is a backtest
>    delta, say so in the implementation report — the operator decides.
> 3. **Do** turn the live cycle the note cites into a unit-test fixture that
>    reproduces the engine's number (pattern: `test_composite_live_fixture.py`,
>    `test_range_break_intraday_confirm.py`). That fixture is the regression
>    case; the backtest is the smoke test.
> 4. **Do not** plan rollbacks or build rollback machinery. Code and config
>    both move forward; the operator rolls back with Claude Code if it is ever
>    needed. New parameters get identity defaults in code with the live value
>    in the active version's `config.yaml`.
> 5. Record in `carry_forward.md` which live cycles the change should be
>    checked against once they exist, so the next cycle calibrates on
>    evidence rather than re-running the backtest.
> 6. **Measure a channel-code fix on the live cycles, not the backtest.**
>    Every cycle stores its snapshot (`market_snapshots`: indicators and
>    every channel's vote). Rebuild the live composite through the real
>    `CompositeStrategy`, feeding stored votes for unchanged channels and
>    recomputing the changed one (exact for daily-resolution channels, which
>    need only stored indicators). First confirm it reproduces the live
>    composites to 4 decimals on the old code, then run it on the new code.
>    Hand-arithmetic misses the low-participation attenuation: the 2026-09-24
>    review estimated +0.326 where the engine gives +0.2330.
> 7. **A fix that moves the composite changes what past composites mean.**
>    The first cycle on new signal code or a new version gets an automatic
>    SYSTEM line in the trading handoff (`tools/engine_provenance.py`). If a
>    number the agent compares against is known (an entry composite), give it
>    recomputed on the new engine in a handoff line of its own.
>
> The live record itself is `signal_attribution`, scored after every cycle by
> `AttributionScorer` (`src/evotrader/evolution/attribution_scorer.py`) and
> read by the evolution tool `get_signal_calibration`. To inspect it yourself,
> query the table directly — `forward_return_1d`/`_5d` are percent, measured
> from `price_at_decision` to the close of the first (and fifth) session AFTER
> the decision's date.

---

## Quick Reference

| Item | Location |
|------|----------|
| Proposals directory | `data/evolution/proposals/` |
| Reviews directory | `data/evolution/reviews/` |
| Carry-forward notes | `data/evolution/notes/carry_forward.md` |
| Strategy code | `src/evotrader/algorithms/strategies/` |
| Strategy manifest | `data/algorithms/strategy_manifest.yaml` |
| Base class | `src/evotrader/algorithms/base.py` → `TradingAlgorithm` |
| Composite strategy | `src/evotrader/algorithms/composite.py` |
| Dynamic loader | `src/evotrader/algorithms/loader.py` |
| Active algorithm | `data/algorithms/active.yaml` |
| Algorithm configs | `data/algorithms/<version>/config.yaml` |
| Algorithm registry | `data/algorithms/registry.yaml` |
| Indicator modules | `src/evotrader/indicators/` |
| Signal model | `src/evotrader/models/signals.py` → `AlgoSignal` |
| Market model | `src/evotrader/models/market.py` → `MarketSnapshot` |
| Database layer | `src/evotrader/db/` |
| DB migrations | `src/evotrader/db/migrations/` |
| Migration runner | `src/evotrader/db/migration.py` |
| MCP integration | `src/evotrader/mcp/` |
| Constitution | `data/constitution.yaml` |
| Settings | `data/settings.yaml` |
| Tests | `tests/unit/` |
| Test conftest | `tests/conftest.py` |

---

## Input Formats

### Strategy Proposals

Markdown files with YAML frontmatter between `---` markers in
`data/evolution/proposals/`:

```markdown
---
proposal_id: p_new_vwap_scalper_20260624_040000
type: new_strategy          # new_strategy | deprecate | composition | param_tune
status: proposed            # proposed | in_progress | implemented | rejected
created_at: 2026-06-24T04:00:00Z
target_strategy: vwap_scalper
# ... type-specific fields
---

# Proposal: VWAP Scalper Strategy

## Reasoning
...
```

#### Frontmatter fields by type

| Type | Key Fields |
|------|-----------| 
| `new_strategy` | `target_strategy`, `required_indicators`, `suggested_weight` |
| `deprecate` | `target_strategy`, `redistribute_weight_to` |
| `composition` | `weight_changes` |
| `param_tune` | `target_strategy`, `param_changes` |

### Code Reviews

Markdown files in `data/evolution/reviews/` with the naming pattern:

```
<YYYYMMDD_HHMMSS>_<descriptive_slug>.md
```

Each review contains:

1. **Header** — Generated timestamp, Status (`PENDING_REVIEW`, `APPLIED`, `REJECTED`)
2. **Files Reviewed** — List of source files analyzed
3. **Findings** — Numbered issues with severity levels:
   - `[CRITICAL]` — Must fix, causes runtime failures or data loss
   - `[HIGH]` — Should fix, causes degraded behavior
   - `[MEDIUM]` — Good to fix, improves reliability or observability
   - `[LOW]` — Nice to have, style or documentation improvement
4. **Proposed Changes** — Concrete `diff` blocks for each file

---

## Implementation Workflows

### Workflow A: Code Reviews (Bug Fixes & Infrastructure)

#### Step 1: Read and Understand the Review

1. Open the review `.md` file and read every finding thoroughly.
2. Identify the **root cause** described in the findings.
3. Note which files are affected and how they relate to each other.

#### Step 2: Research Affected Code

1. Read every file listed in "Files Reviewed" in full.
2. Search for related patterns across the codebase that the review
   might NOT have caught (the review may miss some occurrences):
   ```bash
   # Example: if fixing a row_factory pattern, find ALL occurrences
   grep -rn "row_factory" src/evotrader/
   grep -rn "row\[0\]" src/evotrader/db/
   ```
3. Check for **cascading effects** — will the fix break other code
   that depends on the current (buggy) behavior?

> [!WARNING]
> The proposed diffs in reviews are suggestions, not gospel. The Evolution
> Agent may not see the full picture. Always verify that the proposed fix
> doesn't break other code paths. Common gotchas:
> - Centralizing a change (e.g., connection-level `row_factory`) may break
>   code that relies on different return types (index-based vs dict-based)
> - Removing an import used by test code
> - Changing return types that downstream consumers depend on

#### Step 3: Implement the Fix

Apply changes in dependency order:

1. **Infrastructure/shared code first** (e.g., `connection.py`, `base.py`)
2. **Direct consumers next** (e.g., `journal.py`, `metrics.py`)
3. **Downstream consumers last** (e.g., `reconciliation.py`, `server.py`)

For each file:
- Apply the proposed diff if it's correct
- Adapt the diff if the review missed edge cases
- Clean up unused imports after changes
- Update **all** callers that are affected

#### Step 4: Write Regression Tests

Create or update tests that specifically guard against the bug recurring:

```python
# tests/unit/test_<descriptive_name>.py
"""Regression tests: <brief description of what they guard against>.

See: data/evolution/reviews/<review_filename>.md
"""
```

Test patterns to include:
- **Type assertion** — verify return types match expectations
- **Access pattern** — exercise the exact access pattern that was crashing
- **Round-trip** — record → retrieve → use end-to-end
- **Isolation** — verify the fix doesn't pollute other code paths

---

### Workflow B: `new_strategy` — Add a New Strategy

1. **Read the proposal** — understand signal logic, parameters, required
   indicators, and integration plan.

2. **Verify indicators exist** — check `src/evotrader/models/market.py`
   (`TechnicalIndicators` fields) and `src/evotrader/indicators/` to confirm
   all required indicators are available in `MarketSnapshot.indicators`.
   If any are missing, implement them first.

3. **Create the strategy file** at
   `src/evotrader/algorithms/strategies/<name>.py`:

   ```python
   """<Strategy description>."""
   from __future__ import annotations
   from typing import Any
   from evotrader.algorithms.base import TradingAlgorithm
   from evotrader.models.market import MarketSnapshot
   from evotrader.models.signals import AlgoSignal

   class <ClassName>(TradingAlgorithm):

       def __init__(self, <params>, version: str = "v001") -> None:
           self._<param> = <param>
           self._version = version

       @property
       def name(self) -> str:
           return "<strategy_name>"

       @property
       def version(self) -> str:
           return self._version

       @property
       def description(self) -> str:
           return "<human-readable description>"

       def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
           # Access indicators via snapshot.indicators.<field>
           # Access price via snapshot.quote.last
           # Access regime via snapshot.regime.regime
           #
           # MUST return AlgoSignal with:
           #   name=self.name
           #   value in [-1.0, +1.0]
           #   weight=1.0  (composite handles actual weighting)
           #   metadata={...}  (strategy-specific debug info)
           ...

       def get_parameters(self) -> dict[str, Any]:
           return { ... }

       def set_parameters(self, params: dict[str, Any]) -> None:
           ...

       def validate_parameters(self, params: dict[str, Any]) -> list[str]:
           errors: list[str] = []
           # Add validation logic
           return errors
   ```

4. **Register in the manifest** — add an entry to
   `data/algorithms/strategy_manifest.yaml`:

   ```yaml
   <strategy_name>:
     module: evotrader.algorithms.strategies.<filename>
     class: <ClassName>
     description: "<description>"
     status: active              # or experimental for initial rollout
     added_in: <version>
     proposed_by: evolution_agent
   ```

5. **Update regime weights** — if the proposal specifies integration with
   specific regimes, update the default regime weights in
   `src/evotrader/algorithms/composite.py` →
   `_get_default_regime_weights_map()` to include the new strategy.

6. **Write tests** — add unit tests in `tests/unit/test_algorithms.py`:
   - Test with valid `MarketSnapshot` → signal in [-1, +1]
   - Test with missing indicators → graceful neutral signal
   - Test `get_parameters()` round-trip
   - Test `validate_parameters()` catches invalid values

---

### Workflow C: `deprecate` — Disable a Strategy

1. **Read the proposal** — understand the rationale and weight
   redistribution plan.

2. **Update the manifest** — in `data/algorithms/strategy_manifest.yaml`,
   set the target strategy's `status` to `disabled`:

   ```yaml
   gap:
     # ...
     status: disabled     # was: active
   ```

3. **Redistribute weights** — the proposal's `redistribute_weight_to` field
   specifies where the weight goes. Update other strategies' weights in the
   active algorithm version config (`data/algorithms/<version>/config.yaml`)
   or adjust `_get_default_regime_weights_map()` in `composite.py`.

4. **Do NOT delete the strategy code** — disabled strategies stay in
   `src/evotrader/algorithms/strategies/` for potential reactivation.
   The `StrategyLoader` automatically skips non-active strategies.

---

### Workflow D: `composition` — Change Ensemble Weights

1. **Read the proposal** — the `weight_changes` field has the new weight
   distribution.

2. **Update algorithm config** — modify the active algorithm version's
   `config.yaml` in `data/algorithms/<active_version>/config.yaml` to
   reflect the new `<strategy>_weight` values under the `composite` key.

3. **Verify weights sum to 1.0** — the composite strategy normalizes
   weights, but they should be intentional.

---

### Workflow E: `param_tune` — Adjust Strategy Parameters

1. **Read the proposal** — the `param_changes` field maps parameter names
   to `{old: ..., new: ...}` values.

2. **Create a new algorithm version** — copy the active version directory
   in `data/algorithms/` to a new version (e.g., `v003_<description>/`),
   update `config.yaml` with the new parameter values.

> [!NOTE]
> Parameter changes go through the versioned algorithm system. Do NOT
> modify the strategy Python source code for parameter-only changes.
> Parameters are injected at runtime via the constructor.

---

## Contract Rules

### `TradingAlgorithm` base class requirements

Every strategy **must**:

- Extend `TradingAlgorithm` (from `evotrader.algorithms.base`)
- Implement `name` property → unique string identifier
- Implement `version` property → version string (e.g., `"v001"`)
- Implement `compute_signal(snapshot: MarketSnapshot) -> AlgoSignal`
- Return `AlgoSignal` with `value` clamped to `[-1.0, +1.0]`
- Handle missing indicators gracefully (return neutral `0.0` signal)
- Be **stateless** — all state comes from `MarketSnapshot`

### Code safety rules

Strategy code must NOT:

- Import `os`, `sys`, `subprocess`, or `shutil`
- Use `eval()`, `exec()`, `open()`, or `__import__`
- Make network calls or access the filesystem
- Modify global state

### Available indicators

Check `MarketSnapshot.indicators` (type `TechnicalIndicators`) for fields:

| Category | Fields |
|----------|--------|
| Oscillators | `rsi_14`, `macd_line`, `macd_signal`, `macd_histogram` |
| Bands | `bollinger_upper`, `bollinger_middle`, `bollinger_lower`, `bollinger_width` |
| Trend | `ema_9`, `ema_21`, `sma_20`, `sma_50` |
| Intraday | `vwap`, `ibs` |
| Volatility | `atr_14`, `volume_sma_20`, `relative_volume` |

Additional snapshot fields: `quote.last`, `quote.bid`, `quote.ask`,
`quote.volume`, `daily_change_pct`, `gap_pct`, `regime.regime`.

---

## Key Codebase Patterns

### Database Layer (`src/evotrader/db/`)

- **Connection management**: `Database` class in `connection.py` provides
  `connection()` and `transaction()` async context managers.
- **Row factory**: `dict_factory` is set on the connection at `initialize()`
  time, so ALL reads return `dict` objects. Do NOT set `cursor.row_factory`
  after `execute()` — it doesn't work in aiosqlite.
- **Schema migrations**: `apply_migrations()` in `migration.py` runs BEFORE
  `row_factory` is set, so PRAGMA queries can use index-based access safely.
  The runner executes `.sql` files from `migrations/` and ignores idempotent
  errors (`duplicate column`, `table already exists`, `index already exists`)
  so partial failures can be retried safely on restart.
- **CRUD modules**: `journal.py` (trades), `metrics.py` (daily metrics),
  `thought_log.py` (agent thoughts), `reconciliation.py` (position sync).

### Algorithm System (`src/evotrader/algorithms/`)

- **Strategy manifest** at `data/algorithms/strategy_manifest.yaml` defines
  available strategies, their module paths, and status (`active`/`disabled`).
- **Versioned configs** in `data/algorithms/<version>/config.yaml` hold
  tunable parameters per strategy. The active version is set in
  `data/algorithms/active.yaml`.
- **Loader** (`loader.py`) dynamically imports strategies, instantiates with
  parameters, and builds the `CompositeStrategy`. It also parses
  `composite.regime_weights` from config and maps string keys (e.g.
  `"high_volatility"`) to `MarketRegime` enum members before passing them
  to `CompositeStrategy.__init__()`. When `regime_weights` is absent from
  config, the composite falls back to `_get_default_regime_weights_map()`.
- **Registry** (`data/algorithms/registry.yaml`) tracks version history and
  metadata for algorithm versions.

### Constitution (`data/constitution.yaml`)

- **Inviolable** — only human operators may edit.
- Contains `risk_limits`, `trading_rules`, and `circuit_breakers`.
- Enforced by the Risk Manager Agent on every trade proposal.

---

## Common Fix Patterns

### Pattern: Row Factory / Dict Access Fix
```python
# WRONG: Setting row_factory after execute() does nothing in aiosqlite
cursor = await conn.execute(query)
cursor.row_factory = dict_factory  # ← BROKEN
rows = await cursor.fetchall()

# RIGHT: dict_factory is set on the connection at initialize() time
# in connection.py, so just execute and fetch:
cursor = await conn.execute(query)
rows = await cursor.fetchall()  # ← Returns dicts automatically
```

### Pattern: Index-based → Dict-based Access Migration
```python
# BEFORE (index-based, breaks when row_factory returns dicts):
row = await cursor.fetchone()
return float(row[0]) if row else 0.0

# AFTER (dict-based, works with centralized dict_factory):
row = await cursor.fetchone()
return float(row["total_pnl"]) if row else 0.0

# For aggregate queries (COUNT, MAX, SUM) where column name is an expression:
row = await cursor.fetchone()
if row:
    return int(next(iter(row.values())))
return 0
```

### Pattern: Cleaning Up After Centralization
When a fix centralizes behavior (e.g., connection-level row_factory):
1. Remove all per-call assignments (now redundant/incorrect)
2. Remove unused imports from each file
3. Update ALL index-based access to dict-based access
4. Search the entire `src/` tree for remaining occurrences

### Pattern: Dead Config / Config-Code Drift
A config key exists in `config.yaml` but is never consumed by the code
that builds the runtime object. This is especially dangerous for
`regime_weights` and similar nested config because the system silently
falls back to hardcoded defaults with no warning.

When changing parameters or adding config sections:
1. **Trace the full path**: config file → loader parse → constructor arg
   → runtime usage. Verify every link.
2. **If the loader doesn't pass a config value**, the config is dead —
   fix the loader, don't just update the config file.
3. **When fixing an indicator/code bug**, treat dependent config metadata
   and hardcoded defaults as part of the same change set. Stale workaround
   weights are as harmful as stale workaround instructions.

### Pattern: Idempotent Schema Migrations
Migrations in `src/evotrader/db/migrations/` must be **idempotent** —
a partial failure (crash, timeout) must not block the next startup.

Rules for writing migrations:
1. **Always clean up temp tables first** — if recreating a table via
   `trades_new`, start with `DROP TABLE IF EXISTS trades_new;`.
2. **Use `IF NOT EXISTS` / `IF EXISTS`** — for `CREATE TABLE`,
   `CREATE INDEX`, and `DROP TABLE` statements.
3. **SQLite cannot `ALTER COLUMN`** — to change a column constraint
   (e.g. removing `NOT NULL`), you must:
   ```sql
   DROP TABLE IF EXISTS <table>_new;
   CREATE TABLE <table>_new ( ... );
   INSERT INTO <table>_new (...) SELECT ... FROM <table>;
   DROP TABLE <table>;
   ALTER TABLE <table>_new RENAME TO <table>;
   -- Recreate indexes
   ```
4. **Never use `SELECT *` for the data copy** — always list columns
   explicitly and wrap every `NOT NULL` target column in `COALESCE`
   to backfill safe defaults for legacy rows that may contain NULLs:
   ```sql
   INSERT INTO trades_new (confidence, ...)
   SELECT COALESCE(confidence, 1.0), ... FROM trades;
   ```
   Production data is messy; `IntegrityError: NOT NULL constraint
   failed` during a migration will block every startup.
5. **The migration runner auto-ignores** `duplicate column name`,
   `table already exists`, and `index already exists` errors — but
   other errors will crash startup, so design for retry safety.

---

## Status Update Rules

> [!IMPORTANT]
> Do **NOT** update the `Status` field in review or proposal files.
> The user manages status via the web UI, which syncs to the database.
> Modifying the file status directly will cause the UI and DB to be out of sync.

---

## Verification Checklist

After implementing **any** evolution note (proposal or review), verify:

```bash
# 1. Run the full test suite
uv run python -m pytest tests/ -v

# 2. Validate the strategy manifest loads correctly
uv run python -c "
from pathlib import Path
from evotrader.algorithms.loader import StrategyLoader
loader = StrategyLoader(Path('data/algorithms/strategy_manifest.yaml'))
errors = loader.validate_manifest()
if errors:
    print('Manifest errors:', errors)
else:
    active = loader.get_active_strategies()
    print('Active strategies:', list(active.keys()))
"

# 3. Verify the active algorithm version's config is loadable
uv run python -c "
from pathlib import Path
import yaml
active_data = yaml.safe_load(Path('data/algorithms/active.yaml').read_text())
version = active_data['active_version']
config_path = Path(f'data/algorithms/{version}/config.yaml')
if not config_path.exists():
    print(f'ERROR: Config for active version {version} not found at {config_path}')
else:
    params = yaml.safe_load(config_path.read_text())
    print(f'Active version: {version}')
    print(f'Strategies configured: {list(params.keys())}')
    # Verify composite weights sum to 1.0
    composite = params.get('composite', {})
    weights = {k: v for k, v in composite.items() if k.endswith('_weight')}
    total = sum(weights.values())
    print(f'Composite weights: {weights} (sum={total:.2f})')
    if abs(total - 1.0) > 0.01:
        print('WARNING: Composite weights do not sum to 1.0!')
"

# 4. Build the composite to confirm end-to-end parameter injection works
#    (including regime weights from config)
uv run python -c "
from pathlib import Path
import yaml
from evotrader.algorithms.loader import StrategyLoader
from evotrader.models.market import MarketRegime

loader = StrategyLoader(Path('data/algorithms/strategy_manifest.yaml'))
active = yaml.safe_load(Path('data/algorithms/active.yaml').read_text())
version = active['active_version']
params = yaml.safe_load(Path(f'data/algorithms/{version}/config.yaml').read_text())

composite = loader.build_composite(params)
print(f'Composite built: version={composite.version}')
print(f'Sub-strategies: {list(composite._strategies.keys())}')
print(f'Weights: {composite._weights}')
for regime in MarketRegime:
    rw = composite._regime_weights_map.get(regime)
    if rw:
        print(f'  {regime.value}: {rw}')
print('Algorithm verification: PASS')
"
# 5. Regression backtest — a SMOKE TEST, not a scorecard (see "Backtests Verify — Live Data Calibrates").
#    Compare the previously active version against the new one on the standard run and
#    report the delta together with the placebo band (sd 1.86pp / MDE 3.72pp on MSTR 1h 2y).
uv run python -m evotrader.backtest.runner --compare <previous_version>,<new_version> --period 2y --interval 1h
```

- [ ] All existing tests pass
- [ ] New regression tests added and passing (for code reviews)
- [ ] No unused imports remain in modified files
- [ ] Strategy manifest validates without errors
- [ ] Active algorithm version config loads correctly
- [ ] `StrategyLoader.build_composite()` succeeds with active params
- [ ] Composite weights sum to 1.0
- [ ] Regression backtest run and reported WITH the placebo band; no parameter chosen by its delta
- [ ] Evolution note `.md` file status is **NOT** modified (user manages via UI)
