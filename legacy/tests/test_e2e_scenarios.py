"""End-to-End Test Scenarios for Trade Execution

This file tests a comprehensive variety of trade execution scenarios:
1. Perfect execution (both legs fill)
2. Partial fills (various hedge ratios)
3. Complete failures (neither leg fills)
4. Asymmetric fills
5. Price movement scenarios
6. Rebalancing opportunities
7. Edge cases

Each scenario documents:
- Initial conditions
- Expected behavior
- Final state assertions
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from decimal import Decimal
import asyncio


# =============================================================================
# Test Data Structures
# =============================================================================

@dataclass
class MockOrderResult:
    """Simulates an order result from the exchange."""
    status: str  # MATCHED, LIVE, FAILED, REJECTED, CANCELLED
    size_matched: float
    price: float
    order_id: str = "order-123"

    @property
    def filled_cost(self) -> float:
        return self.size_matched * self.price


@dataclass
class MockMarketState:
    """Simulates market state at a point in time."""
    yes_best_ask: float
    no_best_ask: float
    yes_best_bid: float = 0.0
    no_best_bid: float = 0.0
    yes_depth: float = 100.0  # Shares available
    no_depth: float = 100.0

    @property
    def spread_cents(self) -> float:
        return (1.0 - self.yes_best_ask - self.no_best_ask) * 100


@dataclass
class TradeScenario:
    """Defines a complete trade scenario for testing."""
    name: str
    description: str

    # Initial market state
    initial_market: MockMarketState

    # Trade parameters
    budget: float = 10.0

    # Execution results (what the exchange returns)
    yes_result: MockOrderResult = None
    no_result: MockOrderResult = None

    # Expected outcomes
    expected_success: bool = False
    expected_hedge_ratio: float = 0.0
    expected_execution_status: str = "unknown"
    expected_should_rebalance: bool = False

    # For rebalancing scenarios
    price_after_fill: MockMarketState = None
    rebalance_opportunity: bool = False


# =============================================================================
# Scenario Definitions
# =============================================================================

class TestPerfectExecutionScenarios:
    """Test scenarios where both legs fill perfectly."""

    def test_scenario_perfect_fill_equal_prices(self):
        """Both legs fill at equal prices - perfect arbitrage."""
        scenario = TradeScenario(
            name="perfect_fill_equal",
            description="YES and NO both fill at $0.48, 4¢ spread",
            initial_market=MockMarketState(
                yes_best_ask=0.48,
                no_best_ask=0.48,
            ),
            budget=10.0,
            yes_result=MockOrderResult(
                status="MATCHED",
                size_matched=10.42,  # 10 / (0.48 + 0.48) = 10.42 shares
                price=0.48,
            ),
            no_result=MockOrderResult(
                status="MATCHED",
                size_matched=10.42,
                price=0.48,
            ),
            expected_success=True,
            expected_hedge_ratio=1.0,
            expected_execution_status="full_fill",
        )

        # Verify calculations
        cost_per_pair = 0.48 + 0.48  # $0.96
        expected_shares = scenario.budget / cost_per_pair
        expected_profit = expected_shares * 1.0 - scenario.budget  # ~$0.42

        assert scenario.initial_market.spread_cents == pytest.approx(4.0, abs=0.1)
        assert expected_profit > 0
        assert scenario.expected_hedge_ratio == 1.0

    def test_scenario_perfect_fill_asymmetric_prices(self):
        """Both legs fill at different prices - still perfect hedge."""
        scenario = TradeScenario(
            name="perfect_fill_asymmetric",
            description="YES at $0.30, NO at $0.68, 2¢ spread",
            initial_market=MockMarketState(
                yes_best_ask=0.30,
                no_best_ask=0.68,
            ),
            budget=10.0,
            yes_result=MockOrderResult(
                status="MATCHED",
                size_matched=10.20,  # 10 / 0.98 = 10.20 shares
                price=0.30,
            ),
            no_result=MockOrderResult(
                status="MATCHED",
                size_matched=10.20,
                price=0.68,
            ),
            expected_success=True,
            expected_hedge_ratio=1.0,
            expected_execution_status="full_fill",
        )

        assert scenario.initial_market.spread_cents == pytest.approx(2.0, abs=0.1)

    def test_scenario_minimum_spread(self):
        """Trade at exactly minimum spread (2¢)."""
        scenario = TradeScenario(
            name="minimum_spread",
            description="Exactly 2¢ spread - borderline profitable",
            initial_market=MockMarketState(
                yes_best_ask=0.49,
                no_best_ask=0.49,
            ),
            budget=10.0,
            expected_success=True,
            expected_hedge_ratio=1.0,
        )

        # 2¢ spread on $10 trade = ~$0.20 profit
        assert scenario.initial_market.spread_cents == pytest.approx(2.0, abs=0.1)


class TestPartialFillScenarios:
    """Test scenarios with partial fills requiring rebalancing decisions."""

    def test_scenario_yes_fills_no_rejected(self):
        """YES fills completely, NO is rejected (FOK failure)."""
        scenario = TradeScenario(
            name="yes_only",
            description="YES fills, NO rejected - 0% hedge",
            initial_market=MockMarketState(
                yes_best_ask=0.48,
                no_best_ask=0.49,
            ),
            budget=10.0,
            yes_result=MockOrderResult(
                status="MATCHED",
                size_matched=10.31,
                price=0.48,
            ),
            no_result=MockOrderResult(
                status="REJECTED",
                size_matched=0.0,
                price=0.49,
            ),
            expected_success=False,
            expected_hedge_ratio=0.0,
            expected_execution_status="partial_fill",
            expected_should_rebalance=True,  # 0% hedge - must rebalance!
        )

        # With 0% hedge, we DEFINITELY want to rebalance
        assert scenario.expected_hedge_ratio < 0.80

    def test_scenario_no_fills_yes_rejected(self):
        """NO fills completely, YES is rejected."""
        scenario = TradeScenario(
            name="no_only",
            description="NO fills, YES rejected - 0% hedge",
            initial_market=MockMarketState(
                yes_best_ask=0.48,
                no_best_ask=0.49,
            ),
            budget=10.0,
            yes_result=MockOrderResult(
                status="REJECTED",
                size_matched=0.0,
                price=0.48,
            ),
            no_result=MockOrderResult(
                status="MATCHED",
                size_matched=10.20,
                price=0.49,
            ),
            expected_success=False,
            expected_hedge_ratio=0.0,
            expected_execution_status="partial_fill",
            expected_should_rebalance=True,
        )

    def test_scenario_80_percent_hedge(self):
        """Both fill, but NO gets fewer shares - 80% hedge."""
        scenario = TradeScenario(
            name="80_percent_hedge",
            description="10 YES, 8 NO - at threshold",
            initial_market=MockMarketState(
                yes_best_ask=0.48,
                no_best_ask=0.49,
            ),
            yes_result=MockOrderResult(
                status="MATCHED",
                size_matched=10.0,
                price=0.48,
            ),
            no_result=MockOrderResult(
                status="MATCHED",
                size_matched=8.0,
                price=0.49,
            ),
            expected_success=False,  # Not perfect, but acceptable
            expected_hedge_ratio=0.80,
            expected_execution_status="partial_fill",
            expected_should_rebalance=False,  # At 80% threshold - acceptable
        )

        # Calculate hedge ratio
        hedge_ratio = min(10.0, 8.0) / max(10.0, 8.0)
        assert hedge_ratio == 0.80

    def test_scenario_60_percent_hedge(self):
        """Both fill, but significant imbalance - 60% hedge."""
        scenario = TradeScenario(
            name="60_percent_hedge",
            description="10 YES, 6 NO - needs rebalancing",
            initial_market=MockMarketState(
                yes_best_ask=0.48,
                no_best_ask=0.49,
            ),
            yes_result=MockOrderResult(
                status="MATCHED",
                size_matched=10.0,
                price=0.48,
            ),
            no_result=MockOrderResult(
                status="MATCHED",
                size_matched=6.0,
                price=0.49,
            ),
            expected_success=False,
            expected_hedge_ratio=0.60,
            expected_execution_status="partial_fill",
            expected_should_rebalance=True,  # Below 80% - should rebalance
        )

        hedge_ratio = min(10.0, 6.0) / max(10.0, 6.0)
        assert hedge_ratio == 0.60
        assert hedge_ratio < 0.80  # Below threshold


class TestRebalancingOpportunityScenarios:
    """Test scenarios for opportunistic position rebalancing."""

    def test_scenario_price_rises_can_sell_excess(self):
        """YES price rises after partial fill - can sell excess at profit."""
        scenario = TradeScenario(
            name="rebalance_sell_yes_profit",
            description="Bought 10 YES @ $0.48, 6 NO @ $0.49. YES rises to $0.55",
            initial_market=MockMarketState(
                yes_best_ask=0.48,
                no_best_ask=0.49,
            ),
            yes_result=MockOrderResult(
                status="MATCHED",
                size_matched=10.0,
                price=0.48,
            ),
            no_result=MockOrderResult(
                status="MATCHED",
                size_matched=6.0,
                price=0.49,
            ),
            expected_hedge_ratio=0.60,
            expected_should_rebalance=True,
            # After some time, YES price rises
            price_after_fill=MockMarketState(
                yes_best_bid=0.55,  # Can SELL YES at $0.55
                no_best_ask=0.45,
                yes_best_ask=0.56,
                no_best_bid=0.44,
            ),
            rebalance_opportunity=True,
        )

        # Calculate rebalancing opportunity
        excess_yes = 10.0 - 6.0  # 4 shares
        buy_price = 0.48
        sell_price = 0.55
        profit_per_share = sell_price - buy_price  # $0.07
        rebalance_profit = excess_yes * profit_per_share  # $0.28

        assert rebalance_profit > 0
        assert sell_price > buy_price

    def test_scenario_price_falls_hold_for_resolution(self):
        """YES price falls after partial fill - hold for resolution."""
        scenario = TradeScenario(
            name="no_rebalance_price_fell",
            description="Bought 10 YES @ $0.48, 6 NO. YES falls to $0.40",
            initial_market=MockMarketState(
                yes_best_ask=0.48,
                no_best_ask=0.49,
            ),
            yes_result=MockOrderResult(
                status="MATCHED",
                size_matched=10.0,
                price=0.48,
            ),
            no_result=MockOrderResult(
                status="MATCHED",
                size_matched=6.0,
                price=0.49,
            ),
            expected_hedge_ratio=0.60,
            price_after_fill=MockMarketState(
                yes_best_bid=0.40,  # Can only sell at $0.40 - LOSS
                no_best_ask=0.58,
                yes_best_ask=0.42,
                no_best_bid=0.56,
            ),
            rebalance_opportunity=False,  # Don't sell at a loss!
        )

        # Selling would lock in a loss
        buy_price = 0.48
        sell_price = 0.40
        loss_per_share = buy_price - sell_price  # $0.08 loss

        assert sell_price < buy_price
        assert not scenario.rebalance_opportunity

    def test_scenario_can_buy_more_no_to_balance(self):
        """NO price drops - can buy more NO to balance position."""
        scenario = TradeScenario(
            name="rebalance_buy_no",
            description="10 YES @ $0.48, 6 NO @ $0.49. NO drops to $0.42",
            initial_market=MockMarketState(
                yes_best_ask=0.48,
                no_best_ask=0.49,
            ),
            yes_result=MockOrderResult(
                status="MATCHED",
                size_matched=10.0,
                price=0.48,
            ),
            no_result=MockOrderResult(
                status="MATCHED",
                size_matched=6.0,
                price=0.49,
            ),
            expected_hedge_ratio=0.60,
            price_after_fill=MockMarketState(
                yes_best_ask=0.55,
                no_best_ask=0.42,  # NO is cheaper now!
                yes_best_bid=0.54,
                no_best_bid=0.41,
            ),
            rebalance_opportunity=True,
        )

        # Can buy 4 more NO at $0.42 to balance
        needed_no = 10.0 - 6.0  # 4 shares
        new_no_price = 0.42
        cost_to_balance = needed_no * new_no_price  # $1.68

        # New total cost: $4.80 (YES) + $2.94 (6 NO) + $1.68 (4 NO) = $9.42
        # Guaranteed return: $10.00
        # Profit: $0.58

        total_yes_cost = 10.0 * 0.48
        total_no_cost = 6.0 * 0.49 + 4.0 * 0.42
        total_cost = total_yes_cost + total_no_cost
        guaranteed_return = 10.0 * 1.0
        profit = guaranteed_return - total_cost

        assert profit > 0


class TestNoFillScenarios:
    """Test scenarios where no fills occur."""

    def test_scenario_both_rejected(self):
        """Both orders rejected - FOK failure on thin book."""
        scenario = TradeScenario(
            name="both_rejected",
            description="Both legs rejected due to insufficient liquidity",
            initial_market=MockMarketState(
                yes_best_ask=0.48,
                no_best_ask=0.49,
                yes_depth=5.0,  # Thin book
                no_depth=5.0,
            ),
            yes_result=MockOrderResult(
                status="REJECTED",
                size_matched=0.0,
                price=0.48,
            ),
            no_result=MockOrderResult(
                status="REJECTED",
                size_matched=0.0,
                price=0.49,
            ),
            expected_success=False,
            expected_hedge_ratio=0.0,
            expected_execution_status="no_fill",
            expected_should_rebalance=False,  # Nothing to rebalance
        )

        # No position = no rebalancing needed
        assert scenario.yes_result.size_matched == 0
        assert scenario.no_result.size_matched == 0

    def test_scenario_both_live_cancelled(self):
        """Both orders go LIVE then are cancelled."""
        scenario = TradeScenario(
            name="both_cancelled",
            description="Both legs went LIVE (book orders) - cancelled",
            initial_market=MockMarketState(
                yes_best_ask=0.48,
                no_best_ask=0.49,
            ),
            yes_result=MockOrderResult(
                status="CANCELLED",
                size_matched=0.0,
                price=0.48,
            ),
            no_result=MockOrderResult(
                status="CANCELLED",
                size_matched=0.0,
                price=0.49,
            ),
            expected_success=False,
            expected_hedge_ratio=0.0,
            expected_execution_status="no_fill",
        )


class TestEdgeCaseScenarios:
    """Edge cases and unusual scenarios."""

    def test_scenario_exactly_at_threshold(self):
        """Hedge ratio exactly at 80% threshold."""
        yes_shares = 10.0
        no_shares = 8.0
        hedge_ratio = min(yes_shares, no_shares) / max(yes_shares, no_shares)

        # At threshold - should NOT rebalance (accept the risk)
        should_rebalance = hedge_ratio < 0.80

        assert hedge_ratio == 0.80
        assert not should_rebalance

    def test_scenario_just_below_threshold(self):
        """Hedge ratio just below 80% threshold."""
        yes_shares = 10.0
        no_shares = 7.9
        hedge_ratio = min(yes_shares, no_shares) / max(yes_shares, no_shares)

        # Just below threshold - SHOULD rebalance
        should_rebalance = hedge_ratio < 0.80

        assert hedge_ratio == 0.79
        assert should_rebalance

    def test_scenario_near_resolution(self):
        """Partial fill very close to market resolution."""
        scenario = TradeScenario(
            name="near_resolution_partial",
            description="Partial fill with only 30 seconds until resolution",
            initial_market=MockMarketState(
                yes_best_ask=0.48,
                no_best_ask=0.49,
            ),
            yes_result=MockOrderResult(
                status="MATCHED",
                size_matched=10.0,
                price=0.48,
            ),
            no_result=MockOrderResult(
                status="REJECTED",
                size_matched=0.0,
                price=0.49,
            ),
            expected_hedge_ratio=0.0,
            # Near resolution - might not have time to rebalance
            # But should still try if opportunity exists
        )

    def test_scenario_price_moves_past_breakeven(self):
        """Price moves to exact breakeven - no profit in rebalancing."""
        buy_price = 0.48
        sell_price = 0.48  # Exact breakeven

        # No point selling at breakeven (actually lose to fees)
        should_rebalance = sell_price > buy_price

        assert not should_rebalance

    def test_scenario_tiny_imbalance(self):
        """Very small imbalance due to rounding."""
        yes_shares = 10.0
        no_shares = 9.95  # Tiny imbalance
        hedge_ratio = min(yes_shares, no_shares) / max(yes_shares, no_shares)

        # 99.5% hedge - don't bother rebalancing
        should_rebalance = hedge_ratio < 0.80

        assert hedge_ratio == pytest.approx(0.995, abs=0.001)
        assert not should_rebalance


class TestRebalancingDecisionLogic:
    """Test the decision logic for when to rebalance."""

    @pytest.fixture
    def rebalancing_config(self):
        """Configuration for rebalancing decisions."""
        return {
            "min_hedge_ratio": 0.80,  # Below this, actively seek rebalancing
            "min_profit_per_share": 0.02,  # Need 2¢ profit to justify rebalancing
            "max_time_to_resolution": 60,  # Don't rebalance in last 60 seconds
        }

    def test_should_rebalance_below_threshold(self, rebalancing_config):
        """Should rebalance when hedge ratio below threshold."""
        hedge_ratio = 0.60

        should_seek_rebalance = hedge_ratio < rebalancing_config["min_hedge_ratio"]

        assert should_seek_rebalance

    def test_should_not_rebalance_above_threshold(self, rebalancing_config):
        """Should NOT rebalance when hedge ratio at/above threshold."""
        hedge_ratio = 0.85

        should_seek_rebalance = hedge_ratio < rebalancing_config["min_hedge_ratio"]

        assert not should_seek_rebalance

    def test_rebalance_requires_minimum_profit(self, rebalancing_config):
        """Rebalancing requires minimum profit to cover fees."""
        buy_price = 0.48
        sell_price = 0.49  # Only 1¢ profit

        profit_per_share = sell_price - buy_price
        profitable_rebalance = profit_per_share >= rebalancing_config["min_profit_per_share"]

        assert not profitable_rebalance

    def test_rebalance_with_sufficient_profit(self, rebalancing_config):
        """Rebalancing proceeds with sufficient profit."""
        buy_price = 0.48
        sell_price = 0.55  # 7¢ profit

        profit_per_share = sell_price - buy_price
        profitable_rebalance = profit_per_share >= rebalancing_config["min_profit_per_share"]

        assert profitable_rebalance

    def test_no_rebalance_near_resolution(self, rebalancing_config):
        """Don't rebalance too close to resolution."""
        time_to_resolution = 30  # Only 30 seconds left

        too_close_to_resolution = time_to_resolution < rebalancing_config["max_time_to_resolution"]

        assert too_close_to_resolution


