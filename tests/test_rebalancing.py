"""Tests for Position Rebalancing Strategy

These tests validate the rebalancing logic for partial fills:
1. Detection of positions needing rebalancing
2. Identification of rebalancing opportunities
3. Selection of best rebalancing action
4. Execution of rebalancing trades
5. Profit calculations
"""

import pytest
from dataclasses import dataclass
from typing import Optional, List
from datetime import datetime, timedelta


# =============================================================================
# Data Structures (mirroring implementation)
# =============================================================================

@dataclass
class RebalanceOption:
    """A potential rebalancing action."""
    action: str  # SELL_YES, SELL_NO, BUY_YES, BUY_NO
    shares: float
    price: float
    profit: float

    @property
    def profit_per_share(self) -> float:
        return self.profit / self.shares if self.shares > 0 else 0


@dataclass
class UnbalancedPosition:
    """A position that may need rebalancing."""
    trade_id: str
    yes_shares: float
    no_shares: float
    yes_entry_price: float
    no_entry_price: float
    resolution_time: datetime
    created_at: datetime = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.utcnow()

    @property
    def hedge_ratio(self) -> float:
        if max(self.yes_shares, self.no_shares) == 0:
            return 0.0
        return min(self.yes_shares, self.no_shares) / max(self.yes_shares, self.no_shares)

    @property
    def needs_rebalancing(self) -> bool:
        return self.hedge_ratio < 0.80

    @property
    def excess_side(self) -> str:
        return "YES" if self.yes_shares > self.no_shares else "NO"

    @property
    def deficit_side(self) -> str:
        return "NO" if self.yes_shares > self.no_shares else "YES"

    @property
    def excess_shares(self) -> float:
        return abs(self.yes_shares - self.no_shares)

    @property
    def total_cost(self) -> float:
        return (self.yes_shares * self.yes_entry_price) + (self.no_shares * self.no_entry_price)


@dataclass
class RebalancingConfig:
    """Configuration for rebalancing behavior."""
    min_hedge_ratio: float = 0.80
    min_profit_per_share: float = 0.02
    max_rebalance_wait_seconds: float = 60.0
    prefer_sell_over_buy: bool = True


# =============================================================================
# Rebalancing Logic (to be implemented in strategy)
# =============================================================================

def get_rebalancing_options(
    position: UnbalancedPosition,
    current_yes_bid: float,
    current_yes_ask: float,
    current_no_bid: float,
    current_no_ask: float,
) -> List[RebalanceOption]:
    """Get available rebalancing options for a position."""
    options = []

    if position.yes_shares > position.no_shares:
        excess = position.yes_shares - position.no_shares

        # Option A: Sell excess YES
        sell_profit = excess * (current_yes_bid - position.yes_entry_price)
        if sell_profit > 0:
            options.append(RebalanceOption(
                action="SELL_YES",
                shares=excess,
                price=current_yes_bid,
                profit=sell_profit,
            ))

        # Option B: Buy more NO to balance
        buy_cost = excess * current_no_ask
        new_total_cost = position.total_cost + buy_cost
        guaranteed_return = position.yes_shares * 1.0  # All shares now hedged
        buy_profit = guaranteed_return - new_total_cost
        if buy_profit > 0:
            options.append(RebalanceOption(
                action="BUY_NO",
                shares=excess,
                price=current_no_ask,
                profit=buy_profit,
            ))

    elif position.no_shares > position.yes_shares:
        excess = position.no_shares - position.yes_shares

        # Option A: Sell excess NO
        sell_profit = excess * (current_no_bid - position.no_entry_price)
        if sell_profit > 0:
            options.append(RebalanceOption(
                action="SELL_NO",
                shares=excess,
                price=current_no_bid,
                profit=sell_profit,
            ))

        # Option B: Buy more YES to balance
        buy_cost = excess * current_yes_ask
        new_total_cost = position.total_cost + buy_cost
        guaranteed_return = position.no_shares * 1.0
        buy_profit = guaranteed_return - new_total_cost
        if buy_profit > 0:
            options.append(RebalanceOption(
                action="BUY_YES",
                shares=excess,
                price=current_yes_ask,
                profit=buy_profit,
            ))

    return options


