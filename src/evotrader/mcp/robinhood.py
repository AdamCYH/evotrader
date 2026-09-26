"""Robinhood official MCP server provider.

Connects to Robinhood's Agentic Trading MCP server via Streamable HTTP
transport at ``https://agent.robinhood.com/mcp/trading``.

Authentication is browser-based OAuth: on the first connection the console
shows a link to Robinhood's own sign-in page (the terminal prints it too). You
sign in and authorise the agent there; the app only ever receives a token,
cached in ``~/.evotrader/oauth/``. No API tokens are stored in ``.env``.

Setup:
    1. Have a Robinhood account in good standing, with agentic trading enabled
    2. Run EvoTrader — on the first connection, open the sign-in link it shows
    3. Log in to Robinhood and authorise the Agentic Trading connection
    4. Robinhood creates a dedicated Agentic account for the agent

See: https://robinhood.com/us/en/support/agentic-trading
"""

from __future__ import annotations

from evotrader.mcp.provider import McpProvider, McpServerConfig
from evotrader.models.config import McpProviderEntry

# The official Robinhood MCP endpoint
_ROBINHOOD_MCP_URL = "https://agent.robinhood.com/mcp/trading"


class RobinhoodMcpProvider(McpProvider):
    """Official Robinhood MCP server via Streamable HTTP transport.

    Authentication is browser-based OAuth — no tokens needed in config. On the
    first connection the console (and the terminal) shows the sign-in link.
    """

    def __init__(self, config: McpProviderEntry | None = None) -> None:
        self._config = config or McpProviderEntry(
            url=_ROBINHOOD_MCP_URL,
        )

    @property
    def name(self) -> str:
        return "Robinhood Official"

    def get_server_config(self) -> McpServerConfig:
        """Return Streamable HTTP server configuration."""
        import os

        headers: dict[str, str] = {}

        # Merge any additional headers from config (e.g., custom proxy headers)
        if self._config.headers:
            headers.update(self._config.headers)

        # Check for auth token in environment variables
        auth_token = os.environ.get("ROBINHOOD_AUTH_TOKEN")
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        return McpServerConfig(
            url=self._config.url or _ROBINHOOD_MCP_URL,
            headers=headers,
            transport="streamable_http",
            auth=self._config.auth,
        )

    def get_available_tools(self) -> list[str]:
        """All tools exposed by Robinhood's MCP server."""
        return self.get_read_only_tools() + self.get_write_tools()

    def get_read_only_tools(self) -> list[str]:
        """Read-only market data, portfolio, and order monitoring tools.

        Verified against the live Robinhood MCP server (52 tools total).
        Last synced: 2026-07-29 via scripts/discover_mcp_tools.py (now the mcp-debugger skill's `dump`)
        """
        return [
            # Portfolio & account
            "get_portfolio",
            "get_accounts",
            "get_equity_positions",
            "get_option_positions",
            # Market data — equities
            "get_equity_quotes",
            "get_equity_historicals",
            "get_equity_fundamentals",
            "get_equity_tradability",
            "get_equity_price_book",  # Level II order book (bid/ask depth)
            "get_equity_technical_indicators",  # RSI, MACD, Bollinger, ATR, etc.
            "get_financials",  # Revenue, profit, margins by period
            # Market data — options
            "get_option_chains",
            "get_option_instruments",
            "get_option_quotes",
            "get_option_historicals",
            "get_option_level_upgrade_info",
            # Market data — indexes
            "get_index_quotes",
            "get_indexes",
            # Order monitoring (read-only)
            "get_equity_orders",
            "get_option_orders",
            # Earnings
            "get_earnings_calendar",
            "get_earnings_results",
            # P&L & tax
            "get_pnl_trade_history",
            "get_realized_pnl",
            "get_equity_tax_lots",  # Per-lot cost basis for wash sale analysis
            # Watchlists
            "get_watchlists",
            "get_watchlist_items",
            "get_option_watchlist",
            "get_popular_watchlists",
            # Scanners
            "get_scans",
            "get_scanner_filter_specs",
            # Search
            "search",
        ]

    def get_write_tools(self) -> list[str]:
        """Tools that modify state (order placement / cancellation / watchlists)."""
        return [
            "place_equity_order",
            "review_equity_order",
            "place_option_order",
            "review_option_order",
            "cancel_equity_order",
            "cancel_option_order",
            # Options exercise
            "exercise_option",
            "cancel_option_exercise",
            # Watchlists
            "create_watchlist",
            "update_watchlist",
            "add_to_watchlist",
            "remove_from_watchlist",
            "add_option_to_watchlist",
            "remove_option_from_watchlist",
            "follow_watchlist",
            "unfollow_watchlist",
            # Scanners
            "create_scan",
            "run_scan",
            "update_scan_config",
            "update_scan_filters",
        ]
