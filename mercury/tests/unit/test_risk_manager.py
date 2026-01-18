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
- Daily loss limit tracking with configurable thresholds
- Automatic daily reset scheduling
- Daily stats event publishing
"""
import pytest
from datetime import timedelta
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
            "risk.max_per_market_exposure_usd": Decimal("100.0"),
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


class TestPositionLimitEnforcement:
    """Test position limit enforcement functionality."""

    @pytest.mark.asyncio
    async def test_rejects_when_per_market_limit_exceeded(self, risk_manager):
        """Signal should be rejected when per-market exposure would exceed limit."""
        # Simulate existing exposure in the market via fills
        fill = Fill(
            order_id="order-1",
            market_id="test-market",
            side="YES",
            size=Decimal("200.0"),
            price=Decimal("0.50"),
            cost=Decimal("100.0"),
        )
        risk_manager.record_fill(fill)

        # Current market exposure is $100, limit is $100, new $10 would exceed
        signal = TradingSignal(
            signal_id="test-per-market",
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
        assert "per-market exposure" in reason.lower()

    @pytest.mark.asyncio
    async def test_allows_within_per_market_limit(self, risk_manager):
        """Signal should be allowed when within per-market exposure limit."""
        # Simulate existing exposure in the market via fills
        fill = Fill(
            order_id="order-1",
            market_id="test-market",
            side="YES",
            size=Decimal("100.0"),
            price=Decimal("0.50"),
            cost=Decimal("50.0"),  # $50 exposure
        )
        risk_manager.record_fill(fill)

        # Current market exposure is $50, limit is $100, new $10 is OK
        signal = TradingSignal(
            signal_id="test-per-market-ok",
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

    @pytest.mark.asyncio
    async def test_different_markets_have_separate_limits(self, risk_manager):
        """Each market should have its own exposure limit."""
        # Fill up market-1
        fill1 = Fill(
            order_id="order-1",
            market_id="market-1",
            side="YES",
            size=Decimal("180.0"),
            price=Decimal("0.50"),
            cost=Decimal("90.0"),  # $90 exposure in market-1
        )
        risk_manager.record_fill(fill1)

        # market-2 should still have room
        signal = TradingSignal(
            signal_id="test-market-2",
            strategy_name="gabagool",
            market_id="market-2",  # Different market
            signal_type=SignalType.ARBITRAGE,
            confidence=0.8,
            target_size_usd=Decimal("15.0"),
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.50"),
        )

        allowed, reason = await risk_manager.check_pre_trade(signal)

        assert allowed is True

    @pytest.mark.asyncio
    async def test_market_exposures_tracked_correctly(self, risk_manager):
        """Market exposures should be tracked correctly through fills."""
        assert risk_manager.market_exposures == {}

        fill1 = Fill(
            order_id="order-1",
            market_id="market-1",
            side="YES",
            size=Decimal("20.0"),
            price=Decimal("0.50"),
            cost=Decimal("10.0"),
        )
        risk_manager.record_fill(fill1)

        assert risk_manager.market_exposures["market-1"] == Decimal("10.0")

        fill2 = Fill(
            order_id="order-2",
            market_id="market-1",
            side="NO",
            size=Decimal("30.0"),
            price=Decimal("0.50"),
            cost=Decimal("15.0"),
        )
        risk_manager.record_fill(fill2)

        assert risk_manager.market_exposures["market-1"] == Decimal("25.0")

    def test_reset_clears_market_exposures(self, risk_manager):
        """Daily reset should clear per-market exposures."""
        fill = Fill(
            order_id="order-1",
            market_id="market-1",
            side="YES",
            size=Decimal("20.0"),
            price=Decimal("0.50"),
            cost=Decimal("10.0"),
        )
        risk_manager.record_fill(fill)
        assert len(risk_manager.market_exposures) > 0

        risk_manager.reset_daily()

        assert risk_manager.market_exposures == {}


class TestUnhedgedExposureLimit:
    """Test total unhedged exposure limit enforcement."""

    @pytest.mark.asyncio
    async def test_rejects_non_arbitrage_exceeding_unhedged_limit(self, risk_manager):
        """Non-arbitrage signal should be rejected when unhedged exposure exceeds limit."""
        # Set existing unhedged exposure close to limit ($50)
        risk_manager._unhedged_exposure = Decimal("45.0")

        signal = TradingSignal(
            signal_id="test-unhedged",
            strategy_name="gabagool",
            market_id="test-market",
            signal_type=SignalType.BUY_YES,  # Non-arbitrage
            confidence=0.8,
            target_size_usd=Decimal("10.0"),  # Would push to $55, over $50 limit
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.50"),
        )

        allowed, reason = await risk_manager.check_pre_trade(signal)

        assert allowed is False
        assert "unhedged exposure" in reason.lower()

    @pytest.mark.asyncio
    async def test_arbitrage_skips_unhedged_check(self, risk_manager):
        """Arbitrage signals should not be checked against unhedged exposure limit."""
        # Set existing unhedged exposure at limit
        risk_manager._unhedged_exposure = Decimal("50.0")

        signal = TradingSignal(
            signal_id="test-arbitrage-unhedged",
            strategy_name="gabagool",
            market_id="test-market",
            signal_type=SignalType.ARBITRAGE,  # Arbitrage is hedged
            confidence=0.8,
            target_size_usd=Decimal("10.0"),
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.50"),
        )

        allowed, reason = await risk_manager.check_pre_trade(signal)

        # Should pass unhedged check (fails on per-market if at limit, but not unhedged)
        # Per-market starts at 0, so this should pass
        assert allowed is True

    @pytest.mark.asyncio
    async def test_allows_non_arbitrage_within_unhedged_limit(self, risk_manager):
        """Non-arbitrage signal should be allowed when within unhedged exposure limit."""
        risk_manager._unhedged_exposure = Decimal("30.0")

        signal = TradingSignal(
            signal_id="test-unhedged-ok",
            strategy_name="gabagool",
            market_id="test-market",
            signal_type=SignalType.BUY_NO,  # Non-arbitrage
            confidence=0.8,
            target_size_usd=Decimal("15.0"),  # Would be $45, under $50 limit
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.50"),
        )

        allowed, reason = await risk_manager.check_pre_trade(signal)

        assert allowed is True


class TestStateStoreIntegration:
    """Test RiskManager integration with StateStore for position queries."""

    @pytest.fixture
    def mock_state_store(self):
        """Create a mock StateStore."""
        store = MagicMock()
        store.is_connected = True
        store.get_open_positions = AsyncMock(return_value=[])
        return store

    @pytest.fixture
    def risk_manager_with_store(self, risk_config, mock_event_bus, mock_state_store):
        """Create RiskManager with StateStore."""
        return RiskManager(
            config=risk_config,
            event_bus=mock_event_bus,
            state_store=mock_state_store,
        )

    @pytest.mark.asyncio
    async def test_queries_state_store_for_market_exposure(
        self, risk_manager_with_store, mock_state_store
    ):
        """RiskManager should query StateStore for current market exposure."""
        # Create mock position
        from mercury.services.state_store import Position as StorePosition
        mock_position = MagicMock()
        mock_position.size = Decimal("100.0")
        mock_position.entry_price = Decimal("0.50")
        mock_state_store.get_open_positions.return_value = [mock_position]

        signal = TradingSignal(
            signal_id="test-store-query",
            strategy_name="gabagool",
            market_id="test-market",
            signal_type=SignalType.ARBITRAGE,
            confidence=0.8,
            target_size_usd=Decimal("10.0"),
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.50"),
        )

        await risk_manager_with_store.check_pre_trade(signal)

        # Should have queried state store for market positions
        mock_state_store.get_open_positions.assert_called()

    @pytest.mark.asyncio
    async def test_uses_state_store_positions_for_limit_check(
        self, risk_manager_with_store, mock_state_store
    ):
        """RiskManager should use StateStore positions for per-market limit check."""
        # Create mock position with $90 exposure
        mock_position = MagicMock()
        mock_position.size = Decimal("180.0")
        mock_position.entry_price = Decimal("0.50")  # Cost = 180 * 0.5 = $90
        mock_state_store.get_open_positions.return_value = [mock_position]

        signal = TradingSignal(
            signal_id="test-store-limit",
            strategy_name="gabagool",
            market_id="test-market",
            signal_type=SignalType.ARBITRAGE,
            confidence=0.8,
            target_size_usd=Decimal("15.0"),  # Would push to $105, over $100 limit
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.50"),
        )

        allowed, reason = await risk_manager_with_store.check_pre_trade(signal)

        assert allowed is False
        assert "per-market exposure" in reason.lower()

    @pytest.mark.asyncio
    async def test_falls_back_to_memory_on_store_error(
        self, risk_manager_with_store, mock_state_store
    ):
        """RiskManager should fallback to in-memory tracking on StateStore errors."""
        mock_state_store.get_open_positions.side_effect = Exception("DB error")

        signal = TradingSignal(
            signal_id="test-fallback",
            strategy_name="gabagool",
            market_id="test-market",
            signal_type=SignalType.ARBITRAGE,
            confidence=0.8,
            target_size_usd=Decimal("10.0"),
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.50"),
        )

        # Should not raise, should fallback to in-memory (which is 0)
        allowed, reason = await risk_manager_with_store.check_pre_trade(signal)

        assert allowed is True  # In-memory starts at 0


class TestClosePositionSignals:
    """Test CLOSE_POSITION signal handling at CAUTION level."""

    @pytest.mark.asyncio
    async def test_caution_allows_close_position_signals(self, risk_manager):
        """CAUTION level should allow CLOSE_POSITION signals."""
        # Trip to CAUTION
        for _ in range(4):
            risk_manager.record_failure()

        assert risk_manager.circuit_breaker_state == CircuitBreakerState.CAUTION

        signal = TradingSignal(
            signal_id="test-close",
            strategy_name="gabagool",
            market_id="test-market",
            signal_type=SignalType.CLOSE_POSITION,
            confidence=0.8,
            target_size_usd=Decimal("10.0"),
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.50"),
        )

        allowed, reason = await risk_manager.check_pre_trade(signal)

        assert allowed is True


class TestRiskLimitsConfiguration:
    """Test risk limits are properly configured."""

    def test_per_market_limit_loaded_from_config(self, risk_manager):
        """Per-market exposure limit should be loaded from config."""
        assert risk_manager.limits.max_per_market_exposure_usd == Decimal("100.0")

    def test_limits_property_returns_configured_limits(self, risk_manager):
        """limits property should return configured RiskLimits."""
        limits = risk_manager.limits
        assert limits.max_daily_loss_usd == Decimal("100.0")
        assert limits.max_position_size_usd == Decimal("25.0")
        assert limits.max_unhedged_exposure_usd == Decimal("50.0")
        assert limits.max_per_market_exposure_usd == Decimal("100.0")


class TestDailyLossLimitTracking:
    """Test daily loss limit tracking functionality."""

    def test_peak_pnl_tracking(self, risk_manager):
        """Peak P&L should track the high water mark."""
        assert risk_manager.daily_peak_pnl == Decimal("0")

        # Record profit - should update peak
        risk_manager.record_pnl(Decimal("20.0"))
        assert risk_manager.daily_peak_pnl == Decimal("20.0")

        # Record more profit - should update peak
        risk_manager.record_pnl(Decimal("10.0"))
        assert risk_manager.daily_peak_pnl == Decimal("30.0")

        # Record loss - peak should remain at high water mark
        risk_manager.record_pnl(Decimal("-15.0"))
        assert risk_manager.daily_peak_pnl == Decimal("30.0")
        assert risk_manager.daily_pnl == Decimal("15.0")

    def test_max_drawdown_tracking(self, risk_manager):
        """Max drawdown should track largest drop from peak."""
        assert risk_manager.daily_max_drawdown == Decimal("0")

        # Go up, then down
        risk_manager.record_pnl(Decimal("50.0"))  # Peak at 50
        risk_manager.record_pnl(Decimal("-30.0"))  # Now at 20, drawdown = 30
        assert risk_manager.daily_max_drawdown == Decimal("30.0")

        # Recover partially
        risk_manager.record_pnl(Decimal("10.0"))  # Now at 30, drawdown = 20
        assert risk_manager.daily_max_drawdown == Decimal("30.0")  # Still 30 (max)

        # New higher peak
        risk_manager.record_pnl(Decimal("30.0"))  # Now at 60, peak = 60
        assert risk_manager.daily_peak_pnl == Decimal("60.0")

        # Drop further
        risk_manager.record_pnl(Decimal("-40.0"))  # Now at 20, drawdown = 40
        assert risk_manager.daily_max_drawdown == Decimal("40.0")

    def test_reset_clears_peak_and_drawdown(self, risk_manager):
        """Daily reset should clear peak P&L and max drawdown."""
        risk_manager.record_pnl(Decimal("50.0"))
        risk_manager.record_pnl(Decimal("-30.0"))
        assert risk_manager.daily_peak_pnl == Decimal("50.0")
        assert risk_manager.daily_max_drawdown == Decimal("30.0")

        risk_manager.reset_daily()

        assert risk_manager.daily_peak_pnl == Decimal("0")
        assert risk_manager.daily_max_drawdown == Decimal("0")

    def test_daily_volume_tracking(self, risk_manager):
        """Daily volume should track total trading volume."""
        assert risk_manager.daily_volume == Decimal("0")

        fill1 = Fill(
            order_id="order-1",
            market_id="test-market",
            side="YES",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
            cost=Decimal("5.0"),
        )
        risk_manager.record_fill(fill1)
        assert risk_manager.daily_volume == Decimal("5.0")

        fill2 = Fill(
            order_id="order-2",
            market_id="test-market",
            side="NO",
            size=Decimal("20.0"),
            price=Decimal("0.48"),
            cost=Decimal("9.6"),
        )
        risk_manager.record_fill(fill2)
        assert risk_manager.daily_volume == Decimal("14.6")


class TestDailyResetTimeConfiguration:
    """Test daily reset time configuration parsing."""

    def test_parse_valid_reset_time(self, risk_config, mock_event_bus):
        """Valid reset time should be parsed correctly."""
        risk_config.get.side_effect = lambda key, default=None: {
            "risk.daily_reset_time_utc": "09:30",
            "risk.daily_reset_enabled": True,
        }.get(key, default)

        risk_manager = RiskManager(config=risk_config, event_bus=mock_event_bus)

        assert risk_manager.daily_reset_time_utc.hour == 9
        assert risk_manager.daily_reset_time_utc.minute == 30

    def test_parse_midnight_reset_time(self, risk_config, mock_event_bus):
        """Midnight reset time should be parsed correctly."""
        risk_config.get.side_effect = lambda key, default=None: {
            "risk.daily_reset_time_utc": "00:00",
            "risk.daily_reset_enabled": True,
        }.get(key, default)

        risk_manager = RiskManager(config=risk_config, event_bus=mock_event_bus)

        assert risk_manager.daily_reset_time_utc.hour == 0
        assert risk_manager.daily_reset_time_utc.minute == 0

    def test_parse_invalid_reset_time_uses_default(self, risk_config, mock_event_bus):
        """Invalid reset time should default to midnight."""
        risk_config.get.side_effect = lambda key, default=None: {
            "risk.daily_reset_time_utc": "invalid",
            "risk.daily_reset_enabled": True,
        }.get(key, default)

        risk_manager = RiskManager(config=risk_config, event_bus=mock_event_bus)

        # Should default to 00:00
        assert risk_manager.daily_reset_time_utc.hour == 0
        assert risk_manager.daily_reset_time_utc.minute == 0

    def test_daily_reset_enabled_property(self, risk_config, mock_event_bus):
        """daily_reset_enabled property should reflect config."""
        risk_config.get.side_effect = lambda key, default=None: {
            "risk.daily_reset_enabled": False,
        }.get(key, default)

        risk_manager = RiskManager(config=risk_config, event_bus=mock_event_bus)

        assert risk_manager.daily_reset_enabled is False


class TestDailyResetScheduling:
    """Test daily reset scheduling functionality."""

    def test_next_reset_calculation(self, risk_manager):
        """Next reset should be calculated correctly."""
        from datetime import datetime, timezone

        next_reset = risk_manager.next_reset
        now = datetime.now(timezone.utc)

        # Next reset should be in the future or very close to now
        assert next_reset >= now - timedelta(seconds=1)
        # Should be within 24 hours
        assert next_reset <= now + timedelta(hours=24)

    def test_last_reset_property(self, risk_manager):
        """Last reset should be tracked."""
        from datetime import datetime, timezone

        # Initial last reset should be around initialization time
        assert risk_manager.last_reset is not None

        old_reset = risk_manager.last_reset
        risk_manager.reset_daily()

        # Last reset should be updated
        assert risk_manager.last_reset >= old_reset

    @pytest.mark.asyncio
    async def test_scheduler_starts_when_enabled(self, risk_config, mock_event_bus):
        """Scheduler should start when daily reset is enabled."""
        risk_config.get.side_effect = lambda key, default=None: {
            "risk.daily_reset_enabled": True,
            "risk.daily_reset_time_utc": "00:00",
        }.get(key, default)

        risk_manager = RiskManager(config=risk_config, event_bus=mock_event_bus)
        await risk_manager.start()

        # Scheduler task should be created
        assert risk_manager._reset_task is not None

        await risk_manager.stop()

    @pytest.mark.asyncio
    async def test_scheduler_does_not_start_when_disabled(self, risk_config, mock_event_bus):
        """Scheduler should not start when daily reset is disabled."""
        risk_config.get.side_effect = lambda key, default=None: {
            "risk.daily_reset_enabled": False,
            "risk.daily_reset_time_utc": "00:00",
        }.get(key, default)

        risk_manager = RiskManager(config=risk_config, event_bus=mock_event_bus)
        await risk_manager.start()

        # Scheduler task should not be created
        assert risk_manager._reset_task is None

        await risk_manager.stop()

    @pytest.mark.asyncio
    async def test_scheduler_stops_on_component_stop(self, risk_config, mock_event_bus):
        """Scheduler should be cancelled when component stops."""
        risk_config.get.side_effect = lambda key, default=None: {
            "risk.daily_reset_enabled": True,
            "risk.daily_reset_time_utc": "00:00",
        }.get(key, default)

        risk_manager = RiskManager(config=risk_config, event_bus=mock_event_bus)
        await risk_manager.start()

        assert risk_manager._reset_task is not None
        task = risk_manager._reset_task

        await risk_manager.stop()

        # Task should be cancelled
        assert risk_manager._reset_task is None
        assert task.cancelled()


class TestDailyStatsEventPublishing:
    """Test daily stats event publishing."""

    @pytest.mark.asyncio
    async def test_daily_stats_event_published_on_pnl(self, risk_manager, mock_event_bus):
        """Daily stats event should be published when P&L is recorded."""
        await risk_manager.start()

        risk_manager.record_pnl(Decimal("-25.0"))

        # Allow event loop to process
        import asyncio
        await asyncio.sleep(0.01)

        # Check if daily_stats event was published
        calls = mock_event_bus.publish.call_args_list
        daily_stats_calls = [c for c in calls if c[0][0] == "risk.daily_stats"]

        assert len(daily_stats_calls) > 0
        event_data = daily_stats_calls[0][0][1]
        assert event_data["daily_pnl"] == "-25.0"

    @pytest.mark.asyncio
    async def test_daily_stats_includes_thresholds(self, risk_manager, mock_event_bus):
        """Daily stats event should include threshold information."""
        await risk_manager.start()

        risk_manager.record_pnl(Decimal("-10.0"))

        import asyncio
        await asyncio.sleep(0.01)

        calls = mock_event_bus.publish.call_args_list
        daily_stats_calls = [c for c in calls if c[0][0] == "risk.daily_stats"]

        assert len(daily_stats_calls) > 0
        event_data = daily_stats_calls[0][0][1]

        assert "warning_threshold_usd" in event_data
        assert "caution_threshold_usd" in event_data
        assert "halt_threshold_usd" in event_data
        assert "loss_limit_pct" in event_data

    @pytest.mark.asyncio
    async def test_daily_stats_includes_drawdown(self, risk_manager, mock_event_bus):
        """Daily stats event should include peak and drawdown."""
        await risk_manager.start()

        risk_manager.record_pnl(Decimal("50.0"))
        risk_manager.record_pnl(Decimal("-30.0"))

        import asyncio
        await asyncio.sleep(0.01)

        calls = mock_event_bus.publish.call_args_list
        daily_stats_calls = [c for c in calls if c[0][0] == "risk.daily_stats"]

        # Get the most recent event
        event_data = daily_stats_calls[-1][0][1]

        assert event_data["daily_peak_pnl"] == "50.0"
        assert event_data["daily_max_drawdown"] == "30.0"


class TestLossLimitThresholds:
    """Test loss limit threshold behavior."""

    def test_warning_threshold_trips_at_50_loss(self, risk_manager):
        """WARNING should trip at $50 loss threshold."""
        risk_manager.record_pnl(Decimal("-55.0"))

        assert risk_manager.circuit_breaker_state == CircuitBreakerState.WARNING
        assert risk_manager.size_multiplier == 0.5

    def test_caution_threshold_trips_at_75_loss(self, risk_manager):
        """CAUTION should trip at $75 loss threshold."""
        risk_manager.record_pnl(Decimal("-80.0"))

        assert risk_manager.circuit_breaker_state == CircuitBreakerState.CAUTION
        assert risk_manager.can_open_positions is False

    def test_halt_threshold_trips_at_100_loss(self, risk_manager):
        """HALT should trip at $100 loss threshold."""
        risk_manager.record_pnl(Decimal("-105.0"))

        assert risk_manager.circuit_breaker_state == CircuitBreakerState.HALT

    def test_incremental_losses_accumulate(self, risk_manager):
        """Incremental losses should accumulate toward thresholds."""
        # Record multiple smaller losses
        risk_manager.record_pnl(Decimal("-20.0"))
        assert risk_manager.circuit_breaker_state == CircuitBreakerState.NORMAL

        risk_manager.record_pnl(Decimal("-20.0"))
        assert risk_manager.circuit_breaker_state == CircuitBreakerState.NORMAL

        risk_manager.record_pnl(Decimal("-15.0"))
        # Now at -55, should be WARNING
        assert risk_manager.circuit_breaker_state == CircuitBreakerState.WARNING

        risk_manager.record_pnl(Decimal("-25.0"))
        # Now at -80, should be CAUTION
        assert risk_manager.circuit_breaker_state == CircuitBreakerState.CAUTION

    def test_profits_offset_losses(self, risk_manager):
        """Profits should offset losses in daily tracking."""
        risk_manager.record_pnl(Decimal("-60.0"))
        assert risk_manager.circuit_breaker_state == CircuitBreakerState.WARNING

        # Profit brings us back under threshold
        risk_manager.record_pnl(Decimal("15.0"))
        # Still at WARNING since we don't downgrade via trip
        # (recovery happens on reset)
        assert risk_manager.daily_pnl == Decimal("-45.0")
