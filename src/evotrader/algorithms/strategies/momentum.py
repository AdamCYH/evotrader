"""Momentum (trend-following) strategy.

Profits from directional price movements by combining:
- EMA crossovers (9/21) for short-term momentum
- SMA alignment (20/50) for intermediate trend confirmation
- Volume confirmation to filter low-conviction moves

Best in: Trending Bull and Trending Bear regimes.
"""

from __future__ import annotations

from typing import Any

from evotrader.algorithms.base import TradingAlgorithm
from evotrader.algorithms.units import move_over_sessions_in_atr, session_date_et
from evotrader.indicators.macd import macd_signal
from evotrader.indicators.moving_averages import moving_average_signal
from evotrader.indicators.volume import (
    select_relative_volume,
    validate_rvol_source,
    volume_signal,
)
from evotrader.models.market import MarketSnapshot
from evotrader.models.signals import AlgoSignal

#: A daily MACD histogram smaller than this many ATRs is reported as neutral
#: (``macd_neutral``). It is a reading aid only: the value of the channel does
#: not change. At 0.10 ATR the MACD sub-signal reads inside +/-0.27. Over three
#: years of daily bars the band holds the quietest quarter of MSTR's days (25%)
#: and about 40% of QQQ's and SPY's.
MACD_NEUTRAL_ATR = 0.10


