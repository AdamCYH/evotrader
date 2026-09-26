"""The page the browser lands on after the broker's sign-in redirect.

Found 2026-09-25 (owner report): after signing in to Robinhood the browser
stayed on a "return to the terminal" page even when the web console was
running. The page also said "Authorization Successful" whatever happened: a
cancelled sign-in (``?error=access_denied``), a link from an earlier attempt,
and a response that no flow was waiting for any more all looked like success.
"""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest

from evotrader.mcp import oauth

_CONSOLE = "http://127.0.0.1:8080/"


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def console():
    """The web console is running, as it is under ./run.sh."""
    oauth.set_return_url(_CONSOLE)
    yield _CONSOLE
    oauth.set_return_url(None)


async def _land(query: str, *, timeout: float) -> tuple[str, asyncio.Task]:
    """Start a sign-in flow, send the browser's redirect, return the page it gets."""
    port = _free_port()
    flow = asyncio.create_task(oauth.wait_for_callback(port=port, timeout=timeout))
    await asyncio.sleep(0.3)
    async with httpx.AsyncClient(trust_env=False) as client:
        page = (await client.get(f"http://127.0.0.1:{port}/callback?{query}")).text
    return page, flow


class TestSuccess:
    async def test_it_returns_to_the_console(self, console) -> None:
        page, flow = await _land("code=c1&state=s1", timeout=5.0)
        assert await flow == ("c1", "s1")
        assert "Broker connected" in page
        assert f'content="3;url={console}"' in page, "back to the console after 3 seconds"
        assert f'href="{console}"' in page, "and a link, for a browser that ignores the refresh"

    async def test_without_the_console_it_says_so(self) -> None:
        page, flow = await _land("code=c1&state=s1", timeout=5.0)
        assert await flow == ("c1", "s1")
        assert "Broker connected" in page
        assert "http-equiv" not in page, "nowhere to return to"
        assert "return to the terminal" in page


class TestNotASuccess:
    async def test_a_cancelled_sign_in(self, console) -> None:
        page, flow = await _land("error=access_denied&state=s1", timeout=1.5)
        assert "Broker connected" not in page
        assert "access_denied" in page
        assert "http-equiv" not in page, "stay put so the message can be read"
        assert f'href="{console}"' in page
        with pytest.raises(TimeoutError):
            await flow  # still waiting: the sign-in link can be opened again

    async def test_a_link_from_an_earlier_attempt(self, console, monkeypatch) -> None:
        monkeypatch.setattr(oauth, "_expected_state", "current")
        page, flow = await _land("code=c1&state=earlier", timeout=1.5)
        assert "Broker connected" not in page
        assert "previous authorization attempt" in page
        with pytest.raises(TimeoutError):
            await flow

    def test_a_response_nobody_is_waiting_for(self) -> None:
        assert oauth._pending_callback is None
        problem = oauth._callback_problem({"code": ["c1"], "state": ["s1"]})
        assert problem is not None and "waiting" in problem

    def test_text_from_the_address_bar_is_not_markup(self) -> None:
        page = oauth.render_callback_page("<script>alert(1)</script>", _CONSOLE)
        assert "<script>" not in page
        assert "&lt;script&gt;" in page


def test_the_page_wears_the_consoles_design() -> None:
    """Owner's ask (2026-09-25): the sign-in page should look like the console.

    It inlines the console's own design tokens when served, so changing the
    console's look changes this page too.
    """
    tokens = (oauth._DESIGN_TOKENS).read_text(encoding="utf-8")
    for page in (oauth.render_callback_page(None, _CONSOLE), oauth.render_callback_page("x", None)):
        assert tokens.strip() in page
        assert "var(--bg-light" in page and "var(--font-serif" in page
        assert "$" not in page.replace("$0", ""), "every template placeholder was filled"
