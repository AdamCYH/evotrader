---
name: signal-validation
description: "Measure whether a trading signal, agent instruction, or strategy change actually works — before promoting it. ACTIVATE when evaluating a new algorithm version, a Strategy/Risk instruction rewrite, an Evolution Agent proposal, or any claim that something 'improves performance'. Covers statistical validation, per-witness attribution, agent behavioural scenarios, and the failure modes that make backtests lie."
---

# Signal Validation

Tools and procedure for answering one question: **does this actually work, or
does it only look like it does?**

Every false positive found while building this system shared a shape — a result
that survived until one specific check was applied. This skill is those checks,
in the order that kills bad results fastest.

## Start here: the cheapest disqualifiers

Run these before any deeper analysis. Most claims die here.

| Check | Kills |
|---|---|
| Compare against a **matched control**, not a benchmark | "Beat buy-and-hold" when it just used more leverage |
| **Cluster by date** before computing t-statistics | 30 stocks falling on one market day counted as 30 observations |
| **Split in half** and score the second half alone | Selection on backtest performance |
| Count how many **variants were tried** | Reporting the best of 153 pairs |
| Check the **universe for survivorship** | Buy-the-dip on tickers that still exist |

For an algorithm version, the backtester runs all of these for you, plus the
placebo and positive control below:

```bash
uv run python -m evotrader.backtest.runner --compare vA,vB --validate --variants-tested N
```

Exit code 3 means it ran and nothing survived. The backtest skill
(`.agents/skills/backtest/SKILL.md`) covers running it and reading the report.
For your own P&L series, call the checks directly:

```python
from evotrader.backtest.validation import validate

report = validate(
    "my strategy", pnl, dates,
    split="2020-01-01",
    n_variants_tested=9,
    strategy_returns=strat, benchmark_returns=bench,
    universe=tickers,
)
print(report)   # per-check verdict plus one overall
```

`report.passed` is False if any error-severity check fails. Treat that as
disqualifying, not as a starting point for debate.

## The noise floor — read this before any parameter search

Before asking "is this result significant?", ask "could this measurement have
produced it with no signal at all?"

`runner --validate` builds this for every version it runs (`--placebos N`,
default 40) and prints the smallest return the run can tell apart from luck.
By hand, for a signal series of your own:

```python
from evotrader.backtest.checks import check_version, replay, shift
from evotrader.backtest.runner import RiskParams, simulate_version

risk = RiskParams()
metrics, signals, engine = simulate_version("v002_my_idea", snapshots, risk=risk)
verdict = check_version("v002_my_idea", snapshots, signals, engine, risk, n_placebos=60)
print(verdict.report)          # placebo, positive control and every other check
```

A **circular shift** preserves the signal's marginal distribution and full
autocorrelation — trade count, holding period and costs stay put — while
destroying any true relationship with forward returns. Each shifted run is one
draw from "this signal predicts nothing".

Measured on this system, 60 shifts of the shipped composite:

| | value |
|---|---|
| real result | −1.61% |
| placebo mean / sd | −0.65% / **1.86%** |
| placebo range | −5.12% .. +3.65% (**8.77 pp**) |
| real result's percentile | **35th** — z = −0.52 |

**The shipped signal is indistinguishable from no signal.** More importantly the
*spread* is 8.77 pp, and the entire min-hold parameter sweep spanned 4.04 pp.
Every result reachable by tuning fits inside the noise floor.

**Always pair this with a positive control.** A cheating signal (sign of the
realised forward move) returns **+51.83%** through the identical engine, so the
null is a fact about the signal, not a broken harness. `--validate` runs it
first and says so when it fails (for example, on an instrument priced above the
per-trade allocation without `--fractional`, even the cheat makes 0%). Without that control,
"everything is noise" and "my plumbing is broken" look identical.

Block-mixing truth with a shifted copy of itself traces the sensitivity curve:

| accuracy | 51.2% | 56.0% | 60.8% | 65.4% | 75.6% | 100% |
|---|---|---|---|---|---|---|
| 2y return | −0.33% | +3.78% | +8.73% | +13.27% | +23.97% | +51.83% |

Roughly **0.85 pp of two-year return per 1 pp of directional accuracy above
50%**. Against sd 1.86%, `minimum_detectable_effect(1.86)` = 3.72 pp, so:

> **~56% directional accuracy is the floor this backtest can resolve.**

Every witness here measures ~50%: news 50% (108 cases), algorithm 50%,
agreement 50%. The gap to detectability is 5–6 pp of accuracy; a parameter
change moves the result by ~2 pp of equivalent accuracy, all of it noise.

This is the mechanism behind "4,000 weightings found nothing" and "25 versions,
none beat buy-and-hold": those searches sampled noise and reported the maximum.
**To move the floor, change the measurement — longer history, genuinely
independent instruments, lower turnover — not the parameters.**

## The backtest is not the live system

`BacktestEngine` simulates "trading decisions and portfolio tracking **without
LLM interaction**". The gap this opens is large:

| | trades | policy |
|---|---|---|
| backtest, algorithmic composite | 309 in 2y | trades whenever \|signal\| > 0.03–0.05 |
| live, LLM strategy agent | 0 of 5 real historical setups | flat unless a dated catalyst |

Every parameter result in this repo describes the engine, which the live agent
gates almost shut. Do not read a backtest return as a forecast of live P&L, and
do not tune the engine expecting the live system to move.

## Recorded failures, and what caught each

These are real. Keep them in mind — the numbers repeat.

