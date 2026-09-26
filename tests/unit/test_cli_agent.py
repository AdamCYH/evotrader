"""The hosted strategy agent must be indistinguishable to the rest of the pipeline.

The orchestrator delegates to it as it would to an ``LlmAgent``; the risk manager
must receive the same proposal text. What changes is billing and who owns the
agent loop — and, critically, what happens when the plan quota runs out.

Falling back is a first-class path here, not an error path. A cycle that produces
no strategy output is a cycle that cannot trade, so every failure mode below must
still end in a decision.
"""

from __future__ import annotations

import time
import types
from typing import Any

import pytest
from google.genai import types as genai_types

from evotrader.agents.cli import AgentResult, QuotaState, QuotaStatus
from evotrader.agents.cli_agent import CliBackedAgent

# ── Fakes ─────────────────────────────────────────────────────────


def _event(author: str, text: str) -> Any:
    return types.SimpleNamespace(
        author=author,
        content=genai_types.Content(role="model", parts=[genai_types.Part(text=text)]),
    )


def _ctx(events: list[Any] | None = None) -> Any:
    return types.SimpleNamespace(
        session=types.SimpleNamespace(events=events or []),
        user_content=None,
        invocation_id="inv-1",
        branch=None,
    )


class _Backend:
    def __init__(self, result: AgentResult) -> None:
        self.result = result
        self.requests: list[Any] = []

    async def run(self, request):
        self.requests.append(request)
        return self.result


class _FallbackAgent:
    def __init__(self) -> None:
        self.calls = 0

    async def run_async(self, ctx):
        self.calls += 1
        yield _event("strategy", "fallback decision")


def _runtime(**kw: Any) -> Any:
    from evotrader.models.config import CliRuntimeConfig

    return CliRuntimeConfig(**{"model": "opus", "billing": "subscription", **kw})


def _agent(backend: Any, fallback: Any = None, **kw: Any) -> CliBackedAgent:
    return CliBackedAgent(
        name="strategy",
        backend=backend,
        runtime=_runtime(**kw.pop("runtime", {})),
        instruction_text="You are the Strategy Agent.",
        fallback_agent=fallback,
        **kw,
    )


async def _collect(agent: CliBackedAgent, ctx: Any) -> list[Any]:
    return [e async for e in agent._run_async_impl(ctx)]


# ── Success ───────────────────────────────────────────────────────


async def test_successful_run_yields_the_proposal_as_a_model_event():
    backend = _Backend(AgentResult(ok=True, text="BUY 3 MSTR", turns=4))
    events = await _collect(_agent(backend), _ctx())
    assert len(events) == 1
    assert events[0].author == "strategy"
    assert events[0].content.parts[0].text == "BUY 3 MSTR"


async def test_runtime_settings_reach_the_backend():
    backend = _Backend(AgentResult(ok=True, text="hold"))
    agent = _agent(backend, runtime={"effort": "high", "thinking": "adaptive", "max_turns": 12})
    await _collect(agent, _ctx())
    request = backend.requests[0]
    assert request.model == "opus"
    assert request.effort == "high"
    assert request.thinking.mode == "adaptive"
    assert request.max_turns == 12
    assert request.billing.value == "subscription"


async def test_default_thinking_is_passed_through_as_default():
    backend = _Backend(AgentResult(ok=True, text="hold"))
    await _collect(_agent(backend, runtime={"thinking": "default"}), _ctx())
    assert backend.requests[0].thinking.mode == "default"


# ── Prompt assembly ───────────────────────────────────────────────


async def test_the_orchestrator_request_is_passed_to_the_hosted_agent():
    """A hosted harness starts cold — without this it would decide blind."""
    backend = _Backend(AgentResult(ok=True, text="ok"))
    ctx = _ctx(
        [
            _event("orchestrator", "MSTR at 137.14, ATR 8.47, regime trending_bull"),
            _event("news_sentiment", "Sentiment mildly positive"),
        ]
    )
    await _collect(_agent(backend), ctx)
    prompt = backend.requests[0].prompt
    assert "137.14" in prompt
    assert "Sentiment mildly positive" in prompt
    # Order preserved: oldest first, so the narrative reads forwards.
    assert prompt.index("137.14") < prompt.index("Sentiment")


