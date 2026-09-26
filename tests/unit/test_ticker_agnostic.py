"""The system must not name a ticker anywhere that changes behaviour.

Switching instrument should be a settings change. It used to be a prose rewrite
across seven instruction files plus seven scattered code defaults that each
invented their own symbol — and those defaults were silent, so a snapshot missing
its ticker got labelled "QQQ" and written to the journal as though that were a
fact.

These tests fail if a literal symbol creeps back into either place.
"""

from __future__ import annotations

import re
from pathlib import Path

from evotrader.agents.instruction_context import (
    build_instruction_context,
    render_instructions,
)
from evotrader.config import AppConfig

REPO = Path(__file__).resolve().parents[2]

# Symbols that must never appear literally in an active instruction file or in
# code that decides behaviour.
FORBIDDEN = ("QQQ", "PSQ", "SQQQ", "SPY", "SH", "IWM", "DIA", "MSTR", "TQQQ", "SMST", "MSTZ")


def _active_instruction_files(data_dir: Path) -> list[tuple[str, Path]]:
    """Each agent's active instruction file in a data folder."""
    out = []
    instructions = data_dir / "instructions"
    if not instructions.is_dir():
        return out
    for agent_dir in sorted(instructions.iterdir()):
        active = agent_dir / "active.txt"
        if not active.is_file():
            continue
        version = active.read_text().strip()
        path = agent_dir / f"{version}.md"
        if path.is_file():
            out.append((agent_dir.name, path))
    return out


# ── Instruction files ─────────────────────────────────────────────
# The instructions checked are the ones a new user starts with (starter_data/).


def test_active_instructions_name_no_ticker(starter_data_dir):
    """Instructions must use {{PRIMARY_TICKER}} etc., never a literal symbol."""
    offenders = []
    for agent, path in _active_instruction_files(starter_data_dir):
        text = path.read_text()
        found = {sym for sym in FORBIDDEN if re.search(rf"\b{sym}\b", text)}
        if found:
            offenders.append(f"{agent} instructions ({path.name}) name {sorted(found)}")
    assert not offenders, (
        f"ticker(s) named literally: {offenders}. Use {{{{PRIMARY_TICKER}}}} / "
        f"{{{{ALLOWED_TICKERS}}}} / {{{{BEARISH_VEHICLE}}}} so switching instrument "
        f"stays a settings change."
    )


def test_there_is_at_least_one_active_instruction(starter_data_dir):
    """Guards the test above from silently covering nothing."""
    assert _active_instruction_files(starter_data_dir), "no active instruction files discovered"


# ── Template rendering ────────────────────────────────────────────


def test_placeholders_resolve_for_the_current_config():
    context = build_instruction_context(AppConfig())
    for key in ("PRIMARY_TICKER", "ALLOWED_TICKERS", "BEARISH_VEHICLE"):
        assert context.get(key), f"{key} resolved empty"


def test_rendered_instructions_have_no_placeholders_left(starter_data_dir):
    config = AppConfig()
    context = build_instruction_context(config)
    from evotrader.agents import instructions as loader

    for agent, _ in _active_instruction_files(starter_data_dir):
        raw = loader.load(agent, config.instructions_dir, is_sim=False)
        rendered = render_instructions(raw, context)
        leftover = set(re.findall(r"\{\{([A-Z_]+)\}\}", rendered))
        assert not leftover, f"{agent}: unresolved placeholder(s) {sorted(leftover)}"


def test_rendering_substitutes_the_configured_ticker():
    context = {"PRIMARY_TICKER": "ABC", "ALLOWED_TICKERS": "ABC, DEF"}
    out = render_instructions("Trade {{PRIMARY_TICKER}} only ({{ALLOWED_TICKERS}}).", context)
    assert out == "Trade ABC only (ABC, DEF)."


def test_unknown_placeholder_is_left_visible_not_blanked():
    """A visible {{FOO}} in a prompt is a bug report; an empty string is a mystery."""
    out = render_instructions("x {{NOT_A_REAL_KEY}} y", {"PRIMARY_TICKER": "ABC"})
    assert "{{NOT_A_REAL_KEY}}" in out


