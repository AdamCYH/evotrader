# Changelog

Notable changes to EvoTrader are recorded here, newest first. While the
version starts with 0, any release may change behaviour, settings or the data
folder layout; each entry says what you need to do.

## Unreleased

### Added — in the console

- **The combined signal on the account chart.** A **Signal** button on the
  Account / P&L chart splits it into two panes on the same dates: the curve on
  top, each day lightly tinted green or red by the way the day's average signal
  leaned (stronger days darker), and below it the day's average as a bar with a
  thin line for its range. Hovering a day shows the value, its change from the
  day before, the signal's average and lean, its range, how many readings it
  had and the instrument. A strip under the chart says how often a
  long-leaning day was followed by a gain by the next day the system ran, how
  often a short-leaning day was followed by a loss, and the correlation
  between the two, with the number of days; over a few weeks that describes the
  period, it is not evidence of an edge. The choice is remembered in the
  browser. The daily figures come from a new endpoint,
  `/api/chart/signal-daily?period=…`, one row per US Eastern date, the same
  calendar the equity curve uses.
- **The account chart is easier to read with or without the overlay:** a
  smoother line over a soft fill, points only on hover and on deposit days,
  short date labels, round price levels, and one tooltip for the whole day
  with a guide line through it.

### Added — in what the agents read

