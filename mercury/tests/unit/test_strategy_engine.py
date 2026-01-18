"""
Unit tests for StrategyEngine.

Tests verify:
- Lifecycle methods (start/stop)
- Strategy registration and unregistration
- Runtime enable/disable of strategies
- Market data routing to strategies
- Signal publishing to EventBus
- Health check functionality
"""
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import List
from unittest.mock import MagicMock, AsyncMock
import pytest

from mercury.services.strategy_engine import StrategyEngine
from mercury.domain.market import OrderBook, OrderBookLevel
from mercury.domain.signal import TradingSignal, SignalType, SignalPriority
from mercury.core.lifecycle import HealthStatus


class MockStrategy:
    """A mock strategy implementation for testing."""

    def __init__(self, name: str = "mock_strategy"):
        self._name = name
        self._enabled = True
        self._subscribed_markets: List[str] = []
        self._started = False
        self._stopped = False
        self._signals_to_yield: List[TradingSignal] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._stopped = True

    async def on_market_data(
        self,
        market_id: str,
        book: OrderBook,
    ) -> AsyncIterator[TradingSignal]:
        """Yield configured signals or default arbitrage signal."""
        if self._signals_to_yield:
            for signal in self._signals_to_yield:
                yield signal
        elif book.has_arbitrage_opportunity:
            yield TradingSignal(
                strategy_name=self._name,
                market_id=market_id,
                signal_type=SignalType.ARBITRAGE,
                confidence=0.9,
                priority=SignalPriority.HIGH,
                target_size_usd=Decimal("25.00"),
                yes_price=book.yes_best_ask or Decimal("0"),
                no_price=book.no_best_ask or Decimal("0"),
                expected_pnl=Decimal("1") - (book.combined_ask or Decimal("1")),
            )

    def get_subscribed_markets(self) -> list[str]:
        return self._subscribed_markets

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    def subscribe_to_market(self, market_id: str) -> None:
        """Helper method for tests to add market subscriptions."""
        if market_id not in self._subscribed_markets:
            self._subscribed_markets.append(market_id)

    def set_signals_to_yield(self, signals: List[TradingSignal]) -> None:
        """Set specific signals to yield during on_market_data."""
        self._signals_to_yield = signals


@pytest.fixture
def mock_config():
    """Create mock ConfigManager."""
    config = MagicMock()
    config.get.return_value = None
    return config


@pytest.fixture
def mock_event_bus():
    """Create mock EventBus."""
    bus = MagicMock()
    bus.publish = AsyncMock()
    bus.subscribe = AsyncMock()
    bus.unsubscribe = AsyncMock()
    return bus


@pytest.fixture
def strategy_engine(mock_config, mock_event_bus):
    """Create a StrategyEngine instance for testing."""
    return StrategyEngine(config=mock_config, event_bus=mock_event_bus)


@pytest.fixture
def mock_strategy():
    """Create a mock strategy instance."""
    return MockStrategy("test_strategy")


@pytest.fixture
def order_book_with_arbitrage():
    """Create an order book with arbitrage opportunity (combined ask < 1)."""
    return OrderBook(
        market_id="test_market_123",
        yes_asks=[OrderBookLevel(price=Decimal("0.45"), size=Decimal("100"))],
        yes_bids=[OrderBookLevel(price=Decimal("0.44"), size=Decimal("100"))],
        no_asks=[OrderBookLevel(price=Decimal("0.50"), size=Decimal("100"))],
        no_bids=[OrderBookLevel(price=Decimal("0.49"), size=Decimal("100"))],
        timestamp=datetime.utcnow(),
    )


@pytest.fixture
def order_book_no_arbitrage():
    """Create an order book without arbitrage opportunity."""
    return OrderBook(
        market_id="test_market_456",
        yes_asks=[OrderBookLevel(price=Decimal("0.55"), size=Decimal("100"))],
        yes_bids=[OrderBookLevel(price=Decimal("0.54"), size=Decimal("100"))],
        no_asks=[OrderBookLevel(price=Decimal("0.50"), size=Decimal("100"))],
        no_bids=[OrderBookLevel(price=Decimal("0.49"), size=Decimal("100"))],
        timestamp=datetime.utcnow(),
    )


