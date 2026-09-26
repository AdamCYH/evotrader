from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.trace import ProxyTracerProvider

from evotrader.config import AppConfig
from evotrader.main import _init_telemetry
from evotrader.web.server import create_app


@pytest.fixture(autouse=True)
def reset_otel_provider():
    # Before test: reset OTel trace provider to clean proxy state
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE._done = False

    yield

    # After test: reset to clean state again so subsequent tests don't get polluted
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE._done = False
    if "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS" in os.environ:
        del os.environ["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"]
    if "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT" in os.environ:
        del os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"]

    # Reset global server active app to avoid polluting other test cases
    import evotrader.web.server as server_module

    server_module._active_app = None


@pytest.fixture
def temp_db_dir():
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir)


def test_telemetry_initialization(temp_db_dir):
    # Initialize telemetry
    _init_telemetry(temp_db_dir)

    # Check that environment variables are set
    assert os.environ.get("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS") == "true"
    assert os.environ.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT") == "SPAN_ONLY"

    # Check that tracer provider is not ProxyTracerProvider (i.e. it was set)
    provider = trace.get_tracer_provider()
    assert not isinstance(provider, ProxyTracerProvider)

    # Verify the telemetry file exists
    telemetry_db = temp_db_dir / "telemetry.db"
    assert telemetry_db.exists()


def test_span_export_and_api_retrieval(temp_db_dir):
    # Initialize telemetry with temporary directory
    _init_telemetry(temp_db_dir)

    # Obtain a tracer and start a mock span named "call_llm"
    tracer = trace.get_tracer("gcp.vertex.agent")

    with tracer.start_as_current_span("call_llm") as span:
        span.set_attribute("gen_ai.system", "gcp.vertex.agent")
        span.set_attribute("gen_ai.request.model", "gemini-1.5-pro")
        span.set_attribute("gcp.vertex.agent.session_id", "test-session-123")
        span.set_attribute("gen_ai.usage.input_tokens", 100)
        span.set_attribute("gen_ai.usage.output_tokens", 50)
        span.set_attribute(
            "gcp.vertex.agent.llm_request",
            '{"model": "gemini-1.5-pro", "contents": [{"role": "user", "parts": [{"text": "Hello world"}]}]}',
        )
        span.set_attribute(
            "gcp.vertex.agent.llm_response",
            '{"content": {"role": "model", "parts": [{"text": "Hello user"}]}}',
        )

    # Flush / write. Since SimpleSpanProcessor is synchronous, it writes immediately.
    import json
    import sqlite3

    telemetry_db = temp_db_dir / "telemetry.db"
    conn = sqlite3.connect(str(telemetry_db))
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("SELECT * FROM spans WHERE name = 'call_llm'")
    rows = cursor.fetchall()
    conn.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "test-session-123"
    attrs = json.loads(row["attributes_json"])
    assert attrs["gen_ai.request.model"] == "gemini-1.5-pro"
    assert attrs["gen_ai.usage.input_tokens"] == 100
    assert attrs["gen_ai.usage.output_tokens"] == 50

    # Let's verify /api/telemetry/llm endpoint
    from evotrader.db.connection import Database
    from evotrader.db.journal import TradeJournal
    from evotrader.db.metrics import MetricsStore

    db_mock = MagicMock(spec=Database)
    db_mock._db_path = temp_db_dir / "test.db"
    journal_mock = MagicMock(spec=TradeJournal)
    metrics_mock = MagicMock(spec=MetricsStore)
    mcp_mock = MagicMock()
    config = AppConfig()
    config.db_dir = temp_db_dir

    app = create_app(
        db=db_mock,
        journal=journal_mock,
        metrics=metrics_mock,
        mcp_toolset=mcp_mock,
        config=config,
        runner_fn=MagicMock(),
        memory=MagicMock(),
    )

    # Configure authorization headers dynamically based on environment password settings
    headers = {}
    if "DASHBOARD_PASSWORD" in os.environ:
        headers["Authorization"] = f"Bearer {os.environ['DASHBOARD_PASSWORD']}"

    client = TestClient(app)
    response = client.get("/api/telemetry/llm?session_id=test-session-123", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert "logs" in data
    assert len(data["logs"]) == 1
    log = data["logs"][0]
    assert log["session_id"] == "test-session-123"
    assert log["model"] == "gemini-1.5-pro"
    assert log["input_tokens"] == 100
    assert log["output_tokens"] == 50
    assert log["request"]["model"] == "gemini-1.5-pro"
    assert log["response"]["content"]["role"] == "model"


def test_get_llm_metrics_summary(temp_db_dir):
    _init_telemetry(temp_db_dir)

    tracer = trace.get_tracer("gcp.vertex.agent")

    # Span 1: trading cycle session
    with tracer.start_as_current_span("call_llm") as span:
        span.set_attribute("gen_ai.system", "gcp.vertex.agent")
        span.set_attribute("gen_ai.request.model", "gemini-1.5-pro")
        span.set_attribute("gcp.vertex.agent.session_id", "cycle-20260814-100000")
        span.set_attribute("gcp.vertex.agent.name", "StrategyAgent")
        span.set_attribute("gen_ai.usage.input_tokens", 500)
        span.set_attribute("gen_ai.usage.output_tokens", 200)

    # Span 2: evolution session
    with tracer.start_as_current_span("call_llm") as span:
        span.set_attribute("gen_ai.system", "gcp.vertex.agent")
        span.set_attribute("gen_ai.request.model", "gemini-1.5-flash")
        span.set_attribute("gcp.vertex.agent.session_id", "evol-20260814-200000")
        span.set_attribute("gcp.vertex.agent.name", "EvolutionAgent")
        span.set_attribute("gen_ai.usage.input_tokens", 1200)
        span.set_attribute("gen_ai.usage.output_tokens", 800)

    from evotrader.db.telemetry import TelemetryReader

    reader = TelemetryReader(temp_db_dir / "telemetry.db")
    summary = reader.get_llm_metrics_summary()

    assert summary["total_calls"] == 2
    assert summary["total_tokens"] == 2700
    assert "session_metrics" in summary
    assert len(summary["session_metrics"]) == 2

    # Map by session ID
    by_sid = {s["session_id"]: s for s in summary["session_metrics"]}
    assert "cycle-20260814-100000" in by_sid
    assert by_sid["cycle-20260814-100000"]["session_type"] == "trading"
    assert by_sid["cycle-20260814-100000"]["input_tokens"] == 500
    assert by_sid["cycle-20260814-100000"]["output_tokens"] == 200
    assert by_sid["cycle-20260814-100000"]["last_active"] is not None
    assert by_sid["cycle-20260814-100000"]["last_active_ts"] > 0

    assert "evol-20260814-200000" in by_sid
    assert by_sid["evol-20260814-200000"]["session_type"] == "evolution"
    assert by_sid["evol-20260814-200000"]["input_tokens"] == 1200
    assert by_sid["evol-20260814-200000"]["output_tokens"] == 800
