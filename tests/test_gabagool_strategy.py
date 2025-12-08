"""Tests for Gabagool arbitrage strategy."""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from src.strategies.gabagool import GabagoolStrategy


@pytest.fixture
def mock_config():
    """Create mock configuration."""
    config = MagicMock()
    config.gabagool.enabled = True
    config.gabagool.dry_run = True
    config.gabagool.min_spread_threshold = 0.02
    config.gabagool.max_trade_size_usd = 10.0
    config.gabagool.max_daily_loss_usd = 10.0
    config.gabagool.max_daily_exposure_usd = 100.0
    config.gabagool.markets = ["BTC", "ETH"]
    config.gabagool.order_timeout_seconds = 30
    return config


@pytest.fixture
def mock_client():
    """Create mock Polymarket client."""
    client = AsyncMock()
    return client


@pytest.fixture
def mock_ws_client():
    """Create mock WebSocket client."""
    ws = MagicMock()
    return ws


@pytest.fixture
def mock_market_finder():
    """Create mock market finder."""
    finder = AsyncMock()
    finder.find_active_markets = AsyncMock(return_value=[])
    return finder


class TestGabagoolStrategy:
    """Test Gabagool strategy functionality."""

    def test_calculate_position_sizes_basic(self, mock_client, mock_ws_client, mock_market_finder, mock_config):
        """Test basic position size calculation."""
        strategy = GabagoolStrategy(
            client=mock_client,
            ws_client=mock_ws_client,
            market_finder=mock_market_finder,
            config=mock_config,
        )

        yes_amount, no_amount = strategy.calculate_position_sizes(
            budget=10.0,
            yes_price=0.40,
            no_price=0.55,
        )

        # Total should equal budget
        assert yes_amount + no_amount == pytest.approx(10.0, rel=0.01)
        # Should buy more YES (cheaper)
        assert yes_amount > no_amount

    def test_calculate_position_sizes_inverse_weighting(self, mock_client, mock_ws_client, mock_market_finder, mock_config):
        """Test inverse weighting allocates more to cheaper side."""
        strategy = GabagoolStrategy(
            client=mock_client,
            ws_client=mock_ws_client,
            market_finder=mock_market_finder,
            config=mock_config,
        )

        # YES is much cheaper
        yes_amount, no_amount = strategy.calculate_position_sizes(
            budget=10.0,
            yes_price=0.20,
            no_price=0.75,
        )

        # YES should get significantly more
        assert yes_amount > no_amount
        # Ratio should reflect inverse weighting
        total = 0.20 + 0.75
        expected_yes_weight = 0.75 / total
        expected_no_weight = 0.20 / total
        assert yes_amount / 10.0 == pytest.approx(expected_yes_weight, rel=0.01)

    def test_calculate_position_sizes_capped(self, mock_client, mock_ws_client, mock_market_finder, mock_config):
        """Test position sizes are capped at max trade size."""
        strategy = GabagoolStrategy(
            client=mock_client,
            ws_client=mock_ws_client,
            market_finder=mock_market_finder,
            config=mock_config,
        )

        # Try to allocate more than max
        yes_amount, no_amount = strategy.calculate_position_sizes(
            budget=100.0,  # Large budget
            yes_price=0.40,
            no_price=0.55,
        )

        # Both should be capped at max_trade_size_usd (10.0)
        assert yes_amount <= 10.0
        assert no_amount <= 10.0

    def test_calculate_position_sizes_zero_prices(self, mock_client, mock_ws_client, mock_market_finder, mock_config):
        """Test zero prices return zero allocation."""
        strategy = GabagoolStrategy(
            client=mock_client,
            ws_client=mock_ws_client,
            market_finder=mock_market_finder,
            config=mock_config,
        )

        yes_amount, no_amount = strategy.calculate_position_sizes(
            budget=10.0,
            yes_price=0.0,
            no_price=0.0,
        )

        assert yes_amount == 0.0
        assert no_amount == 0.0

    def test_calculate_mispricing(self):
        """Test mispricing calculation."""
        spread = GabagoolStrategy.calculate_mispricing(0.40, 0.55)
        assert spread == pytest.approx(0.05, rel=0.01)

        spread = GabagoolStrategy.calculate_mispricing(0.50, 0.50)
        assert spread == 0.0

        # Negative spread (overpriced)
        spread = GabagoolStrategy.calculate_mispricing(0.55, 0.50)
        assert spread == pytest.approx(-0.05, rel=0.01)

    def test_should_enter_above_threshold(self):
        """Test should_enter returns True when spread exceeds threshold."""
        assert GabagoolStrategy.should_enter(0.05, threshold=0.02)
        assert GabagoolStrategy.should_enter(0.02, threshold=0.02)

    def test_should_enter_below_threshold(self):
        """Test should_enter returns False when spread below threshold."""
        assert not GabagoolStrategy.should_enter(0.01, threshold=0.02)
        assert not GabagoolStrategy.should_enter(0.0, threshold=0.02)


