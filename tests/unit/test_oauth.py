"""Unit tests for the standalone OAuth module."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import httpx
import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from evotrader.mcp.oauth import (
    FileTokenStorage,
    create_oauth_httpx_factory,
    hashlib_url,
    resolve_pending_callback,
    wait_for_callback,
)


def test_hashlib_url() -> None:
    url = "https://agent.robinhood.com/mcp/trading"
    h = hashlib_url(url)
    assert len(h) == 16
    assert isinstance(h, str)


@pytest.mark.asyncio
async def test_file_token_storage(tmp_path: Path) -> None:
    storage = FileTokenStorage(tmp_path)

    # Initially empty
    assert await storage.get_tokens() is None
    assert await storage.get_client_info() is None

    # Test saving/loading tokens
    token = OAuthToken(
        access_token="test_access",
        refresh_token="test_refresh",
        expires_in=3600,
        scope="trading",
        token_type="Bearer",
    )
    await storage.set_tokens(token)
    loaded_token = await storage.get_tokens()
    assert loaded_token is not None
    assert loaded_token.access_token == "test_access"
    assert loaded_token.refresh_token == "test_refresh"

    # Test saving/loading client info
    client_info = OAuthClientInformationFull(
        client_id="test_client",
        client_secret="test_secret",
        redirect_uris=["http://localhost/callback"],
        token_endpoint_auth_method="none",
    )
    await storage.set_client_info(client_info)
    loaded_client = await storage.get_client_info()
    assert loaded_client is not None
    assert loaded_client.client_id == "test_client"
    assert loaded_client.client_secret == "test_secret"


@pytest.mark.asyncio
async def test_wait_for_callback_via_local_server() -> None:
    # Find a free ephemeral port
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    # Run callback server task
    server_task = asyncio.create_task(wait_for_callback(port=port, timeout=5.0))

    # Give server time to bind/listen
    await asyncio.sleep(0.3)

    async with httpx.AsyncClient(trust_env=False) as client:
        # Request with code and state params
        response = await client.get(
            f"http://127.0.0.1:{port}/callback?code=test_code_123&state=test_state_abc"
        )
        assert response.status_code == 200
        assert "Broker connected" in response.text

    code, state = await server_task
    assert code == "test_code_123"
    assert state == "test_state_abc"


@pytest.mark.asyncio
async def test_wait_for_callback_via_resolve() -> None:
    # Find a free ephemeral port (won't actually be used for this test)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    # Run callback server task
    server_task = asyncio.create_task(wait_for_callback(port=port, timeout=5.0))

    # Give it time to set up the Future
    await asyncio.sleep(0.3)

    # Resolve via the web dashboard path
    result = resolve_pending_callback("web_code_456", "web_state_xyz")
    assert result is True

    code, state = await server_task
    assert code == "web_code_456"
    assert state == "web_state_xyz"


@pytest.mark.asyncio
async def test_wait_for_callback_timeout() -> None:
    # Find a free ephemeral port
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    # Should timeout quickly
    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        await wait_for_callback(port=port, timeout=0.5)


def test_create_oauth_httpx_factory(tmp_path: Path) -> None:
    # Verify the factory returns a client_factory function
    factory = create_oauth_httpx_factory(
        "https://agent.robinhood.com/mcp/trading",
        port=8080,
        cache_dir=tmp_path,
    )
    assert callable(factory)

    # Check that calling the factory creates a valid client with custom auth provider
    client = factory()
    assert isinstance(client, httpx.AsyncClient)
    assert client.auth is not None


def test_clear_oauth_cache(tmp_path: Path) -> None:
    from evotrader.mcp.oauth import _active_providers, clear_oauth_cache

    oauth_test_dir = tmp_path / "oauth_test"
    oauth_test_dir.mkdir(parents=True, exist_ok=True)
    # Held on purpose: _active_providers is a WeakSet, and the factory holds
    # the only strong reference to the provider this test inspects.
    factory = create_oauth_httpx_factory(  # noqa: F841
        "https://agent.robinhood.com/mcp/trading",
        port=8080,
        cache_dir=oauth_test_dir,
    )
    # WeakSet doesn't support indexing — grab the provider from the set
    providers_snapshot = list(_active_providers)
    assert len(providers_snapshot) > 0
    provider = providers_snapshot[-1]

    # Simulate an active session with cached tokens
    provider._initialized = True
    provider.context.current_tokens = OAuthToken(
        access_token="dummy",
        refresh_token="dummy",
        expires_in=3600,
        token_type="Bearer",
    )
    provider.context.token_expiry_time = 9999999999.0

    clear_oauth_cache(cache_dir=oauth_test_dir)

    assert provider._initialized is False
    assert provider.context.current_tokens is None
    assert provider.context.token_expiry_time is None
    assert not oauth_test_dir.exists()
