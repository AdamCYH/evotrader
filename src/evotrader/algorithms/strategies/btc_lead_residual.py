"""BTC lead residual: the primary's move since its prior close against its bitcoin proxy's.

A bitcoin-holding stock re-prices only in its own sessions, while bitcoin and
the instruments that track it re-price around it. Every other channel reads
the primary's own tape; this one reads a second instrument, configured as
``asset.reference_ticker`` (a spot bitcoin fund, say), whose quote and daily
closes the market-data tool puts on the snapshot as ``reference``.

The residual is the primary's return since its prior close minus beta times
the reference's return since its own prior close (the same clock interval for
two instruments that close at the same time), in the primary's ATR. Beta is
the slope of the primary's daily returns on the reference's over the last
``beta_window`` completed sessions, clamped to ``[beta_min, beta_max]``; the
channel abstains when that fit explains little (R squared under
``beta_r2_floor``). Past ``entry_atr`` it votes toward convergence: long when
the primary lags what the reference implies, short when it leads, so it can
call either side.

Gates: a reference quote older than ``max_reference_age_minutes`` (a quiet
pre-market) and the first ``min_minutes_since_open`` minutes of the session
(the opening auction) abstain. At the session's extremes the vote is scaled by
``extreme_dampener``: a long with the session's IBS at or below
``extreme_ibs``, a short at or above one minus it, as for
``intraday_vwap_zscore``. Without a reference on the snapshot it abstains
(``no_reference_feed``), never a zero that reads like a vote.

SHADOW at first: weight 0 in every regime (the loader's default for a version
that does not list it), so it is recorded in every snapshot and scored in the
calibration record without moving the composite.
"""

from __future__ import annotations

import math
from typing import Any

from evotrader.algorithms.base import TradingAlgorithm
from evotrader.models.market import MarketSnapshot
from evotrader.models.signals import AlgoSignal
from evotrader.tools.market_hours import ET, minutes_since_open

_DEFAULTS: dict[str, float] = {
    "entry_atr": 0.35,
    "scale_atr": 0.5,
    "base_strength": 0.7,
    "beta_window": 20,
    "beta_min": 1.0,
    "beta_max": 3.0,
    "beta_r2_floor": 0.3,
    "max_reference_age_minutes": 15.0,
    "min_minutes_since_open": 15.0,
    "extreme_ibs": 0.15,
    "extreme_dampener": 0.5,
}

#: Fewer aligned daily returns than this and there is no beta to speak of.
_MIN_PAIRS = 5


def _day(value: Any) -> str | None:
    """The calendar date of a daily bar, ``YYYY-MM-DD``, as its provider stamped it (UTC)."""
    if hasattr(value, "utctimetuple"):
        t = value.utctimetuple()
        return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"
    text = str(value or "")[:10]
    return text if len(text) == 10 and text[4] == "-" and text[7] == "-" else None


def _session_day(snapshot: MarketSnapshot) -> str:
    """The snapshot's US Eastern date; a naive time is Eastern, as minutes_since_open reads it."""
    ts = snapshot.timestamp
    return (ts if ts.tzinfo is None else ts.astimezone(ET)).date().isoformat()


