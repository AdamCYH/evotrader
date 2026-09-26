"""Tests for the Claude Code evolution backend.

The point of this backend is that it changes *where the model runs*, not what
gets recorded — the web console, the proposals on disk and the DB rows must be
identical to the ADK backend. Most of these tests therefore assert on what
lands in the thought log and the SSE stream.

None of them require ``claude-agent-sdk`` (an optional dependency): the message
translation, schema generation and option building are all exercised with fakes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from evotrader.evolution.claude_code_service import (
    ClaudeCodeEvolutionService,
    _build_options_safely,
    _format_usage,
    _parse_tool_result,
    _strip_mcp_prefix,
    is_available,
)
from evotrader.evolution.claude_code_tools import (
    SERVER_NAME,
    _split_docstring,
    allowed_tool_patterns,
    build_input_schema,
    evolution_tool_functions,
)

# ── Fakes mirroring the SDK's message/block shapes ────────────────
# Class *names* are what the service dispatches on, so these stand in for the
# real SDK types without importing them.


class TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class ThinkingBlock:
    def __init__(self, thinking: str) -> None:
        self.thinking = thinking


class ToolUseBlock:
    def __init__(self, name: str, input: dict, id: str) -> None:
        self.name = name
        self.input = input
        self.id = id


class ToolResultBlock:
    def __init__(self, content: Any, tool_use_id: str, is_error: bool = False) -> None:
        self.content = content
        self.tool_use_id = tool_use_id
        self.is_error = is_error


class AssistantMessage:
    def __init__(self, content: list, session_id: str | None = None) -> None:
        self.content = content
        self.session_id = session_id


class UserMessage:
    def __init__(self, content: list, session_id: str | None = None) -> None:
        self.content = content
        self.session_id = session_id


class ResultMessage:
    def __init__(self, usage: dict, total_cost_usd: float | None = None) -> None:
        self.usage = usage
        self.total_cost_usd = total_cost_usd
        self.content = None


class FakeThoughtLogger:
    """Captures exactly what the real ThoughtLogger would persist."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.runs: list[tuple[str, str]] = []
        self.completions: list[dict] = []

    async def record_event(
        self,
        session_id: str,
        agent_name: str,
        event_type: str,
        content: str,
        meta: dict | None = None,
    ) -> int:
        self.events.append(
            {
                "session_id": session_id,
                "agent_name": agent_name,
                "event_type": event_type,
                "content": content,
                "meta": meta,
            }
        )
        return len(self.events)

    async def record_run_start(self, session_id: str, cycle_type: str) -> None:
        self.runs.append((session_id, cycle_type))

    async def record_run_completion(self, session_id: str, status: str, **kw: Any) -> None:
        self.completions.append({"session_id": session_id, "status": status, **kw})

    async def get_session_events(self, session_id: str) -> list[dict]:
        return [e for e in self.events if e["session_id"] == session_id]


@dataclass
class FakeSchedule:
    max_evolution_duration_seconds: int = 1800


@dataclass
class FakeQuota:
    warn_utilization: float = 0.85
    on_exhausted: str = "api"


@dataclass
class FakeCliRuntime:
    """Mirrors CliRuntimeConfig — the generic runtime shape, not a vendor one."""

    driver: str = "claude_code"
    billing: str = "subscription"
    model: str = "opus"
    thinking: str = "default"
    effort: str | None = None
    max_turns: int = 80
    permission_mode: str = "dontAsk"
    allow_file_tools: bool = True
    quota: FakeQuota = field(default_factory=FakeQuota)


@dataclass
class FakeEvolution:
    # Deprecated shape, retained so the migration path stays covered.
    claude_code: FakeCliRuntime | None = None