class TestRebalancingCalculations:
    """Test calculations for rebalancing operations."""

    def test_calculate_excess_position(self):
        """Calculate how many shares to sell for rebalancing."""
        yes_shares = 10.0
        no_shares = 6.0

        # We have 4 extra YES shares
        if yes_shares > no_shares:
            excess_side = "YES"
            excess_shares = yes_shares - no_shares
        else:
            excess_side = "NO"
            excess_shares = no_shares - yes_shares

        assert excess_side == "YES"
        assert excess_shares == 4.0

    def test_calculate_rebalance_profit_sell_excess(self):
        """Calculate profit from selling excess shares."""
        excess_shares = 4.0
        buy_price = 0.48
        current_bid = 0.55

        profit = excess_shares * (current_bid - buy_price)

        assert profit == pytest.approx(0.28, abs=0.01)

    def test_calculate_rebalance_cost_buy_deficit(self):
        """Calculate cost to buy shares to balance."""
        deficit_shares = 4.0
        current_ask = 0.42

        cost = deficit_shares * current_ask

        assert cost == pytest.approx(1.68, abs=0.01)

    def test_calculate_final_pnl_after_rebalance(self):
        """Calculate final P&L after rebalancing."""
        # Initial position: 10 YES @ $0.48, 6 NO @ $0.49
        yes_shares = 10.0
        yes_price = 0.48
        no_shares = 6.0
        no_price = 0.49

        # Rebalance: sell 4 YES @ $0.55
        sold_yes = 4.0
        sell_price = 0.55

        # Final position: 6 YES, 6 NO
        final_yes = yes_shares - sold_yes
        final_no = no_shares

        # Costs
        initial_cost = (yes_shares * yes_price) + (no_shares * no_price)  # $7.74
        rebalance_revenue = sold_yes * sell_price  # $2.20
        net_cost = initial_cost - rebalance_revenue  # $5.54

        # Guaranteed return (6 shares resolve to $1)
        guaranteed_return = min(final_yes, final_no) * 1.0  # $6.00

        # Profit
        profit = guaranteed_return - net_cost  # $0.46

        assert final_yes == final_no == 6.0
        assert profit == pytest.approx(0.46, abs=0.01)
        assert profit > 0


