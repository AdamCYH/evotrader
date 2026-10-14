"""The primary read against its bitcoin proxy, since the prior close.

Proposed by the evolution agent (p_new_btc_lead_residual): every channel read
the primary's own tape, and the only outside reading was prose. The channel
takes the primary's return since its prior close, minus beta times the
reference's return since its own prior close, in the primary's ATR, and votes
toward convergence past a band. The reference (``asset.reference_ticker``)
reaches it on the snapshot from the market-data tool. Shadow at first: weight
0 everywhere.

Made-up tickers, prices and dates (2026-03 is before daylight saving time).
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from evotrader.algorithms.strategies.btc_lead_residual import (
    BtcLeadResidualStrategy,
    daily_beta,
)
from evotrader.models.market import (
    OHLCV,
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)
from evotrader.tools.market_hours import ET

TODAY = date(2026, 3, 4)


def _sessions(n: int) -> list[date]:
    days, d = [], TODAY - timedelta(days=1)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return sorted(days)


#: Small, uneven daily moves of the reference, and the primary at about 2x them.
REF_MOVES = [0.012, -0.008, 0.015, -0.004, 0.009, -0.013, 0.006, 0.011, -0.007, 0.003] * 3


def _closes(moves: list[float], start: float) -> list[float]:
    out = [start]
    for m in moves:
        out.append(out[-1] * (1 + m))
    return out


def _series(factor: float = 2.0, noise: float = 0.001) -> tuple[list[OHLCV], list[dict]]:
    days = _sessions(len(REF_MOVES) + 1)
    ref = _closes(REF_MOVES, 50.0)
    primary_moves = [factor * m + noise * (-1) ** i for i, m in enumerate(REF_MOVES)]
    primary = _closes(primary_moves, 100.0)
    candles = [
        OHLCV(
            timestamp=datetime(d.year, d.month, d.day), open=c, high=c, low=c, close=c, volume=1e6
        )
        for d, c in zip(days, primary, strict=True)
    ]
    ref_closes = [{"date": d.isoformat(), "close": c} for d, c in zip(days, ref, strict=True)]
    return candles, ref_closes


def _snapshot(
    *,
    price: float = 101.0,
    prior: float = 100.0,
    ref_price: float | None = 51.0,
    ref_prior: float = 50.0,
    ref_age: float | None = 2.0,
    ibs: float | None = 0.5,
    at: datetime | None = None,
    series: tuple[list[OHLCV], list[dict]] | None = None,
) -> MarketSnapshot:
    candles, ref_closes = series or _series()
    when = at or datetime(2026, 3, 4, 11, 0, tzinfo=ET)
    extra = {}
    if ref_price is not None:
        extra["reference"] = {
            "ticker": "REF",
            "price": ref_price,
            "previous_close": ref_prior,
            "quote_age_minutes": ref_age,
            "daily_closes": ref_closes,
        }
    return MarketSnapshot(
        ticker="XYZ",
        timestamp=when,
        quote=Quote(
            ticker="XYZ",
            bid=price - 0.01,
            ask=price + 0.01,
            last=price,
            volume=1e6,
            timestamp=when,
            previous_close=prior,
        ),
        indicators=TechnicalIndicators(atr_14=5.0, ibs=ibs),
        regime=RegimeClassification(regime=MarketRegime.RANGE_BOUND, confidence=0.5, reasoning="t"),
        daily_candles=candles,
        **extra,
    )


CHANNEL = BtcLeadResidualStrategy()


class TestWhenItAbstains:
    def test_without_a_reference_it_says_so(self) -> None:
        out = CHANNEL.compute_signal(_snapshot(ref_price=None))
        assert (out.value, out.metadata["applicable"]) == (0.0, False)
        assert out.metadata["reason"] == "no_reference_feed"

    def test_a_stale_reference_quote(self) -> None:
        out = CHANNEL.compute_signal(_snapshot(ref_age=30.0))
        assert out.metadata["reason"] == "stale_reference"

    def test_the_opening_auction(self) -> None:
        out = CHANNEL.compute_signal(_snapshot(at=datetime(2026, 3, 4, 9, 40, tzinfo=ET)))
        assert out.metadata["reason"] == "opening_auction_window"

    def test_a_fit_that_explains_little(self) -> None:
        candles, ref = _series(factor=0.0, noise=0.02)  # the primary ignores the reference
        out = CHANNEL.compute_signal(_snapshot(series=(candles, ref)))
        assert out.metadata["reason"] == "beta_unreliable"

    def test_too_few_sessions(self) -> None:
        candles, ref = _series()
        out = CHANNEL.compute_signal(_snapshot(series=(candles[-4:], ref[-4:])))
        assert out.metadata["reason"] == "beta_unavailable"


class TestTheBeta:
    def test_the_slope_of_daily_returns(self) -> None:
        candles, ref = _series()
        primary = {c.timestamp.date().isoformat(): c.close for c in candles}
        reference = {r["date"]: r["close"] for r in ref}
        beta, r2, n = daily_beta(primary, reference, 20)
        assert n == 20
        assert beta == pytest.approx(2.0, abs=0.1)
        assert r2 > 0.9

    def test_todays_forming_bar_is_left_out(self) -> None:
        candles, ref = _series()
        forming = OHLCV(timestamp=datetime(2026, 3, 4), open=1, high=1, low=1, close=1, volume=1)
        out = CHANNEL.compute_signal(_snapshot(series=([*candles, forming], ref)))
        assert out.metadata["aligned_returns"] == 20

    def test_it_is_clamped(self) -> None:
        out = CHANNEL.compute_signal(_snapshot(series=_series(factor=4.0)))
        assert out.metadata["beta"] > 3.0 and out.metadata["beta_clamped"] == 3.0


class TestTheVote:
    def _expected(self, price: float, r_primary: float, r_reference: float) -> float:
        candles, ref = _series()
        beta, _, _ = daily_beta(
            {c.timestamp.date().isoformat(): c.close for c in candles},
            {r["date"]: r["close"] for r in ref},
            20,
        )
        residual = (r_primary - min(max(beta, 1.0), 3.0) * r_reference) * price / 5.0
        depth = min((abs(residual) - 0.35) / 0.5, 1.0)
        return -math.copysign(depth * 0.7, residual)

    def test_lagging_the_reference_is_a_long(self) -> None:
        out = CHANNEL.compute_signal(_snapshot(price=101.0, ref_price=51.0))  # +1% vs +2%
        assert out.metadata["leg"] == "lagging" and out.value > 0
        assert out.value == pytest.approx(self._expected(101.0, 0.01, 0.02))

    def test_leading_it_is_a_short(self) -> None:
        out = CHANNEL.compute_signal(_snapshot(price=105.0, ref_price=50.5))  # +5% vs +1%
        assert out.metadata["leg"] == "leading" and out.value < 0
        assert out.value == pytest.approx(self._expected(105.0, 0.05, 0.01))

    def test_inside_the_band_it_is_out_of_scope(self) -> None:
        out = CHANNEL.compute_signal(_snapshot(price=104.0, ref_price=51.0))  # +4% vs +2%
        assert out.value == 0.0
        assert (out.metadata["in_scope"], out.metadata["out_of_scope_reason"]) == (
            False,
            "within_band",
        )

    @pytest.mark.parametrize(
        ("price", "ref_price", "ibs"), [(101.0, 51.0, 0.1), (105.0, 50.5, 0.9)]
    )
    def test_at_the_sessions_extreme_it_is_damped(self, price, ref_price, ibs) -> None:
        plain = CHANNEL.compute_signal(_snapshot(price=price, ref_price=ref_price, ibs=0.5))
        damped = CHANNEL.compute_signal(_snapshot(price=price, ref_price=ref_price, ibs=ibs))
        assert damped.value == pytest.approx(plain.value * 0.5)
        assert damped.metadata["session_extreme_applied"] is True

    def test_before_the_open_it_votes(self) -> None:
        """The overnight move is the point: a pre-market cycle reads it."""
        out = CHANNEL.compute_signal(_snapshot(at=datetime(2026, 3, 4, 8, 30, tzinfo=ET)))
        assert out.metadata["applicable"] is True


class TestTheParameters:
    def test_they_round_trip(self) -> None:
        channel = BtcLeadResidualStrategy(entry_atr=0.5, beta_window=15)
        params = channel.get_parameters()
        assert (params["entry_atr"], params["beta_window"]) == (0.5, 15)
        channel.set_parameters({"extreme_dampener": 0.25})
        assert channel.get_parameters()["extreme_dampener"] == 0.25

    @pytest.mark.parametrize(
        ("params", "problem"),
        [
            ({"beta_min": 3.0, "beta_max": 2.0}, "beta_min must not exceed beta_max"),
            ({"beta_window": 3}, "beta_window must be at least 5"),
            ({"extreme_ibs": 0.7}, "extreme_ibs must be in [0, 0.5]"),
            ({"entry_atr": 0.0}, "entry_atr must be positive"),
        ],
    )
    def test_bad_values_are_refused(self, params, problem) -> None:
        assert any(problem in e for e in CHANNEL.validate_parameters(params))

    def test_the_loader_gives_it_no_weight_unless_a_version_does(self) -> None:
        from evotrader import paths
        from evotrader.algorithms.loader import StrategyLoader

        loader = StrategyLoader(
            paths.project_root() / "starter_data" / "algorithms" / "strategy_manifest.yaml"
        )
        assert loader.build_composite({})._weights["btc_lead_residual"] == 0.0


# ── The reference reaches the snapshot ─────────────────────────────────────


def _broker(calls: list[dict]):
    daily = [
        {
            "begins_at": f"{d.isoformat()}T00:00:00Z",
            "open_price": "50",
            "high_price": "51",
            "low_price": "49",
            "close_price": str(50 + i * 0.1),
            "volume": "1000",
        }
        for i, d in enumerate([*_sessions(30), TODAY])
    ]

    async def call(tool_name, arguments):
        calls.append({"tool": tool_name, **arguments})
        if tool_name == "get_equity_quotes":
            symbol = arguments["symbols"][0]
            last, prior = ("51.0", "50.0") if symbol == "REF" else ("101.0", "100.0")
            return {
                "data": {
                    "results": [
                        {
                            "quote": {
                                "bid_price": last,
                                "ask_price": last,
                                "last_trade_price": last,
                                "previous_close": prior,
                                "volume": "1000",
                                "updated_at": "2026-03-04T15:58:00Z",
                            }
                        }
                    ]
                }
            }
        if tool_name == "get_equity_historicals":
            return {"data": {"results": [{"bars": daily}]}}
        return None

    return call


class TestThePlumbing:
    async def test_the_reading_is_the_quote_and_completed_closes(self) -> None:
        from evotrader.agents.tools import _reference_reading

        now = datetime(2026, 3, 4, 16, 0, tzinfo=UTC)  # 11:00 ET
        with patch("evotrader.agents.tools._call_mcp_tool", side_effect=_broker([])):
            reading = await _reference_reading("REF", now)
        assert (reading["ticker"], reading["price"], reading["previous_close"]) == (
            "REF",
            51.0,
            50.0,
        )
        assert reading["quote_age_minutes"] == pytest.approx(2.0)
        assert all(c["date"] < "2026-03-04" for c in reading["daily_closes"]), "no forming bar"
        assert len(reading["daily_closes"]) == 30

    def test_the_snapshot_keeps_it(self) -> None:
        from evotrader.agents.tools import normalize_market_snapshot

        raw = {
            "ticker": "XYZ",
            "quote": {"last": 101.0, "bid": 101.0, "ask": 101.0, "previous_close": 100.0},
            "reference": {
                "ticker": "REF",
                "price": "51.0",
                "previous_close": 50.0,
                "quote_age_minutes": 2,
                "daily_closes": [{"date": "2026-03-03", "close": "50.0"}, {"junk": 1}],
            },
        }
        snapshot = MarketSnapshot.model_validate(normalize_market_snapshot(raw))
        reference = snapshot.model_extra["reference"]
        assert (reference["price"], reference["daily_closes"]) == (
            51.0,
            [{"date": "2026-03-03", "close": 50.0}],
        )
        broken = {**raw, "reference": {"price": "n/a"}}
        assert normalize_market_snapshot(broken)["reference"] is None

    @pytest.mark.parametrize("configured", [True, False])
    async def test_the_market_data_tool_asks_for_it_only_when_set(
        self, configured, monkeypatch, tmp_path: Path
    ) -> None:
        from evotrader.agents import tools
        from evotrader.config import AppConfig
        from evotrader.tools import asset_context

        config = AppConfig(data_dir=tmp_path / "data")
        config.settings.asset.primary_ticker = "XYZ"
        config.constitution.trading_rules.allowed_tickers = ["XYZ"]
        config.settings.asset.reference_ticker = "REF" if configured else None
        monkeypatch.setattr(tools, "_config", config)
        monkeypatch.setattr(asset_context, "_primary_ticker", asset_context._primary_ticker)
        monkeypatch.setattr(asset_context, "_allowed_tickers", asset_context._allowed_tickers)
        asset_context.bind_asset_context(config)
        monkeypatch.setenv("EVOTRADER_MOCK_TIME", "2026-03-04T11:00:00-05:00")
        calls: list[dict] = []
        with patch("evotrader.agents.tools._call_mcp_tool", side_effect=_broker(calls)):
            await tools.gather_market_data("XYZ")
        asked = [json.dumps(c) for c in calls if "REF" in json.dumps(c)]
        assert bool(asked) is configured
