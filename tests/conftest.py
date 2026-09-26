"""Shared test fixtures for the EvoTrader test suite."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import itertools
import shutil
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import pytest_asyncio
import yaml

from evotrader import paths
from evotrader.db.connection import Database
from evotrader.models.config import Constitution, Settings
from evotrader.models.market import (
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)


@pytest.fixture(autouse=True)
def _isolate_process_wide_asset_state():
    """Reset process-wide asset bindings between tests.

    ``bind_asset_context`` and ``bind_extended_hours_tickers`` are module-level
    globals set once at startup. Any test that exercises ``main.start()`` leaves
    them bound, which silently changed the answer for every later test that asked
    "is this ticker 24-hour eligible?". Isolating them here keeps that a
    one-time startup concern rather than a cross-test dependency.
    """
    from evotrader.tools.asset_context import reset_asset_context
    from evotrader.tools.market_hours import bind_extended_hours_tickers

    reset_asset_context()
    bind_extended_hours_tickers(None)
    yield
    reset_asset_context()
    bind_extended_hours_tickers(None)


@pytest.fixture(autouse=True, scope="session")
def _signin_folder_is_temporary(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Keep every test away from the real broker sign-in in ~/.evotrader.

    Building the broker connection creates the sign-in folder and logs to it.
    With the real one, a test run wrote into the user's home folder, and a
    test could have read their real token.
    """
    folder = tmp_path_factory.mktemp("signin")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(paths.SIGNIN_DIR_ENV, str(folder))
        yield folder


# ---------------------------------------------------------------------------
# The data folder: every test gets its own copy of starter_data/
# ---------------------------------------------------------------------------
#
# The data folder holds a user's settings, instructions, algorithm versions,
# journal and keys. The tests must never read it: on a stranger's clone or in
# CI there is none, and on the owner's machine it holds live values the tests
# would silently come to depend on. So every test runs with
# EVOTRADER_DATA_DIR pointing at a private copy of starter_data/, the files a
# new user starts from. Anything that asks for the data folder — AppConfig(),
# paths.data_dir(), the strategy manifest, the scenario runner — gets that copy.

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_folder_numbers = itertools.count()


