"""
Unit tests for the RiskManager service.

Tests cover:
- Pre-trade validation
- Circuit breaker state transitions
- Exposure tracking
- Daily reset functionality
- Event handling
"""
import pytest
from decimal import Decimal
from unittest.mock import MagicMock, AsyncMock

from mercury.services.risk_manager import RiskManager
from mercury.domain.signal import TradingSignal, SignalType
from mercury.domain.order import Fill
from mercury.domain.risk import CircuitBreakerState


@pytest.fixture
def risk_config():
    """Create mock config for risk manager."""
    config = MagicMock()

    def get_side_effect(key, default=None):
        values = {
            "risk.max_daily_loss_usd": Decimal("100.0"),
            "risk.max_unhedged_exposure_usd": Decimal("50.0"),
            "risk.max_position_size_usd": Decimal("25.0"),
            "risk.circuit_breaker_warning_failures": 3,
            "risk.circuit_breaker_halt_failures": 5,
            "risk.circuit_breaker_warning_loss": Decimal("50.0"),
            "risk.circuit_breaker_halt_loss": Decimal("100.0"),
            "risk.circuit_breaker_cooldown_minutes": 5,
        }
        return values.get(key, default)

    config.get.side_effect = get_side_effect
    return config


@pytest.fixture
def risk_manager(risk_config, mock_event_bus):
    """Create RiskManager instance with mocked dependencies."""
    return RiskManager(config=risk_config, event_bus=mock_event_bus)


class TestRiskManagerInitialization:
    """Test RiskManager initialization."""

    def test_initializes_with_config_values(self, risk_manager):
        """Verify risk limits loaded from config."""
        assert risk_manager._limits.max_daily_loss_usd == Decimal("100.0")
        assert risk_manager._limits.max_position_size_usd == Decimal("25.0")
        assert risk_manager._limits.max_unhedged_exposure_usd == Decimal("50.0")

    def test_initial_state_is_normal(self, risk_manager):
        """Verify initial circuit breaker state is NORMAL."""
        assert risk_manager.circuit_breaker_state == CircuitBreakerState.NORMAL
        assert risk_manager._daily_pnl == Decimal("0")
        assert risk_manager._consecutive_failures == 0
        assert risk_manager.current_exposure == Decimal("0")


class TestPreTradeValidation:
    """Test check_pre_trade validation logic."""

    @pytest.mark.asyncio
    async def test_approves_valid_signal(self, risk_manager):
        """Valid signal should be approved."""
        signal = TradingSignal(
            signal_id="test-1",
            strategy_name="gabagool",
            market_id="test-market",
            signal_type=SignalType.ARBITRAGE,
            confidence=0.8,
            target_size_usd=Decimal("10.0"),
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.50"),
        )

        allowed, reason = await risk_manager.check_pre_trade(signal)

        assert allowed is True
        assert reason is None

    @pytest.mark.asyncio
    async def test_rejects_oversized_position(self, risk_manager):
        """Signal exceeding position size limit should be rejected."""
        signal = TradingSignal(
            signal_id="test-2",
            strategy_name="gabagool",
            market_id="test-market",
            signal_type=SignalType.ARBITRAGE,
            confidence=0.8,
            target_size_usd=Decimal("50.0"),  # Exceeds 25.0 limit
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.50"),
        )

        allowed, reason = await risk_manager.check_pre_trade(signal)

        assert allowed is False
        assert "size" in reason.lower() or "limit" in reason.lower()

    @pytest.mark.asyncio
    async def test_rejects_when_daily_loss_reached(self, risk_manager):
        """Signal should be rejected when daily loss limit reached."""
        risk_manager._daily_pnl = Decimal("-100.0")  # At limit

        signal = TradingSignal(
            signal_id="test-3",
            strategy_name="gabagool",
            market_id="test-market",
            signal_type=SignalType.ARBITRAGE,
            confidence=0.8,
            target_size_usd=Decimal("10.0"),
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.50"),
        )

        allowed, reason = await risk_manager.check_pre_trade(signal)

        assert allowed is False
        assert "daily loss" in reason.lower()

    @pytest.mark.asyncio
    async def test_rejects_when_circuit_breaker_halt(self, risk_manager):
        """Signal should be rejected when circuit breaker is HALT."""
        risk_manager._circuit_breaker_state = CircuitBreakerState.HALT
        from datetime import datetime, timezone

        risk_manager._circuit_breaker_triggered_at = datetime.now(timezone.utc)

        signal = TradingSignal(
            signal_id="test-4",
            strategy_name="gabagool",
            market_id="test-market",
            signal_type=SignalType.ARBITRAGE,
            confidence=0.8,
            target_size_usd=Decimal("10.0"),
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.50"),
        )

        allowed, reason = await risk_manager.check_pre_trade(signal)

        assert allowed is False
        assert "circuit breaker" in reason.lower()