class TestStrategyEngineLifecycle:
    """Tests for StrategyEngine lifecycle methods."""

    @pytest.mark.asyncio
    async def test_start_sets_running_state(self, strategy_engine):
        """Verify start() sets engine to running state."""
        await strategy_engine.start()
        assert strategy_engine.is_running

    @pytest.mark.asyncio
    async def test_stop_clears_running_state(self, strategy_engine):
        """Verify stop() clears running state."""
        await strategy_engine.start()
        await strategy_engine.stop()
        assert not strategy_engine.is_running

    @pytest.mark.asyncio
    async def test_start_subscribes_to_events(self, strategy_engine, mock_event_bus):
        """Verify start() subscribes to required event channels."""
        await strategy_engine.start()

        subscribe_calls = mock_event_bus.subscribe.call_args_list
        subscribed_patterns = [call[0][0] for call in subscribe_calls]

        assert "market.orderbook.*" in subscribed_patterns
        assert "system.strategy.enable" in subscribed_patterns
        assert "system.strategy.disable" in subscribed_patterns

    @pytest.mark.asyncio
    async def test_start_starts_registered_strategies(
        self, strategy_engine, mock_strategy
    ):
        """Verify start() calls start() on all registered strategies."""
        strategy_engine.register_strategy(mock_strategy)
        await strategy_engine.start()

        assert mock_strategy._started

    @pytest.mark.asyncio
    async def test_stop_stops_registered_strategies(
        self, strategy_engine, mock_strategy
    ):
        """Verify stop() calls stop() on all registered strategies."""
        strategy_engine.register_strategy(mock_strategy)
        await strategy_engine.start()
        await strategy_engine.stop()

        assert mock_strategy._stopped


class TestStrategyRegistration:
    """Tests for strategy registration functionality."""

    def test_register_strategy_adds_to_engine(self, strategy_engine, mock_strategy):
        """Verify register_strategy adds strategy to engine."""
        strategy_engine.register_strategy(mock_strategy)

        assert strategy_engine.strategy_count == 1
        assert strategy_engine.get_strategy("test_strategy") is mock_strategy

    def test_register_strategy_updates_market_mapping(
        self, strategy_engine, mock_strategy
    ):
        """Verify register_strategy creates market-to-strategy mappings."""
        mock_strategy.subscribe_to_market("market_1")
        mock_strategy.subscribe_to_market("market_2")
        strategy_engine.register_strategy(mock_strategy)

        # Check internal market mapping
        assert "market_1" in strategy_engine._market_to_strategies
        assert "market_2" in strategy_engine._market_to_strategies
        assert "test_strategy" in strategy_engine._market_to_strategies["market_1"]

    def test_register_duplicate_strategy_is_idempotent(
        self, strategy_engine, mock_strategy
    ):
        """Verify registering same strategy twice doesn't duplicate."""
        strategy_engine.register_strategy(mock_strategy)
        strategy_engine.register_strategy(mock_strategy)

        assert strategy_engine.strategy_count == 1

    def test_unregister_strategy_removes_from_engine(
        self, strategy_engine, mock_strategy
    ):
        """Verify unregister_strategy removes strategy."""
        strategy_engine.register_strategy(mock_strategy)
        strategy_engine.unregister_strategy("test_strategy")

        assert strategy_engine.strategy_count == 0
        assert strategy_engine.get_strategy("test_strategy") is None

    def test_unregister_strategy_cleans_market_mappings(
        self, strategy_engine, mock_strategy
    ):
        """Verify unregister_strategy removes market mappings."""
        mock_strategy.subscribe_to_market("market_1")
        strategy_engine.register_strategy(mock_strategy)
        strategy_engine.unregister_strategy("test_strategy")

        assert "test_strategy" not in strategy_engine._market_to_strategies.get(
            "market_1", set()
        )

    def test_unregister_nonexistent_strategy_is_noop(self, strategy_engine):
        """Verify unregistering non-existent strategy doesn't raise."""
        strategy_engine.unregister_strategy("nonexistent")
        # No exception should be raised

    def test_get_strategy_returns_none_for_unknown(self, strategy_engine):
        """Verify get_strategy returns None for unknown strategies."""
        assert strategy_engine.get_strategy("unknown") is None


