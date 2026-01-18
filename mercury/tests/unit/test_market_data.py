"""
Unit tests for MarketDataService.

Tests cover:
- Service lifecycle (start/stop)
- Market subscription
- Order book state management
- Best prices retrieval
- Staleness detection
- Event publishing
"""
import asyncio
import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mercury.domain.market import OrderBook, OrderBookLevel
from mercury.services.market_data import MarketDataService, MarketState


@pytest.fixture
def mock_websocket():
    """Create a mock WebSocket client."""
    ws = MagicMock()
    ws.start = AsyncMock()
    ws.stop = AsyncMock()
    ws.subscribe = AsyncMock()
    ws.unsubscribe = AsyncMock()
    ws.health_check = AsyncMock(return_value=MagicMock(
        status=MagicMock(value="healthy")
    ))
    return ws


@pytest.fixture
def mock_config():
    """Create a mock ConfigManager."""
    config = MagicMock()
    config.get.return_value = None
    config.get_decimal.return_value = Decimal("30.0")
    return config


@pytest.fixture
def mock_event_bus():
    """Create a mock EventBus."""
    bus = MagicMock()
    bus.publish = AsyncMock()
    bus.subscribe = AsyncMock()
    bus.unsubscribe = AsyncMock()
    return bus


@pytest.fixture
def service(mock_config, mock_event_bus, mock_websocket):
    """Create a MarketDataService instance for testing."""
    return MarketDataService(
        config=mock_config,
        event_bus=mock_event_bus,
        websocket=mock_websocket,
    )


class TestMarketDataServiceLifecycle:
    """Tests for service lifecycle management."""

    @pytest.mark.asyncio
    async def test_start_initializes_service(self, service, mock_websocket, mock_event_bus):
        """Test that start() initializes all components."""
        await service.start()

        assert service.is_running
        mock_websocket.start.assert_called_once()
        mock_event_bus.subscribe.assert_called()
        mock_event_bus.publish.assert_called()

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self, service, mock_websocket):
        """Test that calling start() multiple times is safe."""
        await service.start()
        await service.start()

        assert mock_websocket.start.call_count == 1

    @pytest.mark.asyncio
    async def test_stop_cleans_up_resources(self, service, mock_websocket, mock_event_bus):
        """Test that stop() cleans up all resources."""
        await service.start()
        await service.stop()

        assert not service.is_running
        mock_websocket.stop.assert_called_once()
        mock_event_bus.unsubscribe.assert_called()

    @pytest.mark.asyncio
    async def test_stop_cancels_monitor_task(self, service):
        """Test that stop() cancels the monitoring task."""
        await service.start()
        assert service._monitor_task is not None

        await service.stop()
        assert service._monitor_task.cancelled() or service._monitor_task.done()


class TestMarketSubscription:
    """Tests for market subscription functionality."""

    @pytest.mark.asyncio
    async def test_subscribe_market_adds_to_subscribed_markets(self, service):
        """Test that subscribe_market adds market to subscribed set."""
        await service.start()
        await service.subscribe_market("test-market-id")

        assert "test-market-id" in service.subscribed_markets
        await service.stop()

    @pytest.mark.asyncio
    async def test_subscribe_market_with_tokens(self, service, mock_websocket):
        """Test subscribing with explicit token IDs."""
        await service.start()
        await service.subscribe_market(
            "test-market",
            yes_token_id="yes-token-123",
            no_token_id="no-token-456"
        )

        assert "test-market" in service.subscribed_markets
        mock_websocket.subscribe.assert_called_with(["yes-token-123", "no-token-456"])
        await service.stop()

    @pytest.mark.asyncio
    async def test_subscribe_market_generates_placeholder_tokens(self, service):
        """Test that subscribe_market generates placeholder tokens when not provided."""
        await service.start()
        await service.subscribe_market("my-market")

        state = service._markets["my-market"]
        assert state.yes_token_id == "my-market_yes"
        assert state.no_token_id == "my-market_no"
        await service.stop()

    @pytest.mark.asyncio
    async def test_subscribe_market_is_idempotent(self, service, mock_websocket):
        """Test that subscribing to same market twice is safe."""
        await service.start()
        await service.subscribe_market("test-market")
        await service.subscribe_market("test-market")

        assert mock_websocket.subscribe.call_count == 1
        await service.stop()

    @pytest.mark.asyncio
    async def test_unsubscribe_market_removes_from_subscribed(self, service):
        """Test that unsubscribe removes market from subscribed set."""
        await service.start()
        await service.subscribe_market("test-market")
        await service.unsubscribe_market("test-market")

        assert "test-market" not in service.subscribed_markets
        await service.stop()

    @pytest.mark.asyncio
    async def test_unsubscribe_nonexistent_market_is_safe(self, service):
        """Test that unsubscribing from non-existent market is safe."""
        await service.start()
        await service.unsubscribe_market("nonexistent")  # Should not raise
        await service.stop()


