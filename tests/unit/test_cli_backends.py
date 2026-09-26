"""Tests for the pluggable CLI-hosted agent backends.

The headline property under test is **billing enforcement**. Every
"subscription-billed" evolution run up to 2026-09-14 was actually billed to the
Anthropic API, because the harness silently prefers ``ANTHROPIC_API_KEY`` when one
is in the environment and this project loads one from ``.env`` for its API-path
agents. There was no error, no log line, and no behavioural difference — only a
$2.88-per-run charge and a startup message that asserted the opposite.

So the guard is enforcement rather than detection: the adapter withholds the API
credential, which makes API billing *impossible* for that subprocess. The tests
below pin that down, including with a real-looking key present in ``os.environ``.
"""

from __future__ import annotations

import pytest

from evotrader.agents.cli import (
    AgentRequest,
    AgentResult,
    BillingMode,
    CliAgentBackend,
    LocalTool,
    QuotaState,
    QuotaStatus,
    ThinkingSpec,
    get_backend_class,
    load_backend,
    register_backend,
    registered_drivers,
)
from evotrader.agents.cli.claude_code import (
    _ALWAYS_BLOCKED,
    _BUILTIN_MAP,
    ClaudeCodeBackend,
    _billing_env,
    _looks_like_auth_failure,
    _quota_from_error,
    _quota_from_event,
    _thinking_option,
)

# ── Thinking ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,mode,budget",
    [
        (None, "default", None),
        ("", "default", None),
        ("default", "default", None),
        ("adaptive", "adaptive", None),
        ("off", "off", None),
        ("none", "off", None),
        (False, "off", None),
        (0, "off", None),
        (8000, "budget", 8000),
        ("8000", "budget", 8000),
    ],
)
def test_thinking_spec_parses_settings_values(value, mode, budget):
    spec = ThinkingSpec.parse(value)
    assert (spec.mode, spec.budget_tokens) == (mode, budget)


def test_thinking_spec_rejects_a_typo_rather_than_guessing():
    """A typo in a cost-relevant setting should stop startup, not pick a mode."""
    with pytest.raises(ValueError, match="thinking must be"):
        ThinkingSpec.parse("lots")


def test_default_thinking_sends_nothing_to_the_harness():
    """`default` must not pin a value we did not choose.

    Pinning the harness's current default would let a future change to it
    silently rewrite our behaviour — and on a subscription that shows up as
    reduced quota rather than a bill, which is harder to notice.
    """
    assert _thinking_option(ThinkingSpec.parse("default")) is None
    assert _thinking_option(ThinkingSpec.parse("adaptive")) == {"type": "adaptive"}
    assert _thinking_option(ThinkingSpec.parse("off")) == {"type": "disabled"}
    assert _thinking_option(ThinkingSpec.parse(9000)) == {
        "type": "enabled",
        "budget_tokens": 9000,
    }


# ── Billing enforcement ───────────────────────────────────────────


def test_subscription_and_auto_withhold_the_api_key():
    assert _billing_env(BillingMode.SUBSCRIPTION) == {"ANTHROPIC_API_KEY": ""}
    assert _billing_env(BillingMode.AUTO) == {"ANTHROPIC_API_KEY": ""}
    assert _billing_env(BillingMode.API) == {}


def test_subscription_options_withhold_the_key_even_when_one_is_set(monkeypatch):
    """The regression test for the defect this whole layer exists to prevent.

    A real key in the environment must not reach the subprocess when
    subscription billing is requested.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    backend = ClaudeCodeBackend()
    request = AgentRequest(system_prompt="s", prompt="p")

    subscription = backend._options(request, BillingMode.SUBSCRIPTION)
    assert subscription.env.get("ANTHROPIC_API_KEY") == "", (
        "the API credential must be blanked, or the run bills the API while "
        "claiming to use the subscription"
    )

    api = backend._options(request, BillingMode.API)
    assert "ANTHROPIC_API_KEY" not in api.env, (
        "api billing must pass the inherited key through untouched"
    )


def test_billing_env_never_mutates_this_process(monkeypatch):
    """The ADK fallback path in the same process still needs the real key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    import os

    _billing_env(BillingMode.SUBSCRIPTION)
    ClaudeCodeBackend()._options(
        AgentRequest(system_prompt="s", prompt="p"), BillingMode.SUBSCRIPTION
    )
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-real"