def select_best_option(
    options: List[RebalanceOption],
    config: RebalancingConfig,
) -> Optional[RebalanceOption]:
    """Select the best rebalancing option."""
    if not options:
        return None

    # Filter by minimum profit threshold
    viable = [
        opt for opt in options
        if opt.profit_per_share >= config.min_profit_per_share
    ]

    if not viable:
        return None

    # If preferring sell, check sell options first
    if config.prefer_sell_over_buy:
        sell_options = [o for o in viable if o.action.startswith("SELL")]
        if sell_options:
            return max(sell_options, key=lambda o: o.profit)

    # Otherwise, return highest profit option
    return max(viable, key=lambda o: o.profit)


# =============================================================================
# Tests: Position Detection
# =============================================================================

class TestPositionNeedsRebalancing:
    """Test detection of positions needing rebalancing."""

    def test_perfect_hedge_no_rebalance(self):
        """100% hedge ratio - no rebalancing needed."""
        position = UnbalancedPosition(
            trade_id="test-1",
            yes_shares=10.0,
            no_shares=10.0,
            yes_entry_price=0.48,
            no_entry_price=0.49,
            resolution_time=datetime.utcnow() + timedelta(minutes=10),
        )

        assert position.hedge_ratio == 1.0
        assert not position.needs_rebalancing

    def test_80_percent_hedge_no_rebalance(self):
        """80% hedge ratio (at threshold) - no rebalancing needed."""
        position = UnbalancedPosition(
            trade_id="test-2",
            yes_shares=10.0,
            no_shares=8.0,
            yes_entry_price=0.48,
            no_entry_price=0.49,
            resolution_time=datetime.utcnow() + timedelta(minutes=10),
        )

        assert position.hedge_ratio == 0.80
        assert not position.needs_rebalancing

    def test_79_percent_hedge_needs_rebalance(self):
        """79% hedge ratio (just below threshold) - needs rebalancing."""
        position = UnbalancedPosition(
            trade_id="test-3",
            yes_shares=10.0,
            no_shares=7.9,
            yes_entry_price=0.48,
            no_entry_price=0.49,
            resolution_time=datetime.utcnow() + timedelta(minutes=10),
        )

        assert position.hedge_ratio == 0.79
        assert position.needs_rebalancing

    def test_60_percent_hedge_needs_rebalance(self):
        """60% hedge ratio - definitely needs rebalancing."""
        position = UnbalancedPosition(
            trade_id="test-4",
            yes_shares=10.0,
            no_shares=6.0,
            yes_entry_price=0.48,
            no_entry_price=0.49,
            resolution_time=datetime.utcnow() + timedelta(minutes=10),
        )

        assert position.hedge_ratio == 0.60
        assert position.needs_rebalancing

    def test_zero_hedge_needs_rebalance(self):
        """0% hedge ratio (one leg only) - definitely needs rebalancing."""
        position = UnbalancedPosition(
            trade_id="test-5",
            yes_shares=10.0,
            no_shares=0.0,
            yes_entry_price=0.48,
            no_entry_price=0.49,
            resolution_time=datetime.utcnow() + timedelta(minutes=10),
        )

        assert position.hedge_ratio == 0.0
        assert position.needs_rebalancing

    def test_excess_side_yes(self):
        """Correctly identifies YES as excess side."""
        position = UnbalancedPosition(
            trade_id="test-6",
            yes_shares=10.0,
            no_shares=6.0,
            yes_entry_price=0.48,
            no_entry_price=0.49,
            resolution_time=datetime.utcnow() + timedelta(minutes=10),
        )

        assert position.excess_side == "YES"
        assert position.deficit_side == "NO"
        assert position.excess_shares == 4.0

    def test_excess_side_no(self):
        """Correctly identifies NO as excess side."""
        position = UnbalancedPosition(
            trade_id="test-7",
            yes_shares=6.0,
            no_shares=10.0,
            yes_entry_price=0.48,
            no_entry_price=0.49,
            resolution_time=datetime.utcnow() + timedelta(minutes=10),
        )

        assert position.excess_side == "NO"
        assert position.deficit_side == "YES"
        assert position.excess_shares == 4.0


