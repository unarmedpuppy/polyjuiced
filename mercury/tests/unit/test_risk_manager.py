"""
Unit tests for the RiskManager service.

Tests cover:
- Pre-trade validation
- 4-level circuit breaker state transitions (NORMAL -> WARNING -> CAUTION -> HALT)
- Exposure tracking
- Daily reset functionality
- Event handling
- Circuit breaker event publishing
- Position size multipliers
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
    """Create mock config for risk manager with 4-level circuit breaker thresholds."""
    config = MagicMock()

    def get_side_effect(key, default=None):
        values = {
            "risk.max_daily_loss_usd": Decimal("100.0"),
            "risk.max_unhedged_exposure_usd": Decimal("50.0"),
            "risk.max_position_size_usd": Decimal("25.0"),
            # 4-level circuit breaker: NORMAL -> WARNING -> CAUTION -> HALT
            "risk.circuit_breaker_warning_failures": 3,
            "risk.circuit_breaker_caution_failures": 4,
            "risk.circuit_breaker_halt_failures": 5,
            "risk.circuit_breaker_warning_loss": Decimal("50.0"),
            "risk.circuit_breaker_caution_loss": Decimal("75.0"),
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
        from datetime import datetime, timezone, timedelta

        # Simulate circuit breaker trip to HALT with active cooldown
        risk_manager._circuit_breaker_state = CircuitBreakerState.HALT
        risk_manager._circuit_breaker_triggered_at = datetime.now(timezone.utc)
        risk_manager._cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=5)
        risk_manager._circuit_breaker_reasons = ["Test halt reason"]

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
        assert "circuit breaker" in reason.lower() or "halt" in reason.lower()


class TestCircuitBreaker:
    """Test 4-level circuit breaker state transitions."""

    def test_failure_tracking(self, risk_manager):
        """Test consecutive failure counting."""
        assert risk_manager.consecutive_failures == 0

        risk_manager.record_failure()
        assert risk_manager.consecutive_failures == 1

        risk_manager.record_failure()
        assert risk_manager.consecutive_failures == 2

    def test_warning_after_threshold_failures(self, risk_manager):
        """Circuit breaker should go to WARNING after 3 failures."""
        for _ in range(3):  # Warning threshold
            risk_manager.record_failure()

        assert risk_manager.circuit_breaker_state == CircuitBreakerState.WARNING
        assert risk_manager.size_multiplier == 0.5

    def test_caution_after_threshold_failures(self, risk_manager):
        """Circuit breaker should go to CAUTION after 4 failures."""
        for _ in range(4):  # Caution threshold
            risk_manager.record_failure()

        assert risk_manager.circuit_breaker_state == CircuitBreakerState.CAUTION
        assert risk_manager.size_multiplier == 0.0
        assert not risk_manager.can_open_positions

    def test_halt_after_threshold_failures(self, risk_manager):
        """Circuit breaker should go to HALT after 5 failures."""
        for _ in range(5):  # Halt threshold
            risk_manager.record_failure()

        assert risk_manager.circuit_breaker_state == CircuitBreakerState.HALT
        assert not risk_manager.can_trade

    def test_success_resets_failures(self, risk_manager):
        """Successful trade should reset failure count."""
        risk_manager.record_failure()
        risk_manager.record_failure()
        assert risk_manager.consecutive_failures == 2

        risk_manager.record_success()
        assert risk_manager.consecutive_failures == 0

    def test_warning_on_loss_threshold(self, risk_manager):
        """Circuit breaker should go to WARNING when loss exceeds $50 threshold."""
        risk_manager.record_pnl(Decimal("-60.0"))  # Exceeds 50.0 warning threshold

        assert risk_manager.circuit_breaker_state == CircuitBreakerState.WARNING
        assert risk_manager.size_multiplier == 0.5

    def test_caution_on_loss_threshold(self, risk_manager):
        """Circuit breaker should go to CAUTION when loss exceeds $75 threshold."""
        risk_manager.record_pnl(Decimal("-80.0"))  # Exceeds 75.0 caution threshold

        assert risk_manager.circuit_breaker_state == CircuitBreakerState.CAUTION
        assert risk_manager.size_multiplier == 0.0

    def test_halt_on_loss_threshold(self, risk_manager):
        """Circuit breaker should go to HALT when loss exceeds $100 threshold."""
        risk_manager.record_pnl(Decimal("-110.0"))  # Exceeds 100.0 halt threshold

        assert risk_manager.circuit_breaker_state == CircuitBreakerState.HALT

    def test_size_multiplier_property(self, risk_manager):
        """Test size_multiplier reflects circuit breaker state."""
        assert risk_manager.size_multiplier == 1.0  # NORMAL

        for _ in range(3):
            risk_manager.record_failure()
        assert risk_manager.size_multiplier == 0.5  # WARNING

        risk_manager.record_failure()  # 4th failure
        assert risk_manager.size_multiplier == 0.0  # CAUTION

    def test_reasons_tracked_on_trip(self, risk_manager):
        """Test that reasons are tracked when circuit breaker trips."""
        for _ in range(3):
            risk_manager.record_failure()

        reasons = risk_manager.circuit_breaker_reasons
        assert len(reasons) > 0
        assert any("failure" in r.lower() for r in reasons)

    def test_combined_failure_and_loss(self, risk_manager):
        """Test that the more severe condition determines state."""
        # 3 failures = WARNING
        for _ in range(3):
            risk_manager.record_failure()
        assert risk_manager.circuit_breaker_state == CircuitBreakerState.WARNING

        # Adding loss that triggers CAUTION should upgrade to CAUTION
        risk_manager.record_pnl(Decimal("-80.0"))  # CAUTION loss threshold
        assert risk_manager.circuit_breaker_state == CircuitBreakerState.CAUTION


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


class TestCautionLevel:
    """Test CAUTION level specific behavior."""

    @pytest.mark.asyncio
    async def test_caution_rejects_new_positions(self, risk_manager):
        """CAUTION level should reject all new position signals."""
        # Trip to CAUTION
        for _ in range(4):
            risk_manager.record_failure()

        assert risk_manager.circuit_breaker_state == CircuitBreakerState.CAUTION

        signal = TradingSignal(
            signal_id="test-caution",
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
        assert "caution" in reason.lower()
        assert "only position closes allowed" in reason.lower()

    def test_caution_state_properties(self, risk_manager):
        """Test CAUTION state has correct properties."""
        for _ in range(4):
            risk_manager.record_failure()

        assert risk_manager.circuit_breaker_state == CircuitBreakerState.CAUTION
        assert risk_manager.size_multiplier == 0.0
        assert risk_manager.can_trade is True  # Can close positions
        assert risk_manager.can_open_positions is False


class TestCircuitBreakerEvents:
    """Test circuit breaker event publishing."""

    @pytest.mark.asyncio
    async def test_event_published_on_state_change(self, risk_manager, mock_event_bus):
        """Event should be published when circuit breaker state changes."""
        await risk_manager.start()

        # Trip to WARNING
        for _ in range(3):
            risk_manager.record_failure()

        # Allow event loop to process
        import asyncio
        await asyncio.sleep(0.01)

        # Check event was published
        calls = mock_event_bus.publish.call_args_list
        circuit_breaker_calls = [c for c in calls if c[0][0] == "risk.circuit_breaker"]

        assert len(circuit_breaker_calls) > 0
        event_data = circuit_breaker_calls[0][0][1]
        assert event_data["old_state"] == "NORMAL"
        assert event_data["new_state"] == "WARNING"
        assert "size_multiplier" in event_data
        assert event_data["size_multiplier"] == 0.5

    @pytest.mark.asyncio
    async def test_event_includes_reasons(self, risk_manager, mock_event_bus):
        """Circuit breaker event should include reasons for state change."""
        await risk_manager.start()

        # Trip via loss
        risk_manager.record_pnl(Decimal("-80.0"))  # CAUTION threshold

        import asyncio
        await asyncio.sleep(0.01)

        calls = mock_event_bus.publish.call_args_list
        circuit_breaker_calls = [c for c in calls if c[0][0] == "risk.circuit_breaker"]

        assert len(circuit_breaker_calls) > 0
        event_data = circuit_breaker_calls[0][0][1]
        assert "reasons" in event_data
        assert len(event_data["reasons"]) > 0

    @pytest.mark.asyncio
    async def test_event_includes_state_properties(self, risk_manager, mock_event_bus):
        """Circuit breaker event should include trading capability flags."""
        await risk_manager.start()

        # Trip to HALT
        for _ in range(5):
            risk_manager.record_failure()

        import asyncio
        await asyncio.sleep(0.01)

        calls = mock_event_bus.publish.call_args_list
        circuit_breaker_calls = [c for c in calls if c[0][0] == "risk.circuit_breaker"]

        # Find the HALT event (may have multiple events as we progress through levels)
        halt_events = [c for c in circuit_breaker_calls if c[0][1].get("new_state") == "HALT"]
        assert len(halt_events) > 0

        event_data = halt_events[0][0][1]
        assert event_data["can_trade"] is False
        assert event_data["can_open_positions"] is False
        assert event_data["size_multiplier"] == 0.0


class TestWarningLevelPositionSizing:
    """Test WARNING level position size reduction."""

    @pytest.mark.asyncio
    async def test_warning_reduces_position_limit(self, risk_manager):
        """WARNING level should reduce position size limit by 50%."""
        # Trip to WARNING
        for _ in range(3):
            risk_manager.record_failure()

        assert risk_manager.circuit_breaker_state == CircuitBreakerState.WARNING

        # Normal limit is $25, WARNING reduces to $12.50
        # A $15 signal should be rejected at WARNING level
        signal = TradingSignal(
            signal_id="test-warning-size",
            strategy_name="gabagool",
            market_id="test-market",
            signal_type=SignalType.ARBITRAGE,
            confidence=0.8,
            target_size_usd=Decimal("15.0"),  # Exceeds 50% of limit ($12.50)
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.50"),
        )

        allowed, reason = await risk_manager.check_pre_trade(signal)

        assert allowed is False
        assert "warning" in reason.lower() or "limit" in reason.lower()

    @pytest.mark.asyncio
    async def test_warning_allows_reduced_positions(self, risk_manager):
        """WARNING level should allow positions within reduced limit."""
        for _ in range(3):
            risk_manager.record_failure()

        # $10 is within the $12.50 WARNING limit
        signal = TradingSignal(
            signal_id="test-warning-ok",
            strategy_name="gabagool",
            market_id="test-market",
            signal_type=SignalType.ARBITRAGE,
            confidence=0.8,
            target_size_usd=Decimal("10.0"),  # Within 50% limit
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.50"),
        )

        allowed, reason = await risk_manager.check_pre_trade(signal)

        assert allowed is True
