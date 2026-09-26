"""Instruction evolver — versioned management of agent instructions.

The Evolution Agent uses this to propose, validate, and apply changes
to any agent's system instructions. Changes are:
- Versioned as markdown files (v001.md, v002.md, ...)
- Diff-tracked for audit
- Rate-limited (max 1 change per agent per week by default)
- Rollback-capable
"""

from __future__ import annotations

import difflib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class InstructionEvolver:
    """Manages versioned agent instruction files.

    Directory structure per agent::

        data/instructions/
        ├── orchestrator/
        │   ├── active.txt        # Contains "v001"
        │   ├── v001.md           # Initial instructions
        │   └── v002.md           # Evolved instructions
        ├── strategy/
        │   ├── active.txt
        │   └── v001.md
        └── ...
    """

    # Agents whose instructions CANNOT be modified by evolution
    _PROTECTED_AGENTS = frozenset({"risk_manager"})

    def __init__(self, instructions_dir: Path, is_sim: bool = False) -> None:
        self._dir = instructions_dir
        self._is_sim = is_sim
        self._dir.mkdir(parents=True, exist_ok=True)

    def get_current_version(self, agent_name: str) -> str:
        """Get the active instruction version for an agent."""
        active_ptr = self._dir / agent_name / "active.txt"
        if active_ptr.is_file():
            return active_ptr.read_text().strip()
        return "v001"

    def get_instructions(self, agent_name: str, version: str | None = None) -> str:
        """Read instructions for an agent at a specific version."""
        if version is None:
            version = self.get_current_version(agent_name)

        path = self._dir / agent_name / f"{version}.md"
        if path.is_file():
            return path.read_text()

        raise FileNotFoundError(
            f"Instruction file not found: {path}. "
            f"Ensure data/instructions/{agent_name}/{version}.md exists."
        )

    def list_versions(self, agent_name: str) -> list[dict[str, Any]]:
        """List all instruction versions for an agent."""
        agent_dir = self._dir / agent_name
        if not agent_dir.is_dir():
            return []

        active = self.get_current_version(agent_name)
        versions = []
        for f in sorted(agent_dir.glob("v*.md")):
            version = f.stem

            # Load metadata to check if is_sim is set
            meta_path = agent_dir / f"{version}_metadata.yaml"
            is_sim_val = False
            if meta_path.is_file():
                try:
                    import yaml

                    with open(meta_path) as meta_f:
                        meta_data = yaml.safe_load(meta_f)
                    if isinstance(meta_data, dict):
                        is_sim_val = meta_data.get("is_sim", False)
                except Exception:
                    pass
            else:
                # Fallback to suffix check if no metadata exists
                is_sim_val = version.endswith("_sim")

            # All versions are visible in both live and sim modes

            versions.append(
                {
                    "version": version,
                    "is_active": version == active,
                    "size_bytes": f.stat().st_size,
                    "modified": datetime.fromtimestamp(f.stat().st_mtime, tz=UTC).isoformat(),
                    "is_sim": is_sim_val,
                }
            )
        return versions

    def propose_change(
        self,
        agent_name: str,
        new_instructions: str,
        reasoning: str,
        max_per_week: int | None = None,
    ) -> dict[str, Any]:
        """Propose a new instruction version.

        Validates the change, generates a diff, and saves the new version
        WITHOUT activating it.

        Args:
            agent_name: Target agent (e.g., 'strategy', 'orchestrator').
            new_instructions: The proposed new instruction text.
            reasoning: Why this change should improve the agent's behavior.
            max_per_week: Max instruction changes allowed per agent per week.
                If None, no rate limit is enforced.

        Returns:
            Dict with version info, diff summary, and status.
        """
        # Safety: protected agents
        if agent_name in self._PROTECTED_AGENTS:
            return {
                "status": "rejected",
                "reason": (
                    f"Agent '{agent_name}' instructions are protected. "
                    "Only human operators may modify risk_manager instructions."
                ),
            }

        # Rate limit: check recent proposals for this agent
        if max_per_week is not None:
            recent_count = self._count_recent_proposals(agent_name, days=7)
            if recent_count >= max_per_week:
                return {
                    "status": "rate_limited",
                    "reason": (
                        f"Rate limit reached: {recent_count} instruction change(s) "
                        f"proposed for '{agent_name}' in the last 7 days "
                        f"(max {max_per_week}/week from settings.yaml). "
                        "Wait for the window to expire or adjust "
                        "'max_instruction_changes_per_week' in settings.yaml."
                    ),
                }

        # Get current instructions
        current_version = self.get_current_version(agent_name)
        current_text = self.get_instructions(agent_name, current_version)

        # Validate: instructions must not be empty
        if not new_instructions.strip():
            return {"status": "rejected", "reason": "Instructions cannot be empty."}

        # Validate: instructions should be reasonable length
        if len(new_instructions) < 100:
            return {
                "status": "rejected",
                "reason": "Instructions too short (< 100 chars). Likely incomplete.",
            }

        # Generate new version number
        new_version = self._next_version(agent_name)

        # Generate diff
        diff_lines = list(
            difflib.unified_diff(
                current_text.splitlines(keepends=True),
                new_instructions.splitlines(keepends=True),
                fromfile=f"{agent_name}/{current_version}.md",
                tofile=f"{agent_name}/{new_version}.md",
            )
        )
        diff_text = "".join(diff_lines)

        # Save new version (but don't activate)
        agent_dir = self._dir / agent_name
        agent_dir.mkdir(parents=True, exist_ok=True)

        # Save current version if it doesn't exist on disk yet
        current_path = agent_dir / f"{current_version}.md"
        if not current_path.is_file():
            current_path.write_text(current_text)

        # Save new version
        new_path = agent_dir / f"{new_version}.md"
        new_path.write_text(new_instructions)

        # Save metadata
        metadata = {
            "version": new_version,
            "previous_version": current_version,
            "reasoning": reasoning,
            "proposed_at": datetime.now(UTC).isoformat(),
            "status": "proposed",
            "diff_summary": f"+{len([ln for ln in diff_lines if ln.startswith('+')])} "
            f"-{len([ln for ln in diff_lines if ln.startswith('-')])} lines",
            "is_sim": self._is_sim,
        }
        meta_path = agent_dir / f"{new_version}_metadata.yaml"
        import yaml

        with open(meta_path, "w") as f:
            yaml.dump(metadata, f, default_flow_style=False)

        logger.info(
            "Instruction change proposed: %s %s → %s",
            agent_name,
            current_version,
            new_version,
        )

        return {
            "status": "proposed",
            "agent": agent_name,
            "previous_version": current_version,
            "new_version": new_version,
            "diff": diff_text,
            "diff_summary": metadata["diff_summary"],
            "file_path": str(new_path),
        }

    def activate_version(self, agent_name: str, version: str) -> dict[str, Any]:
        """Activate a proposed instruction version.

        Args:
            agent_name: Target agent.
            version: Version to activate.
        """
        if agent_name in self._PROTECTED_AGENTS:
            return {"status": "rejected", "reason": "Protected agent."}

        version_path = self._dir / agent_name / f"{version}.md"
        if not version_path.is_file():
            return {"status": "rejected", "reason": f"Version {version} not found."}

        # Update active pointer
        active_ptr = self._dir / agent_name / "active.txt"
        active_ptr.write_text(version)

        # Update metadata status if metadata file exists
        meta_path = self._dir / agent_name / f"{version}_metadata.yaml"
        if meta_path.is_file():
            try:
                import yaml

                meta = yaml.safe_load(meta_path.read_text()) or {}
                meta["status"] = "active"
                with open(meta_path, "w") as f:
                    yaml.dump(meta, f, default_flow_style=False, sort_keys=False)
            except Exception as e:
                logger.warning("Failed to update metadata status for %s: %s", version, e)

        logger.info(
            "Instruction version activated: %s → %s (is_sim=%s)", agent_name, version, self._is_sim
        )
        return {"status": "activated", "agent": agent_name, "version": version}

    def rollback(self, agent_name: str, to_version: str) -> dict[str, Any]:
        """Rollback to a previous instruction version."""
        return self.activate_version(agent_name, to_version)

    def _count_recent_proposals(self, agent_name: str, days: int = 7) -> int:
        """Count instruction proposals for an agent within the last N days.

        Uses the *_metadata.yaml files' proposed_at timestamps to determine
        how many proposals were made recently.
        """
        import yaml as _yaml

        agent_dir = self._dir / agent_name
        if not agent_dir.is_dir():
            return 0

        from datetime import timedelta

        cutoff = datetime.now(UTC) - timedelta(days=days)
        count = 0
        for meta_path in agent_dir.glob("v*_metadata.yaml"):
            try:
                meta = _yaml.safe_load(meta_path.read_text())
                if not isinstance(meta, dict):
                    continue
                proposed_at = meta.get("proposed_at")
                if not proposed_at:
                    continue
                ts = datetime.fromisoformat(proposed_at)
                if ts >= cutoff:
                    count += 1
            except Exception:
                continue
        return count

    def _next_version(self, agent_name: str) -> str:
        """Generate the next version number for an agent."""
        import re

        agent_dir = self._dir / agent_name
        suffix = ""
        if not agent_dir.is_dir():
            return f"v001{suffix}"

        existing = sorted(agent_dir.glob("v*.md"))
        if not existing:
            return f"v001{suffix}"

        # Extract highest version number
        max_num = 0
        for f in existing:
            try:
                match = re.match(r"^v(\d+)", f.stem)
                if match:
                    num = int(match.group(1))
                    max_num = max(max_num, num)
            except Exception:
                pass

        return f"v{max_num + 1:03d}{suffix}"
