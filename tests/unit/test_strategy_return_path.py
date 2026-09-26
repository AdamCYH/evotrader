"""Control must come BACK from the strategy agent.

See: data/evolution/reviews/20260916_222823_cli_strategy_transfer_terminates_pipeline.md

ADK reaches an ``LlmAgent`` sub-agent through a function call that RETURNS. It
reaches a custom ``BaseAgent`` sub-agent — which ``CliBackedAgent`` is — through
``transfer_to_agent``, which is a ONE-WAY handoff: ``base_llm_flow.py`` runs the
target, yields its events, and the parent generator ends. The orchestrator never
gets another turn, so ``risk_manager`` and ``execution`` become structurally
unreachable.

Measured: sixteen consecutive cycles across 2026-09-15/16 with zero orders
placed. The last agent-placed order dates from 2026-09-14 18:33Z — the day
before the CLI migration. Three cycles on 09-16 carried a fully specified
``CLOSE MSTR LONG``; the 19:30 one ends with the literal sentence
"Routing to risk_manager for validation, then execution." None arrived.

Every pre-existing CLI test exercises the INPUT side — prompt assembly, tool
schemas. None exercised the return path. That is the fifth recorded instance of
the same class of miss: a guard that does not run the path the real system runs.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from evotrader.config import AppConfig


@pytest.fixture(autouse=True)
def _claude_code_installed(monkeypatch) -> None:
    """These tests are about how a hosted agent is wired, not whether the CLI is here.

    Without the `claude` CLI (as on CI) the factory falls back to the API agent,
    and the wiring under test never happens.
    """
    monkeypatch.setattr(
        "evotrader.agents.cli.claude_code.ClaudeCodeBackend.is_available",
        classmethod(lambda cls: (True, "ok")),
    )


def _config(strategy_runtime: str) -> AppConfig:
    cfg = AppConfig()
    cfg.settings.agent_runtime["strategy"] = strategy_runtime
    return cfg


def _names(items: list[Any]) -> list[str]:
    return [getattr(i, "name", getattr(i, "__name__", str(i))) for i in items]


class TestTheOrchestratorKeepsItsTurn:
    """The structural property, asserted directly.

    A full end-to-end cycle needs a live LLM, so this pins the wiring that
    decides whether control can return at all — which is the thing that broke.
    """

    def test_cli_hosted_strategy_is_a_tool_not_a_sub_agent(self) -> None:
        from evotrader.agents.factory import create_orchestrator_agent

        orch = create_orchestrator_agent(_config("claude_code"))

        assert "strategy" in _names(orch.tools), (
            "strategy must be reachable as a TOOL so control returns to the "
            "orchestrator and risk_manager/execution stay reachable"
        )
        assert "strategy" not in _names(orch.sub_agents), (
            "a custom BaseAgent in sub_agents is reached via transfer_to_agent, "
            "which is a one-way handoff that terminates the cycle"
        )

    def test_the_rest_of_the_pipeline_is_still_reachable(self) -> None:
        from evotrader.agents.factory import create_orchestrator_agent

        orch = create_orchestrator_agent(_config("claude_code"))
        reachable = set(_names(orch.tools)) | set(_names(orch.sub_agents))

        for stage in ("risk_manager", "execution", "news_sentiment"):
            assert stage in reachable, f"{stage} became unreachable"

    def test_api_runtime_keeps_the_sub_agent_path(self) -> None:
        """An ordinary LlmAgent already returns control — do not change it."""
        from evotrader.agents.factory import create_orchestrator_agent

        orch = create_orchestrator_agent(_config("api"))
        assert "strategy" in _names(orch.sub_agents)

    def test_the_hosted_agent_carries_a_description(self) -> None:
        """AgentTool builds its function declaration from the description; an
        empty one leaves the orchestrator guessing when to call it."""
        from evotrader.agents.factory import create_strategy_agent

        agent = create_strategy_agent(_config("claude_code"))
        assert getattr(agent, "description", ""), "hosted agent needs a description"


class TestSessionSharingPreservesTheBriefing:
    """Stock ``AgentTool`` would fix the return path and silently re-break the
    input path, by running the agent against a fresh ``InMemorySessionService``
    whose only event is the request string."""

    async def test_the_agent_sees_the_parent_session_events(self) -> None:
        from evotrader.agents.cli_agent import SessionSharingAgentTool

        seen: dict[str, Any] = {}

        class _Agent:
            name = "strategy"
            description = "test agent"

            async def run_async(self, ctx):
                seen["events"] = list(getattr(ctx.session, "events", []))
                seen["agent"] = ctx.agent
                yield types.SimpleNamespace(
                    actions=None,
                    content=None,
                    _text="PROPOSAL: CLOSE MSTR 3 shares",
                )

        agent = _Agent()
        tool = SessionSharingAgentTool(agent, skip_summarization=True)

        parent_events = ["market-snapshot", "news-report"]
        parent_ctx = types.SimpleNamespace(
            session=types.SimpleNamespace(events=parent_events),
            agent=None,
            model_copy=lambda update: types.SimpleNamespace(
                session=types.SimpleNamespace(events=parent_events),
                agent=update["agent"],
            ),
        )
        tool_context = types.SimpleNamespace(
            _invocation_context=parent_ctx,
            state=types.SimpleNamespace(update=lambda d: None),
        )

        import evotrader.agents.cli_agent as mod

        original = mod._event_text
        mod._event_text = lambda e: getattr(e, "_text", "")
        try:
            result = await tool.run_async(args={}, tool_context=tool_context)
        finally:
            mod._event_text = original

        assert seen["events"] == parent_events, (
            "the hosted agent must see the REAL cycle history — it rebuilds its "
            "whole market briefing from ctx.session.events"
        )
        assert seen["agent"] is agent
        assert "CLOSE MSTR" in result

    async def test_empty_output_is_an_error_not_a_no_trade(self) -> None:
        """Silence and 'no trade' must never be indistinguishable."""
        from evotrader.agents.cli_agent import SessionSharingAgentTool

        class _Silent:
            name = "strategy"
            description = "test agent"

            async def run_async(self, ctx):
                return
                yield  # pragma: no cover

        tool = SessionSharingAgentTool(_Silent(), skip_summarization=True)
        parent_ctx = types.SimpleNamespace(
            session=types.SimpleNamespace(events=[]),
            agent=None,
            model_copy=lambda update: types.SimpleNamespace(
                session=types.SimpleNamespace(events=[]), agent=update["agent"]
            ),
        )
        tool_context = types.SimpleNamespace(
            _invocation_context=parent_ctx,
            state=types.SimpleNamespace(update=lambda d: None),
        )

        result = await tool.run_async(args={}, tool_context=tool_context)
        assert "INFRASTRUCTURE FAULT" in result
        assert "not a decision to stay flat" in result


class TestTheRuleIsGeneral:
    """The fix must hold for agents that do not exist yet.

    The original defect was one agent quietly changing type (LlmAgent ->
    CliBackedAgent) while keeping its old `sub_agents` registration. A
    name-specific fix would leave the next such change to be caught by whoever
    happens to remember — which is how this cost two trading days the first time.
    """

    def test_no_non_llm_agent_may_sit_in_sub_agents(self) -> None:
        """The invariant, asserted structurally rather than per-agent."""
        from google.adk.agents import LlmAgent

        from evotrader.agents.factory import create_orchestrator_agent

        for runtime in ("api", "claude_code"):
            orch = create_orchestrator_agent(_config(runtime))
            offenders = [a.name for a in orch.sub_agents if not isinstance(a, LlmAgent)]
            assert not offenders, (
                f"[{runtime}] {offenders} are reached via transfer_to_agent, "
                "which is a one-way handoff that ends the cycle"
            )

    def test_a_hypothetical_new_hosted_agent_is_wired_as_a_tool(self, monkeypatch) -> None:
        """Simulate someone hosting a DIFFERENT stage tomorrow.

        Nothing in the wiring should need to learn its name.
        """
        import evotrader.agents.factory as fac

        real = fac.create_risk_manager_agent

        def _hosted(config):
            # Renamed deliberately: a stand-in still called "strategy" would be
            # caught by a name-specific check and this test would pass for the
            # wrong reason. The whole question is whether a DIFFERENT stage,
            # hosted tomorrow, is routed correctly without anyone editing the
            # wiring.
            hosted = fac.create_strategy_agent(_config("claude_code"))
            return hosted.model_copy(update={"name": "risk_manager"})

        monkeypatch.setattr(fac, "create_risk_manager_agent", _hosted)
        try:
            orch = fac.create_orchestrator_agent(_config("api"))
        finally:
            monkeypatch.setattr(fac, "create_risk_manager_agent", real)

        from google.adk.agents import LlmAgent

        assert all(isinstance(a, LlmAgent) for a in orch.sub_agents), (
            "a newly hosted stage was registered as a sub_agent without anyone "
            "updating the wiring — the exact 2026-09-16 failure"
        )

    def test_pipeline_order_is_preserved(self) -> None:
        """Routing by type must not reorder the stages."""
        from evotrader.agents.factory import create_orchestrator_agent

        orch = create_orchestrator_agent(_config("api"))
        names = _names(orch.sub_agents)
        assert names == ["news_sentiment", "strategy", "risk_manager", "execution"]


class TestIgnoredRuntimeSettingsAreLoud:
    """A CLI runtime configured for an agent whose factory ignores it is a
    silent no-op — it reads like it works in settings.yaml and does nothing."""

    def test_unsupported_agent_logs_an_error(self, caplog) -> None:
        import logging

        from evotrader.models.config import Settings

        with caplog.at_level(logging.ERROR):
            Settings.model_validate(
                {
                    "agent_runtime": {"execution": "claude_code"},
                    "cli_runtimes": {"claude_code": {"driver": "claude_code"}},
                }
            )
        assert "IGNORED" in caplog.text
        assert "execution" in caplog.text

    def test_supported_agent_is_silent(self, caplog) -> None:
        import logging

        from evotrader.models.config import Settings

        with caplog.at_level(logging.ERROR):
            Settings.model_validate(
                {
                    "agent_runtime": {"strategy": "claude_code"},
                    "cli_runtimes": {"claude_code": {"driver": "claude_code"}},
                }
            )
        assert "IGNORED" not in caplog.text

    def test_api_runtime_is_never_flagged(self, caplog) -> None:
        import logging

        from evotrader.models.config import Settings

        with caplog.at_level(logging.ERROR):
            Settings.model_validate({"agent_runtime": {"execution": "api"}})
        assert "IGNORED" not in caplog.text

    def test_the_capable_set_matches_what_the_code_actually_reads(self) -> None:
        """The set must not drift into a promise the factories do not keep."""
        import inspect

        import evotrader.agents.factory as fac
        import evotrader.main as main_mod
        from evotrader.models.config import CLI_CAPABLE_AGENTS

        callers = inspect.getsource(fac) + inspect.getsource(main_mod)
        for agent in CLI_CAPABLE_AGENTS:
            assert f'runtime_for("{agent}")' in callers, (
                f"{agent} is advertised as CLI-capable but nothing calls runtime_for({agent!r})"
            )
