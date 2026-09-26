---
name: evotrader-developer
description: Developer guide for the EvoTrader codebase — how the agents, instructions, model selection, broker connection and tool wiring fit together, the safety rules the code enforces, how to debug and inspect the databases, test patterns, and recipes for common changes (add a tool, change instructions, add a strategy, add an MCP provider). Read before changing code in this repository.
---

# Developer guide — EvoTrader

For coding agents and people changing the code. It does not repeat what is
written elsewhere; read these first:

- [AGENTS.md](../../../AGENTS.md) — the rules for a coding session: no live trades,
  test first, the data folder, proposal `Status` fields, account data.
- [CONTRIBUTING.md](../../../CONTRIBUTING.md) — setup, the checks every change must
  pass, how instruction and algorithm versions work.
- [docs/architecture.md](../../../docs/architecture.md) — the parts, one trading
  cycle step by step, the self-evolve loop, and
  [where to find things](../../../docs/architecture.md#where-to-find-things).

## The other skills (next to this one, in `.agents/skills/`)

Read the matching skill before starting a task it covers.

| Skill | Use it to |
|---|---|
| [backtest](../backtest/SKILL.md) | Backtest a strategy idea, version, parameter change or instrument with `--validate`, and read the verdict before the return. |
| [mcp-debugger](../mcp-debugger/SKILL.md) | List an MCP server's tools, show a tool's input schema, call a read-only tool, or dump every schema. It refuses order tools, and refuses to run while the app is running. |
| [offline-iteration](../offline-iteration/SKILL.md) | Tune agent instructions or algorithm parameters with scenario tests and backtests, without false positives and without trading. |
| [self-evolve](../self-evolve/SKILL.md) | Implement an evolution proposal or code review from the data folder's `evolution/`, test first. |
| [signal-validation](../signal-validation/SKILL.md) | Check that a signal, instruction or strategy change really works. **Read it before promoting any algorithm version or instruction change.** |

---

## Operations

`./run.sh` runs everything through `uv run` when uv is installed, and falls back
to the project's `.venv` otherwise.

| Command | What it does |
|---|---|
| `./run.sh` (same as `./run.sh --dashboard`) | Starts the **web console** at http://127.0.0.1:8080. Options: `--mode sim\|live`, `--mock-time [ISO time]` (forces practice mode), `--data-dir PATH`, `--sim-deposit USD` (adds play money to the practice account, then exits) |
| `./run.sh offline` | Runs one cycle without the console and exits (for cron). The log goes to `<data folder>/logs/cycle.log`. There is no approval gate in this mode |
| `./run.sh setup` | The onboarding questions: AI provider, keys, console password. `scripts/setup.sh` installs everything first, then runs the same questions |
| `./run.sh --init` | Creates missing data-folder files from `starter_data/`; never overwrites |
| `./run.sh cli-dashboard` | Legacy one-screen terminal report (`--days N`, `--trades N`) |
| `./run.sh --help` | All options |

The data folder is `data/` in the project unless `EVOTRADER_DATA_DIR` (or
`--data-dir`, which wins) says otherwise. Code must build every path into it
from `evotrader.paths.data_dir()`.

---

## Architecture

### Agent system (Google ADK, LiteLLM)

`src/evotrader/agents/factory.py` builds six agents. Market data is gathered by
a code tool, `gather_market_data`, not by an agent (the old `market_intel` agent
is gone).

| Agent | Built by | Its tools | Broker (MCP) access |
|---|---|---|---|
| orchestrator | `create_orchestrator_agent` | `gather_market_data`, `gather_option_chain`, `get_market_status`, `get_performance_summary`, `get_trade_history`, `get_open_positions`, `reconcile_pending_orders`, `reconcile_broker_pnl` | Trading role, filtered to `cancel_equity_order` and `cancel_option_order` only |
| news_sentiment | `create_news_sentiment_agent` | `get_trade_history`, `query_user_notes`, `get_earnings_history` | Research role (Alpha Vantage, Yahoo Finance), filtered by `mcp.tool_filter` |
| strategy | `create_strategy_agent` | `_STRATEGY_TOOLS`: `compute_risk_budget`, `gather_market_data`, `get_open_positions`, `get_ticker_snapshot`, `log_signal_attribution`, `query_past_trades`, `query_user_notes`, `store_learning`, `update_trading_handoff` | None |
| risk_manager | `create_risk_manager_agent` | `check_risk_limits`, `check_option_risk_limits`, `get_open_positions`, `get_performance_summary` | None |
| execution (instructions in `executor/`) | `create_execution_agent` | `record_trade`, `get_open_positions`, `get_market_status`, `assess_order_book` | Trading role, filtered by `mcp.tool_filter`, behind the risk gate |
| evolution | `create_evolution_agent` | Its own set from `evolution/tools.py` plus a few read tools; nothing that trades | None |

- The four stage agents are `LlmAgent`s with `mode="single_turn"` and no
  transfers to their parent or peers; the orchestrator calls them as
  sub-agents. The evolution agent runs on its own, from the scheduler or the
  console's **Evolve** button, never inside a trading cycle.
- The strategy and evolution agents can run on a Claude Code runtime instead
  of the API (`agent_runtime` in settings; see
  [docs/claude_code_evolution.md](../../../docs/claude_code_evolution.md)). A hosted
  strategy agent is wired into the orchestrator as a **tool**
  (`SessionSharingAgentTool`), never as a sub-agent — see the safety rules.

### Instruction system

- Instructions are files in the data folder: `instructions/<agent>/active.txt`
  names the version, `instructions/<agent>/<version>.md` holds the text. There
  are no inline fallbacks; a missing file stops start-up with a clear error.
- `agents/instructions.py` → `instructions.load(agent_name, instructions_dir)`
  reads the file, and `factory._load_instructions` fills the placeholders
  (`{{PRIMARY_TICKER}}`, `{{ALLOWED_TICKERS}}`, `{{BEARISH_VEHICLE}}`,
  `{{DIRECTIONS_AVAILABLE}}`, `{{SCHEDULE}}`) from configuration via
  `agents/instruction_context.py`. An unknown placeholder is left visible.
- The risk manager's instructions get the active constitution appended, with
  `_pct` values shown as percent strings.
- Instructions are read when the agents are built, at start-up: restart after
  activating a version.
- The evolution agent proposes new versions (`vNNN.md` plus
  `vNNN_metadata.yaml`); with `evolution.auto_promote: false` a person activates
  them in the console. The risk manager's instructions are protected, and
  instruction proposals have a weekly limit.
- A new data folder gets its instructions from `starter_data/instructions/`.

### Prompt flow

The system is tool-calling, not prompt-stuffing: agents pull what they need.

1. Each cycle starts a fresh ADK session (`run_cycle_task` in `main.py`), after
   code has reconciled pending orders and fills with the broker.
2. `main.py` sends the orchestrator a kick-off message ("Start a trading cycle.
   Check market status, then use gather_market_data …").
3. On every turn the orchestrator also gets the current time context
   (`agents/temporal_context.py`); the strategy agent gets the time context and
   the note the previous cycle left (`tools/trading_handoff.py`).
4. Agents call tools; the orchestrator hands the gathered data to each stage.
5. `main.py` reads the event stream: it records every thought, tool call and
   response (`agent_thought_log`), streams them to the console, marks the cycle
   `complete` or `partial`, records a *pipeline stall* when the strategy named
   an order that never reached the risk manager or the executor, and resumes
   the cycle once after a temporary AI-provider error if no order could have
   been placed yet (`agents/provider_retry.py`).
6. After the cycle, code reconciles again, audits protective orders and scores
   old signal records.

Continuity between cycles comes from the handoff note, semantic memory
(`store_learning`, `query_past_trades`) and the signal record — not from the
session.

### Model resolution

- `_resolve_model(config, agent_name)` in `factory.py` reads
  `settings.active_model.for_agent(name)`: the agent's own key
  (`orchestrator`, `strategy_agent`, `news_sentiment_agent`,
  `risk_manager_agent`, `executor_agent`, `evolution_agent`) or `default`. With
  separate `model.live` and `model.sim` blocks, the one for the current mode
  applies. With nothing set it falls back to `gemini-2.5-flash`.
- A model id with a provider prefix (`anthropic/…`, `openai/…`) goes through
  `LiteLlm`; Anthropic models use `CachingLiteLLMClient`, which adds prompt
  caching (the `caching:` settings). A plain Gemini id uses ADK's `Gemini`
  class with retries.
- Keys come from `.env`: `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`. `./run.sh setup` writes a model set for the provider you
  pick.

### MCP and OAuth

- Providers are declared in `mcp.providers` in `settings.yaml` and bound to
  roles in `mcp.roles` (`trading`, `research`). `create_mcp_toolsets(config)`
  builds one toolset per provider through `_create_single_mcp_toolset()`, using
  the registry in `mcp/__init__.py` → `get_mcp_provider()`
  (`robinhood_official`, `alpha_vantage`, `stock_analysis`); any other name is
  built from its settings alone. Transports: `streamable_http`, `sse`, `stdio`.
- `auth: "oauth"` (Robinhood) injects an httpx client factory from
  `mcp/oauth.py` → `create_oauth_httpx_factory()`: the MCP SDK's OAuth client
  with PKCE, so no client secret. On the first connection the terminal prints
  the sign-in link and the console shows it in a dialog (which also accepts a
  pasted redirect address when the console is used from another machine). A
  callback server on `127.0.0.1:3893` catches the redirect, and the page then
  returns the browser to the console. The connection timeout is 300 s, to
  leave time for signing in.
- `FileTokenStorage` caches tokens in `~/.evotrader/oauth/<hash of the
  server URL>/`. To force a new sign-in, use **Refresh Broker Token** in the
  console (it clears the cache) or delete `~/.evotrader/oauth/`.
- Alpha Vantage reads `ALPHA_VANTAGE_API_KEY` (several comma-separated keys are
  rotated). Research responses are trimmed by `mcp/response_curators.py`, and
  each agent's tool list is filtered by `mcp.tool_filter`.
- In practice mode, `SimBrokerProxy` (`sim/sim_proxy.py`) wraps the trading
  toolset's session: account and order calls go to the simulated broker, a
  short list of read-only market-data tools passes through to Robinhood, and
  anything else is refused.
- The mcp-debugger skill inspects the broker's tools, and its `dump` command
  writes every schema to `<data folder>/mcp_tool_schemas.json`.

### Dependency injection

Agent tools are plain functions that read module-level globals bound once at
start-up in `main.start()`:

```python
# main.py
bind_dependencies(db=db, journal=journal, metrics=metrics, algo_registry=algo_registry,
                  config=config, memory=memory, mcp_toolset=mcp_toolset,
                  sim_proxy=sim_proxy, strategy_loader=strategy_loader)
bind_evolution_dependencies(...)          # evolution/tools.py
bind_asset_context(config)                # tools/asset_context.py: "what are we trading"
```

`agents/tools.py` then uses `_db`, `_journal`, `_config`, `_memory`,
`_mcp_toolset`, `_sim_proxy` and friends. A tool called before binding finds
`None` and returns an error dict instead of raising. Tests replace the globals
(see Testing patterns).

---

## Critical safety & design rules

1. **Practice mode by default.** `mode: sim` is the code default and the starter
   setting. There is no separate dry-run switch: `settings.dry_run` is derived
   from `mode`.
2. **Fake clock, practice only.** `--mock-time` (or `EVOTRADER_MOCK_TIME`)
   forces `sim`; `--mode live` with `--mock-time` is refused when the command
   line is parsed.
3. **The constitution is only read.** `config.py` loads `constitution.yaml`
   once at start-up; no tool writes it.
4. **Every order goes proposal → risk check → gate.** The strategy agent
   proposes, the risk manager checks with `check_risk_limits`, and the
   executor places — and the executor's `before_tool_callback` (the *risk
   gate*, `callbacks/risk_gate.py` with `_GATED_TOOLS`) re-checks every order
   against the constitution, forces protective stops to good-till-cancelled,
   and, when `require_trade_approval` is on and the console is running, waits
   for a human click.
5. **Exits are never blocked by entry rules.** The risk tool and the gate share
   `callbacks/exit_policy.py` so they cannot disagree about whether an order
   reduces a position.
6. **Least privilege.** Only the executor holds order tools; the orchestrator
   holds only the two cancel tools; the strategy, risk manager and evolution
   agents hold no MCP toolset. Their code tools may read broker data, but none
   can place an order.
7. **Hosted agents return control.** A non-`LlmAgent` stage (the Claude
   Code–hosted strategy agent) must be wired as a tool
   (`SessionSharingAgentTool`). Reached through `transfer_to_agent` it would be
   a one-way handoff, and the risk manager and executor would never run. Only
   the strategy and evolution agents may be hosted; never the executor, whose
   risk gate is an ADK callback the hosted path does not have.
8. **Evolution proposes; people decide.** Code changes are only ever saved as
   reviews. New algorithm and instruction versions stay inactive while
   `auto_promote` is false. The risk manager's instructions cannot be changed.
   The only enforced rate limit is `max_instruction_changes_per_week`, and
   there is no automatic rollback.
9. **Proposed strategy code is checked, never run.** The evolution agent can
   check code with `validate_strategy_code` (`evolution/code_evolver.py`):
   it must parse, define a class with `compute_signal`, import only from an
   allow-list (math, statistics, collections, functools, numpy, pandas,
   `__future__`, typing, and the project's market models, signals, algorithm
   base, indicators and tools), and avoid `eval`, `exec`, `open`, `compile`,
   `__import__`, `globals`, `locals`, `delattr` and imports of `os`, `sys`,
   `subprocess` or `shutil`. Nothing it proposes is written to `src/`; a person
   implements it.
10. **The console stays on this computer.** It listens on `127.0.0.1` unless
    `EVOTRADER_HOST` is set, and then refuses to start without
    `DASHBOARD_PASSWORD`. It grants no cross-origin (CORS) access unless
    `EVOTRADER_CORS_ORIGINS` names origins.
11. **No ticker in code or instructions.** The instrument comes from
    configuration; `tests/unit/test_ticker_agnostic.py` enforces it.

---

## Debugging & database inspection

- **Logs.** The console prints to the terminal and streams the same lines to
  its log drawer. `./run.sh offline` writes `<data folder>/logs/cycle.log`.
  Start-up lines to check first: `Data Dir:`, the mode and `Dry Run:`, the
  active algorithm, and `Composite engine build: <fingerprint>` (which signal
  code is actually running).
- **Run History** in the console shows every cycle's thoughts, tool calls and
  responses, its status and the stages it completed, and any pipeline stall.
- **Where things live** (paths inside the data folder):

| What | Live | Practice |
|---|---|---|
| Trade journal, cycle runs, thoughts, signal record | `db/evotrader.db` | `sim/db/evotrader_sim.db` |
| Simulated broker account (`sim_accounts`, `sim_orders`, `sim_positions`, `sim_transfers`) | — | `sim/db/sim_broker.db` |
| Token telemetry | `db/telemetry.db` | `sim/db/telemetry.db` |
| Semantic memory (ChromaDB) | `memory/` | `sim/memory/` |
| Evolution log (`evolution_log`) | `db/evotrader.db` | `db/evotrader.db` — always the live file |
| Handoff note, engine-change state | `trading/notes/trading_handoff.md`, `trading/engine_provenance.json` | same |
| Instruction versions | `instructions/<agent>/`, `active.txt` | same |
| Broker sign-in | `~/.evotrader/oauth/` (outside the data folder) | same |

Main tables in the journal database: `trades`, `pending_orders`,
`daily_metrics`, `cycle_runs`, `agent_thought_log`, `market_snapshots`,
`signal_attribution`, `evolution_log`, `cash_adjustments`, `audit_log`.

### Database querier

Use `scripts/db_query.py` (or `scripts/query_db.sh`, a wrapper that runs it
with the project's `.venv` Python) instead of writing scratch scripts. It finds
the database for the chosen mode in the configured data folder.

```bash
scripts/query_db.sh --tables                # list tables (practice database is the default)
scripts/query_db.sh --schema trades         # one table's CREATE statement
scripts/query_db.sh --trades --limit 10     # shortcuts: --trades --metrics --sessions --evolution
scripts/query_db.sh --live --evolution      # evolution_log lives in the live file, even in practice mode
scripts/query_db.sh -q "SELECT id, ticker, action, fill_price FROM trades LIMIT 5"
scripts/query_db.sh -d data/sim/db/sim_broker.db -q "SELECT * FROM sim_positions"   # default data folder
```

`--live` selects the live database, `-d` any file, `--json` prints JSON. Use
`-q` for `SELECT` statements only. Semantic memory is easier to inspect in the
console (**Memory & Notes → Vector Explorer**) or with the `get_memory_stats`
tool.

---

## Testing patterns

Commands and rules are in [CONTRIBUTING.md](../../../CONTRIBUTING.md) and
[tests/README.md](../../../tests/README.md). Patterns that recur:

- `pytest` with `pytest-asyncio` in auto mode, so most tests are plain
  `async def`.
- The `db` fixture gives a fresh database with every migration applied, in
  `tmp_path`; `tmp_data_dir` gives a minimal data folder. Tests never touch a
  real journal or data folder.
- Fake the broker by patching the one function every broker call in
  `agents/tools.py` goes through:
  `patch.object(tools, "_call_mcp_tool", side_effect=...)`. Replace bound
  dependencies with `monkeypatch.setattr(tools, "_config", ...)` (or
  `_journal`, `_memory`, …). Use `unittest.mock.AsyncMock` for async
  collaborators.
- For a fix to the signal engine, turn the cycle that exposed the problem into
  a fixture that reproduces the engine's number exactly
  (`tests/unit/test_composite_live_fixture.py` is the pattern). Market prices
  in fixtures are fine; account data is not.
- Guards that keep the code honest: `test_ticker_agnostic.py` (no literal
  tickers), `test_strategy_code_validator.py` (every shipped strategy passes the
  evolution validator), and `test_signal_display_js.py` (runs the console's
  JavaScript tests under Node).

---

## Common tasks

### Add an agent tool

1. Write it as a plain function in `src/evotrader/agents/tools.py` (or a
   helper module under `src/evotrader/tools/`). ADK builds the tool's schema
   from the signature and the docstring, so give every argument a type and an
   `Args:` entry, and say what the tool returns.
2. Read dependencies from the bound globals; return a JSON-friendly dict and
   report expected failures in it (`{"error": ...}`) rather than raising.
3. Add it to the agent's `tools=[...]` in `factory.py`. For the strategy agent,
   add it to `_STRATEGY_TOOLS`, so both runtimes get it. For the evolution
   agent, add it to `create_evolution_agent` **and** to
   `evolution_tool_functions()` in `evolution/claude_code_tools.py`; the two
   lists are kept in step by hand.
4. Add its name to `local_tools` in `get_tool_metadata()` (`factory.py`) so the
   console labels its calls as local.
5. Test it, including the unbound and failure cases. Anything that can place,
   change or cancel an order does not belong in a tool outside the executor.

### Change an agent's instructions

1. Add a new version next to the active one in the data folder
   (`instructions/<agent>/v00N.md`); don't edit the version in use.
2. Keep tickers out of the text; use the placeholders.
3. Try it offline with the scenario harness
   (`uv run python -m evotrader.scenarios.runner --scenario NAME --render`,
   then `--check FILE`), following the offline-iteration skill.
4. Activate it in the console (**Memory & Notes → Agent Instructions**) or by
   writing the version into `active.txt`, then restart.
5. To change the defaults new users get, edit `starter_data/instructions/`
   instead.

### Add a trading strategy

To try a strategy without changing this package, write it in your own module
and name that module in the manifest; the backtest skill shows how. The steps
below are for adding one to the project.

1. Create `src/evotrader/algorithms/strategies/<name>.py` with a class that
   extends `TradingAlgorithm` (`algorithms/base.py`): `name`, `version`, and
   `compute_signal(snapshot) -> AlgoSignal` with a value in [−1, +1]; override
   `get_parameters`, `set_parameters` and `validate_parameters` if it has
   parameters. Parameters are constructor keyword arguments with defaults; a
   parameter added later to an existing strategy must default to the old
   behaviour, so earlier algorithm versions keep working unchanged.
2. Keep it stateless with no I/O, and import only what the evolution validator
   allows (a test checks every file in `strategies/`). When it has nothing to
   say, return `0.0` with `metadata={"applicable": False, "reason": ...}` so the
   composite shares its weight among the strategies that voted.
3. Register it in the data folder's `algorithms/strategy_manifest.yaml`
   (`module`, `class`, `description`, `status: active | experimental |
   disabled`); only `active` strategies are loaded. Add it to
   `starter_data/algorithms/strategy_manifest.yaml` too if new users should get
   it.
4. Give it weight in a new algorithm version's `config.yaml`
   (`composite.<name>_weight` **and** `composite.regime_weights.<regime>.<name>`
   in every regime: with regime weighting on, a strategy missing from that
   table counts as 0, and the loader warns), or start it at weight 0 to record
   how often it fires before it counts. The
   code defaults live in `composite.py` → `_get_default_regime_weights_map()`
   and in `StrategyLoader.build_composite()`.
5. Add unit tests (valid snapshot, missing indicators, parameter round-trip),
   then run a backtest with `--validate` as a smoke test and a practice cycle,
   following the backtest and signal-validation skills.

### Add an MCP provider

1. For a plain server, no code is needed: add it under `mcp.providers` in
   `settings.yaml` (`url` and `transport`, or `command` and `args` for stdio)
   and bind it to a role in `mcp.roles`. The generic provider in
   `mcp/__init__.py` handles it.
2. For custom behaviour (sign-in, key rotation, tool lists for the console),
   subclass `McpProvider` (`mcp/provider.py`) in `mcp/<name>.py`: implement
   `name`, `get_server_config`, `get_available_tools`, `get_read_only_tools`
   and `get_write_tools`, and register it in `_PROVIDER_REGISTRY` in
   `mcp/__init__.py`. Connection details that need code go in
   `_create_single_mcp_toolset()` in `factory.py`.
3. **Adding a broker (`trading` role) is a safety change.** The risk gate only
   checks tools named in `_GATED_TOOLS` (`callbacks/risk_gate.py`), so in live
   mode an order tool with another name would reach the broker unchecked. And
   practice mode only simulates the tool names listed in `SimBrokerProxy`
   (`sim/sim_proxy.py`); it refuses the rest. Extend both lists, with tests,
   before anything else.
4. Filter what each agent sees with `mcp.tool_filter`, and verify on a
   practice cycle.
