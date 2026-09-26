"""The executor must not carry tool schemas it cannot use — nor lose ones it needs.

Measured on 2026-09-14: the execution agent, described in settings as the
"cheapest — mechanical tool calling" agent, was handed **114,338 tokens of tool
descriptions across 77 tools on every call** — 3.09M tokens that day, 58% of
every token the whole system moved. ``place_crypto_order`` alone is ~9,850
tokens, on an agent trading MSTR equity and options.

The filter is deliberately **exclusion-only**. The asymmetry matters: hiding too
little costs tokens, hiding too much leaves an agent silently unable to place a
trade. These tests pin both sides — that the waste goes, and that the order path
provably survives the real shipped configuration.
"""

from __future__ import annotations

import logging

from evotrader.models.config import ToolFilterConfig

# Tools the execution agent must be able to reach to do its job at all. If a
# future pattern hides one of these, that is a broken order path, not a saving.
ORDER_PATH = [
    "place_equity_order",
    "place_option_order",
    "review_equity_order",
    "review_option_order",
    "cancel_equity_order",
    "cancel_option_order",
    "get_equity_positions",
    "get_option_positions",
    "get_equity_orders",
    "get_option_orders",
    "get_equity_quotes",
    "get_option_quotes",
    "get_equity_price_book",
    "get_equity_tradability",
    "get_option_chains",
    "get_accounts",
    "get_portfolio",
    "get_equity_tax_lots",
    "exercise_option",
    "cancel_option_exercise",
    "record_trade",
    "get_market_status",
    "get_open_positions",
    "assess_order_book",
]

# The full Robinhood surface as measured live on 2026-09-14 (77 tools).
LIVE_TOOLS = [
    *ORDER_PATH,
    "add_option_to_watchlist",
    "add_to_watchlist",
    "cancel_crypto_order",
    "create_alert",
    "create_scan",
    "create_watchlist",
    "delete_alert",
    "follow_watchlist",
    "get_alert_log",
    "get_alerts",
    "get_crypto_account_onboarding_info",
    "get_crypto_orders",
    "get_crypto_positions",
    "get_crypto_quotes",
    "get_currency_pairs",
    "get_earnings_calendar",
    "get_earnings_results",
    "get_equity_fundamentals",
    "get_equity_historicals",
    "get_equity_news",
    "get_equity_technical_indicators",
    "get_financials",
    "get_index_historicals",
    "get_index_quotes",
    "get_indexes",
    "get_limited_margin_upgrade_info",
    "get_option_historicals",
    "get_option_instruments",
    "get_option_level_upgrade_info",
    "get_option_watchlist",
    "get_pnl_trade_history",
    "get_popular_watchlists",
    "get_realized_pnl",
    "get_scanner_filter_specs",
    "get_scans",
    "get_sec_filing",
    "get_sec_filing_facts",
    "get_sec_filing_facts_catalog",
    "get_sec_filing_index",
    "get_watchlist_items",
    "get_watchlists",
    "mark_alerts_read",
    "place_crypto_order",
    "preview_crypto_order",
    "remove_from_watchlist",
    "remove_option_from_watchlist",
    "run_scan",
    "search",
    "unfollow_watchlist",
    "update_alert",
    "update_scan_config",
    "update_scan_filters",
    "update_watchlist",
]


# ── The shipped configuration ─────────────────────────────────────


def _shipped_filter() -> ToolFilterConfig:
    import logging as _logging

    _logging.disable(_logging.WARNING)
    try:
        from evotrader.config import AppConfig

        return AppConfig().settings.mcp.tool_filter
    finally:
        _logging.disable(_logging.NOTSET)


def test_shipped_config_keeps_every_order_path_tool():
    """The whole point: a saving must never cost the ability to trade."""
    hidden = _shipped_filter().hidden_for("execution", LIVE_TOOLS)
    lost = sorted(set(ORDER_PATH) & hidden)
    assert not lost, (
        f"the tool filter hides order-path tool(s) {lost} — the execution agent "
        f"would be unable to place, review or cancel orders. Narrow the pattern "
        f"in mcp.tool_filter.exclude.execution, or add the tool to always_keep."
    )


def test_shipped_config_actually_removes_a_meaningful_share():
    """A filter that hides nothing is a no-op wearing a costume."""
    hidden = _shipped_filter().hidden_for("execution", LIVE_TOOLS)
    assert len(hidden) >= 25, f"expected a substantial cut, hid only {len(hidden)}"
    # The measured worst offenders must be among them.
    for waste in ("place_crypto_order", "preview_crypto_order", "create_scan"):
        assert waste in hidden


