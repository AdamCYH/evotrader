"""Tests for OAuth token persistence and expiry restoration.

Covers:
- FileTokenStorage set/get with expiry persistence
- PersistentOAuthClientProvider expiry restoration
"""

from __future__ import annotations

import json
import time

import pytest


class TestFileTokenStorage:
    """Tests for FileTokenStorage with expiry persistence."""

    @pytest.fixture
    def cache_dir(self, tmp_path):
        return tmp_path / "oauth" / "test_hash"

    @pytest.fixture
    def storage(self, cache_dir):
        from evotrader.mcp.oauth import FileTokenStorage

        return FileTokenStorage(cache_dir)

    @pytest.mark.asyncio
    async def test_set_tokens_creates_token_file(self, storage, cache_dir) -> None:
        from mcp.shared.auth import OAuthToken

        token = OAuthToken(
            access_token="test_access",
            refresh_token="test_refresh",
            expires_in=3600,
        )
        await storage.set_tokens(token)

        assert storage.token_file.exists()
        data = json.loads(storage.token_file.read_text())
        assert data["access_token"] == "test_access"
        assert data["refresh_token"] == "test_refresh"

    @pytest.mark.asyncio
    async def test_set_tokens_persists_expiry(self, storage) -> None:
        from mcp.shared.auth import OAuthToken

        token = OAuthToken(
            access_token="test_access",
            expires_in=3600,
        )
        before = time.time()
        await storage.set_tokens(token)
        after = time.time()

        assert storage._expiry_file.exists()
        expiry = storage.get_persisted_expiry()
        assert expiry is not None
        # Expiry should be ~3600 seconds from now
        assert before + 3600 <= expiry <= after + 3600

    @pytest.mark.asyncio
    async def test_get_tokens_roundtrip(self, storage) -> None:
        from mcp.shared.auth import OAuthToken

        token = OAuthToken(
            access_token="test_access",
            refresh_token="test_refresh",
            expires_in=7200,
        )
        await storage.set_tokens(token)

        loaded = await storage.get_tokens()
        assert loaded is not None
        assert loaded.access_token == "test_access"
        assert loaded.refresh_token == "test_refresh"

    @pytest.mark.asyncio
    async def test_no_tokens_returns_none(self, storage) -> None:
        loaded = await storage.get_tokens()
        assert loaded is None

    def test_no_expiry_returns_none(self, storage) -> None:
        assert storage.get_persisted_expiry() is None

    @pytest.mark.asyncio
    async def test_expiry_none_when_no_expires_in(self, storage) -> None:
        from mcp.shared.auth import OAuthToken

        token = OAuthToken(access_token="test_access")  # No expires_in
        await storage.set_tokens(token)
        expiry = storage.get_persisted_expiry()
        assert expiry is None

    @pytest.mark.asyncio
    async def test_client_info_roundtrip(self, storage) -> None:
        from mcp.shared.auth import OAuthClientInformationFull

        client = OAuthClientInformationFull(
            redirect_uris=["http://localhost:3893/callback"],
            client_id="test_client_id",
        )
        await storage.set_client_info(client)

        loaded = await storage.get_client_info()
        assert loaded is not None
        assert loaded.client_id == "test_client_id"
