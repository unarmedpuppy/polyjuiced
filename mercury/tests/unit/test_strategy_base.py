"""
Unit tests for BaseStrategy protocol.

Tests verify:
- Protocol can be implemented correctly
- Async generator pattern for on_market_data works
- Lifecycle methods (start/stop) work properly
- Enable/disable methods work
- Protocol is runtime checkable
"""
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import List
import pytest

from mercury.strategies.base import BaseStrategy
from mercury.domain.market import OrderBook, OrderBookLevel
from mercury.domain.signal import TradingSignal, SignalType, SignalPriority


class MockStrategy:
    """A concrete implementation of the BaseStrategy protocol for testing."""

    def __init__(self, name: str = "mock_strategy"):
        self._name = name
        self._enabled = True
        self._subscribed_markets: List[str] = []
        self._started = False
        self._stopped = False

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
        """Yield signals when arbitrage opportunity detected."""
        if book.has_arbitrage_opportunity:
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


@pytest.fixture
def mock_strategy() -> MockStrategy:
    """Create a mock strategy instance."""
    return MockStrategy("test_strategy")


@pytest.fixture
def order_book_with_arbitrage() -> OrderBook:
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
def order_book_no_arbitrage() -> OrderBook:
    """Create an order book without arbitrage opportunity (combined ask >= 1)."""
    return OrderBook(
        market_id="test_market_456",
        yes_asks=[OrderBookLevel(price=Decimal("0.55"), size=Decimal("100"))],
        yes_bids=[OrderBookLevel(price=Decimal("0.54"), size=Decimal("100"))],
        no_asks=[OrderBookLevel(price=Decimal("0.50"), size=Decimal("100"))],
        no_bids=[OrderBookLevel(price=Decimal("0.49"), size=Decimal("100"))],
        timestamp=datetime.utcnow(),
    )


class TestBaseStrategyProtocol:
    """Tests for BaseStrategy protocol compliance."""

    def test_protocol_is_runtime_checkable(self, mock_strategy: MockStrategy):
        """Verify that BaseStrategy is runtime checkable."""
        assert isinstance(mock_strategy, BaseStrategy)

    def test_name_property(self, mock_strategy: MockStrategy):
        """Verify name property returns strategy identifier."""
        assert mock_strategy.name == "test_strategy"

    def test_enabled_property(self, mock_strategy: MockStrategy):
        """Verify enabled property returns correct state."""
        assert mock_strategy.enabled is True
        mock_strategy.disable()
        assert mock_strategy.enabled is False

    def test_get_subscribed_markets(self, mock_strategy: MockStrategy):
        """Verify get_subscribed_markets returns list of market IDs."""
        assert mock_strategy.get_subscribed_markets() == []
        mock_strategy.subscribe_to_market("market_1")
        mock_strategy.subscribe_to_market("market_2")
        assert mock_strategy.get_subscribed_markets() == ["market_1", "market_2"]


class TestStrategyLifecycle:
    """Tests for strategy lifecycle methods."""

    @pytest.mark.asyncio
    async def test_start_lifecycle(self, mock_strategy: MockStrategy):
        """Verify start() initializes strategy resources."""
        assert mock_strategy._started is False
        await mock_strategy.start()
        assert mock_strategy._started is True

    @pytest.mark.asyncio
    async def test_stop_lifecycle(self, mock_strategy: MockStrategy):
        """Verify stop() cleans up strategy resources."""
        assert mock_strategy._stopped is False
        await mock_strategy.stop()
        assert mock_strategy._stopped is True

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, mock_strategy: MockStrategy):
        """Verify full lifecycle: start -> run -> stop."""
        await mock_strategy.start()
        assert mock_strategy._started is True
        assert mock_strategy._stopped is False

        await mock_strategy.stop()
        assert mock_strategy._started is True
        assert mock_strategy._stopped is True


