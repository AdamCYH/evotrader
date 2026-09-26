"""Stock Analysis MCP server provider.

Connects to the ``stock-analysis-mcp`` server via stdio transport.
Uses ``uvx`` to launch the server as a subprocess — no API key required.

The server provides comprehensive stock analysis including:
- News catalysts and sentiment
- Technical analysis and trends
- Fundamental data (margins, growth, balance sheet)
- Analyst coverage and short interest
- Earnings data and calendar events

See: https://github.com/nickzren/stock-analysis-mcp
"""

from __future__ import annotations

from evotrader.mcp.provider import McpProvider, McpServerConfig
from evotrader.models.config import McpProviderEntry

# Default command to launch the stock-analysis-mcp server
_DEFAULT_COMMAND = "uvx"
_DEFAULT_ARGS = [
    "--from",
    "git+https://github.com/nickzren/stock-analysis-mcp",
    "stock-analysis",
]


class StockAnalysisMcpProvider(McpProvider):
    """Stock Analysis MCP server via stdio transport.

    Runs as a subprocess using ``uvx`` — no API key or external
    service configuration needed.  Provides rich stock analysis
    data powered by yfinance.
    """

    def __init__(self, config: McpProviderEntry | None = None) -> None:
        self._config = config or McpProviderEntry(
            command=_DEFAULT_COMMAND,
            args=_DEFAULT_ARGS,
            transport="stdio",
        )

    @property
    def name(self) -> str:
        return "Stock Analysis"

    def get_server_config(self) -> McpServerConfig:
        """Return stdio server configuration."""
        return McpServerConfig(
            command=self._config.command or _DEFAULT_COMMAND,
            args=self._config.args or _DEFAULT_ARGS,
            transport="stdio",
        )

    def get_available_tools(self) -> list[str]:
        """All tools exposed by stock-analysis-mcp."""
        return self.get_read_only_tools() + self.get_write_tools()

    def get_read_only_tools(self) -> list[str]:
        """Read-only analysis tools."""
        return [
            "analyze",
        ]

    def get_write_tools(self) -> list[str]:
        """No write tools — this is a read-only research server."""
        return []
