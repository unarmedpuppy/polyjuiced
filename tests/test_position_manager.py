"""Tests for Active Position Management.

Tests the position manager's ability to:
1. Track positions with correct hedge ratios
2. Identify rebalancing opportunities
3. Execute rebalancing trades
4. Record telemetry accurately
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

# Import the modules under test
import sys
sys.path.insert(0, "src")

from position_manager import (
    ActivePosition,
    ActivePositionManager,
    RebalancingConfig,
    RebalanceOption,
    RebalanceTrade,
    TradeTelemetry,
    TelemetryEvent,
    create_active_position,
    create_telemetry,
)


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def mock_market():
    """Create a mock market for testing."""
    market = MagicMock()
    market.asset = "BTC"
    market.condition_id = "test-condition-123"
    market.yes_token_id = "yes-token-456"
    market.no_token_id = "no-token-789"
    market.slug = "btc-test-market"
    market.end_time = datetime.utcnow() + timedelta(minutes=10)
    return market


@pytest.fixture
def mock_market_state():
    """Create a mock market state for testing."""
    state = MagicMock()
    state.yes_price = 0.48
    state.no_price = 0.49
    state.yes_best_bid = 0.47
    state.yes_best_ask = 0.48
    state.no_best_bid = 0.48
    state.no_best_ask = 0.49
    return state


@pytest.fixture
def rebalancing_config():
    """Create a test rebalancing config."""
    return RebalancingConfig(
        rebalance_threshold=0.80,
        min_profit_per_share=0.02,
        max_rebalance_wait_seconds=60.0,
        prefer_sell_over_buy=True,
        allow_partial_rebalance=True,
        max_rebalance_trades=5,
        max_position_size_usd=25.0,
        min_spread_dollars=0.02,
    )


@pytest.fixture
def telemetry():
    """Create test telemetry."""
    return create_telemetry(
        trade_id="test-trade-001",
        opportunity_spread=3.0,  # 3 cents
        yes_price=0.48,
        no_price=0.49,
    )


# =============================================================================
# TradeTelemetry Tests
# =============================================================================

class TestTradeTelemetry:
    """Tests for TradeTelemetry dataclass."""

    def test_create_telemetry(self):
        """Test telemetry creation."""
        telemetry = create_telemetry(
            trade_id="test-001",
            opportunity_spread=2.5,
            yes_price=0.48,
            no_price=0.49,
        )

        assert telemetry.trade_id == "test-001"
        assert telemetry.opportunity_spread == 2.5
        assert telemetry.opportunity_yes_price == 0.48
        assert telemetry.opportunity_no_price == 0.49
        assert telemetry.opportunity_detected_at is not None

    def test_record_order_placed(self, telemetry):
        """Test recording order placement time."""
        telemetry.record_order_placed()

        assert telemetry.order_placed_at is not None
        assert telemetry.execution_latency_ms is not None
        assert telemetry.execution_latency_ms >= 0

    def test_record_order_filled(self, telemetry):
        """Test recording order fill."""
        telemetry.record_order_placed()
        telemetry.record_order_filled(yes_shares=10.0, no_shares=8.0)

        assert telemetry.order_filled_at is not None
        assert telemetry.initial_yes_shares == 10.0
        assert telemetry.initial_no_shares == 8.0
        assert telemetry.initial_hedge_ratio == 0.8  # 8/10

    def test_hedge_ratio_calculation(self, telemetry):
        """Test hedge ratio is calculated correctly."""
        telemetry.record_order_filled(yes_shares=10.0, no_shares=6.0)
        assert telemetry.initial_hedge_ratio == 0.6  # 6/10

        telemetry.record_order_filled(yes_shares=5.0, no_shares=5.0)
        assert telemetry.initial_hedge_ratio == 1.0  # Perfect balance

        telemetry.record_order_filled(yes_shares=0.0, no_shares=10.0)
        assert telemetry.initial_hedge_ratio == 0.0  # One-sided

    def test_record_rebalance_started(self, telemetry):
        """Test recording rebalance start."""
        telemetry.record_rebalance_started()
        assert telemetry.rebalance_started_at is not None

    def test_record_position_balanced(self, telemetry):
        """Test recording balanced position."""
        telemetry.record_position_balanced(yes_shares=10.0, no_shares=10.0)

        assert telemetry.position_balanced_at is not None
        assert telemetry.final_yes_shares == 10.0
        assert telemetry.final_no_shares == 10.0
        assert telemetry.final_hedge_ratio == 1.0

    def test_to_dict(self, telemetry):
        """Test conversion to dict for database storage."""
        telemetry.record_order_placed()
        telemetry.record_order_filled(10.0, 8.0)

        data = telemetry.to_dict()

        assert data["trade_id"] == "test-trade-001"
        assert data["opportunity_spread"] == 3.0
        assert data["opportunity_detected_at"] is not None
        assert data["order_placed_at"] is not None
        assert data["initial_yes_shares"] == 10.0
        assert data["initial_no_shares"] == 8.0


# =============================================================================
# ActivePosition Tests
# =============================================================================

class TestActivePosition:
    """Tests for ActivePosition dataclass."""

    def test_create_active_position(self, mock_market, telemetry):
        """Test position creation."""
        position = create_active_position(
            trade_id="test-001",
            market=mock_market,
            yes_shares=10.0,
            no_shares=8.0,
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
            budget=25.0,
        )

        assert position.trade_id == "test-001"
        assert position.yes_shares == 10.0
        assert position.no_shares == 8.0
        assert position.yes_avg_price == 0.48
        assert position.no_avg_price == 0.49
        assert position.original_budget == 25.0

    def test_hedge_ratio(self, mock_market, telemetry):
        """Test hedge ratio calculation."""
        # Imbalanced position
        position = create_active_position(
            trade_id="test-001",
            market=mock_market,
            yes_shares=10.0,
            no_shares=6.0,
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
        )
        assert position.hedge_ratio == 0.6

        # Balanced position
        position2 = create_active_position(
            trade_id="test-002",
            market=mock_market,
            yes_shares=10.0,
            no_shares=10.0,
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
        )
        assert position2.hedge_ratio == 1.0

    def test_is_balanced(self, mock_market, telemetry):
        """Test balance check with 80% threshold."""
        # Below threshold - needs rebalancing
        imbalanced = create_active_position(
            trade_id="test-001",
            market=mock_market,
            yes_shares=10.0,
            no_shares=6.0,  # 60% ratio
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
        )
        assert imbalanced.is_balanced is False
        assert imbalanced.needs_rebalancing is True

        # Above threshold - balanced
        balanced = create_active_position(
            trade_id="test-002",
            market=mock_market,
            yes_shares=10.0,
            no_shares=9.0,  # 90% ratio
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
        )
        assert balanced.is_balanced is True
        assert balanced.needs_rebalancing is False

    def test_excess_and_deficit(self, mock_market, telemetry):
        """Test excess/deficit identification."""
        # YES excess
        position = create_active_position(
            trade_id="test-001",
            market=mock_market,
            yes_shares=10.0,
            no_shares=6.0,
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
        )
        assert position.excess_side == "YES"
        assert position.deficit_side == "NO"
        assert position.excess_shares == 4.0

        # NO excess
        position2 = create_active_position(
            trade_id="test-002",
            market=mock_market,
            yes_shares=5.0,
            no_shares=10.0,
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
        )
        assert position2.excess_side == "NO"
        assert position2.deficit_side == "YES"
        assert position2.excess_shares == 5.0

    def test_total_cost(self, mock_market, telemetry):
        """Test total cost calculation."""
        position = create_active_position(
            trade_id="test-001",
            market=mock_market,
            yes_shares=10.0,
            no_shares=6.0,
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
        )
        # 10 * 0.48 + 6 * 0.49 = 4.80 + 2.94 = 7.74
        assert abs(position.total_cost - 7.74) < 0.001

    def test_remaining_budget(self, mock_market, telemetry):
        """Test remaining budget calculation."""
        position = create_active_position(
            trade_id="test-001",
            market=mock_market,
            yes_shares=10.0,
            no_shares=6.0,
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
            budget=25.0,
        )
        # Budget: 25.0, Cost: 7.74, Remaining: 17.26
        assert abs(position.remaining_budget - 17.26) < 0.01

    def test_guaranteed_return(self, mock_market, telemetry):
        """Test guaranteed return calculation."""
        position = create_active_position(
            trade_id="test-001",
            market=mock_market,
            yes_shares=10.0,
            no_shares=6.0,
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
        )
        # min(10, 6) * $1 = $6
        assert position.guaranteed_return == 6.0

    def test_expected_profit(self, mock_market, telemetry):
        """Test expected profit calculation."""
        position = create_active_position(
            trade_id="test-001",
            market=mock_market,
            yes_shares=10.0,
            no_shares=6.0,
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
        )
        # Return: $6, Cost: $7.74, Profit: -$1.74 (bad hedge!)
        assert abs(position.expected_profit - (-1.74)) < 0.01

        # Balanced position
        balanced = create_active_position(
            trade_id="test-002",
            market=mock_market,
            yes_shares=10.0,
            no_shares=10.0,
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
        )
        # Return: $10, Cost: 10*0.48 + 10*0.49 = $9.70, Profit: $0.30
        assert abs(balanced.expected_profit - 0.30) < 0.01

    def test_update_after_sell(self, mock_market, telemetry):
        """Test position update after selling shares."""
        position = create_active_position(
            trade_id="test-001",
            market=mock_market,
            yes_shares=10.0,
            no_shares=6.0,
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
        )

        # Sell 4 YES at $0.52 (profit)
        profit = position.update_after_sell("YES", 4.0, 0.52)

        assert position.yes_shares == 6.0
        assert position.no_shares == 6.0
        assert position.is_balanced is True
        # Profit: 4 * (0.52 - 0.48) = $0.16
        assert abs(profit - 0.16) < 0.01

    def test_update_after_buy(self, mock_market, telemetry):
        """Test position update after buying shares."""
        position = create_active_position(
            trade_id="test-001",
            market=mock_market,
            yes_shares=10.0,
            no_shares=6.0,
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
        )

        # Buy 4 NO at $0.45
        position.update_after_buy("NO", 4.0, 0.45)

        assert position.yes_shares == 10.0
        assert position.no_shares == 10.0
        assert position.is_balanced is True
        # New NO avg price: (6*0.49 + 4*0.45) / 10 = (2.94 + 1.80) / 10 = 0.474
        assert abs(position.no_avg_price - 0.474) < 0.001


# =============================================================================
# RebalanceOption Tests
# =============================================================================

class TestRebalanceOption:
    """Tests for RebalanceOption dataclass."""

    def test_profit_per_share(self):
        """Test profit per share calculation."""
        option = RebalanceOption(
            action="SELL_YES",
            shares=4.0,
            price=0.52,
            profit=0.16,
        )
        assert option.profit_per_share == 0.04  # 0.16 / 4

    def test_repr(self):
        """Test string representation."""
        option = RebalanceOption(
            action="SELL_YES",
            shares=4.0,
            price=0.52,
            profit=0.16,
        )
        repr_str = repr(option)
        assert "SELL_YES" in repr_str
        assert "4.00" in repr_str


# =============================================================================
# ActivePositionManager Tests
# =============================================================================

class TestActivePositionManager:
    """Tests for ActivePositionManager class."""

    @pytest.fixture
    def manager(self, rebalancing_config):
        """Create a test position manager."""
        mock_client = MagicMock()
        return ActivePositionManager(
            client=mock_client,
            db=None,
            config=rebalancing_config,
        )

    @pytest.mark.asyncio
    async def test_add_position(self, manager, mock_market, telemetry):
        """Test adding a position."""
        position = create_active_position(
            trade_id="test-001",
            market=mock_market,
            yes_shares=10.0,
            no_shares=8.0,
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
        )

        await manager.add_position(position)

        assert "test-001" in manager.positions
        assert len(manager.get_positions_for_market(mock_market.condition_id)) == 1

    @pytest.mark.asyncio
    async def test_remove_position(self, manager, mock_market, telemetry):
        """Test removing a position."""
        position = create_active_position(
            trade_id="test-001",
            market=mock_market,
            yes_shares=10.0,
            no_shares=10.0,
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
        )

        await manager.add_position(position)
        removed = await manager.remove_position("test-001", profit=0.30)

        assert removed is not None
        assert "test-001" not in manager.positions
        assert removed.status == "RESOLVED"

    def test_get_positions_needing_rebalancing(self, manager, mock_market, telemetry):
        """Test filtering positions that need rebalancing."""
        # Imbalanced position
        position1 = create_active_position(
            trade_id="test-001",
            market=mock_market,
            yes_shares=10.0,
            no_shares=6.0,  # 60% ratio
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
        )
        manager.positions["test-001"] = position1

        # Balanced position
        position2 = create_active_position(
            trade_id="test-002",
            market=mock_market,
            yes_shares=10.0,
            no_shares=10.0,  # 100% ratio
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
        )
        manager.positions["test-002"] = position2

        needing_rebalance = manager.get_positions_needing_rebalancing()
        assert len(needing_rebalance) == 1
        assert needing_rebalance[0].trade_id == "test-001"

    def test_get_rebalancing_options_sell_excess(
        self, manager, mock_market, mock_market_state, telemetry
    ):
        """Test getting sell excess rebalancing options."""
        position = create_active_position(
            trade_id="test-001",
            market=mock_market,
            yes_shares=10.0,
            no_shares=6.0,
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
            budget=25.0,
        )

        # Set bid higher than entry to enable profitable sell
        mock_market_state.yes_best_bid = 0.52  # Above 0.48 entry

        options = manager._get_rebalancing_options(position, mock_market_state)

        # Should have SELL_YES option
        sell_options = [o for o in options if o.action == "SELL_YES"]
        assert len(sell_options) >= 1

        sell_option = sell_options[0]
        assert sell_option.shares == 4.0  # Excess shares
        assert sell_option.price == 0.52
        # Profit: 4 * (0.52 - 0.48) = 0.16
        assert abs(sell_option.profit - 0.16) < 0.01

    def test_get_rebalancing_options_buy_deficit(
        self, manager, mock_market, mock_market_state, telemetry
    ):
        """Test getting buy deficit rebalancing options."""
        position = create_active_position(
            trade_id="test-001",
            market=mock_market,
            yes_shares=10.0,
            no_shares=6.0,
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
            budget=25.0,
        )

        # Set ask lower to enable cheaper buying
        mock_market_state.no_best_ask = 0.42  # Below 0.49 entry
        mock_market_state.yes_best_bid = 0.40  # Below entry, no sell opportunity

        options = manager._get_rebalancing_options(position, mock_market_state)

        # Should have BUY_NO option
        buy_options = [o for o in options if o.action == "BUY_NO"]
        assert len(buy_options) >= 1

    def test_select_best_option_prefers_sell(self, manager):
        """Test that sell is preferred over buy when configured."""
        sell_option = RebalanceOption(
            action="SELL_YES",
            shares=4.0,
            price=0.52,
            profit=0.16,
        )
        buy_option = RebalanceOption(
            action="BUY_NO",
            shares=4.0,
            price=0.45,
            profit=0.20,  # Higher profit
        )

        # With prefer_sell_over_buy=True, should select sell
        best = manager._select_best_option([sell_option, buy_option])
        assert best.action == "SELL_YES"

    def test_select_best_option_respects_min_profit(self, manager):
        """Test that options below min profit are rejected."""
        low_profit_option = RebalanceOption(
            action="SELL_YES",
            shares=4.0,
            price=0.52,
            profit=0.04,  # $0.01/share < $0.02 threshold
        )

        best = manager._select_best_option([low_profit_option])
        assert best is None

    def test_should_execute_checks_spread(
        self, manager, mock_market, mock_market_state, telemetry
    ):
        """Test that spread constraint is enforced."""
        position = create_active_position(
            trade_id="test-001",
            market=mock_market,
            yes_shares=10.0,
            no_shares=6.0,
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
        )

        # Option that would compress spread too much
        expensive_buy = RebalanceOption(
            action="BUY_NO",
            shares=4.0,
            price=0.51,  # High price would compress spread
            profit=0.10,
        )

        # Should reject because new spread would be < min_spread
        # Current: 1.0 - 0.48 - 0.49 = 0.03
        # After buy at 0.51: new no_avg = (6*0.49 + 4*0.51)/10 = 0.498
        # New spread: 1.0 - 0.48 - 0.498 = 0.022 >= 0.02, should accept
        result = manager._should_execute(expensive_buy, position, mock_market_state)
        # The math shows 0.022 >= 0.02, so it should pass
        assert result is True

    def test_get_stats(self, manager, mock_market, telemetry):
        """Test statistics generation."""
        position1 = create_active_position(
            trade_id="test-001",
            market=mock_market,
            yes_shares=10.0,
            no_shares=10.0,
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
        )
        position2 = create_active_position(
            trade_id="test-002",
            market=mock_market,
            yes_shares=10.0,
            no_shares=6.0,
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
        )

        manager.positions["test-001"] = position1
        manager.positions["test-002"] = position2

        stats = manager.get_stats()

        assert stats["total_positions"] == 2
        assert stats["balanced_positions"] == 1
        assert stats["needing_rebalance"] == 1


# =============================================================================
# Integration Tests
# =============================================================================

class TestRebalancingScenarios:
    """Integration tests for complete rebalancing scenarios."""

    @pytest.fixture
    def full_manager(self, rebalancing_config):
        """Create manager with mocked client."""
        mock_client = MagicMock()
        mock_client._client = MagicMock()

        # Mock successful order execution
        mock_client._client.create_order.return_value = MagicMock()
        mock_client._client.post_order.return_value = {
            "status": "MATCHED",
            "size_matched": 4.0,
            "id": "order-123",
        }

        return ActivePositionManager(
            client=mock_client,
            db=None,
            config=rebalancing_config,
        )

    @pytest.mark.asyncio
    async def test_full_rebalancing_flow_sell_excess(
        self, full_manager, mock_market, mock_market_state, telemetry
    ):
        """Test complete flow: detect imbalance, find opportunity, execute sell."""
        # Create imbalanced position (YES excess)
        position = create_active_position(
            trade_id="test-001",
            market=mock_market,
            yes_shares=10.0,
            no_shares=6.0,
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
            budget=25.0,
        )

        await full_manager.add_position(position)

        # Confirm imbalanced
        assert position.needs_rebalancing is True
        assert position.telemetry.rebalance_started_at is not None

        # Market moves favorably - YES bid rises
        mock_market_state.yes_best_bid = 0.55  # Above 0.48 entry

        # Trigger rebalancing evaluation
        await full_manager._evaluate_rebalancing(position, mock_market_state)

        # Position should now be balanced (sold excess YES)
        assert position.yes_shares == 6.0
        assert position.no_shares == 6.0
        assert position.is_balanced is True
        assert position.status == "BALANCED"
        assert len(position.rebalance_history) == 1
        assert position.rebalance_history[0].action == "SELL_YES"
        assert position.rebalance_history[0].status == "SUCCESS"

    @pytest.mark.asyncio
    async def test_rebalancing_respects_time_limit(
        self, full_manager, mock_market, mock_market_state, telemetry
    ):
        """Test that rebalancing is skipped near resolution."""
        # Create market ending in 30 seconds (< 60s threshold)
        mock_market.end_time = datetime.utcnow() + timedelta(seconds=30)

        position = create_active_position(
            trade_id="test-001",
            market=mock_market,
            yes_shares=10.0,
            no_shares=6.0,
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
        )

        await full_manager.add_position(position)
        mock_market_state.yes_best_bid = 0.55

        # Trigger rebalancing evaluation
        await full_manager._evaluate_rebalancing(position, mock_market_state)

        # Should NOT have rebalanced - too close to resolution
        assert position.yes_shares == 10.0  # Unchanged
        assert position.no_shares == 6.0
        assert len(position.rebalance_history) == 0

    @pytest.mark.asyncio
    async def test_rebalancing_respects_max_attempts(
        self, full_manager, mock_market, mock_market_state, telemetry
    ):
        """Test that rebalancing stops after max attempts."""
        position = create_active_position(
            trade_id="test-001",
            market=mock_market,
            yes_shares=10.0,
            no_shares=6.0,
            yes_price=0.48,
            no_price=0.49,
            telemetry=telemetry,
        )

        # Simulate 5 prior attempts
        position.telemetry.rebalance_attempts = 5

        await full_manager.add_position(position)
        mock_market_state.yes_best_bid = 0.55

        # Trigger rebalancing evaluation
        await full_manager._evaluate_rebalancing(position, mock_market_state)

        # Should NOT have rebalanced - max attempts reached
        assert position.yes_shares == 10.0  # Unchanged
        assert len(position.rebalance_history) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
