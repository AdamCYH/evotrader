---
name: backtest
description: "Backtest a trading idea on past prices and judge whether the result is evidence or luck. ACTIVATE when asked to test, try, backtest or compare a strategy idea, a new sub-strategy, an algorithm version, a parameter change, or a different instrument or data source. Covers getting data (Yahoo Finance or your own CSVs), writing a strategy outside the package, running with --validate, reading the report in the right order, and reporting back without overclaiming."
---

# Backtest a strategy idea

The backtester replays the rule-based algorithm on past bars, one at a time,
hiding the future, and trades it with realistic fills. With `--validate` it also
says whether the result could have come from luck. **Always pass `--validate`.**
Without it you get a leaderboard, and the top row of a leaderboard is usually
the luckiest, not the best.

It needs no accounts and no AI key. It does **not** run the AI agents or read
news; for those, see the scenario harness in the offline-iteration skill.

## 1. Have a data folder

The runner reads algorithm versions from the data folder
(`EVOTRADER_DATA_DIR`, default `./data`). A fresh one needs no keys:

```bash
./run.sh --init                       # ./data; or ./run.sh --init /path/to/folder
export EVOTRADER_DATA_DIR=/path/to/folder   # when it is not ./data
```

`--init` only copies the starter files; it asks no questions and starts
nothing. (`./run.sh setup` is the interactive setup for trading; a backtest
does not need it.) **Use a separate data folder for experiments**, never the
one a live app trades from: the strategy manifest is shared by every version
in a folder, and the live app reads it too.

Reports are written to `<data folder>/backtest/reports/`, downloads are cached
in `<data folder>/backtest/cache/` (`--refresh` re-downloads).

## 2. Choose the data

| Source | Flags | Limits |
|---|---|---|
| Yahoo Finance | `--ticker SPY --period 2y --interval 1h` | Hourly bars reach 2 years back; 5-minute bars 60 days. The runner refuses a longer request instead of silently returning less. |
| Your own bars | `--intraday-csv FILE --daily-csv FILE --ticker NAME` | Any instrument and any length. With both files no network is used. |

A CSV needs a timestamp as its first column and `open`, `high`, `low`,
`close`, `volume` columns (any capitalisation). Daily bars feed indicators and
the market-regime detector; give at least a year of them before the intraday
window starts.

Instrument notes:

- **Price above the per-trade allocation** (Bitcoin at $100k with $25k and 10%
  per trade): add `--fractional`, or every entry is skipped. The positive
  control catches this, see below.
- **24-hour markets** (crypto, FX): the built-in strategies assume an exchange
  session. The session-anchored ones (VWAP, gap) degrade; read the
  participation table.
- **5-minute data** is needed for strategies that look back more than 7 bars
  within a session; an hourly session only has 7.

## 3. Write the idea as a strategy (skip if you are only changing parameters)

A strategy is a class that returns a vote between −1 (bearish) and +1
(bullish) from one `MarketSnapshot`. It can live **outside this package**; the
loader imports whatever module the manifest names.

```python
# my_strategies/close_vs_open.py
from evotrader.algorithms.base import TradingAlgorithm
from evotrader.models.market import MarketSnapshot
from evotrader.models.signals import AlgoSignal


class CloseVsOpen(TradingAlgorithm):
    def __init__(self, scale: float = 1.0) -> None:      # parameters = keyword args with defaults
        self.scale = scale

    @property
    def name(self) -> str:
        return "close_vs_open"

    @property
    def version(self) -> str:
        return "v001"

    def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
        bars = snapshot.recent_candles                      # this session, up to and including now
        if len(bars) < 2:                                    # nothing to say: abstain, don't vote 0
            return AlgoSignal(name=self.name, value=0.0, weight=1.0,
                              metadata={"applicable": False, "reason": "insufficient_data"})
        move = (bars[-1].close - bars[-1].open) / bars[-1].open * 100
        return AlgoSignal(name=self.name, value=max(-1.0, min(1.0, move * self.scale)), weight=1.0)
```

