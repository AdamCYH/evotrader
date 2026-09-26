"""Precomputes indicators and streams MarketSnapshot objects without lookahead bias."""

from __future__ import annotations

from collections.abc import Generator
from datetime import datetime

import numpy as np
import pandas as pd

from evotrader.indicators.atr import compute_atr
from evotrader.indicators.bollinger import compute_bollinger_bands
from evotrader.indicators.ibs import compute_live_ibs
from evotrader.indicators.macd import compute_macd
from evotrader.indicators.moving_averages import compute_moving_averages
from evotrader.indicators.regime import detect_regime_rule_based
from evotrader.indicators.rsi import compute_rsi
from evotrader.indicators.volume import compute_relative_volume, compute_volume_sma
from evotrader.indicators.vwap import compute_session_vwap
from evotrader.models.market import (
    OHLCV,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)


class SnapshotBuilder:
    """Precomputes indicators across daily and intraday series and yields MarketSnapshots.

    Guarantees zero lookahead bias: for bar at intraday time T on date D:
    - Multi-day trend indicators are referenced strictly from completed day D-1.
    - Intraday session metrics (session VWAP, IBS, session high/low) are built
      only from bars up to time T.
    - ``recent_candles`` holds only bars from session D up to and including T.
    - ``daily_candles`` holds only sessions completed strictly before D.

    ``recent_candles`` is deliberately session-scoped. It previously sliced the
    full intraday frame, so 99.8% of snapshots reached back into prior sessions
    (median span 5 calendar days) while ``indicators.vwap`` stayed anchored to
    the current session. ``IntradayVwapZscoreStrategy`` z-scores deviation from
    that session VWAP, so the mismatched window suppressed it to 6.3% of bars
    against 25.4% once scoped correctly.
    """

    def __init__(
        self,
        df_intraday: pd.DataFrame,
        df_daily: pd.DataFrame,
        ticker: str,
        recent_candles_lookback: int = 20,
        daily_candles_lookback: int = 90,
    ) -> None:
        self.ticker = ticker
        self.recent_candles_lookback = recent_candles_lookback
        self.daily_candles_lookback = daily_candles_lookback
        self.df_intraday = df_intraday.copy()
        self.df_daily = df_daily.copy()

        # Ensure datetime index is localized / consistent
        if not isinstance(self.df_intraday.index, pd.DatetimeIndex):
            self.df_intraday.index = pd.to_datetime(self.df_intraday.index, utc=True)
        if not isinstance(self.df_daily.index, pd.DatetimeIndex):
            self.df_daily.index = pd.to_datetime(self.df_daily.index, utc=True)

        self._precompute_daily_indicators()

    def _precompute_daily_indicators(self) -> None:
        """Vectorized computation of all daily indicators."""
        d = self.df_daily
        c, h, l, v = d["close"], d["high"], d["low"], d["volume"]  # noqa: E741 (OHLC)

        # Moving averages
        mas = compute_moving_averages(c)
        d["sma_20"] = mas.sma_20
        d["sma_50"] = mas.sma_50
        d["ema_9"] = mas.ema_9
        d["ema_21"] = mas.ema_21

        # RSI & MACD
        d["rsi_14"] = compute_rsi(c, period=14)
        macd_line, macd_signal, macd_hist = compute_macd(c)
        d["macd_line"] = macd_line
        d["macd_signal"] = macd_signal
        d["macd_histogram"] = macd_hist

        # Bollinger Bands
        bb = compute_bollinger_bands(c)
        d["bollinger_upper"] = bb.upper
        d["bollinger_middle"] = bb.middle
        d["bollinger_lower"] = bb.lower
        d["bollinger_width"] = bb.width

        # ATR
        d["atr_14"] = compute_atr(h, l, c, period=14)

        # Volume
        d["volume_sma_20"] = compute_volume_sma(v, period=20)
        d["relative_volume"] = compute_relative_volume(v, period=20)

        # Normalize date column for fast lookup
        d["date_norm"] = d.index.date

    def iter_snapshots(self) -> Generator[MarketSnapshot, None, None]:
        """Yields MarketSnapshot chronologically across all intraday bars."""
        intraday = self.df_intraday
        daily = self.df_daily

        # Group intraday bars by date for fast session-based VWAP and extremes
        intraday_dates = intraday.index.date
        unique_dates = np.unique(intraday_dates)

        daily_date_map = {row["date_norm"]: row for _, row in daily.iterrows()}
        daily_dates_sorted = sorted(daily_date_map.keys())

        # Rolling window for intraday recent_candles (last 20 bars)
        for date_val in unique_dates:
            session_mask = intraday_dates == date_val
            session_df = intraday[session_mask]
            if session_df.empty:
                continue

            # Identify completed prior daily close
            prior_dates = [d for d in daily_dates_sorted if d < date_val]
            if not prior_dates:
                # Not enough historical daily data before this day
                continue
            prior_date = prior_dates[-1]
            prior_daily = daily_date_map[prior_date]
            prev_close = float(prior_daily["close"])

            # Completed sessions only — strictly before the current date, so
            # TrendPersistenceStrategy sees the same shape it gets live
            # without ever seeing today's bar.
            daily_candles = [
                OHLCV(
                    timestamp=(d_ts if isinstance(d_ts, datetime) else d_ts.to_pydatetime()),
                    open=float(d_row["open"]),
                    high=float(d_row["high"]),
                    low=float(d_row["low"]),
                    close=float(d_row["close"]),
                    volume=float(d_row["volume"]),
                )
                for d_ts, d_row in daily[daily["date_norm"] < date_val]
                .tail(self.daily_candles_lookback)
                .iterrows()
            ]

            # Compute session opening gap
            first_open = float(session_df.iloc[0]["open"])
            gap_pct = round(((first_open - prev_close) / prev_close) * 100.0, 4)

            # Cumulative session bars
            session_highs = []
            session_lows = []
            session_closes = []
            session_volumes = []

            for i in range(len(session_df)):
                curr_bar = session_df.iloc[i]
                bar_time = curr_bar.name
                bar_close = float(curr_bar["close"])
                bar_high = float(curr_bar["high"])
                bar_low = float(curr_bar["low"])
                bar_vol = float(curr_bar["volume"])

                session_highs.append(bar_high)
                session_lows.append(bar_low)
                session_closes.append(bar_close)
                session_volumes.append(bar_vol)

                # 1. Session VWAP
                s_high_sr = pd.Series(session_highs)
                s_low_sr = pd.Series(session_lows)
                s_close_sr = pd.Series(session_closes)
                s_vol_sr = pd.Series(session_volumes)

                session_vwap, vwap_anchor = compute_session_vwap(
                    s_high_sr, s_low_sr, s_close_sr, s_vol_sr
                )

                # 2. Live IBS & daily change
                curr_session_high = max(session_highs)
                curr_session_low = min(session_lows)
                ibs = compute_live_ibs(curr_session_high, curr_session_low, bar_close)
                daily_change_pct = round(((bar_close - prev_close) / prev_close) * 100.0, 4)

                # 3. Indicators object
                indicators = TechnicalIndicators(
                    rsi_14=float(prior_daily["rsi_14"])
                    if not pd.isna(prior_daily["rsi_14"])
                    else None,
                    macd_line=float(prior_daily["macd_line"])
                    if not pd.isna(prior_daily["macd_line"])
                    else None,
                    macd_signal=float(prior_daily["macd_signal"])
                    if not pd.isna(prior_daily["macd_signal"])
                    else None,
                    macd_histogram=float(prior_daily["macd_histogram"])
                    if not pd.isna(prior_daily["macd_histogram"])
                    else None,
                    bollinger_upper=float(prior_daily["bollinger_upper"])
                    if not pd.isna(prior_daily["bollinger_upper"])
                    else None,
                    bollinger_middle=float(prior_daily["bollinger_middle"])
                    if not pd.isna(prior_daily["bollinger_middle"])
                    else None,
                    bollinger_lower=float(prior_daily["bollinger_lower"])
                    if not pd.isna(prior_daily["bollinger_lower"])
                    else None,
                    bollinger_width=float(prior_daily["bollinger_width"])
                    if not pd.isna(prior_daily["bollinger_width"])
                    else None,
                    ema_9=float(prior_daily["ema_9"])
                    if not pd.isna(prior_daily["ema_9"])
                    else None,
                    ema_21=float(prior_daily["ema_21"])
                    if not pd.isna(prior_daily["ema_21"])
                    else None,
                    sma_20=float(prior_daily["sma_20"])
                    if not pd.isna(prior_daily["sma_20"])
                    else None,
                    sma_50=float(prior_daily["sma_50"])
                    if not pd.isna(prior_daily["sma_50"])
                    else None,
                    vwap=session_vwap,
                    vwap_anchor=vwap_anchor,
                    ibs=ibs,
                    atr_14=float(prior_daily["atr_14"])
                    if not pd.isna(prior_daily["atr_14"])
                    else None,
                    volume_sma_20=float(prior_daily["volume_sma_20"])
                    if not pd.isna(prior_daily["volume_sma_20"])
                    else None,
                    relative_volume=float(prior_daily["relative_volume"])
                    if not pd.isna(prior_daily["relative_volume"])
                    else 1.0,
                    # Same quantity live reads: the PRIOR day's daily ratio.
                    # No session profile here, so channels opting into
                    # rvol_source='session' fall back to this, labelled.
                    relative_volume_source="prior_session_daily",
                )

                # 4. Regime detection (using daily series up to prior day + intraday override)
                prior_daily_window = daily[daily["date_norm"] <= prior_date].tail(60)
                if len(prior_daily_window) >= 50:
                    regime_res = detect_regime_rule_based(
                        close=prior_daily_window["close"],
                        high=prior_daily_window["high"],
                        low=prior_daily_window["low"],
                        volume=prior_daily_window["volume"],
                        daily_change_pct=daily_change_pct,
                        vwap=session_vwap,
                        macd_histogram=indicators.macd_histogram,
                    )
                else:
                    regime_res = RegimeClassification(
                        regime="range_bound",
                        confidence=0.5,
                        reasoning="Insufficient daily history (<50 bars)",
                    )

                # 5. Recent candles — current session only, up to and
                # including the current bar. Never crosses into a prior
                # session: session VWAP is the reference these are measured
                # against, so a multi-day window is a category error.
                start_i = max(0, i + 1 - self.recent_candles_lookback)
                trailing_slice = session_df.iloc[start_i : i + 1]
                recent_candles = [
                    OHLCV(
                        timestamp=ts if isinstance(ts, datetime) else ts.to_pydatetime(),
                        open=float(r["open"]),
                        high=float(r["high"]),
                        low=float(r["low"]),
                        close=float(r["close"]),
                        volume=float(r["volume"]),
                    )
                    for ts, r in trailing_slice.iterrows()
                ]

                # 6. Quote
                ts_dt = bar_time if isinstance(bar_time, datetime) else bar_time.to_pydatetime()
                quote = Quote(
                    ticker=self.ticker,
                    bid=round(bar_close - 0.01, 2),
                    ask=round(bar_close + 0.01, 2),
                    last=bar_close,
                    volume=bar_vol,
                    timestamp=ts_dt,
                    previous_close=prev_close,
                )

                yield MarketSnapshot(
                    ticker=self.ticker,
                    timestamp=ts_dt,
                    quote=quote,
                    indicators=indicators,
                    regime=regime_res,
                    recent_candles=recent_candles,
                    daily_candles=daily_candles,
                    daily_change_pct=daily_change_pct,
                    gap_pct=gap_pct,
                )
