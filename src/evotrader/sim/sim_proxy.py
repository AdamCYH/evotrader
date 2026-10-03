import json
import logging
from typing import Any, ClassVar

from evotrader.utils import select_agentic_account

logger = logging.getLogger(__name__)


class McpToolResponseContentBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class McpToolResponse:
    """Mock response that mimics standard MCP call_tool result object."""

    def __init__(self, text: str, is_error: bool = False) -> None:
        self.content = [McpToolResponseContentBlock(text)]
        self.isError = is_error

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Mimic Pydantic model_dump so ADK tool runner can serialize the response."""
        return {
            "content": [{"type": "text", "text": block.text} for block in self.content],
            "isError": self.isError,
        }


class SimSessionWrapper:
    """Wrapper that wraps an active MCP session to intercept calls."""

    def __init__(self, original_session: Any, sim_proxy: "SimBrokerProxy") -> None:
        self.original_session = original_session
        self.sim_proxy = sim_proxy

    async def call_tool(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        return await self.sim_proxy.call_tool_raw(name, arguments, self.original_session)

    def __getattr__(self, name: str) -> Any:
        # Delegate any other attribute access to the underlying session
        return getattr(self.original_session, name)


class SimSessionManagerWrapper:
    """Wrapper that wraps an MCP session manager to yield SimSessionWrapper."""

    def __init__(self, original_manager: Any, sim_proxy: "SimBrokerProxy") -> None:
        self.original_manager = original_manager
        self.sim_proxy = sim_proxy

    async def create_session(self, *args: Any, **kwargs: Any) -> SimSessionWrapper:
        original_session = await self.original_manager.create_session(*args, **kwargs)
        return SimSessionWrapper(original_session, self.sim_proxy)

    def __getattr__(self, name: str) -> Any:
        # Delegate any other attribute access to the underlying manager
        return getattr(self.original_manager, name)


class SimBrokerProxy:
    """Proxy that routes MCP tool calls to either the real MCP or SimBroker."""

    INTERCEPTED_TOOLS: ClassVar[set[str]] = {
        # Account/Portfolio
        "get_accounts",
        "get_portfolio",
        "get_equity_positions",
        "get_option_positions",
        # The practice account's tax lots, answered by the sim: one open lot per
        # position, no closed lots. The broker's tool reads the real account's
        # lots, which practice mode must never show; refused, it left the
        # wash-sale check with no answer at all.
        "get_equity_tax_lots",
        # Order management
        "place_stock_order",
        "place_equity_order",
        "place_option_order",
        "review_stock_order",
        "review_equity_order",
        "review_option_order",
        "cancel_order",
        # The broker's own cancel tools, the ones the agents are given. Without
        # them an agent's cancel in practice mode was refused as unsupported.
        "cancel_equity_order",
        "cancel_option_order",
        "get_orders",
        "get_equity_orders",
        "get_option_orders",
        "get_order_status",
    }

    # Read-only market data, passed to the real broker as it is. Each of these
    # tools describes the market, not an account: it places nothing, changes
    # nothing and (the tradability checks apart) takes no account number, so its
    # answer is the same whichever account asks. A read-only tool that reads the
    # real account is never passed through, however harmless it looks: positions,
    # portfolio, orders and tax lots are simulated above; realized P&L and trade
    # history (get_realized_pnl, get_pnl_trade_history), the options level
    # (get_option_level_upgrade_info), watchlists (get_watchlists,
    # get_watchlist_items, get_option_watchlist, get_popular_watchlists) and
    # screeners (get_scans, get_scanner_filter_specs, run_scan) describe the
    # owner's account or lead straight to tools that change it, nothing in the
    # pipeline reads them, and so they stay refused below.
    ALLOWED_PASSTHROUGH_TOOLS: ClassVar[set[str]] = {
        # Stocks: prices and bars
        "get_equity_quotes",  # the live quote (the sim fills stock orders against it)
        "get_equity_historicals",  # OHLCV bars
        "get_equity_price_book",  # the Level 2 book: everyone's resting orders, not ours
        "get_equity_technical_indicators",  # computed from the bars
        # Options: contracts and their prices
        "get_option_chains",
        "get_option_instruments",
        "get_option_quotes",  # the live quote (the sim fills option orders against it)
        "get_option_historicals",  # a contract's bars (the IV trend reads them)
        # Companies: facts about the issuer, not about who holds it
        "get_equity_fundamentals",  # valuation ratios, market cap
        "get_financials",  # revenue, profit, margins by period
        "get_earnings_calendar",  # market-wide earnings dates (the event context reads them)
        "get_earnings_results",  # one symbol's reported and estimated earnings
        # Market indexes
        "get_indexes",
        "get_index_quotes",
        # A name or ticker resolved to an instrument
        "search",
        # Whether a symbol may be traded in a session. The one pass-through that
        # takes an account number: call_tool_raw swaps the practice account's for
        # the real one, because the answer is about the instrument and the kind
        # of account, not about what it holds.
        "get_equity_tradability",
        "get_option_tradability",  # not among the broker's tools today; harmless if it returns
    }

    def __init__(self, sim_broker: Any, real_mcp_toolset: Any = None) -> None:
        self.sim_broker = sim_broker
        self.real_mcp_toolset = real_mcp_toolset
        # The real account's number, for the pass-through tools that take one;
        # looked up on first use.
        self.real_account_number: str | None = None

        # Inject the proxy into SimBroker so it can fetch live prices via real toolset
        self.sim_broker.real_mcp_toolset = real_mcp_toolset

    def attach_to_toolset(self, toolset: Any) -> None:
        """Patch the given McpToolset instance to use this proxy."""
        if not hasattr(toolset, "_mcp_session_manager"):
            logger.warning("Given toolset does not have _mcp_session_manager. Skipping attach.")
            return

        logger.info("Injecting SimBrokerProxy session manager wrapper into toolset %s", toolset)
        original_manager = toolset._mcp_session_manager
        # Avoid double patching
        if not isinstance(original_manager, SimSessionManagerWrapper):
            toolset._mcp_session_manager = SimSessionManagerWrapper(original_manager, self)

    async def call_tool_raw(
        self, name: str, arguments: dict[str, Any], original_session: Any
    ) -> Any:
        """Raw call interface that returns McpToolResponse objects for intercepted tools."""
        if name in self.INTERCEPTED_TOOLS:
            try:
                res_dict = await self._handle_sim(name, arguments)
                return McpToolResponse(json.dumps(res_dict))
            except Exception as e:
                logger.error(
                    "Failed handling simulated tool call %s with args %s: %s",
                    name,
                    arguments,
                    e,
                    exc_info=True,
                )
                return McpToolResponse(
                    json.dumps({"data": None, "error": str(e), "isError": True}), is_error=True
                )

        if name in self.ALLOWED_PASSTHROUGH_TOOLS:
            # A pass-through tool that takes an account number is asked about the
            # real account, by its own number; the practice account's name means
            # nothing to the broker.
            if arguments.get("account_number") == self.sim_broker.account_number:
                if self.real_account_number is None:
                    try:
                        real_accounts_res = await original_session.call_tool("get_accounts", {})
                        real_acc_data = json.loads(real_accounts_res.content[0].text)
                        # The broker lists accounts under data.accounts, and the
                        # agentic one is chosen as the live path chooses it. Read
                        # as a bare list, this never found an account, and the
                        # practice account's name went to the real broker.
                        data = real_acc_data.get("data") or {}
                        items = data.get("accounts") if isinstance(data, dict) else data
                        if isinstance(items, list) and items:
                            self.real_account_number = select_agentic_account(items)
                    except Exception as e:
                        logger.warning("Failed to fetch real account number for passthrough: %s", e)

                if self.real_account_number:
                    arguments["account_number"] = self.real_account_number

            # Passthrough allowed read-only market data tools
            return await original_session.call_tool(name, arguments=arguments)

        # Block any other unimplemented/non-intercepted tools from falling back to the live session
        error_msg = f"Error: Tool '{name}' is not supported in simulation mode."
        logger.error(error_msg)
        return McpToolResponse(
            json.dumps({"data": None, "error": error_msg, "isError": True}), is_error=True
        )

    async def _handle_sim(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Convert MCP tool invocation to SimBroker method calls."""
        acc_num = arguments.get("account_number") or self.sim_broker.account_number

        if name == "get_accounts":
            return await self.sim_broker.get_accounts()

        elif name == "get_portfolio":
            return await self.sim_broker.get_portfolio(acc_num)

        elif name == "get_equity_positions":
            return await self.sim_broker.get_equity_positions(acc_num)

        elif name == "get_option_positions":
            return await self.sim_broker.get_option_positions(acc_num)

        elif name == "get_equity_tax_lots":
            return await self.sim_broker.get_equity_tax_lots(
                acc_num, arguments.get("symbol") or arguments.get("ticker")
            )

        elif name in ("place_stock_order", "place_equity_order"):
            return await self.sim_broker.place_equity_order(arguments)

        elif name in ("review_stock_order", "review_equity_order"):
            return await self.sim_broker.review_equity_order(arguments)

        elif name == "place_option_order":
            return await self.sim_broker.place_option_order(arguments)

        elif name == "review_option_order":
            return await self.sim_broker.review_option_order(arguments)

        elif name in ("cancel_order", "cancel_equity_order", "cancel_option_order"):
            order_id = arguments.get("order_id") or arguments.get("id")
            if not order_id:
                raise ValueError("order_id or id is required to cancel an order")
            return await self.sim_broker.cancel_order(order_id)

        elif name in ("get_orders", "get_equity_orders", "get_option_orders"):
            return await self.sim_broker.get_orders(arguments)

        elif name == "get_order_status":
            order_id = arguments.get("order_id") or arguments.get("id")
            if not order_id:
                raise ValueError("order_id or id is required to get order status")
            return await self.sim_broker.get_order_status(order_id)

        else:
            raise NotImplementedError(f"SimBroker does not support tool: {name}")
