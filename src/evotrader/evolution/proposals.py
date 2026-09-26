"""Evolution proposal management system.

Proposals are stored on disk as Markdown files with YAML frontmatter.
"""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class ProposalType(Enum):
    PARAM_TUNE = "param_tune"  # Tune existing strategy parameters
    COMPOSITION_CHANGE = "composition"  # Change weights, add/remove strategies
    NEW_STRATEGY = "new_strategy"  # Propose a new strategy concept
    DEPRECATE_STRATEGY = "deprecate"  # Mark a strategy for removal


class ProposalManager:
    """Manages strategy evolution proposals written as Markdown with YAML frontmatter."""

    def __init__(self, proposals_dir: Path) -> None:
        self.proposals_dir = proposals_dir

    def create_proposal(self, frontmatter: dict[str, Any], body_markdown: str) -> Path:
        """Create a new proposal file on disk.

        Args:
            frontmatter: Machine-readable metadata (id, type, status, created_at, etc.)
            body_markdown: Free-form design document containing rationale and code snippets.

        Returns:
            Path to the created proposal file.
        """
        self.proposals_dir.mkdir(parents=True, exist_ok=True)
        proposal_id = frontmatter.get("proposal_id")
        if not proposal_id:
            raise ValueError("frontmatter must contain 'proposal_id'")

        path = self.proposals_dir / f"{proposal_id}.md"
        frontmatter_str = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)

        with open(path, "w") as f:
            f.write(f"---\n{frontmatter_str}---\n\n{body_markdown.strip()}\n")

        logger.info("Proposal created successfully at %s", path)
        return path

    def parse_proposal(self, path: Path) -> tuple[dict[str, Any], str]:
        """Parse a markdown proposal file into frontmatter dict and markdown body."""
        if not path.is_file():
            raise FileNotFoundError(f"Proposal file not found: {path}")

        with open(path) as f:
            content = f.read()

        parts = content.split("---", 2)
        if len(parts) >= 3:
            frontmatter_str = parts[1]
            body = parts[2]
            try:
                frontmatter = yaml.safe_load(frontmatter_str)
            except Exception as e:
                logger.error("Failed to parse YAML frontmatter in %s: %s", path, e)
                frontmatter = {}
        else:
            frontmatter = {}
            body = content

        return frontmatter if isinstance(frontmatter, dict) else {}, body.strip()

    def list_proposals(self, status: str | None = None) -> list[dict[str, Any]]:
        """List all proposals, optionally filtered by status."""
        if not self.proposals_dir.is_dir():
            return []

        proposals = []
        for path in self.proposals_dir.glob("*.md"):
            try:
                frontmatter, _ = self.parse_proposal(path)
                if frontmatter:
                    frontmatter["file_path"] = str(path)
                    if status is None or frontmatter.get("status") == status:
                        proposals.append(frontmatter)
            except Exception as e:
                logger.warning("Error reading proposal %s: %s", path, e)

        return sorted(proposals, key=lambda x: x.get("created_at", ""), reverse=True)

    def update_status(self, proposal_id: str, new_status: str) -> None:
        """Update the status metadata of a proposal."""
        path = self.proposals_dir / f"{proposal_id}.md"
        if not path.is_file():
            raise FileNotFoundError(f"Proposal '{proposal_id}' not found at {path}")

        frontmatter, body = self.parse_proposal(path)
        frontmatter["status"] = new_status

        self.create_proposal(frontmatter, body)
        logger.info("Updated proposal %s status to %s", proposal_id, new_status)
