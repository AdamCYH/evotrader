# AGENTS.md

Guidance for coding agents (Claude Code, Codex, Copilot, Cursor, Gemini CLI…)
working in this repository. Claude Code reaches it through CLAUDE.md.
Human contributors: the same rules are in [CONTRIBUTING.md](CONTRIBUTING.md).

## What this is

EvoTrader is a multi-agent trading system (Google ADK) that trades one
instrument through Robinhood's MCP server inside hard limits, plus a weekly
evolution agent that proposes improvements. The code is public; each user's
settings, instructions, algorithm versions, journal and keys live in a separate
**data folder**. Start with [docs/architecture.md](docs/architecture.md).

## Commands

```bash
uv sync --all-extras                   # install
uv run pytest -q                       # tests (also runs the JS tests if node exists)
node --test tests/js/*.test.mjs                   # console JavaScript tests
uv run ruff check src tests            # lint
uv run ruff format src tests           # format
./run.sh --mode sim                    # console in practice mode, http://127.0.0.1:8080
./run.sh --mode sim --mock-time        # practice mode at a fixed weekday morning
uv run python -m evotrader.backtest.runner --validate # algorithm-only backtest, judged (--help)
uv run python -m evotrader.scenarios.runner           # instruction scenarios
```

## Rules

1. **No live trades from a coding session.** Never run `./run.sh --mode live`
   or `./run.sh offline` against a live configuration, never set `mode: live`,
   and never call a broker order tool (place, review or cancel an order —
   including through the mcp-debugger skill or a one-off script). Test with unit
   tests and practice cycles only, unless the user explicitly asks otherwise.
2. **Verify on a practice cycle before live.** A change that touches the order
   path — agent order tools, the risk gate (`callbacks/`), the executor,
   reconciliation, the simulated broker — is not done until a practice cycle
   (`./run.sh --mode sim`, then **Trade**) has run through it cleanly. Unit tests
   verify code; they do not verify the integration.
3. **Write a failing test before the fix.** Reproduce the bug in a test, see it
   fail, then fix it, then run the whole suite. A guard that passes before the
   fix proves nothing.
4. **Use the data folder only through `evotrader.paths.data_dir()`.** Never
   build a literal `data/...` path. The user's data folder may be anywhere
   (`EVOTRADER_DATA_DIR`, `--data-dir`). Tests must not read or write the
   user's data folder: build fixtures in `tmp_path` (see `tmp_data_dir` in
   `tests/conftest.py`) or copy from `starter_data/`.
5. **Never edit the `Status` field** of an evolution proposal
   (`evolution/proposals/*.md`) or code review (`evolution/reviews/*.md`). The
   web console owns it and keeps the database in step; the user marks items
   applied or rejected there.
6. **Keep account data out of the repository.** No `.env`, databases, logs,
   OAuth tokens (`~/.evotrader/oauth/`), account numbers, order ids,
   balances, positions or profits — not in code, comments, tests, fixtures,
   docs or commit messages. Market prices of a public ticker in a test fixture
   are fine.
7. **The constitution is the user's.** Don't change `constitution.yaml` or the
   risk manager's instructions unless the user asks for that specific change.
8. **Comments explain why, in plain language.** The reader may not be a
   finance expert: define a term the first time it appears. Describe the
   failure a rule prevents in general terms ("a day stop expired at the close
   and left the position unprotected overnight"), without dates, trade ids or
   amounts.
9. **Name no ticker** in code or instructions that changes behaviour. The
   instrument comes from configuration; instructions use placeholders such as
   `{{PRIMARY_TICKER}}` and `{{BEARISH_VEHICLE}}`
   (`agents/instruction_context.py`). `tests/unit/test_ticker_agnostic.py`
   enforces this.
10. **Exits are never blocked by entry rules.** Anything that can refuse an
    order must let a position be closed or protected. Read
    `callbacks/exit_policy.py` before touching a risk check.

## Conventions worth knowing

- **Percent units.** `_pct` fields in `constitution.yaml` are written in
  percent (`5` means 5%) and converted to fractions on load
  (`models/config.py`); in `settings.yaml`, check each field's description
  before assuming its unit. `_pct` fields in market snapshots and algorithm
  configs are percent (`1.2` means 1.2%). For a new threshold on a price move,
  prefer a multiple of the ATR (average true range, the typical daily move) over
  a percent, so it means the same thing on any instrument.
- **Versions, not edits.** Agent instructions (`instructions/<agent>/vNNN.md`
  plus `active.txt`) and algorithm parameters (`algorithms/<version>/config.yaml`
  plus `active.yaml`) change by adding a new version. A new algorithm parameter
  gets a default in code that reproduces the old behaviour; its real value goes
  in the new version's `config.yaml`.
- **Backtests are smoke tests.** Use them to catch a broken engine or a
  silenced strategy, never to pick a parameter value. The scored live record
  (`signal_attribution`) is what calibrates.
- **Settings are read at start-up.** Restart the app after changing
  `settings.yaml`, `constitution.yaml` or an active instruction version.
- Keep behaviour changes and cleanups in separate commits.

## Skills

All in `.agents/skills/<name>/SKILL.md`, the shared folder of the Agent Skills
standard (read by GitHub Copilot, Cursor, OpenCode and others); `.claude/skills`
links to it for Claude Code. In them,
`data/...` means the data folder, wherever `EVOTRADER_DATA_DIR` puts it.

- **`evotrader-developer`**: how the code fits together, the safety rules it
  enforces, how to debug, test and extend it.
- **`self-evolve`**: implement an evolution proposal or code review
  from the data folder's `evolution/` — read the note and the code it cites,
  test first, verify the algorithm still loads, report the backtest delta with
  its noise band, and record follow-ups in `evolution/notes/carry_forward.md`.
- **`backtest`**: test a strategy idea, version, parameter change or
  instrument on past prices with `--validate`; write a strategy outside the
  package; read the verdict before the return.
- **`signal-validation`**: the checks to run
  before believing that a signal, instruction or strategy change works.
- **`offline-iteration`**: tune instructions or
  parameters with scenario tests and backtests, without false positives.
- **`mcp-debugger`**: list and inspect MCP tools and their schemas, and call
  read-only ones. It refuses order tools (rule 1) and refuses to run while the
  app runs (they would share the broker sign-in).

To start over with a clean slate, point the app at a new data folder
(`./run.sh setup --data-dir PATH`) rather than emptying the old one.
