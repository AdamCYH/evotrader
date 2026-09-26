"""MCP integration layer.

Abstracts MCP server providers behind a pluggable interface so the system
can connect to any MCP-compatible broker or data source without code changes.
"""

from __future__ import annotations

from evotrader.mcp.alpha_vantage import AlphaVantageMcpProvider
from evotrader.mcp.provider import McpProvider, McpServerConfig
from evotrader.mcp.robinhood import RobinhoodMcpProvider
from evotrader.mcp.stock_analysis import StockAnalysisMcpProvider
from evotrader.models.config import McpProviderEntry

# Registry of known MCP provider classes.
# Providers not in this registry are handled as generic config-only providers.
_PROVIDER_REGISTRY: dict[str, type] = {
    "robinhood_official": RobinhoodMcpProvider,
    "alpha_vantage": AlphaVantageMcpProvider,
    "stock_analysis": StockAnalysisMcpProvider,
}


def get_mcp_provider(name: str, entry: McpProviderEntry) -> McpProvider:
    """Get the MCP provider instance by name.

    If the provider name matches a known implementation, returns
    a specialized provider class.  Otherwise, returns a generic
    config-based provider that just passes through the config.

    Args:
        name: Name of the provider (e.g., 'robinhood_official').
        entry: Configuration entry for the provider.

    Returns:
        The instantiated McpProvider.
    """
    provider_cls = _PROVIDER_REGISTRY.get(name)
    if provider_cls is not None:
        return provider_cls(entry)

    # Generic provider for unknown names — uses config directly
    return _GenericMcpProvider(name, entry)


class _GenericMcpProvider(McpProvider):
    """Fallback provider for config-only MCP servers.

    Allows new MCP servers to be added via settings.yaml without
    writing a custom provider class — just add the URL/command
    and transport config.
    """

    def __init__(self, name: str, config: McpProviderEntry) -> None:
        self._name = name
        self._config = config

    @property
    def name(self) -> str:
        return self._name

    def get_server_config(self) -> McpServerConfig:
        return McpServerConfig(
            url=self._config.url,
            headers=self._config.headers,
            transport=self._config.transport,
            auth=self._config.auth,
            command=self._config.command,
            args=self._config.args,
        )

    def get_available_tools(self) -> list[str]:
        return []

    def get_read_only_tools(self) -> list[str]:
        return []

    def get_write_tools(self) -> list[str]:
        return []