class TestStrategyEnableDisable:
    """Tests for runtime enable/disable functionality."""

    def test_enable(self, mock_strategy: MockStrategy):
        """Verify enable() activates the strategy."""
        mock_strategy.disable()
        assert mock_strategy.enabled is False
        mock_strategy.enable()
        assert mock_strategy.enabled is True

    def test_disable(self, mock_strategy: MockStrategy):
        """Verify disable() deactivates the strategy."""
        assert mock_strategy.enabled is True
        mock_strategy.disable()
        assert mock_strategy.enabled is False

    def test_enable_disable_toggle(self, mock_strategy: MockStrategy):
        """Verify enable/disable can be toggled repeatedly."""
        for _ in range(3):
            mock_strategy.disable()
            assert mock_strategy.enabled is False
            mock_strategy.enable()
            assert mock_strategy.enabled is True


class TestOnMarketData:
    """Tests for on_market_data async generator."""

    @pytest.mark.asyncio
    async def test_yields_signal_on_arbitrage_opportunity(
        self,
        mock_strategy: MockStrategy,
        order_book_with_arbitrage: OrderBook,
    ):
        """Verify on_market_data yields signal when arbitrage detected."""
        signals = []
        async for signal in mock_strategy.on_market_data(
            "test_market_123", order_book_with_arbitrage
        ):
            signals.append(signal)

        assert len(signals) == 1
        signal = signals[0]
        assert signal.strategy_name == "test_strategy"
        assert signal.market_id == "test_market_123"
        assert signal.signal_type == SignalType.ARBITRAGE
        assert signal.confidence == 0.9
        assert signal.priority == SignalPriority.HIGH

    @pytest.mark.asyncio
    async def test_yields_no_signal_without_opportunity(
        self,
        mock_strategy: MockStrategy,
        order_book_no_arbitrage: OrderBook,
    ):
        """Verify on_market_data yields nothing when no opportunity."""
        signals = []
        async for signal in mock_strategy.on_market_data(
            "test_market_456", order_book_no_arbitrage
        ):
            signals.append(signal)

        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_signal_contains_correct_prices(
        self,
        mock_strategy: MockStrategy,
        order_book_with_arbitrage: OrderBook,
    ):
        """Verify signal contains correct price information."""
        signals = []
        async for signal in mock_strategy.on_market_data(
            "test_market_123", order_book_with_arbitrage
        ):
            signals.append(signal)

        signal = signals[0]
        assert signal.yes_price == Decimal("0.45")
        assert signal.no_price == Decimal("0.50")
        # Expected PnL = 1 - (0.45 + 0.50) = 0.05
        assert signal.expected_pnl == Decimal("0.05")

    @pytest.mark.asyncio
    async def test_multiple_market_updates(
        self,
        mock_strategy: MockStrategy,
        order_book_with_arbitrage: OrderBook,
        order_book_no_arbitrage: OrderBook,
    ):
        """Verify strategy processes multiple market updates correctly."""
        all_signals = []

        # First update - has opportunity
        async for signal in mock_strategy.on_market_data(
            "market_1", order_book_with_arbitrage
        ):
            all_signals.append(signal)

        # Second update - no opportunity
        async for signal in mock_strategy.on_market_data(
            "market_2", order_book_no_arbitrage
        ):
            all_signals.append(signal)

        # Third update - has opportunity again
        async for signal in mock_strategy.on_market_data(
            "market_3", order_book_with_arbitrage
        ):
            all_signals.append(signal)

        assert len(all_signals) == 2
        assert all_signals[0].market_id == "market_1"
        assert all_signals[1].market_id == "market_3"


class TestProtocolImplementationGuidelines:
    """Tests documenting expected protocol implementation patterns."""

    def test_strategy_name_should_be_unique(self):
        """Verify different strategies have different names."""
        strategy1 = MockStrategy("arbitrage_finder")
        strategy2 = MockStrategy("momentum_trader")
        assert strategy1.name != strategy2.name

    @pytest.mark.asyncio
    async def test_disabled_strategy_still_returns_signals(
        self,
        mock_strategy: MockStrategy,
        order_book_with_arbitrage: OrderBook,
    ):
        """
        Document that disabled strategies still process data.

        The StrategyEngine is responsible for checking enabled status,
        not the strategy itself. This allows for dry-run testing.
        """
        mock_strategy.disable()
        assert mock_strategy.enabled is False

        # Strategy still processes data even when disabled
        signals = []
        async for signal in mock_strategy.on_market_data(
            "test_market", order_book_with_arbitrage
        ):
            signals.append(signal)

        # Signal is generated - StrategyEngine decides whether to use it
        assert len(signals) == 1
