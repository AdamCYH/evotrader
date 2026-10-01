"""A trend is contradicted over several sessions, not only by one big day.

Two blind spots, both of which let a run of small down days go unnoticed by a
long-biased trend reading:

* **momentum's divergence guard** halves the vote when the day moves hard
  against it, but it looks at one session. Six small down days, each well under
  the guard's threshold, can add up to more than a normal day and a half of
  decline without the guard firing. It now also measures the
  move from the close ``divergence_window_days`` sessions ago, when configured.
* **trend_persistence** fires on a multi-session grind only when the moving
  averages are stacked the same way. After a long advance they stay stacked
  upward for weeks, so a clean six-day decline met every condition the channel
  owns and still read 0.0: its bearish side cannot speak. With
  ``counter_stack_scale`` and ``no_stack_scale`` set, the stack scales the vote
  instead of vetoing it.

Both default to off, so every existing algorithm version reads exactly as
before. The numbers here are made up.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from evotrader.algorithms.strategies.momentum import MomentumStrategy
from evotrader.algorithms.strategies.trend_persistence import TrendPersistenceStrategy
from evotrader.algorithms.units import move_over_sessions_in_atr, session_date_et
from evotrader.models.market import (
    OHLCV,
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)
from evotrader.utils import extract_balanced_bracket, safe_parse_json

ATR = 4.0
# Wednesday 2026-09-30, 17:00 ET.
CYCLE = datetime(2026, 9, 30, 21, 0, tzinfo=UTC)
TODAY = date(2026, 9, 30)


def _sessions_before(day: date, n: int) -> list[date]:
    """The n weekdays before ``day``, oldest first (no holidays in the window)."""
    out: list[date] = []
    d = day
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return list(reversed(out))


# A grind down: every day small (0.25 ATR = 1.0), six of them.
GRIND = [110.0, 109.0, 108.0, 107.0, 106.0, 105.0, 104.0]
DATES = _sessions_before(TODAY, len(GRIND))


def _bars(closes: list[float], dates: list[date]) -> list[OHLCV]:
    return [
        OHLCV(
            timestamp=datetime(d.year, d.month, d.day, tzinfo=UTC),
            open=c + 0.2,
            high=c + 0.5,
            low=c - 0.5,
            close=c,
            volume=1e6,
        )
        for c, d in zip(closes, dates, strict=True)
    ]


def _snapshot(
    price: float,
    bars: list[OHLCV],
    *,
    day_chg: float = -0.5,
    ema_9: float = 103.0,
    ema_21: float = 95.0,
    sma_20: float = 96.0,
    sma_50: float = 85.0,
) -> MarketSnapshot:
    """Long-term averages stacked upward: momentum votes long, the stack is bullish."""
    return MarketSnapshot(
        ticker="T",
        timestamp=CYCLE,
        quote=Quote(
            ticker="T",
            bid=price - 0.01,
            ask=price + 0.01,
            last=price,
            volume=1e6,
            timestamp=CYCLE,
            previous_close=price / (1 + day_chg / 100),
        ),
        indicators=TechnicalIndicators(
            atr_14=ATR,
            ema_9=ema_9,
            ema_21=ema_21,
            sma_20=sma_20,
            sma_50=sma_50,
            macd_line=0.8,
            macd_signal=0.6,
            macd_histogram=0.2,
            relative_volume=1.0,
        ),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BULL, confidence=0.7, reasoning="t"
        ),
        daily_change_pct=day_chg,
        daily_candles=bars,
    )


# ── The shared measurement ────────────────────────────────────────────────


class TestTheWindowIsFoundByDate:
    def test_it_measures_from_the_close_n_sessions_ago(self) -> None:
        move = move_over_sessions_in_atr(_bars(GRIND, DATES), 5, 103.0, ATR, TODAY)
        assert move is not None
        atr_move, anchor = move
        assert anchor == DATES[-5]
        assert atr_move == pytest.approx((103.0 - GRIND[-5]) / ATR)

    def test_it_means_the_same_span_whether_or_not_today_has_a_bar(self) -> None:
        """Data sources disagree on whether today's bar is in the daily list,
        and some overwrite the last bar with the live price. Counting bars from
        the end would shift the window by a day between the two; a date does
        not move."""
        without_today = _bars(GRIND, DATES)
        with_today = without_today + _bars([103.0], [TODAY])
        overwritten = _bars(GRIND[:-1] + [103.0], DATES)  # yesterday's bar, today's price
        results = {
            move_over_sessions_in_atr(b, 5, 103.0, ATR, TODAY)
            for b in (without_today, with_today, overwritten)
        }
        assert len(results) == 1

    def test_too_little_history_is_unmeasured_not_zero(self) -> None:
        assert (
            move_over_sessions_in_atr(_bars(GRIND[-3:], DATES[-3:]), 5, 103.0, ATR, TODAY) is None
        )

    def test_a_naive_time_is_read_as_eastern(self) -> None:
        assert session_date_et(datetime(2026, 9, 30, 23, 30)) == date(2026, 9, 30)
        assert session_date_et(datetime(2026, 10, 1, 2, 30, tzinfo=UTC)) == date(2026, 9, 30)


# ── momentum ──────────────────────────────────────────────────────────────


class TestMomentumDivergenceWindow:
    def test_off_by_default_and_unchanged(self) -> None:
        snap = _snapshot(103.0, _bars(GRIND, DATES))
        default = MomentumStrategy().compute_signal(snap)
        explicit_off = MomentumStrategy(
            divergence_window_days=1, divergence_cum_atr=1.0
        ).compute_signal(snap)
        no_threshold = MomentumStrategy(divergence_window_days=5).compute_signal(snap)
        assert default.value > 0
        assert explicit_off.value == default.value == no_threshold.value
        assert default.metadata["divergence_basis"] is None
        assert default.metadata["divergence_window_atr_observed"] is None

    def test_a_grind_of_small_days_now_decays_the_vote(self) -> None:
        snap = _snapshot(103.0, _bars(GRIND, DATES))
        off = MomentumStrategy(divergence_day_change_atr=0.5).compute_signal(snap)
        on = MomentumStrategy(
            divergence_day_change_atr=0.5, divergence_window_days=5, divergence_cum_atr=1.0
        ).compute_signal(snap)
        assert not off.metadata["divergence_applied"], "each day is too small for the day test"
        assert on.metadata["divergence_basis"] == "window"
        assert on.metadata["divergence_window_anchor"] == DATES[-5].isoformat()
        assert on.value == pytest.approx(off.value * 0.5)

    def test_a_window_moving_with_the_vote_does_not_decay_it(self) -> None:
        rally = [float(c) for c in reversed(GRIND)]
        snap = _snapshot(111.0, _bars(rally, DATES), day_chg=0.5)
        on = MomentumStrategy(divergence_window_days=5, divergence_cum_atr=1.0).compute_signal(snap)
        assert on.metadata["divergence_window_atr_observed"] > 0
        assert not on.metadata["divergence_applied"]

    def test_both_tests_firing_decay_once(self) -> None:
        snap = _snapshot(103.0, _bars(GRIND, DATES), day_chg=-3.0)
        day_only = MomentumStrategy(divergence_day_change_atr=0.5).compute_signal(snap)
        both = MomentumStrategy(
            divergence_day_change_atr=0.5, divergence_window_days=5, divergence_cum_atr=1.0
        ).compute_signal(snap)
        assert day_only.metadata["divergence_applied"]
        assert both.metadata["divergence_basis"] == "both"
        assert both.value == pytest.approx(day_only.value), "one decay, not two"

    @pytest.mark.parametrize(
        "params, ok",
        [
            ({"divergence_window_days": 5}, True),
            ({"divergence_window_days": 1}, True),
            ({"divergence_window_days": 0}, False),
            ({"divergence_window_days": 21}, False),
            ({"divergence_window_days": 2.5}, False),
            ({"divergence_cum_atr": None}, True),
            ({"divergence_cum_atr": 1.0}, True),
            ({"divergence_cum_atr": 0}, False),
            ({"divergence_cum_atr": 6.0}, False),
        ],
    )
    def test_validation(self, params, ok) -> None:
        assert (MomentumStrategy().validate_parameters(params) == []) is ok

    def test_round_trip(self) -> None:
        s = MomentumStrategy()
        s.set_parameters({"divergence_window_days": 5, "divergence_cum_atr": 1.0})
        p = s.get_parameters()
        assert (p["divergence_window_days"], p["divergence_cum_atr"]) == (5, 1.0)
        s.set_parameters({"divergence_cum_atr": None})
        assert s.get_parameters()["divergence_cum_atr"] is None


# ── trend_persistence ─────────────────────────────────────────────────────


def _grind_down(*, stack: str) -> MarketSnapshot:
    """Six down days, about 1.6 ATR in all, every one of them in the same direction."""
    closes = [110.0, 109.0, 108.0, 107.0, 106.0, 105.0, 103.6]
    stacks = {
        "bull": dict(ema_9=101.0, ema_21=95.0, sma_20=96.0, sma_50=85.0),  # price > EMA9 > EMA21
        "bear": dict(ema_9=105.0, ema_21=107.0, sma_20=106.0, sma_50=108.0),
        "none": dict(ema_9=104.0, ema_21=100.0, sma_20=96.0, sma_50=85.0),  # price below EMA9
    }
    return _snapshot(103.6, _bars(closes, DATES), **stacks[stack])


class TestTrendPersistenceStackModifier:
    def test_off_by_default_the_stack_still_vetoes(self) -> None:
        for stack in ("bull", "none"):
            sig = TrendPersistenceStrategy().compute_signal(_grind_down(stack=stack))
            assert sig.value == 0.0, stack
            assert sig.metadata["stack_gate"] == "hard"

    def test_with_the_modifier_the_grind_speaks_scaled_by_the_stack(self) -> None:
        hard = TrendPersistenceStrategy().compute_signal(_grind_down(stack="bear"))
        assert hard.value < 0, "precondition: the aligned case fires under the hard gate"
        soft = TrendPersistenceStrategy(counter_stack_scale=0.5, no_stack_scale=0.75)
        aligned = soft.compute_signal(_grind_down(stack="bear"))
        none = soft.compute_signal(_grind_down(stack="none"))
        counter = soft.compute_signal(_grind_down(stack="bull"))
        assert aligned.value == pytest.approx(hard.value), "aligned firings are unchanged"
        assert none.value == pytest.approx(hard.value * 0.75)
        assert counter.value == pytest.approx(hard.value * 0.5)
        assert (aligned.metadata["leg"], none.metadata["leg"], counter.metadata["leg"]) == (
            "with_stack",
            "no_stack",
            "counter_stack",
        )
        assert counter.metadata["stack_gate"] == "modifier"

    def test_the_other_gates_still_apply(self) -> None:
        """The modifier replaces only the stack gate. A drift that is not a
        grind (low efficiency) stays silent."""
        choppy = [110.0, 104.0, 109.0, 103.0, 108.0, 102.0, 103.6]
        snap = _snapshot(103.6, _bars(choppy, DATES), ema_9=101.0, ema_21=95.0)
        sig = TrendPersistenceStrategy(counter_stack_scale=0.5, no_stack_scale=0.75).compute_signal(
            snap
        )
        assert sig.value == 0.0

    @pytest.mark.parametrize(
        "params, ok",
        [
            ({"counter_stack_scale": 0.5, "no_stack_scale": 0.75}, True),
            ({"counter_stack_scale": None, "no_stack_scale": None}, True),
            ({"counter_stack_scale": 0.5}, False),
            ({"counter_stack_scale": 0.5, "no_stack_scale": None}, False),
            ({"counter_stack_scale": 1.5, "no_stack_scale": 0.75}, False),
            ({"counter_stack_scale": 0.5, "no_stack_scale": -0.1}, False),
        ],
    )
    def test_validation(self, params, ok) -> None:
        assert (TrendPersistenceStrategy().validate_parameters(params) == []) is ok


# ── Parsing a review whose text contains a bracket ────────────────────────


class TestBracketsInsideJsonStrings:
    """The review tool failed four submissions on a finding that read
    "(0,5] daily ATRs": the bracket closed the top-level array early."""

    PAYLOAD = '[{"finding": "must be in (0,5] daily ATRs", "n": 1}, {"finding": "ok"}]'

    def test_valid_json_parses_as_is(self) -> None:
        assert safe_parse_json(self.PAYLOAD)[0]["finding"] == "must be in (0,5] daily ATRs"

    def test_fenced_or_wrapped_json_still_extracts(self) -> None:
        wrapped = f"Here it is:\n```json\n{self.PAYLOAD}\n```\nthanks"
        assert safe_parse_json(wrapped) == safe_parse_json(self.PAYLOAD)
        assert safe_parse_json(f"prefix {self.PAYLOAD} suffix")[1] == {"finding": "ok"}

    def test_an_escaped_quote_does_not_end_the_string(self) -> None:
        text = 'x {"a": "say \\"}\\" ok", "b": [1]} tail'
        assert extract_balanced_bracket(text) == '{"a": "say \\"}\\" ok", "b": [1]}'
