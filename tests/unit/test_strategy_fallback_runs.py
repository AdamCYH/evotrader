"""The API fallback must see its own steps, answer, and stop when it cannot.

When the subscription plan was spent, the hosted strategy agent handed the
cycle to its API fallback, an ``LlmAgent``, from inside the orchestrator's
``strategy`` tool call. An ``LlmAgent`` builds each model request from the
session's events, and it is the Runner that adds an agent's events to the
session, before the agent's next step. Inside a tool call nothing did. Every
request the fallback made was its first one again, every model call had the
same input, and it called its first tools over and over, billed per token,
until the app was stopped or the cycle cancelled, and none of it reached the
thought log, so the cycle read as though no fallback had run.

These tests run the real path: a Runner, an orchestrator that calls the
``strategy`` tool, the hosted agent, and a real ``LlmAgent`` fallback whose
model is scripted. The model answers only once it can see its tool's result,
exactly as a real model would.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, ClassVar

import pytest
from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm, LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from evotrader.agents.cli import AgentResult, QuotaState, QuotaStatus
from evotrader.agents.cli_agent import CliBackedAgent, SessionSharingAgentTool, hosted_agents
from evotrader.models.config import CliRuntimeConfig

LIMIT_NOTICE = "You've hit your session limit · resets 9:05am"


def count_shares(ticker: str) -> dict:
    """How many shares of *ticker* are held."""
    return {"ticker": ticker, "shares": 20}


def _has_result(request: Any, name: str) -> bool:
    return any(
        getattr(p, "function_response", None) is not None and p.function_response.name == name
        for c in request.contents or []
        for p in c.parts or []
    )


def _call(name: str, **args: Any) -> LlmResponse:
    return LlmResponse(
        content=types.Content(
            role="model", parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))]
        )
    )


def _text(text: str) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


class _StrategyModel(BaseLlm):
    """Calls count_shares until it can see the result, then answers."""

    model: str = "scripted-strategy"
    answers: bool = True
    seen: ClassVar[list[int]] = []

    async def generate_content_async(self, llm_request, stream: bool = False):
        type(self).seen.append(len(llm_request.contents or []))
        if self.answers and _has_result(llm_request, "count_shares"):
            yield _text("HOLD 20 XYZ: no order.")
        else:
            yield _call("count_shares", ticker="XYZ")


class _OrchestratorModel(BaseLlm):
    """Calls the strategy tool once, then summarises."""

    model: str = "scripted-orchestrator"
    tool_results: ClassVar[list[Any]] = []

    async def generate_content_async(self, llm_request, stream: bool = False):
        for content in llm_request.contents or []:
            for part in content.parts or []:
                if part.function_response is not None and part.function_response.name == "strategy":
                    type(self).tool_results.append(part.function_response.response)
                    yield _text("Cycle done.")
                    return
        yield _call("strategy", request="Decide for XYZ.")


class _Backend:
    def __init__(self, result: AgentResult, delay: float = 0.0) -> None:
        self.result = result
        self.delay = delay
        self.calls = 0

    async def run(self, request):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result


class _Log:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, dict | None]] = []

    async def record_event(self, session_id, agent_name, event_type, content, meta=None):
        self.rows.append((event_type, content, meta))


_EXHAUSTED = AgentResult(
    ok=False,
    error=LIMIT_NOTICE,
    quota=QuotaState(QuotaStatus.EXHAUSTED, resets_at=time.time() + 3600, window="five_hour"),
)


def _hosted(
    backend: _Backend,
    *,
    answers: bool = True,
    on_exhausted: str = "api",
    log: _Log | None = None,
    **limits: Any,
) -> CliBackedAgent:
    runtime = CliRuntimeConfig.model_construct(
        **{**CliRuntimeConfig().model_dump(), "model": "opus", **limits}
    )
    fallback = LlmAgent(
        name="strategy",
        model=_StrategyModel(answers=answers),
        instruction="Decide.",
        tools=[count_shares],
        mode="single_turn",
    )
    return CliBackedAgent(
        name="strategy",
        description="Strategy.",
        backend=backend,
        runtime=runtime,
        instruction_text="Decide.",
        fallback_agent=fallback,
        on_exhausted=on_exhausted,
        thought_logger=log,
    )


async def _cycle(agent: CliBackedAgent) -> tuple[Any, str]:
    """One orchestrator turn through the real Runner; the tool's answer and session id."""
    _StrategyModel.seen.clear()
    _OrchestratorModel.tool_results.clear()
    orchestrator = LlmAgent(
        name="orchestrator",
        model=_OrchestratorModel(),
        instruction="Run the cycle.",
        tools=[SessionSharingAgentTool(agent, skip_summarization=True)],
    )
    service = InMemorySessionService()
    runner = Runner(app_name="t", agent=orchestrator, session_service=service)
    session = await service.create_session(app_name="t", user_id="u")
    message = types.Content(role="user", parts=[types.Part(text="Start.")])
    async for _ in runner.run_async(user_id="u", session_id=session.id, new_message=message):
        pass
    stored = await service.get_session(app_name="t", user_id="u", session_id=session.id)
    return stored, _OrchestratorModel.tool_results[-1]["result"]


async def test_the_fallback_sees_its_tool_result_and_answers() -> None:
    log = _Log()
    agent = _hosted(_Backend(_EXHAUSTED), log=log)

    session, answer = await _cycle(agent)

    assert answer == "HOLD 20 XYZ: no order.", "the tool returns the answer, not the steps"
    assert len(_StrategyModel.seen) == 2, "one tool round, then the answer"
    assert _StrategyModel.seen[1] > _StrategyModel.seen[0], (
        "the second request holds the first step"
    )
    assert agent.outcome(session.id) == {
        "session_id": session.id,
        "decided": True,
        "via": "api_fallback",
        "model_calls": 2,
    }