async def test_the_agents_own_previous_output_is_not_fed_back():
    backend = _Backend(AgentResult(ok=True, text="ok"))
    ctx = _ctx([_event("strategy", "my earlier answer"), _event("orchestrator", "new data")])
    await _collect(_agent(backend), ctx)
    assert "my earlier answer" not in backend.requests[0].prompt


async def test_system_prompt_carries_instructions_and_dynamic_context():
    backend = _Backend(AgentResult(ok=True, text="ok"))
    agent = _agent(backend)
    agent.dynamic_instruction = lambda ctx: "It is 10:30 ET, 5 cycles remain."
    await _collect(agent, _ctx())
    system = backend.requests[0].system_prompt
    assert "Strategy Agent" in system
    assert "5 cycles remain" in system


async def test_an_empty_session_still_produces_a_usable_prompt():
    backend = _Backend(AgentResult(ok=True, text="ok"))
    await _collect(_agent(backend), _ctx())
    assert backend.requests[0].prompt.strip()


# ── Failure and fallback ──────────────────────────────────────────


async def test_a_failed_run_falls_back_so_the_cycle_still_decides():
    backend = _Backend(AgentResult(ok=False, error="harness crashed"))
    fallback = _FallbackAgent()
    events = await _collect(_agent(backend, fallback), _ctx())
    assert fallback.calls == 1
    assert events[0].content.parts[0].text == "fallback decision"


async def test_a_failure_with_no_fallback_reports_rather_than_yielding_nothing():
    """Silence would look like 'no trade' instead of 'no decision was made'."""
    backend = _Backend(AgentResult(ok=False, error="harness crashed"))
    events = await _collect(_agent(backend, None), _ctx())
    assert len(events) == 1
    assert "harness crashed" in (events[0].error_message or "")


async def test_an_ok_run_with_empty_text_is_treated_as_a_failure():
    backend = _Backend(AgentResult(ok=True, text=""))
    fallback = _FallbackAgent()
    await _collect(_agent(backend, fallback), _ctx())
    assert fallback.calls == 1, "an empty proposal is not a decision"


# ── Quota policy ──────────────────────────────────────────────────


async def test_quota_warning_routes_the_following_cycle_away_not_this_one():
    """The current decision must finish where it started."""
    backend = _Backend(
        AgentResult(
            ok=True,
            text="BUY 3 MSTR",
            quota=QuotaState(QuotaStatus.WARNING, utilization=0.92, resets_at=time.time() + 600),
        )
    )
    fallback = _FallbackAgent()
    agent = _agent(backend, fallback)

    first = await _collect(agent, _ctx())
    assert first[0].content.parts[0].text == "BUY 3 MSTR"
    assert fallback.calls == 0, "this cycle must complete on the harness"

    second = await _collect(agent, _ctx())
    assert fallback.calls == 1, "the next cycle must use the fallback"
    assert second[0].content.parts[0].text == "fallback decision"
    assert len(backend.requests) == 1, "the harness must not be called again"


async def test_backoff_expires_so_capacity_is_reused():
    """A stale backoff would keep paying for the API long after quota returned."""
    backend = _Backend(
        AgentResult(
            ok=True,
            text="ok",
            quota=QuotaState(QuotaStatus.WARNING, resets_at=time.time() - 1),
        )
    )
    fallback = _FallbackAgent()
    agent = _agent(backend, fallback)
    await _collect(agent, _ctx())
    await _collect(agent, _ctx())
    assert fallback.calls == 0
    assert len(backend.requests) == 2


async def test_utilisation_below_the_threshold_does_not_trigger_backoff():
    backend = _Backend(
        AgentResult(ok=True, text="ok", quota=QuotaState(QuotaStatus.OK, utilization=0.5))
    )
    fallback = _FallbackAgent()
    agent = _agent(backend, fallback)
    await _collect(agent, _ctx())
    await _collect(agent, _ctx())
    assert fallback.calls == 0