# =============================================================================
# Tests: Rebalancing Options
# =============================================================================

class TestRebalancingOptions:
    """Test identification of rebalancing opportunities."""

    @pytest.fixture
    def long_yes_position(self):
        """Position with excess YES shares."""
        return UnbalancedPosition(
            trade_id="test-long-yes",
            yes_shares=10.0,
            no_shares=6.0,
            yes_entry_price=0.48,
            no_entry_price=0.49,
            resolution_time=datetime.utcnow() + timedelta(minutes=10),
        )

    @pytest.fixture
    def long_no_position(self):
        """Position with excess NO shares."""
        return UnbalancedPosition(
            trade_id="test-long-no",
            yes_shares=6.0,
            no_shares=10.0,
            yes_entry_price=0.48,
            no_entry_price=0.49,
            resolution_time=datetime.utcnow() + timedelta(minutes=10),
        )

    def test_sell_yes_opportunity(self, long_yes_position):
        """Identify opportunity to sell excess YES at profit."""
        options = get_rebalancing_options(
            position=long_yes_position,
            current_yes_bid=0.55,  # +$0.07 from entry
            current_yes_ask=0.56,
            current_no_bid=0.44,
            current_no_ask=0.45,
        )

        sell_options = [o for o in options if o.action == "SELL_YES"]
        assert len(sell_options) == 1

        sell = sell_options[0]
        assert sell.shares == 4.0
        assert sell.price == 0.55
        assert sell.profit == pytest.approx(0.28, abs=0.01)  # 4 × $0.07

    def test_buy_no_opportunity(self, long_yes_position):
        """Identify opportunity to buy more NO to balance."""
        options = get_rebalancing_options(
            position=long_yes_position,
            current_yes_bid=0.50,
            current_yes_ask=0.51,
            current_no_bid=0.44,
            current_no_ask=0.42,  # NO dropped from $0.49 to $0.42
        )

        buy_options = [o for o in options if o.action == "BUY_NO"]
        assert len(buy_options) == 1

        buy = buy_options[0]
        assert buy.shares == 4.0
        assert buy.price == 0.42
        # Cost: 4 × $0.42 = $1.68
        # New total: $4.80 (YES) + $2.94 (6 NO) + $1.68 (4 NO) = $9.42
        # Return: $10.00
        # Profit: $0.58
        assert buy.profit == pytest.approx(0.58, abs=0.01)

    def test_sell_no_opportunity(self, long_no_position):
        """Identify opportunity to sell excess NO at profit."""
        options = get_rebalancing_options(
            position=long_no_position,
            current_yes_bid=0.44,
            current_yes_ask=0.45,
            current_no_bid=0.55,  # +$0.06 from entry
            current_no_ask=0.56,
        )

        sell_options = [o for o in options if o.action == "SELL_NO"]
        assert len(sell_options) == 1

        sell = sell_options[0]
        assert sell.shares == 4.0
        assert sell.price == 0.55
        assert sell.profit == pytest.approx(0.24, abs=0.01)  # 4 × $0.06

    def test_buy_yes_opportunity(self, long_no_position):
        """Identify opportunity to buy more YES to balance."""
        options = get_rebalancing_options(
            position=long_no_position,
            current_yes_bid=0.40,
            current_yes_ask=0.41,  # YES dropped from $0.48 to $0.41
            current_no_bid=0.54,
            current_no_ask=0.55,
        )

        buy_options = [o for o in options if o.action == "BUY_YES"]
        assert len(buy_options) == 1

        buy = buy_options[0]
        assert buy.shares == 4.0
        assert buy.price == 0.41

    def test_no_opportunity_prices_unfavorable(self, long_yes_position):
        """No opportunities when prices are unfavorable."""
        options = get_rebalancing_options(
            position=long_yes_position,
            current_yes_bid=0.40,  # Below entry - can't sell at profit
            current_yes_ask=0.42,
            current_no_bid=0.55,
            current_no_ask=0.60,  # Above entry - buying adds to loss
        )

        # All options would result in loss
        profitable_options = [o for o in options if o.profit > 0]
        assert len(profitable_options) == 0

    def test_multiple_opportunities(self, long_yes_position):
        """Both sell and buy opportunities available."""
        options = get_rebalancing_options(
            position=long_yes_position,
            current_yes_bid=0.55,  # Can sell YES at profit
            current_yes_ask=0.56,
            current_no_bid=0.40,
            current_no_ask=0.42,  # Can buy NO cheaply
        )

        assert len(options) == 2
        actions = {o.action for o in options}
        assert "SELL_YES" in actions
        assert "BUY_NO" in actions