- **Protective coverage, computed.** `get_open_positions` returns
  `protective_coverage`: per ticker, the shares held (every open lot summed)
  against the shares under resting stop orders, with `uncovered_qty` and a
  `status` of `full`, `unknown` (covered on quantity, but a stop lacks its
  trigger price or time in force), `partial`, `none` or `flat`. A take-profit
  is listed but not counted as cover. The same block is in the
  `gather_market_data` snapshot, in `reconcile_pending_orders` (whose
  `order_book.orders` now lists each working order's quantity and levels), and
  in `record_trade`'s response as `coverage_after`, so the executor sees a
  shortfall in the same call that created it. Until now the agents worked this
  out by hand, and shares added to a position could run without a stop until a
  later cycle redid the arithmetic. Each journal row also keeps the figure from
  the moment it was recorded, in a new `coverage_at_record` column written by
  the code, never by an agent; the agent's reasoning text is not edited. The
  column is added automatically at the next start (migration 0026).

### Added — off unless an algorithm version sets them

- **A `gap_fail_continuation` channel, in shadow.** The `gap` channel reads
  0.0 the moment an opening gap has filled, so nothing voted on what a failed
  gap does next. The new channel votes in the direction of the fade once price
  is through the prior close, past session VWAP and has held there for the
  last few 5-minute bars (bearish after a failed gap-up, bullish after a failed
  gap-down), from an hour into the session, fading to nothing over the last
  half hour. It ships at weight 0 in every regime of the starter algorithm, so
  it runs and its firings are recorded but it does not move the combined
  signal until promoted on that record. A version whose config predates a
  shadow channel runs it at weight 0 too.
- **`momentum.divergence_window_days` and `divergence_cum_atr`.** The
  divergence guard halves the momentum vote when the market moves hard
  against it, but it looked at one session, so a decline made of small days
  never tripped it. With both set, the move from the close N sessions ago is
  tested too, in daily ATRs, and either test applies the same single decay.
  The anchor session is found by date, so the span is the same whether or not
  the daily list includes today. The vote's metadata says which test fired
  (`divergence_basis`) and from which session (`divergence_window_anchor`).
- **`trend_persistence.counter_stack_scale` and `no_stack_scale`.** A grind
  fired only when the moving averages were stacked the same way, which after a
  long advance silences the channel's opposite side for weeks. With both set,
  the stack scales the vote (1.0 with it, the counter scale against it, the
  no-stack scale otherwise) instead of vetoing it; `leg` records which.

### Fixed

- **The protection audit compares whole positions, not lots.** After every
  cycle the audit checks that each held position is covered by a resting stop.
  It compared each purchase lot on its own with the ticker's whole stop, so a
  position bought in two lots under a stop sized for the first passed: each lot
  alone fit. It now sums the lots per ticker, leaves option lots out
  (contracts, not shares), and names every lot it summed (`trade_ids`).
- **Shadow channels no longer move the combined signal.** The combined signal
  is scaled down when few channels corroborate it: below 0.4 of the channels on
  duty voting, it is multiplied by that share divided by 0.4. The count
  included channels at weight 0, which is how a new channel runs before it is
  promoted, so a silent shadow channel counted as an absent witness and a
  voting one as a corroborating witness. The combined signal then moved from
  one hour to the next with every weighted input unchanged: with the starter
  algorithm's three shadow channels in scope during regular hours, two
  weighted votes read 2 of 8 and were scaled by 0.625. Only channels with
  weight in the current regime are counted now. **Regular-hours readings are
  higher than before** wherever a weight-0 channel was in scope (the scale it
  caused no longer applies), so compare readings across this change with care.
- **A channel still collecting its session bars counts as on duty.** A channel
  that needs 20 five-minute bars has 12 at 10:30 ET; it was left out of the
  count until it had them, so the same votes were scaled differently at 10:30
  and 11:30. A channel that will have its bars before the close now reports
  `warming_up` and is counted (`swing_failure_reversal`,
  `vwap_reclaim_continuation`); one that cannot have them, such as 20 one-hour
  bars in a 6.5-hour session, stays out as before.
- **The scale can be checked from the cycle's own record.** The market-data
  payload reports `participation_numerator` and `participation_denominator`
  next to `participation_scale`, and the combined signal's metadata names the
  channels warming up (`warming_up_signals`).
- **The engine fingerprint covers `algorithms/units.py`,** the move-size rules
  six strategies share. A change to it would have changed readings without the
  engine-change notice.
- **The attribution record carries the participation scale and its counts.**
  `signal_attribution` has had `composite_unattenuated` and
  `participation_scale` columns since migration 0024, but only the strategy
  agent wrote the row and it was never asked for them, so they stayed empty.
  After each cycle they are now filled, with `participation_numerator` and
  `participation_denominator` (migration 0025), from the market-data response
  the cycle keeps in its log. Past rows are filled the same way on the first
  cycle after the update, where the response has them.
- **The option chain reaches the money.** The broker lists an expiry's contracts
  from the lowest strike up, a page at a time, and `gather_option_chain` read the
  next-page link from one place only and gave up after five pages. When an
  underlying has a long run of strikes below its price, the near-the-money
  contracts fall past what was fetched, and the nearest-strike selection then
  mapped every offset onto the one highest strike it had: the strategy agent saw
  a chain of one strike far below the price, cycle after cycle, and concluded
  options were unavailable. The link is now found wherever the server puts it
  (beside the list, at the top level, or as a bare cursor), pages are followed
  until strikes on both sides of the money are in hand (40 at most), and the
  response says in `errors` when the list stops short: a page that could not be
  fetched, a single strike, or no strike within 8% of the price. It also reports
  what was fetched (`strikes_fetched`: pages, whether more remained, the lowest
  and highest strike).
- **Practice mode passes read-only market data through to the broker.** A
  practice cycle on 2026-10-02 was refused `get_earnings_calendar`,
  `get_equity_price_book`, `get_option_historicals` and `get_equity_tax_lots`
  ("not supported in simulation mode"), so the earnings event context, the
  order-book depth check and the IV trend ran blind in practice mode while they
  work live. Every read-only tool that describes the market rather than an
  account now passes through: quotes, bars, order-book depth and indicators;
  option chains, contracts and their quotes and bars; fundamentals, financials,
  earnings dates and results; indexes; instrument search. Tools that read the
  real account are still answered by the simulated broker or refused, never
  passed through: `get_equity_tax_lots` now answers with the practice account's
  own position as one open lot (it records no closed lots), so the wash-sale
  check runs instead of failing. The tradability check's account swap read the
  broker's account list in the wrong shape and so sent the practice account's
  name to the real broker; it now finds the real account as the live path does.
  From the same audit of the option path: a practice option position is priced
  per contract with its multiplier, as the broker prices one (the position sync
  read it at a hundredth of its cost), and a close placed after the agents'
  cache had lost the contract's ticker closes the held position instead of
  opening a second one under "OPTION".
- **The constitution's account limits are enforced in code.** `max_weekly_loss_pct`
  and `max_drawdown_pct` had no enforcing code, and the daily-loss limit, the
  loss-streak pause and the daily order count were checked only inside a tool
  the risk-manager agent is asked to call. All of them are now one verdict
  (`callbacks/account_rails.py`), applied by `check_risk_limits`,
  `check_option_risk_limits` and the pre-order gate in front of the executor's
  order tools, which refuses a new entry past any of them. The drawdown limit
  measures the account against the highest value the metrics job has recorded.
  Exits and protective orders are never blocked. `get_open_positions` reports
  the account's standing against each limit (`account_rails`) every cycle,
  from the journal and the last recorded account value, so a halt is visible
  before an order is tried. A limit that cannot be measured
  (no account value, no recorded peak) is reported as not evaluated.
- **The daily indicators include yesterday's close.** The broker's daily bars
  end at the previous session all day long, and the live price was written
  over that last bar, so every daily reading (moving averages, MACD, RSI,
  Bollinger bands, ATR, IBS, the regime and its ADX) ran without the most
  recent completed session, and ATR stayed frozen whenever the day traded
  inside yesterday's range. Today's bar is now added after yesterday's
  instead: built from the session's 5-minute bars, or from the live price
  alone before the open. Relative volume and the daily VWAP still come from
  the last completed session. A bar with no range yet gives no IBS reading
  (`ibs_source: forming_bar_no_range`) rather than placing today's price in
  yesterday's range, and after the close the gap is read from today's bar
  (it was never found: the bar's date was read a day early). Each snapshot
  records `daily_bar_mode` and `daily_last_completed_date`. **Every daily
  reading shifts from the first cycle that runs this version**, so compare
  signals from before and after it separately; the engine fingerprint changes
  with it. Backtests are unaffected: they read the previous day's completed
  bars only.
- **Algorithm versions show when they were proposed.** Every version read
  "Proposed: Unknown date": saved versions carried no date, and the console's
  fallback to the evolution log called a method that does not exist, inside an
  error handler that hid it. Versions now record their date when saved, the
  fallback works, and dates are sent with their time zone so every browser
  shows the same time. The same lookup now also picks the version a proposal
  is compared against (`diff_against` in the API): the one it was proposed
  from, or its recorded parent, rather than whichever folder sorts before it.
- **Parsing JSON whose text contains a bracket.** A `]` inside a string value,
  such as the interval "(0,5]", closed the top-level array early and the parse
  failed at an unrelated position. Valid JSON is now parsed as it is, and the
  fallback extraction ignores brackets inside strings.

### Changed

- **The momentum vote says when its MACD reading is noise.** It now reports the
  daily MACD histogram in ATRs (`macd_hist_atr`) and whether it is inside a
  neutral band of 0.10 ATR (`macd_neutral`, with the band as
  `macd_neutral_atr`). A histogram of 0.05 ATR makes the MACD part of the vote
  read -0.14, which an agent can mistake for dissent on a rising day. Reported
  only: the vote itself is unchanged.
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

- **Every cycle sees the latest position sync.** The daily sync runs between
  trading cycles, so what it found reached no cycle's record. It now keeps its
  latest result in the data folder (`trading/last_position_sync.json`), and
  `get_open_positions` shows it as `last_position_sync`: when it ran, its
  status and its cost-basis check.

### Fixed

- **The gap signal can vote at the first cycle of the session.** That cycle
  runs seconds after the open, before the first 5-minute bar exists, so the
  opening gap was unreadable exactly when a gap fade is still ahead, and read
  an hour later, after the gap had filled. In the session's first 15 minutes,
  while there is no bar yet, the gap is now read from the quote
  (`gap_source: quote_at_open`). The starter algorithm gives the gap signal no
  weight, so this makes it observable without changing any trade.
- **A swing-failure vote ages from when the reversal was confirmed**, not from
  the low: from the first close above the flush bar's high, and never from
  earlier than `min_confirm_bars`. A reversal that reclaims at once reads as
  before; one that chopped below the flush high first is no longer discounted
  for that wait. Its votes now report `bars_since_reclaim` and
  `bars_since_confirmed`.
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
