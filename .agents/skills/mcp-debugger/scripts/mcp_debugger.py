"""Inspect an MCP server's tools: list them, show one's input, or call a read-only one.

    uv run python .agents/skills/mcp-debugger/scripts/mcp_debugger.py list
    uv run python .agents/skills/mcp-debugger/scripts/mcp_debugger.py schema get_equity_quotes
    uv run python .agents/skills/mcp-debugger/scripts/mcp_debugger.py call get_equity_quotes '{"symbols": ["SPY"]}'
    uv run python .agents/skills/mcp-debugger/scripts/mcp_debugger.py dump

``dump`` writes every tool's full schema to ``<data folder>/mcp_tool_schemas.json``,
to compare with the tool definitions in ``src/evotrader/mcp/robinhood.py``.

Two things it refuses, both learned the hard way:

* Tools that place, change or cancel orders. Orders go through the app, where
  the constitution, the risk gate and the approval step check them; a direct
  call here would skip all three, on a real account.
* Running while EvoTrader runs. Both would use the one saved broker sign-in,
  and two programs refreshing it can invalidate it (the app then asks you to
  sign in again).
"""

import argparse
import asyncio
import json
import sys

from evotrader import paths
from evotrader.agents.factory import create_mcp_toolsets
from evotrader.callbacks.risk_gate import _GATED_TOOLS, is_unchecked_order_tool
from evotrader.config import AppConfig


def changes_orders(tool: str) -> bool:
    """True for a tool that places, changes or cancels an order."""
    name = tool.lower()
    return (
        tool in _GATED_TOOLS
        or is_unchecked_order_tool(tool)
        or ("order" in name and name.startswith(("cancel", "replace", "modify", "place", "submit")))
    )


async def main():
    parser = argparse.ArgumentParser(description="Inspect an MCP server's tools")
    parser.add_argument(
        "--provider",
        type=str,
        default="trading",
        help="The role/provider group to connect to (e.g., 'trading' or 'research')",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # List tools
    subparsers.add_parser("list", help="List all available tools")

    # Tool Schema
    schema_parser = subparsers.add_parser("schema", help="Get schema for a specific tool")
    schema_parser.add_argument("tool", type=str, help="The name of the tool")

    # Call tool
    call_parser = subparsers.add_parser("call", help="Call a read-only tool")
    call_parser.add_argument("tool", type=str, help="The name of the tool to call")
    call_parser.add_argument("args", type=str, help="JSON string representing the arguments")

    # Dump every schema
    subparsers.add_parser(
        "dump", help="Write every tool's full schema to <data folder>/mcp_tool_schemas.json"
    )

    args = parser.parse_args()

    if args.command == "call" and changes_orders(args.tool):
        sys.exit(
            f"'{args.tool}' places, changes or cancels orders: never from this script. "
            "Orders go through the app, where the risk checks and your approval apply."
        )
    holder = paths.hold(paths.signin_dir())
    if holder is not None:
        who = f" (process {holder})" if holder > 0 else ""
        sys.exit(
            f"EvoTrader is running{who} and uses the broker sign-in. Stop it first: two "
            "programs refreshing one sign-in can invalidate it."
        )

    config = AppConfig()
    mcp_toolsets = create_mcp_toolsets(config)

    if args.provider not in mcp_toolsets:
        print(f"Error: Provider role '{args.provider}' not found in configuration.")
        print(f"Available provider roles: {list(mcp_toolsets.keys())}")
        sys.exit(1)

    toolset = mcp_toolsets[args.provider]
    if not toolset:
        print(f"Error: Provider role '{args.provider}' has no active toolsets.")
        sys.exit(1)

    try:
        session = await toolset[0]._mcp_session_manager.create_session()
    except Exception as e:
        print(f"Error creating session for provider '{args.provider}': {e}")
        sys.exit(1)

    try:
        tools = await session.list_tools()
    except Exception as e:
        print(f"Error listing tools: {e}")
        sys.exit(1)

    if args.command == "list":
        print(f"--- Tools available for provider '{args.provider}' ---")
        for idx, t in enumerate(tools.tools, 1):
            print(f"{idx}. {t.name} - {t.description}")

    elif args.command == "schema":
        tool = next((t for t in tools.tools if t.name == args.tool), None)
        if not tool:
            print(f"Error: Tool '{args.tool}' not found.")
            sys.exit(1)

        print(f"--- Schema for tool '{args.tool}' ---")
        print(f"Description: {tool.description}")
        print("Input Schema:")
        print(json.dumps(tool.inputSchema, indent=2))

    elif args.command == "dump":
        out = paths.data_dir() / "mcp_tool_schemas.json"
        schemas = [
            {"name": t.name, "description": t.description, "input_schema": t.inputSchema}
            for t in sorted(tools.tools, key=lambda t: t.name)
        ]
        out.write_text(json.dumps(schemas, indent=2, default=str), encoding="utf-8")
        print(f"Wrote {len(schemas)} tool schemas to {out}")

    elif args.command == "call":
        tool = next((t for t in tools.tools if t.name == args.tool), None)
        if not tool:
            print(f"Error: Tool '{args.tool}' not found.")
            sys.exit(1)

        try:
            call_args = json.loads(args.args)
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON arguments: {e}")
            sys.exit(1)

        try:
            print(f"Calling '{args.tool}' with args: {json.dumps(call_args)}")
            res = await session.call_tool(args.tool, call_args)
            if getattr(res, "isError", False):
                print("Tool execution returned error flag.")

            for content in res.content:
                if content.type == "text":
                    try:
                        # Try to format JSON if it's JSON text
                        parsed = json.loads(content.text)
                        print(json.dumps(parsed, indent=2))
                    except json.JSONDecodeError:
                        print(content.text)
                else:
                    print(content)
        except Exception as e:
            print(f"Error calling tool '{args.tool}': {e}")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
