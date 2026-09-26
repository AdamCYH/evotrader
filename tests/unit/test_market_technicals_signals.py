from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from evotrader.config import AppConfig
from evotrader.db.connection import Database
from evotrader.db.journal import TradeJournal
from evotrader.db.metrics import MetricsStore
from evotrader.db.thought_log import ThoughtLogger
from evotrader.indicators import (
    IndicatorFormat,
    format_indicator_value,
    get_indicator_spec,
    normalize_snapshot_payload,
)
from evotrader.web.server import create_app


def test_indicator_registry_and_formatting():
    """Verify that the centralized indicator registry formats known and dynamic indicators properly."""
    # 1. Known indicators
    rsi_spec = get_indicator_spec("rsi_14")
    assert rsi_spec.label == "RSI (14)"
    assert rsi_spec.format == IndicatorFormat.OSCILLATOR
    assert format_indicator_value("rsi_14", 55.456) == "55.5"

    vwap_spec = get_indicator_spec("vwap")
    assert vwap_spec.format == IndicatorFormat.CURRENCY
    assert format_indicator_value("vwap", 502.5) == "$502.50"

    bb_width_spec = get_indicator_spec("bollinger_width")
    assert bb_width_spec.format == IndicatorFormat.PERCENT_NORM
    assert format_indicator_value("bollinger_width", 0.0345) == "3.45%"

    # 2. Dynamic / self-evolved indicators
    dyn_spec = get_indicator_spec("keltner_channel_upper")
    assert dyn_spec.format == IndicatorFormat.CURRENCY
    assert dyn_spec.label == "Keltner Channel Upper"

    pct_spec = get_indicator_spec("squeeze_momentum_pct")
    assert pct_spec.format == IndicatorFormat.PERCENT

    vol_spec = get_indicator_spec("dark_pool_vol")
    assert vol_spec.format == IndicatorFormat.INTEGER
    assert format_indicator_value("dark_pool_vol", 1500000) == "1,500,000"


def test_normalize_snapshot_payload_dynamic():
    """Verify normalize_snapshot_payload generically preserves all standard and custom signals."""
    raw = {
        "ticker": "NVDA",
        "quote": {"last": 128.50, "ticker": "NVDA"},
        "indicators": {
            "rsi_14": 70.2,
            "vwap": 127.10,
            "supertrend": 125.00,
            "gamma_exposure": 450000,
            "custom_alpha_score": 0.85,
        },
        "regime": {"regime": "TRENDING_BULL", "confidence": 0.92},
        "algo_signal": {
            "composite_signal": 0.75,
            "algo_version": "v003_evolved",
            "sub_signals": [
                {"name": "gamma_scalp", "value": 0.8, "weight": 0.5},
                {"name": "supertrend_follow", "value": 0.7, "weight": 0.5},
            ],
        },
    }

    normalized = normalize_snapshot_payload(raw)
    assert normalized["ticker"] == "NVDA"
    assert normalized["close"] == 128.50
    assert normalized["composite_signal"] == 0.75
    assert normalized["regime"] == "TRENDING_BULL"
    assert normalized["regime_confidence"] == 0.92
    assert normalized["algo_version"] == "v003_evolved"
    assert len(normalized["sub_signals"]) == 2

    # All dynamic indicators are preserved both in dictionary and top-level
    assert normalized["indicators"]["supertrend"] == 125.00
    assert normalized["supertrend"] == 125.00
    assert normalized["indicators"]["gamma_exposure"] == 450000
    assert normalized["gamma_exposure"] == 450000
    assert normalized["custom_alpha_score"] == 0.85