# =============================================================================
# Tests: Option Selection
# =============================================================================

class TestOptionSelection:
    """Test selection of best rebalancing option."""

    @pytest.fixture
    def config(self):
        return RebalancingConfig(
            min_hedge_ratio=0.80,
            min_profit_per_share=0.02,
            prefer_sell_over_buy=True,
        )

    def test_select_only_option(self, config):
        """Select the only available option."""
        options = [
            RebalanceOption(
                action="SELL_YES",
                shares=4.0,
                price=0.55,
                profit=0.28,
            )
        ]

        best = select_best_option(options, config)
        assert best is not None
        assert best.action == "SELL_YES"

    def test_select_higher_profit(self, config):
        """Select the option with higher profit."""
        config.prefer_sell_over_buy = False  # Disable preference

        options = [
            RebalanceOption(action="SELL_YES", shares=4.0, price=0.55, profit=0.28),
            RebalanceOption(action="BUY_NO", shares=4.0, price=0.42, profit=0.58),
        ]

        best = select_best_option(options, config)
        assert best.action == "BUY_NO"  # Higher profit

    def test_prefer_sell_when_configured(self, config):
        """Prefer sell over buy when configured."""
        config.prefer_sell_over_buy = True

        options = [
            RebalanceOption(action="SELL_YES", shares=4.0, price=0.55, profit=0.20),
            RebalanceOption(action="BUY_NO", shares=4.0, price=0.42, profit=0.58),
        ]

        best = select_best_option(options, config)
        assert best.action == "SELL_YES"  # Preferred even if lower profit

    def test_filter_below_threshold(self, config):
        """Filter out options below profit threshold."""
        config.min_profit_per_share = 0.05

        options = [
            RebalanceOption(action="SELL_YES", shares=4.0, price=0.49, profit=0.04),
            # profit_per_share = 0.01, below 0.05 threshold
        ]

        best = select_best_option(options, config)
        assert best is None

    def test_no_options_returns_none(self, config):
        """Return None when no options available."""
        best = select_best_option([], config)
        assert best is None


# =============================================================================
# Tests: Profit Calculations
# =============================================================================

class TestProfitCalculations:
    """Test profit calculations for rebalancing scenarios."""

    def test_sell_profit_calculation(self):
        """Calculate profit from selling excess shares."""
        entry_price = 0.48
        sell_price = 0.55
        shares = 4.0

        profit = shares * (sell_price - entry_price)

        assert profit == pytest.approx(0.28, abs=0.01)

    def test_buy_profit_calculation(self):
        """Calculate profit from buying to balance."""
        # Initial: 10 YES @ $0.48, 6 NO @ $0.49
        initial_cost = 10.0 * 0.48 + 6.0 * 0.49  # $7.74

        # Buy 4 more NO @ $0.42
        additional_cost = 4.0 * 0.42  # $1.68

        total_cost = initial_cost + additional_cost  # $9.42

        # Guaranteed return: 10 shares × $1
        guaranteed_return = 10.0 * 1.0  # $10.00

        profit = guaranteed_return - total_cost  # $0.58

        assert profit == pytest.approx(0.58, abs=0.01)

    def test_breakeven_no_profit(self):
        """Breakeven scenario has zero profit."""
        entry_price = 0.48
        sell_price = 0.48
        shares = 4.0

        profit = shares * (sell_price - entry_price)

        assert profit == 0.0

    def test_loss_negative_profit(self):
        """Selling below entry results in negative profit."""
        entry_price = 0.48
        sell_price = 0.40
        shares = 4.0

        profit = shares * (sell_price - entry_price)

        assert profit == pytest.approx(-0.32, abs=0.01)

    def test_total_position_cost(self):
        """Calculate total cost of position."""
        position = UnbalancedPosition(
            trade_id="test",
            yes_shares=10.0,
            no_shares=6.0,
            yes_entry_price=0.48,
            no_entry_price=0.49,
            resolution_time=datetime.utcnow() + timedelta(minutes=10),
        )

        expected_cost = 10.0 * 0.48 + 6.0 * 0.49  # $4.80 + $2.94 = $7.74

        assert position.total_cost == pytest.approx(7.74, abs=0.01)