def test_crypto_tools_are_hidden_while_no_crypto_is_traded():
    hidden = _shipped_filter().hidden_for("execution", LIVE_TOOLS)
    assert {t for t in LIVE_TOOLS if "crypto" in t} <= hidden


# ── Filter semantics ──────────────────────────────────────────────


def test_unmatched_tools_stay_visible():
    """Exclusion-only: an unrecognised tool must never be dropped."""
    f = ToolFilterConfig(enabled=True, exclude={"execution": ["crypto"]})
    hidden = f.hidden_for("execution", ["place_equity_order", "brand_new_tool"])
    assert hidden == set()


def test_always_keep_overrides_a_matching_pattern():
    f = ToolFilterConfig(
        enabled=True,
        exclude={"execution": ["order"]},  # deliberately far too broad
        always_keep=["place_equity_order"],
    )
    hidden = f.hidden_for("execution", ["place_equity_order", "get_crypto_orders"])
    assert "place_equity_order" not in hidden
    assert "get_crypto_orders" in hidden


def test_matching_is_case_insensitive():
    f = ToolFilterConfig(enabled=True, exclude={"news_sentiment": ["macd"]})
    assert f.hidden_for("news_sentiment", ["MACDEXT"]) == {"MACDEXT"}


def test_an_agent_with_no_patterns_is_untouched():
    f = ToolFilterConfig(enabled=True, exclude={"execution": ["crypto"]})
    assert f.hidden_for("news_sentiment", ["get_crypto_quotes"]) == set()


def test_disabling_the_filter_restores_every_tool():
    f = ToolFilterConfig(enabled=False, exclude={"execution": ["crypto"]})
    assert f.hidden_for("execution", ["get_crypto_quotes"]) == set()


def test_empty_pattern_string_does_not_match_everything():
    """A blank entry left in YAML must not silently hide the whole toolset."""
    f = ToolFilterConfig(enabled=True, exclude={"execution": ["", "crypto"]})
    hidden = f.hidden_for("execution", ["place_equity_order", "get_crypto_quotes"])
    assert hidden == {"get_crypto_quotes"}


# ── The toolset wrapper ───────────────────────────────────────────


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


class _Wrapped:
    def __init__(self, names: list[str]) -> None:
        self._tools = [_Tool(n) for n in names]
        self.marker = "reachable"

    async def get_tools(self, readonly_context=None):
        return list(self._tools)


async def test_wrapper_hides_configured_tools_and_logs_once(caplog):
    from evotrader.agents.factory import AgentToolFilter

    f = ToolFilterConfig(enabled=True, exclude={"execution": ["crypto"]})
    wrapped = _Wrapped(["place_equity_order", "place_crypto_order"])
    wrapper = AgentToolFilter(wrapped, "execution", f)

    with caplog.at_level(logging.INFO):
        first = await wrapper.get_tools()
        second = await wrapper.get_tools()

    assert [t.name for t in first] == ["place_equity_order"]
    assert [t.name for t in second] == ["place_equity_order"]
    # Logged so the saving is verifiable, but not once per turn.
    assert caplog.text.count("Tool filter (execution)") == 1
    assert "place_crypto_order" in caplog.text


async def test_wrapper_is_transparent_when_nothing_matches():
    from evotrader.agents.factory import AgentToolFilter

    f = ToolFilterConfig(enabled=True, exclude={"execution": ["crypto"]})
    wrapped = _Wrapped(["place_equity_order"])
    wrapper = AgentToolFilter(wrapped, "execution", f)
    tools = await wrapper.get_tools()
    assert [t.name for t in tools] == ["place_equity_order"]
    # Attribute access must pass through to the wrapped toolset.
    assert wrapper.marker == "reachable"


async def test_wrapper_close_does_not_close_the_shared_toolset():
    """Lifecycle is owned externally — the same MCP session backs several agents."""
    from evotrader.agents.factory import AgentToolFilter

    closed = []

    class _Closable(_Wrapped):
        async def close(self):
            closed.append(True)

    wrapper = AgentToolFilter(_Closable(["x"]), "execution", ToolFilterConfig())
    await wrapper.close()
    assert closed == []


def test_execution_agent_applies_the_filter():
    """Wiring check: the config must actually reach the agent."""
    import inspect

    from evotrader.agents import factory

    src = inspect.getsource(factory.create_execution_agent)
    assert "_filtered_for_agent(" in src and '"execution"' in src, (
        "create_execution_agent must wrap its trading toolsets in the filter, or "
        "the 114,338 tokens of tool schemas come straight back"
    )
