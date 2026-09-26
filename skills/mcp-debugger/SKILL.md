---
name: mcp-debugger
description: Use this skill to inspect, list, and test MCP tools and their schemas to interact with the underlying MCP servers.
---

# MCP Debugger Skill

This skill provides a helper script that allows you to easily connect to the underlying Model Context Protocol (MCP) servers (e.g. Robinhood, AlphaVantage) to list available tools, view their required JSON schemas, and test calling them directly.

This avoids the need to write one-off Python scripts to explore the MCP environments.

## How to use

Run the provided Python script located at `skills/mcp-debugger/scripts/mcp_debugger.py`.

The script accepts a `--provider` argument (default is `trading`) which corresponds to the provider group configured in the workspace (e.g., `trading`, `research`).

### 1. List Available Tools
To see all available tools exposed by the MCP server:
```bash
.venv/bin/python skills/mcp-debugger/scripts/mcp_debugger.py --provider trading list
```

### 2. View Tool Schema
To view the description and expected JSON input schema for a specific tool (e.g. `get_option_chains`):
```bash
.venv/bin/python skills/mcp-debugger/scripts/mcp_debugger.py --provider trading schema get_option_chains
```

### 3. Call a Tool
To execute a tool and see its raw JSON response, provide the tool name and a JSON string of arguments:
```bash
.venv/bin/python skills/mcp-debugger/scripts/mcp_debugger.py --provider trading call get_option_chains '{"symbol": "QQQ"}'
```

**Note:** Always ensure that your arguments exactly match the tool's expected schema, which you can check using the `schema` command.