async def test_utilisation_at_the_threshold_triggers_backoff_even_when_status_is_ok():
    """The wall is what we are avoiding; 'allowed' at 90% is still nearly out."""
    backend = _Backend(
        AgentResult(ok=True, text="ok", quota=QuotaState(QuotaStatus.OK, utilization=0.9))
    )
    fallback = _FallbackAgent()
    agent = _agent(backend, fallback, runtime={"quota": {"warn_utilization": 0.85}})
    await _collect(agent, _ctx())
    await _collect(agent, _ctx())
    assert fallback.calls == 1


async def test_exhaustion_with_no_reset_time_backs_off_for_one_cycle_only():
    """Without a reset time, retry next cycle rather than stalling indefinitely."""
    backend = _Backend(AgentResult(ok=True, text="ok", quota=QuotaState(QuotaStatus.EXHAUSTED)))
    agent = _agent(backend, _FallbackAgent())
    await _collect(agent, _ctx())
    assert 0 < agent._backoff_until - time.time() <= 3600


async def test_a_warning_with_no_utilisation_figure_is_not_acted_on():
    """Nothing to compare against the threshold, and the run itself succeeded."""
    backend = _Backend(AgentResult(ok=True, text="ok", quota=QuotaState(QuotaStatus.WARNING)))
    fallback = _FallbackAgent()
    agent = _agent(backend, fallback)
    await _collect(agent, _ctx())
    await _collect(agent, _ctx())
    assert fallback.calls == 0


# ── Console timeline ──────────────────────────────────────────────


async def test_harness_progress_reaches_the_thought_log():
    """The web console timeline must look the same on either runtime."""
    from evotrader.agents.cli import AgentEvent

    logged: list[tuple[str, str]] = []

    class _Logger:
        async def record_event(self, session_id, agent_name, event_type, content, meta=None):
            logged.append((event_type, content))

    backend = _Backend(AgentResult(ok=True, text="ok"))
    agent = _agent(backend, thought_logger=_Logger())
    await _collect(agent, _ctx())

    sink = backend.requests[0].on_event
    assert sink is not None

    # The run itself emits one note recording what the prompt actually
    # contained (review 20260915_224744, finding 3). Since review
    # 20260918_210213 that is typed 'runtime', not 'thought': it is a harness
    # diagnostic, and as a 'thought' it could surface in get_cycle_digest as an
    # agent's final reasoning. Drop it here and assert the harness-progress
    # contract on what follows.
    assert logged and logged[0][0] == "runtime"
    assert logged[0][1].startswith("[runtime] prompt:")
    logged.clear()

    await sink(AgentEvent(kind="thought", content="weighing the signal"))
    await sink(AgentEvent(kind="tool_call", name="compute_risk_budget"))
    assert logged == [("thought", "weighing the signal"), ("tool_call", "compute_risk_budget")]


async def test_a_broken_thought_logger_cannot_break_a_trading_cycle():
    from evotrader.agents.cli import AgentEvent

    class _Broken:
        async def record_event(self, **kwargs):
            raise RuntimeError("db gone")

    backend = _Backend(AgentResult(ok=True, text="ok"))
    agent = _agent(backend, thought_logger=_Broken())
    await _collect(agent, _ctx())
    await backend.requests[0].on_event(AgentEvent(kind="thought", content="x"))


# ── Runtime parity ────────────────────────────────────────────────


