"""Tests for the market hours utility."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from evotrader.tools.market_hours import (
    MarketSession,
    get_current_session,
    is_any_session_active,
    is_market_open,
)

ET = ZoneInfo("America/New_York")


class TestMarketHours:
    def test_regular_hours(self) -> None:
        # Tuesday at 2:30 PM ET
        dt = datetime(2026, 6, 16, 14, 30, tzinfo=ET)
        assert get_current_session(dt) == MarketSession.REGULAR
        assert is_market_open(dt) is True

    def test_pre_market(self) -> None:
        # Tuesday at 7:00 AM ET
        dt = datetime(2026, 6, 16, 7, 0, tzinfo=ET)
        assert get_current_session(dt) == MarketSession.PRE_MARKET
        assert is_market_open(dt) is False
        assert is_any_session_active(dt) is True

    def test_after_hours(self) -> None:
        # Tuesday at 5:00 PM ET
        dt = datetime(2026, 6, 16, 17, 0, tzinfo=ET)
        assert get_current_session(dt) == MarketSession.AFTER_HOURS
        assert is_market_open(dt) is False
        assert is_any_session_active(dt) is True

    def test_closed_overnight(self) -> None:
        # Tuesday at 2:00 AM ET
        dt = datetime(2026, 6, 16, 2, 0, tzinfo=ET)
        assert get_current_session(dt) == MarketSession.CLOSED

    def test_closed_weekend(self) -> None:
        # Saturday at noon ET
        dt = datetime(2026, 6, 20, 12, 0, tzinfo=ET)
        assert get_current_session(dt) == MarketSession.CLOSED
        assert is_any_session_active(dt) is False

    def test_market_open_boundary(self) -> None:
        # Exactly 9:30 AM ET = regular session starts
        dt = datetime(2026, 6, 16, 9, 30, tzinfo=ET)
        assert get_current_session(dt) == MarketSession.REGULAR

    def test_market_close_boundary(self) -> None:
        # Exactly 4:00 PM ET = after-hours starts
        dt = datetime(2026, 6, 16, 16, 0, tzinfo=ET)
        assert get_current_session(dt) == MarketSession.AFTER_HOURS


class TestTwentyFourHourMarket:
    def test_overnight_weekdays(self) -> None:
        # Tuesday at 2:00 AM ET with 24h enabled
        dt = datetime(2026, 6, 16, 2, 0, tzinfo=ET)
        assert get_current_session(dt, twenty_four_hour=True) == MarketSession.OVERNIGHT
        assert is_any_session_active(dt, twenty_four_hour=True) is True

        # Monday at 10:00 PM ET with 24h enabled
        dt2 = datetime(2026, 6, 15, 22, 0, tzinfo=ET)
        assert get_current_session(dt2, twenty_four_hour=True) == MarketSession.OVERNIGHT
        assert is_any_session_active(dt2, twenty_four_hour=True) is True

    def test_overnight_weekends(self) -> None:
        # Sunday at 10:00 PM ET (24h market is open/overnight)
        dt = datetime(2026, 6, 21, 22, 0, tzinfo=ET)
        assert get_current_session(dt, twenty_four_hour=True) == MarketSession.OVERNIGHT

        # Sunday at 10:00 AM ET (24h market not yet open)
        dt2 = datetime(2026, 6, 21, 10, 0, tzinfo=ET)
        assert get_current_session(dt2, twenty_four_hour=True) == MarketSession.CLOSED

        # Friday at 10:00 PM ET (24h market closed for weekend)
        dt3 = datetime(2026, 6, 19, 22, 0, tzinfo=ET)
        assert get_current_session(dt3, twenty_four_hour=True) == MarketSession.CLOSED

    def test_backward_compatibility(self) -> None:
        # Default parameter (twenty_four_hour=False)
        dt = datetime(2026, 6, 16, 2, 0, tzinfo=ET)
        assert get_current_session(dt) == MarketSession.CLOSED
        assert is_any_session_active(dt) is False

    def test_helpers(self) -> None:
        from evotrader.tools.market_hours import (
            bind_extended_hours_tickers,
            get_allowed_order_types,
            is_twenty_four_hour_eligible,
        )

        # With nothing configured, the built-in fallback list applies.
        assert is_twenty_four_hour_eligible("QQQ") is True
        assert is_twenty_four_hour_eligible("SPY") is True
        assert is_twenty_four_hour_eligible("INVALID_TICKER") is False
        assert is_twenty_four_hour_eligible(None) is False

        # Configuration wins over the fallback, so switching instrument does not
        # require this module to know about the new symbol.
        bind_extended_hours_tickers(["MSTR"])
        assert is_twenty_four_hour_eligible("MSTR") is True
        assert is_twenty_four_hour_eligible("QQQ") is False
        bind_extended_hours_tickers(None)

        # An explicit set overrides both.
        assert is_twenty_four_hour_eligible("ANY", ["ANY"]) is True

        assert get_allowed_order_types("QQQ", MarketSession.OVERNIGHT) == ["limit"]
        assert get_allowed_order_types("QQQ", MarketSession.REGULAR) == [
            "market",
            "limit",
            "stop_loss",
            "stop_limit",
        ]
        assert get_allowed_order_types("QQQ", MarketSession.CLOSED) == []

    def test_trading_intervals(self) -> None:
        from evotrader.tools.market_hours import get_trading_interval

        dt = datetime(2026, 6, 16, 2, 0, tzinfo=ET)  # Tuesday 2:00 AM
        assert get_trading_interval(dt, twenty_four_hour=True, overnight_interval=3600) == 3600

    def test_seconds_until_next_session_24h(self) -> None:
        from evotrader.tools.market_hours import seconds_until_next_session

        # Friday 9:00 PM (CLOSED) to Sunday 8:00 PM (OVERNIGHT starts)
        dt = datetime(2026, 6, 19, 21, 0, tzinfo=ET)
        seconds = seconds_until_next_session(dt, twenty_four_hour=True)
        # Friday 9 PM to Sunday 8 PM = 47 hours = 169200 seconds
        assert seconds == 47 * 3600
