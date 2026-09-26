"""CLI Runner for executing decoupled historical backtests and version benchmarks."""

from __future__ import annotations

import argparse
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from evotrader import paths
from evotrader.algorithms.loader import StrategyLoader
from evotrader.backtest.data import HistoricalDataFetcher
from evotrader.backtest.engine import BacktestEngine
from evotrader.backtest.metrics import calculate_tear_sheet
from evotrader.backtest.snapshot_builder import SnapshotBuilder
from evotrader.models.market import MarketSnapshot

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backtest")


@dataclass
class RiskParams:
    """Execution and risk settings, separate from the signal parameters.

    Version ``config.yaml`` files carry only sub-strategy weights and
    thresholds — nothing about sizing or stops — so these were previously
    hardcoded in the engine and diverged from the live configuration. They
    now default to live parity and can be overridden per run or by a
    ``backtest:`` block in a version config.
    """

    initial_cash: float = 25000.0
    max_position_pct: float = 0.10
    # Whole units by default, matching equity execution. Crypto and fractional
    # -share venues are continuously divisible; without this a $2,500 allocation
    # cannot open any position in a $100k instrument.
    fractional_units: bool = False
    slippage_bps: float = 2.0
    stop_loss_atr_mult: float = 2.0
    take_profit_atr_mult: float = 3.5
    entry_threshold_range_bound: float = 0.05
    entry_threshold_trending: float = 0.03
    entry_threshold_high_vol: float = 0.05
    exit_threshold: float = 0.0
    allow_shorts: bool = True
    next_bar_open_fill: bool = True
    intrabar_stops: bool = True

    @classmethod
    def from_sources(cls, config_block: dict[str, Any], overrides: dict[str, Any]) -> RiskParams:
        """Layer: dataclass defaults < version config `backtest:` < CLI flags."""
        fields = {f for f in cls.__dataclass_fields__}
        merged = {k: v for k, v in (config_block or {}).items() if k in fields}
        merged.update({k: v for k, v in overrides.items() if k in fields and v is not None})
        return cls(**merged)

    def to_engine(self, ticker: str = "") -> BacktestEngine:
        return BacktestEngine(
            ticker=ticker,
            fractional_units=self.fractional_units,
            initial_cash=self.initial_cash,
            entry_threshold_range_bound=self.entry_threshold_range_bound,
            entry_threshold_trending=self.entry_threshold_trending,
            entry_threshold_high_vol=self.entry_threshold_high_vol,
            exit_threshold=self.exit_threshold,
            stop_loss_atr_mult=self.stop_loss_atr_mult,
            take_profit_atr_mult=self.take_profit_atr_mult,
            max_position_pct=self.max_position_pct,
            slippage_bps=self.slippage_bps,
            allow_shorts=self.allow_shorts,
            next_bar_open_fill=self.next_bar_open_fill,
            intrabar_stops=self.intrabar_stops,
        )


def build_snapshots(df_intraday: Any, df_daily: Any, ticker: str) -> list[MarketSnapshot]:
    """Materialize the snapshot stream once so many runs can share it."""
    builder = SnapshotBuilder(df_intraday=df_intraday, df_daily=df_daily, ticker=ticker)
    return list(builder.iter_snapshots())


def benchmark_metrics(snapshots: list[MarketSnapshot], exposure: float) -> dict[str, float]:
    """Buy & hold on the same bars, scaled to the strategy's average exposure.

    Comparing a 10%-sized, 50%-of-the-time strategy against 100% buy & hold
    is not a like-for-like read, so report both.
    """
    px = np.array([s.quote.last for s in snapshots], dtype=float)
    if len(px) < 2:
        return {}
    rets = np.diff(px) / px[:-1]
    peak = np.maximum.accumulate(px)
    full_return = (px[-1] / px[0] - 1.0) * 100.0
    return {
        "buy_hold_return_pct": round(full_return, 2),
        "buy_hold_matched_return_pct": round(full_return * exposure, 2),
        "buy_hold_sharpe": (
            round(float(rets.mean() / rets.std() * np.sqrt(252 * 7)), 2) if rets.std() > 0 else 0.0
        ),
        "buy_hold_max_drawdown_pct": round(float(((px - peak) / peak).min()) * 100.0, 2),
        "matched_exposure": round(exposure, 4),
    }