class TestOpportunityValidation:
    """Test opportunity validation logic."""

    def test_validate_opportunity_valid(self, mock_client, mock_ws_client, mock_market_finder, mock_config):
        """Test validation passes for valid opportunity."""
        strategy = GabagoolStrategy(
            client=mock_client,
            ws_client=mock_ws_client,
            market_finder=mock_market_finder,
            config=mock_config,
        )

        opportunity = MagicMock()
        opportunity.spread_cents = 5.0  # 5 cents > 2 cent threshold
        opportunity.market.is_tradeable = True
        opportunity.market.seconds_remaining = 600
        opportunity.yes_price = 0.40
        opportunity.no_price = 0.55

        assert strategy._validate_opportunity(opportunity)

    def test_validate_opportunity_spread_too_small(self, mock_client, mock_ws_client, mock_market_finder, mock_config):
        """Test validation fails for small spread."""
        strategy = GabagoolStrategy(
            client=mock_client,
            ws_client=mock_ws_client,
            market_finder=mock_market_finder,
            config=mock_config,
        )

        opportunity = MagicMock()
        opportunity.spread_cents = 1.0  # 1 cent < 2 cent threshold

        assert not strategy._validate_opportunity(opportunity)

    def test_validate_opportunity_market_not_tradeable(self, mock_client, mock_ws_client, mock_market_finder, mock_config):
        """Test validation fails for non-tradeable market."""
        strategy = GabagoolStrategy(
            client=mock_client,
            ws_client=mock_ws_client,
            market_finder=mock_market_finder,
            config=mock_config,
        )

        opportunity = MagicMock()
        opportunity.spread_cents = 5.0
        opportunity.market.is_tradeable = False

        assert not strategy._validate_opportunity(opportunity)

    def test_validate_opportunity_not_enough_time(self, mock_client, mock_ws_client, mock_market_finder, mock_config):
        """Test validation fails when less than 60 seconds remain."""
        strategy = GabagoolStrategy(
            client=mock_client,
            ws_client=mock_ws_client,
            market_finder=mock_market_finder,
            config=mock_config,
        )

        opportunity = MagicMock()
        opportunity.spread_cents = 5.0
        opportunity.market.is_tradeable = True
        opportunity.market.seconds_remaining = 30

        assert not strategy._validate_opportunity(opportunity)

    def test_validate_opportunity_no_profit(self, mock_client, mock_ws_client, mock_market_finder, mock_config):
        """Test validation fails when prices sum >= 1."""
        strategy = GabagoolStrategy(
            client=mock_client,
            ws_client=mock_ws_client,
            market_finder=mock_market_finder,
            config=mock_config,
        )

        opportunity = MagicMock()
        opportunity.spread_cents = 0  # No profit
        opportunity.market.is_tradeable = True
        opportunity.market.seconds_remaining = 600
        opportunity.yes_price = 0.50
        opportunity.no_price = 0.50

        assert not strategy._validate_opportunity(opportunity)


class TestProfitCalculations:
    """Test profit calculations for different scenarios."""

    def test_profit_calculation_equal_shares(self):
        """Test profit when buying equal shares."""
        # If we buy 10 YES @ $0.40 = $4.00, 10 NO @ $0.55 = $5.50
        # Total cost = $9.50
        # At resolution: min(10, 10) * $1 = $10.00
        # Profit = $10.00 - $9.50 = $0.50
        yes_shares = 10.0
        no_shares = 10.0
        yes_cost = 4.0
        no_cost = 5.5

        min_shares = min(yes_shares, no_shares)
        total_cost = yes_cost + no_cost
        profit = min_shares - total_cost

        assert profit == pytest.approx(0.50, rel=0.01)

    def test_profit_calculation_unequal_shares(self):
        """Test profit when shares are unequal."""
        # 12 YES @ $0.40 = $4.80, 8 NO @ $0.55 = $4.40
        # Total cost = $9.20
        # At resolution: min(12, 8) = 8 pairs = $8.00
        # LOSS of $1.20 on the 4 unhedged YES
        yes_shares = 12.0
        no_shares = 8.0
        yes_cost = 4.8
        no_cost = 4.4

        min_shares = min(yes_shares, no_shares)
        total_cost = yes_cost + no_cost
        profit = min_shares - total_cost

        assert profit == pytest.approx(-1.20, rel=0.01)

    def test_profit_with_spread_scaling(self):
        """Test profit scales with spread."""
        # 5 cent spread: YES $0.45, NO $0.50 → 5% profit
        spread_5 = 1.0 - (0.45 + 0.50)
        assert spread_5 == pytest.approx(0.05, rel=0.01)

        # 2 cent spread: YES $0.49, NO $0.49 → 2% profit
        spread_2 = 1.0 - (0.49 + 0.49)
        assert spread_2 == pytest.approx(0.02, rel=0.01)
