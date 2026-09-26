"""Regression tests: a cycle stopped by a provider 503 is resumed once, when that is safe.

2026-09-24 11:30 ET (session ca7cec51): market data gathered, news_sentiment
failed, then the orchestrator's own model call got ``503 UNAVAILABLE ... high
demand``. The SDK retry (~20 s) gave up and the cycle ended ``error`` with
strategy, risk_manager and execution skipped. No attribution row, and the
second-ever swing_failure_reversal firing on the instrument went unseen.

See: data/evolution/reviews/20260924_224217_mean_reversion_bull_stack_guard_unreachable_strategy_stage_503_skip.md
(finding 2)
"""

from __future__ import annotations

import ast
import inspect

import pytest
from google.genai import errors as genai_errors

from evotrader.agents.provider_retry import (
    is_retryable_provider_error,
    run_with_one_resume,
    short_error,
)


def _live_503() -> Exception:
    """The exact error the 11:30 cycle recorded."""
    return genai_errors.ServerError(
        503,
        {
            "error": {
                "code": 503,
                "message": "This model is currently experiencing high demand. Spikes in demand "
                "are usually temporary. Please try again later.",
                "status": "UNAVAILABLE",
            }
        },
    )


class _LitellmOverloadedError(Exception):
    """Stand-in for an Anthropic 529 surfaced through LiteLLM."""

    status_code = 529


_LitellmOverloadedError.__module__ = "litellm.exceptions"