def run_single_backtest(
    version: str,
    snapshots: list[MarketSnapshot],
    risk: RiskParams | None = None,
    manifest_path: Path | None = None,
    algorithms_dir: Path | None = None,
    overrides: dict[str, Any] | None = None,
    ticker: str = "",
) -> dict[str, Any]:
    """Execute a backtest for a specific algorithm version."""
    algorithms_dir = algorithms_dir or paths.data_dir() / "algorithms"
    manifest_path = manifest_path or algorithms_dir / "strategy_manifest.yaml"
    config_file = algorithms_dir / version / "config.yaml"
    if not config_file.is_file():
        raise FileNotFoundError(
            f"Config file not found for algorithm version '{version}': {config_file}"
        )

    params = yaml.safe_load(config_file.read_text()) or {}
    if risk is None:
        risk = RiskParams.from_sources(params.get("backtest", {}), overrides or {})

    loader = StrategyLoader(manifest_path)
    composite = loader.build_composite(params, active_version=version)
    engine = risk.to_engine(ticker=ticker)

    logger.info("Running backtest for version '%s' over %d bars...", version, len(snapshots))

    # Track which sub-strategies were actually able to vote. A strategy that
    # abstains on every bar is a data-plumbing gap, not a market judgement,
    # and it silently changes the ensemble via weight renormalization.
    abstained: Counter[str] = Counter()
    emitted: Counter[str] = Counter()
    seen: Counter[str] = Counter()
    reasons: dict[str, Counter[str]] = {}

    for snapshot in snapshots:
        detailed = composite.compute_detailed_signal(snapshot)
        for sub in detailed.signals:
            if sub.metadata.get("role") == "multiplier":
                continue
            seen[sub.name] += 1
            if not sub.metadata.get("applicable", True):
                abstained[sub.name] += 1
                reasons.setdefault(sub.name, Counter())[
                    str(sub.metadata.get("reason", "unspecified"))
                ] += 1
            elif abs(sub.value) >= 1e-3:
                emitted[sub.name] += 1
        engine.step(snapshot, composite.compute_signal(snapshot))

    total = len(snapshots)
    participation = {
        name: {
            "abstain_pct": round(abstained[name] / n * 100.0, 1),
            "emit_pct": round(emitted[name] / n * 100.0, 1),
            "inert": abstained[name] == n,
            "top_abstain_reason": (reasons[name].most_common(1)[0][0] if reasons.get(name) else ""),
        }
        for name, n in seen.items()
        if n
    }
    inert = sorted(k for k, v in participation.items() if v["inert"])
    if inert:
        logger.warning(
            "%d sub-strategies never voted (%s) — the ensemble is renormalized "
            "over the survivors, so this is NOT the live weighting.",
            len(inert),
            ", ".join(inert),
        )

    metrics = calculate_tear_sheet(
        initial_cash=risk.initial_cash,
        closed_trades=engine.closed_trades,
        equity_curve=engine.equity_curve,
        execution_assumptions=engine.execution_assumptions,
    )
    exposure = (
        float(np.mean([bool(r["in_position"]) for r in engine.equity_curve]))
        if engine.equity_curve
        else 0.0
    )
    metrics["version"] = version
    metrics["total_bars"] = total
    metrics["exposure_pct"] = round(exposure * 100.0, 1)
    metrics["participation"] = participation
    metrics["inert_strategies"] = inert
    metrics["benchmark"] = benchmark_metrics(snapshots, exposure * risk.max_position_pct)
    metrics["ticker"] = ticker
    metrics["skipped_unaffordable"] = engine.skipped_unaffordable
    metrics["fractional_units"] = risk.fractional_units
    metrics["max_position_pct"] = risk.max_position_pct
    if snapshots:
        metrics["window_start"] = snapshots[0].timestamp.isoformat()
        metrics["window_end"] = snapshots[-1].timestamp.isoformat()
    return metrics


