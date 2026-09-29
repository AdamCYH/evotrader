# Changelog

Notable changes to EvoTrader are recorded here, newest first. While the
version starts with 0, any release may change behaviour, settings or the data
folder layout; each entry says what you need to do.

## Unreleased

### Changed

- **Thresholds on the size of a price move can be set in the instrument's own
  units.** Five thresholds were in percent of price, which silently tunes them
  to one instrument: 0.7% is two thirds of a normal day on SPY and a tenth of
  one on a volatile single stock, where the gate then fires on noise. Each now
  has an optional form in daily ATRs (average true range, the typical size of
  a day's move): `gap.min_gap_atr` and `gap_fade_threshold_atr`,
  `range_break_continuation.min_break_atr`, `trend_persistence.counter_day_atr`
  and `event_window_timing.min_overextension_atr`. All five use one shared rule
  (`algorithms/units.py`), and each strategy records which basis decided.
- **The starter algorithm (`v001_initial`) uses the ATR forms**, set to match
  the old percent values on SPY, so on SPY it behaves as before and on another
  ticker the thresholds scale with it. **If you run your own algorithm
  version, nothing changes:** the ATR forms default to off, and a version that
  does not set them keeps its percent thresholds. To opt in, add them to that
  version's `config.yaml`.

### Fixed

- **The daily position sync no longer rewrites a lot's price to the
  position's average.** After a buy at a different price, each lot differs
  from the broker's average cost by construction, and the sync rewrote such
  lots, broker-confirmed prices included, on every run: each lot's unrealized
  P&L moved, and the difference would have been booked as realized P&L when
  the lot closed. A lot is now checked only against its own order's fill
  price. The lots' average is still compared with the broker's, but a
  disagreement is only reported (`cost_basis_check.sources_disagree` on the
  sync result), never written. A pending buy the sync marks filled takes its
  own order's price too. Lots the old check rewrote are put back from their
  orders on the next sync.
- **A cancelled good-till-cancelled order is labelled cancelled, not
  "expired".** The label was decided by when an order was placed, so a gtc
  stop cancelled to resize it days later read "Day order expired ... (no
  time_in_force)" — the message that once led an agent to sell a position
  instead of re-placing its stop. A day order now reads expired only when the
  broker dropped it at or after its session's close. Wrong labels already
  written stay until corrected by hand.
- **Rows a data repair closed out keep the repair's label** when later news
  arrives for the broker order they share.
- **A stop resized to cover a buy placed in the same cycle** is no longer
  flagged OVER_COVER while that buy awaits the broker's confirmation.

## 0.2.0 — first public release — 2026-09-26

The first public version. Development before this happened in a private
repository, and that history is not included.

The project was called **Gold Digger** in private development; it was renamed
EvoTrader for this release. If you ran a private copy: the package is now
`evotrader` (was `gold_digger`), environment variables start `EVOTRADER_`
(were `GOLD_DIGGER_`), the databases are `evotrader*.db` (were
`gold_digger*.db`), the broker sign-in cache is `~/.evotrader/` (was
`~/.gold_digger/`), and strategy module paths in `algorithms/*.yaml` start
`evotrader.`. Rename those once, with the app stopped.

### What's in it

- **Trading cycle.** A team of agents built on Google's Agent Development Kit:
  an orchestrator, a news agent, a strategy agent, a risk manager and an
  executor. Each agent's model is set in `settings.yaml`; Google Gemini is the
  default, Anthropic Claude is supported, OpenAI is experimental.
- **Algorithm.** A composite engine that combines ten rule-based strategies with
  weights that depend on the market regime, lets a strategy abstain without
  diluting the others, and keeps its parameters in numbered algorithm versions.
- **Hard limits.** A constitution the agents cannot change, enforced by the
  risk manager's checks and by a risk gate in code on every order; protective
  stop orders are forced to good-till-cancelled; exits are never blocked by
  rules meant for new positions.
- **Approval gate.** Optionally, every order waits for your approval in the
  web console.
- **Practice mode**, the default: a simulated broker that fills orders against
  live Robinhood quotes, with its own journal and memory.
- **Robinhood** through its official MCP server, with sign-in on Robinhood's
  own page; the app never sees your password.
- **Signal attribution.** Every cycle records what the algorithm, the news and
  the strategy agent each said, and scores them against the price one and five
  trading days later.
- **Evolution agent**, on a schedule you set. It writes strategy proposals, code
  reviews, carry-forward notes, and new algorithm and instruction versions that
  stay inactive until you approve them. It can run on the API or on a Claude
  subscription through Claude Code, as can the strategy agent.
- **Skills for coding agents.** `self-evolve` for implementing approved
  proposals with tests, plus `signal-validation`, `offline-iteration`,
  `mcp-debugger` and `fresh-start`.
- **Web console.** Dashboard, trade approval, the reasoning of every cycle,
  token usage, review of proposals and versions, and your notes to the agents.
- **Your own data folder**, separate from the code (`EVOTRADER_DATA_DIR` or
  `--data-dir`), created from `starter_data/` by the first-run setup
  (`scripts/setup.sh`, `./run.sh setup`).
- **Offline tools.** An algorithm-only backtester and a scenario harness for
  testing agent instructions.
- **Cost controls.** Prompt caching for Anthropic models, trimming of oversized
  data-provider responses, and per-agent model choice.
- Apache License 2.0, continuous integration, and contributor documentation.

### Fixed before release

Fixes from trying the first public version the way a newcomer would.

### Changed — check these if you set things up by hand