# =============================================================================
# Tests: Edge Cases
# =============================================================================

class TestEdgeCases:
    """Test edge cases in rebalancing logic."""

    def test_zero_shares(self):
        """Handle zero shares gracefully."""
        position = UnbalancedPosition(
            trade_id="test",
            yes_shares=0.0,
            no_shares=0.0,
            yes_entry_price=0.48,
            no_entry_price=0.49,
            resolution_time=datetime.utcnow() + timedelta(minutes=10),
        )

        assert position.hedge_ratio == 0.0
        assert position.excess_shares == 0.0

    def test_exactly_balanced(self):
        """Exactly balanced position doesn't need rebalancing."""
        position = UnbalancedPosition(
            trade_id="test",
            yes_shares=10.0,
            no_shares=10.0,
            yes_entry_price=0.48,
            no_entry_price=0.49,
            resolution_time=datetime.utcnow() + timedelta(minutes=10),
        )

        assert position.hedge_ratio == 1.0
        assert not position.needs_rebalancing
        assert position.excess_shares == 0.0

    def test_tiny_imbalance(self):
        """Tiny imbalance from rounding doesn't trigger rebalancing."""
        position = UnbalancedPosition(
            trade_id="test",
            yes_shares=10.0,
            no_shares=9.99,  # 0.01 share difference
            yes_entry_price=0.48,
            no_entry_price=0.49,
            resolution_time=datetime.utcnow() + timedelta(minutes=10),
        )

        # 99.9% hedge ratio - above threshold
        assert position.hedge_ratio == pytest.approx(0.999, abs=0.001)
        assert not position.needs_rebalancing

    def test_very_small_profit_filtered(self):
        """Very small profits are filtered out."""
        config = RebalancingConfig(min_profit_per_share=0.02)

        options = [
            RebalanceOption(
                action="SELL_YES",
                shares=4.0,
                price=0.485,  # Only $0.005 above entry of $0.48
                profit=0.02,  # Only $0.005 per share
            )
        ]

        best = select_best_option(options, config)
        assert best is None  # Filtered out


# =============================================================================
# Tests: Time Constraints
# =============================================================================

class TestTimeConstraints:
    """Test time-based constraints on rebalancing."""

    def test_position_near_resolution(self):
        """Position close to resolution."""
        position = UnbalancedPosition(
            trade_id="test",
            yes_shares=10.0,
            no_shares=6.0,
            yes_entry_price=0.48,
            no_entry_price=0.49,
            resolution_time=datetime.utcnow() + timedelta(seconds=30),
        )

        time_remaining = (position.resolution_time - datetime.utcnow()).total_seconds()
        max_rebalance_wait = 60.0

        too_close = time_remaining < max_rebalance_wait

        assert too_close

    def test_position_has_time(self):
        """Position with plenty of time before resolution."""
        position = UnbalancedPosition(
            trade_id="test",
            yes_shares=10.0,
            no_shares=6.0,
            yes_entry_price=0.48,
            no_entry_price=0.49,
            resolution_time=datetime.utcnow() + timedelta(minutes=10),
        )

        time_remaining = (position.resolution_time - datetime.utcnow()).total_seconds()
        max_rebalance_wait = 60.0

        has_time = time_remaining >= max_rebalance_wait

        assert has_time


