"""Algorithm version registry.

Manages versioned algorithm configurations on disk. Supports:
- Loading the currently active algorithm version
- Promoting a new version to active
- Rolling back to a previous version
- Listing all available versions with metadata
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class AlgorithmRegistry:
    """Versioned algorithm configuration registry.

    Directory structure::

        data/algorithms/
        ├── registry.yaml      # Catalog of all versions
        ├── active.yaml        # Points to the currently active version
        └── v001_initial/
            ├── config.yaml    # Tunable parameters
            └── metadata.yaml  # Name, version, creation info

    Usage::

        registry = AlgorithmRegistry(data_dir / "algorithms")
        active = registry.get_active_version()
        params = registry.load_parameters(active)
    """

    def __init__(self, algorithms_dir: Path, is_sim: bool = False) -> None:
        self._dir = algorithms_dir
        self._is_sim = is_sim
        self._dir.mkdir(parents=True, exist_ok=True)
        self._ensure_defaults()

    def get_active_version(self) -> str:
        """Return the currently active algorithm version identifier."""
        active_path = self._dir / "active.yaml"
        if not active_path.is_file():
            return "v001_initial"
        data = self._load_yaml(active_path)
        return str(data.get("active_version", "v001_initial"))

    def set_active_version(self, version: str) -> None:
        """Promote a version to active.

        Args:
            version: Version identifier (e.g., 'v002_evolved_rsi').

        Raises:
            ValueError: If the version directory doesn't exist.
        """
        version_dir = self._dir / version
        if not version_dir.is_dir():
            raise ValueError(f"Algorithm version '{version}' not found at {version_dir}")
        self._save_yaml(self._dir / "active.yaml", {"active_version": version})

        # Mark metadata status as 'active' so it is no longer flagged as 'proposed'
        metadata_path = version_dir / "metadata.yaml"
        if metadata_path.is_file():
            try:
                meta = self._load_yaml(metadata_path)
                meta["status"] = "active"
                self._save_yaml(metadata_path, meta)
            except Exception as e:
                logger.warning("Failed to update metadata status for %s: %s", version, e)

        logger.info("Active algorithm version set to: %s", version)

    def load_parameters(self, version: str | None = None) -> dict[str, Any]:
        """Load algorithm parameters for a given version.

        Args:
            version: Version identifier. Defaults to active version.

        Returns:
            Dictionary of algorithm parameters.
        """
        if version is None:
            version = self.get_active_version()
        config_path = self._dir / version / "config.yaml"
        if not config_path.is_file():
            logger.warning("No config.yaml for version %s, using empty params", version)
            return {}
        return self._load_yaml(config_path)

    def load_metadata(self, version: str | None = None) -> dict[str, Any]:
        """Load algorithm metadata for a given version."""
        if version is None:
            version = self.get_active_version()
        metadata_path = self._dir / version / "metadata.yaml"
        if not metadata_path.is_file():
            return {"name": version, "version": version}
        return self._load_yaml(metadata_path)

    def _next_version(self) -> str:
        """Generate the next auto-incremented version prefix."""
        import re

        max_num = 0
        if self._dir.is_dir():
            for p in self._dir.iterdir():
                if p.is_dir():
                    match = re.match(r"^v(\d+)", p.name)
                    if match:
                        max_num = max(max_num, int(match.group(1)))
        return f"v{max_num + 1:03d}"

    def save_version(
        self,
        version: str,
        parameters: dict[str, Any],
        metadata: dict[str, Any],
    ) -> Path:
        """Save a new algorithm version to disk with auto-increment and lineage tracking.

        Args:
            version: Version identifier (e.g., 'v002_evolved_rsi' or 'auto').
            parameters: Algorithm parameters to save.
            metadata: Version metadata (name, description, created_by, etc.).

        Returns:
            Path to the created version directory.
        """
        import re

        # 1. Automatic lineage tracking
        if "parent_version" not in metadata:
            try:
                metadata["parent_version"] = self.get_active_version()
            except Exception:
                metadata["parent_version"] = None

        # 2. Auto-increment and version name correction
        if not version or version == "auto":
            version = self._next_version()
        else:
            # Parse prefix and descriptive text
            match = re.match(r"^v(\d+)(.*)$", version)
            next_prefix = self._next_version()
            next_num = int(re.match(r"^v(\d+)", next_prefix).group(1))
            suffix = ""

            if match:
                num = int(match.group(1))
                desc = match.group(2)
                # If proposed version number is already used or less than next, bump it
                if num < next_num:
                    if desc.startswith("_"):
                        desc = desc[1:]
                    if desc.endswith("_sim"):
                        desc = desc[:-4]
                    if desc:
                        version = f"v{next_num:03d}_{desc}{suffix}"
                    else:
                        version = f"v{next_num:03d}{suffix}"
            else:
                desc = version
                if desc.endswith("_sim"):
                    desc = desc[:-4]
                version = f"{next_prefix}_{desc}"

        version_dir = self._dir / version
        version_dir.mkdir(parents=True, exist_ok=True)
        metadata["is_sim"] = False

        metadata["version"] = version

        self._save_yaml(version_dir / "config.yaml", parameters)
        self._save_yaml(version_dir / "metadata.yaml", metadata)
        self._update_registry(version, metadata)
        logger.info(
            "Algorithm version saved: %s (parent=%s)", version, metadata.get("parent_version")
        )
        return version_dir

    def list_versions(self) -> list[dict[str, Any]]:
        """List all available algorithm versions with metadata."""
        registry_path = self._dir / "registry.yaml"
        if not registry_path.is_file():
            return []
        data = self._load_yaml(registry_path)
        versions = data.get("versions", [])

        # All versions are visible to both modes
        active = self.get_active_version()
        for v in versions:
            v["is_active"] = v.get("version") == active
        return versions

    def rollback(self, to_version: str) -> None:
        """Rollback to a previous algorithm version.

        This is a shortcut for ``set_active_version`` with additional
        logging for the Evolution Agent's audit trail.
        """
        current = self.get_active_version()
        self.set_active_version(to_version)
        logger.warning(
            "Algorithm ROLLED BACK from %s to %s",
            current,
            to_version,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_defaults(self) -> None:
        """Create the default v001_initial version if no versions exist."""
        initial_dir = self._dir / "v001_initial"
        if initial_dir.is_dir():
            return

        default_params = {
            "momentum": {
                "ema_short": 9,
                "ema_long": 21,
                "sma_short": 20,
                "sma_long": 50,
                "volume_confirmation": True,
            },
            "mean_reversion": {
                "rsi_period": 14,
                "rsi_oversold": 30,
                "rsi_overbought": 70,
                "bollinger_period": 20,
                "bollinger_std": 2.0,
                "ibs_oversold": 0.2,
                "ibs_overbought": 0.8,
            },
            "gap": {
                "min_gap_pct": 0.3,
                "gap_fade_threshold": 1.0,
                "opening_range_minutes": 15,
            },
            "composite": {
                "momentum_weight": 0.35,
                "mean_reversion_weight": 0.40,
                "gap_weight": 0.25,
            },
        }

        default_metadata = {
            "name": "Initial Composite Strategy",
            "version": "v001_initial",
            "description": (
                "Ensemble of momentum, mean reversion, and gap strategies "
                "with rule-based regime detection and default parameters."
            ),
            "created_by": "system",
            "created_at": "2026-06-18",
            "is_sim": False,
        }

        self.save_version("v001_initial", default_params, default_metadata)
        self.set_active_version("v001_initial")
        logger.info("Default algorithm version v001_initial created")

    def _update_registry(self, version: str, metadata: dict[str, Any]) -> None:
        """Add or update a version entry in the registry catalog."""
        registry_path = self._dir / "registry.yaml"
        data = self._load_yaml(registry_path) if registry_path.is_file() else {}
        versions: list[dict[str, Any]] = data.get("versions", [])

        # Update existing entry or add new
        entry = {
            "version": version,
            "name": metadata.get("name", version),
            "description": metadata.get("description", ""),
            "created_by": metadata.get("created_by", "unknown"),
            "created_at": metadata.get("created_at", ""),
            "is_sim": metadata.get("is_sim", self._is_sim),
        }
        existing_idx = next(
            (i for i, v in enumerate(versions) if v.get("version") == version),
            None,
        )
        if existing_idx is not None:
            versions[existing_idx] = entry
        else:
            versions.append(entry)

        data["versions"] = versions
        self._save_yaml(registry_path, data)

    @staticmethod
    def _load_yaml(path: Path) -> dict[str, Any]:
        with open(path) as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _save_yaml(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
