"""Tests for position sizing calculations."""

import pytest
from unittest.mock import MagicMock

from src.risk.position_sizing import PositionSizer, PositionSize


@pytest.fixture
def config():
    """Create a mock config for testing."""
    config = MagicMock()
    config.min_spread_threshold = 0.02  # 2 cents
    config.max_trade_size_usd = 10.0
    config.max_per_window_usd = 20.0
    config.max_daily_exposure_usd = 100.0
    return config


@pytest.fixture
def sizer(config):
    """Create a PositionSizer instance."""
    return PositionSizer(config)


class TestPositionSizer:
    """Test PositionSizer calculations."""

    def test_calculate_basic(self, sizer):
        """Test basic position calculation with valid spread."""
        # YES = 0.40, NO = 0.55, spread = 0.05 (5 cents)
        result = sizer.calculate(
            yes_price=0.40,
            no_price=0.55,
            available_budget=10.0,
        )

        assert result.is_valid
        assert result.total_cost == pytest.approx(10.0, rel=0.01)
        assert result.expected_profit > 0
        # YES weight = 0.55/0.95 = 0.579, NO weight = 0.40/0.95 = 0.421
        assert result.yes_amount_usd > result.no_amount_usd  # More YES (cheaper)

    def test_calculate_inverse_weighting(self, sizer):
        """Test that inverse weighting allocates more to cheaper side."""
        # YES is cheaper, so should get more allocation
        result = sizer.calculate(
            yes_price=0.30,  # Cheaper
            no_price=0.65,
            available_budget=10.0,
        )

        # YES weight = 0.65/0.95, NO weight = 0.30/0.95
        # Should buy more YES because it's cheaper
        assert result.yes_amount_usd > result.no_amount_usd

    def test_calculate_zero_spread(self, sizer):
        """Test that zero/negative spread returns zero position."""
        # YES + NO = 1.0, no profit
        result = sizer.calculate(
            yes_price=0.50,
            no_price=0.50,
        )

        assert not result.is_valid
        assert result.total_cost == 0

    def test_calculate_below_threshold(self, sizer):
        """Test that spread below threshold returns zero position."""
        # Spread = 1 cent, below 2 cent threshold
        result = sizer.calculate(
            yes_price=0.495,
            no_price=0.495,
        )

        assert not result.is_valid
        assert result.total_cost == 0

    def test_calculate_with_size_multiplier(self, sizer):
        """Test position sizing with reduced multiplier (circuit breaker)."""
        result_full = sizer.calculate(
            yes_price=0.40,
            no_price=0.55,
            available_budget=10.0,
            size_multiplier=1.0,
        )

        result_half = sizer.calculate(
            yes_price=0.40,
            no_price=0.55,
            available_budget=10.0,
            size_multiplier=0.5,
        )

        assert result_half.total_cost == pytest.approx(result_full.total_cost * 0.5, rel=0.01)

    def test_calculate_kelly(self, sizer):
        """Test Kelly criterion position sizing."""
        result = sizer.calculate_kelly(
            yes_price=0.40,
            no_price=0.55,
            win_probability=0.95,
            kelly_fraction=0.25,
            bankroll=100.0,
        )

        assert result.is_valid
        # Kelly should give a reasonable position size
        assert 0 < result.total_cost <= 10.0  # Capped at max_trade_size

    def test_calculate_spread_scaled(self, sizer):
        """Test spread-scaled position sizing."""
        # Small spread = smaller position
        result_small = sizer.calculate_spread_scaled(
            yes_price=0.48,
            no_price=0.50,  # 2 cent spread
        )

        # Large spread = larger position
        result_large = sizer.calculate_spread_scaled(
            yes_price=0.40,
            no_price=0.55,  # 5 cent spread
        )

        assert result_large.total_cost >= result_small.total_cost

    def test_validate_position_valid(self, sizer):
        """Test validation of a valid position."""
        position = PositionSize(
            yes_amount_usd=5.0,
            no_amount_usd=5.0,
            yes_shares=10.0,
            no_shares=10.0,
            total_cost=10.0,
            expected_profit=0.50,
            profit_percentage=5.0,
        )

        is_valid, reason = sizer.validate_position(position)
        assert is_valid
        assert reason == "OK"

    def test_validate_position_exceeds_daily_limit(self, sizer):
        """Test validation fails when would exceed daily exposure."""
        position = PositionSize(
            yes_amount_usd=5.0,
            no_amount_usd=5.0,
            yes_shares=10.0,
            no_shares=10.0,
            total_cost=10.0,
            expected_profit=0.50,
            profit_percentage=5.0,
        )

        # Already at 95 exposure, adding 10 would exceed 100 limit
        is_valid, reason = sizer.validate_position(
            position, current_daily_exposure=95.0
        )
        assert not is_valid
        assert "daily exposure" in reason.lower()


class TestPositionSize:
    """Test PositionSize dataclass."""

    def test_is_valid_true(self):
        """Test is_valid returns True for valid position."""
        position = PositionSize(
            yes_amount_usd=5.0,
            no_amount_usd=5.0,
            yes_shares=10.0,
            no_shares=10.0,
            total_cost=10.0,
            expected_profit=0.50,
            profit_percentage=5.0,
        )
        assert position.is_valid

    def test_is_valid_false_zero_cost(self):
        """Test is_valid returns False for zero cost."""
        position = PositionSize(
            yes_amount_usd=0.0,
            no_amount_usd=0.0,
            yes_shares=0.0,
            no_shares=0.0,
            total_cost=0.0,
            expected_profit=0.0,
            profit_percentage=0.0,
        )
        assert not position.is_valid

    def test_is_valid_false_negative_profit(self):
        """Test is_valid returns False for negative profit."""
        position = PositionSize(
            yes_amount_usd=5.0,
            no_amount_usd=5.0,
            yes_shares=10.0,
            no_shares=10.0,
            total_cost=10.0,
            expected_profit=-0.50,
            profit_percentage=-5.0,
        )
        assert not position.is_valid
