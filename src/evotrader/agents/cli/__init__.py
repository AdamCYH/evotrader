"""Pluggable backends for agents hosted by a CLI harness rather than an API.

See :mod:`evotrader.agents.cli.types` for the vocabulary and
:mod:`evotrader.agents.cli.base` for the contract and registry.
"""

from evotrader.agents.cli.base import (
    CliAgentBackend,
    get_backend_class,
    load_backend,
    register_backend,
    registered_drivers,
)
from evotrader.agents.cli.types import (
    AgentEvent,
    AgentRequest,
    AgentResult,
    BillingMode,
    BuiltinCapability,
    EffortLevel,
    LocalTool,
    QuotaState,
    QuotaStatus,
    ThinkingSpec,
)

__all__ = [
    "AgentEvent",
    "AgentRequest",
    "AgentResult",
    "BillingMode",
    "BuiltinCapability",
    "CliAgentBackend",
    "EffortLevel",
    "LocalTool",
    "QuotaState",
    "QuotaStatus",
    "ThinkingSpec",
    "get_backend_class",
    "load_backend",
    "register_backend",
    "registered_drivers",
]
