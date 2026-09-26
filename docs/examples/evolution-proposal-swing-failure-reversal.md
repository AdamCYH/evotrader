*Example output of the evolution loop, lightly edited and anonymised.*

The evolution agent wrote this **strategy proposal** after a run of losing
trades that had bought into a falling market. It looked for what the few good
trades had in common and proposed a new rule-based strategy around it. A human
approved it, and it was implemented as
[`src/evotrader/algorithms/strategies/swing_failure_reversal.py`](../../src/evotrader/algorithms/strategies/swing_failure_reversal.py).
The note at the end says what happened next.

Anonymised: trade numbers, prices, dates, profits and account details are removed
or replaced with general descriptions, and "the traded ETF" stands for the
instrument. Everything else — the structure, the reasoning, the parameters — is
as the agent wrote it. The file's YAML front matter is shown as a code block.

A few terms: a *flush* is a quick drop to a new low; *VWAP* is the day's average
traded price; *ATR* (average true range) is the typical daily price range, used
here as a ruler for "how far"; the *composite* is the algorithm's combined
signal, and a *regime* is the market state it is weighted for (for example
*range_bound*, a market going sideways).

---

```yaml
---
proposal_id: p_new_swing_failure_reversal_20260727_133920
type: new_strategy
status: applied
created_at: '2026-07-27T20:39:20Z'
target_strategy: swing_failure_reversal
required_indicators:
- recent_candles (intraday OHLC)
- vwap
- vwap_anchor
- atr_14
- relative_volume
---
```

# Proposal: swing_failure_reversal

## Description

Fires long ONLY after a downside stretch has demonstrably STOPPED extending —
requiring a swing-failure structure (a higher low following a flush, with
reclaim of a reference level) instead of firing continuously while price makes
new lower lows. Directly targets the discriminator that separated the system's
two large winners from its run of consecutive losers: winners had price
STABILIZE and reclaim; losers all made continued lower lows while the daily
oscillators kept re-arming.

## Rationale

Proposed dynamically by Evolution Agent.

## Signal Logic

```text
HYPOTHESIS
The existing mean_reversion sub-signal measures DEPTH of stretch
(RSI/Bollinger/IBS). Depth is necessary but NOT sufficient — in a multi-session
downtrend the daily band re-arms at every new lower low, so depth alone fires
every hour of a falling knife (documented: a run of consecutive losing trades as
the traded ETF ground about 4% lower over several sessions). What distinguished
the two large winners was not depth but STRUCTURE: the stretch had stopped
extending. This strategy measures the derivative of the stretch, not the
stretch itself.

SIGNAL LOGIC (from recent_candles, intraday granularity)

1. ABSTAIN GUARDS (return applicable=False)
   - fewer than `lookback_bars` candles, or atr_14 is None/<=0
   - vwap_anchor != "current_session"  (same anchor discipline as intraday_vwap_zscore)

2. LOCATE THE FLUSH
   flush_idx = index of the lowest low within the last `lookback_bars`
   flush_low = low at flush_idx
   bars_since_flush = len(candles) - 1 - flush_idx
   - Require bars_since_flush >= `min_confirm_bars` (default 2): we need at
     least a couple of bars of evidence that the low is holding.
   - Require bars_since_flush <= `max_confirm_bars` (default 8): after that the
     reversal is no longer fresh — this is the staleness guard that stops the
     signal re-firing all session on one old low.

3. REQUIRE THE STRETCH TO HAVE BEEN REAL
   stretch_atr = (vwap - flush_low) / atr
   - Require stretch_atr >= `min_stretch_atr` (default 1.2). No stretch, no
     reversal to trade. This preserves the "genuine extreme" quality bar that
     made the two winners work, rather than fading noise.

4. REQUIRE STRUCTURAL CONFIRMATION (the actual innovation — all must hold)
   a) HIGHER LOW: min(low of bars after flush_idx) > flush_low
      i.e. price has NOT made a new lower low since the flush.
   b) RECLAIM: last close > high of the flush bar
      i.e. price has traded back through the flush bar, not merely paused.
   c) NO NEW LOW ON LAST BAR: last low > flush_low

5. STRENGTH
   base = tanh((stretch_atr - min_stretch_atr) / stretch_scale)   # deeper flush that held = stronger
   freshness = confirm_decay ** max(0, bars_since_flush - min_confirm_bars)
   reclaim_quality = clamp((last_close - flush_high) / atr / reclaim_scale, 0, 1)
   value = clamp(base * freshness * (0.5 + 0.5 * reclaim_quality) * base_strength, 0, 1)

   Long-only by construction (returns 0.0 or positive). Rationale: the account
   is structurally long-only (the constitution forbids short selling, and put
   options were not a practical alternative), so a symmetric bearish leg would
   be unexpressable and is deliberately omitted rather than emitted and
   discarded. If shorting or affordable puts become available, mirror the logic
   on the upside (lower high + failure to hold a spike) as a v002.

6. METADATA (for attribution)
   applicable, flush_low, bars_since_flush, stretch_atr, higher_low (bool),
   reclaimed (bool), reclaim_quality, freshness, reason

REPLAY EXPECTATION AGAINST THE KNOWN SAMPLE
- The first five losing entries: ABSTAIN. Every one made continued lower lows
  after entry — condition 4a fails by construction. All five losses avoided.
- The next entry (one morning, bought at the session low): ABSTAIN at entry, no
  confirmation yet — then FIRES ~1-2 cycles later once the low held and price
  reclaimed toward VWAP. This is exactly the move the strategy agent had just
  exited on a time stop and then described as "I exited 45 minutes before it
  delivered the VWAP reclaim I was waiting for." The strategy would have
  entered where the agent exited.
- The last entry (at a close): FIRES weakly — session low held, VWAP
  reclaimed. Correct: this was the one instance in the run where the setup
  actually worked.
```

