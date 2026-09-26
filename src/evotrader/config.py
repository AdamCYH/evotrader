"""Configuration loading and validation.

Loads ``constitution.yaml`` (inviolable risk rules) and ``settings.yaml``
(hot-reloadable system configuration) from the data directory. Validates
all values against Pydantic schemas on startup.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from dotenv import dotenv_values

from evotrader import paths
from evotrader.models.config import Constitution, Settings, TradingMode

logger = logging.getLogger(__name__)


class AppConfig:
    """Centralised application configuration.

    Holds both the immutable ``constitution`` and the mutable ``settings``,
    along with resolved filesystem paths for all runtime data directories.
    """

    def __init__(
        self,
        project_root: Path | None = None,
        data_dir: Path | None = None,
        mode_override: TradingMode | str | None = None,
    ) -> None:
        self.project_root = project_root or _find_project_root()
        self.data_dir = data_dir or paths.data_dir(self.project_root)
        self._mode_override = mode_override

        # Load environment variables from .env files
        _load_env_files(self.data_dir, self.project_root)

        # Load and validate configuration files
        self.constitution = _load_constitution(self.data_dir)
        self.settings = _load_settings(self.data_dir)

        # Enforce overrides and safety checks
        import os

        if os.environ.get("EVOTRADER_MOCK_TIME"):
            if self.settings.mode != TradingMode.SIM:
                logger.warning(
                    "⚠️ Simulation time override active (EVOTRADER_MOCK_TIME) but "
                    "mode was set to live. Forcing mode to sim for safety."
                )
            self.settings.mode = TradingMode.SIM
        elif self._mode_override is not None:
            self.settings.mode = TradingMode(self._mode_override)

        # Resolve key subdirectory paths
        self._resolve_directories()

        # Ensure runtime directories exist
        self._ensure_directories()

        logger.info(
            "Configuration loaded — data_dir=%s, mode=%s, ticker=%s",
            self.data_dir,
            self.settings.mode.value,
            self.settings.asset.primary_ticker,
        )

    def reload_settings(self) -> None:
        """Reload settings.yaml without restarting the application.

        The constitution is never reloaded at runtime — it requires a restart.
        """
        self.settings = _load_settings(self.data_dir)
        import os

        if os.environ.get("EVOTRADER_MOCK_TIME"):
            self.settings.mode = TradingMode.SIM
        elif self._mode_override is not None:
            self.settings.mode = TradingMode(self._mode_override)
        self._resolve_directories()
        self._ensure_directories()
        logger.info("Settings reloaded from %s", self.data_dir / "settings.yaml")

    def _resolve_directories(self) -> None:
        """Resolve all runtime directories based on active mode."""
        # Shared configuration/instructions/notes/algorithms directories
        self.algorithms_dir = self.data_dir / "algorithms"
        self.instructions_dir = self.data_dir / "instructions"
        self.notes_dir = self.data_dir / "notes"

        # Separate runtime directories for DB, sessions, and memory
        from evotrader.models.config import TradingMode

        if self.settings.mode == TradingMode.SIM:
            sim_base = self.data_dir / "sim"
            self.db_dir = sim_base / "db"
            self.sessions_dir = sim_base / "sessions"
            self.memory_dir = sim_base / "memory"
        else:
            self.db_dir = self.data_dir / "db"
            self.sessions_dir = self.data_dir / "sessions"
            self.memory_dir = self.data_dir / "memory"

    @property
    def db_path(self) -> Path:
        """Path to the SQLite database file."""
        if self.settings.dry_run.enabled:
            return self.db_dir / "evotrader_sim.db"
        return self.db_dir / "evotrader.db"

    def _ensure_directories(self) -> None:
        """Create runtime directories if they don't exist."""
        for directory in (
            self.db_dir,
            self.sessions_dir,
            self.memory_dir,
            self.algorithms_dir,
            self.instructions_dir,
            self.notes_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_project_root() -> Path:
    """The project root (the directory with pyproject.toml); see :mod:`evotrader.paths`."""
    return paths.project_root()


# Values this process took from .env files. A later load may update them (a
# file can change while the app runs) without mistaking them for the shell's.
_from_env_files: dict[str, str] = {}
_warned_shadowed: set[str] = set()


def _load_env_files(data_dir: Path, project_root: Path) -> None:
    """Load the data folder's .env, then the project's — never over the environment.

    A variable already set in the environment (by your shell, or by a
    command-line flag such as --mock-time) wins over both files, as with most
    tools; the data folder's .env wins over the project's. An empty value
    (``KEY=``, as in .env.example) means "not set".

    Until 2026-09-26 the data folder's .env overrode the environment, so
    ``EVOTRADER_PORT=8114 ./run.sh`` was silently ignored when the file named a
    port, and the example file's empty ``EVOTRADER_MOCK_TIME=`` cancelled
    ``--mock-time``.
    """
    import os

    taken: set[str] = set()
    for env_file in (data_dir / ".env", project_root / ".env"):
        if not env_file.is_file():
            continue
        for key, value in dotenv_values(env_file).items():
            if not value or key in taken:
                continue
            taken.add(key)
            current = os.environ.get(key)
            if current and current != _from_env_files.get(key):
                if current != value and key not in _warned_shadowed:
                    _warned_shadowed.add(key)
                    logger.warning(
                        "%s is set in your environment and, differently, in %s: using the "
                        "environment's value.",
                        key,
                        env_file,
                    )
                continue
            os.environ[key] = value
            _from_env_files[key] = value
        logger.debug("Loaded environment from %s", env_file)


def _load_yaml(path: Path) -> dict:  # type: ignore[type-arg]
    """Load and parse a YAML file, returning an empty dict if the file is missing."""
    if not path.is_file():
        logger.warning("Configuration file not found: %s — using defaults", path)
        return {}
    with open(path) as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def _load_constitution(data_dir: Path) -> Constitution:
    """Load and validate the constitution (inviolable risk rules)."""
    path = data_dir / "constitution.yaml"
    raw = _load_yaml(path)
    constitution = Constitution.model_validate(raw)
    logger.info(
        "Constitution loaded — max_daily_loss=%.1f%%, max_drawdown=%.1f%%, "
        "short_sell=%s, tickers=%s",
        constitution.risk_limits.max_daily_loss_pct * 100,
        constitution.risk_limits.max_drawdown_pct * 100,
        constitution.trading_rules.allow_short_sell,
        constitution.trading_rules.allowed_tickers,
    )
    return constitution


def _load_settings(data_dir: Path) -> Settings:
    """Load and validate system settings (hot-reloadable)."""
    path = data_dir / "settings.yaml"
    raw = _load_yaml(path)
    settings = Settings.model_validate(raw)
    logger.info(
        "Settings loaded — roles=%s, regime_detection=%s, dry_run=%s, evolution_cron=%s",
        dict(settings.mcp.roles),
        settings.strategy.regime_detection.value,
        settings.dry_run.enabled,
        settings.schedule.evolution_cron,
    )
    return settings