def test_mutating_and_network_tools_are_blocked_unconditionally():
    """Config must not be able to hand a hosted agent Write, Edit or Bash."""
    options = ClaudeCodeBackend()._options(
        AgentRequest(
            system_prompt="s",
            prompt="p",
            builtin_tools=("read_files", "search_files", "list_files"),
        ),
        BillingMode.SUBSCRIPTION,
    )
    for blocked in ("Write", "Edit", "Bash", "WebFetch", "WebSearch"):
        assert blocked in options.disallowed_tools
    assert set(options.tools) == {"Read", "Grep", "Glob"}


def test_unmappable_builtin_capability_is_dropped_not_passed_through():
    """A capability this harness lacks must not become a bogus tool name."""
    options = ClaudeCodeBackend()._options(
        AgentRequest(system_prompt="s", prompt="p", builtin_tools=("read_files", "teleport")),
        BillingMode.API,
    )
    assert options.tools == ["Read"]
    assert "teleport" not in _BUILTIN_MAP


def test_host_settings_are_not_inherited():
    """A run must behave the same on every machine."""
    options = ClaudeCodeBackend()._options(
        AgentRequest(system_prompt="s", prompt="p"), BillingMode.API
    )
    assert options.setting_sources == []


# ── Quota normalisation ───────────────────────────────────────────


class _Info:
    def __init__(self, **kw):
        self.status = kw.get("status")
        self.utilization = kw.get("utilization")
        self.resets_at = kw.get("resets_at")
        self.rate_limit_type = kw.get("rate_limit_type")
        self.overage_status = kw.get("overage_status")
        self.overage_disabled_reason = kw.get("overage_disabled_reason")


def test_quota_statuses_map_onto_three_actionable_states():
    assert _quota_from_event(_Info(status="allowed")).status is QuotaStatus.OK
    assert _quota_from_event(_Info(status="allowed_warning")).status is QuotaStatus.WARNING
    assert _quota_from_event(_Info(status="rejected")).status is QuotaStatus.EXHAUSTED


def test_rejected_with_allowed_overage_is_a_warning_not_ok():
    """Overage spends real money, so it must not read as business as usual."""
    state = _quota_from_event(_Info(status="rejected", overage_status="allowed"))
    assert state.status is QuotaStatus.WARNING
    assert state.usable is True


def test_quota_carries_the_reset_time_and_window():
    state = _quota_from_event(
        _Info(
            status="allowed_warning",
            utilization=0.91,
            resets_at=1234,
            rate_limit_type="five_hour",
        )
    )
    assert (state.utilization, state.resets_at, state.window) == (0.91, 1234, "five_hour")


class _Result:
    def __init__(self, status=None, errors=None, result=""):
        self.api_error_status = status
        self.errors = errors
        self.result = result


def test_a_429_is_read_as_exhausted_even_with_no_quota_event():
    """A run can fail on a 429 without a rate-limit transition being emitted.

    Being wrong costs one unnecessary fallback; missing it costs a trading cycle.
    """
    assert _quota_from_error(_Result(status=429)).status is QuotaStatus.EXHAUSTED


def test_quota_wording_in_an_error_is_recognised():
    assert _quota_from_error(_Result(result="usage limit reached")) is not None
    assert _quota_from_error(_Result(errors=["rate limit exceeded"])) is not None
    assert _quota_from_error(_Result(result="something else entirely")) is None


def test_auth_failure_is_distinguished_from_a_quota_wall():
    """The fixes differ: authenticate, versus wait or pay."""
    assert _looks_like_auth_failure("Not logged in · Please run /login")
    assert _looks_like_auth_failure("API Error: 401 API key is invalid.")
    assert not _looks_like_auth_failure("rate limit exceeded, resets at 5pm")


# ── AUTO fallback policy ──────────────────────────────────────────


