import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from evotrader.sim import SimBroker, SimBrokerProxy


@pytest.fixture
def temp_db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "sim_broker.db"


@pytest.fixture
async def sim_broker(temp_db_path):
    broker = SimBroker(temp_db_path)
    await broker.initialize()
    yield broker
    await broker.close()


@pytest.mark.asyncio
async def test_sim_broker_init(sim_broker):
    assert sim_broker.account_number == "SIM_AGENT_TRADER"
    accounts_res = await sim_broker.get_accounts()
    assert accounts_res["data"]["accounts"][0]["account_number"] == "SIM_AGENT_TRADER"
    assert accounts_res["data"]["accounts"][0]["agentic_allowed"] is True


@pytest.mark.asyncio
async def test_sim_broker_deposit_withdraw(sim_broker):
    # Check initial cash balance
    portfolio = await sim_broker.get_portfolio(sim_broker.account_number)
    assert portfolio["data"]["cash"] == 0.0

    # Deposit cash
    deposit_res = await sim_broker.deposit(1000.0)
    assert deposit_res["status"] == "success"
    assert deposit_res["cash_balance"] == 1000.0

    portfolio = await sim_broker.get_portfolio(sim_broker.account_number)
    assert portfolio["data"]["cash"] == 1000.0

    # Withdraw cash
    withdraw_res = await sim_broker.withdraw(400.0)
    assert withdraw_res["status"] == "success"
    assert withdraw_res["cash_balance"] == 600.0

    portfolio = await sim_broker.get_portfolio(sim_broker.account_number)
    assert portfolio["data"]["cash"] == 600.0

    # Error case: too much withdrawal
    with pytest.raises(ValueError):
        await sim_broker.withdraw(1000.0)


@pytest.mark.asyncio
async def test_place_equity_market_order(sim_broker):
    # Set default config
    sim_broker.slippage_model = "none"

    # Deposit cash
    await sim_broker.deposit(5000.0)

    # Mock real-time price resolution
    sim_broker._get_live_bid_ask = AsyncMock(return_value=(150.0, 150.0))

    # Place buy market order
    order_res = await sim_broker.place_equity_order(
        {"symbol": "QQQ", "side": "buy", "type": "market", "quantity": 10}
    )

    assert order_res["data"]["status"] == "filled"
    assert float(order_res["data"]["price"]) == 150.0

    # Verify positions and cash
    portfolio = await sim_broker.get_portfolio(sim_broker.account_number)
    assert portfolio["data"]["cash"] == 3500.0  # 5000 - 10 * 150

    positions = await sim_broker.get_equity_positions(sim_broker.account_number)
    assert len(positions["data"]["positions"]) == 1
    assert positions["data"]["positions"][0]["symbol"] == "QQQ"
    assert float(positions["data"]["positions"][0]["quantity"]) == 10.0

    # Place sell market order
    sim_broker._get_live_bid_ask = AsyncMock(return_value=(155.0, 155.0))
    sell_res = await sim_broker.place_equity_order(
        {"symbol": "QQQ", "side": "sell", "type": "market", "quantity": 5}
    )

    assert sell_res["data"]["status"] == "filled"
    assert float(sell_res["data"]["price"]) == 155.0

    # Re-check portfolio
    portfolio = await sim_broker.get_portfolio(sim_broker.account_number)
    assert portfolio["data"]["cash"] == 4275.0  # 3500 + 5 * 155

    positions = await sim_broker.get_equity_positions(sim_broker.account_number)
    assert float(positions["data"]["positions"][0]["quantity"]) == 5.0


@pytest.mark.asyncio
async def test_place_equity_limit_order(sim_broker):
    sim_broker.slippage_model = "none"
    await sim_broker.deposit(5000.0)

    # 1. Limit order that fills immediately
    sim_broker._get_live_bid_ask = AsyncMock(return_value=(145.0, 145.0))
    order_res = await sim_broker.place_equity_order(
        {"symbol": "QQQ", "side": "buy", "type": "limit", "limit_price": 146.0, "quantity": 10}
    )
    assert order_res["data"]["status"] == "filled"

    # 2. Limit order that is pending
    sim_broker._get_live_bid_ask = AsyncMock(return_value=(150.0, 150.0))
    order_res2 = await sim_broker.place_equity_order(
        {"symbol": "QQQ", "side": "buy", "type": "limit", "limit_price": 140.0, "quantity": 10}
    )
    assert order_res2["data"]["status"] == "pending"

    # Re-check cash & buying power (buying power blocks the cash for pending buys)
    port = await sim_broker.get_portfolio(sim_broker.account_number)
    assert port["data"]["cash"] == 3550.0  # 5000 - 10 * 145 (filled buy)
    assert (
        port["data"]["buying_power"]["buying_power"] == 2150.0
    )  # cash - blocked buy limit (10 * 140)

    # Cancel pending order
    cancel_res = await sim_broker.cancel_order(order_res2["data"]["id"])
    assert cancel_res["data"]["status"] == "cancelled"

    port = await sim_broker.get_portfolio(sim_broker.account_number)
    assert port["data"]["buying_power"]["buying_power"] == 3550.0  # Cash unblocked