def test_bare_ticker_in_prose_is_not_rewritten():
    """The old loader did `text.replace("SPY", ticker)`, rewriting any mention."""
    text = "Benchmark against SPY for context."
    out = render_instructions(text, {"PRIMARY_TICKER": "MSTR"})
    assert out == text


# ── Bearish vehicle derivation ────────────────────────────────────


def _config_with(inverse=None, options=False, shorts=False, margin=False, ticker="ABC"):
    config = AppConfig()
    config.settings.asset.primary_ticker = ticker
    config.settings.asset.inverse_ticker = inverse
    rules = config.constitution.trading_rules
    rules.allowed_tickers = [ticker] + ([inverse] if inverse else [])
    rules.allow_options = options
    rules.allow_short_sell = shorts
    rules.allow_margin = margin
    return config


def test_inverse_etf_is_preferred_when_configured():
    text = build_instruction_context(_config_with(inverse="XYZ", options=True))["BEARISH_VEHICLE"]
    assert "XYZ" in text
    assert "LONG" in text


def test_puts_used_when_no_inverse_etf_exists():
    text = build_instruction_context(_config_with(options=True))["BEARISH_VEHICLE"]
    assert "puts" in text
    assert "no inverse etf" in text.lower()


def test_shorting_requires_both_permission_and_margin():
    only_shorts = build_instruction_context(_config_with(shorts=True))["BEARISH_VEHICLE"]
    assert "NOT AVAILABLE" in only_shorts, "shorting without margin is not executable"

    with_margin = build_instruction_context(_config_with(shorts=True, margin=True))[
        "BEARISH_VEHICLE"
    ]
    assert "short ABC" in with_margin


def test_no_bearish_vehicle_says_so_plainly():
    text = build_instruction_context(_config_with())["BEARISH_VEHICLE"]
    assert "NOT AVAILABLE" in text
    assert "stand aside" in text


# ── Code-level defaults ───────────────────────────────────────────


def test_asset_context_returns_the_configured_ticker():
    from evotrader.tools.asset_context import (
        bind_asset_context,
        is_allowed,
        primary_ticker,
        reset_asset_context,
    )

    reset_asset_context()
    assert primary_ticker() is None, "must not invent a ticker when unbound"

    bind_asset_context(_config_with(ticker="ABC"))
    assert primary_ticker() == "ABC"
    assert is_allowed("abc") is True
    assert is_allowed("QQQ") is False
    reset_asset_context()


def test_unbound_context_permits_nothing():
    from evotrader.tools.asset_context import is_allowed, reset_asset_context

    reset_asset_context()
    assert is_allowed("MSTR") is False
    assert is_allowed(None) is False


def test_no_behavioural_ticker_literals_in_source():
    """Docstring examples are fine; a literal that picks a ticker is not."""
    src = REPO / "src" / "evotrader"
    offenders: list[str] = []
    pattern = re.compile(r'(?:or|=|default=|fallback\w*\s*=)\s*"(QQQ|SPY|PSQ|IWM|MSTR)"')

    # The pydantic schema is where defaults legitimately live, and this module's
    # own docstring quotes the literals it exists to replace.
    exempt = {"models/config.py", "tools/asset_context.py"}

    for path in src.rglob("*.py"):
        if str(path.relative_to(src)) in exempt:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            # Skip comments, docstring prose, and example lines.
            if stripped.startswith(("#", "``", '"""')) or "e.g." in line or ">>>" in line:
                continue
            if pattern.search(line):
                offenders.append(f"{path.relative_to(REPO)}:{lineno}: {stripped[:90]}")

    assert not offenders, (
        "ticker literals that decide behaviour — read them from configuration "
        "via evotrader.tools.asset_context instead:\n  " + "\n  ".join(offenders)
    )


# ── Web UI ────────────────────────────────────────────────────────


