"""Tax lot analysis and wash sale detection.

Fetches tax lot data from the Robinhood MCP ``get_tax_lots`` tool and
analyses it for wash sale risk.  A wash sale occurs when a security is
sold at a loss and substantially identical securities are purchased
within 30 calendar days before or after the sale.

The wash sale guard is configurable:
- ``mode="warn"``: allow the trade but emit an advisory warning
- ``mode="block"``: reject the trade as a risk violation

See: https://www.irs.gov/publications/p550#en_US_2023_publink100010601
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from evotrader.models.config import WashSaleGuard

logger = logging.getLogger(__name__)


async def fetch_tax_lots(
    ticker: str,
    call_mcp_tool,
    account_number: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch tax lot data for a ticker from the broker.

    Args:
        ticker: Equity symbol (e.g., "QQQ").
        call_mcp_tool: Async callable to invoke MCP tools.
        account_number: Robinhood account number (required by server).
            If None, attempts the call without it (will likely fail).

    Returns:
        List of tax lot dicts with keys like ``acquired_date``,
        ``quantity``, ``cost_basis``, ``sold_date``, ``proceeds``,
        ``gain_loss``, etc.  Empty list on failure.
    """
    args: dict[str, Any] = {"symbol": ticker}
    if account_number:
        args["account_number"] = account_number

    raw = await call_mcp_tool("get_equity_tax_lots", args)
    if not raw:
        return []

    # Robinhood nests data under {"data": {"tax_lots": [...]}}
    data = raw.get("data", {})
    lots = data.get("tax_lots", data.get("results", []))
    if isinstance(lots, list):
        return lots

    return []


def check_wash_sale_risk(
    ticker: str,
    tax_lots: list[dict[str, Any]],
    config: WashSaleGuard,
) -> dict[str, Any]:
    """Analyse tax lots for wash sale risk on a new buy.

    A wash sale is triggered when:
    1. The ticker has a lot that was sold at a loss, AND
    2. The sale occurred within ``config.lookback_days`` calendar days.

    Args:
        ticker: The ticker being considered for a new entry.
        tax_lots: Tax lots returned by ``fetch_tax_lots``.
        config: Wash sale configuration from the constitution.

    Returns:
        Dict with keys:
        - ``wash_sale_risk`` (bool): True if risk detected.
        - ``days_since_last_loss_sale`` (int | None): Days since the most
          recent loss sale within the window.
        - ``disallowed_loss`` (float): Total loss that would be disallowed.
        - ``loss_lots`` (list): Details of the lots triggering the risk.
    """
    if not config.enabled or not tax_lots:
        return {
            "wash_sale_risk": False,
            "days_since_last_loss_sale": None,
            "disallowed_loss": 0.0,
            "loss_lots": [],
        }

    now = datetime.now(UTC)
    cutoff = now - timedelta(days=config.lookback_days)

    loss_lots: list[dict[str, Any]] = []
    total_disallowed = 0.0
    most_recent_days: int | None = None

    for lot in tax_lots:
        # Parse sold date — skip lots that haven't been sold
        sold_date_str = lot.get("sold_date") or lot.get("close_date")
        if not sold_date_str:
            continue

        try:
            sold_date = datetime.fromisoformat(str(sold_date_str).replace("Z", "+00:00"))
            if sold_date.tzinfo is None:
                sold_date = sold_date.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            continue

        # Only consider lots within the lookback window
        if sold_date < cutoff:
            continue

        # Check for loss
        gain_loss = lot.get("gain_loss", lot.get("realized_gain_loss", 0.0))
        try:
            gain_loss = float(gain_loss or 0.0)
        except (ValueError, TypeError):
            continue

        if gain_loss >= 0:
            continue  # Not a loss

        # This lot was sold at a loss within the wash sale window
        days_since = (now - sold_date).days
        total_disallowed += abs(gain_loss)
        loss_lots.append(
            {
                "sold_date": sold_date.isoformat(),
                "days_ago": days_since,
                "loss": gain_loss,
                "quantity": lot.get("quantity"),
                "cost_basis": lot.get("cost_basis"),
            }
        )

        if most_recent_days is None or days_since < most_recent_days:
            most_recent_days = days_since

    wash_sale_risk = len(loss_lots) > 0

    if wash_sale_risk:
        logger.warning(
            "⚠️  WASH SALE RISK for %s: %d lot(s) sold at a loss "
            "within %d days (total disallowed loss: $%.2f)",
            ticker,
            len(loss_lots),
            config.lookback_days,
            total_disallowed,
        )

    return {
        "wash_sale_risk": wash_sale_risk,
        "days_since_last_loss_sale": most_recent_days,
        "disallowed_loss": round(total_disallowed, 2),
        "loss_lots": loss_lots,
    }