class TestCircuitBreaker:
    """Test circuit breaker state transitions."""

    def test_failure_tracking(self, risk_manager):
        """Test consecutive failure counting."""
        assert risk_manager._consecutive_failures == 0

        risk_manager.record_failure()
        assert risk_manager._consecutive_failures == 1

        risk_manager.record_failure()
        assert risk_manager._consecutive_failures == 2

    def test_warning_after_threshold_failures(self, risk_manager):
        """Circuit breaker should go to WARNING after threshold failures."""
        for _ in range(3):  # Warning threshold
            risk_manager.record_failure()

        assert risk_manager.circuit_breaker_state == CircuitBreakerState.WARNING

    def test_halt_after_threshold_failures(self, risk_manager):
        """Circuit breaker should go to HALT after threshold failures."""
        for _ in range(5):  # Halt threshold
            risk_manager.record_failure()

        assert risk_manager.circuit_breaker_state == CircuitBreakerState.HALT

    def test_success_resets_failures(self, risk_manager):
        """Successful trade should reset failure count."""
        risk_manager.record_failure()
        risk_manager.record_failure()
        assert risk_manager._consecutive_failures == 2

        risk_manager.record_success()
        assert risk_manager._consecutive_failures == 0

    def test_warning_on_loss_threshold(self, risk_manager):
        """Circuit breaker should go to WARNING when loss exceeds warning threshold."""
        risk_manager.record_pnl(Decimal("-60.0"))  # Exceeds 50.0 warning threshold

        assert risk_manager.circuit_breaker_state == CircuitBreakerState.WARNING

    def test_halt_on_loss_threshold(self, risk_manager):
        """Circuit breaker should go to HALT when loss exceeds halt threshold."""
        risk_manager.record_pnl(Decimal("-110.0"))  # Exceeds 100.0 halt threshold

        assert risk_manager.circuit_breaker_state == CircuitBreakerState.HALT


class TestExposureTracking:
    """Test exposure and fill tracking."""

    def test_fill_updates_exposure(self, risk_manager):
        """Fill should update current exposure."""
        assert risk_manager.current_exposure == Decimal("0")

        fill = Fill(
            order_id="order-1",
            market_id="test-market",
            side="YES",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
            cost=Decimal("5.0"),
        )

        risk_manager.record_fill(fill)

        assert risk_manager.current_exposure == Decimal("5.0")

    def test_multiple_fills_accumulate(self, risk_manager):
        """Multiple fills should accumulate exposure."""
        fill1 = Fill(
            order_id="order-1",
            market_id="test-market",
            side="YES",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
            cost=Decimal("5.0"),
        )

        fill2 = Fill(
            order_id="order-2",
            market_id="test-market",
            side="NO",
            size=Decimal("10.0"),
            price=Decimal("0.48"),
            cost=Decimal("4.8"),
        )

        risk_manager.record_fill(fill1)
        risk_manager.record_fill(fill2)

        assert risk_manager.current_exposure == Decimal("9.8")

    def test_fill_increments_trade_count(self, risk_manager):
        """Each fill should increment daily trade count."""
        assert risk_manager._daily_trades == 0

        fill = Fill(
            order_id="order-1",
            market_id="test-market",
            side="YES",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
            cost=Decimal("5.0"),
        )

        risk_manager.record_fill(fill)
        assert risk_manager._daily_trades == 1

        risk_manager.record_fill(fill)
        assert risk_manager._daily_trades == 2


class TestPnLTracking:
    """Test P&L tracking."""

    def test_pnl_recording(self, risk_manager):
        """P&L should be tracked."""
        assert risk_manager._daily_pnl == Decimal("0")

        risk_manager.record_pnl(Decimal("10.0"))
        assert risk_manager._daily_pnl == Decimal("10.0")

        risk_manager.record_pnl(Decimal("-5.0"))
        assert risk_manager._daily_pnl == Decimal("5.0")

    def test_pnl_property(self, risk_manager):
        """daily_pnl property should return correct value."""
        risk_manager.record_pnl(Decimal("-25.0"))
        assert risk_manager.daily_pnl == Decimal("-25.0")


