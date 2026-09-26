*Example output of the evolution loop, lightly edited and anonymised.*

The evolution agent wrote this **code review** after reading the algorithm's
source and the numbers it produced in recent live cycles. It found that the
composite signal — the weighted average of every strategy's vote — was being
quietly shrunk by a strategy that had nothing to say. A human approved it, and
all three findings were implemented; the note at the end says where.

Anonymised: this review contained no trades, prices or account details, so it
is almost unchanged; references to earlier algorithm versions are generalised.

A few terms: the *gap* strategy trades the jump between one day's close and the
next day's open; a *regime* is the market state the weights are chosen for
(*range_bound* means a market going sideways); *IBS* (internal bar strength)
says where the price closed within the day's range.

---

# Code Review: composite_deadweight_dilution_and_gap_signal_lifecycle

**Generated**: 2026-07-13T21:04:54Z
**Status**: APPLIED

## Files Reviewed

- `src/evotrader/algorithms/composite.py`
- `src/evotrader/algorithms/strategies/gap.py`
- `src/evotrader/models/market.py`

## Findings

### 1. [HIGH] src/evotrader/algorithms/composite.py

**Issue**: Dead-weight dilution: compute_signal / compute_detailed_signal take a
weighted average over ALL sub-strategies, including ones that report a
structurally inapplicable 0.0 signal (gap returns exactly 0.0 with
gap_type='none' on every intraday cycle without a >=0.3% overnight gap). In
range_bound, gap holds 20-30% weight, so on ~90%+ of hourly cycles the
composite magnitude is silently attenuated by that factor. Observed in all 10
recent cycles: gap=0.0000 while momentum/MR agreed bearish; composite -0.0706
instead of ~-0.10. This inflates the effective entry threshold by ~43% and
systematically suppresses valid trades (invisible losses), while also making
promoted regime-weight tunings misleading (the true live weighting is whatever
remains after dead weight).

**Suggestion**: Distinguish 'no opinion / not applicable' from 'genuinely
neutral'. Add an `applicable: bool` (or `abstain`) field to AlgoSignal metadata
(gap sets applicable=False when gap_type=='none'). In CompositeStrategy,
renormalize weights over the applicable sub-signals only: composite =
sum(w_i * s_i for applicable) / sum(w_i for applicable). Keep the current
behavior as a config flag (renormalize_on_abstain: true) so the change is
backtestable and reversible. Log both raw and renormalized values in metadata
for attribution.

### 2. [MEDIUM] src/evotrader/algorithms/strategies/gap.py

**Issue**: Gap signal has no intraday lifecycle: when a qualifying gap exists,
the fade signal is computed from the static overnight gap_pct and persists at
(roughly) constant strength for the entire session — the same
stale-daily-input pathology as the IBS bug fixed in earlier algorithm versions.
The 'gap_filling' check only halves the signal and its condition is nearly
always true for gap-ups (daily_change < gap_pct is satisfied by any intraday
pullback), making the 0.5 dampener effectively a constant rather than a fill
detector. Also, `opening_range_minutes` is accepted, stored, and exposed as a
tunable parameter but never used — the documented opening-range-breakout logic
does not exist, so tuning it is a no-op (misleading for parameter evolution).

**Suggestion**: 1) Decay the fade signal by elapsed session time (e.g., linear
or exponential decay to 0 by ~2h after open) since gap-fill edge is
concentrated in the first 1-2 hours. 2) Fix gap_filling: measure remaining
unfilled gap fraction, e.g. remaining = 1 - clamp(fill_progress, 0, 1) where
fill_progress = (gap_pct - daily_change)/gap_pct for gap-ups, and scale the
signal by `remaining`, going to 0 when the gap is fully filled (currently a
filled gap still emits half-strength fade). 3) Either implement the
opening-range-breakout component using recent_candles or remove
opening_range_minutes from the parameter surface.

### 3. [LOW] src/evotrader/algorithms/composite.py

