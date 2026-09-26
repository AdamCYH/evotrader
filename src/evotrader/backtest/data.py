"""Historical data acquisition and local CSV caching for backtesting."""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import yfinance as yf

from evotrader import paths

logger = logging.getLogger(__name__)


# yfinance hard limits on intraday history, by interval. These are server-side
# caps: chunking the requests does not extend them. Exceeding one silently
# returns a short frame, which reads as "the strategy had no data" rather than
# as a data problem — so we raise instead.
MAX_INTRADAY_DAYS = {
    "1m": 7,
    "2m": 60,
    "5m": 60,
    "15m": 60,
    "30m": 60,
    "90m": 60,
    "1h": 730,
    "60m": 730,
}

_PERIOD_DAYS = {"d": 1, "mo": 30, "y": 365}


def _period_to_days(period: str) -> int | None:
    """Approximate a yfinance period string ('2y', '6mo', '60d') in days."""
    p = period.strip().lower()
    for suffix, mult in sorted(_PERIOD_DAYS.items(), key=lambda kv: -len(kv[0])):
        if p.endswith(suffix):
            try:
                return int(float(p[: -len(suffix)]) * mult)
            except ValueError:
                return None
    return None


class HistoricalDataFetcher:
    """Acquires and caches historical market data for backtesting."""

    def __init__(self, cache_dir: Path | str | None = None) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else paths.data_dir() / "backtest" / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _check_interval_limit(interval: str, period: str) -> None:
        """Fail loudly when the requested window exceeds the provider's cap."""
        cap = MAX_INTRADAY_DAYS.get(interval)
        want = _period_to_days(period)
        if cap is None or want is None or want <= cap:
            return
        raise ValueError(
            f"yfinance serves at most {cap} days of {interval} bars, but "
            f"period='{period}' asks for ~{want} days. Either shorten the "
            f"period, use a coarser interval (1h reaches 730 days), or supply "
            f"your own bars via load_csv() — a multi-year intraday backtest at "
            f"{interval} granularity needs a paid provider (Polygon, Databento, "
            f"Alpaca)."
        )

    def load_csv(self, path: Path | str) -> pd.DataFrame:
        """Load OHLCV bars from an external CSV (e.g. Polygon/Databento export).

        The first column must be a parseable timestamp index; columns are
        matched case-insensitively against open/high/low/close/volume.
        """
        df = pd.read_csv(path, index_col=0)
        return self._normalize_columns(df)

    def fetch_intraday(
        self,
        ticker: str,
        period: str = "2y",
        interval: str = "1h",
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """Fetch intraday bars (e.g. 1h over 2y).

        Uses local cache if available unless force_refresh=True.
        """
        cache_path = self.cache_dir / f"{ticker}_{interval}_{period}.csv"
        if not force_refresh and cache_path.is_file():
            logger.info("Loading cached intraday data from %s", cache_path)
            df = pd.read_csv(cache_path, index_col=0)
            return self._normalize_columns(df)

        self._check_interval_limit(interval, period)
        logger.info("Downloading %s (%s, %s) via yfinance...", ticker, interval, period)
        raw_df = yf.download(
            ticker,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=False,
        )
        if raw_df.empty:
            raise ValueError(f"No intraday data returned for {ticker} ({interval}, {period})")

        df = self._normalize_columns(raw_df)
        df.to_csv(cache_path)
        logger.info("Saved %d intraday bars to %s", len(df), cache_path)
        return df

    def fetch_daily(
        self,
        ticker: str,
        period: str = "5y",
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """Fetch daily bars (e.g. 1d over 5y) for indicators and regime detection."""
        cache_path = self.cache_dir / f"{ticker}_1d_{period}.csv"
        if not force_refresh and cache_path.is_file():
            logger.info("Loading cached daily data from %s", cache_path)
            df = pd.read_csv(cache_path, index_col=0)
            return self._normalize_columns(df)

        logger.info("Downloading %s daily (%s) via yfinance...", ticker, period)
        raw_df = yf.download(
            ticker,
            period=period,
            interval="1d",
            progress=False,
            auto_adjust=False,
        )
        if raw_df.empty:
            raise ValueError(f"No daily data returned for {ticker} ({period})")

        df = self._normalize_columns(raw_df)
        df.to_csv(cache_path)
        logger.info("Saved %d daily bars to %s", len(df), cache_path)
        return df

    @staticmethod
    def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
        """Normalize dataframe columns to lowercase simple names."""
        clean_df = df.copy()
        if isinstance(clean_df.columns, pd.MultiIndex):
            # yfinance often returns MultiIndex like ('Close', 'QQQ')
            clean_df.columns = [c[0].lower() for c in clean_df.columns]
        else:
            clean_df.columns = [str(c).lower() for c in clean_df.columns]

        # Ensure essential columns are present
        required = {"open", "high", "low", "close", "volume"}
        missing = required - set(clean_df.columns)
        if missing:
            raise ValueError(f"Dataframe missing required OHLCV columns: {missing}")

        clean_df = clean_df[["open", "high", "low", "close", "volume"]].dropna()

        # Intraday CSVs round-trip with mixed DST offsets ("-04:00" / "-05:00"),
        # which defeats pandas' inference and leaves an index of strings. Force
        # it so a cached run and a fresh download agree on dtype and tz.
        if not isinstance(clean_df.index, pd.DatetimeIndex):
            clean_df.index = pd.to_datetime(clean_df.index, utc=True, format="mixed")
        elif clean_df.index.tz is not None:
            clean_df.index = clean_df.index.tz_convert("UTC")

        return clean_df.sort_index()
