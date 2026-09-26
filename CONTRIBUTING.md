# Contributing to EvoTrader

Thanks for helping. Bug reports, fixes, tests, documentation and ideas are all
welcome. This page covers how to set up, the checks a change must pass, and how
the versioned instructions and algorithms work.

Please read the [Code of Conduct](CODE_OF_CONDUCT.md). To report a security
problem, don't open an issue — follow [SECURITY.md](SECURITY.md).

## The one rule that protects you

**Never put live data, databases, `.env` files or screenshots of your account
in an issue or pull request.** That includes trade journals, account numbers,
order ids, balances, positions, API keys, OAuth tokens and console screenshots
that show any of them. If a bug only shows up with your data, describe it in
words and reproduce it with made-up numbers in a test. Market prices of a
public ticker are fine; anything about your account is not.

## Set up

You need macOS or Linux, [uv](https://docs.astral.sh/uv/getting-started/installation/),
and optionally Node.js 18+ for the JavaScript tests.

```bash
git clone https://github.com/AdamCYH/evotrader.git
cd evotrader
uv sync --all-extras          # Python 3.12+, runtime and dev dependencies
```

Use `scripts/setup.sh` instead if you also want a working data folder and keys
to run the app (see the [README](README.md)). Most code changes need neither:
the tests don't call any AI provider or broker.

### Working against a private data folder

If you trade with EvoTrader yourself, keep your data folder outside the
checkout, in its own private repository, and point the app at it:

```bash
export EVOTRADER_DATA_DIR=~/evotrader-data   # or: ./run.sh --data-dir ~/evotrader-data
```

Then code changes happen here, in branches and pull requests, and changes to
your settings, instructions and algorithm versions are commits in your data
repository. Nothing private can end up in a pull request because it was never
in this checkout. Code should reach the data folder only through
`evotrader.paths.data_dir()`, never through a literal `data/...` path, so
that this one setting moves everything.

## Checks every change must pass

```bash
uv run ruff check src tests            # lint
uv run ruff format --check src tests   # formatting (run without --check to fix)
uv run pytest -q                       # Python tests
node --test tests/js/*.test.mjs                   # JavaScript tests for the console
```

`uv run pytest -q` also runs the JavaScript tests through
`tests/unit/test_signal_display_js.py` when Node is installed. Continuous
integration runs all four on every pull request. Type checking with mypy is
configured but not yet enforced; fixing a module so it passes
`uv run mypy src/evotrader/<module>.py` is a welcome contribution.

## How we fix bugs: test first

1. **Write a test that fails** because of the bug, and watch it fail. A test
   that passes before the fix proves nothing — this has happened here, with a
   guard that passed against the broken code for the wrong reason.
2. Fix the code.
3. Run the whole suite, not just your test.
4. If the change touches the order path (agents' order tools, the risk gate,
   the executor, reconciliation, the simulated broker), **run a practice
   cycle** before asking for review: `./run.sh --mode sim`, press **Trade**,
   and read the cycle in Run History. `./run.sh --mock-time` lets you do this
   outside market hours.

Test names describe the behaviour or the failure they guard against
(`test_risk_gate_exit_path.py`, `test_protective_tif.py`). Tests must not
depend on anyone's personal data folder: build what you need in `tmp_path`
(the `tmp_data_dir` fixture in `tests/conftest.py` gives you a minimal one) or
copy it from `starter_data/`. Market prices inside a test fixture are fine;
account balances, order ids and profits are not.

## Code style

- Ruff settles formatting and most style questions; the rule set is in
  `pyproject.toml`, each ignored rule with its reason.
- Comments explain **why**, in plain language, for a reader who is not a
  finance expert. Say what a term means the first time. A rule that exists
  because something went wrong is worth a sentence about the failure it
  prevents ("a stop placed as a day order expired at the close and left the
  position unprotected overnight") — without dates, trade ids, amounts or
  anything else from a real account.
- No ticker symbol in code or instructions that changes behaviour; the traded
  instrument comes from configuration (`tests/unit/test_ticker_agnostic.py`
  checks this).
- Keep behaviour changes and cleanups in separate pull requests.

## How instruction versions work

Each agent's system instructions are Markdown files in the data folder, one
folder per agent:

```text
instructions/strategy/
├── active.txt              # contains "v002": the version in use
├── v001.md
├── v002.md
└── v002_metadata.yaml      # who proposed it, why, when, status
```

- A change is a **new version**, never an edit to one in use, so every past
  prompt stays readable and you can switch back. The evolution agent writes
  new versions this way; so can you. Activate one in the console
  (**Memory & Notes → Agent Instructions**, which shows a diff) or by editing
  `active.txt`, then restart.
- Instructions never name a ticker. They use placeholders filled in from
  configuration: `{{PRIMARY_TICKER}}`, `{{ALLOWED_TICKERS}}`,
  `{{BEARISH_VEHICLE}}`, `{{DIRECTIONS_AVAILABLE}}` and `{{SCHEDULE}}`
  (`src/evotrader/agents/instruction_context.py`).
- The risk manager's instructions are protected: the evolution agent cannot
  propose or activate versions for it.
- The defaults for new users are in `starter_data/instructions/`. Improvements
  to them are welcome as pull requests; keep them short and generic.
- To try an instruction change without spending money on live cycles, use the
  scenario harness: `uv run python -m evotrader.scenarios.runner` lists the
  scenarios, `--scenario NAME --render` prints the exact prompt, and
  `--check FILE` scores a saved response. The
  [offline-iteration](skills/offline-iteration/SKILL.md) skill describes the
  discipline that keeps such a search honest.

## How algorithm versions work

The rule-based part of the system is code plus versioned parameters:

- **Strategy code** lives in `src/evotrader/algorithms/strategies/`. Each
  strategy extends `TradingAlgorithm` (`algorithms/base.py`), is stateless,
  does no I/O, returns a vote between −1 and +1, and marks itself not
  applicable when it has nothing to say (the composite then shares its weight
  among the strategies that voted).
- **The manifest** (`algorithms/strategy_manifest.yaml` in the data folder)
  lists the strategies the loader may use and their status (`active`,
  `disabled`, `experimental`).
- **A version** is a folder such as `algorithms/v002_wider_rsi/` holding
  `config.yaml` (each strategy's parameters, and the composite's weights per
  market regime) and `metadata.yaml`. `registry.yaml` lists every version and
  `active.yaml` names the one in use. Versions are activated in the console
  (**Memory & Notes → Algorithm Proposals**).
- **A new parameter** gets a default in code that reproduces the old behaviour
  exactly (an "identity" default), and its real value goes into a new
  version's `config.yaml`. Old versions then keep behaving as they did.
- **Backtests are smoke tests, not scorecards.**
  `uv run python -m evotrader.backtest.runner --compare vA,vB` replays the
  algorithm alone on past prices and catches a broken engine, a silenced
  strategy or a flipped sign. It leaves out the agents, the news and real
  fills, so don't choose a parameter because it moved the backtest number;
  read the report's participation table and the
  [signal-validation](skills/signal-validation/SKILL.md) skill first.

## Evolution proposals and reviews

The evolution agent writes strategy proposals to `evolution/proposals/` and
code reviews to `evolution/reviews/` in the data folder
([examples](docs/examples/)). Implementing one follows the
[self-evolve](skills/self-evolve/SKILL.md) skill. **Never edit the `Status`
field of a proposal or review by hand**: the console owns it and keeps the
database in step. Mark it applied or rejected in the console instead.

## Pull requests

- One topic per pull request, with a short description of what changed and
  why. The [template](.github/PULL_REQUEST_TEMPLATE.md) has the checklist.
- Update the docs and [CHANGELOG.md](CHANGELOG.md) when behaviour or setup
  changes.
- Contributions are accepted under the project's
  [Apache License 2.0](LICENSE) (section 5 of the license).

Contributors who use Claude Code will find the project conventions in
[CLAUDE.md](CLAUDE.md).