@dataclass
class FakeSettings:
    schedule: FakeSchedule = field(default_factory=FakeSchedule)
    evolution: FakeEvolution = field(default_factory=FakeEvolution)
    cli_runtimes: dict = field(default_factory=lambda: {"claude_code": FakeCliRuntime()})
    agent_runtime: dict = field(default_factory=lambda: {"evolution": "claude_code"})

    def runtime_for(self, agent: str):
        from evotrader.models.config import AgentRuntimeKind

        name = self.agent_runtime.get(agent, "api")
        if name == "api":
            return AgentRuntimeKind.API, None
        return AgentRuntimeKind.CLI, self.cli_runtimes.get(name)


class FakeConfig:
    def __init__(self, tmp_path: Any) -> None:
        self.settings = FakeSettings()
        self.data_dir = tmp_path
        self.project_root = tmp_path


@pytest.fixture
def service(tmp_path):
    logger = FakeThoughtLogger()
    svc = ClaudeCodeEvolutionService(
        thought_logger=logger,
        config=FakeConfig(tmp_path),
        instructions="You are the evolution agent.",
    )
    return svc, logger


# ── Message translation: what the web console will show ───────────


@pytest.mark.asyncio
async def test_text_block_is_recorded_as_a_thought(service):
    svc, log = service
    await svc._handle_message("s1", AssistantMessage([TextBlock("Reviewing recent cycles.")]), {})

    thoughts = [e for e in log.events if e["event_type"] == "thought"]
    assert any(e["content"] == "Reviewing recent cycles." for e in thoughts)
    assert all(e["agent_name"] in ("evolution", "system") for e in thoughts)


@pytest.mark.asyncio
async def test_empty_text_is_not_recorded(service):
    svc, log = service
    await svc._handle_message("s1", AssistantMessage([TextBlock("   \n ")]), {})

    assert [e for e in log.events if e["event_type"] == "thought"] == []


@pytest.mark.asyncio
async def test_tool_use_is_recorded_with_the_adk_event_shape(service):
    svc, log = service
    await svc._handle_message(
        "s1",
        AssistantMessage(
            [
                ToolUseBlock(
                    f"mcp__{SERVER_NAME}__analyse_performance",
                    {"lookback_days": 30},
                    "tu_1",
                )
            ]
        ),
        {},
    )

    call = next(e for e in log.events if e["event_type"] == "tool_call")
    # Name must be stripped so the console renders it like the ADK backend.
    assert call["content"] == "analyse_performance"
    assert call["meta"]["args"] == {"lookback_days": 30}
    assert call["meta"]["tool_call_id"] == "tu_1"


@pytest.mark.asyncio
async def test_tool_result_is_paired_with_its_call_name(service):
    """Claude Code's tool_result blocks carry only an id, not the tool name."""
    svc, log = service
    await svc._handle_message(
        "s1",
        AssistantMessage(
            [ToolUseBlock(f"mcp__{SERVER_NAME}__get_cycle_digest", {"limit": 5}, "tu_9")]
        ),
        {},
    )
    await svc._handle_message(
        "s1",
        UserMessage([ToolResultBlock([{"type": "text", "text": '{"cycles": 5}'}], "tu_9")]),
        {},
    )

    resp = next(e for e in log.events if e["event_type"] == "tool_response")
    assert resp["content"] == "get_cycle_digest"
    # JSON is unwrapped so meta.response matches the ADK backend's column shape.
    assert resp["meta"]["response"] == {"cycles": 5}
    assert resp["meta"]["tool_call_id"] == "tu_9"


@pytest.mark.asyncio
async def test_tool_error_is_flagged_in_meta(service):
    svc, log = service
    await svc._handle_message(
        "s1",
        AssistantMessage([ToolUseBlock("mcp__evotrader__promote_algorithm_version", {}, "t1")]),
        {},
    )
    await svc._handle_message(
        "s1",
        UserMessage([ToolResultBlock("boom", "t1", is_error=True)]),
        {},
    )

    resp = next(e for e in log.events if e["event_type"] == "tool_response")
    assert resp["meta"]["is_error"] is True
    assert resp["meta"]["response"] == "boom"


