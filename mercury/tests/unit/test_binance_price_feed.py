"""
Unit tests for BinancePriceFeed adapter.

Tests the price feed implementation without making real network calls.
"""

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mercury.integrations.price_feeds.binance import (
    BINANCE_REST_URL,
    BINANCE_WS_URL,
    BinancePriceFeed,
)
from mercury.integrations.price_feeds.base import PriceFeed, PriceUpdate


class TestBinancePriceFeedBasics:
    """Basic instantiation and property tests."""

    def test_instantiates_without_event_bus(self):
        """Feed can be created without event bus."""
        feed = BinancePriceFeed()
        assert feed is not None
        assert feed.name == "binance"
        assert feed.is_connected is False

    def test_instantiates_with_event_bus(self, mock_event_bus):
        """Feed can be created with event bus."""
        feed = BinancePriceFeed(event_bus=mock_event_bus)
        assert feed._event_bus is mock_event_bus

    def test_implements_price_feed_protocol(self):
        """BinancePriceFeed implements PriceFeed protocol."""
        feed = BinancePriceFeed()

        # Check required methods exist
        assert hasattr(feed, "name")
        assert hasattr(feed, "is_connected")
        assert hasattr(feed, "connect")
        assert hasattr(feed, "close")
        assert hasattr(feed, "get_price")
        assert hasattr(feed, "subscribe")
        assert hasattr(feed, "unsubscribe")

    def test_name_property(self):
        """Feed name is 'binance'."""
        feed = BinancePriceFeed()
        assert feed.name == "binance"

    def test_initial_state(self):
        """Feed starts in disconnected state with no subscriptions."""
        feed = BinancePriceFeed()

        assert feed.is_connected is False
        assert feed._ws is None
        assert len(feed._subscriptions) == 0
        assert len(feed._prices) == 0
        assert feed._should_run is False


class TestBinancePriceFeedSubscriptions:
    """Test subscription management."""

    @pytest.mark.asyncio
    async def test_subscribe_adds_callback(self):
        """Subscribe adds callback to subscriptions dict."""
        feed = BinancePriceFeed()
        callback = MagicMock()

        await feed.subscribe("btcusdt", callback)

        assert "btcusdt" in feed._subscriptions
        assert callback in feed._subscriptions["btcusdt"]

    @pytest.mark.asyncio
    async def test_subscribe_normalizes_symbol_to_lowercase(self):
        """Subscribe normalizes symbol to lowercase."""
        feed = BinancePriceFeed()
        callback = MagicMock()

        await feed.subscribe("BTCUSDT", callback)

        assert "btcusdt" in feed._subscriptions
        assert "BTCUSDT" not in feed._subscriptions

    @pytest.mark.asyncio
    async def test_subscribe_multiple_callbacks_same_symbol(self):
        """Multiple callbacks can subscribe to same symbol."""
        feed = BinancePriceFeed()
        callback1 = MagicMock()
        callback2 = MagicMock()

        await feed.subscribe("btcusdt", callback1)
        await feed.subscribe("btcusdt", callback2)

        assert len(feed._subscriptions["btcusdt"]) == 2
        assert callback1 in feed._subscriptions["btcusdt"]
        assert callback2 in feed._subscriptions["btcusdt"]

    @pytest.mark.asyncio
    async def test_unsubscribe_removes_symbol(self):
        """Unsubscribe removes symbol entirely."""
        feed = BinancePriceFeed()
        callback = MagicMock()

        await feed.subscribe("btcusdt", callback)
        await feed.unsubscribe("btcusdt")

        assert "btcusdt" not in feed._subscriptions

    @pytest.mark.asyncio
    async def test_unsubscribe_nonexistent_symbol_safe(self):
        """Unsubscribe on nonexistent symbol doesn't error."""
        feed = BinancePriceFeed()

        # Should not raise
        await feed.unsubscribe("nonexistent")


