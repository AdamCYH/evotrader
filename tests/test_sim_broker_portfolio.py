import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from evotrader.sim.sim_broker import SimBroker


@pytest_asyncio.fixture
async def broker():
    # Use a temporary file for SQLite to test schema setup and queries
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "sim_broker.db"
        b = SimBroker(db_path=db_path)

        # Mock price fetchers to return deterministic prices
        b._get_live_price = AsyncMock(return_value=100.0)
        b._get_live_option_price = AsyncMock(return_value=5.0)
        b._get_live_bid_ask = AsyncMock(return_value=(100.0, 100.0))
        b._get_live_option_bid_ask = AsyncMock(return_value=(5.0, 5.0))

        await b.initialize()

        # Override initial cash for testing
        conn = await b._get_conn()
        await conn.execute(
            "UPDATE sim_accounts SET cash_balance = 10000.0 WHERE account_number = ?",
            (b.account_number,),
        )
        await conn.commit()

        yield b

        # Cleanup
        if b._connection:
            await b._connection.close()


@pytest.mark.asyncio
async def test_empty_portfolio(broker):
    port = await broker.get_portfolio(broker.account_number)
    data = port["data"]
    assert data["cash"] == 10000.0
    assert data["total_value"] == 10000.0
    assert data["buying_power"]["buying_power"] == 10000.0


@pytest.mark.asyncio
async def test_long_equity_portfolio(broker):
    conn = await broker._get_conn()
    await conn.execute(
        "INSERT INTO sim_positions (account_number, ticker, asset_type, quantity, avg_cost_basis) VALUES (?, ?, ?, ?, ?)",
        (broker.account_number, "AAPL", "EQUITY", 10.0, 95.0),
    )
    await conn.commit()

    port = await broker.get_portfolio(broker.account_number)
    data = port["data"]

    # 10 shares * 100.0 (mock price) = 1000.0
    assert data["cash"] == 10000.0
    assert data["total_value"] == 11000.0
    assert data["buying_power"]["buying_power"] == 10000.0


@pytest.mark.asyncio
async def test_short_equity_portfolio(broker):
    conn = await broker._get_conn()
    await conn.execute(
        "INSERT INTO sim_positions (account_number, ticker, asset_type, quantity, avg_cost_basis) VALUES (?, ?, ?, ?, ?)",
        (broker.account_number, "QQQ", "EQUITY", -5.0, 105.0),
    )
    await conn.commit()

    port = await broker.get_portfolio(broker.account_number)
    data = port["data"]

    # -5 shares * 100.0 (mock price) = -500.0
    # margin_blocked = abs(-500) * 2.0 = 1000.0
    assert data["cash"] == 10000.0
    assert data["total_value"] == 9500.0
    assert data["buying_power"]["buying_power"] == 9000.0  # 10000 - 1000


@pytest.mark.asyncio
async def test_long_option_portfolio(broker):
    conn = await broker._get_conn()
    await conn.execute(
        "INSERT INTO sim_positions (account_number, ticker, asset_type, option_id, option_type, quantity, avg_cost_basis) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (broker.account_number, "SPY", "OPTION", "uuid-123", "call", 2.0, 4.0),
    )
    await conn.commit()

    port = await broker.get_portfolio(broker.account_number)
    data = port["data"]

    # 2 contracts * 5.0 (mock price) * 100 = 1000.0
    assert data["cash"] == 10000.0
    assert data["total_value"] == 11000.0
    assert data["buying_power"]["buying_power"] == 10000.0


@pytest.mark.asyncio
async def test_pending_buy_order_blocks_cash(broker):
    conn = await broker._get_conn()
    # Add a pending buy order for 5 shares at limit price 90.0
    await conn.execute(
        "INSERT INTO sim_orders (id, account_number, ticker, asset_type, side, quantity, limit_price, status, order_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("order-1", broker.account_number, "MSFT", "EQUITY", "buy", 5.0, 90.0, "pending", "limit"),
    )
    await conn.commit()

    port = await broker.get_portfolio(broker.account_number)
    data = port["data"]

    # blocked cash = 5 * 90.0 = 450.0
    assert data["cash"] == 10000.0
    assert data["total_value"] == 10000.0
    assert data["buying_power"]["buying_power"] == 9550.0


@pytest.mark.asyncio
async def test_complex_portfolio(broker):
    conn = await broker._get_conn()
    # Long Equity: 10 * 100 = 1000
    await conn.execute(
        "INSERT INTO sim_positions (account_number, ticker, asset_type, quantity, avg_cost_basis) VALUES (?, ?, ?, ?, ?)",
        (broker.account_number, "AAPL", "EQUITY", 10.0, 95.0),
    )
    # Short Equity: -5 * 100 = -500 (margin 1000)
    await conn.execute(
        "INSERT INTO sim_positions (account_number, ticker, asset_type, quantity, avg_cost_basis) VALUES (?, ?, ?, ?, ?)",
        (broker.account_number, "QQQ", "EQUITY", -5.0, 105.0),
    )
    # Long Option: 1 * 5.0 * 100 = 500
    await conn.execute(
        "INSERT INTO sim_positions (account_number, ticker, asset_type, option_id, option_type, quantity, avg_cost_basis) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (broker.account_number, "SPY", "OPTION", "uuid-123", "call", 1.0, 4.0),
    )
    # Pending Option Buy: 2 * 3.0 * 100 = 600 blocked
    await conn.execute(
        "INSERT INTO sim_orders (id, account_number, ticker, asset_type, side, quantity, limit_price, status, order_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("order-2", broker.account_number, "SPY", "OPTION", "buy", 2.0, 3.0, "pending", "limit"),
    )
    await conn.commit()

    port = await broker.get_portfolio(broker.account_number)
    data = port["data"]

    # Total positions = 1000 - 500 + 500 = 1000
    # Total value = 10000 + 1000 = 11000
    # Blocked cash = 600 (from pending order)
    # Margin blocked = 1000 (from short)
    # Buying power = 10000 - 600 - 1000 = 8400

    assert data["cash"] == 10000.0
    assert data["total_value"] == 11000.0
    assert data["buying_power"]["buying_power"] == 8400.0