@pytest.mark.asyncio
async def test_unknown_tool_result_id_does_not_crash(service):
    svc, log = service
    await svc._handle_message("s1", UserMessage([ToolResultBlock("orphaned", "never_seen")]), {})

    resp = next(e for e in log.events if e["event_type"] == "tool_response")
    assert resp["content"] == "unknown"


@pytest.mark.asyncio
async def test_thinking_block_is_recorded(service):
    svc, log = service
    await svc._handle_message("s1", AssistantMessage([ThinkingBlock("weighing options")]), {})

    assert any(e["content"] == "weighing options" for e in log.events)


@pytest.mark.asyncio
async def test_claude_session_id_is_captured_once_for_resume(service):
    svc, log = service
    await svc._handle_message("s1", AssistantMessage([TextBlock("a")], session_id="claude-abc"), {})
    await svc._handle_message("s1", AssistantMessage([TextBlock("b")], session_id="claude-abc"), {})

    assert svc._claude_session_ids["s1"] == "claude-abc"
    marker = [e for e in log.events if e["agent_name"] == "system"]
    assert len(marker) == 1, "session marker should be recorded exactly once"
    assert marker[0]["meta"]["claude_session_id"] == "claude-abc"


@pytest.mark.asyncio
async def test_result_message_collects_usage(service):
    svc, _ = service
    usage_summary: dict = {}
    await svc._handle_message(
        "s1",
        ResultMessage(usage={"input_tokens": 1000, "output_tokens": 200}, total_cost_usd=0.42),
        usage_summary,
    )

    assert usage_summary["usage"]["input_tokens"] == 1000
    assert usage_summary["estimated_cost_usd"] == 0.42


# ── Resume support ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claude_session_id_recovers_from_the_thought_log(service):
    """A restarted process must still be able to continue a timed-out run."""
    svc, log = service
    await log.record_event(
        session_id="s-old",
        agent_name="system",
        event_type="thought",
        content="Claude Code session: claude-xyz",
        meta={"claude_session_id": "claude-xyz"},
    )

    assert await svc._recover_claude_session_id("s-old") == "claude-xyz"


@pytest.mark.asyncio
async def test_recover_handles_json_encoded_meta(service):
    svc, log = service
    log.events.append(
        {
            "session_id": "s-old",
            "agent_name": "system",
            "event_type": "thought",
            "content": "x",
            "meta": json.dumps({"claude_session_id": "claude-json"}),
        }
    )

    assert await svc._recover_claude_session_id("s-old") == "claude-json"


@pytest.mark.asyncio
async def test_continue_session_without_a_recorded_id_fails_clearly(service):
    svc, _ = service
    with pytest.raises(ValueError, match="cannot resume"):
        await svc.continue_session("s-unknown")


@pytest.mark.asyncio
async def test_trigger_refuses_concurrent_runs(service):
    svc, _ = service
    svc._is_running = True
    with pytest.raises(RuntimeError, match="already running"):
        await svc.trigger()


# ── Prompt construction ───────────────────────────────────────────


def test_prompt_includes_carry_forward_notes(tmp_path):
    notes = tmp_path / "evolution" / "notes"
    notes.mkdir(parents=True)
    (notes / "carry_forward.md").write_text("# Notes\n\n- item one\n")

    svc = ClaudeCodeEvolutionService(
        thought_logger=FakeThoughtLogger(),
        config=FakeConfig(tmp_path),
        instructions="rules",
    )
    prompt = svc._build_prompt("")

    assert "Carry-Forward Notes" in prompt
    assert "item one" in prompt


def test_prompt_includes_operator_comment(tmp_path):
    svc = ClaudeCodeEvolutionService(
        thought_logger=FakeThoughtLogger(),
        config=FakeConfig(tmp_path),
        instructions="rules",
    )
    prompt = svc._build_prompt("why is the VWAP weight so high?")

    assert "Operator Question" in prompt
    assert "why is the VWAP weight so high?" in prompt