## Suggested Parameters

| Parameter | Value | Description |
|---|---|---|
| `lookback_bars` | `20` | Suggested initial parameter |
| `min_confirm_bars` | `2` | Suggested initial parameter |
| `max_confirm_bars` | `8` | Suggested initial parameter |
| `min_stretch_atr` | `1.2` | Suggested initial parameter |
| `stretch_scale` | `0.8` | Suggested initial parameter |
| `confirm_decay` | `0.75` | Suggested initial parameter |
| `reclaim_scale` | `0.5` | Suggested initial parameter |
| `base_strength` | `0.8` | Suggested initial parameter |
| `require_current_session_anchor` | `True` | Suggested initial parameter |

## Integration Details

PROPOSED range_bound WEIGHT: 0.15, funded by mean_reversion (0.20 -> 0.10) and
gap (0.05 -> 0.0, which abstains on nearly every cycle anyway). Suggested
range_bound set: momentum 0.0, mean_reversion 0.10, gap 0.0,
intraday_vwap_zscore 0.40, swing_failure_reversal 0.15, event_window_timing
0.05, range_break_continuation 0.15, options_positioning 0.15.

WHY THIS IS COMPLEMENTARY, NOT REDUNDANT. intraday_vwap_zscore measures "how
stretched are we NOW" and fires AT the extreme — it is early and occasionally
catches a knife mid-fall. swing_failure_reversal measures "did the stretch
STOP" and fires AFTER the turn — it is later but structurally confirmed. They
are deliberately correlated in DIRECTION but decorrelated in TIMING, so when
both fire together the composite reads genuinely strong (a stretch that is also
confirmed turning) and earns the aggressive sizing tier. That co-firing case is
precisely the archetype of the two large winners.

WHY THIS SERVES THE OPERATOR'S GOAL. It adds positive-expectancy FLOW rather
than merely subtracting negative-expectancy flow. The previous algorithm
version removed the losing signature from the actionable set; without a
replacement source the system risks drifting toward fewer trades, which is
explicitly not the goal. This strategy re-supplies entries from the one setup
shape with a demonstrated positive record, so trade count is maintained while
expectancy per trade rises.

BACKTEST GATES: (1) must abstain on all five of the losing entry cycles; (2)
must fire in the midday window where the low held and price reclaimed; (3)
standalone win rate over the 30-day range_bound window must exceed the 0% of
the mean-reversion-only signature it replaces; (4) must not fire more than ~2x
per session on the same flush (staleness guard working).

---

## What happened next (editor's note, not part of the proposal)

The strategy was implemented with tests, added to the strategy manifest and
given weight in several regimes. Then, for weeks, it never fired in live
trading — and the scored live record, not a backtest, showed why. A later
evolution run found two gates that made it unreachable at the system's hourly
rhythm:

- **Timing.** "Fresh" meant 2–8 five-minute bars after the low: a 10–40 minute
  window, while cycles ran once an hour. A flush was visible only if it
  happened to land in one small slice of the hour.
- **Depth.** 1.2 ATRs below VWAP was several times deeper than any intraday
  drop in the record, including the drops the two winning trades had bought.

It proposed a new algorithm version that rescaled the confirmation window to
the hourly cadence (up to 14 bars, with the decay reshaped to match) and set
the depth gate at 0.30 ATR, with the two live cycles that exposed the problem
turned into unit-test fixtures. The lesson the loop keeps teaching: a rule that
reads well can still be silent in practice, and only the scored live record
shows it.