Rules: no I/O, no state between calls (everything comes from the snapshot),
and abstain with `applicable: False` rather than voting 0 so the other
strategies share its weight. Look at `snapshot.indicators`, `daily_candles`,
`regime` and the built-in strategies in `src/evotrader/algorithms/strategies/`
for what is available.

Register it in `<data folder>/algorithms/strategy_manifest.yaml`:

```yaml
  close_vs_open:
    module: close_vs_open        # importable module path
    class: CloseVsOpen
    description: "Last bar's body as a directional vote"
    status: active
```

and make the module importable: `export PYTHONPATH=/path/to/my_strategies`
(or install your package into the environment). The live app loads the same
manifest, so it needs the same `PYTHONPATH`. A module that fails to import is
logged as an error and left out; check the participation table lists it.

To contribute a strategy to the project instead, put it in
`src/evotrader/algorithms/strategies/` and follow "Add a trading strategy" in
the evotrader-developer skill.

## How the engine trades

Know this before mapping an idea onto it:

- It decides on each bar's close and **fills at the next bar's open** (on
  hourly bars, an entry decided at the 9:30 bar fills at about 10:30).
- It enters long when the combined signal is at least 0.03 in a trending
  market or 0.05 otherwise, short at the negatives of those.
- It **exits a long when the signal drops to 0 or below** (a short when it
  rises to 0 or above). A strategy that abstains therefore closes the
  position on the next bar. There is no "exit at the close" rule.
- A stop at 2 daily ATRs (average true range: the typical daily move) and a
  take-profit at 3.5 ATRs are checked against each bar's high and low.
- Each trade uses 10% of cash and pays 0.02% slippage per side.

All of these are flags or a `backtest:` block in the version's config. An idea
that needs a rule the engine lacks (hold for exactly one day, exit at the
close) is better tested with your own script on daily bars; see the end of
this skill.

## 4. Make a version for it

Never edit the active version. Copy it and change the copy:

```bash
cp -R data/algorithms/v001_initial data/algorithms/v002_close_vs_open
```

In the copy's `config.yaml`:

- add the strategy's parameters as a block named after it
  (`close_vs_open: {scale: 1.0}`);
- give it a weight under `composite:` (`close_vs_open_weight: 0.2`);
- **and add it to every regime under `composite.regime_weights`**. With
  `regime_adaptive: true` (the starter default) that table replaces the flat
  weights, and a strategy missing from it counts as 0: it runs, shows as
  emitting in the participation table, and never moves a trade. The loader
  logs a warning when this happens. Weight 0 in every regime is the deliberate
  version: recorded, not voting.

Update the copy's `metadata.yaml` so it describes the new version. The
runner does not need an entry in `registry.yaml`; the console does, if you
later want to activate the version there.

Execution settings (stops, size, slippage, long-only) can go in a `backtest:`
block of the version's config or on the command line; `--help` lists them.

### Testing the idea on its own

Usually the first question is whether the idea works alone, not blended with
the built-in strategies. In a separate data folder:

1. In `strategy_manifest.yaml`, set every other strategy to
   `status: disabled`. Weight 0 is not enough for all of them:
   `event_window_timing` is an overlay that scales the combined signal
   whatever its weight.
2. In the version's `composite:` block, set `regime_adaptive: false` and your
   strategy's weight to 1.0. With regime weighting on and no table of your
   own, a built-in table applies that does not list your strategy.
3. Expect the log to stay quiet about bars where your strategy abstains; the
   participation table reports how often it voted.

## 5. Run it

```bash
R="uv run python -m evotrader.backtest.runner"

$R --version v002_close_vs_open --validate                     # one version
$R --compare v001_initial,v002_close_vs_open --validate        # side by side
$R --version v002_close_vs_open --walk-forward 4 --validate    # stability across 4 periods
```

