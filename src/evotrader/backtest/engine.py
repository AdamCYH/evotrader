"""Pure-algorithmic simulated execution engine for backtesting."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from evotrader.models.market import MarketSnapshot
from evotrader.models.signals import AlgoSignal

logger = logging.getLogger(__name__)

# Live-parity defaults, mirroring PositionSizingConfig in models/config.py.
# The engine previously hardcoded 0.50 sizing, which ran the backtest at 5x
# the size the live system is configured for and inflated every drawdown
# figure by the same factor.
DEFAULT_MAX_POSITION_PCT = 0.10
DEFAULT_STOP_LOSS_ATR_MULT = 2.0


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass
class PendingOrder:
    """A signal-driven order awaiting the next bar's open.

    Stops and take-profits are deliberately NOT routed through this: a
    resting order fills on the bar that touches it, not one bar later.
    """

    kind: str  # "open" | "close"
    reason: str = ""
    side: PositionSide | None = None
    regime: str = ""
    author: str = ""
    sig_val: float = 0.0


@dataclass
class BacktestTrade:
    """Record of an individual backtest trade lifecycle."""

    trade_id: int
    ticker: str
    side: PositionSide
    entry_time: datetime
    entry_price: float
    shares: float
    exit_time: datetime | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    realized_pnl: float = 0.0
    return_pct: float = 0.0
    bars_held: int = 0
    authoring_signal: str = ""
    entry_regime: str = ""
    signal_value_entry: float = 0.0
    mae_pct: float = 0.0  # Max Adverse Excursion %
    mfe_pct: float = 0.0  # Max Favorable Excursion %


class BacktestEngine:
    """Simulates trading decisions and portfolio tracking without LLM interaction.

    Execution model (defaults chosen for realism, not for flattering results):

    - ``next_bar_open_fill`` — a signal is computed from a bar's close, so it
      cannot also be *filled* at that same close. Signal-driven orders (entries
      and signal reversals) queue and fill at the next bar's open. Set False to
      reproduce the old same-bar-close fills.
    - ``intrabar_stops`` — stop-loss and take-profit are resting orders and
      trigger when the bar's high/low touches them, not only when the close
      crosses. Unlike signal orders they fill on the touching bar, since a
      resting order does not wait for the next open.

    Both assumptions are individually worth several percentage points of
    reported return, in opposite directions, so they are recorded in the
    tear sheet rather than left implicit.
    """

    def __init__(
        self,
        initial_cash: float = 25000.0,
        ticker: str = "",
        fractional_units: bool = False,
        entry_threshold_range_bound: float = 0.05,
        entry_threshold_trending: float = 0.03,
        entry_threshold_high_vol: float = 0.05,
        exit_threshold: float = 0.0,
        stop_loss_atr_mult: float = DEFAULT_STOP_LOSS_ATR_MULT,
        take_profit_atr_mult: float = 3.5,
        max_position_pct: float = DEFAULT_MAX_POSITION_PCT,
        slippage_bps: float = 2.0,
        allow_shorts: bool = True,
        next_bar_open_fill: bool = True,
        intrabar_stops: bool = True,
    ) -> None:
        self.cash = initial_cash
        self.initial_cash = initial_cash
        self.ticker = ticker
        self.fractional_units = fractional_units
        # Entries dropped because the allocation could not buy one whole unit.
        # Tracked rather than silently discarded: on a high-priced instrument
        # this silently turns every signal into a no-trade, which reads as "the
        # strategy declined" instead of "the harness could not size it".
        self.skipped_unaffordable = 0
        self.entry_threshold_range_bound = entry_threshold_range_bound
        self.entry_threshold_trending = entry_threshold_trending
        self.entry_threshold_high_vol = entry_threshold_high_vol
        self.exit_threshold = exit_threshold
        self.stop_loss_atr_mult = stop_loss_atr_mult
        self.take_profit_atr_mult = take_profit_atr_mult
        self.max_position_pct = max_position_pct
        self.slippage_bps = slippage_bps
        self.allow_shorts = allow_shorts
        self.next_bar_open_fill = next_bar_open_fill
        self.intrabar_stops = intrabar_stops

        self.position: BacktestTrade | None = None
        self.closed_trades: list[BacktestTrade] = []
        self.equity_curve: list[dict[str, float | str]] = []
        self._trade_counter = 0
        self._pending: PendingOrder | None = None

    @property
    def equity(self) -> float:
        """Current mark-to-market equity."""
        if self.position is None:
            return self.cash
        # If position is open, mark-to-market is tracked during step()
        return getattr(self, "_last_equity", self.cash)

    @property
    def execution_assumptions(self) -> dict[str, float | bool]:
        """The assumptions this run's numbers depend on."""
        return {
            "next_bar_open_fill": self.next_bar_open_fill,
            "intrabar_stops": self.intrabar_stops,
            "slippage_bps": self.slippage_bps,
            "max_position_pct": self.max_position_pct,
            "stop_loss_atr_mult": self.stop_loss_atr_mult,
            "take_profit_atr_mult": self.take_profit_atr_mult,
            "allow_shorts": self.allow_shorts,
        }

    def _get_entry_threshold(self, regime: str) -> float:
        if "trending" in regime:
            return self.entry_threshold_trending
        if "high_volatility" in regime:
            return self.entry_threshold_high_vol
        return self.entry_threshold_range_bound

    @staticmethod
    def _bar_ohlc(snapshot: MarketSnapshot) -> tuple[float, float, float, float]:
        """Current bar's OHLC, falling back to the quote when unavailable.

        ``recent_candles`` is session-scoped and ends on the current bar, so
        its last element is this bar. A flat fallback degrades intrabar stop
        checks to close-only rather than producing wrong fills.
        """
        price = snapshot.quote.last
        candles = snapshot.recent_candles
        if not candles:
            return price, price, price, price
        bar = candles[-1]
        if bar.timestamp != snapshot.timestamp:
            return price, price, price, price
        return bar.open, bar.high, bar.low, bar.close

    def step(self, snapshot: MarketSnapshot, signal: AlgoSignal) -> None:
        """Simulate one bar: fill pending orders, check stops, exits, entries."""
        price = snapshot.quote.last
        bar_open, bar_high, bar_low, _ = self._bar_ohlc(snapshot)
        atr = snapshot.indicators.atr_14 or (price * 0.01)
        regime = snapshot.regime.regime.value
        sig_val = signal.value
        author = signal.metadata.get("authoring_signal", "composite")

        # ── 0. Fill orders queued on the previous bar, at this bar's open ──
        if self._pending is not None:
            pending = self._pending
            self._pending = None
            if pending.kind == "close" and self.position is not None:
                self._close_position(bar_open, snapshot.timestamp, pending.reason)
            elif pending.kind == "open" and self.position is None and pending.side:
                self._open_position(
                    side=pending.side,
                    price=bar_open,
                    timestamp=snapshot.timestamp,
                    regime=pending.regime,
                    author=pending.author,
                    sig_val=pending.sig_val,
                )

        closed_this_bar = False
        # ── 1. Manage existing position ──────────────────────────────
        if self.position is not None:
            pos = self.position
            pos.bars_held += 1
            entry_p = pos.entry_price

            # Update MAE / MFE excursions
            if pos.side == PositionSide.LONG:
                cur_ret = (price - entry_p) / entry_p
                pos.mfe_pct = max(pos.mfe_pct, cur_ret * 100.0)
                pos.mae_pct = min(pos.mae_pct, cur_ret * 100.0)

                # Check Stop Loss & Take Profit
                sl_price = entry_p - (atr * self.stop_loss_atr_mult)
                tp_price = entry_p + (atr * self.take_profit_atr_mult)

                if (bar_low <= sl_price) if self.intrabar_stops else (price <= sl_price):
                    # A gap through the stop fills at the open, not the stop.
                    fill = min(sl_price, bar_open) if self.intrabar_stops else price
                    self._close_position(fill, snapshot.timestamp, "STOP_LOSS")
                    closed_this_bar = True
                elif (bar_high >= tp_price) if self.intrabar_stops else (price >= tp_price):
                    fill = max(tp_price, bar_open) if self.intrabar_stops else price
                    self._close_position(fill, snapshot.timestamp, "TAKE_PROFIT")
                    closed_this_bar = True
                elif sig_val <= -self.exit_threshold:
                    closed_this_bar = self._exit_on_signal(price, snapshot.timestamp)

            elif pos.side == PositionSide.SHORT:
                cur_ret = (entry_p - price) / entry_p
                pos.mfe_pct = max(pos.mfe_pct, cur_ret * 100.0)
                pos.mae_pct = min(pos.mae_pct, cur_ret * 100.0)

                sl_price = entry_p + (atr * self.stop_loss_atr_mult)
                tp_price = entry_p - (atr * self.take_profit_atr_mult)

                if (bar_high >= sl_price) if self.intrabar_stops else (price >= sl_price):
                    fill = max(sl_price, bar_open) if self.intrabar_stops else price
                    self._close_position(fill, snapshot.timestamp, "STOP_LOSS")
                    closed_this_bar = True
                elif (bar_low <= tp_price) if self.intrabar_stops else (price <= tp_price):
                    fill = min(tp_price, bar_open) if self.intrabar_stops else price
                    self._close_position(fill, snapshot.timestamp, "TAKE_PROFIT")
                    closed_this_bar = True
                elif sig_val >= self.exit_threshold:
                    closed_this_bar = self._exit_on_signal(price, snapshot.timestamp)

        # ── 2. Evaluate entry if flat ────────────────────────────────
        if self.position is None and not closed_this_bar and self._pending is None:
            threshold = self._get_entry_threshold(regime)

            entry_side: PositionSide | None = None
            if sig_val >= threshold:
                entry_side = PositionSide.LONG
            elif sig_val <= -threshold and self.allow_shorts:
                entry_side = PositionSide.SHORT

            if entry_side is not None:
                if self.next_bar_open_fill:
                    self._pending = PendingOrder(
                        kind="open",
                        side=entry_side,
                        regime=regime,
                        author=author,
                        sig_val=sig_val,
                    )
                else:
                    self._open_position(
                        side=entry_side,
                        price=price,
                        timestamp=snapshot.timestamp,
                        regime=regime,
                        author=author,
                        sig_val=sig_val,
                    )

        # ── 3. Mark-to-market equity recording ───────────────────────
        unrealized = 0.0
        if self.position is not None:
            if self.position.side == PositionSide.LONG:
                unrealized = (price - self.position.entry_price) * self.position.shares
            else:
                unrealized = (self.position.entry_price - price) * self.position.shares

        current_equity = self.cash + unrealized
        self._last_equity = current_equity

        self.equity_curve.append(
            {
                "timestamp": snapshot.timestamp.isoformat(),
                "equity": round(current_equity, 2),
                "cash": round(self.cash, 2),
                "price": round(price, 2),
                "in_position": self.position is not None,
                "signal": round(sig_val, 4),
                "regime": regime,
            }
        )

    def _exit_on_signal(self, price: float, timestamp: datetime) -> bool:
        """Queue or execute a signal-driven exit. Returns True if closed now."""
        if self.next_bar_open_fill:
            self._pending = PendingOrder(kind="close", reason="SIGNAL_REVERSAL")
            return False
        self._close_position(price, timestamp, "SIGNAL_REVERSAL")
        return True

    def _open_position(
        self,
        side: PositionSide,
        price: float,
        timestamp: datetime,
        regime: str,
        author: str,
        sig_val: float,
    ) -> None:
        """Execute position entry with slippage."""
        # Slippage: buy pays more, short sells for less
        slip_factor = self.slippage_bps / 10000.0
        fill_price = (
            price * (1.0 + slip_factor)
            if side == PositionSide.LONG
            else price * (1.0 - slip_factor)
        )

        # Allocate capital
        alloc_cash = self.cash * self.max_position_pct

        if self.fractional_units:
            # Continuously divisible instrument (crypto, fractional shares).
            shares = alloc_cash / fill_price
            if shares <= 0.0:
                self.skipped_unaffordable += 1
                return
        else:
            # Whole units only.
            if alloc_cash < fill_price:
                # One unit costs more than the entire per-trade allocation. On
                # a high-priced instrument this fires on EVERY entry, so count
                # it — a run reporting zero trades for this reason is a sizing
                # failure, not a market result.
                self.skipped_unaffordable += 1
                return
            shares = float(int(alloc_cash // fill_price))
            if shares <= 0:
                self.skipped_unaffordable += 1
                return

        self._trade_counter += 1
        self.position = BacktestTrade(
            trade_id=self._trade_counter,
            ticker=self.ticker,
            side=side,
            entry_time=timestamp,
            entry_price=fill_price,
            shares=shares,
            authoring_signal=author,
            entry_regime=regime,
            signal_value_entry=sig_val,
        )

    def _close_position(
        self,
        price: float,
        timestamp: datetime,
        reason: str,
    ) -> None:
        """Execute position exit with slippage and record P&L."""
        if self.position is None:
            return

        pos = self.position
        slip_factor = self.slippage_bps / 10000.0
        fill_price = (
            price * (1.0 - slip_factor)
            if pos.side == PositionSide.LONG
            else price * (1.0 + slip_factor)
        )

        if pos.side == PositionSide.LONG:
            pnl = (fill_price - pos.entry_price) * pos.shares
            ret = (fill_price - pos.entry_price) / pos.entry_price
        else:
            pnl = (pos.entry_price - fill_price) * pos.shares
            ret = (pos.entry_price - fill_price) / pos.entry_price

        self.cash += pnl
        pos.exit_time = timestamp
        pos.exit_price = fill_price
        pos.exit_reason = reason
        pos.realized_pnl = round(pnl, 2)
        pos.return_pct = round(ret * 100.0, 4)

        self.closed_trades.append(pos)
        self.position = None
