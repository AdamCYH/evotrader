import json
import logging
from typing import Any, ClassVar

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

    ALLOWED_PASSTHROUGH_TOOLS: ClassVar[set[str]] = {
        # Read-only market data tools allowed in sim mode
        "get_equity_quotes",
        "get_equity_historicals",
        "get_option_chains",
        "get_option_instruments",
        "get_option_quotes",
        "get_equity_tradability",
        "get_option_tradability",
    }

    def __init__(self, sim_broker: Any, real_mcp_toolset: Any = None) -> None:
        self.sim_broker = sim_broker
        self.real_mcp_toolset = real_mcp_toolset

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
            # Swap sim account number with real account number for passthrough tools
            if arguments.get("account_number") == self.sim_broker.account_number:
                if getattr(self, "real_account_number", None) is None:
                    try:
                        real_accounts_res = await original_session.call_tool("get_accounts", {})
                        real_acc_data = json.loads(real_accounts_res.content[0].text)
                        items = real_acc_data.get("data", [])
                        if isinstance(items, list) and items:
                            self.real_account_number = next(
                                (
                                    a.get("account_number")
                                    for a in items
                                    if a.get("type", "").lower() == "agent"
                                ),
                                items[0].get("account_number"),
                            )
                    except Exception as e:
                        logger.warning("Failed to fetch real account number for passthrough: %s", e)

                if getattr(self, "real_account_number", None):
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
