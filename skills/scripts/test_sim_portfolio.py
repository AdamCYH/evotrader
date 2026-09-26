import asyncio
import json

from evotrader.agents.factory import create_mcp_toolsets
from evotrader.sim.sim_broker import SimBroker


async def main():
    mcp_toolsets = create_mcp_toolsets(None)
    mcp = (mcp_toolsets.get("trading") or [None])[0]
    broker = SimBroker(db_path="data/sim/db/sim_broker.db", real_mcp_toolset=mcp)
    port = await broker.get_portfolio("SIM_AGENT_TRADER")
    print(json.dumps(port, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