@pytest.mark.asyncio
async def test_fill_pending_limit_order(sim_broker):
    sim_broker.slippage_model = "none"
    await sim_broker.deposit(5000.0)

    # Mock real-time price resolution: initial price high, so buy limit order does not fill immediately
    sim_broker._get_live_bid_ask = AsyncMock(return_value=(150.0, 150.0))
    order_res = await sim_broker.place_equity_order(
        {"symbol": "QQQ", "side": "buy", "type": "limit", "limit_price": 140.0, "quantity": 10}
    )
    assert order_res["data"]["status"] == "pending"

    # Now, change bid/ask to be low enough so that the limit order crosses and gets filled
    sim_broker._get_live_bid_ask = AsyncMock(return_value=(135.0, 135.0))

    # Trigger processing of pending orders (usually done during common queries/actions)
    await sim_broker._process_pending_orders_and_expirations()

    # Verify that the order is filled and status is updated
    orders = await sim_broker.get_orders({})
    results = orders["data"]["results"]
    assert len(results) == 1
    assert results[0]["status"] == "filled"
    assert float(results[0]["price"]) == 135.0

    # Verify that the position was correctly created
    positions = await sim_broker.get_equity_positions(sim_broker.account_number)
    assert len(positions["data"]["positions"]) == 1
    assert positions["data"]["positions"][0]["symbol"] == "QQQ"
    # Verify cash and buying power
    port = await sim_broker.get_portfolio(sim_broker.account_number)
    assert port["data"]["cash"] == 3650.0
    assert port["data"]["buying_power"]["buying_power"] == 3650.0


@pytest.mark.asyncio
async def test_place_option_order(sim_broker):
    sim_broker.slippage_model = "none"
    await sim_broker.deposit(5000.0)

    sim_broker._get_live_option_bid_ask = AsyncMock(return_value=(1.50, 1.50))

    # Place buy option order
    order_res = await sim_broker.place_option_order(
        {
            "legs": [{"option_id": "opt_xyz", "side": "buy", "position_effect": "open"}],
            "type": "market",
            "quantity": 2,
        }
    )

    assert order_res["data"]["status"] == "filled"

    # Re-check portfolio
    port = await sim_broker.get_portfolio(sim_broker.account_number)
    # Option uses 100x multiplier. Cost = 2 * 1.50 * 100 = 300.
    assert port["data"]["cash"] == 4700.0

    positions = await sim_broker.get_option_positions(sim_broker.account_number)
    assert len(positions["data"]["positions"]) == 1
    assert positions["data"]["positions"][0]["option_id"] == "opt_xyz"
    assert float(positions["data"]["positions"][0]["quantity"]) == 2.0


@pytest.mark.asyncio
async def test_auto_close_expired_options(sim_broker):
    sim_broker.slippage_model = "none"
    sim_broker.auto_close_expired_options = True
    await sim_broker.deposit(5000.0)

    # Directly insert an expired option position into the DB
    conn = await sim_broker._get_conn()
    await conn.execute(
        """
        INSERT INTO sim_positions (
            account_number, asset_type, ticker, option_id, quantity, avg_cost_basis,
            option_type, strike, expiration
        ) VALUES (?, 'OPTION', 'SPY', 'opt_spy_expired', 1, 1.50, 'call', 400.0, '2020-01-01')
        """,
        (sim_broker.account_number,),
    )
    await conn.commit()

    # Mock stock price resolver to return 410.0 (intrinsic value = 10.0)
    sim_broker._get_live_price = AsyncMock(return_value=410.0)

    # Process pending orders / auto close
    await sim_broker._process_pending_orders_and_expirations()

    # Check cash balance: 5000 + 1 * 10.0 * 100 = 6000
    port = await sim_broker.get_portfolio(sim_broker.account_number)
    assert port["data"]["cash"] == 6000.0

    # Ensure position is gone
    positions = await sim_broker.get_option_positions(sim_broker.account_number)
    assert len(positions["data"]["positions"]) == 0