def test_web_ui_names_no_ticker():
    """The dashboard must render whatever is configured, never a baked-in symbol.

    Four literals used to live here. Two were cosmetic (static badge text,
    overwritten on the first poll). Two were display fallbacks that would show a
    wrong symbol against real data — a pending MSTR order labelled "SPY" reads as
    an order in an instrument you are not trading.
    """
    web = REPO / "src" / "evotrader" / "web"
    offenders: list[str] = []

    for path in sorted(web.rglob("*")):
        if path.suffix not in {".html", ".js", ".css"} or not path.is_file():
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            # Comments may reference the old literals to explain them.
            if stripped.startswith(("*", "//", "/*", "<!--", "#")):
                continue
            found = {s for s in FORBIDDEN if re.search(rf"\b{s}\b", line)}
            if found:
                offenders.append(
                    f"{path.relative_to(REPO)}:{lineno}: {sorted(found)} — {stripped[:70]}"
                )

    assert not offenders, (
        "hardcoded ticker(s) in the web UI. The dashboard already receives the "
        "configured symbol as `local.ticker` from /api/portfolio; use "
        "getDisplayTicker() from components/utils.js for fallbacks:\n  " + "\n  ".join(offenders)
    )


def test_web_ui_exposes_the_configured_ticker_to_the_frontend():
    """The UI can only avoid a literal if the backend actually tells it."""
    server = (REPO / "src" / "evotrader" / "web" / "server.py").read_text()
    assert '"ticker": config.settings.asset.primary_ticker' in server, (
        "the portfolio payload must carry the configured ticker, or the frontend "
        "has nothing to fall back to"
    )


def test_display_ticker_accessor_exists_and_is_shared():
    """Fallbacks need one shared source, reachable from every component."""
    utils = (
        REPO / "src" / "evotrader" / "web" / "static" / "js" / "components" / "utils.js"
    ).read_text()
    assert "export function setDisplayTicker" in utils
    assert "export function getDisplayTicker" in utils

    # utils.js is re-exported wholesale, so app.js reaches it via `ui.*`.
    components = (
        REPO / "src" / "evotrader" / "web" / "static" / "js" / "components.js"
    ).read_text()
    assert 'export * from "./components/utils.js"' in components


# ── Direction availability ────────────────────────────────────────


def test_long_only_when_shorting_is_not_executable():
    """Shorting needs BOTH permission and margin; either alone is not enough."""
    text = build_instruction_context(_config_with(options=True))["DIRECTIONS_AVAILABLE"]
    assert "LONG ONLY" in text
    # The real constraint is the account type, not the instrument: MSTR reports
    # short_selling_tradability="tradable", but a cash account cannot borrow.
    assert "margin account" in text
    assert "cash account" in text
    # The dangerous outcome must be spelled out, not merely forbidden.
    assert "LIQUIDATE" in text


def test_both_directions_only_when_shorts_and_margin_are_enabled():
    shorts_no_margin = build_instruction_context(_config_with(shorts=True))["DIRECTIONS_AVAILABLE"]
    assert "LONG ONLY" in shorts_no_margin

    both = build_instruction_context(_config_with(shorts=True, margin=True))["DIRECTIONS_AVAILABLE"]
    assert "LONG and SHORT" in both


def test_no_instruction_claims_sell_opens_a_short(starter_data_dir):
    """Verified against the live MCP: `sell` closes a long, it cannot open a short.

    Executor v007 said `side: "sell"` "sells shares you don't own, opening a
    short". With a long open, that instruction would liquidate it.
    """
    false_claims = (
        "opening a short",
        "sells shares you don't own",
        "sell_short",
        "buy_to_cover",
        "borrow cost",
    )
    offenders = []
    for agent, path in _active_instruction_files(starter_data_dir):
        text = path.read_text().lower()
        for claim in false_claims:
            if claim.lower() in text:
                offenders.append(f"{agent} ({path.name}): {claim!r}")

    assert not offenders, (
        "instruction(s) describe short-selling mechanics the broker does not "
        "support — place_equity_order accepts only buy/sell and states 'no short "
        "sells':\n  " + "\n  ".join(offenders)
    )


