"""Tests for multi-MCP provider architecture.

Tests the role-based MCP configuration and provider resolution.
"""

from __future__ import annotations

import pytest

from evotrader.models.config import McpConfig, McpProviderEntry

# ---------------------------------------------------------------------------
# McpProviderEntry
# ---------------------------------------------------------------------------


class TestMcpProviderEntry:
    """Tests for McpProviderEntry data model."""

    def test_http_provider_defaults(self):
        entry = McpProviderEntry(url="https://example.com/mcp")
        assert entry.transport == "streamable_http"
        assert entry.command is None
        assert entry.args == []
        assert entry.auth is None

    def test_stdio_provider(self):
        entry = McpProviderEntry(
            command="uvx",
            args=["--from", "pkg", "cmd"],
            transport="stdio",
        )
        assert entry.transport == "stdio"
        assert entry.command == "uvx"
        assert entry.args == ["--from", "pkg", "cmd"]
        assert entry.url is None

    def test_url_is_optional(self):
        """URL should be optional for stdio providers."""
        entry = McpProviderEntry(transport="stdio", command="uvx")
        assert entry.url is None


# ---------------------------------------------------------------------------
# McpConfig — role-based essentials
# ---------------------------------------------------------------------------


class TestMcpConfigRoleBasics:
    """Tests for McpConfig when only roles are defined (no legacy provider)."""

    def test_active_provider_requires_trading_role(self):
        """active_provider() should raise when no 'trading' role is configured."""
        config = McpConfig(
            providers={
                "robinhood_official": McpProviderEntry(
                    url="https://agent.robinhood.com/mcp/trading"
                ),
            },
            roles={},
        )
        with pytest.raises(ValueError, match="No 'trading' role configured"):
            config.active_provider()

    def test_active_provider_with_trading_role(self):
        """active_provider() resolves from 'trading' role."""
        config = McpConfig(
            providers={
                "robinhood_official": McpProviderEntry(
                    url="https://agent.robinhood.com/mcp/trading"
                ),
            },
            roles={"trading": "robinhood_official"},
        )
        entry = config.active_provider()
        assert entry.url == "https://agent.robinhood.com/mcp/trading"

    def test_unconfigured_role_returns_empty(self):
        """Unconfigured roles should return an empty list."""
        config = McpConfig(
            providers={
                "robinhood_official": McpProviderEntry(
                    url="https://agent.robinhood.com/mcp/trading"
                ),
            },
            roles={"trading": "robinhood_official"},
        )
        result = config.providers_for_role("research")
        assert result == []


# ---------------------------------------------------------------------------
# McpConfig — role-based mapping
# ---------------------------------------------------------------------------


class TestMcpConfigRoles:
    """Tests for role-based MCP provider mapping."""

    @pytest.fixture
    def multi_config(self):
        return McpConfig(
            providers={
                "robinhood_official": McpProviderEntry(
                    url="https://agent.robinhood.com/mcp/trading",
                    transport="streamable_http",
                ),
                "stock_analysis": McpProviderEntry(
                    command="uvx",
                    args=[
                        "--from",
                        "git+https://github.com/nickzren/stock-analysis-mcp",
                        "stock-analysis",
                    ],
                    transport="stdio",
                ),
            },
            roles={
                "trading": "robinhood_official",
                "research": "stock_analysis",
            },
        )

    def test_role_resolution_trading(self, multi_config):
        result = multi_config.providers_for_role("trading")
        assert len(result) == 1
        name, entry = result[0]
        assert name == "robinhood_official"
        assert entry.transport == "streamable_http"

    def test_role_resolution_research(self, multi_config):
        result = multi_config.providers_for_role("research")
        assert len(result) == 1
        name, entry = result[0]
        assert name == "stock_analysis"
        assert entry.transport == "stdio"
        assert entry.command == "uvx"

    def test_unknown_role_returns_empty(self, multi_config):
        result = multi_config.providers_for_role("unknown")
        assert result == []

    def test_active_provider_uses_trading_role(self, multi_config):
        """active_provider() should resolve via 'trading' role first."""
        entry = multi_config.active_provider()
        assert entry.url == "https://agent.robinhood.com/mcp/trading"

    def test_provider_by_name(self, multi_config):
        entry = multi_config.provider_by_name("stock_analysis")
        assert entry.command == "uvx"
        assert entry.transport == "stdio"

    def test_provider_by_name_unknown_raises(self, multi_config):
        with pytest.raises(ValueError, match="not found"):
            multi_config.provider_by_name("nonexistent")