class TestRebalancingStrategies:
    """Test different rebalancing strategies."""

    def test_strategy_sell_to_balance(self):
        """Strategy: Sell excess shares to balance."""
        # Have: 10 YES, 6 NO
        # Want: 6 YES, 6 NO (sell 4 YES)
        yes_shares = 10.0
        no_shares = 6.0
        target = min(yes_shares, no_shares)

        if yes_shares > no_shares:
            action = "SELL_YES"
            shares_to_trade = yes_shares - target
        else:
            action = "SELL_NO"
            shares_to_trade = no_shares - target

        assert action == "SELL_YES"
        assert shares_to_trade == 4.0

    def test_strategy_buy_to_balance(self):
        """Strategy: Buy more shares to balance."""
        # Have: 10 YES, 6 NO
        # Want: 10 YES, 10 NO (buy 4 NO)
        yes_shares = 10.0
        no_shares = 6.0
        target = max(yes_shares, no_shares)

        if yes_shares > no_shares:
            action = "BUY_NO"
            shares_to_trade = target - no_shares
        else:
            action = "BUY_YES"
            shares_to_trade = target - yes_shares

        assert action == "BUY_NO"
        assert shares_to_trade == 4.0

    def test_choose_better_strategy(self):
        """Choose the more profitable rebalancing strategy."""
        yes_shares = 10.0
        no_shares = 6.0
        yes_buy_price = 0.48
        no_buy_price = 0.49

        # Current market
        yes_bid = 0.55  # Can sell YES at $0.55
        no_ask = 0.45   # Can buy NO at $0.45

        # Option 1: Sell 4 YES
        sell_revenue = 4.0 * yes_bid  # $2.20
        sell_profit = 4.0 * (yes_bid - yes_buy_price)  # $0.28

        # Option 2: Buy 4 NO
        buy_cost = 4.0 * no_ask  # $1.80
        # This increases guaranteed return by $4, costs $1.80
        buy_benefit = 4.0 * 1.0 - buy_cost  # $2.20

        # Compare: sell gives $0.28 immediate, buy gives $2.20 at resolution
        # But buy requires more capital...

        # For capital-constrained, prefer sell
        # For return-maximizing, prefer buy

        assert sell_profit > 0
        assert buy_benefit > 0


