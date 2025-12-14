"""Integration tests for partial fill handling.

Tests various partial fill scenarios including:
- One leg fills, other rejected
- Both legs fill but at different sizes
- Hedge ratio calculations
- Position tracking for rebalancing
"""

import pytest
from tests.fixtures import (
    MockPolymarketClient,
    MockPolymarketWebSocket,
    MockDatabase,
    ScenarioRunner,
    MARKETS,
    EXECUTION_SCENARIOS,
)


class TestOneLegFill:
    """Tests where only one leg of the trade fills."""

    @pytest.mark.asyncio
    async def test_yes_fills_no_rejected(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """YES fills but NO is rejected - hold YES position."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["yes_fills_no_rejected"])

        result = await runner.run_opportunity(budget=10.0)

        # Should not be considered success (not fully hedged)
        assert result.success is False
        assert result.partial_fill is True

        # YES filled, NO did not
        assert result.yes_filled_size > 0
        assert result.no_filled_size == 0

        # Hedge ratio should be 0 (completely unhedged)
        assert result.hedge_ratio == 0.0

        # Trade recorded as one_leg_only
        runner.assert_trade_recorded(expected_status="one_leg_only")

    @pytest.mark.asyncio
    async def test_no_fills_yes_rejected(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """NO fills but YES is rejected - hold NO position."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["no_fills_yes_rejected"])

        result = await runner.run_opportunity(budget=10.0)

        assert result.success is False
        assert result.partial_fill is True

        # NO filled, YES did not
        assert result.yes_filled_size == 0
        assert result.no_filled_size > 0

        # Completely unhedged
        assert result.hedge_ratio == 0.0

    @pytest.mark.asyncio
    async def test_one_leg_fill_error_message(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Error message indicates which leg filled."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["yes_fills_no_rejected"])

        result = await runner.run_opportunity(budget=10.0)

        assert result.error is not None
        assert "YES filled" in result.error or "PARTIAL" in result.error


class TestPartialHedge:
    """Tests where both legs fill but at different amounts."""

    @pytest.mark.asyncio
    async def test_partial_fill_80pct_hedge(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """80% hedge ratio - at threshold, no rebalancing needed."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["partial_fill_80pct"])

        result = await runner.run_opportunity(budget=10.0)

        # Considered success at 80% threshold
        assert result.success is True

        # Both legs filled
        assert result.yes_filled_size > 0
        assert result.no_filled_size > 0

        # Hedge ratio approximately 0.8
        assert abs(result.hedge_ratio - 0.8) < 0.05

        # Assertion should pass
        runner.assert_execution_matches(EXECUTION_SCENARIOS["partial_fill_80pct"])

    @pytest.mark.asyncio
    async def test_partial_fill_60pct_hedge(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """60% hedge ratio - needs rebalancing."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["partial_fill_60pct"])

        result = await runner.run_opportunity(budget=10.0)

        # Still marked as success (orders filled)
        assert result.success is True

        # Both legs filled but imbalanced
        assert result.yes_filled_size == 10.0
        assert result.no_filled_size == 6.0

        # Hedge ratio is 60%
        assert abs(result.hedge_ratio - 0.6) < 0.05

        # Should need rebalancing
        trades = runner.get_recorded_trades()
        assert len(trades) == 1
        assert trades[0].get("needs_rebalancing") is True

    @pytest.mark.asyncio
    async def test_partial_fill_40pct_hedge(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """40% hedge ratio - severely imbalanced, high risk."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["partial_fill_40pct"])

        result = await runner.run_opportunity(budget=10.0)

        assert result.success is True  # Orders technically filled

        # Very imbalanced
        assert result.hedge_ratio == 0.4

        # Should definitely need rebalancing
        trades = runner.get_recorded_trades()
        assert trades[0].get("needs_rebalancing") is True


class TestHedgeRatioCalculation:
    """Tests for correct hedge ratio calculation."""

    @pytest.mark.asyncio
    async def test_hedge_ratio_yes_larger(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Hedge ratio when YES fills more than NO."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        # YES fills 10, NO fills 6
        await runner.configure_execution(EXECUTION_SCENARIOS["partial_fill_60pct"])

        result = await runner.run_opportunity(budget=10.0)

        # Hedge ratio = min/max = 6/10 = 0.6
        assert result.hedge_ratio == 0.6

    @pytest.mark.asyncio
    async def test_hedge_ratio_perfectly_matched(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Hedge ratio is 1.0 when fills are equal."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["perfect_fill"])

        result = await runner.run_opportunity(budget=10.0)

        # Both legs equal
        assert result.hedge_ratio == 1.0

    @pytest.mark.asyncio
    async def test_hedge_ratio_zero_when_one_leg_zero(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Hedge ratio is 0 when one leg doesn't fill."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["yes_fills_no_rejected"])

        result = await runner.run_opportunity(budget=10.0)

        assert result.hedge_ratio == 0.0


class TestAsymmetricLiquidity:
    """Tests for markets with asymmetric liquidity."""

    @pytest.mark.asyncio
    async def test_deep_yes_shallow_no_execution(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Market where YES is deep but NO is shallow."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_deep_yes_shallow_no"])
        # Expect partial fill due to NO side liquidity
        await runner.configure_execution(EXECUTION_SCENARIOS["partial_fill_60pct"])

        result = await runner.run_opportunity(budget=10.0)

        # Should result in partial fill
        assert result.yes_filled_size > result.no_filled_size
        assert result.hedge_ratio < 1.0


class TestTradeRecordingPartialFills:
    """Tests that partial fills are correctly recorded."""

    @pytest.mark.asyncio
    async def test_partial_fill_trade_fields(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """Verify all trade fields recorded for partial fill."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        market = MARKETS["btc_3c_spread"]
        await runner.setup_market(market)
        await runner.configure_execution(EXECUTION_SCENARIOS["partial_fill_60pct"])

        await runner.run_opportunity(budget=10.0)

        trades = runner.get_recorded_trades()
        assert len(trades) == 1
        trade = trades[0]

        # Check all expected fields
        assert trade["asset"] == "BTC"
        assert trade["condition_id"] == market.condition_id
        assert trade["yes_token_id"] == market.yes_token_id
        assert trade["no_token_id"] == market.no_token_id
        assert trade["yes_shares"] == 10.0
        assert trade["no_shares"] == 6.0
        assert trade["execution_status"] == "partial_fill"
        assert abs(trade["hedge_ratio"] - 0.6) < 0.05
        assert trade["needs_rebalancing"] is True

    @pytest.mark.asyncio
    async def test_one_leg_trade_expected_profit_zero(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
    ):
        """One-leg fill should have 0 expected profit (no hedge)."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["yes_fills_no_rejected"])

        await runner.run_opportunity(budget=10.0)

        trades = runner.get_recorded_trades()
        # With 0 matched shares, expected profit is 0
        assert trades[0]["expected_profit"] == 0.0


class TestPartialFillScenarioVariations:
    """Parameterized tests across all partial fill scenarios."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "scenario_name,expected_hedge_ratio,needs_rebalancing",
        [
            ("partial_fill_80pct", 0.8, False),  # At threshold
            ("partial_fill_60pct", 0.6, True),   # Below threshold
            ("partial_fill_40pct", 0.4, True),   # Way below threshold
            ("yes_fills_no_rejected", 0.0, True),  # One-leg
            ("no_fills_yes_rejected", 0.0, True),  # One-leg other direction
        ],
    )
    async def test_partial_fill_scenarios(
        self,
        mock_client: MockPolymarketClient,
        mock_ws: MockPolymarketWebSocket,
        mock_db: MockDatabase,
        scenario_name: str,
        expected_hedge_ratio: float,
        needs_rebalancing: bool,
    ):
        """Test all partial fill scenarios."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS[scenario_name])

        result = await runner.run_opportunity(budget=10.0)

        assert abs(result.hedge_ratio - expected_hedge_ratio) < 0.05, (
            f"Expected hedge ratio {expected_hedge_ratio}, got {result.hedge_ratio}"
        )

        # Check needs_rebalancing flag on recorded trade
        if result.trade_id:
            trades = runner.get_recorded_trades()
            if needs_rebalancing and result.hedge_ratio > 0:
                # Only trades with partial fills need rebalancing
                assert trades[0].get("needs_rebalancing") == needs_rebalancing