# ---------------------------------------------------------------------------
# MCP Provider implementations
# ---------------------------------------------------------------------------


class TestRobinhoodProvider:
    """Tests for Robinhood MCP provider."""

    def test_get_news_not_in_tools(self):
        """get_news should NOT be in Robinhood's tool list (it doesn't exist)."""
        from evotrader.mcp.robinhood import RobinhoodMcpProvider

        provider = RobinhoodMcpProvider()
        all_tools = provider.get_available_tools()
        assert "get_news" not in all_tools
        assert "get_equity_quotes" in all_tools
        assert "get_equity_historicals" in all_tools

    def test_server_config_transport(self):
        from evotrader.mcp.robinhood import RobinhoodMcpProvider

        provider = RobinhoodMcpProvider()
        config = provider.get_server_config()
        assert config.transport == "streamable_http"


class TestStockAnalysisProvider:
    """Tests for Stock Analysis MCP provider."""

    def test_default_config(self):
        from evotrader.mcp.stock_analysis import StockAnalysisMcpProvider

        provider = StockAnalysisMcpProvider()
        config = provider.get_server_config()
        assert config.transport == "stdio"
        assert config.command == "uvx"
        assert "stock-analysis" in config.args

    def test_name(self):
        from evotrader.mcp.stock_analysis import StockAnalysisMcpProvider

        provider = StockAnalysisMcpProvider()
        assert provider.name == "Stock Analysis"

    def test_no_write_tools(self):
        from evotrader.mcp.stock_analysis import StockAnalysisMcpProvider

        provider = StockAnalysisMcpProvider()
        assert provider.get_write_tools() == []

    def test_analyze_in_read_tools(self):
        from evotrader.mcp.stock_analysis import StockAnalysisMcpProvider

        provider = StockAnalysisMcpProvider()
        assert "analyze" in provider.get_read_only_tools()


# ---------------------------------------------------------------------------
# MCP Provider registry
# ---------------------------------------------------------------------------


class TestMcpProviderRegistry:
    """Tests for the MCP provider factory/registry."""

    def test_known_provider_robinhood(self):
        from evotrader.mcp import get_mcp_provider

        entry = McpProviderEntry(url="https://agent.robinhood.com/mcp/trading")
        provider = get_mcp_provider("robinhood_official", entry)
        assert provider.name == "Robinhood Official"

    def test_known_provider_stock_analysis(self):
        from evotrader.mcp import get_mcp_provider

        entry = McpProviderEntry(command="uvx", transport="stdio")
        provider = get_mcp_provider("stock_analysis", entry)
        assert provider.name == "Stock Analysis"

    def test_unknown_provider_returns_generic(self):
        """Unknown providers should get a generic fallback, not raise."""
        from evotrader.mcp import get_mcp_provider

        entry = McpProviderEntry(
            url="https://some-mcp.example.com",
            transport="sse",
        )
        provider = get_mcp_provider("my_custom_mcp", entry)
        assert provider.name == "my_custom_mcp"
        config = provider.get_server_config()
        assert config.transport == "sse"
        assert config.url == "https://some-mcp.example.com"