async def test_the_orchestrators_session_does_not_take_the_fallbacks_steps() -> None:
    session, _ = await _cycle(_hosted(_Backend(_EXHAUSTED)))

    authors = [e.author for e in session.events]
    assert "strategy" not in authors, "the fallback's steps stay in its private copy"


async def test_the_fallback_is_in_the_thought_log() -> None:
    log = _Log()
    await _cycle(_hosted(_Backend(_EXHAUSTED), log=log))

    runtime = [c for t, c, _ in log.rows if t == "runtime"]
    assert any(
        "subscription quota exhausted on the five_hour window" in c
        and "the API fallback decides this cycle, billed per token" in c
        for c in runtime
    ), runtime
    assert (
        "tool_call",
        "count_shares",
        {"args": {"ticker": "XYZ"}, "runtime": "api_fallback"},
    ) in log.rows
    assert ("thought", "HOLD 20 XYZ: no order.", {"runtime": "api_fallback"}) in log.rows


async def test_a_fallback_that_never_answers_is_stopped() -> None:
    log = _Log()
    agent = _hosted(_Backend(_EXHAUSTED), answers=False, log=log, fallback_max_model_calls=3)

    session, answer = await _cycle(agent)

    assert len(_StrategyModel.seen) == 3
    assert "could not run" in answer and "not a decision to stay flat" in answer
    outcome = agent.outcome(session.id)
    assert outcome["decided"] is False
    assert "reached 3 model calls without an answer" in outcome["reason"]
    assert any(t == "runtime" and "no decision this cycle" in c for t, c, _ in log.rows)


async def test_a_hung_harness_is_stopped_at_the_stage_limit() -> None:
    backend = _Backend(AgentResult(ok=True, text="late"), delay=5.0)
    agent = _hosted(backend, max_run_seconds=0.2)

    started = time.monotonic()
    session, answer = await _cycle(agent)

    assert time.monotonic() - started < 3.0
    assert _StrategyModel.seen == [], "no time was left for the fallback"
    assert "could not run" in answer
    assert "the stage's 0-minute limit" in agent.outcome(session.id)["reason"]


async def test_skip_spends_nothing_on_a_spent_plan() -> None:
    backend = _Backend(_EXHAUSTED)
    agent = _hosted(backend, on_exhausted="skip")

    session, answer = await _cycle(agent)
    assert _StrategyModel.seen == [], "no API call"
    assert "could not run: subscription quota exhausted on the five_hour window" in answer
    assert agent.outcome(session.id)["on_exhausted"] == "skip"
    assert agent.out_of_quota().startswith("quota exhausted on the five_hour window, until ")

    await _cycle(agent)
    assert backend.calls == 1, "while the window is spent the harness is not asked again"
    assert _StrategyModel.seen == []


async def test_a_spent_plan_still_falls_back_on_api() -> None:
    backend = _Backend(_EXHAUSTED)
    agent = _hosted(backend)
    await _cycle(agent)
    await _cycle(agent)
    assert backend.calls == 1
    assert len(_StrategyModel.seen) == 2, "the second cycle went straight to the fallback"


def test_the_orchestrator_names_its_hosted_agents() -> None:
    agent = _hosted(_Backend(_EXHAUSTED))
    orchestrator = LlmAgent(
        name="orchestrator",
        model=_OrchestratorModel(),
        tools=[SessionSharingAgentTool(agent, skip_summarization=True), count_shares],
    )
    assert hosted_agents(orchestrator) == {"strategy": agent}


def test_the_factory_passes_the_quota_policy(monkeypatch) -> None:
    from evotrader.agents.factory import create_strategy_agent
    from evotrader.config import AppConfig

    monkeypatch.setattr(
        "evotrader.agents.cli.claude_code.ClaudeCodeBackend.is_available",
        classmethod(lambda cls: (True, "ok")),
    )
    cfg = AppConfig()
    cfg.settings.cli_runtimes["claude_code"] = CliRuntimeConfig(quota={"on_exhausted": "skip"})
    cfg.settings.agent_runtime["strategy"] = "claude_code"
    agent = create_strategy_agent(cfg)
    assert agent.on_exhausted == "skip"
    assert agent.fallback_agent is not None, "it still decides when the harness itself fails"


@pytest.mark.parametrize("is_error", [False, True])
async def test_the_harness_limit_notice_is_not_an_answer(monkeypatch, is_error) -> None:
    import claude_agent_sdk as sdk

    from evotrader.agents.cli import AgentRequest, BillingMode
    from evotrader.agents.cli.claude_code import ClaudeCodeBackend

    info = sdk.types.RateLimitInfo(
        status="rejected", resets_at=int(time.time()) + 3600, rate_limit_type="five_hour"
    )

    async def fake_query(prompt, options):
        yield sdk.RateLimitEvent(rate_limit_info=info, uuid="u1", session_id="s1")
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text=LIMIT_NOTICE)], model="opus")
        yield sdk.ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=0,
            is_error=is_error,
            num_turns=1,
            session_id="s1",
            result=LIMIT_NOTICE,
        )

    monkeypatch.setattr(sdk, "query", fake_query)
    result = await ClaudeCodeBackend()._run_once(
        AgentRequest(system_prompt="s", prompt="p"), BillingMode.SUBSCRIPTION
    )
    assert result.ok is False
    assert result.quota.status is QuotaStatus.EXHAUSTED
