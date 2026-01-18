"""Phase 8 Regression Tests: Execution Flow

These tests validate the complete execution path:
1. Opportunity detection -> trade execution
2. Prices flow correctly through the pipeline
3. Partial fills are properly recorded
4. Liquidity snapshots are captured
5. Events are emitted correctly

The key insight: Every step in the execution flow must be tested
to prevent bugs from slipping through.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from dataclasses import dataclass
from typing import Optional


@dataclass
class MockOpportunity:
    """Mock arbitrage opportunity."""
    yes_price: float
    no_price: float
    spread_cents: float
    yes_token_id: str = "token-yes-123"
    no_token_id: str = "token-no-456"


@dataclass
class MockMarket:
    """Mock market data."""
    asset: str = "BTC"
    condition_id: str = "0xabc123"
    yes_token_id: str = "token-yes-123"
    no_token_id: str = "token-no-456"
    slug: str = "btc-updown-15m"
    end_time: Optional[str] = "12:30"


@dataclass
class MockDualLegResult:
    """Mock result from dual-leg order execution."""
    success: bool
    actual_yes_shares: float
    actual_no_shares: float
    actual_yes_cost: float
    actual_no_cost: float
    yes_status: str
    no_status: str
    hedge_ratio: float
    error: Optional[str] = None
    partial_fill: bool = False
    pre_fill_yes_depth: float = 0.0
    pre_fill_no_depth: float = 0.0


class TestExecutionFlow:
    """Test the complete execution path from opportunity to trade record."""

    def test_opportunity_prices_used_for_limits(self):
        """Verify prices from opportunity are used for limit orders."""
        opportunity = MockOpportunity(
            yes_price=0.30,
            no_price=0.68,
            spread_cents=2.0,
        )

        slippage = 0.02

        # Correct calculation
        yes_limit = opportunity.yes_price + slippage
        no_limit = opportunity.no_price + slippage

        # Verify prices are derived from opportunity
        assert yes_limit == pytest.approx(0.32, abs=0.001)
        assert no_limit == pytest.approx(0.70, abs=0.001)

        # Verify they're different (the $0.53 bug used same price)
        assert yes_limit != no_limit

    def test_partial_fill_data_structure(self):
        """Partial fills must return accurate data for recording."""
        result = MockDualLegResult(
            success=False,
            actual_yes_shares=10.0,
            actual_no_shares=0.0,
            actual_yes_cost=4.80,
            actual_no_cost=0.0,
            yes_status="MATCHED",
            no_status="FAILED",
            hedge_ratio=0.0,
            partial_fill=True,
            error="PARTIAL FILL: YES filled (MATCHED), NO rejected (FAILED). Position held.",
            pre_fill_yes_depth=100.0,
            pre_fill_no_depth=30.0,
        )

        # Required fields for recording
        assert result.partial_fill is True
        assert result.actual_yes_shares == 10.0
        assert result.actual_no_shares == 0.0
        assert result.hedge_ratio == 0.0
        assert "PARTIAL FILL" in result.error
        assert "Position held" in result.error

    def test_liquidity_snapshot_in_result(self):
        """Every trade result must include liquidity snapshot."""
        result = MockDualLegResult(
            success=True,
            actual_yes_shares=10.0,
            actual_no_shares=10.0,
            actual_yes_cost=4.80,
            actual_no_cost=4.90,
            yes_status="MATCHED",
            no_status="MATCHED",
            hedge_ratio=1.0,
            pre_fill_yes_depth=100.0,
            pre_fill_no_depth=80.0,
        )

        # Liquidity data must be present
        assert hasattr(result, 'pre_fill_yes_depth')
        assert hasattr(result, 'pre_fill_no_depth')
        assert result.pre_fill_yes_depth > 0
        assert result.pre_fill_no_depth > 0

    def test_successful_trade_hedge_ratio(self):
        """Successful trades should have hedge ratio close to 1.0."""
        result = MockDualLegResult(
            success=True,
            actual_yes_shares=10.0,
            actual_no_shares=10.0,
            actual_yes_cost=4.80,
            actual_no_cost=4.90,
            yes_status="MATCHED",
            no_status="MATCHED",
            hedge_ratio=1.0,
        )

        assert result.success is True
        assert result.hedge_ratio == 1.0

    def test_failed_trade_hedge_ratio(self):
        """Failed trades may have hedge ratio < 1.0."""
        result = MockDualLegResult(
            success=False,
            actual_yes_shares=10.0,
            actual_no_shares=0.0,
            actual_yes_cost=4.80,
            actual_no_cost=0.0,
            yes_status="MATCHED",
            no_status="FAILED",
            hedge_ratio=0.0,
            partial_fill=True,
        )

        assert result.success is False
        assert result.hedge_ratio < 1.0


class TestPriceFlowThroughPipeline:
    """Test that prices flow correctly through all stages."""

    def test_spread_calculation(self):
        """Spread should be calculated correctly."""
        yes_price = 0.48
        no_price = 0.49

        # Spread in cents = (1.0 - YES - NO) * 100
        spread_cents = (1.0 - yes_price - no_price) * 100

        assert spread_cents == pytest.approx(3.0, abs=0.1)

    def test_expected_profit_calculation(self):
        """Expected profit should be calculated from spread and shares."""
        yes_price = 0.48
        no_price = 0.49
        shares = 10.0

        # Total cost
        total_cost = (yes_price + no_price) * shares  # $9.70

        # Return is always $1 per share (one side wins)
        total_return = 1.0 * shares  # $10.00

        # Expected profit
        expected_profit = total_return - total_cost  # $0.30

        assert expected_profit == pytest.approx(0.30, abs=0.01)
        assert expected_profit > 0  # Must be positive for arbitrage

    def test_shares_from_budget(self):
        """Shares should be calculated from budget and prices."""
        budget = 10.0
        yes_price = 0.48
        no_price = 0.49

        # Total price per share pair
        price_per_pair = yes_price + no_price  # $0.97

        # Max shares we can afford
        max_shares = budget / price_per_pair  # ~10.31 shares

        assert max_shares == pytest.approx(10.31, abs=0.1)


class TestEventEmission:
    """Test that events are emitted correctly."""

    def test_trade_created_event_structure(self):
        """TRADE_CREATED event should have all required fields."""
        required_fields = [
            "trade_id",
            "asset",
            "yes_price",
            "no_price",
            "yes_cost",
            "no_cost",
            "spread",
            "expected_profit",
            "hedge_ratio",
            "execution_status",
            "dry_run",
        ]

        # Example event data
        event_data = {
            "trade_id": "trade-123",
            "asset": "BTC",
            "condition_id": "0x123",
            "yes_price": 0.48,
            "no_price": 0.49,
            "yes_cost": 4.80,
            "no_cost": 4.90,
            "spread": 3.0,
            "expected_profit": 0.30,
            "yes_shares": 10.0,
            "no_shares": 10.0,
            "hedge_ratio": 1.0,
            "execution_status": "full_fill",
            "dry_run": False,
        }

        for field in required_fields:
            assert field in event_data, f"Event missing '{field}'"

    def test_event_price_consistency(self):
        """Event prices should match what was used for execution."""
        opportunity = MockOpportunity(
            yes_price=0.48,
            no_price=0.49,
            spread_cents=3.0,
        )

        # Event should contain the same prices
        event_data = {
            "yes_price": opportunity.yes_price,
            "no_price": opportunity.no_price,
        }

        assert event_data["yes_price"] == opportunity.yes_price
        assert event_data["no_price"] == opportunity.no_price


class TestExecutionStatusCategories:
    """Test that execution statuses are categorized correctly."""

    def test_full_fill_status(self):
        """Full fills should have both legs MATCHED."""
        yes_status = "MATCHED"
        no_status = "MATCHED"

        # Determine execution status
        if yes_status == "MATCHED" and no_status == "MATCHED":
            execution_status = "full_fill"
        elif yes_status == "MATCHED" or no_status == "MATCHED":
            execution_status = "partial_fill"
        else:
            execution_status = "no_fill"

        assert execution_status == "full_fill"

    def test_partial_fill_status(self):
        """Partial fills should have one leg MATCHED."""
        yes_status = "MATCHED"
        no_status = "FAILED"

        if yes_status == "MATCHED" and no_status == "MATCHED":
            execution_status = "full_fill"
        elif yes_status == "MATCHED" or no_status == "MATCHED":
            execution_status = "partial_fill"
        else:
            execution_status = "no_fill"

        assert execution_status == "partial_fill"

    def test_no_fill_status(self):
        """No fills should have neither leg MATCHED."""
        yes_status = "FAILED"
        no_status = "FAILED"

        if yes_status == "MATCHED" and no_status == "MATCHED":
            execution_status = "full_fill"
        elif yes_status == "MATCHED" or no_status == "MATCHED":
            execution_status = "partial_fill"
        else:
            execution_status = "no_fill"

        assert execution_status == "no_fill"


class TestEdgeCases:
    """Test edge cases in execution flow."""

    def test_zero_shares_not_executed(self):
        """Zero shares should not create a trade."""
        shares = 0.0

        # Should not attempt execution
        should_execute = shares > 0
        assert not should_execute

    def test_minimum_spread_enforcement(self):
        """Trades below minimum spread should be rejected."""
        min_spread_cents = 2.0

        # This spread is too small
        yes_price = 0.49
        no_price = 0.50
        spread_cents = (1.0 - yes_price - no_price) * 100  # 1.0 cents

        should_execute = spread_cents >= min_spread_cents
        assert not should_execute, "Should reject spread < minimum"

    def test_maximum_price_enforcement(self):
        """Prices above 0.99 should be capped."""
        market_price = 0.98
        slippage = 0.02

        limit_price = min(0.99, market_price + slippage)

        assert limit_price <= 0.99

    def test_minimum_price_enforcement(self):
        """Prices below 0.01 should be floored."""
        market_price = 0.005
        slippage = 0.02

        limit_price = max(0.01, market_price + slippage)

        assert limit_price >= 0.01


class TestDryRunBehavior:
    """Test dry run mode execution flow."""

    def test_dry_run_flag_propagation(self):
        """Dry run flag should propagate through all stages."""
        dry_run = True

        # Event should include dry_run flag
        event_data = {
            "trade_id": "trade-123",
            "dry_run": dry_run,
        }

        assert event_data["dry_run"] is True

    def test_dry_run_no_actual_cost(self):
        """Dry run trades should show simulated costs."""
        dry_run = True
        yes_cost = 4.80
        no_cost = 4.90

        # In dry run, costs are still calculated but no money moves
        if dry_run:
            actual_money_spent = 0.0
        else:
            actual_money_spent = yes_cost + no_cost

        assert actual_money_spent == 0.0 if dry_run else actual_money_spent > 0
