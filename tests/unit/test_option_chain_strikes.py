"""The option chain tool offers strikes around the money, however the broker pages them.

The broker lists an expiry's contracts from the lowest strike up, a page at a
time. With made-up numbers, these pin that: the offsets pick distinct strikes on
both sides of the money from a full list; the next-page link is followed
wherever the server puts it, and the walk stops once both sides are in hand; and
a list of one strike, or one that stops short of the money, is reported as the
broker's list stopping short rather than passed off as the chain.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from evotrader.agents import tools as tools_module

PRICE = 100.0
EXPIRY = "2026-11-20"
NEXT_LINK = "https://broker.example/options/instruments/?chain_symbol=QQQ&cursor=page-two"


def _instruments(strikes, types=("call", "put")) -> list[dict]:
    """A call and a put at each strike, as the broker lists them."""
    return [
        {
            "id": f"{t[0]}-{int(s)}",
            "type": t,
            "strike_price": f"{s:.4f}",
            "expiration_date": EXPIRY,
            "state": "active",
            "tradability": "tradable",
        }
        for s in strikes
        for t in types
    ]


def _equity_quote(price: float = PRICE) -> dict:
    return {
        "data": {
            "results": [
                {
                    "quote": {
                        "bid": str(price - 0.01),
                        "ask": str(price + 0.01),
                        "last_trade_price": str(price),
                        "last_extended_hours_trade_price": None,
                        "previous_close": str(price - 1),
                        "adjusted_previous_close": str(price - 1),
                        "volume": "1000",
                        "updated_at": "2026-10-20T14:30:00Z",
                    }
                }
            ]
        }
    }


def _option_quotes(args: dict) -> dict:
    return {
        "data": {
            "results": [
                {
                    "quote": {
                        "instrument_id": iid,
                        "bid_price": "1.00",
                        "ask_price": "1.20",
                        "adjusted_mark_price": "1.10",
                        "delta": "0.30",
                        "open_interest": "100",
                        "volume": "10",
                    }
                }
                for iid in args.get("instrument_ids", [])
            ]
        }
    }


def _fake_broker(pages: dict, calls: list[dict]):
    """``pages`` maps a cursor (None for the first page) to that page's response."""

    async def call(tool_name: str, args: dict) -> dict | None:
        if tool_name == "get_option_chains":
            return {"data": {"chains": [{"expiration_dates": [EXPIRY]}]}}
        if tool_name == "get_equity_quotes":
            return _equity_quote()
        if tool_name == "get_option_instruments":
            calls.append(dict(args))
            return pages[args.get("cursor")]
        if tool_name == "get_option_quotes":
            return _option_quotes(args)
        return None

    return call


async def _gather(pages: dict, calls: list[dict] | None = None) -> dict:
    calls = [] if calls is None else calls
    journal = AsyncMock()
    journal.get_open_trades = AsyncMock(return_value=[])
    with (
        patch.object(tools_module, "_call_mcp_tool", side_effect=_fake_broker(pages, calls)),
        patch.object(tools_module, "_journal", journal),
        patch.object(tools_module, "_config", None),
    ):
        return await tools_module.gather_option_chain("QQQ")


def _strikes(result: dict, side: str) -> list[float]:
    return sorted(c["strike"] for c in result["contracts"] if c["type"] == side)


def _chain_errors(result: dict) -> list[str]:
    return [e for e in (result["errors"] or []) if "strike" in e or "contracts" in e]


async def test_offsets_pick_distinct_strikes_around_the_money() -> None:
    result = await _gather({None: {"data": {"instruments": _instruments(range(80, 121))}}})

    assert _strikes(result, "call") == [100.0, 101.0, 102.0, 103.0, 105.0, 108.0]
    assert _strikes(result, "put") == [92.0, 95.0, 97.0, 98.0, 99.0, 100.0]
    assert result["contracts_count"] == 12
    assert result["strikes_fetched"] == {
        "pages": 1,
        "more_pages": False,
        "strikes": 41,
        "lowest_strike": 80.0,
        "highest_strike": 120.0,
    }
    assert _chain_errors(result) == []


