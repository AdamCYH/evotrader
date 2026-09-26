---
name: mcp-debugger
description: Use this skill to inspect, list, and test MCP tools and their schemas to interact with the underlying MCP servers.
---

# MCP Debugger Skill

This skill provides a helper script that allows you to easily connect to the underlying Model Context Protocol (MCP) servers (e.g. Robinhood, AlphaVantage) to list available tools, view their required JSON schemas, and test calling them directly.

This avoids the need to write one-off Python scripts to explore the MCP environments.

## How to use

Run it from the project folder, with the app **stopped** (it refuses otherwise:
two programs refreshing one broker sign-in can invalidate it). `--provider`
picks the provider group from the settings (default `trading`; also `research`).

```bash
uv run python .agents/skills/mcp-debugger/scripts/mcp_debugger.py list
uv run python .agents/skills/mcp-debugger/scripts/mcp_debugger.py schema get_option_chains
uv run python .agents/skills/mcp-debugger/scripts/mcp_debugger.py call get_equity_quotes '{"symbols": ["SPY"]}'
uv run python .agents/skills/mcp-debugger/scripts/mcp_debugger.py dump
```

- `list`: every tool the server offers.
- `schema TOOL`: one tool's description and the input it expects.
- `call TOOL JSON`: call a **read-only** tool and print its raw answer. Tools
  that place, change or cancel orders are refused: orders go through the app,
  where the constitution, the risk gate and your approval check them.
- `dump`: write every tool's full schema to `<data folder>/mcp_tool_schemas.json`,
  to compare with the definitions in `src/evotrader/mcp/robinhood.py`.

Check a tool's `schema` before a `call`: the arguments must match it exactly.
