from evotrader.models.mcp import McpEquityQuote, McpOptionQuote, McpPortfolio, McpPosition


def test_equity_quote_regular_hours():
    # Only regular hours price available
    quote = McpEquityQuote(last_trade_price=100.0)
    assert quote.resolve_live_price() == 100.0


def test_equity_quote_extended_hours():
    # Extended hours takes precedence if bid/ask is dead
    quote = McpEquityQuote(last_trade_price=100.0, last_extended_hours_trade_price=105.0)
    assert quote.resolve_live_price() == 105.0


def test_equity_quote_24_hour_market():
    # Bid/Ask spread takes highest precedence for 24-hour market
    quote = McpEquityQuote(
        last_trade_price=100.0,
        last_extended_hours_trade_price=105.0,
        bid_price=109.5,
        ask_price=110.5,
    )
    assert quote.resolve_live_price() == 110.0  # Mid price


def test_equity_quote_stale_bid_ask():
    # If ask or bid is 0, fall back to extended/regular
    quote = McpEquityQuote(
        last_trade_price=100.0,
        last_extended_hours_trade_price=105.0,
        bid_price=0.0,
        ask_price=110.5,
    )
    assert quote.resolve_live_price() == 105.0


def test_option_quote_regular_hours():
    # Only regular hours price available
    quote = McpOptionQuote(last_trade_price=2.50)
    assert quote.resolve_live_price() == 2.50


def test_option_quote_mark_price():
    # Adjusted mark takes precedence if bid/ask is dead
    quote = McpOptionQuote(last_trade_price=2.50, adjusted_mark_price=2.75)
    assert quote.resolve_live_price() == 2.75


def test_option_quote_bid_ask():
    # Bid/Ask spread takes highest precedence
    quote = McpOptionQuote(
        last_trade_price=2.50, adjusted_mark_price=2.75, bid_price=2.90, ask_price=3.10
    )
    assert quote.resolve_live_price() == 3.00


def test_portfolio_parsing():
    data = {
        "account_number": "ACC123",
        "total_value": "15000.50",
        "cash": 5000.50,
        "buying_power": {"buying_power": "4500.0"},
    }
    port = McpPortfolio.from_dict(data)
    assert port.account_number == "ACC123"
    assert port.total_value == 15000.50
    assert port.cash == 5000.50
    assert port.buying_power == 4500.0


def test_position_parsing_equity():
    data = {
        "symbol": "AAPL",
        "quantity": "50.0",
        "average_buy_price": "145.20",
        "asset_type": "EQUITY",
    }
    pos = McpPosition.from_dict(data)
    assert pos.symbol == "AAPL"
    assert pos.quantity == 50.0
    assert pos.average_price == 145.20
    assert pos.asset_type == "EQUITY"


def test_position_parsing_option():
    # Robinhood's average_price for options is per-contract total
    # ($5.50/share × 100 = $550.00). from_dict normalizes to per-share.
    data = {
        "symbol": "SPY",
        "quantity": "1.0",
        "average_price": "550.00",
        "option_id": "uuid-123",
        "type": "call",
        "strike_price": "500.0",
        "expiration_date": "2026-12-31",
    }
    pos = McpPosition.from_dict(data)
    assert pos.symbol == "SPY"
    assert pos.quantity == 1.0
    assert pos.average_price == 5.50
    assert pos.asset_type == "OPTION"
    assert pos.option_id == "uuid-123"
    assert pos.option_type == "call"
    assert pos.strike == 500.0
    assert pos.expiration == "2026-12-31"