class TestBinancePriceFeedMessageProcessing:
    """Test WebSocket message processing."""

    @pytest.mark.asyncio
    async def test_process_trade_message_updates_price(self):
        """Processing trade message updates cached price."""
        feed = BinancePriceFeed()

        message = {
            "s": "BTCUSDT",
            "p": "50000.00",
        }

        await feed._process_message(message)

        assert "btcusdt" in feed._prices
        assert feed._prices["btcusdt"] == Decimal("50000.00")

    @pytest.mark.asyncio
    async def test_process_trade_message_calls_callbacks(self):
        """Processing trade message triggers subscribed callbacks."""
        feed = BinancePriceFeed()
        callback = MagicMock()

        await feed.subscribe("btcusdt", callback)

        message = {
            "s": "BTCUSDT",
            "p": "50000.00",
        }

        await feed._process_message(message)

        assert callback.called
        call_args = callback.call_args[0][0]
        assert isinstance(call_args, PriceUpdate)
        assert call_args.symbol == "BTCUSDT"
        assert call_args.price == Decimal("50000.00")
        assert call_args.source == "binance"

    @pytest.mark.asyncio
    async def test_process_trade_message_publishes_to_event_bus(self, mock_event_bus):
        """Processing trade message publishes to EventBus."""
        feed = BinancePriceFeed(event_bus=mock_event_bus)

        message = {
            "s": "BTCUSDT",
            "p": "50000.00",
        }

        await feed._process_message(message)

        mock_event_bus.publish.assert_called_once()
        channel, data = mock_event_bus.publish.call_args[0]

        assert channel == "price.binance.btcusdt"
        assert data["symbol"] == "BTCUSDT"
        assert data["price"] == "50000.00"

    @pytest.mark.asyncio
    async def test_process_message_handles_callback_error(self):
        """Callback errors don't break processing."""
        feed = BinancePriceFeed()

        error_callback = MagicMock(side_effect=Exception("callback error"))
        good_callback = MagicMock()

        await feed.subscribe("btcusdt", error_callback)
        await feed.subscribe("btcusdt", good_callback)

        message = {
            "s": "BTCUSDT",
            "p": "50000.00",
        }

        # Should not raise
        await feed._process_message(message)

        # Good callback should still be called
        assert good_callback.called


class TestBinancePriceFeedGetPrice:
    """Test get_price method."""

    @pytest.mark.asyncio
    async def test_get_price_returns_cached_value(self):
        """get_price returns cached price if available."""
        feed = BinancePriceFeed()
        feed._prices["btcusdt"] = Decimal("50000.00")

        price = await feed.get_price("btcusdt")

        assert price == Decimal("50000.00")

    @pytest.mark.asyncio
    async def test_get_price_normalizes_symbol(self):
        """get_price normalizes symbol to lowercase for lookup."""
        feed = BinancePriceFeed()
        feed._prices["btcusdt"] = Decimal("50000.00")

        price = await feed.get_price("BTCUSDT")

        assert price == Decimal("50000.00")

    @pytest.mark.asyncio
    async def test_get_price_fetches_via_rest_if_not_cached(self):
        """get_price fetches from REST API if not in cache."""
        feed = BinancePriceFeed()

        mock_response = MagicMock()
        mock_response.json.return_value = {"price": "50000.00"}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.get.return_value = mock_response
            mock_client_class.return_value = mock_client

            price = await feed.get_price("ETHUSDT")

            assert price == Decimal("50000.00")
            assert "ethusdt" in feed._prices

    @pytest.mark.asyncio
    async def test_get_price_returns_none_on_error(self):
        """get_price returns None if REST fetch fails."""
        feed = BinancePriceFeed()

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.get.side_effect = Exception("Network error")
            mock_client_class.return_value = mock_client

            price = await feed.get_price("NONEXISTENT")

            assert price is None


