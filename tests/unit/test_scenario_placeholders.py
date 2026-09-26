"""The scenario harness must fill instruction placeholders the way the live factory does.

Found 2026-09-18 while measuring the strategy agent on the five ``hist_*``
snapshots: two of five responses flagged "prompt template placeholders
unsubstituted: DIRECTIONS_AVAILABLE, BEARISH_VEHICLE, ALLOWED_TICKERS,
PRIMARY_TICKER, SCHEDULE". The live path renders these from configuration
(``factory.load_instructions`` -> ``render_instructions``); the runner handed
the agent the RAW file. Both bearish reads (SHORT 0.15 on hist_large_advance
and hist_large_decline) were declined as "no placeable vehicle" — the harness,
not the instructions, blocked the short leg, and the measurement was biased
against it.
"""

from __future__ import annotations

import re

import pytest

from evotrader.scenarios import runner
from evotrader.scenarios.model import load_scenario, render_prompt

_PLACEHOLDER = re.compile(r"\{\{[A-Z_]+\}\}")
_HIST = "scenarios/12_hist_large_advance.yaml"


@pytest.fixture
def an_inverse_etf_is_configured(update_data_yaml) -> None:
    """settings.yaml trades SPY with SH, a -1x inverse ETF, as the bearish vehicle.

    The configured bearish vehicle then has text of its own ("it rises when SPY
    falls"), so a test can tell it apart from a scenario's.
    """
    update_data_yaml(
        "settings.yaml",
        {
            "asset": {
                "primary_ticker": "SPY",
                "inverse_ticker": "SH",
                "instruments": {
                    "SH": {
                        "description": "-1x inverse S&P 500 ETF; bearish vehicle only",
                        "role": "bearish vehicle",
                        "leverage": -1.0,
                    }
                },
            }
        },
    )
    update_data_yaml("constitution.yaml", {"trading_rules": {"allowed_tickers": ["SPY", "SH"]}})


def test_without_overrides_the_live_configuration_is_rendered(
    an_inverse_etf_is_configured,
) -> None:
    """The exact defect: the raw file reached the agent with ``{{...}}`` intact."""
    text = runner._load_instructions("strategy", None)
    assert not _PLACEHOLDER.findall(text), _PLACEHOLDER.findall(text)
    assert "it rises when SPY falls" in text, "settings.yaml asset.inverse_ticker must be rendered"


def test_rendered_scenario_prompt_has_no_unfilled_placeholders(starter_data_dir) -> None:
    scenario = load_scenario(starter_data_dir / _HIST)
    prompt = render_prompt(
        scenario, runner._load_instructions("strategy", None, scenario.placeholders)
    )
    assert not _PLACEHOLDER.findall(prompt), _PLACEHOLDER.findall(prompt)


def test_a_scenario_may_override_the_live_values(
    starter_data_dir, an_inverse_etf_is_configured
) -> None:
    """A QQQ-era snapshot is rendered with QQQ's bearish vehicle, not the configured one."""
    scenario = load_scenario(starter_data_dir / _HIST)
    assert scenario.placeholders["PRIMARY_TICKER"] == "QQQ"
    text = runner._load_instructions("strategy", None, scenario.placeholders)
    assert "it rises when QQQ falls" in text
    assert "it rises when SPY falls" not in text


def test_every_hist_scenario_carries_its_own_placeholders(starter_data_dir) -> None:
    hist = sorted((starter_data_dir / "scenarios").glob("1*_hist_*.yaml"))
    assert hist, "no historical scenarios found: the check below would pass on nothing"
    for path in hist:
        sc = load_scenario(path)
        assert {"PRIMARY_TICKER", "BEARISH_VEHICLE", "DIRECTIONS_AVAILABLE"} <= set(
            sc.placeholders
        ), path