# ── Safety: what Claude Code is allowed to do ─────────────────────


def test_write_bash_and_edit_are_never_allowed():
    patterns = allowed_tool_patterns(allow_file_tools=True)
    assert patterns == [f"mcp__{SERVER_NAME}__*", "Read", "Grep", "Glob"]
    for forbidden in ("Write", "Edit", "Bash"):
        assert forbidden not in patterns


def test_file_tools_can_be_withheld():
    assert allowed_tool_patterns(allow_file_tools=False) == [f"mcp__{SERVER_NAME}__*"]


def test_options_block_mutating_builtins(tmp_path, monkeypatch):
    """The option dict must deny Write/Edit/Bash regardless of file-tool access."""

    @dataclass
    class FakeOptions:
        system_prompt: str = ""
        mcp_servers: dict = field(default_factory=dict)
        allowed_tools: list = field(default_factory=list)
        tools: list = field(default_factory=list)
        disallowed_tools: list = field(default_factory=list)
        permission_mode: str = ""
        max_turns: int = 0
        cwd: str = ""
        setting_sources: list = field(default_factory=list)
        model: str = ""
        resume: str | None = None

    svc = ClaudeCodeEvolutionService(
        thought_logger=FakeThoughtLogger(),
        config=FakeConfig(tmp_path),
        instructions="rules",
    )
    svc._mcp_server = object()  # skip building the real MCP server

    import sys
    import types as pytypes

    fake_sdk = pytypes.ModuleType("claude_agent_sdk")
    fake_sdk.ClaudeAgentOptions = FakeOptions  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    opts = svc._build_options(resume=None)

    assert "Write" in opts.disallowed_tools
    assert "Edit" in opts.disallowed_tools
    assert "Bash" in opts.disallowed_tools
    assert opts.tools == ["Read", "Grep", "Glob"]
    assert opts.permission_mode == "dontAsk"
    # Must not inherit the host machine's CLAUDE.md, hooks or MCP servers.
    assert opts.setting_sources == []
    assert opts.system_prompt.startswith("rules")


def test_unknown_options_are_dropped_not_fatal(caplog):
    """The SDK moves faster than this project; don't break every run over it."""

    @dataclass
    class Narrow:
        system_prompt: str = ""

    result = _build_options_safely(Narrow, {"system_prompt": "hi", "some_future_option": True})

    assert result.system_prompt == "hi"
    assert "some_future_option" in caplog.text


# ── Helpers ───────────────────────────────────────────────────────


def test_strip_mcp_prefix():
    assert _strip_mcp_prefix("mcp__evotrader__analyse_performance") == "analyse_performance"
    assert _strip_mcp_prefix("Read") == "Read"


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, {}),
        ('{"a": 1}', {"a": 1}),
        ("not json", "not json"),
        ([{"type": "text", "text": '{"b": 2}'}], {"b": 2}),
        ([{"type": "text", "text": "plain"}], "plain"),
        ({"already": "dict"}, {"already": "dict"}),
    ],
)
def test_parse_tool_result(raw, expected):
    assert _parse_tool_result(raw) == expected


def test_format_usage():
    text = _format_usage(
        {"usage": {"input_tokens": 10, "output_tokens": 5}, "estimated_cost_usd": 1.5}
    )
    assert "input_tokens=10" in text
    assert "est_cost=$1.5000" in text
    assert _format_usage({}) == "usage not reported"


def test_is_available_reports_an_actionable_message():
    ok, message = is_available()
    if not ok:
        assert "claude-agent-sdk" in message or "claude" in message
        assert "install" in message.lower() or "PATH" in message


# ── Tool bridging ─────────────────────────────────────────────────


