"""Near-money put/call readings, recorded every cycle — phase 1 of feeding
`options_positioning`.

`options_positioning` has never voted on any instrument: `normalize_market_snapshot`
reads `options_context`, and nothing upstream ever sets it. Meanwhile
`gather_option_chain` fetches a near-money chain with per-contract volume and
open interest every cycle, and throws those numbers away. On 2026-09-22 at
15:30 ET that chain read puts 2,322 to calls 1,456 — a ratio of 1.59 — shortly
before a -0.75% after-hours move.

Why this only RECORDS, and does not yet feed the channel:

* **Ordering.** The composite is computed inside `gather_market_data`, and the
  option chain is fetched AFTERWARDS by a separate tool, on every cycle on
  record. Filling `options_context` from the chain the way the review proposed
  would arrive after the signal it is meant to inform. Feeding it properly means
  fetching the chain before the composite — an extra broker call per cycle, or a
  shared cache — which is phase 2.
* **No baseline yet.** The strategy's `pcr_baseline 1.0 / pcr_std 0.5` describe a
  TOTAL-market ratio. A seven-contract near-money ratio is a different quantity,
  and it needs its own sense of normal before a z-score means anything. Volume
  also accumulates through the session, so a 10:30 reading and a 15:30 reading
  are not comparable; how to handle that is best decided with real readings in
  hand. The review's own design keeps the channel abstaining for 10+ sessions
  while that forms. Recording now starts that clock; waiting would only delay it.

Stored as one JSON line per reading under `data/trading/options_pcr/<TICKER>.jsonl`
— outside `data/notes/`, which is ingested into semantic memory. Recording is
best-effort: a failure here must never break the option chain tool.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _num(value: Any) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def pcr_from_chain(contracts: list[dict[str, Any]]) -> dict[str, Any]:
    """Put/call volume and open-interest ratios over the fetched chain.

    A ratio is None when either side is empty: dividing by a zero side is an
    artefact of which strikes were fetched, not a reading of positioning.
    """
    put_vol = call_vol = put_oi = call_oi = 0.0
    counted = 0
    for c in contracts or []:
        kind = str((c or {}).get("type") or "").strip().lower()
        if kind not in ("put", "call"):
            continue
        counted += 1
        if kind == "put":
            put_vol += _num(c.get("volume"))
            put_oi += _num(c.get("open_interest"))
        else:
            call_vol += _num(c.get("volume"))
            call_oi += _num(c.get("open_interest"))

    def _ratio(p: float, q: float) -> float | None:
        return round(p / q, 4) if p > 0 and q > 0 else None

    return {
        "pc_volume_ratio": _ratio(put_vol, call_vol),
        "pc_oi_ratio": _ratio(put_oi, call_oi),
        "put_volume": int(put_vol),
        "call_volume": int(call_vol),
        "put_oi": int(put_oi),
        "call_oi": int(call_oi),
        "contracts_counted": counted,
        "pcr_scope": "near_money_chain",
    }


def _history_path(data_dir: Path, ticker: str) -> Path:
    return Path(data_dir) / "trading" / "options_pcr" / f"{ticker.upper()}.jsonl"


def record_pcr(
    data_dir: Path, ticker: str, reading: dict[str, Any], now: datetime | None = None
) -> bool:
    """Append one reading. Returns False (never raises) if it could not."""
    from evotrader.tools.market_hours import ET

    now = now or datetime.now(UTC)
    entry = {
        "recorded_at": now.astimezone(UTC).isoformat(),
        # The session's own calendar, so a baseline can count sessions rather
        # than UTC days that straddle an evening.
        "session_date": now.astimezone(ET).date().isoformat(),
        "et_time": now.astimezone(ET).strftime("%H:%M"),
        **reading,
    }
    try:
        path = _history_path(data_dir, ticker)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return True
    except Exception as e:
        logger.warning("Could not record put/call reading for %s: %s", ticker, e)
        return False


def sessions_recorded(data_dir: Path, ticker: str) -> int:
    """How many distinct sessions have at least one reading."""
    path = _history_path(data_dir, ticker)
    if not path.is_file():
        return 0
    dates: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            dates.add(json.loads(line)["session_date"])
        except Exception:
            continue
    return len(dates)
