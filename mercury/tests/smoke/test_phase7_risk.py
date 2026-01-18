"""
Phase 7 Smoke Test: Risk Manager

Verifies that Phase 7 deliverables work:
- RiskManager validates signals
- Circuit breaker works
- Position limits enforced
- Daily loss tracking works
- Signal validation flow works
- Exposure tracking from fills works

Run: pytest tests/smoke/test_phase7_risk.py -v
"""
import pytest
from decimal import Decimal
from unittest.mock import MagicMock, AsyncMock


class TestPhase7RiskManager:
    """Phase 7 must pass ALL these tests to be considered complete."""

    def test_risk_manager_importable(self):
        """Verify RiskManager can be imported."""
        from mercury.services.risk_manager import RiskManager
        assert RiskManager is not None

    @pytest.mark.asyncio
    async def test_risk_manager_starts_stops(self, mock_config, mock_event_bus):
        """Verify RiskManager lifecycle works."""
        from mercury.services.risk_manager import RiskManager

        mock_config.get.side_effect = lambda k, d=None: {
            "risk.max_daily_loss_usd": Decimal("100.0"),
            "risk.max_unhedged_exposure_usd": Decimal("50.0"),
            "risk.max_position_size_usd": Decimal("25.0"),
        }.get(k, d)

        manager = RiskManager(config=mock_config, event_bus=mock_event_bus)

        await manager.start()
        assert manager.is_running

        await manager.stop()
        assert not manager.is_running

    @pytest.mark.asyncio
    async def test_pre_trade_validation_approves_valid(self, mock_config, mock_event_bus):
        """Verify valid signals are approved."""
        from mercury.services.risk_manager import RiskManager
        from mercury.domain.signal import TradingSignal, SignalType

        mock_config.get.side_effect = lambda k, d=None: {
            "risk.max_daily_loss_usd": Decimal("100.0"),
            "risk.max_unhedged_exposure_usd": Decimal("50.0"),
            "risk.max_position_size_usd": Decimal("25.0"),
        }.get(k, d)

        manager = RiskManager(config=mock_config, event_bus=mock_event_bus)

        signal = TradingSignal(
            signal_id="test-signal",
            strategy_name="gabagool",
            market_id="test-market",
            signal_type=SignalType.ARBITRAGE,
            confidence=0.8,
            target_size_usd=Decimal("10.0"),
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.50"),
        )

        allowed, reason = await manager.check_pre_trade(signal)

        assert allowed is True
        assert reason == "" or reason is None

    @pytest.mark.asyncio
    async def test_pre_trade_rejects_oversized(self, mock_config, mock_event_bus):
        """Verify oversized signals are rejected."""
        from mercury.services.risk_manager import RiskManager
        from mercury.domain.signal import TradingSignal, SignalType

        mock_config.get.side_effect = lambda k, d=None: {
            "risk.max_daily_loss_usd": Decimal("100.0"),
            "risk.max_unhedged_exposure_usd": Decimal("50.0"),
            "risk.max_position_size_usd": Decimal("25.0"),
        }.get(k, d)

        manager = RiskManager(config=mock_config, event_bus=mock_event_bus)

        signal = TradingSignal(
            signal_id="test-signal",
            strategy_name="gabagool",
            market_id="test-market",
            signal_type=SignalType.ARBITRAGE,
            confidence=0.8,
            target_size_usd=Decimal("100.0"),  # Exceeds max
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.50"),
        )

        allowed, reason = await manager.check_pre_trade(signal)

        assert allowed is False
        assert "size" in reason.lower() or "limit" in reason.lower()

    def test_circuit_breaker_states(self):
        """Verify circuit breaker has correct states."""
        from mercury.domain.risk import CircuitBreakerState

        assert CircuitBreakerState.NORMAL is not None
        assert CircuitBreakerState.WARNING is not None
        assert CircuitBreakerState.CAUTION is not None
        assert CircuitBreakerState.HALT is not None

    @pytest.mark.asyncio
    async def test_circuit_breaker_trips_on_failures(self, mock_config, mock_event_bus):
        """Verify circuit breaker trips after consecutive failures."""
        from mercury.services.risk_manager import RiskManager
        from mercury.domain.risk import CircuitBreakerState

        mock_config.get.side_effect = lambda k, d=None: {
            "risk.max_daily_loss_usd": Decimal("100.0"),
            "risk.max_unhedged_exposure_usd": Decimal("50.0"),
            "risk.max_position_size_usd": Decimal("25.0"),
            "risk.circuit_breaker_warning_failures": 3,
            "risk.circuit_breaker_halt_failures": 5,
        }.get(k, d)

        manager = RiskManager(config=mock_config, event_bus=mock_event_bus)

        assert manager.circuit_breaker_state == CircuitBreakerState.NORMAL

        # Record failures
        for _ in range(3):
            manager.record_failure()

        assert manager.circuit_breaker_state == CircuitBreakerState.WARNING

        for _ in range(2):
            manager.record_failure()

        assert manager.circuit_breaker_state == CircuitBreakerState.HALT

    @pytest.mark.asyncio
    async def test_circuit_breaker_trips_on_daily_loss(self, mock_config, mock_event_bus):
        """Verify circuit breaker trips on daily loss limit."""
        from mercury.services.risk_manager import RiskManager
        from mercury.domain.risk import CircuitBreakerState

        mock_config.get.side_effect = lambda k, d=None: {
            "risk.max_daily_loss_usd": Decimal("100.0"),
            "risk.max_unhedged_exposure_usd": Decimal("50.0"),
            "risk.max_position_size_usd": Decimal("25.0"),
            "risk.circuit_breaker_warning_loss": Decimal("50.0"),
            "risk.circuit_breaker_halt_loss": Decimal("100.0"),
        }.get(k, d)

        manager = RiskManager(config=mock_config, event_bus=mock_event_bus)

        manager.record_pnl(Decimal("-60.0"))
        assert manager.circuit_breaker_state == CircuitBreakerState.WARNING

        manager.record_pnl(Decimal("-50.0"))  # Total: -110
        assert manager.circuit_breaker_state == CircuitBreakerState.HALT

    @pytest.mark.asyncio
    async def test_signal_validation_flow(self, mock_config, mock_event_bus):
        """Verify signal validation publishes approved/rejected events."""
        from mercury.services.risk_manager import RiskManager
        from mercury.domain.signal import TradingSignal, SignalType

        mock_config.get.side_effect = lambda k, d=None: {
            "risk.max_daily_loss_usd": Decimal("100.0"),
            "risk.max_unhedged_exposure_usd": Decimal("50.0"),
            "risk.max_position_size_usd": Decimal("25.0"),
        }.get(k, d)

        manager = RiskManager(config=mock_config, event_bus=mock_event_bus)
        await manager.start()

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

        await manager.validate_signal(signal)

        # Check approved event was published
        calls = mock_event_bus.publish.call_args_list
        channels = [call[0][0] for call in calls]

        assert any("risk.approved" in c for c in channels)

        await manager.stop()

    @pytest.mark.asyncio
    async def test_exposure_tracking_from_fills(self, mock_config, mock_event_bus):
        """Verify exposure is tracked from fill events."""
        from mercury.services.risk_manager import RiskManager
        from mercury.domain.order import Fill

        mock_config.get.side_effect = lambda k, d=None: {
            "risk.max_daily_loss_usd": Decimal("100.0"),
            "risk.max_unhedged_exposure_usd": Decimal("50.0"),
            "risk.max_position_size_usd": Decimal("25.0"),
        }.get(k, d)

        manager = RiskManager(config=mock_config, event_bus=mock_event_bus)

        assert manager.current_exposure == Decimal("0")

        fill = Fill(
            order_id="test-order",
            market_id="test-market",
            side="YES",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
            cost=Decimal("5.0"),
        )

        manager.record_fill(fill)

        assert manager.current_exposure == Decimal("5.0")

    @pytest.mark.asyncio
    async def test_daily_reset(self, mock_config, mock_event_bus):
        """Verify daily counters reset."""
        from mercury.services.risk_manager import RiskManager
        from mercury.domain.risk import CircuitBreakerState

        mock_config.get.side_effect = lambda k, d=None: {
            "risk.max_daily_loss_usd": Decimal("100.0"),
            "risk.max_unhedged_exposure_usd": Decimal("50.0"),
            "risk.max_position_size_usd": Decimal("25.0"),
        }.get(k, d)

        manager = RiskManager(config=mock_config, event_bus=mock_event_bus)

        manager.record_pnl(Decimal("-50.0"))
        manager.record_failure()
        manager.record_failure()
        manager.record_failure()

        assert manager._daily_pnl < 0
        assert manager._consecutive_failures > 0

        manager.reset_daily()

        assert manager._daily_pnl == Decimal("0")
        assert manager._consecutive_failures == 0
        assert manager.circuit_breaker_state == CircuitBreakerState.NORMAL