class MomentumStrategy(TradingAlgorithm):
    """Trend-following strategy based on moving average crossovers.

    Generates bullish signals when short-term averages cross above
    long-term averages with volume confirmation, and bearish signals
    for the reverse.
    """

    def __init__(
        self,
        ema_short: int = 9,
        ema_long: int = 21,
        sma_short: int = 20,
        sma_long: int = 50,
        volume_confirmation: bool = True,
        volume_dampener_floor: float = 0.7,
        intraday_divergence_decay: float = 0.5,
        divergence_day_change_pct: float = 1.0,
        # The same threshold in the instrument's own units: today's move as a
        # multiple of the daily ATR. None (the default) keeps the percent test,
        # so every existing version behaves exactly as before; a version opts
        # in by setting it. When set, percent is only the fallback for a
        # snapshot with no ATR or previous close.
        divergence_day_change_atr: float | None = None,
        # The same guard over SEVERAL sessions. The day test above sees one
        # session, so a decline made of small days never trips it: six down
        # days of a fifth to a third of a normal day each add up to well over
        # a normal day and a half, while no single day crosses the threshold.
        # When window > 1 and a threshold is set, the move from the close
        # `window` sessions ago is tested too, and either test applies the same
        # decay. Window 1 or no threshold (the defaults) is every existing
        # version's behaviour.
        divergence_window_days: int = 1,
        divergence_cum_atr: float | None = None,
        # Which relative-volume reading confirms the trend — see
        # indicators.volume.select_relative_volume. 'daily' is every existing
        # version's behaviour.
        rvol_source: str = "daily",
        version: str = "v001",
    ) -> None:
        self._ema_short = ema_short
        self._ema_long = ema_long
        self._sma_short = sma_short
        self._sma_long = sma_long
        self._volume_confirmation = volume_confirmation
        self._volume_dampener_floor = volume_dampener_floor
        self._intraday_divergence_decay = intraday_divergence_decay
        self._divergence_day_change_pct = divergence_day_change_pct
        self._divergence_day_change_atr = divergence_day_change_atr
        self._divergence_window_days = divergence_window_days
        self._divergence_cum_atr = divergence_cum_atr
        self._rvol_source = rvol_source
        self._version = version

    @property
    def name(self) -> str:
        return "momentum"

    @property
    def version(self) -> str:
        return self._version

    @property
    def description(self) -> str:
        return (
            f"Trend-following strategy using EMA({self._ema_short}/{self._ema_long}) "
            f"and SMA({self._sma_short}/{self._sma_long}) crossovers "
            f"{'with' if self._volume_confirmation else 'without'} volume confirmation."
        )

    def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
        """Compute momentum signal from moving averages and MACD."""
        ind = snapshot.indicators

        # Moving average composite signal — bind to locals inside the
        # None-guard so the type checker sees them as non-optional without
        # relying on assert statements (stripped under -O).
        ma_sig = 0.0
        ema_9 = ind.ema_9
        ema_21 = ind.ema_21
        sma_20 = ind.sma_20
        sma_50 = ind.sma_50
        if all(v is not None for v in (ema_9, ema_21, sma_20, sma_50)):
            # The all() guard guarantees these are not None.
            ma_sig = moving_average_signal(
                close=snapshot.quote.last,
                ema_9=ema_9,  # type: ignore[arg-type]
                ema_21=ema_21,  # type: ignore[arg-type]
                sma_20=sma_20,  # type: ignore[arg-type]
                sma_50=sma_50,  # type: ignore[arg-type]
                atr=ind.atr_14,
            )

        # MACD signal
        macd_sig = 0.0
        macd_l = ind.macd_line
        macd_s = ind.macd_signal
        macd_h = ind.macd_histogram
        if all(v is not None for v in (macd_l, macd_s, macd_h)):
            macd_sig = macd_signal(
                macd_line_value=macd_l,  # type: ignore[arg-type]
                signal_line_value=macd_s,  # type: ignore[arg-type]
                histogram_value=macd_h,  # type: ignore[arg-type]
                # TODO: plumb histogram_prev from a prior-candle indicator
                # field once available on MarketSnapshot (OHLCV has no
                # macd_histogram attr today).
                atr=ind.atr_14,
            )

        # Combine: 60% MA crossover, 40% MACD
        macd_hist_atr = (
            round(macd_h / ind.atr_14, 4)
            if macd_h is not None and ind.atr_14 is not None and ind.atr_14 > 0
            else None
        )

        raw_signal = 0.6 * ma_sig + 0.4 * macd_sig

        # Intraday-divergence dampener: daily MAs lag multi-day reversals.
        # If the daily-MA signal points one way while today's realized move
        # is strongly the other way, decay the stale component instead of
        # letting it saturate the composite for days (7/17-7/21 incident).
        #
        # "Strongly" is measured in the instrument's own units when configured:
        # today's move as a multiple of its daily ATR. A percent threshold means
        # different things on different instruments — 0.6% was ~0.5 ATR on QQQ
        # and ~0.1 ATR on MSTR, so on MSTR the haircut toggled on noise and
        # moved the composite across the strategy's strong band by itself
        # (2026-09-23: 0.275 <-> 0.555 with the MA/MACD internals constant).
        divergence_applied = False
        day_change = snapshot.daily_change_pct
        day_change_available = day_change is not None
        atr = ind.atr_14
        prev_close = snapshot.quote.previous_close if snapshot.quote else None
        divergence_atr_observed: float | None = None
        if day_change is not None and atr and atr > 0 and prev_close and prev_close > 0:
            divergence_atr_observed = abs(day_change / 100.0 * prev_close) / atr
        use_atr = (
            self._divergence_day_change_atr is not None and divergence_atr_observed is not None
        )
        threshold_source = "atr" if use_atr else "pct"
        strong_day = False
        if day_change is not None and raw_signal != 0.0 and raw_signal * day_change < 0:
            strong_day = (
                divergence_atr_observed >= self._divergence_day_change_atr  # type: ignore[operator]
                if use_atr
                else abs(day_change) >= self._divergence_day_change_pct
            )

        # The multi-session test: price now against the close `window`
        # trading days ago, in ATRs. Found by date (see
        # units.move_over_sessions_in_atr), so it means the same span whether
        # the daily list ends with yesterday's bar or today's.
        window_on = self._divergence_window_days > 1 and self._divergence_cum_atr is not None
        window_atr: float | None = None
        window_anchor = None
        strong_window = False
        if window_on:
            move = move_over_sessions_in_atr(
                snapshot.daily_candles,
                self._divergence_window_days,
                snapshot.quote.last if snapshot.quote else None,
                atr,
                session_date_et(snapshot.timestamp),
            )
            if move is not None:
                window_atr, window_anchor = move
                strong_window = (
                    raw_signal != 0.0
                    and raw_signal * window_atr < 0
                    and abs(window_atr) >= self._divergence_cum_atr  # type: ignore[operator]
                )

        # One decay, whichever test (or both) found the trend contradicted.
        divergence_basis: str | None = None
        if strong_day or strong_window:
            raw_signal *= self._intraday_divergence_decay
            divergence_applied = True
            divergence_basis = (
                "both" if strong_day and strong_window else ("day" if strong_day else "window")
            )

        # Volume confirmation: dampen signal if volume is low.
        # Volume modulates CONFIDENCE, not directional validity. A confirmed
        # trend must not be silently shrunk to noise by low intraday volume,
        # or it can never overcome the mean-reversion fade in the composite.
        vol_multiplier_raw = 0.5
        vol_multiplier_applied = 0.5
        # True only when the dampener actually ran AND the floor clamped it.
        # The 0.5 defaults above are placeholders for the disabled/no-data
        # path, where nothing is multiplied at all — comparing them against the
        # floor would report a clamp that never happened.
        vol_floor_binding = False
        rvol, rvol_source = select_relative_volume(ind, self._rvol_source)
        if self._volume_confirmation and rvol is not None:
            vol_multiplier_raw = volume_signal(rvol)
            vol_multiplier_applied = max(self._volume_dampener_floor, vol_multiplier_raw)
            vol_floor_binding = vol_multiplier_raw < self._volume_dampener_floor
            raw_signal *= vol_multiplier_applied

        signal_value = max(-1.0, min(1.0, raw_signal))

        return AlgoSignal(
            name=self.name,
            value=signal_value,
            weight=1.0,
            metadata={
                "ma_signal": ma_sig,
                "macd_signal": macd_sig,
                # The daily MACD histogram in ATRs, signed, and whether it is
                # inside the neutral band. A histogram of 0.05 ATR makes the
                # MACD sub-signal read -0.14, small enough to be noise and
                # large enough to be read as dissent on a day the price rises.
                # Inside the band it is neither a co-sign nor a dissent; the
                # flag is reported so that is not decided anew every cycle.
                "macd_hist_atr": macd_hist_atr,
                "macd_neutral": (
                    abs(macd_hist_atr) < MACD_NEUTRAL_ATR if macd_hist_atr is not None else None
                ),
                "macd_neutral_atr": MACD_NEUTRAL_ATR,
                # Saturation telemetry: distinguishes a genuine extreme
                # read from a clamped one.  Without this, an uninformative
                # rail-pinned -1.0 is indistinguishable downstream from a
                # true -1.0, which is how this defect survived 18 versions.
                "ma_saturated": abs(ma_sig) >= 0.995,
                "macd_saturated": abs(macd_sig) >= 0.695,
                "signal_saturated": abs(ma_sig) >= 0.995 and abs(macd_sig) >= 0.695,
                "volume_multiplier_raw": vol_multiplier_raw,
                "volume_multiplier_applied": vol_multiplier_applied,
                # The floor clamps the raw read on essentially every cycle
                # (observed: raw 0.2465 -> applied 0.7 in 8 of 8 live cycles on
                # 2026-09-10), so the dampener's configured shape is doing no
                # work and the underlying function goes unobserved. That is the
                # entry_z/min_std failure mode again: a parameter that looks
                # meaningful but is not identifiable because another binds
                # first. Emitted as a boolean so it is countable rather than
                # inferred — if this is True on nearly every cycle, the floor
                # IS the dampener and the curve beneath it is decorative.
                "volume_floor_binding": vol_floor_binding,
                "divergence_applied": divergence_applied,
                # Distinguishes a guard that declined to fire from one that
                # could not be evaluated at all (None input).
                "day_change_available": day_change_available,
                "day_change_pct": day_change if day_change is not None else "N/A",
                "divergence_threshold_pct": self._divergence_day_change_pct,
                "divergence_threshold_atr": self._divergence_day_change_atr,
                "divergence_threshold_source": threshold_source,
                "divergence_basis": divergence_basis,
                "divergence_window_days": self._divergence_window_days,
                "divergence_cum_atr": self._divergence_cum_atr,
                # Signed: negative is a decline over the window. None when the
                # window test is off or the history is too short to measure.
                "divergence_window_atr_observed": (
                    round(window_atr, 4) if window_atr is not None else None
                ),
                # The session the window is measured from — which day the
                # number describes.
                "divergence_window_anchor": (
                    window_anchor.isoformat() if window_anchor is not None else None
                ),
                # Reported whenever it can be computed, even under the percent
                # test, so the calibration record shows the move in ATRs either way.
                "divergence_atr_observed": (
                    round(divergence_atr_observed, 4)
                    if divergence_atr_observed is not None
                    else None
                ),
                # Which day's volume confirmed the trend. 'prior_session_daily'
                # is yesterday's ratio, constant all session.
                "rvol_source": rvol_source,
                # The ratio itself, as the other two volume readers report it:
                # volume_multiplier_raw saturates outside 0.5..2.0 and cannot
                # be inverted there.
                "rvol": round(rvol, 4) if rvol is not None else None,
            },
        )

    def get_parameters(self) -> dict[str, Any]:
        return {
            "ema_short": self._ema_short,
            "ema_long": self._ema_long,
            "sma_short": self._sma_short,
            "sma_long": self._sma_long,
            "volume_confirmation": self._volume_confirmation,
            "volume_dampener_floor": self._volume_dampener_floor,
            "intraday_divergence_decay": self._intraday_divergence_decay,
            "divergence_day_change_pct": self._divergence_day_change_pct,
            "divergence_day_change_atr": self._divergence_day_change_atr,
            "divergence_window_days": self._divergence_window_days,
            "divergence_cum_atr": self._divergence_cum_atr,
            "rvol_source": self._rvol_source,
        }

    def set_parameters(self, params: dict[str, Any]) -> None:
        if "ema_short" in params:
            self._ema_short = int(params["ema_short"])
        if "ema_long" in params:
            self._ema_long = int(params["ema_long"])
        if "sma_short" in params:
            self._sma_short = int(params["sma_short"])
        if "sma_long" in params:
            self._sma_long = int(params["sma_long"])
        if "volume_confirmation" in params:
            self._volume_confirmation = bool(params["volume_confirmation"])
        if "volume_dampener_floor" in params:
            self._volume_dampener_floor = float(params["volume_dampener_floor"])
        if "intraday_divergence_decay" in params:
            self._intraday_divergence_decay = float(params["intraday_divergence_decay"])
        if "divergence_day_change_pct" in params:
            self._divergence_day_change_pct = float(params["divergence_day_change_pct"])
        if "divergence_day_change_atr" in params:
            v = params["divergence_day_change_atr"]
            self._divergence_day_change_atr = None if v is None else float(v)
        if "divergence_window_days" in params:
            self._divergence_window_days = int(params["divergence_window_days"])
        if "divergence_cum_atr" in params:
            v = params["divergence_cum_atr"]
            self._divergence_cum_atr = None if v is None else float(v)
        if "rvol_source" in params:
            self._rvol_source = str(params["rvol_source"])

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        if "ema_short" in params and "ema_long" in params:
            if params["ema_short"] >= params["ema_long"]:
                errors.append("ema_short must be less than ema_long")
        if "sma_short" in params and "sma_long" in params:
            if params["sma_short"] >= params["sma_long"]:
                errors.append("sma_short must be less than sma_long")
        if "volume_dampener_floor" in params:
            floor_val = float(params["volume_dampener_floor"])
            if not (0.0 <= floor_val <= 1.0):
                errors.append(f"volume_dampener_floor must be between 0.0 and 1.0, got {floor_val}")
        if "intraday_divergence_decay" in params:
            decay = float(params["intraday_divergence_decay"])
            if not (0.0 <= decay <= 1.0):
                errors.append(f"intraday_divergence_decay must be between 0.0 and 1.0, got {decay}")
        if "divergence_day_change_pct" in params:
            pct = float(params["divergence_day_change_pct"])
            if pct <= 0 or pct > 10.0:
                errors.append(
                    f"divergence_day_change_pct must be in (0, 10.0] percent "
                    f"(e.g. 1.0 = 1%), got {pct}"
                )
        if (
            "divergence_day_change_atr" in params
            and params["divergence_day_change_atr"] is not None
        ):
            v = float(params["divergence_day_change_atr"])
            if not (0.0 < v <= 3.0):
                errors.append(f"divergence_day_change_atr must be in (0, 3.0] daily ATRs, got {v}")
        if "divergence_window_days" in params:
            try:
                w = int(params["divergence_window_days"])
                if not (1 <= w <= 20) or w != params["divergence_window_days"]:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(
                    "divergence_window_days must be a whole number of sessions in "
                    f"[1, 20] (1 = off), got {params['divergence_window_days']!r}"
                )
        if "divergence_cum_atr" in params and params["divergence_cum_atr"] is not None:
            v = float(params["divergence_cum_atr"])
            if not (0.0 < v <= 5.0):
                errors.append(f"divergence_cum_atr must be in (0, 5.0] daily ATRs, got {v}")
        if "rvol_source" in params:
            err = validate_rvol_source(params["rvol_source"])
            if err:
                errors.append(err)
        return errors
