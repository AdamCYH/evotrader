---
name: offline-iteration
description: "Search for a better system-instruction version or algorithm parameter set using scenario tests and backtests, without risking live capital. ACTIVATE when asked to tune, calibrate, improve, or iterate on a Strategy/Risk instruction, composite weights, or entry/exit parameters. Enforces the isolation and statistical discipline that stop a search from producing false positives."
---

# Offline Iteration

A loop for improving instructions and parameters offline. The loop is easy; the
discipline is the point. A search that runs without these constraints will
always find something, and it will always be noise.

## The three rules that make results mean anything

**1. Test agent decisions with a fresh subagent, every time.**

A model that has seen the conversation knows what you are hoping for. It will
produce it. Testing must go through a subagent with **no prior context**:

```
Agent(subagent_type="general-purpose", prompt="""
Read this file in full and follow it exactly. It contains your system
instructions and one cycle of market data:
  <absolute path to rendered prompt>

You are the Strategy Agent described in that file. Act exactly as that agent
would. Do not comment on this being a test. Do not read any other file.

Produce your cycle output as the instructions require: reasoning, then final
decision, then the exact log_signal_attribution JSON.
""")
```

Render the prompt to a file first so the subagent reads instructions and data
in one place and cannot wander the repo:

```bash
python -m evotrader.scenarios.runner --scenario NAME --version v013 --render > /tmp/p.txt
```

Never paste the instruction text into the subagent prompt yourself, never
mention the hypothesis under test, and never run two versions in one agent —
whichever it reads second is contaminated by the first.

**2. Fit and test on separated data.** Both dimensions:

- *Temporal* — fit on year 1, test on year 2.
- *Cross-sectional* — fit on QQQ, test on tickers the fit never saw.

Cross-sectional alone is weak: nine equity ETFs over the same two years are
roughly two independent samples, not nine.

**3. Count every variant you try.** Thresholds, weights, horizons, regimes —
they all count. Ten variants moves the significance bar from t≈2.0 to t≈2.8.
`validation.multiple_testing_penalty(n_tested, best_t)` does the arithmetic.

**4. Run the placebo before the search, not after.** Circularly shift the signal
against prices 60 times and re-run; that is the distribution of results this
measurement produces with no signal at all. On a 2-year QQQ backtest its sd is
**1.86%** and its range is **8.77 pp** — wider than any parameter sweep yet run
here, which means a sweep cannot tell you anything. Pair it with a positive
control (perfect foresight returns +51.83%) so a null result cannot be confused
with broken plumbing. `validation.placebo_test` and `circular_shift_indices`.

## Before you start: can this be measured at all?

The detection floor here is **~56% directional accuracy** (0.85 pp of two-year
return per 1 pp of accuracy, against a placebo sd of 1.86%). Every witness in
this system measures ~50%. If your hypothesis cannot plausibly move accuracy by
5–6 points, the backtest cannot see it and the search will only find noise.

And note which system you are tuning: the backtest engine runs **without the
LLM**, trading 309 times in 2 years, while the live agent stayed flat on all
five real historical setups it was given. Tuning the engine does not move the
live system.

## The loop

```
1. State the hypothesis and what would falsify it. Write it down first.
2. Change ONE thing — an instruction section, or one parameter family.
3. Backtest:  python -m evotrader.backtest.runner --version V --walk-forward 6
4. Scenario-test with fresh subagents (rule 1).
5. Validate:  validate(...) from evotrader.backtest.validation
6. Record the result — including failures. A failed hypothesis is data.
7. If it survives, price the implementation before believing it.
```

Step 7 is where most survivors die. The volatility premium reaches t = 7.70 and
still loses to the index after skew and spreads.

## Scenario coverage worth having

`data/scenarios/` should exercise the decisions that cost money, not the easy
ones:

| Scenario | Tests |
|---|---|
| Strong algorithm, no news | The agreement gate. This is the 0-for-8 pattern. |
| Drawdown, position underwater | That the agent holds rather than panic-selling |
| Breached stop | That a named cause produces an exit, unlike a directional view |
| Conflicting witnesses | That conflict produces flat, not a hedge |
| Genuine dated catalyst | That real evidence actually unlocks size |
| Muted/annihilated channel | Zero-typing: absence is not calm |

Generate history-based cases with `scenarios/generate.py` (quantile-selected,
outcome hidden) and news-paired cases with `scenarios/from_history.py`
(backward-only matching).

## Rewriting an instruction: edit, never rewrite

**Both instruction rewrites in this repo lost critical content when written
from scratch.** The strategy version dropped STOPS — the section titled "THE
SINGLE BIGGEST FIXABLE LOSS SOURCE" — along with nine filed signal defects. The
risk version dropped every tool name and the prohibition on selling options.

Procedure:

1. Copy the current version.
2. Replace only the sections the hypothesis touches.
3. Diff section headings old vs new and confirm every omission is deliberate.
4. Grep for tool names, verdict vocabulary, and hard prohibitions.
5. Check for contradictions with what you added — one rewrite left "reduce
   sizing in a drawdown" next to a new rule forbidding exactly that.

## Do not over-specify