def test_both_runtimes_offer_the_strategy_agent_the_same_tools():
    """A decision must not depend on which runtime happened to serve it."""
    import logging as _logging

    _logging.disable(_logging.WARNING)
    try:
        from evotrader.agents.factory import (
            _STRATEGY_TOOLS,
            _create_api_strategy_agent,
            create_strategy_agent,
        )
        from evotrader.config import AppConfig

        config = AppConfig()
        api_names = {
            getattr(t, "__name__", getattr(t, "name", ""))
            for t in _create_api_strategy_agent(config).tools
        }

        config.settings.agent_runtime["strategy"] = "claude_code"
        hosted = create_strategy_agent(config)
        if type(hosted).__name__ != "CliBackedAgent":
            pytest.skip("CLI runtime unavailable on this machine")
        hosted_names = {t.name for t in hosted.local_tools}

        assert hosted_names == api_names == {fn.__name__ for fn in _STRATEGY_TOOLS}
    finally:
        _logging.disable(_logging.NOTSET)


def test_the_hosted_strategy_agent_keeps_an_api_fallback():
    """Without one, a quota wall means no trading decision at all."""
    import logging as _logging

    _logging.disable(_logging.WARNING)
    try:
        from evotrader.agents.factory import create_strategy_agent
        from evotrader.config import AppConfig

        config = AppConfig()
        config.settings.agent_runtime["strategy"] = "claude_code"
        agent = create_strategy_agent(config)
        if type(agent).__name__ != "CliBackedAgent":
            pytest.skip("CLI runtime unavailable on this machine")
        assert agent.fallback_agent is not None
        assert type(agent.fallback_agent).__name__ == "LlmAgent"
    finally:
        _logging.disable(_logging.NOTSET)


def test_strategy_agent_has_no_tool_that_can_reach_the_broker():
    """Why hosting THIS agent is safe: it proposes, it cannot place.

    If a broker-touching tool is ever added here, hosting it externally stops
    being a low-risk change and this test should fail loudly.
    """
    from evotrader.agents.factory import _STRATEGY_TOOLS

    forbidden = ("place_", "cancel_", "review_", "exercise_", "order")
    offenders = [
        fn.__name__
        for fn in _STRATEGY_TOOLS
        if any(bad in fn.__name__.lower() for bad in forbidden)
    ]
    assert not offenders, (
        f"strategy agent gained broker-capable tool(s) {offenders}; it is hosted "
        f"on an external harness precisely because it could not place orders"
    )


# ── Tool results must reach the hosted agent ──────────────────────
#
# The first live cycle came back `partial` because they did not: the agent was
# handed the orchestrator's prose but none of the tool output, and correctly
# refused to size a position against data it had never seen. These pin the fix.


def _tool_event(author: str, name: str, payload: Any) -> Any:
    return types.SimpleNamespace(
        author=author,
        content=genai_types.Content(
            role="user",
            parts=[
                genai_types.Part(
                    function_response=genai_types.FunctionResponse(name=name, response=payload)
                )
            ],
        ),
    )


async def test_market_data_tool_results_reach_the_hosted_agent():
    backend = _Backend(AgentResult(ok=True, text="ok"))
    ctx = _ctx(
        [
            _tool_event(
                "orchestrator",
                "gather_market_data",
                {"ticker": "MSTR", "last": 130.615, "indicators": {"atr_14": 8.477}},
            ),
            _event("orchestrator", "Decide the trade for cycle 4."),
        ]
    )
    await _collect(_agent(backend), ctx)
    prompt = backend.requests[0].prompt
    assert "gather_market_data" in prompt
    assert "130.615" in prompt
    assert "8.477" in prompt
    assert "Decide the trade" in prompt


async def test_a_huge_tool_result_cannot_crowd_out_the_market_snapshot():
    """The option chain is fetched after the snapshot and is far larger.

    A newest-first walk with only a global budget would spend it all on the chain
    and drop the quote the position is sized against.
    """
    backend = _Backend(AgentResult(ok=True, text="ok"))
    ctx = _ctx(
        [
            _tool_event("orchestrator", "gather_market_data", {"last": 130.615}),
            _tool_event("orchestrator", "gather_option_chain", {"contracts": ["x" * 500_000]}),
            _event("orchestrator", "Decide."),
        ]
    )
    await _collect(_agent(backend), ctx)
    prompt = backend.requests[0].prompt
    assert "130.615" in prompt, "the market snapshot must survive a huge option chain"
    assert "truncated at" in prompt
    assert len(prompt) <= 200_000


