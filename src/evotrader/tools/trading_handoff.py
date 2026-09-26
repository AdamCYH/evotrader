"""The trading agent's note to its own next cycle.

Each cycle starts from a brand-new ADK session, so nothing survives by default,
and the trading agents have no tool that reads their own past reasoning — only
the evolution agent gets the cycle digest. What continuity exists today is
``store_learning``, which is opt-in and recalled by semantic similarity rather
than recency, so "what did I decide an hour ago" can return a note from a
fortnight ago about a different setup.

This is the other half: one short note, rewritten in full every cycle, injected
into the next cycle's prompt whether or not the agent thinks to look for it.

Design, and why it differs from the evolution agent's carry-forward file:

* **Full rewrite, never append.** Evolution runs weekly and a human prunes
  completed items between runs. Nothing prunes this nine times a day, and a
  leftover line ("stop at 149.80") is wrong money once the position moves.
  Replacing the whole note each cycle makes staleness structurally impossible.
* **Stamped with author, time and cycle.** A cycle that dies midway leaves the
  previous note in place; the reader has to be able to see how old it is and
  which cycle wrote it. The session id ties it back to the thought log.
* **Notes, not instructions.** The header says so explicitly. This is one
  agent's account of what it saw, not a directive — the new cycle re-reads
  broker truth and decides for itself. A stale note that reads as a command is
  the failure mode worth designing against.
* **Machines write here too.** ``SYSTEM:`` lines are appended by post-cycle
  checks that run in a ``finally`` block, so they land even when the agent
  crashed before writing its own part. An agent can forget; the audit cannot.

Stored outside ``data/notes/``, which is ingested into semantic memory at
startup — a handoff there would pollute every search and never expire. It lives
in its own directory that the notes UI already knows how to list.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

HANDOFF_FILENAME = "trading_handoff.md"
#: Past this, position state has very likely moved underneath the note.
STALE_AFTER_HOURS = 18.0
#: A hard ceiling, not a target. The handoff is prepended to the next cycle's
#: prompt, so every character here is context the next agent reads before its
#: own fresh evidence — a long note argues with the reader instead of briefing
#: it. Asking for brevity in the tool description is necessary but not
#: sufficient; this is the backstop.
_MAX_CHARS = 1200
#: Separates the human-readable heading (for the notes UI) from the note itself.
#: Without it the heading is re-injected into the prompt alongside the framing
#: that render_for_prompt already adds, and the agent reads the same sentence twice.
_BODY_MARKER = "<!-- handoff-body -->"


def handoff_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "trading" / "notes"


def handoff_path(data_dir: Path) -> Path:
    return handoff_dir(data_dir) / HANDOFF_FILENAME


def write_handoff(
    data_dir: Path,
    body: str,
    *,
    agent: str = "strategy",
    cycle: str = "",
    session_id: str = "",
    now: datetime | None = None,
) -> Path:
    """Replace the handoff with *body*. Never appends."""
    from evotrader.tools.market_hours import _resolve_now

    now = _resolve_now(now)
    text = (body or "").strip()
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS] + "\n…[truncated]"

    header = [
        "---",
        "category: trading_handoff",
        f"written_at: '{now.isoformat()}'",
        f"written_by: {agent}",
        f"cycle: '{cycle}'" if cycle else "cycle: ''",
        f"session_id: '{session_id}'",
        "---",
        "",
        f"# Trading handoff — {agent} at {now:%Y-%m-%d %H:%M} ET"
        + (f" (cycle {cycle})" if cycle else ""),
        "",
        "Notes from the previous cycle, not instructions.",
        "",
        _BODY_MARKER,
        "",
    ]
    path = handoff_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(header) + text + "\n", encoding="utf-8")
    return path


def append_system_line(data_dir: Path, line: str) -> bool:
    """Add a machine-written ``SYSTEM:`` line to the current handoff.

    Used by post-cycle checks, which run in a ``finally`` block and therefore
    still report when the agent crashed before writing anything.
    """
    path = handoff_path(data_dir)
    entry = f"SYSTEM: {line.strip()}"
    try:
        if not path.is_file():
            write_handoff(data_dir, entry, agent="system")
            return True
        existing = path.read_text(encoding="utf-8")
        if entry in existing:
            return False
        path.write_text(existing.rstrip("\n") + "\n" + entry + "\n", encoding="utf-8")
        return True
    except Exception as e:
        logger.warning("Could not append a SYSTEM line to the handoff: %s", e)
        return False


def read_handoff(data_dir: Path, now: datetime | None = None) -> dict[str, Any] | None:
    """The current handoff plus its age, or None if there is not one."""
    path = handoff_path(data_dir)
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("Could not read the trading handoff: %s", e)
        return None

    from evotrader.tools.market_hours import ET, _resolve_now

    meta: dict[str, str] = {}
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            for line in parts[1].splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip().strip("'\"")
            body = parts[2]

    # Drop the display heading; only the note itself goes into a prompt.
    if _BODY_MARKER in body:
        body = body.split(_BODY_MARKER, 1)[1]

    written_at = None
    if meta.get("written_at"):
        try:
            written_at = datetime.fromisoformat(meta["written_at"])
        except ValueError:
            written_at = None

    now = _resolve_now(now)
    age_hours = None
    if written_at is not None:
        if written_at.tzinfo is None:
            written_at = written_at.replace(tzinfo=ET)
        age_hours = (now - written_at).total_seconds() / 3600.0

    return {
        "body": body.strip(),
        "written_at": meta.get("written_at", ""),
        "written_by": meta.get("written_by", "unknown"),
        "cycle": meta.get("cycle", ""),
        "session_id": meta.get("session_id", ""),
        "age_hours": age_hours,
        "stale": age_hours is not None and age_hours > STALE_AFTER_HOURS,
    }


def render_for_prompt(data_dir: Path, now: datetime | None = None) -> str:
    """The block injected into the next cycle's prompt. Empty when there is none."""
    from evotrader.tools.market_hours import ET

    note = read_handoff(data_dir, now=now)
    if not note or not note["body"]:
        return ""

    age = note["age_hours"]
    if age is None:
        when = "time unknown"
    elif age < 1:
        when = f"{age * 60:.0f} minutes ago"
    else:
        when = f"{age:.1f} hours ago"

    stamp = ""
    if note["written_at"]:
        try:
            stamp = (
                datetime.fromisoformat(note["written_at"])
                .astimezone(ET)
                .strftime("%Y-%m-%d %I:%M %p ET")
                + ", "
            )
        except ValueError:
            stamp = ""

    lines = [
        "<trading_handoff>",
        f"Written by {note['written_by']}"
        + (f" (cycle {note['cycle']})" if note["cycle"] else "")
        + f" at {stamp}{when}. Same Eastern clock as the temporal context above.",
        "",
        "These are NOTES from a previous cycle, not instructions. They record what "
        "one agent saw and intended; they do not authorise anything and they may "
        "already be wrong. Read broker truth this cycle and decide for yourself.",
    ]
    if note["stale"]:
        lines += [
            "",
            f"STALE — written {when}, likely across a session boundary. Position "
            "state has probably moved since. Verify every fact below against the "
            "broker before relying on any of it.",
        ]
    lines += ["", note["body"], "</trading_handoff>"]
    return "\n".join(lines)
