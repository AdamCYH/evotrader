# Tests

How the test suite is organised and how to run it. The rules for writing tests
— test first, no personal data — are in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Running the tests

```bash
uv run pytest -q                      # everything
node --test tests/js/*.test.mjs                  # only the console's JavaScript tests
uv run pytest tests/unit/test_server.py                          # one file
uv run pytest tests/unit/test_server.py -k "trigger_evolution"   # tests matching a name
uv run pytest --cov=src/evotrader   # with a coverage report
```

`uv run pytest` also runs the JavaScript tests, through
`tests/unit/test_signal_display_js.py`, when Node.js is installed; without Node
that one test is skipped. No test calls an AI provider or a broker.

## Layout

```text
tests/
├── conftest.py      shared fixtures (below)
├── unit/            the Python tests, one module per behaviour or per failure it guards
├── js/              node:test tests for the console's display rules
├── test_mcp_models.py
└── test_sim_broker_portfolio.py
```

Many modules are named after the problem they prevent from coming back
(`test_protective_tif.py`, `test_risk_gate_exit_path.py`,
`test_deadweight_renormalization.py`); the module docstring explains the
failure in plain words.

## Shared fixtures (`tests/conftest.py`)

- **No test reads your data folder.** An automatic fixture points
  `EVOTRADER_DATA_DIR` at the test's own copy of `starter_data/` (made the
  way `scripts/init_data.py` makes a new user's folder), so `AppConfig()`,
  `paths.data_dir()` and everything built on them see the starter settings,
  instructions, algorithm and scenarios, whether or not a `data/` folder
  exists. The suite passes on a fresh clone with no data folder.
- **`starter_data_dir`** — that copy's path, for a test that reads the files
  itself. Change them freely; the next test gets a fresh copy.
- **`update_data_yaml`** — changes a few values in that copy's
  `settings.yaml` or `constitution.yaml` and keeps the rest, for a test that
  depends on particular values: it states them rather than relying on
  whatever a data folder holds.
- **`active_algorithm_config`** — the parameters of the starter's active
  algorithm version.
- **`db`** (async) — a fresh SQLite database in the test's temporary folder,
  with every migration from `src/evotrader/db/migrations/` applied. It never
  touches a real journal.
- **`tmp_data_dir`** — a temporary data folder holding a minimal
  `constitution.yaml` and `settings.yaml` (`mode: sim`).
- **`constitution`**, **`settings`** — the configuration models with their
  default values.
- **`sample_quote`**, **`sample_indicators`**, **`sample_regime`**,
  **`sample_snapshot`** — a complete market snapshot for an index ETF at a
  fixed time, for strategy and indicator tests.
- An automatic fixture resets the process-wide "what are we trading" bindings
  between tests, so one test's start-up cannot change another's answer.

## Where the app keeps its databases

Tests use temporary folders. The running app uses the data folder (`data/` in
the project unless `EVOTRADER_DATA_DIR` or `--data-dir` says otherwise):

| Mode | Trade journal and cycle history | Memory | Orders go to |
|---|---|---|---|
| Tests | `tmp_path/test.db` | none | nowhere |
| Practice (`--mode sim`) | `sim/db/evotrader_sim.db` | `sim/memory/` | the simulated broker (`sim/db/sim_broker.db`) |
| Live (`--mode live`) | `db/evotrader.db` | `memory/` | Robinhood |

In both modes every order first passes the risk gate, and the approval gate
when `require_trade_approval` is on and the console is running.

Evolution records are always written to `db/evotrader.db`, so practice and
live runs share one evolution history.

## Good habits

1. **Async tests.** The code is built on `asyncio` and `aiosqlite`, so most
   tests are `async def`; `pytest-asyncio` runs them automatically
   (`asyncio_mode = "auto"` in `pyproject.toml`).
   ```python
   async def test_my_query(db):
       async with db.connection() as conn:
           ...
   ```
2. **Time.** Code that depends on the time of day in New York takes a `now`
   argument or honours `EVOTRADER_MOCK_TIME`; set one of them rather than
   depending on when the test runs.
3. **Lint and format** before pushing:
   ```bash
   uv run ruff check src tests
   uv run ruff format src tests
   ```