async def test_auto_falls_back_to_api_only_on_an_auth_failure(monkeypatch):
    attempts: list[BillingMode] = []

    async def fake_run_once(self, request, billing):
        attempts.append(billing)
        if billing is BillingMode.SUBSCRIPTION:
            return AgentResult(
                ok=False, error="Not logged in", auth_failed=True, billing_used=billing
            )
        return AgentResult(ok=True, text="done", billing_used=billing)

    monkeypatch.setattr(ClaudeCodeBackend, "_run_once", fake_run_once)
    result = await ClaudeCodeBackend().run(
        AgentRequest(system_prompt="s", prompt="p", billing=BillingMode.AUTO)
    )
    assert attempts == [BillingMode.SUBSCRIPTION, BillingMode.API]
    assert result.ok and result.billing_used is BillingMode.API


async def test_auto_does_not_spend_money_to_escape_a_quota_wall(monkeypatch):
    """Whether to pay for capacity is the caller's policy decision, not this layer's."""
    attempts: list[BillingMode] = []

    async def fake_run_once(self, request, billing):
        attempts.append(billing)
        return AgentResult(
            ok=False,
            quota=QuotaState(QuotaStatus.EXHAUSTED),
            auth_failed=False,
            billing_used=billing,
        )

    monkeypatch.setattr(ClaudeCodeBackend, "_run_once", fake_run_once)
    result = await ClaudeCodeBackend().run(
        AgentRequest(system_prompt="s", prompt="p", billing=BillingMode.AUTO)
    )
    assert attempts == [BillingMode.SUBSCRIPTION], "a quota wall must not silently bill the API"
    assert result.quota_exhausted


async def test_explicit_subscription_never_retries_on_the_api(monkeypatch):
    attempts: list[BillingMode] = []

    async def fake_run_once(self, request, billing):
        attempts.append(billing)
        return AgentResult(ok=False, auth_failed=True, billing_used=billing)

    monkeypatch.setattr(ClaudeCodeBackend, "_run_once", fake_run_once)
    await ClaudeCodeBackend().run(
        AgentRequest(system_prompt="s", prompt="p", billing=BillingMode.SUBSCRIPTION)
    )
    assert attempts == [BillingMode.SUBSCRIPTION]


# ── Tool bridging ─────────────────────────────────────────────────


async def test_a_failing_tool_reports_to_the_agent_instead_of_killing_the_run():
    """An exception would lose every turn of reasoning already done."""
    from evotrader.agents.cli.claude_code import _to_sdk_tool

    async def explode(**_):
        raise RuntimeError("upstream is down")

    sdk_tool = _to_sdk_tool(
        LocalTool(
            name="lookup",
            description="d",
            input_schema={"type": "object", "properties": {}},
            handler=explode,
        )
    )
    out = await sdk_tool.handler({})
    assert out["isError"] is True
    assert "upstream is down" in out["content"][0]["text"]


async def test_tool_results_keep_their_structure_as_text():
    from evotrader.agents.cli.claude_code import _to_sdk_tool

    async def handler(ticker: str):
        return {"ticker": ticker, "atr": 8.47}

    sdk_tool = _to_sdk_tool(
        LocalTool(
            name="atr",
            description="d",
            input_schema={"type": "object", "properties": {"ticker": {"type": "string"}}},
            handler=handler,
        )
    )
    out = await sdk_tool.handler({"ticker": "MSTR"})
    assert '"ticker": "MSTR"' in out["content"][0]["text"]
    assert "8.47" in out["content"][0]["text"]


# ── Registry ──────────────────────────────────────────────────────


def test_the_shipped_driver_is_registered():
    assert "claude_code" in registered_drivers()
    assert get_backend_class("claude_code") is ClaudeCodeBackend


def test_unknown_driver_names_the_real_alternatives():
    backend, reason = load_backend("gemini_cli")
    assert backend is None
    assert "claude_code" in reason


def test_a_driver_name_cannot_be_silently_reused():
    """Two adapters sharing a driver would make settings ambiguous."""

    class Clashing(CliAgentBackend):
        driver = "claude_code"

        @classmethod
        def is_available(cls):
            return True, ""

        async def run(self, request):
            return AgentResult(ok=True)

    with pytest.raises(ValueError, match="already registered"):
        register_backend(Clashing)


