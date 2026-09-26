"""A dead OAuth flow must not leave a live-looking prompt in the dashboard.

Live incident, 2026-09-18. Robinhood's token refresh returned 404 at 15:30, so
the system asked for a full re-login and broadcast ``oauth_required``. Nobody was
at the screen; the flow timed out at 15:35. The retry then refreshed cleanly and
the system was fully connected again from 15:40 onwards.

But ``wait_for_callback`` only cleared ``web.server.pending_oauth`` on the
SUCCESS path, and the timeout raised straight past it. So the stale prompt stayed
set — and the SSE endpoint REPLAYS ``pending_oauth`` to every new connection, so
every dashboard reload re-raised an authorization prompt for a flow that had been
dead for hours. At 18:20 the operator authorized against it, pasted the redirect
URL, and got "No pending OAuth flow to resolve. Please restart the system and try
again." — advice to restart a system that was healthy and authenticated.
"""

from __future__ import annotations

import asyncio

import pytest

from evotrader.mcp import oauth


@pytest.fixture(autouse=True)
def _clean_server_state():
    import evotrader.web.server as server

    server.pending_oauth = None
    yield
    server.pending_oauth = None


class TestATimedOutFlowDismissesTheseModal:
    async def test_timeout_clears_the_pending_prompt(self) -> None:
        """THE BUG: after this raised, the dashboard still showed the prompt."""
        import evotrader.web.server as server

        server.pending_oauth = {"url": "https://example.com/authorize?state=abc", "port": 3893}

        with pytest.raises(TimeoutError):
            await oauth.wait_for_callback(port=0, timeout=0.05)

        assert server.pending_oauth is None, (
            "a dead flow must not keep prompting; the SSE endpoint replays this "
            "to every new dashboard connection"
        )

    async def test_timeout_tells_the_dashboard_to_dismiss(self) -> None:
        import evotrader.web.server as server

        events: list[tuple[str, object]] = []
        server.pending_oauth = {"url": "https://example.com/authorize", "port": 3893}

        original = server.broadcast_sse_event
        try:
            server.broadcast_sse_event = lambda t, d: events.append((t, d))
            with pytest.raises(TimeoutError):
                await oauth.wait_for_callback(port=0, timeout=0.05)
        finally:
            server.broadcast_sse_event = original

        assert any(t == "oauth_failed" for t, _ in events), events

    async def test_success_still_clears_it(self) -> None:
        import evotrader.web.server as server

        server.pending_oauth = {"url": "https://example.com/authorize", "port": 3893}

        async def resolve_soon() -> None:
            await asyncio.sleep(0.02)
            oauth.resolve_pending_callback("thecode", None)

        task = asyncio.create_task(resolve_soon())
        code, _ = await oauth.wait_for_callback(port=0, timeout=2.0)
        await task

        assert code == "thecode"
        assert server.pending_oauth is None


class TestTheCallbackIsResolvedFromTheServerThread:
    """The local callback server runs ``handle_request`` in a worker thread, so
    it resolves the Future from OFF the event loop. Today the 0.5-second poll in
    that server keeps the loop waking up, which masks the fact that
    ``Future.set_result`` is not thread-safe; this covers the path working, and
    the implementation schedules the result thread-safely so it does not depend
    on that polling to stay correct."""

    async def test_a_code_arriving_on_another_thread_is_delivered(self) -> None:
        import threading
        import time

        def worker() -> None:
            time.sleep(0.2)
            oauth.resolve_pending_callback("threadcode", "st")

        threading.Thread(target=worker, daemon=True).start()
        code, state = await oauth.wait_for_callback(port=0, timeout=5.0)
        assert (code, state) == ("threadcode", "st")


class TestTheDashboardErrorIsAccurate:
    def test_it_does_not_tell_a_healthy_system_to_restart(self) -> None:
        """The operator was told to restart a system that was connected and
        authenticated. The message must not say that."""
        from pathlib import Path

        src = Path("src/evotrader/web/server.py").read_text()
        start = src.index("async def oauth_callback(")
        body = src[start : start + 3000]
        assert "Please restart the system" not in body, (
            "restarting was never the fix here — the flow had already completed"
        )


class TestFaviconDoesNotCrash:
    def test_response_is_imported(self) -> None:
        """Every /favicon.ico hit raised NameError: name 'Response' is not
        defined, returning a 500 and a full ASGI traceback in the log."""
        from fastapi.responses import Response  # noqa: F401

        import evotrader.web.server as server

        assert hasattr(server, "Response"), "server.py uses Response but never imported it"


class TestEveryWriterOfPendingOauthDeclaresItGlobal:
    """Regression, 2026-09-18 (self-inflicted, caught on restart).

    The stale-prompt check assigned to the module global ``pending_oauth``
    inside the SSE ``event_generator`` without declaring it ``global``. Python
    then treats the name as LOCAL for that whole function, so the read one line
    earlier raised ``UnboundLocalError`` — and every dashboard connection died
    with a 500 instead of receiving live events. The generator never ends, so a
    request-level test on it hangs; this asserts the invariant directly, and it
    covers every function in the module rather than the one that broke.
    """

    def test_no_function_assigns_pending_oauth_without_declaring_it_global(self) -> None:
        import ast
        from pathlib import Path as _Path

        tree = ast.parse(_Path("src/evotrader/web/server.py").read_text())
        offenders: list[str] = []

        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            declares = any(
                isinstance(n, ast.Global) and "pending_oauth" in n.names
                # Only this function's own body, not a nested def's.
                for n in _own_nodes(fn)
            )
            assigns = any(
                isinstance(t, ast.Name) and t.id == "pending_oauth"
                for n in _own_nodes(fn)
                if isinstance(n, ast.Assign)
                for t in n.targets
            )
            if assigns and not declares:
                offenders.append(f"{fn.name} (line {fn.lineno})")

        assert not offenders, (
            "these assign to the module global pending_oauth without declaring "
            f"it global, which makes every READ in them raise: {offenders}"
        )


def _own_nodes(fn):
    """Nodes belonging to *fn* itself, not to functions nested inside it."""
    import ast

    for node in ast.walk(fn):
        if node is fn:
            continue
        yield node
