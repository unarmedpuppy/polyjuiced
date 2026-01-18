"""Integration tests for complete arbitrage execution flow.

Tests the full execution path from opportunity detection through
order placement and fill handling.
"""

import pytest
from tests.fixtures import (
    MockPolymarketClient,
    MockPolymarketWebSocket,
    MockDatabase,
    ScenarioRunner,
    MARKETS,
    EXECUTION_SCENARIOS,
    COMPLETE_SCENARIOS,
)


class TestArbitrageExecution:
    """End-to-end arbitrage execution tests."""

    @pytest.mark.asyncio
    async def test_perfect_execution_3c_spread(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Both legs fill perfectly - standard arbitrage."""
        # Setup
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["perfect_fill"])

        # Execute
        result = await runner.run_opportunity(budget=10.0)

        # Assert execution success
        assert result.success is True
        assert result.partial_fill is False
        assert result.error is None

        # Verify both legs filled
        assert result.yes_filled_size > 0
        assert result.no_filled_size > 0
        assert result.hedge_ratio == 1.0

        # Verify trade recorded
        runner.assert_trade_recorded(
            expected_status="full_fill",
            expected_hedge_ratio=1.0,
        )

        # Verify order placement
        mock_client.assert_order_placed(
            token_id=MARKETS["btc_3c_spread"].yes_token_id,
        )

    @pytest.mark.asyncio
    async def test_perfect_execution_4c_spread(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """4 cent spread - higher profit margin."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_4c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["perfect_fill"])

        result = await runner.run_opportunity(budget=10.0)

        assert result.success is True
        assert result.hedge_ratio == 1.0

        # Higher spread should mean higher expected profit
        trades = runner.get_recorded_trades()
        assert len(trades) == 1
        assert trades[0]["expected_profit"] > 0

    @pytest.mark.asyncio
    async def test_eth_market_execution(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Test ETH market execution (different asset)."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["eth_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["perfect_fill"])

        result = await runner.run_opportunity(budget=10.0)

        assert result.success is True

        trades = runner.get_recorded_trades()
        assert trades[0]["asset"] == "ETH"

    @pytest.mark.asyncio
    async def test_large_order_execution(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Larger order size fills completely."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["perfect_fill_large"])

        result = await runner.run_opportunity(budget=50.0)

        assert result.success is True
        # Larger fills
        assert result.yes_filled_size >= 50.0
        assert result.no_filled_size >= 50.0


class TestOpportunityValidation:
    """Tests for pre-trade opportunity validation."""

    @pytest.mark.asyncio
    async def test_no_spread_opportunity_detected(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """When spread is negative, execution still attempts but likely fails."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_no_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["both_rejected"])

        result = await runner.run_opportunity(budget=10.0)

        # With no/negative spread, orders should fail
        assert result.success is False

    @pytest.mark.asyncio
    async def test_multiple_opportunities_sequential(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Execute multiple opportunities in sequence."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)

        # First trade - BTC
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["perfect_fill"])
        result1 = await runner.run_opportunity(budget=10.0)
        assert result1.success is True

        # Second trade - ETH (without full reset)
        await runner.setup_market(MARKETS["eth_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["perfect_fill"])
        result2 = await runner.run_opportunity(budget=10.0)
        assert result2.success is True

        # Both trades recorded
        all_results = runner.get_all_results()
        assert len(all_results) == 2


class TestOrderExecution:
    """Tests for order execution specifics."""

    @pytest.mark.asyncio
    async def test_order_parameters_correct(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Verify correct parameters passed to order execution."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        market = MARKETS["btc_3c_spread"]
        await runner.setup_market(market)
        await runner.configure_execution(EXECUTION_SCENARIOS["perfect_fill"])

        await runner.run_opportunity(budget=10.0)

        # Get the order call details
        call = mock_client.get_last_dual_leg_call()
        assert call is not None
        assert call["yes_token_id"] == market.yes_token_id
        assert call["no_token_id"] == market.no_token_id
        assert call["yes_price"] == market.yes_best_ask
        assert call["no_price"] == market.no_best_ask

    @pytest.mark.asyncio
    async def test_order_books_queried(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Order books should be available for price queries."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        market = MARKETS["btc_3c_spread"]
        await runner.setup_market(market)

        # Query order book directly
        yes_book = await mock_client.get_order_book(market.yes_token_id)
        no_book = await mock_client.get_order_book(market.no_token_id)

        # Should have data
        assert len(yes_book["asks"]) > 0
        assert len(no_book["asks"]) > 0


class TestCallbackIntegration:
    """Tests for runner callback functionality."""

    @pytest.mark.asyncio
    async def test_opportunity_callback_fired(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Opportunity detected callback is called."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["perfect_fill"])

        # Track callback
        callback_fired = {"value": False}
        callback_market = {"value": None}

        def on_opportunity(market):
            callback_fired["value"] = True
            callback_market["value"] = market

        runner.on_opportunity_detected(on_opportunity)
        await runner.run_opportunity(budget=10.0)

        assert callback_fired["value"] is True
        assert callback_market["value"].asset == "BTC"

    @pytest.mark.asyncio
    async def test_trade_executed_callback_fired(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Trade executed callback is called with trade details."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["perfect_fill"])

        # Track callback
        callback_data = {"trade_id": None, "result": None}

        def on_trade(trade_id, result):
            callback_data["trade_id"] = trade_id
            callback_data["result"] = result

        runner.on_trade_executed(on_trade)
        await runner.run_opportunity(budget=10.0)

        assert callback_data["trade_id"] is not None
        assert callback_data["result"]["success"] is True


class TestCompleteScenarios:
    """Tests using pre-built complete scenarios."""

    @pytest.mark.asyncio
    async def test_standard_arb_success_scenario(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Run standard success scenario end-to-end."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        scenario = COMPLETE_SCENARIOS["standard_arb_success"]

        await runner.setup_complete_scenario(scenario)
        result = await runner.run_opportunity(budget=scenario.budget)

        assert result.success is True
        assert result.hedge_ratio == scenario.expected_final_hedge_ratio

        trades = runner.get_recorded_trades()
        assert len(trades) == scenario.expected_trade_count

    @pytest.mark.asyncio
    async def test_scenario_with_all_assertions(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Full scenario with all assertions."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["perfect_fill"])

        result = await runner.run_opportunity(budget=10.0)

        # Use runner assertions
        runner.assert_execution_matches(EXECUTION_SCENARIOS["perfect_fill"])
        runner.assert_trade_recorded(
            expected_status="full_fill",
            expected_hedge_ratio=1.0,
        )