class TestAlphaVantageKeyRotation:
    """Tests for Alpha Vantage key rotation and retry logic."""

    def test_get_server_config_multiple_keys(self, monkeypatch):
        """AlphaVantageMcpProvider should use only the first key in server config."""
        from evotrader.mcp.alpha_vantage import AlphaVantageMcpProvider

        monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "key1,key2,key3")
        provider = AlphaVantageMcpProvider()
        config = provider.get_server_config()

        assert "apikey=key1" in config.url
        assert "key2" not in config.url
        assert "key3" not in config.url

    def test_key_rotation_success(self):
        """AlphaVantageKeyRotatorAuth should rotate keys on rate limit and succeed."""
        import urllib.parse

        import httpx

        from evotrader.mcp.alpha_vantage import AlphaVantageKeyRotatorAuth

        rotated_keys = []

        def mock_handler(request: httpx.Request) -> httpx.Response:
            q = urllib.parse.parse_qs(request.url.query.decode("utf-8"))
            key = q.get("apikey", [None])[0]
            rotated_keys.append(key)
            if key == "key1":
                # Simulate rate limit response body
                return httpx.Response(
                    200,
                    text='{"Information": "Our standard API rate limit is 25 requests per day..."}',
                )
            else:
                return httpx.Response(200, json={"data": "success"})

        transport = httpx.MockTransport(mock_handler)
        auth = AlphaVantageKeyRotatorAuth(["key1", "key2"])

        with httpx.Client(auth=auth, transport=transport) as client:
            response = client.get("https://mcp.alphavantage.co/mcp?apikey=key1")

            assert response.status_code == 200
            assert response.json() == {"data": "success"}
            assert rotated_keys == ["key1", "key2"]

    def test_key_rotation_exhaustion(self):
        """AlphaVantageKeyRotatorAuth should stop retrying and return the last response when keys are exhausted."""
        import urllib.parse

        import httpx

        from evotrader.mcp.alpha_vantage import AlphaVantageKeyRotatorAuth

        rotated_keys = []

        def mock_handler(request: httpx.Request) -> httpx.Response:
            q = urllib.parse.parse_qs(request.url.query.decode("utf-8"))
            key = q.get("apikey", [None])[0]
            rotated_keys.append(key)
            # Both keys will be rate limited
            return httpx.Response(
                200, text='{"Information": "Our standard API rate limit is 25 requests per day..."}'
            )

        transport = httpx.MockTransport(mock_handler)
        auth = AlphaVantageKeyRotatorAuth(["key1", "key2"])

        with httpx.Client(auth=auth, transport=transport) as client:
            response = client.get("https://mcp.alphavantage.co/mcp?apikey=key1")

            assert response.status_code == 200
            assert "standard API rate limit is 25 requests per day" in response.text
            assert rotated_keys == ["key1", "key2"]


# ---------------------------------------------------------------------------
# RetryingAsyncTransport
# ---------------------------------------------------------------------------


class TestRetryingAsyncTransport:
    """Tests for RetryingAsyncTransport to ensure reliability of MCP calls."""

    @pytest.mark.asyncio
    async def test_retry_on_503(self, monkeypatch):
        import httpx

        from evotrader.mcp.provider import RetryingAsyncTransport

        attempts = 0

        async def mock_handle(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return httpx.Response(503, text="Service Unavailable")
            return httpx.Response(200, text="Success")

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", mock_handle)

        transport = RetryingAsyncTransport(max_retries=3, backoff_factor=0.001)
        request = httpx.Request("GET", "https://mcp.example.com")

        response = await transport.handle_async_request(request)
        assert response.status_code == 200
        assert response.text == "Success"
        assert attempts == 3

    @pytest.mark.asyncio
    async def test_retry_on_exception(self, monkeypatch):
        import httpx

        from evotrader.mcp.provider import RetryingAsyncTransport

        attempts = 0

        async def mock_handle(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise httpx.ConnectError("Connection refused")
            return httpx.Response(200, text="Success")

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", mock_handle)

        transport = RetryingAsyncTransport(max_retries=3, backoff_factor=0.001)
        request = httpx.Request("GET", "https://mcp.example.com")

        response = await transport.handle_async_request(request)
        assert response.status_code == 200
        assert response.text == "Success"
        assert attempts == 3

    @pytest.mark.asyncio
    async def test_retry_exhaustion(self, monkeypatch):
        import httpx

        from evotrader.mcp.provider import RetryingAsyncTransport

        attempts = 0

        async def mock_handle(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            raise httpx.ConnectError("Connection refused")

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", mock_handle)

        transport = RetryingAsyncTransport(max_retries=3, backoff_factor=0.001)
        request = httpx.Request("GET", "https://mcp.example.com")

        with pytest.raises(httpx.ConnectError):
            await transport.handle_async_request(request)

        assert attempts == 4  # 1 initial + 3 retries