class TestOrderBookManagement:
    """Tests for order book state management."""

    def test_get_order_book_returns_none_for_unknown_market(self, service):
        """Test that get_order_book returns None for unknown markets."""
        assert service.get_order_book("unknown-market") is None

    def test_get_order_book_returns_book_when_available(self, service):
        """Test that get_order_book returns the order book when available."""
        book = OrderBook(
            market_id="test",
            yes_bids=[OrderBookLevel(price=Decimal("0.45"), size=Decimal("100"))],
            yes_asks=[OrderBookLevel(price=Decimal("0.50"), size=Decimal("100"))],
            no_bids=[OrderBookLevel(price=Decimal("0.48"), size=Decimal("100"))],
            no_asks=[OrderBookLevel(price=Decimal("0.52"), size=Decimal("100"))],
        )
        service._order_books["test"] = book

        retrieved = service.get_order_book("test")
        assert retrieved is not None
        assert retrieved.yes_bids[0].price == Decimal("0.45")

    def test_order_book_properties(self, service):
        """Test OrderBook computed properties."""
        book = OrderBook(
            market_id="test",
            yes_bids=[OrderBookLevel(price=Decimal("0.45"), size=Decimal("100"))],
            yes_asks=[OrderBookLevel(price=Decimal("0.50"), size=Decimal("100"))],
            no_bids=[OrderBookLevel(price=Decimal("0.48"), size=Decimal("100"))],
            no_asks=[OrderBookLevel(price=Decimal("0.52"), size=Decimal("100"))],
        )
        service._order_books["test"] = book

        retrieved = service.get_order_book("test")
        assert retrieved.yes_best_bid == Decimal("0.45")
        assert retrieved.yes_best_ask == Decimal("0.50")
        assert retrieved.no_best_bid == Decimal("0.48")
        assert retrieved.no_best_ask == Decimal("0.52")


class TestBestPrices:
    """Tests for best prices retrieval."""

    def test_get_best_prices_returns_none_for_unknown_market(self, service):
        """Test that get_best_prices returns None for unknown markets."""
        assert service.get_best_prices("unknown") is None

    def test_get_best_prices_returns_yes_bid_ask(self, service):
        """Test that get_best_prices returns YES bid/ask tuple."""
        book = OrderBook(
            market_id="test",
            yes_bids=[OrderBookLevel(price=Decimal("0.45"), size=Decimal("100"))],
            yes_asks=[OrderBookLevel(price=Decimal("0.50"), size=Decimal("100"))],
            no_bids=[],
            no_asks=[],
        )
        service._order_books["test"] = book

        result = service.get_best_prices("test")
        assert result == (Decimal("0.45"), Decimal("0.50"))

    def test_get_best_prices_returns_none_when_incomplete(self, service):
        """Test that get_best_prices returns None when book is incomplete."""
        # Book with only bids, no asks
        book = OrderBook(
            market_id="test",
            yes_bids=[OrderBookLevel(price=Decimal("0.45"), size=Decimal("100"))],
            yes_asks=[],  # No asks
            no_bids=[],
            no_asks=[],
        )
        service._order_books["test"] = book

        assert service.get_best_prices("test") is None


