"""Alpha Vantage MCP server provider.

Connects to the **official** Alpha Vantage MCP server via streamable HTTP.
Uses NASDAQ-licensed data — no scraping, no subprocess.

The server uses Progressive Tool Discovery to minimize token usage:
- ``TOOL_LIST`` — enumerate available functions
- ``TOOL_GET``  — retrieve schema for a specific function
- ``TOOL_CALL`` — invoke a function with arguments

Key capabilities for our use case:
- ``NEWS_SENTIMENT`` — AI-scored news sentiment by ticker
- ``COMPANY_OVERVIEW`` — fundamentals, sector, market cap
- ``EARNINGS`` — quarterly/annual earnings data
- ``TOP_GAINERS_LOSERS`` — market movers
- ``TIME_SERIES_*`` — intraday/daily/weekly OHLCV
- Economic indicators: CPI, FEDERAL_FUNDS_RATE, NONFARM_PAYROLL, etc.

See: https://mcp.alphavantage.co/
"""

from __future__ import annotations

import logging
import os
import urllib.parse
from collections.abc import Callable, Generator
from typing import Any

import httpx

from evotrader.mcp.provider import McpProvider, McpServerConfig
from evotrader.models.config import McpProviderEntry

logger = logging.getLogger(__name__)

# Default Alpha Vantage MCP server URL
_DEFAULT_URL = "https://mcp.alphavantage.co/mcp"


class AlphaVantageKeyRotatorAuth(httpx.Auth):
    """HTTPX Auth class that intercepts Alpha Vantage rate limits and rotates keys.

    Alpha Vantage returns 200 responses with an "Information" warning containing
    rate limit messages rather than standard 429 HTTP statuses. We check for
    these strings in the body, switch the API key in the request's query parameters,
    and retry the request.
    """

    requires_response_body = True

    def __init__(self, api_keys: list[str]) -> None:
        self.api_keys = api_keys
        self.current_index = 0

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        response = yield request

        while self._is_rate_limited(response):
            if self.current_index + 1 < len(self.api_keys):
                old_key = self.api_keys[self.current_index]
                self.current_index += 1
                new_key = self.api_keys[self.current_index]

                # Obfuscate keys for logging safety
                def _obfuscate(k: str) -> str:
                    return k[:4] + "..." + k[-4:] if len(k) > 8 else k

                logger.warning(
                    "Alpha Vantage API rate limit detected. Rotating key from %s to %s.",
                    _obfuscate(old_key),
                    _obfuscate(new_key),
                )

                # Rebuild/modify the URL query parameters with the new API key
                q = urllib.parse.parse_qs(request.url.query.decode("utf-8"))
                q["apikey"] = [new_key]
                new_query = urllib.parse.urlencode(q, doseq=True).encode("utf-8")

                # Mutate request URL in-place
                request.url = request.url.copy_with(query=new_query)

                # Retry request
                response = yield request
            else:
                logger.error("All Alpha Vantage API keys have been exhausted and rate-limited.")
                break

    def _is_rate_limited(self, response: httpx.Response) -> bool:
        """Parse response text to check if rate-limited by Alpha Vantage."""
        try:
            content = response.text.lower()
            # Alpha Vantage standard rate limit notices contain 'rate limit' and 'requests per day' or 'requests per minute'
            if "rate limit" in content and (
                "requests per day" in content
                or "requests per minute" in content
                or "standard api rate limit" in content
            ):
                return True
        except Exception as e:
            logger.debug("Failed to check rate limit in Alpha Vantage response: %s", e)
        return False


def create_alpha_vantage_httpx_factory(
    api_keys: list[str],
) -> Callable[..., httpx.AsyncClient]:
    """Create an httpx client factory that injects Alpha Vantage key rotation.

    Args:
        api_keys: The list of API keys to rotate between.

    Returns:
        A factory function that returns an httpx.AsyncClient configured with
        the key rotator auth.
    """
    auth_provider = AlphaVantageKeyRotatorAuth(api_keys)

    def client_factory(
        headers: dict[str, Any] | None = None,
        auth: httpx.Auth | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> httpx.AsyncClient:
        from evotrader.mcp.provider import RetryingAsyncTransport

        kwargs: dict[str, Any] = {
            "follow_redirects": True,
            "auth": auth_provider,
            "transport": RetryingAsyncTransport(),
        }
        if timeout is not None:
            kwargs["timeout"] = timeout
        if headers is not None:
            kwargs["headers"] = headers
        return httpx.AsyncClient(**kwargs)

    return client_factory


class AlphaVantageMcpProvider(McpProvider):
    """Alpha Vantage official MCP server via streamable HTTP.

    NASDAQ-licensed data delivered over HTTP.  No subprocess, no scraping,
    no security concerns.  Requires a free API key from Alpha Vantage
    (set ``ALPHA_VANTAGE_API_KEY`` in ``.env``).

    Free tier: 25 requests/day.
    Premium ($49.99/mo): 75 requests/minute.
    """

    def __init__(self, config: McpProviderEntry | None = None) -> None:
        self._config = config or McpProviderEntry(
            url=_DEFAULT_URL,
            transport="streamable_http",
        )

    @property
    def name(self) -> str:
        return "Alpha Vantage"

    def get_server_config(self) -> McpServerConfig:
        """Return streamable HTTP server configuration.

        Appends the first API key as a query parameter to the server URL,
        matching the Alpha Vantage MCP authentication pattern.
        """
        api_key_str = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
        # Get the first key in the comma-separated list
        api_keys = [k.strip() for k in api_key_str.split(",") if k.strip()]
        api_key = api_keys[0] if api_keys else ""

        base_url = self._config.url or _DEFAULT_URL

        # Append API key as query parameter (official pattern)
        if api_key:
            separator = "&" if "?" in base_url else "?"
            url = f"{base_url}{separator}apikey={api_key}"
        else:
            url = base_url

        return McpServerConfig(
            url=url,
            transport="streamable_http",
        )

    def get_available_tools(self) -> list[str]:
        """All tools exposed by Alpha Vantage MCP.

        Alpha Vantage uses Progressive Tool Discovery — the actual
        financial functions (TIME_SERIES_DAILY, NEWS_SENTIMENT, etc.)
        are accessed via these wrapper tools.
        """
        return self.get_read_only_tools() + self.get_write_tools()

    def get_read_only_tools(self) -> list[str]:
        """Read-only tools — all Alpha Vantage tools are read-only."""
        return [
            "TOOL_LIST",
            "TOOL_GET",
            "TOOL_CALL",
        ]

    def get_write_tools(self) -> list[str]:
        """No write tools — Alpha Vantage is a read-only data provider."""
        return []