def walk_forward(
    version: str,
    snapshots: list[MarketSnapshot],
    folds: int,
    risk: RiskParams,
    min_fold_bars: int = 50,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Split the window into sequential folds and score each independently.

    This does not fit anything — the algorithm's parameters come from a
    frozen version config. It exists so a single flattering window cannot
    be mistaken for a stable edge.

    Folds are contiguous and disjoint. A fold too short to be meaningful is
    skipped loudly rather than silently, since a quietly missing fold is
    exactly how an unstable result comes to look consistent.
    """
    n = len(snapshots)
    if folds < 1:
        raise ValueError(f"folds must be >= 1, got {folds}")
    size = n // folds
    if size == 0:
        raise ValueError(f"{n} snapshots cannot be split into {folds} folds")

    results = []
    for i in range(folds):
        start = i * size
        end = n if i == folds - 1 else (i + 1) * size
        chunk = snapshots[start:end]
        if len(chunk) < min_fold_bars:
            logger.warning(
                "Fold %d skipped: %d bars is below min_fold_bars=%d.",
                i + 1,
                len(chunk),
                min_fold_bars,
            )
            continue
        m = run_single_backtest(version, chunk, risk=risk, **kwargs)
        m["fold"] = i + 1
        results.append(m)
    return results


def format_report_markdown(metrics: dict[str, Any], period: str, interval: str) -> str:
    sym = metrics.get("ticker", "?")
    """Format single-version metrics as a clean markdown report."""
    v = metrics.get("version", "unknown")
    regime_rows = ""
    for reg, rstats in metrics.get("regime_breakdown", {}).items():
        regime_rows += (
            f"| `{reg}` | {rstats['trade_count']} | {rstats['win_rate']:.1%} | "
            f"${rstats['total_pnl']:+,.2f} | ${rstats['avg_pnl']:+,.2f} |\n"
        )

    side_rows = ""
    for side, sstats in metrics.get("side_breakdown", {}).items():
        side_rows += (
            f"| **{side}** | {sstats['trade_count']} | {sstats['win_rate']:.1%} | "
            f"${sstats['total_pnl']:+,.2f} | ${sstats['avg_pnl']:+,.2f} |\n"
        )

    attribution_rows = ""
    for auth, astats in metrics.get("authoring_attribution", {}).items():
        attribution_rows += (
            f"| `{auth}` | {astats['trade_count']} | {astats['win_rate']:.1%} | "
            f"${astats['total_pnl']:+,.2f} | ${astats['avg_pnl']:+,.2f} |\n"
        )

    part_rows = ""
    for name, p in sorted(metrics.get("participation", {}).items()):
        flag = " ⚠️ **inert**" if p["inert"] else ""
        reason = p.get("top_abstain_reason") or "—"
        part_rows += (
            f"| `{name}` | {p['emit_pct']:.1f}% | {p['abstain_pct']:.1f}%{flag} | `{reason}` |\n"
        )

    exit_rows = ""
    for r, c in metrics.get("exit_reasons", {}).items():
        exit_rows += f"- **{r}**: {c} trades\n"

    pf_str = (
        f"{metrics['profit_factor']:.2f}"
        if metrics.get("profit_factor") is not None
        else "∞ (No Losses)"
    )

    ea = metrics.get("execution_assumptions", {})
    ea_rows = "".join(f"| {k} | `{val}` |\n" for k, val in ea.items())

    bm = metrics.get("benchmark", {})
    bm_block = ""
    if bm:
        bm_block = f"""
| Benchmark | Value |
|---|---|
| {sym} buy & hold (full notional) | {bm["buy_hold_return_pct"]:+.2f}% |
| {sym} at matched exposure ({bm["matched_exposure"]:.1%}) | {bm["buy_hold_matched_return_pct"]:+.2f}% |
| {sym} Sharpe / max DD | {bm["buy_hold_sharpe"]:.2f} / {bm["buy_hold_max_drawdown_pct"]:.2f}% |
"""

    inert = metrics.get("inert_strategies", [])
    inert_warning = ""
    if inert:
        inert_warning = (
            f"\n> ⚠️ **{len(inert)} sub-strategies never voted**: "
            f"{', '.join(f'`{s}`' for s in inert)}. The composite renormalizes "
            f"weights over the survivors, so these results describe a **differently "
            f"weighted ensemble than the live system**. See the participation "
            f"table for why each abstained.\n"
        )

    # A run that took no trades because it could never afford one unit is a
    # sizing failure, not a market result. Without this the report shows
    # "0 trades / Profit Factor infinity", which reads like the strategy declined.
    skipped = metrics.get("skipped_unaffordable", 0)
    sizing_warning = ""
    if skipped:
        sizing_warning = (
            f"\n> 🛑 **{skipped:,} entries were dropped because one unit of {sym} "
            f"cost more than the per-trade allocation** "
            f"(${metrics['initial_cash']:,.0f} x {metrics.get('max_position_pct', 0.1):.0%}). "
            + (
                "Every metric below is a sizing artefact, not a market result. "
                if metrics.get("total_trades", 0) == 0
                else "Some signals could not be acted on, so this understates activity. "
            )
            + "Re-run with `--fractional` for a continuously divisible instrument "
            "(crypto, fractional shares), or raise `--cash`.\n"
        )

    return f"""# Backtest Report: {v}

**Generated**: {datetime.now(UTC).isoformat()}  
**Dataset**: {sym} ({interval} bars, {period}) | **Total Bars**: {metrics.get("total_bars", 0)}  
**Window**: {metrics.get("window_start", "n/a")} → {metrics.get("window_end", "n/a")}  
**Initial Capital**: ${metrics["initial_cash"]:,.2f} | **Final Equity**: ${metrics["final_equity"]:,.2f}
{inert_warning}{sizing_warning}
---

## 1. Key Performance Highlights

| Metric | Value |
|---|---|
| **Total Return** | **{metrics["total_return_pct"]:+.2f}%** (${metrics["total_pnl"]:+,.2f}) |
| **CAGR** | **{metrics["cagr"]:+.2f}%** |
| **Sharpe Ratio** | **{metrics["sharpe_ratio"]:.2f}** |
| **Sortino Ratio** | **{metrics["sortino_ratio"]:.2f}** |
| **Max Drawdown** | **{metrics["max_drawdown_pct"]:.2f}%** ({metrics["max_drawdown_duration_bars"]} bars) |
| **Total Trades** | **{metrics["trade_count"]}** |
| **Win Rate** | **{metrics["win_rate"]:.1%}** |
| **Profit Factor** | **{pf_str}** |
| **Average Win / Loss** | **+${metrics["avg_win"]:,.2f}** / **-${abs(metrics["avg_loss"]):,.2f}** (W/L: {metrics["win_loss_ratio"]:.2f}) |
| **Average Hold Duration** | **{metrics["avg_bars_held"]} bars** |
| **Time in Market** | **{metrics.get("exposure_pct", 0):.1f}%** of bars |
{bm_block}
---

## 2. Execution Assumptions

These drive the numbers above and are not neutral — record them with the result.

| Assumption | Value |
|---|---|
{ea_rows}| annualization (bars/day) | `{metrics.get("bars_per_day", "n/a")}` |

---

## 3. Directional Breakdown

| Side | Trades | Win Rate | Net P&L | Avg Trade P&L |
|---|---|---|---|---|
{side_rows if side_rows else "| *None* | 0 | 0.0% | $0.00 | $0.00 |\n"}

---

## 4. Market Regime Stratification

| Regime | Trades | Win Rate | Net P&L | Avg Trade P&L |
|---|---|---|---|---|
{regime_rows if regime_rows else "| *None* | 0 | 0.0% | $0.00 | $0.00 |\n"}

---

## 5. Sub-Strategy Participation

A strategy that never emits is a missing input, not a market judgement.

| Sub-Strategy | Emitted (non-zero) | Abstained | Top abstain reason |
|---|---|---|---|
{part_rows if part_rows else "| *None* | 0.0% | 0.0% | — |\n"}

---

## 6. Authoring Signal Attribution

| Sub-Strategy | Trades Authored | Win Rate | Net P&L | Avg Trade P&L |
|---|---|---|---|---|
{attribution_rows if attribution_rows else "| *None* | 0 | 0.0% | $0.00 | $0.00 |\n"}

---

## 7. Exit Reasons
{exit_rows if exit_rows else "- None\n"}
"""


def format_comparison_table(results: list[dict[str, Any]]) -> str:
    """Format multiple version results as a comparison markdown table."""
    headers = [
        "Version",
        "Total Return %",
        "Total P&L",
        "CAGR",
        "Sharpe",
        "Max DD %",
        "Trades",
        "Win Rate",
        "Profit Factor",
        "Inert",
    ]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for m in results:
        pf = f"{m['profit_factor']:.2f}" if m.get("profit_factor") is not None else "∞"
        row = [
            f"`{m['version']}`",
            f"{m['total_return_pct']:+.2f}%",
            f"${m['total_pnl']:+,.2f}",
            f"{m['cagr']:+.2f}%",
            f"{m['sharpe_ratio']:.2f}",
            f"{m['max_drawdown_pct']:.2f}%",
            str(m["trade_count"]),
            f"{m['win_rate']:.1%}",
            pf,
            str(len(m.get("inert_strategies", []))),
        ]
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def format_walk_forward_table(results: list[dict[str, Any]]) -> str:
    headers = [
        "Fold",
        "Window",
        "Return %",
        "Sharpe",
        "Max DD %",
        "Trades",
        "Win Rate",
        "Benchmark (matched)",
    ]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for m in results:
        bm = m.get("benchmark", {})
        lines.append(
            "| "
            + " | ".join(
                [
                    str(m.get("fold", "?")),
                    f"{m.get('window_start', '')[:10]} → {m.get('window_end', '')[:10]}",
                    f"{m['total_return_pct']:+.2f}%",
                    f"{m['sharpe_ratio']:.2f}",
                    f"{m['max_drawdown_pct']:.2f}%",
                    str(m["trade_count"]),
                    f"{m['win_rate']:.1%}",
                    f"{bm.get('buy_hold_matched_return_pct', 0):+.2f}%",
                ]
            )
            + " |"
        )
    rets = [m["total_return_pct"] for m in results]
    if rets:
        pos = sum(1 for r in rets if r > 0)
        lines.append("")
        lines.append(
            f"**Consistency**: {pos}/{len(rets)} folds positive · "
            f"mean {np.mean(rets):+.2f}% · stdev {np.std(rets):.2f}% · "
            f"worst {min(rets):+.2f}%"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="EvoTrader Algorithm Backtester")
    parser.add_argument("--version", type=str, default=None, help="Algorithm version to backtest")
    parser.add_argument(
        "--compare", type=str, default=None, help="Comma-separated versions, or 'all'"
    )
    parser.add_argument(
        "--ticker",
        type=str,
        default=None,
        help=(
            "Symbol to backtest (default: asset.primary_ticker from settings.yaml). "
            "Accepts any yfinance symbol, e.g. QQQ, SPY, BTC-USD. The strategy "
            "suite assumes an exchange-session instrument: on a 24/7 asset the "
            "session VWAP anchor falls on UTC midnight and the gap channel goes "
            "inert, so check the participation table before reading the result."
        ),
    )
    parser.add_argument(
        "--period", type=str, default="2y", help="Historical period (e.g. 2y, 1y, 6mo)"
    )
    parser.add_argument(
        "--interval", type=str, default="1h", help="Intraday bar interval (e.g. 1h, 5m)"
    )
    parser.add_argument(
        "--intraday-csv",
        type=str,
        default=None,
        help="External intraday OHLCV CSV (overrides yfinance)",
    )
    parser.add_argument("--refresh", action="store_true", help="Force fresh data download")
    parser.add_argument(
        "--walk-forward",
        type=int,
        default=0,
        metavar="N",
        help="Split the window into N sequential folds",
    )

    risk_group = parser.add_argument_group("risk / execution overrides")
    risk_group.add_argument(
        "--cash",
        dest="initial_cash",
        type=float,
        default=None,
        help="Initial cash (default 25000)",
    )
    risk_group.add_argument(
        "--max-position-pct",
        type=float,
        default=None,
        help="Position size as fraction of cash (default 0.10, live parity)",
    )
    risk_group.add_argument(
        "--slippage-bps",
        type=float,
        default=None,
        help="Per-side slippage in bps (default 2.0)",
    )
    risk_group.add_argument("--stop-loss-atr-mult", type=float, default=None)
    risk_group.add_argument("--take-profit-atr-mult", type=float, default=None)
    risk_group.add_argument("--exit-threshold", type=float, default=None)
    risk_group.add_argument(
        "--no-shorts",
        dest="allow_shorts",
        action="store_false",
        default=None,
        help="Long-only",
    )
    risk_group.add_argument(
        "--fractional",
        dest="fractional_units",
        action="store_true",
        default=None,
        help=(
            "Allow fractional position sizes. Required for instruments priced "
            "above the per-trade allocation (e.g. BTC-USD at $100k with a "
            "$25k account and max_position_pct=0.10)."
        ),
    )
    risk_group.add_argument(
        "--legacy-fills",
        action="store_true",
        help="Same-bar-close fills and close-only stops (pre-fix behaviour)",
    )
    args = parser.parse_args()

    data = paths.data_dir()
    reports_dir = data / "backtest" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    overrides: dict[str, Any] = {
        k: getattr(args, k)
        for k in (
            "initial_cash",
            "max_position_pct",
            "slippage_bps",
            "stop_loss_atr_mult",
            "take_profit_atr_mult",
            "exit_threshold",
            "allow_shorts",
            "fractional_units",
        )
    }
    if args.legacy_fills:
        overrides["next_bar_open_fill"] = False
        overrides["intrabar_stops"] = False

    # Resolve active version if not specified
    if not args.version and not args.compare:
        active_yaml = data / "algorithms" / "active.yaml"
        if active_yaml.is_file():
            active_data = yaml.safe_load(active_yaml.read_text()) or {}
            args.version = active_data.get("active_version", "v023_staleness_annihilation_fix")
        else:
            args.version = "v023_staleness_annihilation_fix"

    # Resolve the symbol: explicit flag wins, else the configured primary ticker.
    ticker = args.ticker
    if not ticker:
        settings_path = data / "settings.yaml"
        if settings_path.is_file():
            settings = yaml.safe_load(settings_path.read_text()) or {}
            ticker = (settings.get("asset") or {}).get("primary_ticker")
    if not ticker:
        raise SystemExit(
            "No ticker to backtest: pass --ticker, or set asset.primary_ticker "
            "in data/settings.yaml. Refusing to guess — a silent default would "
            "report results for an instrument you did not ask for."
        )
    ticker = ticker.upper()

    # Acquire data
    fetcher = HistoricalDataFetcher()
    if args.intraday_csv:
        logger.info("Loading intraday bars from %s", args.intraday_csv)
        df_intraday = fetcher.load_csv(args.intraday_csv)
    else:
        logger.info(
            "Fetching historical data (%s: intraday=%s over %s, daily=5y)...",
            ticker,
            args.interval,
            args.period,
        )
        df_intraday = fetcher.fetch_intraday(
            ticker, period=args.period, interval=args.interval, force_refresh=args.refresh
        )
    df_daily = fetcher.fetch_daily(ticker, period="5y", force_refresh=args.refresh)

    logger.info("Building snapshots for %s...", ticker)
    snapshots = build_snapshots(df_intraday, df_daily, ticker=ticker)
    logger.info("Built %d snapshots.", len(snapshots))

    if args.compare:
        if args.compare.strip() == "all":
            versions = sorted(
                p.name
                for p in (data / "algorithms").iterdir()
                if p.is_dir() and (p / "config.yaml").is_file()
            )
        else:
            versions = [v.strip() for v in args.compare.split(",") if v.strip()]
        logger.info("Benchmarking %d versions: %s", len(versions), versions)
        results = []
        failures: list[tuple[str, str]] = []
        for v in versions:
            try:
                results.append(
                    run_single_backtest(v, snapshots, overrides=overrides, ticker=ticker)
                )
            except Exception as e:  # a broken archived config must not kill sweep
                logger.error("Version '%s' failed to run: %s", v, e)
                failures.append((v, f"{type(e).__name__}: {e}"))

        table_md = format_comparison_table(results)
        print("\n" + "=" * 80)
        print("ALGORITHM VERSION COMPARISON BENCHMARK")
        print("=" * 80)
        print(table_md)
        if failures:
            print("\nFailed to run:")
            for v, why in failures:
                print(f"  - {v}: {why}")
        print("=" * 80 + "\n")

        body = f"# Algorithm Comparison Benchmark\n\n{table_md}\n"
        if failures:
            body += "\n## Failed to run\n\n" + "".join(f"- `{v}`: {why}\n" for v, why in failures)
        comp_path = reports_dir / f"comparison_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.md"
        comp_path.write_text(body)
        logger.info("Saved comparison benchmark to %s", comp_path)

    elif args.walk_forward:
        v = args.version
        params = yaml.safe_load((data / "algorithms" / v / "config.yaml").read_text()) or {}
        risk = RiskParams.from_sources(params.get("backtest", {}), overrides)
        folds = walk_forward(v, snapshots, args.walk_forward, risk)
        table_md = format_walk_forward_table(folds)
        print("\n" + "=" * 80)
        print(f"WALK-FORWARD: {v} over {args.walk_forward} folds")
        print("=" * 80)
        print(table_md)
        print("=" * 80 + "\n")
        wf_path = reports_dir / f"walkforward_{v}_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.md"
        wf_path.write_text(f"# Walk-Forward: {v}\n\n{table_md}\n")
        logger.info("Saved walk-forward report to %s", wf_path)

    else:
        v = args.version
        logger.info("Running backtest for single version: %s", v)
        metrics = run_single_backtest(v, snapshots, overrides=overrides, ticker=ticker)
        report_md = format_report_markdown(metrics, period=args.period, interval=args.interval)

        print("\n" + report_md)

        out_path = reports_dir / f"report_{v}_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.md"
        out_path.write_text(report_md)
        logger.info("Saved backtest report to %s", out_path)


if __name__ == "__main__":
    main()
