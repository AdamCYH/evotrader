"""`quota.on_exhausted` must actually do something.

Until 2026-09-18 it was config that no code read. `settings.yaml` told the
operator the evolution run would fall back to the API when the subscription
window was spent; it did not — the run simply failed, and the only real
fallback was a *startup* check for a missing harness.

These tests pin all three policies, and the cost warning that has to accompany
the expensive one.
"""

from __future__ import annotations

import logging
import types
from typing import Any

import pytest

from evotrader.agents.cli.types import QuotaState, QuotaStatus
from evotrader.evolution.claude_code_service import ClaudeCodeEvolutionService


class _ThoughtLogger:
    def __init__(self) -> None:
        self.completions: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []

    async def record_run_completion(self, **kw: Any) -> None:
        self.completions.append(kw)

    async def record_event(self, **kw: Any) -> None:
        self.events.append(kw)


def _config(on_exhausted: str) -> Any:
    from evotrader.config import AppConfig
    from evotrader.models.config import CliRuntimeConfig

    cfg = AppConfig()
    # Evolution hosted on a subscription (CLI) runtime of its own, the only
    # setup in which a quota can run out.
    cfg.settings.cli_runtimes["claude_code_evolution"] = CliRuntimeConfig(driver="claude_code")
    cfg.settings.agent_runtime["evolution"] = "claude_code_evolution"
    _, runtime = cfg.settings.runtime_for("evolution")
    assert runtime is not None, "evolution must be on a CLI runtime for this test"
    runtime.quota.on_exhausted = on_exhausted
    return cfg


def _service(on_exhausted: str, api_fallback: Any = None) -> ClaudeCodeEvolutionService:
    svc = ClaudeCodeEvolutionService(
        thought_logger=_ThoughtLogger(),
        config=_config(on_exhausted),
        instructions="test",
        api_fallback=api_fallback,
    )
    svc._quota = QuotaState(QuotaStatus.EXHAUSTED, detail="5-hour window spent")
    return svc


class TestTheSettingIsActuallyRead:
    async def test_skip_abandons_the_cycle_without_billing(self) -> None:
        svc = _service("skip")
        result = await svc._on_quota_exhausted("s1", "prompt", {})

        assert result["status"] == "skipped"
        assert result["reason"] == "quota_exhausted"
        assert svc.thought_logger.completions[-1]["status"] == "SKIPPED"

    async def test_fail_raises(self) -> None:
        svc = _service("fail")
        with pytest.raises(RuntimeError, match="quota exhausted"):
            await svc._on_quota_exhausted("s1", "prompt", {})
        assert svc.thought_logger.completions[-1]["status"] == "FAILED"

    async def test_api_restarts_the_cycle_on_the_api_service(self) -> None:
        called: list[str] = []

        class _ApiService:
            async def trigger(self, user_comment: str = "") -> dict[str, Any]:
                called.append("trigger")
                return {"status": "complete", "session_id": "api-1"}

        svc = _service("api", api_fallback=lambda: _ApiService())
        result = await svc._on_quota_exhausted("s1", "prompt", {})

        assert called == ["trigger"], "the API service was never actually run"
        assert result["status"] == "complete"
        assert result["fell_back_to_api"] is True
        assert result["quota_detail"] == "5-hour window spent"

    async def test_api_without_a_wired_fallback_fails_loudly(self) -> None:
        """Better to fail than to pretend the setting worked."""
        svc = _service("api", api_fallback=None)
        with pytest.raises(RuntimeError):
            await svc._on_quota_exhausted("s1", "prompt", {})


class TestTheExpensivePathIsLoud:
    async def test_api_fallback_warns_about_billing_and_repeated_work(self, caplog) -> None:
        class _ApiService:
            async def trigger(self, user_comment: str = "") -> dict[str, Any]:
                return {"status": "complete"}

        svc = _service("api", api_fallback=lambda: _ApiService())
        with caplog.at_level(logging.ERROR):
            await svc._on_quota_exhausted("s1", "prompt", {})

        assert "COST WARNING" in caplog.text
        assert "BILLED PER TOKEN" in caplog.text
        # The operator must know the subscription run's progress is lost.
        assert "repeated" in caplog.text.lower()

    async def test_the_restart_is_recorded_in_the_thought_log(self) -> None:
        """An app-log line is not visible to the console or the next session."""

        class _ApiService:
            async def trigger(self, user_comment: str = "") -> dict[str, Any]:
                return {"status": "complete"}

        svc = _service("api", api_fallback=lambda: _ApiService())
        await svc._on_quota_exhausted("s1", "prompt", {})

        blob = " ".join(str(e) for e in svc.thought_logger.events)
        assert "quota exhausted" in blob or "restarting this cycle" in blob


class TestDetection:
    """One notion of 'exhausted', shared with the agent adapter."""

    async def test_a_rate_limit_event_sets_the_quota_state(self) -> None:
        svc = _service("skip")
        svc._quota = None

        info = types.SimpleNamespace(
            status="rejected",
            overage_status="disabled",
            utilization=1.0,
            resets_at=None,
            rate_limit_type="5h",
            overage_disabled_reason="window spent",
        )
        msg = type("RateLimitEvent", (), {"rate_limit_info": info})()
        await svc._handle_message("s1", msg, {})

        assert svc._quota is not None
        assert svc._quota.status is QuotaStatus.EXHAUSTED

    async def test_overage_allowed_is_a_warning_not_the_wall(self) -> None:
        """Pay-as-you-go still spends real money — but it is not exhaustion."""
        svc = _service("skip")
        svc._quota = None

        info = types.SimpleNamespace(
            status="rejected",
            overage_status="allowed",
            utilization=1.0,
            resets_at=None,
            rate_limit_type="5h",
            overage_disabled_reason=None,
        )
        msg = type("RateLimitEvent", (), {"rate_limit_info": info})()
        await svc._handle_message("s1", msg, {})

        assert svc._quota.status is QuotaStatus.WARNING

    async def test_a_429_result_message_is_read_as_the_wall(self) -> None:
        svc = _service("skip")
        svc._quota = None

        msg = type(
            "ResultMessage",
            (),
            {
                "is_error": True,
                "api_error_status": 429,
                "errors": [],
                "result": "",
                "usage": None,
                "session_id": None,
            },
        )()
        await svc._handle_message("s1", msg, {})

        assert svc._quota is not None
        assert svc._quota.status is QuotaStatus.EXHAUSTED

    async def test_an_ordinary_error_is_not_read_as_the_wall(self) -> None:
        """A crash must not silently trigger a billed restart."""
        svc = _service("skip")
        svc._quota = None

        msg = type(
            "ResultMessage",
            (),
            {
                "is_error": True,
                "api_error_status": 500,
                "errors": ["null pointer somewhere"],
                "result": "",
                "usage": None,
                "session_id": None,
            },
        )()
        await svc._handle_message("s1", msg, {})

        assert svc._quota is None