| Claim | Reality | Caught by |
|---|---|---|
| Pairs trading, Sharpe 0.46 | −0.03 out of sample | `out_of_sample_split` |
| Covered calls, +17.3%/yr | Live index +6.3%/yr | comparison to a real track record |
| Mega-cap dip, t = 7.05 | t = 3.77 after clustering | `clustered_tstat` |
| "Quiet periods only", t = 6.58 | Artefact of deleting the left tail | `period_stability` |
| Strong signal, no news, sized 86% of capital | The 0-for-8 pattern | scenario test |
| Trend legs inverted: IC +0.051, **14/15 tickers, both halves** | clustered t = +0.23; signs flip on a longer window | `clustered_tstat` |
| Long-only variant, +2.28% vs −1.10% | Beta plus a single fold; 3 of 4 folds at or below matched QQQ | walk-forward vs matched control |
| "The stop loss is the entire loss": 24 stops, −$1,968, 0% win | Widening 2→5 ATR made it **worse**; losses re-routed through signal exits | moving the gate instead of reading the table |

The pattern: **the more impressive a result looks, the more likely one check
demolishes it.** A t-statistic above 5 on financial data is nearly always a
methodology bug, not a discovery.

## Per-witness attribution

Scoring a blend tells you the blend was wrong. It cannot say which input was
wrong, so every proposed fix becomes a guess.

```python
from evotrader.backtest.attribution import attribute, score_calibration

report = attribute(
    {"algo": algo_dirs, "news": news_dirs, "llm": llm_dirs},
    forward_returns, timestamps,
)
print(report)                       # per-source scores
report.agreement_adds_value         # the question the ensemble rests on
```

`agreement_adds_value` is the multi-witness thesis in one boolean. If agreement
does not beat the best single source, the witnesses are correlated — they are
re-reading the same information and the ensemble is decoration.

`score_calibration(convictions, directions, forward_returns)` checks whether a
stated conviction of 0.8 actually wins 80% of the time. Conviction drives
position size, so persistent overconfidence at high bands is expensive.

## Testing agent instructions

An instruction that reads well can still leave a rule ambiguous, and a live
agent will find that ambiguity. Scenarios test the instruction, not the code.

```bash
python -m evotrader.scenarios.runner                                   # list
python -m evotrader.scenarios.runner --scenario NAME --version v013 --render > p.txt
python -m evotrader.scenarios.runner --scenario NAME --check response.txt
```

Render the prompt, give it to a fresh agent with **no conversation context**
(a subagent works), save the response, then check it. Exit code is non-zero on
an error-severity violation.

Scenarios live in `data/scenarios/*.yaml`: market state, `facts` for gating
rules, and `rules` with `forbid`/`require` regexes.

> Regex checking catches broken rules. It does not catch bad reasoning dressed
> in the right words. Read the transcripts too.

## Scenarios from real history

Hand-written scenarios test compliance. Only real history tests profitability.

```python
from evotrader.scenarios.from_history import (
    load_news, load_snapshots, pair, attach_forward_returns, strong_cases)

paired = pair(load_snapshots(DB), load_news(DB), max_age_minutes=90)
scored = attach_forward_returns(paired, prices, horizon_days=5)
```

**`pair()` matches news backwards only.** A report filed after the snapshot is
information the agent could not have had; pairing it manufactures edge. If you
modify this, keep `direction="backward"`.

`generate.py` builds scenarios from live-pipeline snapshots, selected by
forward-return **quantile** rather than by hand — choosing interesting-looking
setups measures the author's hindsight instead of the agent's judgement. The
realised outcome is stored in the file but never rendered into the prompt;
a test asserts this.

## Baselines worth remembering

Measured on this system. Anything claiming to beat these needs the checks above.

| Thing | Result |
|---|---|
| Composite signal vs next-day returns | correlation ≈ −0.03, 12 instruments |
| News sentiment, 108 real paired cases | 50% hit rate, clustered t = 0.25 |
| Algorithm, same cases | 50% hit rate, clustered t = 0.64 |
| News + algorithm agreeing, 38 cases | 50% — agreement added nothing |
| Volatility premium (VIX vs realised) | +3.56 pts, t = 7.70 — real, but unharvestable net of costs |
| Not panicking in a drawdown | +4.12 pp/yr — the largest measured effect found |
| Gross edge of the shipped composite | ≈ 0. At 0 bps slippage the 2y backtest returns −0.34% |
| Round-trip cost hurdle | ~4 bps of notional. Slippage was $250 of a $275 loss over 255 trades |

The "not panicking" row is worth dwelling on: the biggest edge located anywhere
in this work was behavioural, not predictive.

The two cost rows set the bar every future signal has to clear. Gross P&L is
already ~0, so a candidate is not interesting because its IC is positive — it is
interesting only if its per-trade edge beats ~4 bps round-trip at the frequency
it actually trades.

## Two traps that survived every other check

**Correlated tickers are not independent samples.** A per-ticker sign count
("14 of 15 positive") reads like cross-sectional confirmation and is worth
almost nothing when the names share a factor. Cluster by date *before* believing
any cross-sectional result; `clustered_tstat` prints the naive figure next to
the clustered one so the inflation is visible.

**An exit-reason table attributes losses to whichever gate fired, not to the
cause.** Stops showed 24 trades, −$1,968, 0% win rate — an open-and-shut case
for changing the stop. Widening it made the result worse: the same losses simply
left through signal reversal instead. Test an exit hypothesis by *moving the
gate and re-running*, never by reading the attribution table.

## When a result survives everything

Then ask what it costs to harvest. The volatility premium is statistically
overwhelming (t = 7.70) and still loses to the index once skew and spreads are
paid. **Statistical significance is necessary, not sufficient** — always price
the implementation before believing a strategy.
