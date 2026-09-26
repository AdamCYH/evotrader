"""Tests for configuration loading and Pydantic model validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from evotrader.config import AppConfig
from evotrader.models.config import (
    Constitution,
    RegimeWeightEntry,
    Settings,
    StrategyConfig,
)


class TestConstitution:
    """Tests for the Constitution model."""

    def test_default_values(self) -> None:
        c = Constitution()
        assert c.risk_limits.max_single_position_pct == 0.10
        assert c.risk_limits.max_daily_loss_pct == 0.02
        assert c.trading_rules.allow_short_sell is True
        assert c.trading_rules.allow_margin is False
        assert c.circuit_breakers.consecutive_losses_pause == 5

    def test_custom_values(self) -> None:
        c = Constitution.model_validate(
            {
                "risk_limits": {"max_order_value_usd": 10000},
                "trading_rules": {"allowed_tickers": ["SPY", "QQQ"]},
            }
        )
        assert c.risk_limits.max_order_value_usd == 10000
        assert c.trading_rules.allowed_tickers == ["SPY", "QQQ"]
        # Defaults still apply for unset fields
        assert c.risk_limits.max_single_position_pct == 0.10

    def test_extended_hours_defaults(self) -> None:
        c = Constitution()
        assert c.trading_rules.allow_extended_hours is True
        assert c.trading_rules.extended_hours_tickers == ["SPY"]

    def test_extended_hours_custom_default(self) -> None:
        c = Constitution.model_validate({"trading_rules": {"allowed_tickers": ["QQQ"]}})
        assert c.trading_rules.allow_extended_hours is True
        assert c.trading_rules.extended_hours_tickers == ["QQQ"]

    def test_validation_rejects_invalid_pct(self) -> None:
        with pytest.raises(Exception):
            Constitution.model_validate(
                {
                    "risk_limits": {"max_single_position_pct": 101}  # > 100%
                }
            )

    def test_to_llm_display_dict_pct_fields_are_percent_strings(self) -> None:
        """_pct fields must be shown as 'N%' strings for LLM consumption.

        This prevents the documented bug where model_dump_json() exposed
        post-normalization fractions (1.0 = 100%) that models misread as
        '1% hard caps'.
        """
        c = Constitution.model_validate(
            {
                "risk_limits": {
                    "max_single_position_pct": 100,  # YAML: 100 → internal: 1.0
                    "max_daily_loss_pct": 5,  # YAML: 5 → internal: 0.05
                    "max_stop_loss_pct": 2.5,  # YAML: 2.5 → internal: 0.025
                }
            }
        )
        display = c.to_llm_display_dict()
        rl = display["risk_limits"]
        assert rl["max_single_position_pct"] == "100%"
        assert rl["max_daily_loss_pct"] == "5%"
        assert rl["max_stop_loss_pct"] == "2.5%"  # 2.5 → 0.025 → 2.5%

    def test_to_llm_display_dict_non_pct_fields_unchanged(self) -> None:
        """Non-_pct fields should pass through unchanged."""
        c = Constitution()
        display = c.to_llm_display_dict()
        rl = display["risk_limits"]
        assert rl["max_order_value_usd"] == 5000.0
        assert rl["require_stop_loss"] is True

    def test_to_llm_display_dict_nested_pct_fields(self) -> None:
        """_pct fields in nested models (level_ii) are also converted."""
        c = Constitution.model_validate({"level_ii": {"thin_book_warn_pct": 50}})
        display = c.to_llm_display_dict()
        assert display["level_ii"]["thin_book_warn_pct"] == "50%"


class TestSettings:
    """Tests for the Settings model."""

    def test_default_values(self) -> None:
        from evotrader.models.config import TradingMode

        s = Settings()
        assert s.asset.primary_ticker == "SPY"
        assert s.mode == TradingMode.SIM
        assert s.dry_run.enabled is True
        assert "robinhood_official" in s.mcp.providers
        assert s.require_trade_approval is True

    def test_custom_values(self) -> None:
        s = Settings.model_validate(
            {
                "require_trade_approval": False,
                "portfolio_cache_seconds": 120,
                "dashboard_poll_interval_seconds": 5,
            }
        )
        assert s.require_trade_approval is False
        assert s.portfolio_cache_seconds == 120
        assert s.dashboard_poll_interval_seconds == 5

    def test_schedule_config(self) -> None:
        from evotrader.models.config import ScheduleConfig

        s = Settings()
        assert s.schedule.description == "Manual trigger only (on-demand via UI)"
        assert s.schedule.cycle_cron is None
        assert s.schedule.cycle_cron_enabled is False
        assert s.schedule.evolution_cron_enabled is False
        assert s.schedule.overnight_interval_seconds == 3600
        assert s.schedule.max_cycle_events == 100

        # Custom values validation
        custom_schedule = ScheduleConfig.model_validate(
            {
                "description": "Every hour during regular market hours",
                "cycle_cron": "0 9-16 * * 1-5",
                "cycle_cron_enabled": True,
                "evolution_cron_enabled": False,
                "overnight_interval_seconds": 1800,
                "max_cycle_events": 50,
            }
        )
        assert custom_schedule.description == "Every hour during regular market hours"
        assert custom_schedule.cycle_cron == "0 9-16 * * 1-5"
        assert custom_schedule.cycle_cron_enabled is True
        assert custom_schedule.evolution_cron_enabled is False
        assert custom_schedule.overnight_interval_seconds == 1800
        assert custom_schedule.max_cycle_events == 50

        # Validation fails if max_cycle_events is <= 0
        with pytest.raises(Exception):
            ScheduleConfig.model_validate(
                {
                    "max_cycle_events": 0,
                }
            )

    def test_regime_weights_must_sum_to_one(self) -> None:
        with pytest.raises(Exception):
            RegimeWeightEntry(algo=0.5, llm=0.3)  # Sums to 0.8

    def test_regime_weights_valid(self) -> None:
        entry = RegimeWeightEntry(algo=0.6, llm=0.4)
        assert entry.algo == 0.6
        assert entry.llm == 0.4

    def test_algo_weights_must_sum_to_one(self) -> None:
        with pytest.raises(Exception):
            StrategyConfig(
                algo_weights={
                    "rsi": 0.5,
                    "macd": 0.1,
                    # Sums to 0.6, not 1.0
                }
            )

    def test_active_mcp_provider(self) -> None:
        s = Settings()
        s.mcp.roles = {"trading": "robinhood_official"}
        provider = s.mcp.active_provider()
        assert "robinhood" in provider.url.lower()

    def test_model_for_agent_fallback(self) -> None:
        s = Settings()
        s.model.default = "gemini-3.5-flash"
        s.model.strategy_agent = "gemini-2.5-pro"

        assert s.model.for_agent("strategy_agent") == "gemini-2.5-pro"
        assert s.model.for_agent("orchestrator") == "gemini-3.5-flash"
        assert s.model.for_agent("nonexistent") == "gemini-3.5-flash"

    def test_dual_model_config_validation(self) -> None:
        from evotrader.models.config import DualModelConfig, ModelConfig

        # Nested dict should parse as DualModelConfig
        s = Settings.model_validate(
            {
                "model": {
                    "sim": {
                        "default": "gemini-3.5-flash",
                        "strategy_agent": "gemini-3.1-flash-lite",
                    },
                    "live": {
                        "default": "gemini-3.5-flash",
                        "strategy_agent": "anthropic/claude-opus-4-8",
                    },
                }
            }
        )
        assert isinstance(s.model, DualModelConfig)
        assert s.model.sim.strategy_agent == "gemini-3.1-flash-lite"
        assert s.model.live.strategy_agent == "anthropic/claude-opus-4-8"

        # Flat dict should parse as ModelConfig (backward compatibility)
        s_flat = Settings.model_validate(
            {
                "model": {
                    "default": "gemini-3.5-flash",
                    "strategy_agent": "anthropic/claude-opus-4-8",
                }
            }
        )
        assert isinstance(s_flat.model, ModelConfig)
        assert s_flat.model.strategy_agent == "anthropic/claude-opus-4-8"

    def test_active_model_resolution(self) -> None:
        from evotrader.models.config import TradingMode

        s = Settings.model_validate(
            {
                "mode": "sim",
                "model": {
                    "sim": {
                        "default": "gemini-3.5-flash",
                        "strategy_agent": "gemini-3.1-flash-lite",
                    },
                    "live": {
                        "default": "gemini-3.5-flash",
                        "strategy_agent": "anthropic/claude-opus-4-8",
                    },
                },
            }
        )
        # Mode is sim -> active_model should return sim config
        assert s.active_model.strategy_agent == "gemini-3.1-flash-lite"

        # Change mode to live -> active_model should return live config
        s.mode = TradingMode.LIVE
        assert s.active_model.strategy_agent == "anthropic/claude-opus-4-8"

        # Flat config should return the same config regardless of mode
        s_flat = Settings.model_validate(
            {
                "mode": "sim",
                "model": {
                    "default": "gemini-3.5-flash",
                    "strategy_agent": "anthropic/claude-opus-4-8",
                },
            }
        )
        assert s_flat.active_model.strategy_agent == "anthropic/claude-opus-4-8"
        s_flat.mode = TradingMode.LIVE
        assert s_flat.active_model.strategy_agent == "anthropic/claude-opus-4-8"


class TestMarketModels:
    """Tests for market data models."""

    def test_quote_properties(self, sample_quote) -> None:  # type: ignore[no-untyped-def]
        assert sample_quote.mid == pytest.approx(450.01)
        assert sample_quote.spread == pytest.approx(0.02)

    def test_snapshot_has_all_fields(self, sample_snapshot) -> None:  # type: ignore[no-untyped-def]
        assert sample_snapshot.ticker == "SPY"
        assert sample_snapshot.indicators.rsi_14 == 45.0
        assert sample_snapshot.regime.regime.value == "range_bound"


class TestAppConfig:
    """Tests for the AppConfig class and its environment overrides."""

    def test_app_config_dry_run_override(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Create an AppConfig with default settings (mode: sim)
        config = AppConfig(data_dir=tmp_data_dir)
        assert config.settings.mode == "sim"
        assert config.settings.dry_run.enabled is True

        # Now mock settings.yaml to have mode: live
        (tmp_data_dir / "settings.yaml").write_text("mode: live\n")

        # Instantiate AppConfig when EVOTRADER_MOCK_TIME is NOT set
        config2 = AppConfig(data_dir=tmp_data_dir)
        assert config2.settings.mode == "live"
        assert config2.settings.dry_run.enabled is False

        # Set EVOTRADER_MOCK_TIME and instantiate AppConfig - it should force mode to sim
        monkeypatch.setenv("EVOTRADER_MOCK_TIME", "2026-06-17T10:00:00-04:00")
        config3 = AppConfig(data_dir=tmp_data_dir)
        assert config3.settings.mode == "sim"
        assert config3.settings.dry_run.enabled is True

    def test_app_config_cli_overrides(self, tmp_data_dir: Path) -> None:
        # 1. Base settings file has mode: sim. Override with "live".
        (tmp_data_dir / "settings.yaml").write_text("mode: sim\n")
        config = AppConfig(data_dir=tmp_data_dir, mode_override="live")
        assert config.settings.mode == "live"

        # 2. Base settings file has mode: live. Override with "sim".
        (tmp_data_dir / "settings.yaml").write_text("mode: live\n")
        config2 = AppConfig(data_dir=tmp_data_dir, mode_override="sim")
        assert config2.settings.mode == "sim"

    def test_app_config_reload_preserves_override(self, tmp_data_dir: Path) -> None:
        (tmp_data_dir / "settings.yaml").write_text("mode: sim\n")
        config = AppConfig(data_dir=tmp_data_dir, mode_override="live")
        assert config.settings.mode == "live"

        # Reload settings. Base file still says mode: sim, but override should be preserved
        config.reload_settings()
        assert config.settings.mode == "live"

    def test_app_config_simulation_safety_takes_priority(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If simulation time is active, it must force mode=sim even if mode_override="live" was requested!
        monkeypatch.setenv("EVOTRADER_MOCK_TIME", "2026-06-17T10:00:00-04:00")
        (tmp_data_dir / "settings.yaml").write_text("mode: live\n")
        config = AppConfig(data_dir=tmp_data_dir, mode_override="live")
        assert config.settings.mode == "sim"

        # Reload should also keep it sim
        config.reload_settings()
        assert config.settings.mode == "sim"

    def test_app_config_directory_routing_and_seeding(self, tmp_path: Path) -> None:
        live_dir = tmp_path / "data"
        live_dir.mkdir()

        # Create dummy live directories
        (live_dir / "instructions").mkdir()
        (live_dir / "instructions" / "orchestrator").mkdir()
        (live_dir / "instructions" / "orchestrator" / "active.txt").write_text("v001")
        (live_dir / "instructions" / "orchestrator" / "v001.md").write_text(
            "# Base Orchestrator Rules"
        )

        (live_dir / "notes").mkdir()
        (live_dir / "notes" / "market_observations.md").write_text("# Observation")

        (live_dir / "algorithms").mkdir()
        (live_dir / "algorithms" / "active.yaml").write_text("active_version: v001_initial")

        # 1. Instantiate AppConfig in SIM mode
        (live_dir / "settings.yaml").write_text("mode: sim\n")
        config = AppConfig(data_dir=live_dir)

        # Verify paths are routed to shared data/ dirs
        assert config.instructions_dir == live_dir / "instructions"
        assert config.notes_dir == live_dir / "notes"
        assert config.algorithms_dir == live_dir / "algorithms"
        assert config.db_dir == live_dir / "sim" / "db"
        assert config.sessions_dir == live_dir / "sim" / "sessions"
        assert config.memory_dir == live_dir / "sim" / "memory"

        # 2. Instantiate AppConfig in LIVE mode
        (live_dir / "settings.yaml").write_text("mode: live\n")
        config_live = AppConfig(data_dir=live_dir)

        # Verify paths are routed to standard dirs
        assert config_live.instructions_dir == live_dir / "instructions"
        assert config_live.notes_dir == live_dir / "notes"
        assert config_live.algorithms_dir == live_dir / "algorithms"
        assert config_live.db_dir == live_dir / "db"
        assert config_live.sessions_dir == live_dir / "sessions"
        assert config_live.memory_dir == live_dir / "memory"