@pytest.mark.asyncio
async def test_sim_proxy_routing(sim_broker):
    # Mock real toolset session and call_tool
    mock_session = AsyncMock()
    mock_session.call_tool = AsyncMock(
        return_value=MagicMock(content=[MagicMock(text='{"data": {"live": true}}')], isError=False)
    )

    mock_manager = AsyncMock()
    mock_manager.create_session = AsyncMock(return_value=mock_session)

    mock_toolset = MagicMock()
    mock_toolset._mcp_session_manager = mock_manager

    # Initialize proxy
    proxy = SimBrokerProxy(sim_broker, real_mcp_toolset=mock_toolset)
    proxy.attach_to_toolset(mock_toolset)

    # Deposit cash to ensure order is approved
    await sim_broker.deposit(5000.0)

    # Create session through patched manager
    wrapped_session = await mock_toolset._mcp_session_manager.create_session()

    # 1. Call intercepted tool (should go to SimBroker)
    res = await wrapped_session.call_tool("get_accounts", {})
    import json

    data = json.loads(res.content[0].text)
    assert data["data"]["accounts"][0]["account_number"] == "SIM_AGENT_TRADER"

    # Verify model_dump capability
    assert hasattr(res, "model_dump")
    dumped = res.model_dump()
    assert dumped["isError"] is False
    assert len(dumped["content"]) == 1
    assert dumped["content"][0]["type"] == "text"
    assert "SIM_AGENT_TRADER" in dumped["content"][0]["text"]

    # 1.5. Call intercepted review order tool
    res_review = await wrapped_session.call_tool(
        "review_equity_order",
        {
            "symbol": "QQQ",
            "side": "buy",
            "quantity": 2,
            "limit_price": 300.0,
        },
    )
    review_data = json.loads(res_review.content[0].text)
    assert review_data["data"]["status"] == "approved"
    assert float(review_data["data"]["estimated_cost"]) == 600.0

    # 2. Call non-intercepted tool (should pass through to real MCP)
    res_real = await wrapped_session.call_tool("get_equity_quotes", {"symbols": ["SPY"]})
    data_real = json.loads(res_real.content[0].text)
    assert data_real["data"]["live"] is True
    mock_session.call_tool.assert_called_with("get_equity_quotes", arguments={"symbols": ["SPY"]})

    # 3. Call blocked/unimplemented tool (should fail instead of passing through)
    res_blocked = await wrapped_session.call_tool("place_crypto_order", {"symbol": "BTC"})
    assert res_blocked.isError is True
    blocked_data = json.loads(res_blocked.content[0].text)
    assert blocked_data["data"] is None
    assert "not supported in simulation mode" in blocked_data["error"]


@pytest.mark.asyncio
async def test_review_equity_order(sim_broker):
    await sim_broker.deposit(5000.0)

    # 1. Successful review
    review_res = await sim_broker.review_equity_order(
        {
            "symbol": "QQQ",
            "side": "buy",
            "quantity": 2.5,
            "limit_price": 400.0,
            "type": "limit",
        }
    )

    assert review_res["data"]["status"] == "approved"
    assert float(review_res["data"]["estimated_cost"]) == 1000.0

    # Verify no order was written to database
    orders = await sim_broker.get_orders({})
    assert len(orders.get("data", {}).get("results", [])) == 0

    # 2. Rejected review due to buying power
    review_rej = await sim_broker.review_equity_order(
        {
            "symbol": "QQQ",
            "side": "buy",
            "quantity": 15.0,
            "limit_price": 400.0,
            "type": "limit",
        }
    )
    assert review_rej["data"]["status"] == "rejected"
    assert "Insufficient buying power" in review_rej["data"]["reason"]


@pytest.mark.asyncio
async def test_review_option_order(sim_broker):
    await sim_broker.deposit(5000.0)

    # 1. Successful option review
    review_res = await sim_broker.review_option_order(
        {
            "legs": [
                {
                    "option_id": "opt_xyz",
                    "side": "buy",
                }
            ],
            "quantity": 2,
            "price": 1.50,
        }
    )

    assert review_res["data"]["status"] == "approved"
    # Option multiplier: 2 * 1.50 * 100 = 300
    assert float(review_res["data"]["estimated_cost"]) == 300.0

    # Verify no position or order created
    positions = await sim_broker.get_option_positions(sim_broker.account_number)
    assert len(positions["data"]["positions"]) == 0
