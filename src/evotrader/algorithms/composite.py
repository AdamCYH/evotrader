"""Composite (ensemble) strategy.

The core strategy for EvoTrader — combines multiple strategies with configurable weights.
The Hybrid Scorer then blends this algorithmic composite signal with the LLM signal using
regime-dependent weights.
"""

from __future__ import annotations

import ast
import hashlib
import logging
from pathlib import Path
from typing import Any

from evotrader.algorithms.base import TradingAlgorithm
from evotrader.models.market import MarketRegime, MarketSnapshot
from evotrader.models.signals import AlgoSignal, CompositeAlgoSignal

logger = logging.getLogger(__name__)

# Below this magnitude a sub-signal is treated as not having voted. It is not
# a neutral opinion — it is silence, and silence must not be counted as
# participation or absorb ensemble weight.
SIGNAL_EPSILON = 1e-3


#: Every module whose code can change a composite value for the same snapshot
#: and config, relative to the ``evotrader`` package. ``units.py`` holds the
#: move-size rules six strategies share; it was outside the hash until
#: 2026-10-05, so a change to it would have moved composites silently.
_ENGINE_SOURCES = (
    "algorithms/composite.py",
    "algorithms/base.py",
    "algorithms/units.py",
    "algorithms/strategies",
    "indicators",
)


