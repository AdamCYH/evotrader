import asyncio

from evotrader.agents.factory import create_mcp_toolsets
from evotrader.config import AppConfig


async def main():
    config = AppConfig()
    mcp_toolsets = create_mcp_toolsets(config)
    trading = mcp_toolsets.get("trading")
    if not trading:
        return
    session = await trading[0]._mcp_session_manager.create_session()
    tools = await session.list_tools()
    for t in tools.tools:
        print(t.name)


if __name__ == "__main__":
    asyncio.run(main())