class TestDailyReset:
    """Test daily reset functionality."""

    def test_reset_clears_pnl(self, risk_manager):
        """Daily reset should clear P&L."""
        risk_manager.record_pnl(Decimal("-50.0"))
        risk_manager.reset_daily()

        assert risk_manager._daily_pnl == Decimal("0")

    def test_reset_clears_failures(self, risk_manager):
        """Daily reset should clear failure count."""
        for _ in range(3):
            risk_manager.record_failure()
        risk_manager.reset_daily()

        assert risk_manager._consecutive_failures == 0

    def test_reset_clears_exposure(self, risk_manager):
        """Daily reset should clear exposure."""
        fill = Fill(
            order_id="order-1",
            market_id="test-market",
            side="YES",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
            cost=Decimal("5.0"),
        )
        risk_manager.record_fill(fill)
        risk_manager.reset_daily()

        assert risk_manager.current_exposure == Decimal("0")

    def test_reset_resets_circuit_breaker(self, risk_manager):
        """Daily reset should reset circuit breaker to NORMAL."""
        for _ in range(5):
            risk_manager.record_failure()
        assert risk_manager.circuit_breaker_state == CircuitBreakerState.HALT

        risk_manager.reset_daily()
        assert risk_manager.circuit_breaker_state == CircuitBreakerState.NORMAL

    def test_reset_clears_trade_count(self, risk_manager):
        """Daily reset should clear trade count."""
        fill = Fill(
            order_id="order-1",
            market_id="test-market",
            side="YES",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
            cost=Decimal("5.0"),
        )
        risk_manager.record_fill(fill)
        risk_manager.reset_daily()

        assert risk_manager._daily_trades == 0


class TestSignalValidation:
    """Test signal validation and event publishing."""

    @pytest.mark.asyncio
    async def test_validate_signal_publishes_approved(self, risk_manager, mock_event_bus):
        """Valid signal validation should publish approved event."""
        signal = TradingSignal(
            signal_id="test-signal-1",
            strategy_name="gabagool",
            market_id="test-market",
            signal_type=SignalType.ARBITRAGE,
            confidence=0.8,
            target_size_usd=Decimal("10.0"),
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.50"),
        )

        result = await risk_manager.validate_signal(signal)

        assert result is not None
        assert mock_event_bus.publish.called
        calls = mock_event_bus.publish.call_args_list
        channels = [call[0][0] for call in calls]
        assert any("risk.approved" in c for c in channels)

    @pytest.mark.asyncio
    async def test_validate_signal_publishes_rejected(self, risk_manager, mock_event_bus):
        """Invalid signal validation should publish rejected event."""
        signal = TradingSignal(
            signal_id="test-signal-2",
            strategy_name="gabagool",
            market_id="test-market",
            signal_type=SignalType.ARBITRAGE,
            confidence=0.8,
            target_size_usd=Decimal("100.0"),  # Exceeds limit
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.50"),
        )

        result = await risk_manager.validate_signal(signal)

        assert result is None
        calls = mock_event_bus.publish.call_args_list
        channels = [call[0][0] for call in calls]
        assert any("risk.rejected" in c for c in channels)


class TestLifecycle:
    """Test component lifecycle."""

    @pytest.mark.asyncio
    async def test_start_subscribes_to_events(self, risk_manager, mock_event_bus):
        """Start should subscribe to signal and order events."""
        await risk_manager.start()

        assert risk_manager.is_running
        calls = mock_event_bus.subscribe.call_args_list
        patterns = [call[0][0] for call in calls]
        assert "signal.*" in patterns
        assert "order.filled" in patterns
        assert "position.closed" in patterns

    @pytest.mark.asyncio
    async def test_stop_sets_not_running(self, risk_manager, mock_event_bus):
        """Stop should set is_running to False."""
        await risk_manager.start()
        await risk_manager.stop()

        assert not risk_manager.is_running

    @pytest.mark.asyncio
    async def test_health_check_healthy_normal(self, risk_manager):
        """Health check should return healthy when NORMAL."""
        await risk_manager.start()

        result = await risk_manager.health_check()

        from mercury.core.lifecycle import HealthStatus

        assert result.status == HealthStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_health_check_degraded_when_halt(self, risk_manager):
        """Health check should return degraded when circuit breaker HALT."""
        await risk_manager.start()

        for _ in range(5):
            risk_manager.record_failure()

        result = await risk_manager.health_check()

        from mercury.core.lifecycle import HealthStatus

        assert result.status == HealthStatus.DEGRADED
