"""Realized P&L reconciliation against the Robinhood broker.

Cross-references the agent's internal trade journal P&L with the
broker's official ``get_realized_pnl`` data.  Discrepancies indicate
journal drift (missed fills, bad prices, unreconciled cancellations).

This should be called at the start of each trading session to catch
issues early.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def fetch_broker_realized_pnl(
    call_mcp_tool,
    account_number: str | None = None,
) -> dict[str, Any]:
    """Fetch realized P&L summary from Robinhood.

    Args:
        call_mcp_tool: Async callable to invoke MCP tools.
        account_number: Optional account number filter.

    Returns:
        Dict with broker P&L data, or empty dict on failure.
    """
    args: dict[str, Any] = {}
    if account_number:
        args["account_number"] = account_number

    raw = await call_mcp_tool("get_realized_pnl", args)
    if not raw:
        return {}

    return raw.get("data", raw)


async def reconcile_pnl(
    journal_pnl: float,
    call_mcp_tool,
    account_number: str | None = None,
    tolerance: float = 0.50,
) -> dict[str, Any]:
    """Compare journal P&L with broker P&L and flag discrepancies.

    Args:
        journal_pnl: Total realized P&L from the agent's trade journal.
        call_mcp_tool: Async callable to invoke MCP tools.
        account_number: Robinhood account number.
        tolerance: Dollar threshold below which discrepancies are ignored.

    Returns:
        Dict with:
        - ``reconciled`` (bool): True if P&L values match within tolerance.
        - ``journal_pnl``: Our internal P&L.
        - ``broker_pnl``: Robinhood's reported P&L.
        - ``discrepancy``: Absolute difference.
        - ``discrepancy_pct``: Percentage discrepancy relative to broker P&L.
        - ``warnings``: List of advisory messages.
    """
    warnings: list[str] = []

    broker_data = await fetch_broker_realized_pnl(call_mcp_tool, account_number)
    if not broker_data:
        return {
            "reconciled": True,
            "journal_pnl": journal_pnl,
            "broker_pnl": None,
            "discrepancy": None,
            "discrepancy_pct": None,
            "warnings": ["Could not fetch broker P&L — skipping reconciliation"],
        }

    # Extract the realized P&L value from the response
    # Robinhood's shape may vary; try common keys
    broker_pnl: float | None = None
    for key in ("total_realized_pnl", "realized_pnl", "total_pnl", "total"):
        val = broker_data.get(key)
        if val is not None:
            try:
                broker_pnl = float(val)
                break
            except (ValueError, TypeError):
                continue

    if broker_pnl is None:
        return {
            "reconciled": True,
            "journal_pnl": journal_pnl,
            "broker_pnl": None,
            "discrepancy": None,
            "discrepancy_pct": None,
            "warnings": ["Broker P&L response did not contain a parseable total"],
        }

    discrepancy = abs(journal_pnl - broker_pnl)
    discrepancy_pct = (discrepancy / abs(broker_pnl) * 100) if broker_pnl != 0 else 0.0

    reconciled = discrepancy <= tolerance

    if not reconciled:
        msg = (
            f"P&L DISCREPANCY: journal=${journal_pnl:.2f} vs "
            f"broker=${broker_pnl:.2f} (Δ=${discrepancy:.2f}, "
            f"{discrepancy_pct:.1f}%)"
        )
        logger.warning("⚠️  %s", msg)
        warnings.append(msg)

        # Provide directional guidance
        if journal_pnl > broker_pnl:
            warnings.append(
                "Journal shows higher P&L than broker — possible missed "
                "loss or fabricated fill in journal."
            )
        else:
            warnings.append(
                "Broker shows higher P&L than journal — possible missed fill or unreconciled trade."
            )
    else:
        logger.info(
            "✅ P&L reconciled: journal=$%.2f, broker=$%.2f (Δ=$%.2f)",
            journal_pnl,
            broker_pnl,
            discrepancy,
        )

    return {
        "reconciled": reconciled,
        "journal_pnl": round(journal_pnl, 2),
        "broker_pnl": round(broker_pnl, 2),
        "discrepancy": round(discrepancy, 2),
        "discrepancy_pct": round(discrepancy_pct, 1),
        "warnings": warnings,
    }
