"""Tests for circuit breaker functionality."""

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from src.risk.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerLevel,
    CircuitBreakerState,
)


@pytest.fixture
def config():
    """Create a mock config for testing."""
    config = MagicMock()
    config.min_spread_threshold = 0.02
    config.max_daily_loss_usd = 10.0
    config.max_daily_exposure_usd = 100.0
    config.max_unhedged_exposure_usd = 20.0
    config.max_slippage_cents = 1.0
    return config


@pytest.fixture
def breaker(config):
    """Create a CircuitBreaker instance."""
    return CircuitBreaker(config)


class TestCircuitBreakerState:
    """Test CircuitBreakerState dataclass."""

    def test_is_tripped_normal(self):
        """Test is_tripped is False for NORMAL level."""
        state = CircuitBreakerState(level=CircuitBreakerLevel.NORMAL)
        assert not state.is_tripped

    def test_is_tripped_warning(self):
        """Test is_tripped is False for WARNING level."""
        state = CircuitBreakerState(level=CircuitBreakerLevel.WARNING)
        assert not state.is_tripped

    def test_is_tripped_caution(self):
        """Test is_tripped is True for CAUTION level."""
        state = CircuitBreakerState(level=CircuitBreakerLevel.CAUTION)
        assert state.is_tripped

    def test_is_tripped_halt(self):
        """Test is_tripped is True for HALT level."""
        state = CircuitBreakerState(level=CircuitBreakerLevel.HALT)
        assert state.is_tripped

    def test_can_trade_normal(self):
        """Test can_trade is True for NORMAL level."""
        state = CircuitBreakerState(level=CircuitBreakerLevel.NORMAL)
        assert state.can_trade

    def test_can_trade_halt(self):
        """Test can_trade is False for HALT level."""
        state = CircuitBreakerState(level=CircuitBreakerLevel.HALT)
        assert not state.can_trade

    def test_can_trade_in_cooldown(self):
        """Test can_trade is False during cooldown."""
        state = CircuitBreakerState(
            level=CircuitBreakerLevel.WARNING,
            cooldown_until=datetime.utcnow() + timedelta(minutes=5),
        )
        assert not state.can_trade

    def test_size_multiplier_normal(self):
        """Test size_multiplier is 1.0 for NORMAL level."""
        state = CircuitBreakerState(level=CircuitBreakerLevel.NORMAL)
        assert state.size_multiplier == 1.0

    def test_size_multiplier_warning(self):
        """Test size_multiplier is 0.5 for WARNING level."""
        state = CircuitBreakerState(level=CircuitBreakerLevel.WARNING)
        assert state.size_multiplier == 0.5

    def test_size_multiplier_caution(self):
        """Test size_multiplier is 0.0 for CAUTION level."""
        state = CircuitBreakerState(level=CircuitBreakerLevel.CAUTION)
        assert state.size_multiplier == 0.0


class TestCircuitBreaker:
    """Test CircuitBreaker functionality."""

    def test_initial_state(self, breaker):
        """Test circuit breaker starts in NORMAL state."""
        assert breaker.state.level == CircuitBreakerLevel.NORMAL
        assert breaker.can_trade
        assert breaker.size_multiplier == 1.0

    def test_check_pre_trade_valid(self, breaker):
        """Test pre-trade check passes for valid trade."""
        can_proceed, reason = breaker.check_pre_trade(
            yes_price=0.40,
            no_price=0.55,
            trade_amount=10.0,
            time_remaining_seconds=600,
        )
        assert can_proceed
        assert reason == "OK"

    def test_check_pre_trade_no_spread(self, breaker):
        """Test pre-trade check fails when prices sum >= 1."""
        can_proceed, reason = breaker.check_pre_trade(
            yes_price=0.50,
            no_price=0.50,
            trade_amount=10.0,
            time_remaining_seconds=600,
        )
        assert not can_proceed
        assert "no profit" in reason.lower()

    def test_check_pre_trade_below_spread_threshold(self, breaker):
        """Test pre-trade check fails for small spread."""
        can_proceed, reason = breaker.check_pre_trade(
            yes_price=0.49,
            no_price=0.50,  # 1 cent spread, below 2 cent threshold
            trade_amount=10.0,
            time_remaining_seconds=600,
        )
        assert not can_proceed
        assert "threshold" in reason.lower()

    def test_check_pre_trade_not_enough_time(self, breaker):
        """Test pre-trade check fails when less than 60 seconds remain."""
        can_proceed, reason = breaker.check_pre_trade(
            yes_price=0.40,
            no_price=0.55,
            trade_amount=10.0,
            time_remaining_seconds=30,
        )
        assert not can_proceed
        assert "60 seconds" in reason.lower()

    def test_check_pre_trade_exceeds_daily_exposure(self, breaker):
        """Test pre-trade check fails when would exceed daily exposure."""
        # Simulate existing exposure near limit
        breaker._daily_exposure = 95.0

        can_proceed, reason = breaker.check_pre_trade(
            yes_price=0.40,
            no_price=0.55,
            trade_amount=10.0,
            time_remaining_seconds=600,
        )
        assert not can_proceed
        assert "daily exposure" in reason.lower()

    def test_check_post_trade_success(self, breaker):
        """Test post-trade check updates exposure on success."""
        initial_exposure = breaker._daily_exposure

        breaker.check_post_trade(
            success=True,
            yes_filled=10.0,
            no_filled=10.0,
            yes_cost=4.0,
            no_cost=5.5,
        )

        assert breaker._daily_exposure == initial_exposure + 9.5

    def test_check_post_trade_failure_increments_counter(self, breaker):
        """Test post-trade check increments failure counter."""
        assert breaker._consecutive_failures == 0

        breaker.check_post_trade(
            success=False,
            yes_filled=0,
            no_filled=0,
            yes_cost=0,
            no_cost=0,
        )

        assert breaker._consecutive_failures == 1

    def test_consecutive_failures_trips_warning(self, breaker):
        """Test consecutive failures trip WARNING level."""
        for _ in range(3):
            breaker.check_post_trade(
                success=False,
                yes_filled=0,
                no_filled=0,
                yes_cost=0,
                no_cost=0,
            )

        assert breaker.state.level == CircuitBreakerLevel.WARNING

    def test_record_pnl_loss_trips_halt(self, breaker):
        """Test daily loss limit trips HALT level."""
        # Record loss exceeding limit
        breaker.record_pnl(-15.0)

        assert breaker.state.level == CircuitBreakerLevel.HALT
        assert not breaker.can_trade

    def test_reset_from_warning(self, breaker):
        """Test reset returns to NORMAL state."""
        # First trip to WARNING
        for _ in range(3):
            breaker.check_post_trade(
                success=False,
                yes_filled=0,
                no_filled=0,
                yes_cost=0,
                no_cost=0,
            )

        # Clear cooldown for test
        breaker._state.cooldown_until = None

        breaker.reset()

        assert breaker.state.level == CircuitBreakerLevel.NORMAL
        assert breaker._consecutive_failures == 0

    def test_reset_daily(self, breaker):
        """Test daily reset clears counters."""
        breaker._daily_loss = -5.0
        breaker._daily_exposure = 50.0

        breaker.reset_daily()

        assert breaker._daily_loss == 0.0
        assert breaker._daily_exposure == 0.0