def test_every_evolution_tool_produces_a_valid_schema():
    functions = evolution_tool_functions()
    assert len(functions) >= 25

    for fn in functions:
        schema = build_input_schema(fn)
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        assert isinstance(schema["required"], list)
        for name, prop in schema["properties"].items():
            assert prop.get("default") is not None or "default" not in prop, (
                f"{fn.__name__}.{name} declares a null default"
            )


def test_required_params_match_the_signature():
    from evotrader.evolution.tools import propose_new_strategy

    schema = build_input_schema(propose_new_strategy)
    assert set(schema["required"]) == {
        "name",
        "description",
        "signal_logic",
        "indicators",
        "params",
        "integration",
    }


def test_optional_params_are_not_required_and_carry_defaults():
    from evotrader.evolution.tools import query_cycle_thoughts

    schema = build_input_schema(query_cycle_thoughts)
    assert schema["required"] == []
    assert schema["properties"]["limit"]["default"] == 50
    assert schema["properties"]["limit"]["type"] == "integer"


def test_container_types_are_mapped():
    from evotrader.evolution.tools import propose_deprecation, propose_new_strategy

    new_strategy = build_input_schema(propose_new_strategy)
    assert new_strategy["properties"]["indicators"] == {
        "type": "array",
        "items": {"type": "string"},
        "description": "List of technical indicators required by this strategy.",
    }

    deprecation = build_input_schema(propose_deprecation)
    weights = deprecation["properties"]["redistribute_weight_to"]
    assert weights["type"] == "object"
    assert weights["additionalProperties"] == {"type": "number"}


def test_optional_union_type_is_unwrapped():
    from evotrader.evolution.tools import list_proposals

    schema = build_input_schema(list_proposals)
    assert schema["properties"]["status"]["type"] == "string"
    assert schema["required"] == []
    assert "default" not in schema["properties"]["status"]


def test_docstring_is_split_into_summary_and_param_docs():
    from evotrader.evolution.tools import propose_parameter_change

    summary, params = _split_docstring(propose_parameter_change)
    assert summary
    assert "Args:" not in summary
    assert "version_name" in params
    assert params["version_name"]


def test_every_tool_gets_a_description():
    for fn in evolution_tool_functions():
        summary, _ = _split_docstring(fn)
        assert summary.strip(), f"{fn.__name__} has no usable description"