class TestStrategyEnableDisable:
    """Tests for runtime enable/disable functionality."""

    @pytest.mark.asyncio
    async def test_enable_strategy_enables_registered_strategy(
        self, strategy_engine, mock_strategy
    ):
        """Verify enable_strategy enables a registered strategy."""
        mock_strategy.disable()
        strategy_engine.register_strategy(mock_strategy)

        result = await strategy_engine.enable_strategy("test_strategy")

        assert result is True
        assert mock_strategy.enabled is True

    @pytest.mark.asyncio
    async def test_enable_nonexistent_strategy_returns_false(self, strategy_engine):
        """Verify enable_strategy returns False for unknown strategy."""
        result = await strategy_engine.enable_strategy("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_disable_strategy_disables_registered_strategy(
        self, strategy_engine, mock_strategy
    ):
        """Verify disable_strategy disables a registered strategy."""
        strategy_engine.register_strategy(mock_strategy)

        result = await strategy_engine.disable_strategy("test_strategy")

        assert result is True
        assert mock_strategy.enabled is False

    @pytest.mark.asyncio
    async def test_disable_nonexistent_strategy_returns_false(self, strategy_engine):
        """Verify disable_strategy returns False for unknown strategy."""
        result = await strategy_engine.disable_strategy("nonexistent")
        assert result is False

    def test_is_strategy_enabled_returns_true_for_enabled(
        self, strategy_engine, mock_strategy
    ):
        """Verify is_strategy_enabled returns True for enabled strategies."""
        strategy_engine.register_strategy(mock_strategy)
        assert strategy_engine.is_strategy_enabled("test_strategy") is True

    def test_is_strategy_enabled_returns_false_for_disabled(
        self, strategy_engine, mock_strategy
    ):
        """Verify is_strategy_enabled returns False for disabled strategies."""
        mock_strategy.disable()
        strategy_engine.register_strategy(mock_strategy)
        assert strategy_engine.is_strategy_enabled("test_strategy") is False

    def test_is_strategy_enabled_returns_false_for_unknown(self, strategy_engine):
        """Verify is_strategy_enabled returns False for unknown strategies."""
        assert strategy_engine.is_strategy_enabled("unknown") is False

    def test_enabled_strategies_property(self, strategy_engine):
        """Verify enabled_strategies returns list of enabled strategy names."""
        strategy1 = MockStrategy("strategy_1")
        strategy2 = MockStrategy("strategy_2")
        strategy2.disable()

        strategy_engine.register_strategy(strategy1)
        strategy_engine.register_strategy(strategy2)

        enabled = strategy_engine.enabled_strategies
        assert "strategy_1" in enabled
        assert "strategy_2" not in enabled


