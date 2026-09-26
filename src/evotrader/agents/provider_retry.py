"""Resume a trading cycle once when the model provider was briefly unavailable.

2026-09-24 11:30 ET (session ca7cec51): the market data was gathered, then the
orchestrator's next model call got ``503 UNAVAILABLE ... high demand``. The
SDK's own retry (4 attempts, ~20 s) gave up, the exception left the runner, and
the whole cycle ended ``error`` with strategy, risk and execution skipped. The
next chance was an hour away, and the cycle held swing_failure_reversal's second
firing on this instrument — which no agent ever saw.

Demand spikes last minutes, not seconds. So a cycle that stops this way is
resumed ONCE, after a pause, in the SAME session: the gathered data, the news
report and anything else already done stay in the conversation, so the
orchestrator carries on from where it stopped instead of starting over, and the
snapshot the cycle is scored on is the one already stored.

A resume is refused unless it is safe to repeat nothing:

* the error is a transient provider fault (5xx, 429, a timeout, a dropped
  connection) — never a policy block, a bad request or a bug;
* no order could have been placed: neither risk_manager nor execution ran;
* the conversation is well formed: every tool the orchestrator called has
  answered, so the resumed request is valid and no tool is left half-run.

These are decided by the caller, which is the one watching the event stream.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

logger = logging.getLogger(__name__)

#: HTTP statuses a provider returns for "not now": timeout, rate limit,
#: server-side failure, overload (Anthropic's 529).
RETRYABLE_HTTP_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})

#: Exceptions are only judged by status when a provider SDK or HTTP client
#: raised them — an unrelated exception with a numeric ``code`` is not a
#: provider fault.
_PROVIDER_MODULES = (
    "google.genai",
    "google.api_core",
    "litellm",
    "anthropic",
    "openai",
    "httpx",
    "httpcore",
    "aiohttp",
)
_TRANSIENT_NAME_MARKERS = (
    "timeout",
    "unavailable",
    "ratelimit",
    "overloaded",
    "connect",
    "internalserver",
)


def is_retryable_provider_error(exc: BaseException) -> bool:
    """True for a transient fault on the model provider's side.

    Walks the ``__cause__``/``__context__`` chain, since SDKs and ADK wrap the
    original error.
    """
    seen: set[int] = set()
    err: BaseException | None = exc
    while err is not None and id(err) not in seen:
        seen.add(id(err))
        if isinstance(err, (TimeoutError, ConnectionError)):
            return True
        if (type(err).__module__ or "").startswith(_PROVIDER_MODULES):
            for attr in ("code", "status_code"):
                status = getattr(err, attr, None)
                if isinstance(status, int) and status in RETRYABLE_HTTP_STATUS:
                    return True
            name = type(err).__name__.lower()
            if any(marker in name for marker in _TRANSIENT_NAME_MARKERS):
                return True
        err = err.__cause__ or err.__context__
    return False


def short_error(exc: BaseException, limit: int = 160) -> str:
    """One line of an error, for a log row or a prompt."""
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def run_with_one_resume(
    start: Callable[[Any], AsyncIterator[Any]],
    first_message: Any,
    *,
    delay_seconds: float,
    blocked_reason: Callable[[], str | None],
    resume_message: Callable[[BaseException], Any],
    on_resume: Callable[[BaseException, float], Awaitable[None]] | None = None,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> AsyncIterator[Any]:
    """Yield ``start(first_message)``'s events; on a retryable provider error,
    wait ``delay_seconds`` and continue with ``start(resume_message(exc))`` once.

    ``blocked_reason()`` is asked at the moment of failure and returns why a
    resume would be unsafe, or None. Any other error, a second failure, a
    blocked resume or ``delay_seconds <= 0`` re-raises the original exception.
    """
    message = first_message
    resumed = False
    while True:
        try:
            async for event in start(message):
                yield event
            return
        except Exception as exc:
            if resumed or delay_seconds <= 0 or not is_retryable_provider_error(exc):
                raise
            reason = blocked_reason()
            if reason:
                logger.warning(
                    "Provider error, not resuming the cycle: %s (%s)", reason, short_error(exc)
                )
                raise
            resumed = True
            logger.warning(
                "Provider error (%s); resuming this cycle once in %.0f s.",
                short_error(exc),
                delay_seconds,
            )
            if on_resume is not None:
                await on_resume(exc, delay_seconds)
            await sleep(delay_seconds)
            message = resume_message(exc)