**Issue**: compute_signal and compute_detailed_signal duplicate the ensemble
math; any renormalization fix must be applied in both places or they will
diverge (attribution shown to agents would disagree with the traded signal).

**Suggestion**: Refactor compute_signal to delegate to compute_detailed_signal
(compute once, project to AlgoSignal), so the ensemble math lives in exactly
one place.

## Proposed Changes

### src/evotrader/algorithms/composite.py

Renormalize ensemble weights over applicable (non-abstaining) sub-signals;
single source of truth for ensemble math.

```diff
@@ compute_detailed_signal @@
-        composite_value = sum(s.value * s.weight for s in sub_signals)
-        composite_value = max(-1.0, min(1.0, composite_value))
+        applicable = [s for s in sub_signals if s.metadata.get("applicable", True)]
+        if self._renormalize_on_abstain and applicable and len(applicable) < len(sub_signals):
+            w_total = sum(s.weight for s in applicable)
+            composite_value = (
+                sum(s.value * s.weight for s in applicable) / w_total if w_total > 0 else 0.0
+            )
+        else:
+            composite_value = sum(s.value * s.weight for s in sub_signals)
+        composite_value = max(-1.0, min(1.0, composite_value))
@@ __init__ @@
+        # Config-gated so the change is backtestable/reversible
+        self._renormalize_on_abstain = renormalize_on_abstain
@@ compute_signal @@
-        # (existing duplicated weighted-average loop)
+        detailed = self.compute_detailed_signal(snapshot)
+        # project CompositeAlgoSignal -> AlgoSignal (metadata built from detailed.signals)
```

### src/evotrader/algorithms/strategies/gap.py

Mark no-gap cycles as abstentions; add intraday decay and true fill-fraction
scaling.

```diff
@@ compute_signal @@
         if gap_pct is None or abs(gap_pct) < self._min_gap_pct:
             return AlgoSignal(
                 name=self.name,
                 value=0.0,
                 weight=1.0,
-                metadata={"gap_pct": gap_pct or 0.0, "gap_type": "none"},
+                metadata={"gap_pct": gap_pct or 0.0, "gap_type": "none", "applicable": False},
             )
@@ fade branch @@
-            gap_filling = (gap_pct > 0 and daily_change < gap_pct) or (
-                gap_pct < 0 and daily_change > gap_pct
-            )
-            if gap_filling:
-                fade_signal *= 0.5
+            # Scale by the unfilled fraction of the gap (0 when fully filled)
+            fill_progress = (gap_pct - daily_change) / gap_pct if gap_pct != 0 else 0.0
+            remaining = max(0.0, min(1.0, 1.0 - fill_progress))
+            fade_signal *= remaining
+            # Decay edge over session time: gap-fill alpha concentrates in first ~2h
+            minutes_since_open = _session_minutes(snapshot.timestamp)
+            if minutes_since_open is not None:
+                fade_signal *= max(0.0, 1.0 - minutes_since_open / 120.0)
```

---

## What happened next (editor's note, not part of the review)

All three findings were implemented, each with regression tests:

- The composite engine now shares a silent strategy's weight among the
  strategies that voted (`renormalize_on_abstain`, on by default, in
  [`composite.py`](../../src/evotrader/algorithms/composite.py)). The idea
  was later tightened: a vote too small to matter is also treated as silence
  (`SIGNAL_EPSILON`), because silence is not a neutral opinion.
- The gap strategy marks cycles without a real gap as not applicable, scales
  its signal by how much of the gap is still unfilled, and fades it out over
  the first two hours of the session; the unused `opening_range_minutes`
  parameter is no longer offered for tuning, though old configs that set it
  still load
  ([`gap.py`](../../src/evotrader/algorithms/strategies/gap.py)).
- `compute_signal` now delegates to `compute_detailed_signal`, so the signal
  the agents see and the signal the system trades on come from the same code.

The suggested diffs were a starting point, not a patch: the implementation
follows the findings and adapts the code to what the files actually looked
like, as the `self-evolve` skill asks.