@pytest.mark.asyncio
async def test_market_snapshots_signals_storage_and_retrieval(tmp_path):
    """Verify that market snapshots store and retrieve composite signals, regime, and sub_signals."""
    db_path = tmp_path / "test_signals.db"
    db = Database(db_path)
    await db.initialize()

    logger = ThoughtLogger(db)

    # 1. Simulate gather_market_data tool response with custom and self-evolved indicators
    sub_signals = [
        {"name": "momentum", "value": 0.65, "weight": 0.25, "metadata": {"rsi": 62.5}},
        {"name": "mean_reversion", "value": -0.10, "weight": 0.20, "metadata": {"zscore": 0.4}},
        {"name": "intraday_vwap_zscore", "value": 0.35, "weight": 0.15, "metadata": {}},
        {"name": "range_break_continuation", "value": 0.80, "weight": 0.15, "metadata": {}},
        {"name": "trend_persistence", "value": 0.50, "weight": 0.10, "metadata": {}},
        {"name": "options_positioning", "value": 0.20, "weight": 0.05, "metadata": {}},
        {"name": "swing_failure_reversal", "value": 0.00, "weight": 0.10, "metadata": {}},
    ]
    meta_response = {
        "ticker": "QQQ",
        "indicators": {
            "rsi_14": 62.5,
            "bollinger_upper": 510.0,
            "bollinger_middle": 500.0,
            "bollinger_lower": 490.0,
            "bollinger_width": 0.04,
            "vwap": 499.5,
            "adx_14": 28.4,
            "atr_14": 4.12,
            "supertrend_level": 495.0,  # Self-evolved indicator
            "order_imbalance_ratio": 1.45,  # Self-evolved indicator
        },
        "quote": {"last": 502.50, "ticker": "QQQ"},
        "regime": {"regime": "TRENDING_BULL", "confidence": 0.88},
        "algo_signal": {
            "composite_signal": 0.62,
            "algo_version": "v001",
            "sub_signals": sub_signals,
        },
    }

    session_id = "test-session-123"
    await logger.record_event(
        session_id=session_id,
        agent_name="orchestrator",
        event_type="tool_response",
        content="gather_market_data",
        meta={"response": meta_response},
    )

    # 2. Retrieve snapshots from DB
    snapshots = await logger.get_market_snapshots(limit=10)
    assert len(snapshots) == 1
    snap = snapshots[0]

    assert snap["ticker"] == "QQQ"
    assert snap["session_id"] == session_id
    assert snap["rsi_14"] == 62.5
    assert snap["close"] == 502.50
    assert snap["composite_signal"] == 0.62
    assert snap["regime"] == "TRENDING_BULL"
    assert snap["regime_confidence"] == 0.88
    assert len(snap["sub_signals"]) == 7
    assert snap["sub_signals"][0]["name"] == "momentum"
    assert snap["sub_signals"][0]["value"] == 0.65

    # Check that self-evolved indicators are dynamically present without dedicated columns
    assert snap["indicators"]["supertrend_level"] == 495.0
    assert snap["supertrend_level"] == 495.0
    assert snap["order_imbalance_ratio"] == 1.45

    await db.close()


@pytest.mark.asyncio
async def test_api_tech_chart_endpoint(tmp_path):
    """Verify that /api/chart/tech returns points and latest snapshot with full signal fields."""
    db_path = tmp_path / "test_api_signals.db"
    db = Database(db_path)
    await db.initialize()

    thought_logger = ThoughtLogger(db)
    journal = TradeJournal(db)
    metrics = MetricsStore(db)

    # Insert test snapshot with dynamic indicators
    meta_response = {
        "ticker": "SPY",
        "indicators": {
            "rsi_14": 45.0,
            "vwap": 550.0,
            "adx_14": 22.0,
            "custom_signal_score": 0.42,
        },
        "quote": {"last": 551.0, "ticker": "SPY"},
        "regime": {"regime": "RANGE_BOUND", "confidence": 0.75},
        "algo_signal": {
            "composite_signal": 0.05,
            "algo_version": "v002",
            "sub_signals": [{"name": "momentum", "value": 0.1, "weight": 0.5}],
        },
    }
    await thought_logger.record_event(
        session_id="session-spy-1",
        agent_name="orchestrator",
        event_type="tool_response",
        content="gather_market_data",
        meta={"response": meta_response},
    )

    config = AppConfig(data_dir=tmp_path / "data")

    app = create_app(
        db=db,
        journal=journal,
        metrics=metrics,
        mcp_toolset=MagicMock(),
        config=config,
        runner_fn=AsyncMock(),
        memory=MagicMock(),
    )
    client = TestClient(app)
    pwd = os.environ.get("DASHBOARD_PASSWORD", "test_password")
    client.headers.update({"Authorization": f"Bearer {pwd}"})

    # Call endpoint
    response = client.get("/api/chart/tech")
    assert response.status_code == 200
    data = response.json()

    assert "points" in data
    assert "latest" in data
    assert len(data["points"]) == 1
    latest = data["latest"]
    assert latest["ticker"] == "SPY"
    assert latest["composite_signal"] == 0.05
    assert latest["regime"] == "RANGE_BOUND"
    assert len(latest["sub_signals"]) == 1
    assert latest["indicators"]["custom_signal_score"] == 0.42
    assert latest["custom_signal_score"] == 0.42

    await db.close()