# =============================================================================
# Tests: Complete Scenarios
# =============================================================================

class TestCompleteRebalancingScenarios:
    """Test complete rebalancing scenarios end-to-end."""

    def test_scenario_sell_to_profit(self):
        """Complete scenario: sell excess shares at profit."""
        # 1. Create unbalanced position
        position = UnbalancedPosition(
            trade_id="scenario-1",
            yes_shares=10.0,
            no_shares=6.0,
            yes_entry_price=0.48,
            no_entry_price=0.49,
            resolution_time=datetime.utcnow() + timedelta(minutes=10),
        )

        # 2. Verify needs rebalancing
        assert position.needs_rebalancing
        assert position.hedge_ratio == 0.60

        # 3. Get options with favorable YES price
        options = get_rebalancing_options(
            position=position,
            current_yes_bid=0.55,
            current_yes_ask=0.56,
            current_no_bid=0.42,
            current_no_ask=0.44,
        )

        # 4. Select best option
        config = RebalancingConfig(prefer_sell_over_buy=True)
        best = select_best_option(options, config)

        # 5. Verify decision
        assert best is not None
        assert best.action == "SELL_YES"
        assert best.shares == 4.0
        assert best.profit > 0

        # 6. Simulate execution
        new_yes_shares = position.yes_shares - best.shares  # 10 - 4 = 6

        # 7. Verify final state
        final_hedge_ratio = min(new_yes_shares, position.no_shares) / max(new_yes_shares, position.no_shares)
        assert final_hedge_ratio == 1.0

    def test_scenario_buy_to_balance(self):
        """Complete scenario: buy deficit shares to balance."""
        # 1. Create unbalanced position
        position = UnbalancedPosition(
            trade_id="scenario-2",
            yes_shares=10.0,
            no_shares=6.0,
            yes_entry_price=0.48,
            no_entry_price=0.49,
            resolution_time=datetime.utcnow() + timedelta(minutes=10),
        )

        # 2. Get options with favorable NO price
        options = get_rebalancing_options(
            position=position,
            current_yes_bid=0.46,  # YES dropped - can't sell profitably
            current_yes_ask=0.47,
            current_no_bid=0.40,
            current_no_ask=0.42,  # NO cheap to buy
        )

        # 3. Select best option (only buy should be profitable)
        config = RebalancingConfig(prefer_sell_over_buy=False)
        best = select_best_option(options, config)

        # 4. Verify decision
        assert best is not None
        assert best.action == "BUY_NO"
        assert best.shares == 4.0
        assert best.profit > 0

        # 5. Simulate execution
        new_no_shares = position.no_shares + best.shares  # 6 + 4 = 10

        # 6. Verify final state
        final_hedge_ratio = min(position.yes_shares, new_no_shares) / max(position.yes_shares, new_no_shares)
        assert final_hedge_ratio == 1.0

    def test_scenario_no_opportunity(self):
        """Complete scenario: no profitable rebalancing opportunity."""
        # 1. Create unbalanced position
        position = UnbalancedPosition(
            trade_id="scenario-3",
            yes_shares=10.0,
            no_shares=6.0,
            yes_entry_price=0.48,
            no_entry_price=0.49,
            resolution_time=datetime.utcnow() + timedelta(minutes=10),
        )

        # 2. Get options with unfavorable prices
        options = get_rebalancing_options(
            position=position,
            current_yes_bid=0.40,  # Way below entry
            current_yes_ask=0.42,
            current_no_bid=0.55,
            current_no_ask=0.58,  # Way above entry
        )

        # 3. All options should be unprofitable or filtered
        config = RebalancingConfig()
        best = select_best_option(options, config)

        # 4. No viable option
        assert best is None

        # 5. Position remains unbalanced - hold to resolution
        assert position.needs_rebalancing
