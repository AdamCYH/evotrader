"""The risk manager's last verdict, read back from the cycle's own record.

The risk manager is an agent. Its verdict exists as the report it writes
("Verdict: REJECTED", then its reasons) and as the arguments of the
``check_risk_limits`` call it makes, and nothing passed either to the next
cycle, which starts from a new session. So a rejected proposal looked, a cycle
later, like an order that had vanished at the broker: a cycle read it that way
and proposed the same over-wide stop again, and was refused for the same
reason.

Both are read back from ``agent_thought_log``, where the orchestrator's
``risk_manager`` tool response holds the report, so nothing new is stored:
``latest_risk_review`` for any reader (``get_open_positions`` returns it as
``last_risk_verdict``), ``note_rejection`` for the post-cycle SYSTEM line in the
trading handoff. Only a cycle's LAST review counts: a proposal refused and then
approved in the same cycle went through.

The verdict is taken only where the report labels it ("Verdict: ...",
"Result: ...", "Decision: ..."). A report that does not is read as having no
verdict, so a misread can never put a rejection that did not happen in front of
the next cycle.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: "Verdict: REJECTED", "**Verdict:** **MODIFIED**", "VALIDATION RESULT: REJECTED".
_VERDICT_RE = re.compile(
    r"\b(?:verdict|result|decision)\b[^A-Za-z\n]{0,20}(APPROVED|REJECTED|MODIFIED)\b",
    re.IGNORECASE,
)

#: The heading of the part of a rejection the next cycle can act on: a heading
#: or numbered line ("### 3. Required Modifications", "### 4. Resolution & Path to
#: Approval", "## **RECOMMENDED MODIFICATION**") naming one of these.
_NEXT_STEP_RE = re.compile(
    r"^[ \t]*(?:#+|\*\*|\d+\.)[^\n]*?\b(?:required\s+(?:modifications?|actions?|changes?)"
    r"|resubmission|path\s+to\s+approval|recommended\s+(?:modifications?|actions?)"
    r"|what\s+must\s+happen)\b[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)

_CHECK_TOOLS = ("check_risk_limits", "check_option_risk_limits")

#: Characters of the report's reasons carried into a SYSTEM line.
REASONS_CHARS = 300


def verdict_of(report: str | None) -> str | None:
    """APPROVED, MODIFIED or REJECTED where the report labels it, else None."""
    found = _VERDICT_RE.search(report or "")
    return found.group(1).upper() if found else None


def _plain(text: str) -> str:
    """Markdown emphasis, headings, code marks and table bars removed, one line."""
    text = re.sub(r"[*`]+|\$\$", "", text)
    text = re.sub(r"[#|>]+", " ", text)
    return re.sub(r"\s+", " ", text).strip(" -:")


def reasons_of(report: str | None, limit: int = REASONS_CHARS) -> str:
    """What the report asks for: its required-modification section, else the text after its verdict."""
    text = report or ""
    step = _NEXT_STEP_RE.search(text)
    if step:
        body = text[step.end() :]
    else:
        verdict = _VERDICT_RE.search(text)
        body = text[verdict.end() :] if verdict else text
    out = _plain(body)
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def _meta(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _report_text(meta: dict[str, Any]) -> str:
    response = meta.get("response")
    if isinstance(response, dict):
        response = response.get("result", "")
    return response if isinstance(response, str) else ""


async def latest_risk_review(
    db: Any, *, session_id: str | None = None, since: str | None = None
) -> dict[str, Any] | None:
    """The newest risk-manager report, of one session or of any since ``since`` (UTC ISO).

    ``trade`` is the arguments of the risk manager's last ``check_risk_limits``
    (or ``check_option_risk_limits``) call in that review, ``violations`` what
    that check returned, ``reasons`` the report's own words (see
    ``reasons_of``). None when there is no report.
    """
    where = ["event_type = 'tool_response'", "content = 'risk_manager'"]
    params: list[Any] = []
    if session_id:
        where.append("session_id = ?")
        params.append(session_id)
    if since:
        where.append("timestamp >= ?")
        params.append(since)
    tools = ", ".join(f"'{name}'" for name in _CHECK_TOOLS)
    async with db.connection() as conn:
        report_row = await (
            await conn.execute(
                "SELECT session_id, timestamp, meta FROM agent_thought_log "
                f"WHERE {' AND '.join(where)} ORDER BY timestamp DESC LIMIT 1",
                params,
            )
        ).fetchone()
        if not report_row:
            return None
        session, at = report_row["session_id"], report_row["timestamp"]
        # This review began with the orchestrator's call; an earlier review in
        # the same session had its own check.
        started = await (
            await conn.execute(
                "SELECT MAX(timestamp) AS at FROM agent_thought_log WHERE session_id = ? "
                "AND event_type = 'tool_call' AND content = 'risk_manager' AND timestamp <= ?",
                (session, at),
            )
        ).fetchone()
        began = (started["at"] if started else None) or ""
        latest = {}
        for event in ("tool_call", "tool_response"):
            row = await (
                await conn.execute(
                    "SELECT meta FROM agent_thought_log WHERE session_id = ? "
                    f"AND event_type = ? AND content IN ({tools}) "
                    "AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
                    (session, event, began, at),
                )
            ).fetchone()
            latest[event] = _meta(row["meta"]) if row else {}
    report = _report_text(_meta(report_row["meta"]))
    check = latest["tool_response"].get("response")
    violations = check.get("violations") if isinstance(check, dict) else None
    args = latest["tool_call"].get("args")
    return {
        "session_id": session,
        "at": at,
        "verdict": verdict_of(report),
        "trade": args if isinstance(args, dict) and args else None,
        "violations": [str(v) for v in violations] if isinstance(violations, list) else [],
        "reasons": reasons_of(report),
    }


def _describe(trade: dict[str, Any] | None) -> str:
    """'OPEN LONG 20 XYZ @ 12.5, stop 9.0', or 'the proposal' without the check's arguments."""
    if not trade:
        return "the proposal"
    action = str(trade.get("action") or "OPEN").upper()
    direction = str(trade.get("direction") or "").upper()
    ticker = trade.get("ticker") or "?"
    if "option_type" in trade:
        text = (
            f"{action} {direction} {trade.get('contracts')}x {ticker} {trade.get('strike')} "
            f"{trade.get('option_type')} {trade.get('expiration')} @ "
            f"{trade.get('premium_per_contract')}"
        )
    else:
        text = f"{action} {direction} {trade.get('quantity')} {ticker} @ {trade.get('price')}"
    if trade.get("stop_price") is not None:
        text += f", stop {trade['stop_price']}"
    return " ".join(text.split())


def rejection_line(review: dict[str, Any]) -> str:
    """The SYSTEM line for a REJECTED review: what, when, that nothing was sent, and why."""
    from evotrader.tools.market_hours import ET

    try:
        stamp = datetime.fromisoformat(str(review["at"])).astimezone(ET).strftime("%H:%M ET")
    except (KeyError, TypeError, ValueError):
        stamp = "an earlier cycle"
    why = "; ".join(review.get("violations") or []) or review.get("reasons") or "none given"
    if len(why) > REASONS_CHARS:
        why = why[: REASONS_CHARS - 1].rstrip() + "…"
    session = str(review.get("session_id") or "")[:8]
    return (
        f"risk_manager REJECTED {_describe(review.get('trade'))} at {stamp} (session "
        f"{session}); nothing was sent to the broker. Reasons: {why}"
    )


async def note_rejection(db: Any, session_id: str, data_dir: Path) -> bool:
    """Append the SYSTEM line when this session's last risk review REJECTED. True if written."""
    from evotrader.tools.trading_handoff import append_system_line

    review = await latest_risk_review(db, session_id=session_id)
    if not review or review["verdict"] != "REJECTED":
        return False
    return append_system_line(data_dir, rejection_line(review))
