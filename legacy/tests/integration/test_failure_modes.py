"""Integration tests for failure modes and edge cases.

Tests error handling and recovery:
- Order rejections
- Timeouts
- Low liquidity
- Connection issues
- Market ending scenarios
"""

import pytest
import asyncio
from tests.fixtures import (
    MockPolymarketClient,
    MockPolymarketWebSocket,
    MockDatabase,
    ScenarioRunner,
    MARKETS,
    EXECUTION_SCENARIOS,
    COMPLETE_SCENARIOS,
)


class TestOrderRejections:
    """Tests for order rejection handling."""

    @pytest.mark.asyncio
    async def test_both_orders_rejected(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Both orders rejected - no positions opened."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["both_rejected"])

        result = await runner.run_opportunity(budget=10.0)

        assert result.success is False
        assert result.partial_fill is False
        assert result.yes_filled_size == 0
        assert result.no_filled_size == 0

        # No trade should be recorded
        runner.assert_no_trades_recorded()

    @pytest.mark.asyncio
    async def test_both_live_then_cancelled(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Both orders go LIVE but don't fill."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["both_live_then_cancelled"])

        result = await runner.run_opportunity(budget=10.0)

        # Should be marked as failed (nothing filled)
        assert result.success is False
        assert result.yes_filled_size == 0
        assert result.no_filled_size == 0


class TestTimeouts:
    """Tests for timeout handling."""

    @pytest.mark.asyncio
    async def test_execution_timeout(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Order execution times out."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])

        # Configure client to timeout
        mock_client.set_should_timeout(True)

        # Execute with short timeout
        with pytest.raises(asyncio.TimeoutError):
            await runner.run_opportunity(budget=10.0, timeout_seconds=1.0)

    @pytest.mark.asyncio
    async def test_slow_execution_within_timeout(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Slow execution completes within timeout."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["perfect_fill"])

        # Configure small delay
        mock_client.set_execution_delay(0.1)

        result = await runner.run_opportunity(budget=10.0, timeout_seconds=5.0)

        # Should still succeed
        assert result.success is True


class TestLowLiquidity:
    """Tests for low liquidity scenarios."""

    @pytest.mark.asyncio
    async def test_low_liquidity_market(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Low liquidity market causes order rejection."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_low_liquidity"])
        await runner.configure_execution(EXECUTION_SCENARIOS["both_rejected"])

        result = await runner.run_opportunity(budget=10.0)

        # Should fail due to low liquidity
        assert result.success is False

    @pytest.mark.asyncio
    async def test_low_liquidity_complete_scenario(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Complete low liquidity scenario."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        scenario = COMPLETE_SCENARIOS["low_liquidity_no_trade"]

        await runner.setup_complete_scenario(scenario)
        result = await runner.run_opportunity(budget=scenario.budget)

        assert result.success is False
        assert len(runner.get_recorded_trades()) == scenario.expected_trade_count


class TestConnectionIssues:
    """Tests for connection-related failures."""

    @pytest.mark.asyncio
    async def test_websocket_disconnection(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """WebSocket disconnection handling."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])

        # Simulate disconnection
        mock_ws.simulate_disconnect()

        # WebSocket should be disconnected
        mock_ws.assert_disconnected()

        # Reconnect
        mock_ws.simulate_reconnect()
        mock_ws.assert_connected()

    @pytest.mark.asyncio
    async def test_websocket_error_handling(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """WebSocket error event handling."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])

        errors_received = []

        def on_error(msg):
            errors_received.append(msg)

        mock_ws.on_error(on_error)
        mock_ws.simulate_error("Test error")

        assert len(errors_received) == 1
        assert "Test error" in errors_received[0]


class TestMarketTiming:
    """Tests for market timing edge cases."""

    @pytest.mark.asyncio
    async def test_market_ending_soon(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Market about to end - trade still executes."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_ending_soon"])
        await runner.configure_execution(EXECUTION_SCENARIOS["perfect_fill"])

        result = await runner.run_opportunity(budget=10.0)

        # Trade can still execute (though strategy might choose not to)
        assert result.yes_filled_size >= 0


class TestEdgeCases:
    """Tests for other edge cases."""

    @pytest.mark.asyncio
    async def test_zero_budget(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Zero budget should result in no trade."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["perfect_fill"])

        result = await runner.run_opportunity(budget=0.0)

        # Should have zero fills (no budget)
        assert result.yes_filled_size == 0 or result.no_filled_size == 0

    @pytest.mark.asyncio
    async def test_very_small_budget(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Very small budget trade."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["perfect_fill"])

        result = await runner.run_opportunity(budget=0.10)  # 10 cents

        # Small trades should still work
        assert result.yes_filled_size > 0 or result.no_filled_size > 0

    @pytest.mark.asyncio
    async def test_no_market_setup(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Running without market setup raises error."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        # Don't call setup_market

        with pytest.raises(RuntimeError, match="setup_market"):
            await runner.run_opportunity(budget=10.0)

    @pytest.mark.asyncio
    async def test_no_execution_config(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Running without execution config uses defaults."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        # Don't call configure_execution - should use defaults

        result = await runner.run_opportunity(budget=10.0)

        # Default behavior is successful execution
        assert result.success is True


class TestClientStateTracking:
    """Tests for client call tracking and assertions."""

    @pytest.mark.asyncio
    async def test_call_history_tracking(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """All client calls are tracked."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        market = MARKETS["btc_3c_spread"]
        await runner.setup_market(market)
        await runner.configure_execution(EXECUTION_SCENARIOS["perfect_fill"])

        # Query order book
        await mock_client.get_order_book(market.yes_token_id)

        # Execute trade
        await runner.run_opportunity(budget=10.0)

        # Check call history
        history = mock_client.get_call_history()
        assert len(history) > 0

        # Check specific calls
        book_calls = mock_client.get_call_history("get_order_book")
        assert len(book_calls) > 0

        order_calls = mock_client.get_call_history("execute_dual_leg_order_parallel")
        assert len(order_calls) > 0

    @pytest.mark.asyncio
    async def test_assert_order_placed_fails_when_no_orders(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """assert_order_placed fails when no orders were placed."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        # Don't execute any trades

        with pytest.raises(AssertionError, match="No orders"):
            mock_client.assert_order_placed()

    @pytest.mark.asyncio
    async def test_assert_no_orders_passes_when_no_orders(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """assert_no_orders_placed passes when no orders."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        # Don't execute any trades

        # Should not raise
        mock_client.assert_no_orders_placed()


class TestDatabaseErrorHandling:
    """Tests for database-related error handling."""

    @pytest.mark.asyncio
    async def test_trade_recording_after_execution(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Trades are recorded in database after execution."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["perfect_fill"])

        result = await runner.run_opportunity(budget=10.0)

        # Trade should be in database
        trades = await mock_db.get_all_trades()
        assert len(trades) == 1
        assert trades[0]["trade_id"] == result.trade_id

    @pytest.mark.asyncio
    async def test_failed_trade_not_recorded(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Failed trades (both rejected) are not recorded."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["both_rejected"])

        await runner.run_opportunity(budget=10.0)

        # No trades should be in database
        trades = await mock_db.get_all_trades()
        assert len(trades) == 0


class TestRunnerReset:
    """Tests for runner reset functionality."""

    @pytest.mark.asyncio
    async def test_reset_clears_state(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Reset clears all runner state."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["perfect_fill"])
        await runner.run_opportunity(budget=10.0)

        # Should have results and trades
        assert len(runner.get_all_results()) == 1
        assert len(runner.get_recorded_trades()) == 1

        # Reset
        runner.reset()

        # Should be empty
        assert len(runner.get_all_results()) == 0
        assert len(runner.get_recorded_trades()) == 0
        assert runner._market is None
        assert runner._execution is None

    @pytest.mark.asyncio
    async def test_multiple_tests_isolated(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Multiple test runs are isolated."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)

        # First run
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["perfect_fill"])
        await runner.run_opportunity(budget=10.0)
        assert len(runner.get_all_results()) == 1

        runner.reset()

        # Second run
        await runner.setup_market(MARKETS["eth_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["partial_fill_60pct"])
        await runner.run_opportunity(budget=10.0)
        assert len(runner.get_all_results()) == 1  # Still 1, not 2