class TestMarketDataRouting:
    """Tests for market data routing to strategies."""

    @pytest.mark.asyncio
    async def test_routes_market_data_to_subscribed_strategy(
        self, strategy_engine, mock_strategy, mock_event_bus, order_book_with_arbitrage
    ):
        """Verify market data is routed to strategies subscribed to that market."""
        mock_strategy.subscribe_to_market("test_market_123")
        strategy_engine.register_strategy(mock_strategy)
        await strategy_engine.start()

        # Simulate market data event
        await strategy_engine._on_market_data(
            {
                "market_id": "test_market_123",
                "yes_bid": "0.44",
                "yes_ask": "0.45",
                "no_bid": "0.49",
                "no_ask": "0.50",
            }
        )

        # Should publish signal since arbitrage opportunity exists
        assert mock_event_bus.publish.called

    @pytest.mark.asyncio
    async def test_does_not_route_to_unsubscribed_strategy(
        self, strategy_engine, mock_strategy, mock_event_bus
    ):
        """Verify market data is not routed to strategies not subscribed."""
        mock_strategy.subscribe_to_market("different_market")
        strategy_engine.register_strategy(mock_strategy)
        await strategy_engine.start()

        # Reset mock to clear subscription calls
        mock_event_bus.publish.reset_mock()

        await strategy_engine._on_market_data(
            {
                "market_id": "test_market_123",
                "yes_bid": "0.44",
                "yes_ask": "0.45",
                "no_bid": "0.49",
                "no_ask": "0.50",
            }
        )

        # No signal should be published since strategy isn't subscribed
        assert not mock_event_bus.publish.called

    @pytest.mark.asyncio
    async def test_does_not_route_to_disabled_strategy(
        self, strategy_engine, mock_strategy, mock_event_bus
    ):
        """Verify disabled strategies don't receive market data."""
        mock_strategy.subscribe_to_market("test_market_123")
        mock_strategy.disable()
        strategy_engine.register_strategy(mock_strategy)
        await strategy_engine.start()

        mock_event_bus.publish.reset_mock()

        await strategy_engine._on_market_data(
            {
                "market_id": "test_market_123",
                "yes_bid": "0.44",
                "yes_ask": "0.45",
                "no_bid": "0.49",
                "no_ask": "0.50",
            }
        )

        # No signal published since strategy is disabled
        assert not mock_event_bus.publish.called

    @pytest.mark.asyncio
    async def test_routes_to_multiple_strategies(
        self, strategy_engine, mock_event_bus
    ):
        """Verify market data can be routed to multiple strategies."""
        strategy1 = MockStrategy("strategy_1")
        strategy2 = MockStrategy("strategy_2")

        strategy1.subscribe_to_market("shared_market")
        strategy2.subscribe_to_market("shared_market")

        strategy_engine.register_strategy(strategy1)
        strategy_engine.register_strategy(strategy2)
        await strategy_engine.start()

        mock_event_bus.publish.reset_mock()

        await strategy_engine._on_market_data(
            {
                "market_id": "shared_market",
                "yes_bid": "0.44",
                "yes_ask": "0.45",
                "no_bid": "0.49",
                "no_ask": "0.50",
            }
        )

        # Both strategies should generate signals
        publish_calls = mock_event_bus.publish.call_args_list
        channels = [call[0][0] for call in publish_calls]

        assert "signal.strategy_1" in channels
        assert "signal.strategy_2" in channels

    @pytest.mark.asyncio
    async def test_handles_missing_market_id(self, strategy_engine, mock_event_bus):
        """Verify handler gracefully handles missing market_id."""
        await strategy_engine.start()
        mock_event_bus.publish.reset_mock()

        # This should not raise
        await strategy_engine._on_market_data({})

        # No signal should be published
        assert not mock_event_bus.publish.called


class TestSignalPublishing:
    """Tests for signal publishing to EventBus."""

    @pytest.mark.asyncio
    async def test_publishes_signal_to_correct_channel(
        self, strategy_engine, mock_strategy, mock_event_bus
    ):
        """Verify signals are published to signal.{strategy_name} channel."""
        signal = TradingSignal(
            signal_id="test-signal-123",
            strategy_name="test_strategy",
            market_id="test_market",
            signal_type=SignalType.ARBITRAGE,
            confidence=0.9,
            target_size_usd=Decimal("25.00"),
            yes_price=Decimal("0.45"),
            no_price=Decimal("0.50"),
        )

        await strategy_engine._publish_signal("test_strategy", signal)

        mock_event_bus.publish.assert_called_once()
        call_args = mock_event_bus.publish.call_args
        assert call_args[0][0] == "signal.test_strategy"

    @pytest.mark.asyncio
    async def test_signal_payload_contains_required_fields(
        self, strategy_engine, mock_event_bus
    ):
        """Verify published signal contains all required fields."""
        signal = TradingSignal(
            signal_id="test-signal-123",
            strategy_name="test_strategy",
            market_id="test_market",
            signal_type=SignalType.ARBITRAGE,
            confidence=0.85,
            target_size_usd=Decimal("25.00"),
            yes_price=Decimal("0.45"),
            no_price=Decimal("0.50"),
            metadata={"reason": "test"},
        )

        await strategy_engine._publish_signal("test_strategy", signal)

        payload = mock_event_bus.publish.call_args[0][1]

        assert payload["signal_id"] == "test-signal-123"
        assert payload["strategy"] == "test_strategy"
        assert payload["market_id"] == "test_market"
        assert payload["signal_type"] == "ARBITRAGE"
        assert payload["target_size_usd"] == "25.00"
        assert payload["yes_price"] == "0.45"
        assert payload["no_price"] == "0.50"
        assert payload["confidence"] == 0.85
        assert payload["metadata"] == {"reason": "test"}