class TestClassification:
    def test_the_live_503_is_retryable(self) -> None:
        assert is_retryable_provider_error(_live_503())

    def test_rate_limit_and_overload_are_retryable(self) -> None:
        assert is_retryable_provider_error(
            genai_errors.ClientError(
                429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}
            )
        )
        assert is_retryable_provider_error(_LitellmOverloadedError("overloaded"))

    def test_timeouts_and_dropped_connections_are_retryable(self) -> None:
        assert is_retryable_provider_error(TimeoutError())
        assert is_retryable_provider_error(ConnectionResetError())

    def test_a_wrapped_provider_error_is_found(self) -> None:
        try:
            try:
                raise _live_503()
            except Exception as inner:
                raise RuntimeError("runner failed") from inner
        except RuntimeError as outer:
            assert is_retryable_provider_error(outer)

    @pytest.mark.parametrize(
        "exc",
        [
            genai_errors.ClientError(
                400, {"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}}
            ),
            genai_errors.ClientError(
                403, {"error": {"code": 403, "message": "no", "status": "PERMISSION_DENIED"}}
            ),
            RuntimeError("Systematic termination: Cycle exceeded the maximum allowed event limit"),
            ValueError("bug"),
            KeyError("x"),
        ],
    )
    def test_everything_else_is_not(self, exc: Exception) -> None:
        assert not is_retryable_provider_error(exc)

    def test_a_numeric_code_outside_a_provider_sdk_is_not_a_status(self) -> None:
        class UnrelatedError(Exception):
            code = 503

        assert not is_retryable_provider_error(UnrelatedError())

    def test_short_error_is_one_line(self) -> None:
        text = short_error(_live_503())
        assert "\n" not in text and text.startswith("503 UNAVAILABLE") and len(text) <= 160


class _Runner:
    """Plays scripted attempts: each is a list of events, optionally ending in an error."""

    def __init__(self, *attempts: tuple[list[str], Exception | None]) -> None:
        self.attempts, self.messages = list(attempts), []

    def start(self, message):
        self.messages.append(message)
        events, error = self.attempts.pop(0)

        async def gen():
            for e in events:
                yield e
            if error is not None:
                raise error

        return gen()


async def _collect(runner: _Runner, *, blocked=lambda: None, delay: float = 90.0):
    sleeps, resumes, events = [], [], []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    async def on_resume(exc: BaseException, d: float) -> None:
        resumes.append((type(exc).__name__, d))

    async for e in run_with_one_resume(
        runner.start,
        "START",
        delay_seconds=delay,
        blocked_reason=blocked,
        resume_message=lambda exc: "RESUME",
        on_resume=on_resume,
        sleep=sleep,
    ):
        events.append(e)
    return events, sleeps, resumes


class TestResume:
    async def test_the_0924_cycle_would_have_continued(self) -> None:
        """Gather + news answered, then the orchestrator's call 503'd: resume once
        in the same session and finish the pipeline."""
        runner = _Runner(
            (["gather_market_data", "news_sentiment"], _live_503()),
            (["strategy", "summary"], None),
        )
        events, sleeps, resumes = await _collect(runner)
        assert events == ["gather_market_data", "news_sentiment", "strategy", "summary"]
        assert runner.messages == ["START", "RESUME"]
        assert sleeps == [90.0] and resumes == [("ServerError", 90.0)]

    async def test_only_once(self) -> None:
        runner = _Runner(([], _live_503()), ([], _live_503()))
        with pytest.raises(genai_errors.ServerError):
            await _collect(runner)
        assert runner.messages == ["START", "RESUME"]

    async def test_not_when_blocked(self) -> None:
        runner = _Runner((["execution"], _live_503()))
        with pytest.raises(genai_errors.ServerError):
            await _collect(runner, blocked=lambda: "the order path had started")
        assert runner.messages == ["START"]

    async def test_not_for_a_non_provider_error(self) -> None:
        runner = _Runner(([], ValueError("bug")))
        with pytest.raises(ValueError):
            await _collect(runner)
        assert runner.messages == ["START"]

    async def test_zero_delay_disables_it(self) -> None:
        runner = _Runner(([], _live_503()))
        with pytest.raises(genai_errors.ServerError):
            await _collect(runner, delay=0)

    async def test_a_clean_cycle_is_untouched(self) -> None:
        runner = _Runner((["a", "b"], None))
        events, sleeps, _ = await _collect(runner)
        assert events == ["a", "b"] and sleeps == [] and runner.messages == ["START"]


class TestCycleRunnerWiring:
    """The safety conditions live in main.py, next to the event stream."""

    def _src(self) -> str:
        from evotrader import main

        return inspect.getsource(main)

    def test_the_cycle_runs_through_the_resume_wrapper(self) -> None:
        tree = ast.parse(self._src())
        called = {
            getattr(n.func, "id", getattr(n.func, "attr", ""))
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
        }
        assert "run_with_one_resume" in called

    def test_resume_is_blocked_once_the_order_path_started(self) -> None:
        src = self._src()
        block = src[src.index("def _resume_blocked_reason") : src.index("def _resume_prompt")]
        assert "_ORDER_PATH_STAGES & stages_seen" in block
        assert "open_calls" in block and "last_author" in block

    def test_order_path_stages_are_risk_and_execution(self) -> None:
        from evotrader.main import _ORDER_PATH_STAGES

        assert {"risk_manager", "execution"} == _ORDER_PATH_STAGES

    def test_delay_is_a_setting_with_a_sane_default(self) -> None:
        from evotrader.models.config import ScheduleConfig

        field = ScheduleConfig.model_fields["provider_retry_delay_seconds"]
        assert field.default == 90
        with pytest.raises(ValueError):
            ScheduleConfig(provider_retry_delay_seconds=900)


class TestRealAdkSession:
    """The resume relies on ADK keeping a session usable after a model error
    raised out of ``Runner.run_async``. Checked against the real Runner and
    session service with a scripted model, so a framework upgrade that changes
    this fails here rather than in a live cycle."""

    async def test_same_session_continues_with_history_intact(self) -> None:
        from google.adk.agents import LlmAgent
        from google.adk.models.base_llm import BaseLlm
        from google.adk.models.llm_response import LlmResponse
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types

        requests: list[list[tuple[str, list[str]]]] = []
        ran: list[str] = []

        def _names(content: types.Content) -> list[str]:
            out = []
            for p in content.parts or []:
                if p.function_call:
                    out.append(f"call:{p.function_call.name}")
                elif p.function_response:
                    out.append(f"response:{p.function_response.name}")
                else:
                    out.append(p.text or "")
            return out

        class Scripted(BaseLlm):
            model: str = "scripted"
            step: int = 0

            async def generate_content_async(self, llm_request, stream: bool = False):
                self.step += 1
                requests.append([(c.role, _names(c)) for c in llm_request.contents])
                if self.step == 1:
                    yield LlmResponse(
                        content=types.Content(
                            role="model",
                            parts=[
                                types.Part(
                                    function_call=types.FunctionCall(
                                        name="gather_market_data", args={"ticker": "MSTR"}
                                    )
                                )
                            ],
                        )
                    )
                elif self.step == 2:
                    raise _live_503()
                elif self.step == 3:
                    yield LlmResponse(
                        content=types.Content(
                            role="model",
                            parts=[
                                types.Part(
                                    function_call=types.FunctionCall(
                                        name="strategy", args={"request": "decide"}
                                    )
                                )
                            ],
                        )
                    )
                else:
                    yield LlmResponse(
                        content=types.Content(role="model", parts=[types.Part(text="HOLD")])
                    )

        def gather_market_data(ticker: str) -> dict:
            """Gather."""
            ran.append("gather_market_data")
            return {"composite_signal": 0.205}

        def strategy(request: str) -> str:
            """Decide."""
            ran.append("strategy")
            return "HOLD"

        svc = InMemorySessionService()
        runner = Runner(
            app_name="t",
            session_service=svc,
            agent=LlmAgent(
                name="orchestrator",
                model=Scripted(),
                instruction="x",
                tools=[gather_market_data, strategy],
            ),
        )
        session = await svc.create_session(app_name="t", user_id="u")

        async def no_sleep(_s: float) -> None:
            return None

        authors = []
        async for event in run_with_one_resume(
            lambda m: runner.run_async(user_id="u", session_id=session.id, new_message=m),
            types.Content(role="user", parts=[types.Part(text="Start a trading cycle.")]),
            delay_seconds=90,
            blocked_reason=lambda: None,
            sleep=no_sleep,
            resume_message=lambda exc: types.Content(
                role="user", parts=[types.Part(text="RESUME")]
            ),
        ):
            authors.append(event.author)

        assert ran == ["gather_market_data", "strategy"], "nothing re-run, nothing skipped"
        assert requests[2] == [
            ("user", ["Start a trading cycle."]),
            ("model", ["call:gather_market_data"]),
            ("user", ["response:gather_market_data"]),
            ("user", ["RESUME"]),
        ], "the resumed request carries the whole, well-formed conversation"
        assert set(authors) == {"orchestrator"}