def daily_beta(
    primary: dict[str, float], reference: dict[str, float], window: int
) -> tuple[float | None, float, int]:
    """Slope and R squared of the primary's daily returns on the reference's.

    Keyed by ``YYYY-MM-DD``. Returns over consecutive dates both series have,
    the last ``window`` of them. ``(None, 0.0, n)`` with fewer than five, or no
    variance.
    """
    days = sorted(set(primary) & set(reference))
    pairs = [
        (reference[b] / reference[a] - 1.0, primary[b] / primary[a] - 1.0)
        for a, b in (days[i - 1 : i + 1] for i in range(1, len(days)))
        if reference[a] > 0 and primary[a] > 0
    ][-window:]
    n = len(pairs)
    if n < _MIN_PAIRS:
        return None, 0.0, n
    mx = sum(x for x, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    syy = sum((y - my) ** 2 for _, y in pairs)
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    if sxx <= 0 or syy <= 0:
        return None, 0.0, n
    return sxy / sxx, sxy * sxy / (sxx * syy), n


class BtcLeadResidualStrategy(TradingAlgorithm):
    """Votes toward convergence of the primary with its bitcoin proxy."""

    def __init__(
        self,
        entry_atr: float = _DEFAULTS["entry_atr"],
        scale_atr: float = _DEFAULTS["scale_atr"],
        base_strength: float = _DEFAULTS["base_strength"],
        beta_window: int = int(_DEFAULTS["beta_window"]),
        beta_min: float = _DEFAULTS["beta_min"],
        beta_max: float = _DEFAULTS["beta_max"],
        beta_r2_floor: float = _DEFAULTS["beta_r2_floor"],
        max_reference_age_minutes: float = _DEFAULTS["max_reference_age_minutes"],
        min_minutes_since_open: float = _DEFAULTS["min_minutes_since_open"],
        extreme_ibs: float = _DEFAULTS["extreme_ibs"],
        extreme_dampener: float = _DEFAULTS["extreme_dampener"],
        version: str = "v001",
    ) -> None:
        self._params = {
            "entry_atr": float(entry_atr),
            "scale_atr": float(scale_atr),
            "base_strength": float(base_strength),
            "beta_window": int(beta_window),
            "beta_min": float(beta_min),
            "beta_max": float(beta_max),
            "beta_r2_floor": float(beta_r2_floor),
            "max_reference_age_minutes": float(max_reference_age_minutes),
            "min_minutes_since_open": float(min_minutes_since_open),
            "extreme_ibs": float(extreme_ibs),
            "extreme_dampener": float(extreme_dampener),
        }
        self._version = version

    @property
    def name(self) -> str:
        return "btc_lead_residual"

    @property
    def version(self) -> str:
        return self._version

    @property
    def resolution(self) -> str:
        """Intraday: reads the live quote against the prior close every cycle."""
        return "intraday"

    @property
    def description(self) -> str:
        return (
            "Primary vs its bitcoin proxy since the prior close, beta-adjusted, "
            f"toward convergence past {self._params['entry_atr']:g} ATR"
        )

    def _abstain(self, reason: str, **extra: Any) -> AlgoSignal:
        return AlgoSignal(
            name=self.name,
            value=0.0,
            weight=1.0,
            metadata={"applicable": False, "resolution": "intraday", "reason": reason, **extra},
        )

    def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
        p = self._params
        ref = (getattr(snapshot, "model_extra", None) or {}).get("reference") or {}
        ref_price = ref.get("price")
        ref_prior = ref.get("previous_close")
        ref_closes = ref.get("daily_closes") or []
        if not ref_price or not ref_prior or not ref_closes:
            return self._abstain("no_reference_feed")
        age = ref.get("quote_age_minutes")
        if age is not None and age > p["max_reference_age_minutes"]:
            return self._abstain(
                "stale_reference", reference_ticker=ref.get("ticker"), reference_age_minutes=age
            )

        atr = snapshot.indicators.atr_14
        price = snapshot.quote.last
        prior = snapshot.quote.previous_close
        if not atr or atr <= 0 or not price or price <= 0 or not prior or prior <= 0:
            return self._abstain("missing_inputs")
        elapsed = minutes_since_open(snapshot.timestamp)
        if elapsed is not None and elapsed < p["min_minutes_since_open"]:
            return self._abstain("opening_auction_window", minutes_since_open=round(elapsed, 1))

        today = _session_day(snapshot)
        primary = {
            d: c.close
            for c in snapshot.daily_candles
            if (d := _day(c.timestamp)) is not None and d < today and c.close > 0
        }
        reference = {
            d: float(c["close"])
            for c in ref_closes
            if (d := _day(c.get("date"))) is not None and d < today and c.get("close")
        }
        beta, r2, pairs = daily_beta(primary, reference, int(p["beta_window"]))
        if beta is None:
            return self._abstain("beta_unavailable", aligned_returns=pairs)
        if r2 < p["beta_r2_floor"]:
            return self._abstain(
                "beta_unreliable", beta=round(beta, 3), r2=round(r2, 3), aligned_returns=pairs
            )
        beta_c = min(max(beta, p["beta_min"]), p["beta_max"])

        r_primary = price / prior - 1.0
        r_reference = float(ref_price) / float(ref_prior) - 1.0
        residual_atr = (r_primary - beta_c * r_reference) * price / atr
        meta: dict[str, Any] = {
            "applicable": True,
            "resolution": "intraday",
            "reference_ticker": ref.get("ticker"),
            "reference_price": round(float(ref_price), 4),
            "reference_previous_close": round(float(ref_prior), 4),
            "reference_age_minutes": age,
            "beta": round(beta, 3),
            "beta_clamped": round(beta_c, 3),
            "r2": round(r2, 3),
            "aligned_returns": pairs,
            "r_primary_pct": round(100.0 * r_primary, 3),
            "r_reference_pct": round(100.0 * r_reference, 3),
            "residual_atr": round(residual_atr, 4),
            "entry_atr": p["entry_atr"],
        }
        if abs(residual_atr) < p["entry_atr"]:
            meta.update(in_scope=False, out_of_scope_reason="within_band")
            return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)

        depth = min((abs(residual_atr) - p["entry_atr"]) / p["scale_atr"], 1.0)
        # Lagging (below what the reference implies) -> long; leading -> short.
        value = -math.copysign(depth * p["base_strength"], residual_atr)
        ibs_raw = snapshot.indicators.ibs
        ibs = None if ibs_raw is None else max(0.0, min(1.0, float(ibs_raw)))
        band = p["extreme_ibs"]
        extreme = ibs is not None and (
            (value > 0 and ibs <= band) or (value < 0 and ibs >= 1.0 - band)
        )
        if extreme:
            value *= p["extreme_dampener"]
        meta.update(
            in_scope=True,
            leg="lagging" if value > 0 else "leading",
            session_extreme_applied=extreme,
            ibs_at_compute=None if ibs is None else round(ibs, 4),
        )
        return AlgoSignal(
            name=self.name, value=max(-1.0, min(1.0, value)), weight=1.0, metadata=meta
        )

    def get_parameters(self) -> dict[str, Any]:
        return dict(self._params)

    def set_parameters(self, params: dict[str, Any]) -> None:
        for key, value in params.items():
            if key in _DEFAULTS:
                self._params[key] = int(value) if key == "beta_window" else float(value)

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        merged = {**self._params, **params}
        for key in ("entry_atr", "scale_atr", "base_strength", "beta_min", "beta_max"):
            if float(merged[key]) <= 0:
                errors.append(f"{key} must be positive, got {merged[key]}")
        if float(merged["beta_min"]) > float(merged["beta_max"]):
            errors.append("beta_min must not exceed beta_max")
        if int(merged["beta_window"]) < _MIN_PAIRS:
            errors.append(f"beta_window must be at least {_MIN_PAIRS}, got {merged['beta_window']}")
        if not 0.0 <= float(merged["beta_r2_floor"]) < 1.0:
            errors.append(f"beta_r2_floor must be in [0, 1), got {merged['beta_r2_floor']}")
        if not 0.0 <= float(merged["extreme_ibs"]) <= 0.5:
            errors.append(f"extreme_ibs must be in [0, 0.5], got {merged['extreme_ibs']}")
        if not 0.0 <= float(merged["extreme_dampener"]) <= 1.0:
            errors.append(f"extreme_dampener must be in [0, 1], got {merged['extreme_dampener']}")
        return errors