class TestEventHandlers:
    """Tests for system event handlers."""

    @pytest.mark.asyncio
    async def test_on_enable_strategy_handler(
        self, strategy_engine, mock_strategy, mock_event_bus
    ):
        """Verify _on_enable_strategy enables the named strategy."""
        mock_strategy.disable()
        strategy_engine.register_strategy(mock_strategy)

        await strategy_engine._on_enable_strategy({"strategy": "test_strategy"})

        assert mock_strategy.enabled is True

    @pytest.mark.asyncio
    async def test_on_disable_strategy_handler(
        self, strategy_engine, mock_strategy, mock_event_bus
    ):
        """Verify _on_disable_strategy disables the named strategy."""
        strategy_engine.register_strategy(mock_strategy)

        await strategy_engine._on_disable_strategy({"strategy": "test_strategy"})

        assert mock_strategy.enabled is False

    @pytest.mark.asyncio
    async def test_handlers_ignore_missing_strategy_name(self, strategy_engine):
        """Verify handlers don't fail when strategy name is missing."""
        # Should not raise
        await strategy_engine._on_enable_strategy({})
        await strategy_engine._on_disable_strategy({})


class TestHealthCheck:
    """Tests for health check functionality."""

    @pytest.mark.asyncio
    async def test_health_degraded_when_no_strategies(self, strategy_engine):
        """Verify health is degraded when no strategies registered."""
        await strategy_engine.start()
        result = await strategy_engine.health_check()

        assert result.status == HealthStatus.DEGRADED
        assert "No strategies registered" in result.message

    @pytest.mark.asyncio
    async def test_health_degraded_when_no_enabled(
        self, strategy_engine, mock_strategy
    ):
        """Verify health is degraded when no strategies enabled."""
        mock_strategy.disable()
        strategy_engine.register_strategy(mock_strategy)
        await strategy_engine.start()

        result = await strategy_engine.health_check()

        assert result.status == HealthStatus.DEGRADED
        assert "No strategies enabled" in result.message

    @pytest.mark.asyncio
    async def test_health_healthy_with_enabled_strategies(
        self, strategy_engine, mock_strategy
    ):
        """Verify health is healthy when strategies are enabled."""
        strategy_engine.register_strategy(mock_strategy)
        await strategy_engine.start()

        result = await strategy_engine.health_check()

        assert result.status == HealthStatus.HEALTHY
        assert "1/1" in result.message


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_strategy_error_does_not_crash_engine(
        self, strategy_engine, mock_event_bus
    ):
        """Verify strategy errors don't crash the engine."""

        class ErrorStrategy(MockStrategy):
            async def on_market_data(self, market_id, book):
                raise RuntimeError("Strategy error")
                yield  # Makes it a generator

        error_strategy = ErrorStrategy("error_strategy")
        error_strategy.subscribe_to_market("test_market")
        strategy_engine.register_strategy(error_strategy)
        await strategy_engine.start()

        # Should not raise
        await strategy_engine._on_market_data(
            {
                "market_id": "test_market",
                "yes_bid": "0.44",
                "yes_ask": "0.45",
                "no_bid": "0.49",
                "no_ask": "0.50",
            }
        )

    @pytest.mark.asyncio
    async def test_strategy_start_error_logged_but_continues(
        self, strategy_engine, mock_event_bus
    ):
        """Verify strategy start errors don't prevent engine startup."""

        class StartErrorStrategy(MockStrategy):
            async def start(self):
                raise RuntimeError("Start error")

        error_strategy = StartErrorStrategy("error_strategy")
        working_strategy = MockStrategy("working_strategy")

        strategy_engine.register_strategy(error_strategy)
        strategy_engine.register_strategy(working_strategy)

        # Should not raise
        await strategy_engine.start()

        # Working strategy should still be started
        assert working_strategy._started

    @pytest.mark.asyncio
    async def test_strategy_stop_error_logged_but_continues(
        self, strategy_engine, mock_event_bus
    ):
        """Verify strategy stop errors don't prevent engine shutdown."""

        class StopErrorStrategy(MockStrategy):
            async def stop(self):
                raise RuntimeError("Stop error")

        error_strategy = StopErrorStrategy("error_strategy")
        working_strategy = MockStrategy("working_strategy")

        strategy_engine.register_strategy(error_strategy)
        strategy_engine.register_strategy(working_strategy)

        await strategy_engine.start()

        # Should not raise
        await strategy_engine.stop()

        # Working strategy should still be stopped
        assert working_strategy._stopped
