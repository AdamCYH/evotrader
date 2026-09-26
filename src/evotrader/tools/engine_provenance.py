"""Notice when the signal engine or the algorithm version changes between cycles.

A composite value only means something next to other values from the same
engine and the same version. The strategy agent compares across cycles all the
time — its add rule asks whether today's composite is higher than the one at
entry — and nothing told it when the ruler itself had changed. After the
2026-09-24 mean_reversion guard fix, an open position's entry composite of
+0.3027 would read +0.3691 on the fixed engine, and most later cycles would
have cleared the old figure with no change in the market at all.

So the market-data tool records which engine (``composite_source_fingerprint``)
and which ``algo_version`` produced each composite. On the first cycle after
either changes it adds a ``SYSTEM:`` line to the trading handoff. That lands
before the strategy stage reads the handoff, so the first cycle affected is the
one told. The line is a note, not an instruction: what to compare against is
the agent's call.

Generic by construction: it fires on any code change to a channel or indicator
and on any version activation, and needs no per-change bookkeeping.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger(__name__)

STATE_FILENAME = "engine_provenance.json"


def _state_path(data_dir: Path) -> Path:
    return Path(data_dir) / "trading" / STATE_FILENAME


def note_engine(
    data_dir: Path,
    *,
    algo_version: str,
    fingerprint: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record the engine behind this cycle's composite and report a change.

    Returns ``{"changed", "since", "previous", "notice"}``. The first sighting
    is not a change — there is nothing earlier to compare against — and an
    unreadable or unwritable state file never raises.
    """
    from evotrader.tools.market_hours import ET, _resolve_now

    now = _resolve_now(now)
    current = {"algo_version": str(algo_version), "fingerprint": str(fingerprint)}
    path = _state_path(data_dir)
    previous: dict[str, Any] | None = None
    try:
        if path.is_file():
            previous = json.loads(path.read_text(encoding="utf-8")) or None
    except Exception as e:
        logger.warning("Engine provenance unreadable at %s: %s", path, e)

    if previous and all(previous.get(k) == v for k, v in current.items()):
        return {"changed": False, "since": previous.get("since"), "previous": None, "notice": None}

    since = now.isoformat()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**current, "since": since}), encoding="utf-8")
    except Exception as e:
        logger.warning("Could not record engine provenance: %s", e)

    if not previous:
        return {"changed": False, "since": since, "previous": None, "notice": None}

    what = []
    if previous.get("algo_version") != current["algo_version"]:
        what.append(f"algorithm version {previous.get('algo_version')} → {current['algo_version']}")
    if previous.get("fingerprint") != current["fingerprint"]:
        what.append(f"signal code {previous.get('fingerprint')} → {current['fingerprint']}")
    notice = (
        f"{now.astimezone(ET):%Y-%m-%d %H:%M} ET: the composite is now computed "
        f"differently ({'; '.join(what)}). Composite values from earlier cycles, "
        f"including any entry composite quoted in these notes, may not be "
        f"comparable with this cycle's."
    )
    return {"changed": True, "since": since, "previous": previous, "notice": notice}