**Count honestly with `--variants-tested N`**: every version, weight, threshold
and instrument you have tried for this idea, in this session and earlier ones.
`--compare` counts the versions it runs, but it cannot know about the ones you
tried and dropped. Ten tries raise the bar from t > 2.0 to about t > 2.8.

Exit codes: `0` at least one version survived every check; `3` the run worked
and nothing survived; `1` a crash; `2` a bad option.

## 6. Read the report in this order

1. **Positive control** (first row of the Validation table). A signal that
   cheats by reading the next bar. If it FAILS, the harness cannot see an edge
   on this data at all (too few bars, or positions it cannot afford) and
   nothing else in the report means anything. Fix the setup first.
2. **Sub-strategy participation.** A strategy marked *inert* never voted: the
   ensemble you measured is not the one configured. Your new strategy must
   appear here with a non-zero "Emitted" share.
3. **Validation verdict.** Any FAIL is disqualifying. The placebo row states
   the smallest return this run can tell apart from luck; a difference between
   two versions smaller than that is not a difference.
4. **Only then** the returns, Sharpe ratio (return per unit of risk) and
   drawdown (largest peak-to-trough fall).

What each check asks:

| Check | Question |
|---|---|
| positive control | Can this harness see an edge at all? |
| clustered t-stat | Is the average trade more than luck, counting each trading day once? |
| out-of-sample | Does it still work in the second half of the window? |
| period stability | Is the profit spread across years? (Needs 3 calendar years to pass.) |
| concentration | Is the profit spread across trades, not a few big wins? (Needs 20 trades.) |
| multiple testing | Does it clear the higher bar for the number of variants tried? |
| vs benchmark | Is it a better risk-adjusted holding than the instrument itself? |
| placebo | Does it beat the same signal shifted in time so it predicts nothing? |

## 7. Decide, and report back

- **Nothing survived (exit 3).** That is the common, correct answer. Report it
  plainly. Do not then tweak parameters until something passes: each tweak is
  another variant, and the placebo spread is usually wider than anything a
  parameter sweep can move. Change the idea or the measurement (more history,
  another instrument, lower trading frequency), not the knobs.
- **Something survived.** Still not a green light. Estimate what it costs to
  trade live (spread, commissions, a leveraged or inverse fund for the short
  side), then run a practice cycle (`./run.sh --mode sim`) before real money.
  The backtest leaves out the AI agents, which in live trading decide whether
  and how much to trade.

Report to the person in plain words: the command you ran, the instrument and
window, the positive-control result, the participation of the new strategy,
the verdict with each FAIL named, the return next to the placebo's "smallest
distinguishable" figure, and how many variants you counted. Never lead with
the return.

## Ideas the runner cannot express

Yahoo's hourly bars reach back only 2 years, which is too short to judge an
idea that trades once a day. For those, write a short script on daily bars
(decades of them are free) and reuse the checks:

1. Build one return per trade, net of costs, and its date.
2. `validate(name, returns, dates, split=..., n_variants_tested=N)` from
   `evotrader.backtest.validation`.
3. Placebo: rebuild the returns with the signal circularly shifted by each of
   `circular_shift_indices(n, 40)`, total each run, and pass the real total
   and the placebo totals to `placebo_test`.
4. Positive control: the same with a signal that knows the day's move. It
   must pass the placebo test, or the script cannot see an edge.

Report per-trade figures in that case; the sum of per-trade percentages is
not a portfolio return.

## Using the checks from Python

For custom experiments (a signal series you built yourself, several
instruments), the pieces are importable:
`evotrader.backtest.runner.simulate_version` (a run plus its per-bar signals),
`evotrader.backtest.checks.check_version` (the full verdict) and
`evotrader.backtest.validation` (the individual checks). The signal-validation
skill explains the statistics and the traps that survived them.