@pytest.mark.parametrize(
    "page_one",
    [
        {"data": {"instruments": _instruments(range(80, 100)), "next": NEXT_LINK}},
        {"data": {"instruments": _instruments(range(80, 100))}, "next": NEXT_LINK},
        {"data": {"instruments": _instruments(range(80, 100)), "next_cursor": "page-two"}},
    ],
    ids=["next beside the list", "next at the top level", "bare next_cursor"],
)
async def test_pages_are_followed_until_both_sides_of_the_money_are_in_hand(page_one) -> None:
    calls: list[dict] = []
    pages = {
        None: page_one,
        "page-two": {
            "data": {
                "instruments": _instruments(range(100, 121)),
                "next": NEXT_LINK.replace("page-two", "page-three"),
            }
        },
        "page-three": {"data": {"instruments": _instruments(range(121, 141))}},
    }

    result = await _gather(pages, calls)

    # The third page is left alone: after two, strikes on both sides are in hand.
    assert [c.get("cursor") for c in calls] == [None, "page-two"]
    assert calls[1]["chain_symbol"] == "QQQ"
    assert calls[1]["expiration_dates"] == EXPIRY
    assert _strikes(result, "call") == [100.0, 101.0, 102.0, 103.0, 105.0, 108.0]
    assert _strikes(result, "put") == [92.0, 95.0, 97.0, 98.0, 99.0, 100.0]
    assert result["strikes_fetched"]["pages"] == 2
    assert result["strikes_fetched"]["more_pages"] is True
    assert _chain_errors(result) == []


async def test_a_list_of_one_strike_is_reported_as_such() -> None:
    result = await _gather({None: {"data": {"instruments": _instruments([80.0])}}})

    assert result["contracts_count"] == 2
    assert {c["strike"] for c in result["contracts"]} == {80.0}
    assert result["strikes_fetched"]["strikes"] == 1
    assert any(
        e.startswith(f"Only one strike (80.0) came back for {EXPIRY} (2 contract(s))")
        for e in result["errors"]
    )


async def test_a_list_that_stops_short_of_the_money_says_so() -> None:
    result = await _gather({None: {"data": {"instruments": _instruments(range(80, 91))}}})

    # Every offset collapses onto the highest strike there is, and the response says why.
    assert {c["strike"] for c in result["contracts"]} == {90.0}
    assert any(
        f"None of the 11 strikes the broker returned for {EXPIRY} (80.0–90.0, 1 page(s))" in e
        and "within 8% of the underlying (100.0)" in e
        for e in result["errors"]
    )


async def test_a_page_that_cannot_be_fetched_is_reported_and_the_rest_used() -> None:
    pages = {
        None: {"data": {"instruments": _instruments(range(80, 100)), "next": NEXT_LINK}},
        "page-two": None,
    }

    result = await _gather(pages)

    assert any(
        e.startswith(f"Page 2 of the {EXPIRY} contracts could not be fetched")
        for e in result["errors"]
    )
    assert result["strikes_fetched"]["pages"] == 1
    assert result["strikes_fetched"]["more_pages"] is True
    # The first page stops just under the price: the call side collapses onto its
    # highest strike, while the put side still finds its spread below the money.
    assert _strikes(result, "call") == [99.0]
    assert _strikes(result, "put") == [92.0, 95.0, 97.0, 98.0, 99.0]


def test_a_next_link_without_a_cursor_cannot_be_followed() -> None:
    raw = {
        "data": {"instruments": [], "next": "https://broker.example/options/instruments/?page=2"}
    }
    assert tools_module._next_page_cursor(raw) is None
    assert tools_module._next_page_cursor({"data": {"instruments": []}}) is None