async def test_tool_results_are_labelled_so_the_agent_knows_their_source():
    backend = _Backend(AgentResult(ok=True, text="ok"))
    ctx = _ctx([_tool_event("orchestrator", "gather_market_data", {"last": 1.0})])
    await _collect(_agent(backend), ctx)
    assert '<tool_result name="gather_market_data">' in backend.requests[0].prompt


async def test_mixed_text_and_tool_parts_are_both_kept():
    backend = _Backend(AgentResult(ok=True, text="ok"))
    event = types.SimpleNamespace(
        author="orchestrator",
        content=genai_types.Content(
            role="user",
            parts=[
                genai_types.Part(text="Here is the snapshot:"),
                genai_types.Part(
                    function_response=genai_types.FunctionResponse(
                        name="gather_market_data", response={"last": 130.615}
                    )
                ),
            ],
        ),
    )
    await _collect(_agent(backend), _ctx([event]))
    prompt = backend.requests[0].prompt
    assert "Here is the snapshot:" in prompt
    assert "130.615" in prompt


# ── Backoff must follow OUR threshold, not the harness's advisory ──
#
# Observed on the first sim run: the harness reported `allowed_warning` on the
# seven_day window at 76% used, and the agent diverted to the metered API even
# though the configured threshold was 85%. A weekly window at 76% is a healthy
# plan with days of headroom; treating it as a wall would have put every
# remaining cycle on the paid path.


async def test_a_harness_warning_below_our_threshold_does_not_divert_traffic():
    backend = _Backend(
        AgentResult(
            ok=True,
            text="ok",
            quota=QuotaState(QuotaStatus.WARNING, utilization=0.76, window="seven_day"),
        )
    )
    fallback = _FallbackAgent()
    agent = _agent(backend, fallback, runtime={"quota": {"warn_utilization": 0.85}})
    await _collect(agent, _ctx())
    await _collect(agent, _ctx())
    assert fallback.calls == 0, "76% of a weekly window is not a reason to pay per token"
    assert len(backend.requests) == 2


async def test_a_harness_warning_at_our_threshold_does_divert():
    backend = _Backend(
        AgentResult(
            ok=True,
            text="ok",
            quota=QuotaState(QuotaStatus.WARNING, utilization=0.86, window="five_hour"),
        )
    )
    fallback = _FallbackAgent()
    agent = _agent(backend, fallback, runtime={"quota": {"warn_utilization": 0.85}})
    await _collect(agent, _ctx())
    await _collect(agent, _ctx())
    assert fallback.calls == 1


async def test_exhaustion_diverts_regardless_of_reported_utilisation():
    backend = _Backend(AgentResult(ok=True, text="ok", quota=QuotaState(QuotaStatus.EXHAUSTED)))
    fallback = _FallbackAgent()
    agent = _agent(backend, fallback, runtime={"quota": {"warn_utilization": 0.85}})
    await _collect(agent, _ctx())
    await _collect(agent, _ctx())
    assert fallback.calls == 1


async def test_a_distant_reset_is_rechecked_within_a_day():
    """A seven_day reset must not route a week of trading onto the paid API unseen."""
    backend = _Backend(
        AgentResult(
            ok=True,
            text="ok",
            quota=QuotaState(
                QuotaStatus.EXHAUSTED,
                resets_at=time.time() + 7 * 86_400,
                window="seven_day",
            ),
        )
    )
    agent = _agent(backend, _FallbackAgent())
    await _collect(agent, _ctx())
    assert agent._backoff_until - time.time() <= 86_400 + 5


async def test_near_limit_backoff_is_one_cycle_so_capacity_is_retried():
    backend = _Backend(
        AgentResult(ok=True, text="ok", quota=QuotaState(QuotaStatus.WARNING, utilization=0.99))
    )
    agent = _agent(backend, _FallbackAgent())
    await _collect(agent, _ctx())
    assert 0 < agent._backoff_until - time.time() <= 3600
