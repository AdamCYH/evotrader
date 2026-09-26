"""Custom tools exposed to ADK agents.

Each module defines one or more Python functions that are registered as
custom tools via ``LlmAgent(tools=[...])``. Tools receive typed inputs
and return structured dict outputs for reliable agent-tool interaction.
"""
