import argparse
import asyncio
import json
import sys

from evotrader.agents.factory import create_mcp_toolsets
from evotrader.config import AppConfig


async def main():
    parser = argparse.ArgumentParser(description="MCP Debugger for AI Agents")
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
    call_parser = subparsers.add_parser("call", help="Call a specific tool")
    call_parser.add_argument("tool", type=str, help="The name of the tool to call")
    call_parser.add_argument("args", type=str, help="JSON string representing the arguments")

    args = parser.parse_args()

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