def _init_data_script() -> ModuleType:
    """``scripts/init_data.py``, the script that makes a user's data folder."""
    spec = importlib.util.spec_from_file_location(
        "_init_data_for_tests", _PROJECT_ROOT / "scripts" / "init_data.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def _starter_data_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """starter_data/ made into a data folder once per run, the way a new user makes theirs."""
    template = tmp_path_factory.mktemp("starter-data-template") / "data"
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        exit_code = _init_data_script().init_data(template)
    assert exit_code == 0, (
        f"starter_data/ does not make a data folder the app loads:\n{output.getvalue()}"
    )
    return template


class StarterDataFolder:
    """One test's data folder, copied from the template the first time it is asked for.

    Copying takes several milliseconds and most tests never touch the data
    folder, so copying up front for each of the ~1,800 tests would add over ten
    seconds to every run.
    """

    def __init__(self, template: Path, path: Path) -> None:
        self.template = template
        self.path = path

    def ensure(self) -> Path:
        if not self.path.exists():
            shutil.copytree(self.template, self.path)
        return self.path


@pytest.fixture(scope="session")
def _data_folders(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("data-folders").resolve()


@pytest.fixture(autouse=True)
def _data_folder_is_starter_data(
    monkeypatch: pytest.MonkeyPatch, _starter_data_template: Path, _data_folders: Path
) -> StarterDataFolder:
    """Point EVOTRADER_DATA_DIR at this test's own copy of starter_data/.

    The copy is made when ``paths.data_dir()`` first names it (``AppConfig()``
    asks through it too). A test that reads the files itself takes the
    ``starter_data_dir`` fixture instead. Tests that set EVOTRADER_DATA_DIR or
    pass ``AppConfig(data_dir=...)`` themselves still get exactly what they ask
    for.
    """
    folder = StarterDataFolder(_starter_data_template, _data_folders / str(next(_folder_numbers)))
    real_data_dir = paths.data_dir

    def data_dir(root: Path | None = None) -> Path:
        found = real_data_dir(root)
        if found == folder.path:
            folder.ensure()
        return found

    monkeypatch.setenv(paths.DATA_DIR_ENV, str(folder.path))
    monkeypatch.setattr(paths, "data_dir", data_dir)
    return folder


@pytest.fixture
def starter_data_dir(_data_folder_is_starter_data: StarterDataFolder) -> Path:
    """This test's data folder: a fresh copy of starter_data/, which EVOTRADER_DATA_DIR names.

    Change files in it freely; the next test gets a new copy.
    """
    return _data_folder_is_starter_data.ensure()


def _merge(into: dict[str, Any], values: dict[str, Any]) -> None:
    for key, value in values.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            _merge(into[key], value)
        else:
            into[key] = value


@pytest.fixture
def update_data_yaml(starter_data_dir: Path) -> Callable[[str, dict[str, Any]], Path]:
    """Change some values in a YAML file of this test's data folder and keep the rest.

    For a test that depends on particular settings or limits, so it states
    them itself instead of relying on whatever a data folder happens to hold::

        update_data_yaml("settings.yaml", {"position_sizing": {"volatility_target_annual": 0.6}})
        config = AppConfig()   # loads the changed file

    Nested mappings are merged key by key; any other value (a number, a list)
    replaces the one in the file. Returns the data folder.
    """

    def update(file_name: str, values: dict[str, Any]) -> Path:
        path = starter_data_dir / file_name
        content = yaml.safe_load(path.read_text()) or {}
        _merge(content, values)
        path.write_text(yaml.safe_dump(content, sort_keys=False))
        return starter_data_dir

    return update


@pytest.fixture
def active_algorithm_config(starter_data_dir: Path) -> dict[str, Any]:
    """The parameters (config.yaml) of the algorithm version a new user starts with."""
    algorithms = starter_data_dir / "algorithms"
    version = yaml.safe_load((algorithms / "active.yaml").read_text())["active_version"]
    return yaml.safe_load((algorithms / version / "config.yaml").read_text())


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """Create a temporary data directory with default config files."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # Write minimal constitution
    (data_dir / "constitution.yaml").write_text(
        "risk_limits:\n  max_order_value_usd: 1000\ntrading_rules:\n  allowed_tickers: ['SPY']\n"
    )

    # Write minimal settings
    (data_dir / "settings.yaml").write_text("mode: sim\n")

    return data_dir


@pytest.fixture
def constitution() -> Constitution:
    """Default constitution for testing."""
    return Constitution()


@pytest.fixture
def settings() -> Settings:
    """Default settings for testing."""
    return Settings()


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    """Initialised in-memory-like database for testing."""
    db_path = tmp_path / "test.db"
    database = Database(db_path)
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def sample_quote() -> Quote:
    """Sample SPY quote for testing."""
    return Quote(
        ticker="SPY",
        bid=450.00,
        ask=450.02,
        last=450.01,
        volume=50_000_000.0,
        timestamp=datetime(2026, 6, 18, 14, 30, 0),
    )


@pytest.fixture
def sample_indicators() -> TechnicalIndicators:
    """Sample technical indicators for testing."""
    return TechnicalIndicators(
        rsi_14=45.0,
        macd_line=0.5,
        macd_signal=0.3,
        macd_histogram=0.2,
        bollinger_upper=455.0,
        bollinger_middle=450.0,
        bollinger_lower=445.0,
        bollinger_width=0.022,
        ema_9=450.5,
        ema_21=449.8,
        sma_20=450.0,
        sma_50=448.0,
        vwap=450.10,
        ibs=0.55,
        atr_14=3.5,
        volume_sma_20=45_000_000.0,
        relative_volume=1.11,
    )


@pytest.fixture
def sample_regime() -> RegimeClassification:
    """Sample regime classification for testing."""
    return RegimeClassification(
        regime=MarketRegime.RANGE_BOUND,
        confidence=0.75,
        reasoning="ADX below 25, price oscillating around SMA-20",
        adx=22.0,
        trend_direction=0.01,
        volatility_percentile=45.0,
    )


@pytest.fixture
def sample_snapshot(
    sample_quote: Quote,
    sample_indicators: TechnicalIndicators,
    sample_regime: RegimeClassification,
) -> MarketSnapshot:
    """Complete market snapshot for testing."""
    return MarketSnapshot(
        ticker="SPY",
        timestamp=datetime(2026, 6, 18, 14, 30, 0),
        quote=sample_quote,
        indicators=sample_indicators,
        regime=sample_regime,
        daily_change_pct=0.3,
        gap_pct=0.2,
    )
