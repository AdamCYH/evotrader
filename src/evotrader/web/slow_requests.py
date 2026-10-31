"""The console logs each request that was slow to answer, and what else was going on.

A page that feels slow can be slow for several reasons: the server took long
to work out the answer, the answer was large and took long to send, many
requests were waiting at once, or a trading cycle or evolution run was busy in
the same process. The browser sees only the wait. ``SlowRequestLog`` writes
one line for each request that took longer than a limit (half a second unless
``EVOTRADER_SLOW_REQUEST_SECONDS`` says otherwise; 0 turns it off):

    Slow console request: GET /api/trades?limit=500 200 took 2.31 s (first byte
    at 2.25 s, 240 KB; 3 other requests open; app running)

``first byte`` is when the server began its answer; the rest of ``took`` was
spent sending it. The event stream (``text/event-stream``) stays open for as
long as the page does, so it is never counted.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

ENV_VAR = "EVOTRADER_SLOW_REQUEST_SECONDS"
DEFAULT_SECONDS = 0.5


def limit_from_env() -> float:
    """The limit in seconds; 0 when the log is off."""
    raw = os.environ.get(ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number of seconds; using %s", ENV_VAR, raw, DEFAULT_SECONDS)
        return DEFAULT_SECONDS
    return max(value, 0.0)


def _size(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    if n >= 1_000:
        return f"{n / 1_000:.0f} KB"
    return f"{n} B"


class SlowRequestLog:
    """ASGI middleware: one log line per request slower than ``limit_seconds``."""

    def __init__(
        self,
        app: Any,
        *,
        limit_seconds: float,
        app_status: Callable[[], str] = lambda: "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.limit_seconds = limit_seconds
        self.app_status = app_status
        self.clock = clock
        self.open = 0

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http" or self.limit_seconds <= 0:
            await self.app(scope, receive, send)
            return

        start = self.clock()
        others = self.open
        status_then = self.app_status()
        answer: dict[str, Any] = {"code": None, "first_byte": None, "bytes": 0, "stream": False}

        async def timed_send(message: dict) -> None:
            if message["type"] == "http.response.start":
                answer["code"] = message["status"]
                answer["first_byte"] = self.clock() - start
                for name, value in message.get("headers", []):
                    if name.lower() == b"content-type" and value.startswith(b"text/event-stream"):
                        answer["stream"] = True
            elif message["type"] == "http.response.body":
                answer["bytes"] += len(message.get("body", b""))
            await send(message)

        self.open += 1
        try:
            await self.app(scope, receive, timed_send)
        finally:
            self.open -= 1
            took = self.clock() - start
            if took >= self.limit_seconds and not answer["stream"]:
                self._log(scope, took, answer, others, status_then)

    def _log(self, scope: dict, took: float, answer: dict, others: int, status_then: str) -> None:
        path = scope.get("path", "")
        query = scope.get("query_string", b"").decode("latin-1")
        if query:
            path = f"{path}?{query[:80]}"
        first = answer["first_byte"]
        details = [
            f"first byte at {first:.2f} s, {_size(answer['bytes'])}"
            if first is not None
            else "no answer sent",
            f"{others} other request{'' if others == 1 else 's'} open",
        ]
        status_now = self.app_status()
        if status_then:
            details.append(
                f"app {status_then}"
                if status_now == status_then
                else f"app {status_then}, then {status_now}"
            )
        logger.info(
            "Slow console request: %s %s %s took %.2f s (%s)",
            scope.get("method", ""),
            path,
            answer["code"] or "-",
            took,
            "; ".join(details),
        )