class TestStalenessDetection:
    """Tests for stale market data detection."""

    def test_is_market_stale_returns_true_for_unknown(self, service):
        """Test that is_market_stale returns True for unknown markets."""
        assert service.is_market_stale("unknown") is True

    def test_is_market_stale_returns_true_when_no_updates(self, service):
        """Test that is_market_stale returns True when there are no updates."""
        state = MarketState(
            market_id="test",
            yes_token_id="yes",
            no_token_id="no",
        )
        service._markets["test"] = state

        assert service.is_market_stale("test") is True

    @pytest.mark.asyncio
    async def test_check_staleness_publishes_alert(self, service, mock_event_bus):
        """Test that _check_staleness publishes stale alerts."""
        # Set up a market with old last_update
        service._markets["test"] = MarketState(
            market_id="test",
            yes_token_id="yes",
            no_token_id="no",
        )
        service._last_update["test"] = time.time() - 100  # 100 seconds ago
        service._stale_threshold = Decimal("30.0")

        await service._check_staleness()

        # Should have published stale alert
        mock_event_bus.publish.assert_called()
        # Find the stale alert call
        calls = mock_event_bus.publish.call_args_list
        stale_call = None
        for call in calls:
            if "stale" in call[0][0]:
                stale_call = call
                break
        assert stale_call is not None


class TestEventPublishing:
    """Tests for event publishing functionality."""

    @pytest.mark.asyncio
    async def test_publish_order_book_publishes_to_event_bus(self, service, mock_event_bus):
        """Test that _publish_order_book publishes to EventBus."""
        from datetime import datetime, timezone

        book = OrderBook(
            market_id="test",
            yes_bids=[OrderBookLevel(price=Decimal("0.45"), size=Decimal("100"))],
            yes_asks=[OrderBookLevel(price=Decimal("0.50"), size=Decimal("100"))],
            no_bids=[],
            no_asks=[],
            timestamp=datetime.now(timezone.utc),
        )

        await service._publish_order_book(book)

        mock_event_bus.publish.assert_called()
        channel = mock_event_bus.publish.call_args[0][0]
        assert channel == "market.orderbook.test"

    @pytest.mark.asyncio
    async def test_start_publishes_connected_event(self, service, mock_event_bus):
        """Test that start() publishes connected event."""
        await service.start()

        # Find the connected event call
        calls = mock_event_bus.publish.call_args_list
        connected_call = None
        for call in calls:
            if "market.data.connected" in call[0][0]:
                connected_call = call
                break
        assert connected_call is not None
        await service.stop()

    @pytest.mark.asyncio
    async def test_stop_publishes_disconnected_event(self, service, mock_event_bus):
        """Test that stop() publishes disconnected event."""
        await service.start()
        mock_event_bus.publish.reset_mock()
        await service.stop()

        # Find the disconnected event call
        calls = mock_event_bus.publish.call_args_list
        disconnected_call = None
        for call in calls:
            if "market.data.disconnected" in call[0][0]:
                disconnected_call = call
                break
        assert disconnected_call is not None


class TestHealthCheck:
    """Tests for health check functionality."""

    @pytest.mark.asyncio
    async def test_health_check_returns_healthy_when_running(self, service, mock_websocket):
        """Test health check returns healthy when service is running."""
        from mercury.core.lifecycle import HealthStatus

        mock_websocket.health_check.return_value = MagicMock(
            status=HealthStatus.HEALTHY,
            message="OK"
        )

        await service.start()
        result = await service.health_check()

        assert result.status == HealthStatus.HEALTHY
        await service.stop()

    @pytest.mark.asyncio
    async def test_health_check_returns_unhealthy_when_ws_down(self, service, mock_websocket):
        """Test health check returns unhealthy when WebSocket is down."""
        from mercury.core.lifecycle import HealthStatus

        mock_websocket.health_check.return_value = MagicMock(
            status=HealthStatus.UNHEALTHY,
            message="Not connected"
        )

        await service.start()
        result = await service.health_check()

        assert result.status == HealthStatus.UNHEALTHY
        await service.stop()


class TestMarketCount:
    """Tests for market counting properties."""

    @pytest.mark.asyncio
    async def test_market_count_returns_zero_initially(self, service):
        """Test that market_count is 0 initially."""
        assert service.market_count == 0

    @pytest.mark.asyncio
    async def test_market_count_increases_on_subscribe(self, service):
        """Test that market_count increases when subscribing."""
        await service.start()
        await service.subscribe_market("market-1")
        await service.subscribe_market("market-2")

        assert service.market_count == 2
        await service.stop()

    @pytest.mark.asyncio
    async def test_connected_tokens_count(self, service):
        """Test that connected_tokens count is accurate."""
        await service.start()
        await service.subscribe_market("market-1")

        # Each market has 2 tokens (yes + no)
        assert service.connected_tokens == 2
        await service.stop()