def test_a_driver_must_declare_a_name():
    class Nameless(CliAgentBackend):
        driver = ""

        @classmethod
        def is_available(cls):
            return True, ""

        async def run(self, request):
            return AgentResult(ok=True)

    with pytest.raises(ValueError, match="non-empty"):
        register_backend(Nameless)


def test_blocked_tool_list_is_not_configurable():
    """Documented invariant: config cannot widen this."""
    assert set(_ALWAYS_BLOCKED) >= {"Write", "Edit", "Bash", "WebFetch", "WebSearch"}


# ── The evolution path must carry the same guard ──────────────────


def _config_with_evolution_on_its_own_cli_runtime():
    """Settings with evolution hosted on a CLI runtime block of its own.

    That is how a subscription user runs it: the strategy agent keeps the
    ``claude_code`` block and evolution gets a separate one, so its model can be
    raised without moving the agent that runs every cycle.
    """
    from evotrader.config import AppConfig
    from evotrader.models.config import CliRuntimeConfig

    config = AppConfig()
    config.settings.cli_runtimes["claude_code_evolution"] = CliRuntimeConfig(driver="claude_code")
    config.settings.agent_runtime["evolution"] = "claude_code_evolution"
    return config


def _evolution_service(billing: str, tmp_path):
    import logging as _logging

    _logging.disable(_logging.WARNING)
    try:
        from evotrader.evolution.claude_code_service import ClaudeCodeEvolutionService

        config = _config_with_evolution_on_its_own_cli_runtime()
        # Resolve the runtime the way the service does. Hardcoding
        # cli_runtimes["claude_code"] silently stopped testing evolution
        # when it moved to its own Fable block on 2026-09-18.
        _, _rt = config.settings.runtime_for("evolution")
        _rt.billing = billing
        return ClaudeCodeEvolutionService(thought_logger=None, config=config, instructions="rules")
    finally:
        _logging.disable(_logging.NOTSET)


def test_evolution_options_withhold_the_api_key_too(monkeypatch, tmp_path):
    """The evolution service builds its own options and had no guard.

    The adapter is not the only place that constructs a hosted run, so the
    enforcement has to reach here as well — this is the exact path that billed
    ~$2.88 a run to the API while reporting subscription billing.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-looks-real")
    options = _evolution_service("subscription", tmp_path)._build_options(None)
    assert options.env.get("ANTHROPIC_API_KEY") == ""


def test_evolution_api_billing_passes_the_key_through(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-looks-real")
    options = _evolution_service("api", tmp_path)._build_options(None)
    assert "ANTHROPIC_API_KEY" not in options.env


def test_evolution_honours_configured_thinking_and_effort(tmp_path):
    import logging as _logging

    _logging.disable(_logging.WARNING)
    try:
        from evotrader.evolution.claude_code_service import ClaudeCodeEvolutionService

        config = _config_with_evolution_on_its_own_cli_runtime()
        # Resolve the runtime the way the service does. Hardcoding
        # cli_runtimes["claude_code"] silently stopped testing evolution
        # when it moved to its own Fable block on 2026-09-18.
        _, runtime = config.settings.runtime_for("evolution")
        runtime.effort = "xhigh"
        runtime.thinking = "adaptive"
        svc = ClaudeCodeEvolutionService(thought_logger=None, config=config, instructions="rules")
        options = svc._build_options(None)
        assert options.effort == "xhigh"
        assert options.thinking == {"type": "adaptive"}

        runtime.thinking = "default"
        # `default` must send nothing, so the harness applies its own.
        assert (
            ClaudeCodeEvolutionService(thought_logger=None, config=config, instructions="rules")
            ._build_options(None)
            .thinking
            is None
        )
    finally:
        _logging.disable(_logging.NOTSET)


def test_subscription_cost_is_labelled_notional_not_spend():
    """A plan-billed run still reports what it would have cost on the API."""
    from evotrader.evolution.claude_code_service import _format_usage

    subscription = _format_usage({"estimated_cost_usd": 2.88, "billing": "subscription"})
    assert "notional_cost=$2.8800" in subscription
    assert "billing=subscription" in subscription

    api = _format_usage({"estimated_cost_usd": 2.88, "billing": "api"})
    assert "est_cost=$2.8800" in api
    assert "billing=api" in api