def _logic_dump(source: str) -> str:
    """The module's syntax tree without docstrings, so comment and docstring
    edits leave the fingerprint alone and only a change of behaviour moves it."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.dump(tree, include_attributes=False)


def _source_fingerprint() -> str:
    """Short hash of the signal engine's code, for build provenance.

    A deployed process can lag the repository, and when it does the composite
    arithmetic changes silently: every downstream conclusion is then measured
    against an engine nobody is looking at. This is not hypothetical. On
    2026-09-10 the running process was still renormalizing over the
    ``applicable`` pool while this file had renormalized over ``voting`` since
    2026-09-08, and the only way anyone noticed was by reconstructing a live
    cycle's arithmetic by hand and matching it to 9 significant figures.

    Emitting a fingerprint makes the running build identifiable straight from
    the logs, so a future session can read which engine produced a number
    instead of reverse-engineering it.

    It covers every sub-strategy and indicator module, not only this file: the
    2026-09-24 mean_reversion guard fix moved the composite by +0.04..+0.07 on
    every uptrend cycle without touching composite.py, and a hash of this file
    alone would have reported the same engine before and after.
    """
    try:
        root = Path(__file__).resolve().parents[1]
        digest = hashlib.sha256()
        for rel in _ENGINE_SOURCES:
            path = root / rel
            for f in sorted(path.rglob("*.py")) if path.is_dir() else [path]:
                digest.update(f.relative_to(root).as_posix().encode())
                digest.update(_logic_dump(f.read_text(encoding="utf-8")).encode())
        return digest.hexdigest()[:12]
    except (OSError, SyntaxError, ValueError):
        # Source may be unreadable (zipimport, frozen bundle). Provenance is a
        # diagnostic — never let its absence break signal generation.
        return "unknown"


COMPOSITE_SOURCE_FINGERPRINT = _source_fingerprint()


class CompositeStrategy(TradingAlgorithm):
    """Ensemble of multiple sub-strategies with configurable weighting.

    Each sub-strategy produces an independent signal in [-1, +1]. The
    composite signal is their weighted average. Weights can be:
    - Static (from config)
    - Regime-adaptive (different weights per detected regime)
    - Evolved (optimised by the Evolution Agent over time)

    When ``renormalize_on_abstain`` is True (the default), sub-strategies
    that mark themselves as not applicable (``metadata["applicable"] == False``)
    are excluded from the weighted average and the remaining weights are
    renormalized so that the composite is not silently attenuated by
    dead-weight zero signals.
    """

    def __init__(
        self,
        sub_strategies: dict[str, TradingAlgorithm],
        weights: dict[str, float],
        regime_adaptive: bool = True,
        version: str = "v001",
        regime_weights: dict[MarketRegime, dict[str, float]] | None = None,
        renormalize_on_abstain: bool = True,
        inverted_strategies: set[str] | None = None,
    ) -> None:
        self._regime_adaptive = regime_adaptive
        self._version = version
        self._strategies = sub_strategies
        self._renormalize_on_abstain = renormalize_on_abstain
        # Sub-strategies whose emitted value is negated before aggregation.
        #
        # Weights stay positive: the renormalization path sums them
        # (``w_total``) and caps each survivor at MAX_AMPLIFICATION x its
        # configured weight, and a negative weight would corrupt both. So an
        # inverted strategy keeps its weight and has its *value* flipped.
        #
        # Measured basis: fitting weights on QQQ's first year and testing on
        # nine unseen tickers in the second put momentum at a negative
        # coefficient in all four regimes (-0.44, -0.34, -0.28, -0.49). Mean
        # out-of-sample IC went from -0.035 (0/9 tickers positive) with the
        # shipped weights to +0.025 (7/9) with momentum inverted.
        self._inverted = set(inverted_strategies or ())
        if self._inverted:
            unknown = self._inverted - set(self._strategies)
            if unknown:
                logger.warning(
                    "Inverted strategies not present in this composite: %s",
                    ", ".join(sorted(unknown)),
                )
            logger.info("Inverting sub-signal(s): %s", ", ".join(sorted(self._inverted)))

        # Normalize main weights
        self._weights = {name: weights.get(name, 0.0) for name in self._strategies}
        total_w = sum(self._weights.values())
        if total_w > 0:
            self._weights = {k: v / total_w for k, v in self._weights.items()}
        else:
            self._weights = {k: 1.0 / len(self._strategies) for k in self._strategies}

        # Normalize and set regime weights
        if regime_weights is None:
            regime_weights = self._get_default_regime_weights_map()

        self._regime_weights_map = {}
        for r_type, w_dict in regime_weights.items():
            r_w = {name: w_dict.get(name, 0.0) for name in self._strategies}
            tot = sum(r_w.values())
            if tot > 0:
                self._regime_weights_map[r_type] = {k: v / tot for k, v in r_w.items()}
            else:
                self._regime_weights_map[r_type] = self._weights.copy()

        # With regime weighting on, the per-regime table replaces the flat
        # weights and a strategy missing from it counts as zero. A strategy
        # added with only a flat weight then runs, looks active in a backtest's
        # participation table, and never moves a trade. Listing it at 0 is a
        # deliberate choice (record, don't vote) and stays quiet.
        if regime_adaptive and regime_weights:
            unlisted = sorted(
                name
                for name in self._strategies
                if weights.get(name, 0.0) > 0
                and not any(name in w_dict for w_dict in regime_weights.values())
            )
            if unlisted:
                logger.warning(
                    "%s has a weight but no entry in composite.regime_weights, so it "
                    "counts as 0 in every regime and never votes. Add it to each "
                    "regime's weights (0 to record it without voting).",
                    ", ".join(unlisted),
                )

    @property
    def name(self) -> str:
        return "composite"

    @property
    def version(self) -> str:
        return self._version

    @property
    def inverted_strategies(self) -> set[str]:
        """Sub-strategies whose emitted value is negated before aggregation."""
        return set(self._inverted)

    @property
    def description(self) -> str:
        desc_parts = [
            f"{name}({weight:.0%}{'*' if name in self._inverted else ''})"
            for name, weight in self._weights.items()
        ]
        inv = f", inverted={sorted(self._inverted)}" if self._inverted else ""
        return f"Ensemble: {' + '.join(desc_parts)}, regime_adaptive={self._regime_adaptive}{inv}"

    def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
        """Compute ensemble signal by combining all sub-strategies.

        Delegates to ``compute_detailed_signal`` so that the ensemble
        math lives in exactly one place (no risk of divergence between
        the simple and detailed code paths).
        """
        detailed = self.compute_detailed_signal(snapshot)

        # Build metadata identical to the previous standalone implementation
        weights = self._get_regime_weights(snapshot.regime.regime)
        metadata: dict[str, float | str | bool] = {
            "regime": snapshot.regime.regime.value,
        }
        # ── BUILD PROVENANCE ─────────────────────────────────
        # Which engine produced this number. Without it, a deployed process
        # lagging the repo is detectable only by hand-reconstructing the
        # arithmetic from sub-signal metadata — which is how the 2026-09-10
        # deployment gap was eventually caught, three sessions late.
        metadata["composite_source_fingerprint"] = COMPOSITE_SOURCE_FINGERPRINT
        # Names the pool the renormalization actually used, so the
        # voting-vs-applicable behaviour is self-describing per cycle. A
        # pre-fix build cannot emit "voting" at all, which makes this a
        # one-glance check for the same gap.
        metadata["renormalization_pool"] = "voting" if detailed.n_voting else "applicable"
        for sig in detailed.signals:
            metadata[f"{sig.name}_signal"] = sig.value
            metadata[f"{sig.name}_weight"] = weights.get(sig.name, 0.0)

        if detailed.renormalized:
            metadata["renormalized"] = True
            # Log effective post-renormalization weights so performance
            # attribution can distinguish configured vs actual weighting.
            # When a strategy abstains, its weight is redistributed to
            # the remaining strategies proportionally.
            applicable_sigs = [
                s
                for s in detailed.signals
                if s.metadata.get("applicable", True) and s.metadata.get("role") != "multiplier"
            ]
            w_total = sum(s.weight for s in applicable_sigs)
            if w_total > 0:
                for s in applicable_sigs:
                    metadata[f"{s.name}_effective_weight"] = round(s.weight / w_total, 4)

        # Emit live-signal diagnostics so downstream agents can normalize
        # their "strong signal" judgement against what actually voted.
        additive_sigs = [
            s
            for s in detailed.signals
            if s.metadata.get("role") != "multiplier" and s.metadata.get("applicable", True)
        ]
        # An emission of 1.6e-4 is arithmetically non-zero but contributes
        # nothing. Counting it as a vote inflates apparent corroboration.
        non_zero = [s for s in additive_sigs if abs(s.value) >= SIGNAL_EPSILON]
        metadata["near_zero_signals"] = ", ".join(
            s.name for s in additive_sigs if 0.0 < abs(s.value) < SIGNAL_EPSILON
        )
        metadata["live_signal_count"] = len(non_zero)
        metadata["total_signal_count"] = len(additive_sigs)
        # effective_max_magnitude: sum of |weights| of non-zero-emitting
        # applicable strategies — the max |composite| if all non-zero
        # strategies were maximally aligned at ±1.0.
        metadata["effective_max_magnitude"] = round(
            sum(weights.get(s.name, 0.0) for s in non_zero), 4
        )

        # Scale-free conviction: a composite of +0.13 when only 2 of 7
        # sub-signals voted is NOT proportionally 'strong'. Emitting this
        # directly prevents the documented mis-sizing failure where a
        # low-participation composite was labelled STRONG and sized at
        # 40% of buying power (trade #43).
        eff_max = metadata["effective_max_magnitude"]
        metadata["normalized_conviction"] = (
            round(detailed.composite_value / eff_max, 4)
            if isinstance(eff_max, float) and eff_max > 0
            else 0.0
        )

        # Authoring signal: empirically the single strongest predictor of
        # outcome is WHICH sub-signal carried the entry.
        if non_zero:
            author = max(
                non_zero,
                key=lambda s: abs(s.value) * weights.get(s.name, 0.0),
            )
            metadata["authoring_signal"] = author.name
            metadata["authoring_contribution"] = round(
                author.value * weights.get(author.name, 0.0), 4
            )

        # Participation ratio: fraction of additive sub-signals that were
        # even ABLE to vote.  A permanently low value indicates dead data
        # plumbing rather than genuine market-driven abstention.
        #
        # These counts come from compute_detailed_signal rather than being
        # rebuilt here. They were previously recomputed with near-identical but
        # separate comprehensions, so a future edit to one filter and not the
        # other would have left this diagnostic quietly disagreeing with the
        # arithmetic it claims to describe.
        if detailed.n_additive:
            _participation = detailed.n_applicable / detailed.n_additive
            metadata["participation_ratio"] = round(_participation, 3)
            # Distinct from participation_ratio: of the channels able to vote,
            # how many spoke, shadow channels included. Silence and
            # inapplicability are different failures and are reported
            # separately. The attenuation's own counts are
            # participation_numerator and participation_denominator below.
            metadata["voting_ratio"] = (
                round(detailed.n_voting / detailed.n_applicable, 3)
                if detailed.n_applicable
                else 0.0
            )
            _abstaining = [
                s.name
                for s in detailed.signals
                if s.metadata.get("role") != "multiplier" and not s.metadata.get("applicable", True)
            ]
            metadata["abstaining_signals"] = ", ".join(_abstaining)
            # Off-duty channels: applicable, but structurally unable to fire in
            # this regime. Reported separately so "nobody spoke" can be read
            # apart from "nobody was on duty" without re-deriving it.
            metadata["off_duty_signals"] = ", ".join(
                s.name
                for s in detailed.signals
                if s.metadata.get("role") != "multiplier"
                and s.metadata.get("applicable", True)
                and s.metadata.get("in_scope", True) is False
                and abs(s.value) < SIGNAL_EPSILON
            )
            metadata["in_scope_count"] = detailed.n_in_scope
            # On duty, only early: still collecting the session bars they need.
            metadata["warming_up_signals"] = ", ".join(
                s.name
                for s in detailed.signals
                if s.metadata.get("role") != "multiplier"
                and not s.metadata.get("applicable", True)
                and s.metadata.get("warming_up") is True
            )
            # The counts the attenuation used (weighted channels only) and the
            # factor it applied.
            metadata["participation_numerator"] = detailed.participation_numerator
            metadata["participation_denominator"] = detailed.participation_denominator
            metadata["participation_scale"] = round(detailed.participation_scale, 4)
            # Where MAX_AMPLIFICATION actually bound. Empty on most cycles; when
            # it lists EVERY pool member the cap divided out and did nothing.
            metadata["amplification_capped"] = ", ".join(detailed.amplification_capped)
            if _participation < 0.5:
                logger.warning(
                    "Composite participation %.0f%% — %d/%d sub-signals "
                    "abstained (%s). Verify input data plumbing.",
                    _participation * 100,
                    detailed.n_additive - detailed.n_applicable,
                    detailed.n_additive,
                    ", ".join(_abstaining),
                )

        return AlgoSignal(
            name=self.name,
            value=detailed.composite_value,
            weight=1.0,
            metadata=metadata,
        )

    def compute_detailed_signal(self, snapshot: MarketSnapshot) -> CompositeAlgoSignal:
        """Compute ensemble signal with full sub-signal detail.

        Sub-strategies are split into two groups:

        1. **Additive** (default) — combined via weighted average.  When
           ``renormalize_on_abstain`` is enabled, non-applicable signals are
           excluded and remaining weights are renormalized.
        2. **Overlay** (``metadata["role"] == "multiplier"``) — excluded from
           the weighted average entirely.  Instead, each applicable overlay's
           ``metadata["factor"]`` is applied as a post-aggregation multiplier
           on the composite value (clamped to [0, 1.5]).  This makes gating
           semantics (e.g. event-window blackout) enforceable.
        """
        weights = self._get_regime_weights(snapshot.regime.regime)
        sub_signals = []

        for name, strategy in self._strategies.items():
            sig = strategy.compute_signal(snapshot)
            value = sig.value
            # Declared INPUT resolution, surfaced so the composite's witness mix
            # is countable. A daily-resolution channel re-emits the same reading
            # every hourly cycle; on 2026-09-17 two of them held 0.31 of
            # trending_bull weight while varying by less than 0.007 all day.
            # Emitted only — nothing attenuates on it yet.
            metadata = {
                **sig.metadata,
                "resolution": getattr(strategy, "resolution", "daily"),
            }
            if name in self._inverted:
                # Record the raw emission so attribution can still score the
                # strategy's own output, not just the inverted contribution.
                metadata = {**metadata, "inverted": True, "raw_value": sig.value}
                value = -value
            weighted_sig = AlgoSignal(
                name=sig.name,
                value=value,
                weight=weights.get(name, 0.0),
                metadata=metadata,
            )
            sub_signals.append(weighted_sig)

        # Split overlay (multiplicative) strategies from additive ones.
        # Overlays declare role='multiplier' in metadata and, when applicable,
        # carry a 'factor' that scales the composite post-aggregation.
        overlays = [
            s
            for s in sub_signals
            if s.metadata.get("role") == "multiplier" and s.metadata.get("applicable", True)
        ]
        additive = [s for s in sub_signals if s.metadata.get("role") != "multiplier"]

        # Renormalize: exclude abstaining additive sub-signals and rescale weights
        renormalized = False
        amplification_capped: list[str] = []
        applicable = [s for s in additive if s.metadata.get("applicable", True)]

        # A channel that is applicable but emitted ~0.0 is dead weight: it adds
        # nothing to the numerator while absorbing its full share of the
        # denominator. In range_bound this was structural rather than
        # occasional — range_break_continuation (0.18), swing_failure_reversal
        # (0.12) and trend_persistence (0.12) emitted 0.0 in 10 of 10 live
        # cycles, and intraday_vwap_zscore (0.25) in 9 of 10, so two thirds of
        # ensemble authority sat with channels that never spoke. Measured
        # effect: with momentum at its observed 0.123 the composite reached
        # 0.024 against a 0.05 entry threshold; momentum would have needed
        # 0.356, roughly 3x its observed ceiling. The regime was mathematically
        # incapable of producing an entry.
        #
        # Renormalizing over the channels that actually voted is strictly
        # expansionary — it can only increase |composite| — so it does not
        # conflict with the anti-conservatism mandate.
        voting = [s for s in applicable if abs(s.value) >= SIGNAL_EPSILON]
        # Fall back to `applicable` when nothing voted, so a fully silent
        # ensemble still takes the normal path and yields ~0 rather than
        # dividing by an empty pool.
        pool = voting if voting else applicable

        if self._renormalize_on_abstain and pool and len(pool) < len(additive):
            w_total = sum(s.weight for s in pool)
            # Guard against abstain-cascade amplification: renormalizing
            # over a small surviving set can scale a deliberately-small
            # weight far beyond its intended authority.  Cap each
            # survivor's effective weight at MAX_AMPLIFICATION × its
            # configured weight so evolution decisions are preserved.
            #
            # CAVEAT — the cap is applied BEFORE normalisation, so it is
            # self-cancelling when it binds on every survivor. Worked example
            # (cycle 8, 2026-09-10): momentum w 0.22 and mean_reversion w 0.12,
            # w_total 0.34 -> raw_eff 0.647 and 0.353, caps 0.55 and 0.30. Both
            # bind, eff_weights {0.55, 0.30}, eff_total 0.85, final weights
            # 0.647 and 0.353 — precisely the uncapped values. The guard only
            # bites when it binds on a strict SUBSET of survivors.
            #
            # Genuinely limiting a lone survivor's authority would mean capping
            # the FINAL effective weight, after normalisation. That changes
            # behaviour and is deliberately not done here — it belongs in a
            # separate, reasoned change, not folded in as a cleanup.
            # `amplification_capped` is emitted so the guard's real influence
            # is countable instead of assumed.
            MAX_AMPLIFICATION = 2.5
            if w_total > 0:
                eff_weights: dict[str, float] = {}
                for s in pool:
                    raw_eff = s.weight / w_total
                    capped_eff = s.weight * MAX_AMPLIFICATION
                    if capped_eff < raw_eff:
                        amplification_capped.append(s.name)
                    eff_weights[s.name] = min(raw_eff, capped_eff)
                eff_total = sum(eff_weights.values())
                composite_value = (
                    sum(s.value * eff_weights[s.name] for s in pool) / eff_total
                    if eff_total > 0
                    else 0.0
                )
            else:
                composite_value = 0.0
            renormalized = True
            log_parts = [f"{s.name}={s.value:.3f}(w={s.weight:.2f})" for s in pool]
            logger.debug(
                "Composite signal (renormalized, %d/%d voting of %d applicable): %s = %.3f",
                len(pool),
                len(additive),
                len(applicable),
                " + ".join(log_parts),
                composite_value,
            )
        else:
            composite_value = sum(s.value * s.weight for s in additive)
            log_parts = [f"{s.name}={s.value:.3f}(w={s.weight:.2f})" for s in additive]
            logger.debug(
                "Composite signal: %s = %.3f",
                " + ".join(log_parts),
                composite_value,
            )

        # Apply overlay gates/amplifiers (e.g., event window timing).
        # Each overlay's factor scales the composite multiplicatively.
        for ov in overlays:
            factor = float(ov.metadata.get("factor", 1.0))
            factor = max(0.0, min(1.5, factor))
            logger.debug(
                "Overlay %s: factor=%.3f applied to composite %.3f",
                ov.name,
                factor,
                composite_value,
            )
            composite_value *= factor

        # ── LOW-PARTICIPATION ATTENUATION ────────────────────
        # Captured before the scale is applied so a caller can compare this
        # cycle against another hour without re-deriving the denominator. See
        # CompositeAlgoSignal.composite_unattenuated.
        composite_unattenuated = composite_value
        participation_scale = 1.0
        # renormalize_on_abstain rescales surviving weights to sum to 1,
        # so a lone survivor speaks with the authority of the full
        # ensemble.  Scale the composite down when participation is thin
        # so magnitude reflects corroboration, not just data presence.
        in_scope = [
            s
            for s in applicable
            if s.metadata.get("in_scope", True) or abs(s.value) >= SIGNAL_EPSILON
        ]
        # On duty but early: not yet applicable only because the session has
        # not produced the bars the channel needs, which it will before the
        # close (base.warming_up). Counted in the denominator below so the
        # scale does not change with the hour.
        warming = [
            s
            for s in additive
            if not s.metadata.get("applicable", True) and s.metadata.get("warming_up") is True
        ]

        MIN_PARTICIPATION = 0.4
        participation_numerator = 0
        participation_denominator = 0
        if additive:
            # Drive participation off signals that actually emitted a
            # directional read, relative to channels able to vote this cycle.
            # Counting structurally dark channels (e.g. gap, options) diluted
            # the denominator and applied an artificial ~0.55x haircut.
            #
            # The denominator is `applicable`, NOT the `pool` used for
            # renormalization above. That difference is deliberate and load
            # bearing: `pool` excludes silent channels so they stop diluting
            # the weighted average, but participation must still ask "how many
            # channels that COULD have spoken did?". Switching this denominator
            # to `voting` would make the ratio identically 1.0 and delete the
            # safeguard that stops a lone survivor speaking with the authority
            # of the whole ensemble.
            # ── OFF-DUTY vs SILENT ───────────────────────────
            # A channel can report applicable=True and emit 0.0 for two very
            # different reasons: it evaluated the tape and found nothing, or
            # its preconditions do not exist in this regime at all. Only the
            # first is an absent witness. range_break_continuation needs price
            # OUTSIDE a Bollinger band and trend_persistence needs a
            # multi-session grind; in a quiet range neither can fire no matter
            # what the market does, yet both were counted as channels that
            # looked and saw nothing — 0-for-18 across the 2026-09-10 census.
            # On cycle 8 that inflated the denominator enough to push
            # participation under MIN_PARTICIPATION and apply an unearned
            # 0.833x haircut.
            #
            # Channels marking in_scope=False are excluded here, exactly as
            # applicable=False channels are already excluded from the
            # renormalization pool. This is NOT a demotion: when a real break
            # or grind appears they fire at full authority and rejoin the
            # denominator automatically.
            #
            # A channel that VOTED is always counted in scope regardless of its
            # marker, so participation can never exceed 1.0 even if a strategy
            # emits a contradictory pair.
            #
            # ── ONLY CHANNELS WITH WEIGHT COUNT ──────────────
            # A shadow channel (weight 0.0 in this regime) is recorded in the
            # snapshot but carries no authority, so it is neither a witness
            # when it votes nor an absent one when it is silent. Counting it
            # moved the headline with every weighted input unchanged: with two
            # weighted channels voting and three silent shadows in scope, 2 of
            # 8 scaled the composite by 0.625; one shadow voting turned that
            # into 3 of 8 and 0.9375; on the weighted channels alone it is 2 of
            # 5 and no scale. Silent shadows did most of the damage, because
            # they sit in the denominator on every regular-hours cycle.
            #
            # ── WARMING UP COUNTS ────────────────────────────
            # A weighted channel still collecting its session bars is on duty
            # (base.warming_up), so it is in the denominator from the first
            # cycle that can measure the bar pace. Without it the same two
            # votes read 2 of 4 at 10:30 ET and 2 of 5 at 11:30, because a
            # 20-bar channel fills at 11:10 on five-minute bars.
            #
            # With no weighted channel on duty (a configuration that weights
            # only channels that cannot speak) the composite is zero whatever
            # the scale, and the count falls back to every channel as before.
            duty = [*in_scope, *warming]
            weighted_duty = [s for s in duty if s.weight > 0]
            if weighted_duty:
                participation_numerator = sum(1 for s in voting if s.weight > 0)
                participation_denominator = len(weighted_duty)
            else:
                participation_numerator = len(voting)
                participation_denominator = (
                    len(in_scope)
                    if in_scope
                    else (len(applicable) if applicable else len(additive))
                )
            participation = (
                participation_numerator / participation_denominator
                if participation_denominator
                else 0.0
            )
            if participation < MIN_PARTICIPATION:
                participation_scale = participation / MIN_PARTICIPATION
                composite_value *= participation_scale

        composite_value = max(-1.0, min(1.0, composite_value))

        return CompositeAlgoSignal(
            signals=sub_signals,
            composite_value=composite_value,
            algo_version=f"{self.name}_{self._version}",
            renormalized=renormalized,
            amplification_capped=amplification_capped,
            # The pools this method actually used, so callers report the same
            # denominators the arithmetic applied instead of recomputing them.
            n_additive=len(additive),
            n_applicable=len(applicable),
            n_voting=len(voting),
            n_in_scope=len(in_scope),
            composite_unattenuated=max(-1.0, min(1.0, composite_unattenuated)),
            participation_scale=participation_scale,
            participation_numerator=participation_numerator,
            participation_denominator=participation_denominator,
        )

    def _get_regime_weights(self, regime: MarketRegime) -> dict[str, float]:
        """Get strategy weights adjusted for the current market regime."""
        if not self._regime_adaptive:
            return self._weights

        return self._regime_weights_map.get(regime, self._weights)

    def get_parameters(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "regime_adaptive": self._regime_adaptive,
            "renormalize_on_abstain": self._renormalize_on_abstain,
        }
        for name in self._strategies:
            params[f"{name}_weight"] = self._weights.get(name, 0.0)
        for name, strategy in self._strategies.items():
            params[name] = strategy.get_parameters()
        return params

    def set_parameters(self, params: dict[str, Any]) -> None:
        if "regime_adaptive" in params:
            self._regime_adaptive = bool(params["regime_adaptive"])
        if "renormalize_on_abstain" in params:
            self._renormalize_on_abstain = bool(params["renormalize_on_abstain"])

        for name in self._strategies:
            weight_key = f"{name}_weight"
            if weight_key in params:
                self._weights[name] = float(params[weight_key])
            if name in params and isinstance(params[name], dict):
                self._strategies[name].set_parameters(params[name])

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        weights = []
        for name in self._strategies:
            weight_key = f"{name}_weight"
            if weight_key in params:
                weights.append(float(params[weight_key]))
            else:
                weights.append(self._weights.get(name, 0.0))

        total = sum(weights)
        if abs(total - 1.0) > 0.01:
            errors.append(f"Strategy weights must sum to 1.0, got {total:.4f}")

        for name, strategy in self._strategies.items():
            if name in params and isinstance(params[name], dict):
                errors.extend(strategy.validate_parameters(params[name]))

        return errors

    @staticmethod
    def _get_default_regime_weights_map() -> dict[MarketRegime, dict[str, float]]:
        return {
            MarketRegime.TRENDING_BULL: {
                "momentum": 0.43,
                "mean_reversion": 0.18,
                "gap": 0.17,
                "intraday_vwap_zscore": 0.10,
                "event_window_timing": 0.02,
                "range_break_continuation": 0.0,
                "options_positioning": 0.10,
                "swing_failure_reversal": 0.0,
                "trend_persistence": 0.0,
                "vwap_reclaim_continuation": 0.0,
                "gap_fail_continuation": 0.0,
                "vwap_reclaim_fade": 0.0,
            },
            MarketRegime.TRENDING_BEAR: {
                "momentum": 0.43,
                "mean_reversion": 0.18,
                "gap": 0.17,
                "intraday_vwap_zscore": 0.10,
                "event_window_timing": 0.02,
                "range_break_continuation": 0.0,
                "options_positioning": 0.10,
                "swing_failure_reversal": 0.0,
                "trend_persistence": 0.0,
                "vwap_reclaim_continuation": 0.0,
                "gap_fail_continuation": 0.0,
                "vwap_reclaim_fade": 0.0,
            },
            MarketRegime.RANGE_BOUND: {
                "momentum": 0.0,
                "mean_reversion": 0.10,
                "gap": 0.0,
                "intraday_vwap_zscore": 0.40,
                "event_window_timing": 0.05,
                "range_break_continuation": 0.15,
                "options_positioning": 0.15,
                "swing_failure_reversal": 0.15,
                "trend_persistence": 0.0,
                "vwap_reclaim_continuation": 0.0,
                "gap_fail_continuation": 0.0,
                "vwap_reclaim_fade": 0.0,
            },
            MarketRegime.HIGH_VOLATILITY: {
                "momentum": 0.47,
                "mean_reversion": 0.13,
                "gap": 0.17,
                "intraday_vwap_zscore": 0.10,
                "event_window_timing": 0.03,
                "range_break_continuation": 0.0,
                "options_positioning": 0.10,
                "swing_failure_reversal": 0.0,
                "trend_persistence": 0.0,
                "vwap_reclaim_continuation": 0.0,
                "gap_fail_continuation": 0.0,
                "vwap_reclaim_fade": 0.0,
            },
        }