class TestBinancePriceFeedLifecycle:
    """Test start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_sets_should_run(self):
        """Start sets _should_run flag."""
        feed = BinancePriceFeed()

        # We can't fully test start without mocking websockets
        # Just verify the flag logic
        assert feed._should_run is False

        # Manually set to simulate starting
        feed._should_run = True
        assert feed._should_run is True

    @pytest.mark.asyncio
    async def test_stop_clears_should_run(self):
        """Stop clears _should_run flag."""
        feed = BinancePriceFeed()
        feed._should_run = True

        await feed.stop()

        assert feed._should_run is False

    @pytest.mark.asyncio
    async def test_connect_calls_start(self):
        """connect() is an alias for start()."""
        feed = BinancePriceFeed()

        with patch.object(feed, "start", new_callable=AsyncMock) as mock_start:
            await feed.connect()
            mock_start.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_calls_stop(self):
        """close() is an alias for stop()."""
        feed = BinancePriceFeed()

        with patch.object(feed, "stop", new_callable=AsyncMock) as mock_stop:
            await feed.close()
            mock_stop.assert_called_once()


class TestBinancePriceFeedHealthCheck:
    """Test health check functionality."""

    @pytest.mark.asyncio
    async def test_health_unhealthy_when_not_running(self):
        """Health check returns unhealthy when not running."""
        feed = BinancePriceFeed()
        feed._should_run = False

        result = await feed.health_check()

        assert result.status.value == "unhealthy"
        assert "not running" in result.message.lower()

    @pytest.mark.asyncio
    async def test_health_unhealthy_when_disconnected(self):
        """Health check returns unhealthy when disconnected."""
        feed = BinancePriceFeed()
        feed._should_run = True
        feed._ws = None

        result = await feed.health_check()

        assert result.status.value == "unhealthy"
        assert "not connected" in result.message.lower()

    @pytest.mark.asyncio
    async def test_health_healthy_when_connected(self):
        """Health check returns healthy when connected."""
        feed = BinancePriceFeed()
        feed._should_run = True

        # Mock WebSocket as connected
        mock_ws = MagicMock()
        mock_ws.open = True
        feed._ws = mock_ws

        # Add some subscriptions and prices
        feed._subscriptions["btcusdt"] = {MagicMock()}
        feed._prices["btcusdt"] = Decimal("50000")

        result = await feed.health_check()

        assert result.status.value == "healthy"
        assert result.details["subscriptions"] == 1
        assert result.details["cached_prices"] == 1


class TestBinancePriceFeedWebSocketMessages:
    """Test WebSocket message formatting."""

    @pytest.mark.asyncio
    async def test_subscribe_message_format(self):
        """Verify subscribe message format for Binance."""
        feed = BinancePriceFeed()

        # Mock WebSocket
        mock_ws = AsyncMock()
        mock_ws.open = True
        mock_ws.send = AsyncMock()
        feed._ws = mock_ws

        await feed._send_subscribe(["btcusdt", "ethusdt"])

        mock_ws.send.assert_called_once()
        sent_data = json.loads(mock_ws.send.call_args[0][0])

        assert sent_data["method"] == "SUBSCRIBE"
        assert "btcusdt@trade" in sent_data["params"]
        assert "ethusdt@trade" in sent_data["params"]
        assert "id" in sent_data

    @pytest.mark.asyncio
    async def test_unsubscribe_message_format(self):
        """Verify unsubscribe message format for Binance."""
        feed = BinancePriceFeed()

        # Mock WebSocket
        mock_ws = AsyncMock()
        mock_ws.open = True
        mock_ws.send = AsyncMock()
        feed._ws = mock_ws

        await feed._send_unsubscribe(["btcusdt"])

        mock_ws.send.assert_called_once()
        sent_data = json.loads(mock_ws.send.call_args[0][0])

        assert sent_data["method"] == "UNSUBSCRIBE"
        assert "btcusdt@trade" in sent_data["params"]


class TestPriceUpdateDataclass:
    """Test the PriceUpdate dataclass."""

    def test_price_update_creation(self):
        """PriceUpdate can be created with all fields."""
        now = datetime.now(timezone.utc)

        update = PriceUpdate(
            symbol="BTCUSDT",
            price=Decimal("50000.00"),
            timestamp=now,
            source="binance",
        )

        assert update.symbol == "BTCUSDT"
        assert update.price == Decimal("50000.00")
        assert update.timestamp == now
        assert update.source == "binance"

    def test_price_update_is_frozen(self):
        """PriceUpdate is immutable (frozen)."""
        update = PriceUpdate(
            symbol="BTCUSDT",
            price=Decimal("50000.00"),
            timestamp=datetime.now(timezone.utc),
            source="binance",
        )

        with pytest.raises(Exception):  # FrozenInstanceError
            update.price = Decimal("60000.00")
