"""Abstract MCP provider interface.

Defines the contract that all MCP server providers must implement.
This abstraction enables pluggable broker/data-source integration —
swap Robinhood for any MCP-compatible service without touching core code.

Framework-agnostic: uses a simple ``McpServerConfig`` dataclass instead of
any SDK-specific types. The agent factory translates this to the appropriate
framework format at startup.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class RetryingAsyncTransport(httpx.AsyncHTTPTransport):
    """Subclass of httpx.AsyncHTTPTransport that automatically retries requests on connection errors/timeouts/5xx status."""

    def __init__(
        self,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        retries = 0
        while True:
            try:
                response = await super().handle_async_request(request)
                if response.status_code in (502, 503, 504) and retries < self.max_retries:
                    retries += 1
                    sleep_time = self.backoff_factor * (2 ** (retries - 1))
                    logger.warning(
                        "MCP HTTP request returned status %d. Retrying %d/%d in %.2fs...",
                        response.status_code,
                        retries,
                        self.max_retries,
                        sleep_time,
                    )
                    await asyncio.sleep(sleep_time)
                    continue
                return response
            except (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.WriteTimeout,
            ) as exc:
                if retries < self.max_retries:
                    retries += 1
                    sleep_time = self.backoff_factor * (2 ** (retries - 1))
                    logger.warning(
                        "MCP HTTP request failed with exception: %s. Retrying %d/%d in %.2fs...",
                        exc,
                        retries,
                        self.max_retries,
                        sleep_time,
                    )
                    await asyncio.sleep(sleep_time)
                    continue
                raise


@dataclass(frozen=True)
class McpServerConfig:
    """Framework-agnostic MCP server configuration.

    This is translated to the appropriate SDK type by the agent factory:
    - ADK (streamable_http): ``StreamableHTTPConnectionParams``
    - ADK (sse): ``SseConnectionParams``
    - ADK (stdio): ``StdioConnectionParams``
    """

    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    transport: str = "streamable_http"  # "streamable_http", "sse", or "stdio"
    auth: str | None = None
    command: str | None = None
    args: list[str] = field(default_factory=list)


class McpProvider(ABC):
    """Abstract base class for MCP server providers.

    Each implementation encapsulates the configuration needed to connect
    to a specific MCP server (broker, data source, etc.) and declares
    which tools it exposes for safety policy configuration.

    Implementing a new provider requires only:
    1. Subclass ``McpProvider``
    2. Implement the abstract methods
    3. Register the provider name in ``settings.yaml``
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name (e.g., 'Robinhood Official')."""
        ...

    @abstractmethod
    def get_server_config(self) -> McpServerConfig:
        """Return the MCP server configuration.

        This is passed to the agent factory which translates it
        to the appropriate framework-specific format.
        """
        ...

    @abstractmethod
    def get_available_tools(self) -> list[str]:
        """Return names of all tools this MCP server exposes.

        Used for documentation, policy generation, and capability discovery.
        """
        ...

    @abstractmethod
    def get_read_only_tools(self) -> list[str]:
        """Return names of read-only tools (safe to always allow).

        These tools only retrieve data and cannot modify state. They are
        automatically allowed in safety policies.
        """
        ...

    @abstractmethod
    def get_write_tools(self) -> list[str]:
        """Return names of tools that modify state (place orders, cancel, etc.).

        These tools require explicit safety policy approval and are gated
        through the Risk Manager in live trading mode.
        """
        ...

    def get_all_tool_names(self) -> set[str]:
        """Return a set of all tool names (read + write)."""
        return set(self.get_read_only_tools()) | set(self.get_write_tools())
