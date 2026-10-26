"""The live channel record, in the strategy agent's prompt every cycle.

The strategy agent reasons from base rates: which channel's call has been right
how often. Those were typed into its instructions by hand, so they changed only
when the instructions did, and a count copied once could outlive the record it
came from: a hand-written row vetoed every call of the one channel the live
record scored as informative. This renders the record itself at the start of
each cycle, from the same aggregation the evolution agent's
``get_signal_calibration`` uses (:func:`evolution.tools.calibration_report`):
by author, by channel and the final call by side, to the session's close and to
the next session's close, for the primary instrument over the last 30 days,
trimmed to a size a prompt can carry. A row resting on fewer than ``MIN_DAYS``
independent days, or with a t under ``INFORMATIVE_T``, is marked: it is not a
base rate.
"""

from __future__ import annotations

from typing import Any

MIN_DAYS = 5
INFORMATIVE_T = 2.0
MAX_CHARS = 2200
LOOKBACK_DAYS = 30
HORIZONS = (("close", "To the session close"), ("1d", "To the next session's close"))


def _cell(entry: dict[str, Any]) -> str | None:
    """``calls/days hit mean t`` and its mark, or None when there is nothing to read."""
    n = int(entry.get("n_calls") or 0)
    hit, mean = entry.get("hit_rate"), entry.get("mean_signed_return_pct")
    if n < 3 or hit is None or mean is None:
        return None
    days = int(entry.get("n_independent_days") or 0)
    t = entry.get("t_stat")
    if days < MIN_DAYS:
        mark = " (days)"
    elif t is None or abs(t) < INFORMATIVE_T:
        mark = " (chance)"
    else:
        mark = ""
    t_text = f" t{t:+.1f}" if t is not None else ""
    return f"{n}/{days}d {hit:.0%} {mean:+.2f}%{t_text}{mark}"


def render_channel_record(
    reports: dict[str, dict[str, Any]],
    *,
    ticker: str,
    lookback_days: int = LOOKBACK_DAYS,
    active_algo_version: str = "",
    active_counts: dict[str, int] | None = None,
    max_chars: int = MAX_CHARS,
) -> str:
    """The block for the prompt, from one calibration report per horizon.

    Empty when no horizon has three scored calls. When the block is longer than
    ``max_chars``, the rows with the fewest calls are left out first, and the
    block says how many.
    """
    # (horizon, group, calls, text) for every readable row.
    cells: list[tuple[str, str, int, str]] = []
    heads: dict[str, str] = {}
    for horizon, label in HORIZONS:
        rep = reports.get(horizon) or {}
        if int(rep.get("n_scored") or 0) < 3 or "by_channel" not in rep:
            continue
        up = (rep.get("base_rate") or {}).get("share_of_cycles_price_rose")
        rose = f", price rose on {up:.0%}" if up is not None else ""
        heads[horizon] = f"### {label}: {rep['n_scored']} calls{rose}"
        rows = [("authors", a.get("author"), a) for a in rep.get("by_author") or []]
        rows += [("channels", c.get("channel"), c) for c in rep.get("by_channel") or []]
        final = (rep.get("by_side") or {}).get("final") or {}
        rows += [("final", side, final.get(side) or {}) for side in ("long", "short")]
        for group, name, entry in rows:
            text = _cell(entry)
            if text:
                cells.append((horizon, group, int(entry.get("n_calls") or 0), f"{name} {text}"))
    if not heads:
        return ""

    def compose(kept: list[tuple[str, str, int, str]], dropped: int) -> str:
        out = [
            f"## Channel record (live: {ticker.upper()}, last {lookback_days} days, "
            "scored calls only)",
            "Each row: calls/independent days, hit rate, mean signed return, t. Read "
            "this, not a remembered count: a row marked (days) rests on fewer than "
            f"{MIN_DAYS} independent days and one marked (chance) has |t| under "
            f"{INFORMATIVE_T:g}; neither is a base rate.",
        ]
        for horizon, _ in HORIZONS:
            if horizon not in heads:
                continue
            out.append(heads[horizon])
            for group in ("authors", "channels", "final"):
                rows = sorted(
                    (c for c in kept if c[0] == horizon and c[1] == group),
                    key=lambda c: (-c[2], c[3]),
                )
                if rows:
                    out.append(f"- {group}: " + "; ".join(c[3] for c in rows))
        if active_algo_version:
            counts = active_counts or {}
            out.append(
                f"Under the active algorithm ({active_algo_version}): "
                f"{counts.get('close', 0)} calls scored to the close, "
                f"{counts.get('1d', 0)} to the next session. The rows above pool "
                "every version."
            )
        if dropped:
            out.append(f"({dropped} rows with the fewest calls left out to fit.)")
        return "\n".join(out)

    kept = list(cells)
    text = compose(kept, 0)
    # The final call by side is never dropped; channels and authors go smallest first.
    droppable = sorted((c for c in cells if c[1] != "final"), key=lambda c: c[2])
    dropped = 0
    while len(text) > max_chars and droppable:
        kept.remove(droppable.pop(0))
        dropped += 1
        text = compose(kept, dropped)
    return text


async def build_channel_record(
    store: Any,
    ticker: str,
    *,
    active_algo_version: str = "",
    lookback_days: int = LOOKBACK_DAYS,
    max_chars: int = MAX_CHARS,
) -> str:
    """Render the record from ``store``: one calibration report per horizon."""
    from evotrader.evolution.tools import calibration_report

    reports: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for horizon, _ in HORIZONS:
        reports[horizon] = await calibration_report(
            store,
            lookback_days=lookback_days,
            ticker=ticker,
            horizon=horizon,
            include_agreement=False,
        )
        if active_algo_version:
            own = await calibration_report(
                store,
                lookback_days=lookback_days,
                ticker=ticker,
                horizon=horizon,
                algo_version=active_algo_version,
                include_agreement=False,
            )
            counts[horizon] = int(own.get("n_scored") or 0)
    return render_channel_record(
        reports,
        ticker=ticker,
        lookback_days=lookback_days,
        active_algo_version=active_algo_version,
        active_counts=counts,
        max_chars=max_chars,
    )
