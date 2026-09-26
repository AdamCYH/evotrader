"""Discover all tools on the Robinhood MCP server using the MCP SDK session.

Usage:
    uv run python scripts/discover_mcp_tools.py
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


async def main():
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    from evotrader.mcp.oauth import create_oauth_httpx_factory

    server_url = "https://agent.robinhood.com/mcp/trading"
    httpx_factory = create_oauth_httpx_factory(server_url)

    async with (
        streamablehttp_client(
            url=server_url,
            httpx_client_factory=httpx_factory,
            timeout=60.0,
        ) as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()

        result = await session.list_tools()
        tools = result.tools

        print(f"\n{'=' * 70}")
        print(f"  ROBINHOOD MCP SERVER — {len(tools)} tools discovered")
        print(f"{'=' * 70}\n")

        # Categorize
        tool_data = []
        for tool in sorted(tools, key=lambda t: t.name):
            schema = tool.inputSchema or {}
            props = schema.get("properties", {})
            required = schema.get("required", [])
            additional = schema.get("additionalProperties", "not set")

            entry = {
                "name": tool.name,
                "description": (tool.description or "")[:120],
                "params": list(props.keys()),
                "required": required,
                "additionalProperties": additional,
                "full_schema": schema,
            }
            tool_data.append(entry)

            print(f"📦 {tool.name}")
            print(f"   {entry['description']}")
            print(f"   Params: {entry['params']}")
            print(f"   Required: {required}")
            if additional is not True and additional != "not set":
                print(f"   additionalProperties: {additional}")
            print()

        # Write full dump
        from evotrader import paths

        out_path = paths.data_dir() / "mcp_tool_schemas.json"
        with open(out_path, "w") as f:
            json.dump(tool_data, f, indent=2, default=str)
        print(f"✅ Full schemas written to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