def test_agents_that_decide_direction_are_told_what_is_possible():
    """Strategy proposes, risk approves, executor places — all three need it."""
    config = AppConfig()
    from evotrader.agents.factory import _load_instructions

    for agent in ("strategy", "executor", "risk_manager"):
        text = _load_instructions(agent, config)
        assert "LONG ONLY" in text or "LONG and SHORT" in text, (
            f"{agent} is not told which order directions are placeable"
        )


# ── Instrument descriptions and leverage ──────────────────────────
#
# The agent used to see a bare ticker and infer the instrument from price
# behaviour. That is survivable for ordinary stock and dangerous for a leveraged
# inverse fund: sized like stock, a -2x vehicle carries twice the intended risk
# and nothing downstream would catch it.


def _leveraged_config(ticker="ABC", inverse="XYZ", leverage=-2.0):
    config = AppConfig()
    config.settings.asset.primary_ticker = ticker
    config.settings.asset.inverse_ticker = inverse
    config.settings.asset.instruments = {
        ticker: {"description": "the traded instrument", "role": "primary"},
        inverse: {
            "description": f"{leverage:g}x inverse {ticker}; bearish vehicle only",
            "leverage": leverage,
            "role": "bearish vehicle",
        },
    }
    rules = config.constitution.trading_rules
    rules.allowed_tickers = [ticker, inverse]
    return config


def test_describe_renders_ticker_with_its_description():
    asset = _leveraged_config().settings.asset
    assert asset.describe("ABC") == "ABC (the traded instrument)"
    assert "bearish vehicle only" in asset.describe("XYZ")


def test_describe_falls_back_to_a_bare_ticker():
    """A symbol with no entry must render exactly as before."""
    asset = _leveraged_config().settings.asset
    assert asset.describe("NOPE") == "NOPE"


def test_leverage_defaults_to_one_for_undescribed_symbols():
    asset = _leveraged_config().settings.asset
    assert asset.leverage_of("XYZ") == -2.0
    assert asset.leverage_of("ABC") == 1.0
    assert asset.leverage_of("NOPE") == 1.0


def test_allowed_tickers_carries_the_descriptions():
    text = build_instruction_context(_leveraged_config())["ALLOWED_TICKERS"]
    assert "ABC (the traded instrument)" in text
    assert "XYZ (" in text


def test_a_leveraged_vehicle_states_its_sizing_rule():
    """The number that changes arithmetic, not just understanding."""
    text = build_instruction_context(_leveraged_config(leverage=-2.0))["BEARISH_VEHICLE"]
    assert "LEVERAGED" in text
    assert "$1 of XYZ carries $2 of ABC exposure" in text
    assert "buy $N/2 of XYZ" in text


def test_a_leveraged_vehicle_never_claims_to_be_free_of_decay():
    """Regression: the template asserted 'no time decay' for any inverse ETF.

    True for an unleveraged fund, false for a daily-reset leveraged one — and
    exactly the sort of wrong premise that produces a bad hold over a weekend.
    """
    text = build_instruction_context(_leveraged_config(leverage=-2.0))["BEARISH_VEHICLE"]
    assert "no time decay" not in text.lower()
    assert "DECAY" in text


def test_an_unleveraged_inverse_omits_the_leverage_warnings():
    """A plain -1x inverse ETF should not be burdened with sizing math."""
    text = build_instruction_context(_leveraged_config(leverage=-1.0))["BEARISH_VEHICLE"]
    assert "LEVERAGED" not in text
    assert "direction: LONG" in text


def test_the_shipped_config_describes_every_permitted_ticker():
    """A permitted symbol with no description is a gap the agent has to guess at."""
    config = AppConfig()
    asset = config.settings.asset
    allowed = list(config.constitution.trading_rules.allowed_tickers or [])
    missing = [t for t in allowed if t not in asset.instruments]
    assert not missing, (
        f"permitted ticker(s) {missing} have no entry in asset.instruments — the "
        f"agent sees a bare symbol and must infer what it is from price action"
    )