- **Your shell now wins over `.env` files.** A variable already set in the
  environment beats the same variable in a `.env` (the data folder's `.env`
  still beats the project's), and an empty value (`KEY=`) means "not set".
  Before, the data folder's `.env` won, so `EVOTRADER_PORT=8081 ./run.sh` was
  ignored when the file named a port, and an empty `EVOTRADER_MOCK_TIME=`
  cancelled `--mock-time`. The app warns when the two differ.
- **An option the app doesn't know stops it** with an error. It used to be
  ignored, so a typo such as `--mock_time` ran in the mode the settings say.

### Backtesting

- **`--validate`** judges a backtest instead of just scoring it: the
  statistical checks in `backtest/validation.py`, a placebo (the version's own
  signal shifted in time so it predicts nothing, replayed 40 times) and a
  positive control (a signal that cheats by reading the next bar, which must
  win, or the harness cannot see an edge on that data). The report gains a
  Validation table and a verdict; exit code 3 means the run worked and nothing
  survived. `--variants-tested N` raises the bar for the number of things tried.
- **`--daily-csv`** joins `--intraday-csv`: with both, a backtest needs no
  network and works for any instrument you have bars for.
- **A strategy can live outside the package**: name its module in the
  manifest and put it on `PYTHONPATH`. The new `backtest` skill walks an agent
  through testing an idea end to end.
- **The loader warns when a strategy has a weight but no entry in
  `composite.regime_weights`.** With regime weighting on, such a strategy
  counted as 0 in every regime: it ran and looked active, but never moved a
  trade.
- The out-of-sample check no longer fails when trade times and the split date
  differ in having a time zone (it raised an error on a run with no trades).

### Setup

- **Easier to read.** Each part of `./run.sh setup` has a heading, choices and
  defaults stand out, a key that works gets a green tick, and long
  explanations are broken at the terminal's width (a command stays on one
  line, to copy). No colour in a pipe or a log, or when `NO_COLOR` is set.
  `scripts/setup.sh` colours its checks too.
- **The Claude subscription is mentioned where the API key is asked for.**
  API keys are paid per use; the strategy and evolution agents, which cost
  the most, can run on a Claude Pro or Max subscription instead. Easy mode now
  says so and sets nothing up: to switch later, run `./run.sh setup` again and
  choose Detailed. Detailed mode says the same when Claude Code isn't
  installed (with it installed, it asks).

### Skills

- All skills now live in `.agents/skills/`, the shared folder of the Agent
  Skills standard (GitHub Copilot, Cursor, OpenCode and others read it), with
  `.claude/skills` a link to it for Claude Code; the `skills/` folder and its
  duplicate of `self-evolve` are gone.
- The instructions for coding agents are in `AGENTS.md`, the cross-tool
  standard; `CLAUDE.md` imports it for Claude Code.
- `fresh-start` is removed: it deleted journals, memory and the broker
  sign-in. To start over, point the app at a new data folder
  (`./run.sh setup --data-dir PATH`).
- The MCP debugger refuses tools that place, change or cancel orders (they go
  through the app's risk checks and approval), and refuses to run while the app
  runs (they would share the broker sign-in). `scripts/discover_mcp_tools.py`
  is now its `dump` command; two stale helper scripts are removed.

### Fixed

- Ctrl+C stops the app within seconds, even with a console tab open; it used
  to hang until killed.
- `./run.sh setup --data-dir PATH` sets up that folder (it set up `./data`),
  and a relative folder is found where you typed it, not inside the project.
- Quitting setup takes away the data folder it had just made, so the app no
  longer starts from a half-made folder with no password and no key. The app
  now also warns at start-up when the console password or an AI key is missing.
- Running setup again starts from your current choices (easy or detailed, the
  provider, each agent's model, the port): pressing Enter throughout changes
  nothing.
- Setup checks a new AI key with its provider (a free, read-only request), so
  a mistyped key shows during setup instead of at the first trading cycle.
- Setup recognises a Gemini key saved as `GOOGLE_API_KEY`, queries model names
  such as `gpt-5` that lack a provider in front, and asks for at least $1 of
  play money.
- The Robinhood sign-in files are readable only by you, and no part of the
  token is printed any more. The tests never touch your real `~/.evotrader`.
- A console port already in use gets a clear message; `--sim-deposit` checks
  the amount; `./run.sh cli-dashboard` works again; the console's sign-in window
  can be closed; start-up and shutdown print far less noise.
- Only one EvoTrader runs on a data folder, and on a broker sign-in: a second
  copy stops with a message instead of trading the same account.
- A second Ctrl+C no longer hangs while a broker sign-in is pending, and a
  third stops the app at once.
- `./run.sh offline` shows the Robinhood sign-in link (it was held back in a
  buffer, so a first sign-in from offline mode was impossible).
- After **Refresh Broker Token** in the console, the next sign-in is saved
  again; it failed until a restart. A sign-in that times out prints one line,
  not a hundred.
- Without a console password, the console no longer waits minutes on
  "CONNECTING", and its portfolio doesn't wait on a pending sign-in.
- Setup keeps practice mode's own models (`model: sim`) unless you say
  otherwise, says truthfully which agents stay on a Claude subscription and
  offers to move them, uses a key already exported in your shell, and doesn't
  treat a hand-made `.env` as a detailed setup.
- Unreadable `settings.yaml` or `--mock-time` values, and mistyped options, get
  a plain message with a suggestion instead of a traceback. The backtester
  replays your data folder's algorithm version (it named a private one), and
  advice from setup, the app and the tools names your data folder.
