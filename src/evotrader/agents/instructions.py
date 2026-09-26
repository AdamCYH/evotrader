"""File-based instruction loader for EvoTrader agents.

All agent system instructions live in ``data/instructions/{agent_name}/``
as versioned markdown files. The active version is determined by the
``active.txt`` pointer file in each agent's directory.

Directory layout::

    data/instructions/
    ├── orchestrator/
    │   ├── active.txt          # contains "v001"
    │   └── v001.md             # instruction content
    ├── strategy/
    │   ├── active.txt          # contains "v002" (after evolution)
    │   ├── v001.md
    │   └── v002.md
    └── ...

The Evolution Agent creates new versions over time and updates
``active.txt`` to point to them.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load(agent_name: str, instructions_dir: Path, is_sim: bool = False) -> str:
    """Load the active system instruction for an agent from disk.

    Reads ``instructions_dir/{agent_name}/active.txt`` to determine the
    active version, then loads ``{version}.md`` from the same directory.

    Args:
        agent_name: Agent identifier (e.g., ``'orchestrator'``, ``'strategy'``).
        instructions_dir: Path to the ``data/instructions/`` directory.
        is_sim: Unused (retained for backward compatibility).

    Returns:
        The instruction text.

    Raises:
        FileNotFoundError: If the agent directory, pointer file, or the
            versioned ``.md`` file is missing.
    """
    agent_dir = instructions_dir / agent_name

    if not agent_dir.is_dir():
        raise FileNotFoundError(
            f"No instruction directory for agent '{agent_name}' at {agent_dir}. "
            f"Expected: {agent_dir}/active.txt + {{version}}.md"
        )

    active_ptr = agent_dir / "active.txt"
    if not active_ptr.is_file():
        raise FileNotFoundError(
            f"No active.txt pointer for agent '{agent_name}' at {active_ptr}. "
            f"Create it with the version name (e.g., 'v001')."
        )

    version = active_ptr.read_text().strip()
    if not version:
        raise ValueError(
            f"Active pointer file for agent '{agent_name}' is empty. "
            f"It should contain a version name (e.g., 'v001')."
        )

    instruction_file = agent_dir / f"{version}.md"
    if not instruction_file.is_file():
        raise FileNotFoundError(
            f"Instruction file '{instruction_file.name}' not found for "
            f"agent '{agent_name}'. active pointer points to version '{version}' "
            f"but {instruction_file} does not exist."
        )

    text = instruction_file.read_text()
    logger.info(
        "Loaded instructions for '%s' from %s (%d chars, is_sim=%s)",
        agent_name,
        instruction_file,
        len(text),
        is_sim,
    )
    return text


def list_agents(instructions_dir: Path, is_sim: bool = False) -> list[str]:
    """Return names of all agents that have instruction directories.

    Args:
        instructions_dir: Path to the ``data/instructions/`` directory.
        is_sim: Unused (retained for backward compatibility).

    Returns:
        Sorted list of agent names.
    """
    if not instructions_dir.is_dir():
        return []

    agents = []
    for d in instructions_dir.iterdir():
        if d.is_dir():
            if (d / "active.txt").is_file():
                agents.append(d.name)
    return sorted(agents)


def get_active_version(agent_name: str, instructions_dir: Path, is_sim: bool = False) -> str:
    """Return the active version string for an agent.

    Args:
        agent_name: Agent identifier.
        instructions_dir: Path to the ``data/instructions/`` directory.
        is_sim: Unused (retained for backward compatibility).

    Returns:
        Version string (e.g., ``'v001'``).

    Raises:
        FileNotFoundError: If the pointer file is missing.
    """
    active_ptr = instructions_dir / agent_name / "active.txt"
    if not active_ptr.is_file():
        raise FileNotFoundError(f"No active.txt for agent '{agent_name}'")
    return active_ptr.read_text().strip()
