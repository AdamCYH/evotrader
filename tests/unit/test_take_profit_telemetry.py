"""Where the upside stands, computed: resting take-profits, the best move since entry, T1.

See: data/evolution/reviews/
20261007_201905_journal_record_trade_idempotent_by_order_id_and_take_profit_coverage.md
(finding 3)

``protective_coverage`` answers the downside question. Nothing answered the
upside one: whether any take-profit rests, how far the first target (T1) is
from the price, and how far each lot ever got (its maximum favourable
excursion). The strategy agent worked the T1 distance out by hand every cycle,
and its answers moved by a couple of tenths of an ATR between cycles; how close
positions come to their target before they are stopped or trimmed lived only
in notes. Now ``take_profit_coverage`` and, per lot, ``gain_atr``, ``mfe_atr``
and ``t1_distance_atr`` say it. All numbers are made up.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from evotrader.db.journal import TradeJournal
from evotrader.db.protection_audit import coverage_by_ticker, take_profit_coverage
from evotrader.models.config import PositionSizingConfig
from evotrader.models.trade import OrderType, TradeAction, TradeDirection, TradeProposal
from evotrader.tools.position_telemetry import lot_excursion

# ── The take-profit block ─────────────────────────────────────────────────


def _lot(trade_id: int, qty: float, price: float) -> dict:
    return {
        "id": trade_id,
        "ticker": "XYZ",
        "direction": "LONG",
        "quantity": qty,
        "remaining_quantity": qty,
        "price": price,
        "fill_price": price,
        "option_id": None,
    }


def _order(order_id: str, action: str, qty: float, **levels) -> dict:
    return {
        "order_id": order_id,
        "ticker": "XYZ",
        "action": action,
        "quantity": qty,
        "time_in_force": "gtc",
        "source": "broker_sync",
        "stop_price": None,
        "limit_price": None,
        **levels,
    }


LOTS = [_lot(1, 10.0, 100.0), _lot(2, 4.0, 101.0)]
BOOK = [
    _order("s1", "STOP_LOSS", 10.0, order_type="stop_market", stop_price=96.0),
    _order("t1", "TAKE_PROFIT", 4.0, order_type="limit", limit_price=106.0),
]


class TestTakeProfitCoverage:
    def test_a_resting_take_profit_is_located_in_atrs(self) -> None:
        block = coverage_by_ticker(LOTS, BOOK)["XYZ"]
        tp = take_profit_coverage(block, LOTS, "XYZ", atr=2.0, mark=103.0)
        blended = (10 * 100.0 + 4 * 101.0) / 14
        assert tp["status"] == "resting"
        assert tp["tp_qty"] == 4.0
        assert tp["tp_qty_uncovered"] == 10.0
        assert tp["blended_entry"] == pytest.approx(blended, abs=1e-4)
        [level] = tp["levels"]
        assert level["limit_price"] == 106.0
        assert level["atr_from_entry"] == pytest.approx((106.0 - blended) / 2.0, abs=1e-4)
        assert level["atr_from_mark"] == pytest.approx(1.5)

    def test_without_an_atr_the_levels_are_prices_only(self) -> None:
        block = coverage_by_ticker(LOTS, BOOK)["XYZ"]
        [level] = take_profit_coverage(block, LOTS, "XYZ")["levels"]
        assert level["atr_from_entry"] is None and level["atr_from_mark"] is None

    def test_no_take_profit_is_none(self) -> None:
        block = coverage_by_ticker(LOTS, BOOK[:1])["XYZ"]
        tp = take_profit_coverage(block, LOTS, "XYZ", atr=2.0, mark=103.0)
        assert tp["status"] == "none" and tp["levels"] == []
        assert tp["tp_qty_uncovered"] == 14.0

    def test_nothing_held_or_resting_is_nothing(self) -> None:
        assert take_profit_coverage(None, [], "XYZ") is None


# ── Each lot's excursion ──────────────────────────────────────────────────

# A lot bought on Tuesday 2026-03-03 at 10:00 ET, read on Friday 03-06 at 11:00 ET.
ENTRY = datetime(2026, 3, 3, 15, 0, tzinfo=UTC)
NOW = datetime(2026, 3, 6, 16, 0, tzinfo=UTC)


def _daily(day: str, high: float, low: float) -> dict:
    return {"timestamp": f"{day}T00:00:00+00:00", "high": high, "low": low, "close": high}


DAILY = [
    _daily("2026-03-02", 99.0, 95.0),
    _daily("2026-03-03", 110.0, 97.0),  # the entry day: its high may predate the entry
    _daily("2026-03-04", 103.0, 99.0),
    _daily("2026-03-05", 104.5, 100.5),
    _daily("2026-03-06", 102.0, 100.8),  # today, forming
]


def _excursion(**overrides):
    args = {
        "direction": "LONG",
        "entry_price": 100.0,
        "entry_time": ENTRY.isoformat(),
        "daily_bars": DAILY,
        "session_bars": [],
        "mark": 101.0,
        "atr": 2.0,
        "now": NOW,
    }
    args.update(overrides)
    return lot_excursion(**args)


class TestLotExcursion:
    def test_gain_and_best_move_since_entry(self) -> None:
        out = _excursion()
        assert out["gain_atr"] == pytest.approx(0.5)
        assert out["mfe_price"] == 104.5
        assert out["mfe_atr"] == pytest.approx(2.25)
        assert out["mfe_basis"] == "daily_bars_after_entry_session"

    def test_the_entry_days_own_high_is_left_out(self) -> None:
        """110 printed on the entry day, maybe before the entry: not counted."""
        assert _excursion()["mfe_price"] != 110.0

    def test_the_distance_to_t1_when_a_target_is_set(self) -> None:
        out = _excursion(target_atr=1.5)
        assert out["t1_price"] == pytest.approx(103.0)
        assert out["t1_distance_atr"] == pytest.approx(1.0)
        assert "t1_distance_atr" not in _excursion(), "no target configured, no claim"

    def test_past_the_target_reads_negative(self) -> None:
        assert _excursion(target_atr=1.5, mark=104.0)["t1_distance_atr"] == pytest.approx(-0.5)

    def test_a_lot_bought_today_reads_only_the_bars_after_it(self) -> None:
        entry = datetime(2026, 3, 6, 15, 30, tzinfo=UTC)  # 10:30 ET today
        session = [
            {"timestamp": "2026-03-06T14:30:00+00:00", "high": 108.0, "low": 99.0},  # 09:30
            {"timestamp": "2026-03-06T15:30:00+00:00", "high": 102.6, "low": 100.0},  # 10:30
            {"timestamp": "2026-03-06T15:35:00+00:00", "high": 101.4, "low": 100.2},
        ]
        out = _excursion(entry_time=entry.isoformat(), session_bars=session)
        assert out["mfe_price"] == 102.6
        assert out["mfe_basis"] == "session_bars_after_entry"

    def test_a_lot_that_only_fell_has_no_favourable_excursion(self) -> None:
        out = _excursion(entry_price=110.0, mark=101.0)
        assert out["mfe_atr"] == 0.0
        assert out["gain_atr"] == pytest.approx(-4.5)

    def test_a_short_lot_mirrors(self) -> None:
        out = _excursion(direction="SHORT", entry_price=103.0, mark=101.0, target_atr=1.0)
        assert out["gain_atr"] == pytest.approx(1.0)
        assert out["mfe_price"] == 99.0
        assert out["mfe_atr"] == pytest.approx(2.0)
        assert out["t1_price"] == pytest.approx(101.0)
        assert out["t1_distance_atr"] == pytest.approx(0.0)

    def test_nothing_to_measure_with_is_nothing(self) -> None:
        assert _excursion(atr=None) == {}
        assert _excursion(mark=None) == {}


class TestTheTargetSetting:
    def test_off_unless_set(self) -> None:
        assert PositionSizingConfig().target_atr_multiplier is None

    def test_the_starter_target_matches_the_starter_instruction(self) -> None:
        settings = yaml.safe_load(Path("starter_data/settings.yaml").read_text())
        target = settings["position_sizing"]["target_atr_multiplier"]
        assert target == 1.5
        instruction = Path("starter_data/instructions/strategy/v001.md").read_text()
        assert "**Target:** at least 1.5 × ATR from entry" in instruction


# ── In the tools ──────────────────────────────────────────────────────────


@pytest.fixture
def journal(db) -> TradeJournal:
    return TradeJournal(db)


def _proposal(qty: float, price: float) -> TradeProposal:
    return TradeProposal(
        ticker="T",
        direction=TradeDirection.LONG,
        action=TradeAction.OPEN,
        quantity=qty,
        order_type=OrderType.LIMIT,
        limit_price=price,
        algo_signal=0.2,
        hybrid_score=0.2,
        confidence=0.5,
        regime="trending_bull",
        algo_version="test",
    )


async def _book(journal: TradeJournal) -> None:
    await journal.record_trade(_proposal(6.0, 104.0))
    for oid, action, qty, levels in (
        ("s1", "STOP_LOSS", 6.0, {"order_type": "stop_market", "stop_price": 99.0}),
        ("t1", "TAKE_PROFIT", 2.0, {"order_type": "limit", "limit_price": 112.0}),
    ):
        row = {**_order(oid, action, qty, **levels), "ticker": "T"}
        await journal.save_pending_order(oid, json.dumps(row))


async def test_get_open_positions_carries_the_take_profit_block(journal) -> None:
    from evotrader.agents import tools

    await _book(journal)
    tools._journal = journal
    tools._open_positions_cache = None
    try:
        result = await tools.get_open_positions()
    finally:
        tools._journal = None
        tools._open_positions_cache = None
    tp = result["take_profit_coverage"]["T"]
    assert tp["status"] == "resting" and tp["tp_qty"] == 2.0
    assert tp["tp_qty_uncovered"] == 4.0


async def test_the_market_snapshot_measures_each_lot_and_the_take_profit(
    journal, monkeypatch
) -> None:
    """The whole tool, with a made-up broker (test_daily_series_today's)."""
    from evotrader.agents import tools
    from tests.unit.test_daily_series_today import _broker

    await _book(journal)
    # The lot was bought two sessions before the read.
    async with journal._db.transaction() as conn:
        await conn.execute(
            "UPDATE trades SET timestamp = ? WHERE action = 'OPEN'",
            ((datetime(2026, 3, 2, 15, 0, tzinfo=UTC)).isoformat(),),
        )
    monkeypatch.setenv("EVOTRADER_MOCK_TIME", "2026-03-04T11:00:00-05:00")
    monkeypatch.setattr(tools, "_target_atr_multiplier", lambda: 1.0)
    tools._journal = journal
    tools._open_positions_cache = None
    try:
        with patch("evotrader.agents.tools._call_mcp_tool", side_effect=_broker(110.0, [])):
            result = await tools.gather_market_data("T")
    finally:
        tools._journal = None
        tools._open_positions_cache = None

    atr = result["indicators"]["atr_14"]
    [pos] = result["open_positions"]
    assert pos["gain_atr"] == pytest.approx((110.0 - 104.0) / atr, abs=1e-3)
    assert pos["mfe_atr"] >= pos["gain_atr"]
    assert pos["t1_distance_atr"] == pytest.approx(1.0 - pos["gain_atr"], abs=1e-3)
    tp = result["take_profit_coverage"]
    assert tp["status"] == "resting"
    [level] = tp["levels"]
    assert level["atr_from_mark"] == pytest.approx((112.0 - 110.0) / atr, abs=1e-3)
    assert result["protective_coverage"]["status"] == "full"


def test_a_naive_entry_time_is_not_a_crash() -> None:
    out = _excursion(entry_time=(ENTRY.replace(tzinfo=None) - timedelta(hours=1)).isoformat())
    assert out["gain_atr"] == pytest.approx(0.5)