Instructions guide judgement; they do not enumerate cases. A rule stated once
with its reasoning generalises. The same rule illustrated with six worked
examples teaches the agent to pattern-match those six and mishandle the seventh.

Prefer "a lean that failed the priced-in test is not a witness" over three
examples of leans that failed.

## Recording a decision: status semantics

`evolution_log.status` drives the review queue in the web console. Choosing the
wrong one leaves an item pending forever.

| Status | Means | Use when |
|---|---|---|
| `PROPOSED` | **Awaiting a human decision** | You genuinely want the user to choose. Nothing else. |
| `ACTIVE` | Promoted and live | Must match the `active.txt` pointer |
| `REJECTED` | Decided against, reasoning kept | You already concluded "do not promote" |
| `SUPERSEDED` | Replaced before ever going live | A later version overtook it |
| `ARCHIVED` | Historical, no action | Cleanup |

**If you already know the answer, do not write `PROPOSED`.** A change you have
tested and decided against is `REJECTED` with the evidence recorded — not a
queue item with "do not promote" buried in the reasoning text. That mistake put
a rejected algorithm version in the review queue indefinitely.

Lead the `reasoning` field with the verdict (`NOT PROMOTED — ...`) so it reads
correctly in the console without expanding, and end with a `RE-TEST WHEN:`
clause if the decision could change with better data.

### Verify both halves after any status change

DB status and the `active.txt` pointer are **independent**. The console can
update one without the other, and it has:

```bash
sqlite3 data/db/evotrader.db \
  "SELECT target_component,new_version,status FROM evolution_log WHERE status!='ACTIVE'"
cat data/instructions/strategy/active.txt data/instructions/risk_manager/active.txt
```

A version showing `ACTIVE` whose pointer still names the old file is **not
live**. Observed: a strategy approval applied both halves while the
risk-manager approval applied neither, in the same UI session.

Also check the write actually landed. `update_status` returns `bool`; a `False`
means no row matched or the write was refused. It used to return `None` and
swallow the exception, so a rejection blocked by a schema CHECK constraint
reported success and changed nothing.

## What has already been searched

Do not re-run these. Details in `skills/signal-validation/SKILL.md`.

| Searched | Result |
|---|---|
| Minimum holding period, 13 values 0–60 bars | No structure; +0.86% at 35 sits between −2.36% at 28 and −2.29% at 49 |
| Placebo (60 circular shifts) | Real signal at the 35th percentile of its own null |
| 4,000 random composite weightings | Best in-sample IC +0.084 → +0.041 out |
| Entry thresholds 0.5x–4x | Monotonically worse as the bar rises |
| Entry thresholds re-run on a positive-IC composite | Also no better (−1.45/−1.11/−1.20/−1.22% at 0.03/0.10/0.18/0.25) |
| Stop/take-profit geometry | Stops barely bind; widening 2→5 ATR makes it *worse* |
| Exit hysteresis | Helps, but only by increasing exposure |
| 25 archived algorithm versions | None beat buy-and-hold |
| News sentiment, 108 real cases | 50% hit rate, t = 0.25 |
| **Trend-leg inversion (momentum / trend_persistence / range_break)** | **Dead. Clustered t = +0.23. See below.** |

**The momentum-inversion line is CLOSED.** It was the most promising-looking
result in this repo and it was an artefact. Recording it in full because it
will look promising again to anyone who re-derives it.

The claim: momentum's fitted weight is negative, and inverting it lifts
out-of-sample IC. It reproduced and got *stronger* under a wider test —
inverting all three trend legs took composite IC from −0.026 to **+0.051** at a
7-bar horizon, positive on **14 of 15 tickers in both halves** of a 2-year
window, at all three horizons tested. Signal strength was monotone in
conviction: top two deciles +9.29 bps, bottom two −3.24 bps.

Every one of those numbers is inflated by the same thing. **Clustered by
calendar day, it is nothing:**

| | naive t | clustered t |
|---|---|---|
| composite, 1-bar | +1.65 | **+0.79** |
| composite, 7-bar | +0.93 | **+0.23** |
| QQQ alone, 7-bar | — | **−0.03** |

Per-leg clustered t at 7 bars: momentum −0.48, mean_reversion +0.21,
trend_persistence −0.72. `clustered_tstat` says it outright: *"naive figure was
materially inflated by same-day clustering."*

**"14 of 15 tickers" was one observation counted fifteen times.** The panel is
QQQ, SPY, XLK, TQQQ, IWM, AAPL, NVDA, TSLA, ARKK, XLF, XLV, XLE, EEM, GLD, TLT
— one equity factor. The earlier "8 of 9 tickers" was the same artefact, which
is exactly what rule 2 already warned about and it still got through.

A confirmatory run on 5 years of daily bars (1,249 dates, 2.5x the independent
time) did not merely fail to confirm — **the signs flipped**: momentum +10.7
bps/5d (clustered t +1.58), mean_reversion −10.3 bps (t −1.55). An effect that
reverses sign between windows and reaches significance in neither is noise.

Do not re-run this on more equity tickers. Adding correlated names adds no
independent samples — it only inflates the naive statistic again.