def _adk_evolution_tool_names() -> set[str]:
    """Read the `tools=[...]` list out of `create_evolution_agent` via AST."""
    import ast
    import inspect
    import textwrap

    from evotrader.agents import factory

    tree = ast.parse(textwrap.dedent(inspect.getsource(factory.create_evolution_agent)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "tools" and isinstance(kw.value, ast.List):
                return {element.id for element in kw.value.elts if isinstance(element, ast.Name)}
    raise AssertionError("could not find tools=[...] in create_evolution_agent")


def test_tool_set_matches_the_adk_evolution_agent():
    """Both backends must offer the same tools or they behave differently.

    Fails when a tool is added to one backend and not the other — the most
    likely way these two paths drift apart.
    """
    adk_tools = _adk_evolution_tool_names()
    bridged = {fn.__name__ for fn in evolution_tool_functions()}

    assert adk_tools, "AST parse found no ADK evolution tools — the test is broken"
    assert not (adk_tools - bridged), (
        f"available to ADK but not to Claude Code: {sorted(adk_tools - bridged)}"
    )
    assert not (bridged - adk_tools), (
        f"available to Claude Code but not to ADK: {sorted(bridged - adk_tools)}"
    )


# ── Interface parity with the ADK service ─────────────────────────


def test_backend_is_a_drop_in_replacement():
    """The web server talks to whichever backend is configured.

    It calls trigger / continue_session / is_running / get_last_run /
    mark_timeout and reads `.thought_logger`. If the two services drift, the
    console breaks on one of them only — so pin the shared surface here.
    """
    import inspect

    from evotrader.evolution.evolution_service import EvolutionService

    used_by_web_server = [
        "trigger",
        "continue_session",
        "is_running",
        "get_last_run",
        "mark_timeout",
    ]

    for name in used_by_web_server:
        adk_method = getattr(EvolutionService, name)
        cc_method = getattr(ClaudeCodeEvolutionService, name, None)
        assert cc_method is not None, f"Claude Code backend is missing {name}()"
        assert inspect.signature(adk_method) == inspect.signature(cc_method), (
            f"{name}() signature differs between backends:\n"
            f"  adk:         {inspect.signature(adk_method)}\n"
            f"  claude_code: {inspect.signature(cc_method)}"
        )


def test_backend_exposes_thought_logger(tmp_path):
    svc = ClaudeCodeEvolutionService(
        thought_logger=FakeThoughtLogger(),
        config=FakeConfig(tmp_path),
        instructions="rules",
    )
    assert svc.thought_logger is not None


def test_agent_runtime_selects_the_evolution_service(tmp_path, monkeypatch):
    """`agent_runtime.evolution` must actually change which service is built."""
    from evotrader.config import AppConfig
    from evotrader.main import _create_evolution_service

    log = FakeThoughtLogger()
    monkeypatch.setattr("evotrader.agents.factory.create_evolution_agent", lambda cfg: object())

    config = AppConfig()
    config.settings.agent_runtime["evolution"] = "api"
    svc = _create_evolution_service(config, session_service=object(), thought_logger=log)
    assert type(svc).__name__ == "EvolutionService"

    config = AppConfig()
    config.settings.agent_runtime["evolution"] = "claude_code"
    monkeypatch.setattr(
        "evotrader.agents.cli.claude_code.ClaudeCodeBackend.is_available",
        classmethod(lambda cls: (True, "ok")),
    )
    svc = _create_evolution_service(config, session_service=object(), thought_logger=log)
    assert type(svc).__name__ == "ClaudeCodeEvolutionService"


def test_unavailable_runtime_falls_back_to_the_api_runtime(monkeypatch, caplog):
    """A missing CLI must not leave the system unable to evolve at all."""
    import logging

    from evotrader.config import AppConfig
    from evotrader.main import _create_evolution_service

    config = AppConfig()
    config.settings.agent_runtime["evolution"] = "claude_code"
    monkeypatch.setattr(
        "evotrader.agents.cli.claude_code.ClaudeCodeBackend.is_available",
        classmethod(lambda cls: (False, "the 'claude' CLI is not on PATH.")),
    )
    monkeypatch.setattr("evotrader.agents.factory.create_evolution_agent", lambda cfg: object())

    with caplog.at_level(logging.WARNING):
        svc = _create_evolution_service(
            config, session_service=object(), thought_logger=FakeThoughtLogger()
        )

    assert type(svc).__name__ == "EvolutionService"
    # The warning must name the cost consequence, not just the failure: this
    # fallback silently switches from a subscription seat to per-token billing.
    assert "billed per token" in caplog.text.lower()


def test_legacy_evolution_backend_keys_still_load(caplog):
    """An older settings.yaml must migrate forward, loudly, not be ignored.

    A silently dropped setting is exactly how the billing defect went unnoticed:
    the file said one thing and the system did another with no complaint.
    """
    import logging

    from evotrader.models.config import Settings

    with caplog.at_level(logging.WARNING):
        settings = Settings.model_validate(
            {
                "evolution": {
                    "backend": "claude_code",
                    "claude_code": {"model": "opus", "max_turns": 42},
                }
            }
        )

    kind, runtime = settings.runtime_for("evolution")
    assert kind.value == "cli"
    assert runtime is not None
    assert runtime.model == "opus"
    assert runtime.max_turns == 42
    assert "deprecated" in caplog.text.lower()


def test_unlisted_agents_default_to_the_api_runtime():
    """A typo in an agent name must not silently move a real agent."""
    from evotrader.models.config import Settings

    settings = Settings.model_validate(
        {
            "cli_runtimes": {"claude_code": {"driver": "claude_code"}},
            "agent_runtime": {"stratgey": "claude_code"},  # deliberate typo
        }
    )
    assert settings.runtime_for("strategy")[0].value == "api"


def test_agent_runtime_rejects_a_runtime_that_does_not_exist():
    from evotrader.models.config import Settings

    with pytest.raises(ValueError, match="names no runtime"):
        Settings.model_validate({"agent_runtime": {"strategy": "gemini_cli"}})


class TestClaudeCliResolution:
    """The CLI is often installed where PATH cannot see it.

    nvm puts the binary under a specific Node version and exports PATH from
    shell startup, so a server launched from an IDE or supervisor misses it —
    and the backend silently falls back to per-token API billing.

    Exercised against the adapter module, which owns this logic for every agent
    running on a hosted harness. ``claude_code_service`` re-exports the same
    functions for its existing callers.
    """

    def test_prefers_path_when_available(self, monkeypatch, tmp_path) -> None:
        import shutil as _sh

        from evotrader.agents.cli import claude_code as svc

        fake = tmp_path / "claude"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        # resolve_claude_cli imports shutil inside the function, so patch the
        # module attribute itself.
        monkeypatch.setattr(_sh, "which", lambda _: str(fake))
        assert svc.resolve_claude_cli() == fake

    def test_finds_binary_outside_path_and_fixes_path(self, monkeypatch, tmp_path) -> None:
        import os
        import shutil as _sh

        from evotrader.agents.cli import claude_code as svc

        bin_dir = tmp_path / "node" / "bin"
        bin_dir.mkdir(parents=True)
        fake = bin_dir / "claude"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)

        monkeypatch.setattr(_sh, "which", lambda _: None)
        monkeypatch.setattr(svc, "_candidate_cli_paths", lambda: [fake])
        monkeypatch.setenv("PATH", "/usr/bin")

        assert svc.resolve_claude_cli() == fake
        # Must also repair PATH, or the SDK subprocess still fails.
        assert str(bin_dir) in os.environ["PATH"]

    def test_returns_none_when_genuinely_absent(self, monkeypatch, tmp_path) -> None:
        import shutil as _sh

        from evotrader.agents.cli import claude_code as svc

        monkeypatch.setattr(_sh, "which", lambda _: None)
        monkeypatch.setattr(svc, "_candidate_cli_paths", lambda: [tmp_path / "nope"])
        assert svc.resolve_claude_cli() is None

    def test_non_executable_file_is_not_accepted(self, monkeypatch, tmp_path) -> None:
        import shutil as _sh

        from evotrader.agents.cli import claude_code as svc

        fake = tmp_path / "claude"
        fake.write_text("not executable")
        fake.chmod(0o644)
        monkeypatch.setattr(_sh, "which", lambda _: None)
        monkeypatch.setattr(svc, "_candidate_cli_paths", lambda: [fake])
        assert svc.resolve_claude_cli() is None

    def test_is_available_message_names_the_resolved_path(self, monkeypatch, tmp_path) -> None:
        import shutil as _sh

        from evotrader.agents.cli import claude_code as svc

        fake = tmp_path / "claude"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        monkeypatch.setattr(_sh, "which", lambda _: None)
        monkeypatch.setattr(svc, "_candidate_cli_paths", lambda: [fake])
        ok, msg = svc.ClaudeCodeBackend.is_available()
        assert ok
        assert str(fake) in msg

    def test_candidate_list_covers_nvm_and_common_installs(self) -> None:
        from evotrader.agents.cli.claude_code import _candidate_cli_paths

        joined = " ".join(str(p) for p in _candidate_cli_paths())
        for expected in (".nvm", ".claude", ".local", "homebrew", "/usr/local"):
            assert expected in joined, f"{expected} not covered"
