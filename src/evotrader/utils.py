"""Common utility functions for EvoTrader."""

import datetime
import json
import re
from typing import Any


def extract_balanced_bracket(s: str, open_b: str = "{", close_b: str = "}") -> str:
    """Extract the first balanced bracket structure (e.g. `{}` or `[]`) from a string.

    Brackets inside a JSON string value are text, not structure, and are not
    counted. Counting them closed a top-level array early whenever a value
    contained a half-open interval such as "(0, 5]": json.loads then reported
    an unterminated string at an unrelated offset, which hid the cause.
    """
    start = s.find(open_b)
    if start == -1:
        return s

    count = 0
    in_string = False
    escaped = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_b:
            count += 1
        elif ch == close_b:
            count -= 1
            if count == 0:
                return s[start : i + 1]
    return s[start:]


def clean_json_text(text: str) -> str:
    """Pre-clean JSON text from markdown fences and trailing characters."""
    if not text:
        return ""

    s = text.strip()

    # Strip markdown code blocks: ```json ... ``` or ``` ... ```
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", s, re.DOTALL)
    if match:
        s = match.group(1).strip()

    # Determine whether we should extract balanced curly braces or brackets
    idx_curly = s.find("{")
    idx_square = s.find("[")

    if idx_curly != -1 and (idx_square == -1 or idx_curly < idx_square):
        s = extract_balanced_bracket(s, "{", "}")
    elif idx_square != -1:
        s = extract_balanced_bracket(s, "[", "]")

    return s


def safe_parse_json(text: str) -> Any:
    """Parse JSON string reliably, handling markdown blocks and syntax anomalies.

    Text that is already valid JSON is parsed as it is. Extraction only runs for
    text that needs it (a code fence, prose around the JSON), so it can never
    damage a payload that was correct to begin with.
    """
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        pass
    cleaned = clean_json_text(text)
    return json.loads(cleaned)


def select_agentic_account(accounts: list[dict]) -> str | None:
    """Pick the correct account number from a list of Robinhood accounts.

    Selection priority:
        1. Account with ``agentic_allowed: true``
        2. Account marked ``is_default: true``
        3. First account in the list

    Returns:
        The account_number string, or None if the list is empty.
    """
    for a in accounts:
        if a.get("agentic_allowed", False):
            return a.get("account_number")
    for a in accounts:
        if a.get("is_default", False):
            return a.get("account_number")
    if accounts:
        return accounts[0].get("account_number")
    return None


def parse_occ_symbol(symbol: str) -> dict[str, str | float] | None:
    """Parse an OCC option symbol into its components.

    Args:
        symbol: OCC option symbol string (e.g., 'QQQ   240119P00300000' or 'AAPL240119C00300000').

    Returns:
        A dictionary with keys 'ticker', 'expiration', 'option_type', 'strike',
        or None if the symbol cannot be parsed.
    """
    if not symbol:
        return None

    match = re.search(r"([A-Z]+)\s*(\d{6})([CP])(\d{8})", symbol.upper())
    if not match:
        return None

    ticker = match.group(1).strip()
    exp_str = match.group(2)
    opt_type_char = match.group(3)
    strike_str = match.group(4)

    # Assume 20XX for the year
    expiration = f"20{exp_str[0:2]}-{exp_str[2:4]}-{exp_str[4:6]}"

    option_type = "call" if opt_type_char == "C" else "put"
    strike = float(strike_str) / 1000.0

    return {
        "ticker": ticker,
        "expiration": expiration,
        "option_type": option_type,
        "strike": strike,
    }


def get_period_cutoff_dt(
    period: str, tzinfo: datetime.tzinfo | None = None
) -> datetime.datetime | None:
    """Resolve a period string ('today', 'week', 'month') into a datetime cutoff.

    Args:
        period: The string period (e.g. 'week' for trailing 7 days).
        tzinfo: Timezone to use for 'now'.

    Returns:
        The cutoff datetime, or None if period is 'all' or unrecognized.
    """
    if not period or period == "all":
        return None

    now = datetime.datetime.now(tzinfo)

    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        return (now - datetime.timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        return (now - datetime.timedelta(days=30)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    return None


# ── Centralized UTC ↔ ET timestamp helpers ──────────────────────────
# Trade timestamps are stored as UTC ISO strings in the database.
# Period cutoffs and date grouping must work in Eastern Time.
# These helpers are the SINGLE SOURCE OF TRUTH for those conversions
# to prevent the recurring UTC/ET mismatch bug.


def _get_et() -> datetime.tzinfo:
    """Lazily import ET timezone to avoid circular imports."""
    from zoneinfo import ZoneInfo

    return ZoneInfo("America/New_York")


def utc_timestamp_to_et_date(timestamp: str) -> str:
    """Convert a UTC ISO timestamp string to an ET date string (YYYY-MM-DD).

    Use this whenever you need to attribute a trade or event to a calendar
    date in Eastern Time.  For example, ``2026-07-15T01:57:23+00:00``
    (1:57 AM UTC) is actually July 14 at 9:57 PM ET and returns
    ``'2026-07-14'``.

    Args:
        timestamp: ISO-8601 timestamp string (with or without TZ info).

    Returns:
        Date string in ``'YYYY-MM-DD'`` format, in Eastern Time.
    """
    dt = datetime.datetime.fromisoformat(timestamp)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt.astimezone(_get_et()).strftime("%Y-%m-%d")


def get_period_cutoff_utc_str(period: str) -> str | None:
    """Return the period cutoff as a UTC-formatted string for SQL queries.

    Trade timestamps in the DB are stored as UTC ISO strings.  This
    function converts the ET-based cutoff to a UTC string so that
    ``WHERE timestamp >= ?`` works correctly without manual conversion
    at every callsite.

    Args:
        period: Period string ('today', 'week', 'month', 'all').

    Returns:
        UTC cutoff string like ``'2026-07-08T04:00:00'``, or None for 'all'.
    """
    et = _get_et()
    cutoff_dt = get_period_cutoff_dt(period, et)
    if cutoff_dt is None:
        return None
    from zoneinfo import ZoneInfo

    cutoff_utc = cutoff_dt.astimezone(ZoneInfo("UTC"))
    return cutoff_utc.strftime("%Y-%m-%dT%H:%M:%S")