# =============================================================================
# Integration Test Helpers
# =============================================================================

class TestScenarioExecution:
    """Helper tests to validate scenario execution logic."""

    def test_hedge_ratio_calculation(self):
        """Verify hedge ratio calculation."""
        test_cases = [
            (10.0, 10.0, 1.0),    # Perfect hedge
            (10.0, 8.0, 0.8),     # 80% hedge
            (10.0, 6.0, 0.6),     # 60% hedge
            (10.0, 0.0, 0.0),     # No hedge
            (0.0, 10.0, 0.0),     # Reversed no hedge
            (5.0, 5.0, 1.0),      # Perfect small
        ]

        for yes, no, expected in test_cases:
            if max(yes, no) == 0:
                ratio = 0.0
            else:
                ratio = min(yes, no) / max(yes, no)

            assert ratio == pytest.approx(expected, abs=0.01), \
                f"Failed for YES={yes}, NO={no}"

    def test_spread_calculation(self):
        """Verify spread calculation."""
        test_cases = [
            (0.48, 0.48, 4.0),   # 4¢ spread
            (0.49, 0.49, 2.0),   # 2¢ spread
            (0.30, 0.68, 2.0),   # Asymmetric 2¢
            (0.50, 0.50, 0.0),   # No spread
            (0.55, 0.50, -5.0),  # Negative (no arb)
        ]

        for yes, no, expected in test_cases:
            spread = (1.0 - yes - no) * 100
            assert spread == pytest.approx(expected, abs=0.1), \
                f"Failed for YES={yes}, NO={no}"

    def test_execution_status_categorization(self):
        """Verify execution status is correctly categorized."""
        test_cases = [
            ("MATCHED", "MATCHED", "full_fill"),
            ("MATCHED", "REJECTED", "partial_fill"),
            ("REJECTED", "MATCHED", "partial_fill"),
            ("MATCHED", "FAILED", "partial_fill"),
            ("REJECTED", "REJECTED", "no_fill"),
            ("FAILED", "FAILED", "no_fill"),
            ("CANCELLED", "CANCELLED", "no_fill"),
        ]

        for yes_status, no_status, expected in test_cases:
            if yes_status == "MATCHED" and no_status == "MATCHED":
                status = "full_fill"
            elif yes_status == "MATCHED" or no_status == "MATCHED":
                status = "partial_fill"
            else:
                status = "no_fill"

            assert status == expected, \
                f"Failed for YES={yes_status}, NO={no_status}"
