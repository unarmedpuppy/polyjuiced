"""
Integration tests for all Polymarket clients.

This module provides comprehensive integration tests for:
- Gamma API client (market discovery)
- CLOB client (order book fetching)
- WebSocket client (connection, reconnection, resubscription)

Tests use mocked API responses where appropriate and real Redis
for event bus integration tests.

Run: pytest tests/integration/test_polymarket_clients.py -v
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mercury.core.events import EventBus
from mercury.core.lifecycle import HealthStatus
from mercury.integrations.polymarket.clob import (
    CLOBClient,
    CLOBClientError,
    InsufficientLiquidityError,
)
from mercury.integrations.polymarket.gamma import (
    GammaClient,
    GammaClientError,
    MarketCache,
)
from mercury.integrations.polymarket.types import (
    Market15Min,
    MarketInfo,
    OrderBookData,
    OrderBookLevel,
    PolymarketSettings,
)
from mercury.integrations.polymarket.websocket import (
    PolymarketWebSocket,
    SubscriptionState,
    STALE_THRESHOLD,
)


# =============================================================================
# Shared Fixtures
# =============================================================================


@pytest.fixture
def polymarket_settings():
    """Create test Polymarket settings."""
    return PolymarketSettings(
        private_key="0x" + "a" * 64,
        api_key="test_api_key",
        api_secret="test_api_secret",
        api_passphrase="test_passphrase",
        gamma_url="https://gamma-api.polymarket.com",
        clob_url="https://clob.polymarket.com",
        ws_url="wss://ws-subscriptions-clob.polymarket.com/ws/market",
    )


@pytest.fixture
def sample_market_data():
    """Sample market API response data."""
    return {
        "conditionId": "0x1234567890abcdef",
        "questionId": "q_test_123",
        "question": "Will BTC reach $100k by end of 2025?",
        "slug": "btc-100k-2025",
        "clobTokenIds": json.dumps(["111222333", "444555666"]),
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.65", "0.35"]),
        "active": True,
        "closed": False,
        "resolved": False,
        "volume": "1500000",
        "liquidity": "75000",
        "endDate": "2025-12-31T23:59:59Z",
        "eventSlug": "crypto-predictions-2025",
        "eventTitle": "Crypto Predictions 2025",
    }


@pytest.fixture
def sample_15min_market_data():
    """Sample 15-minute market API response data."""
    end_timestamp = int(time.time()) // 900 * 900 + 900  # Next 15-min boundary
    return {
        "conditionId": "0xbtc15min123",
        "questionId": "q_btc_15m",
        "question": "BTC Up or Down?",
        "slug": f"btc-updown-15m-{end_timestamp}",
        "clobTokenIds": json.dumps(["up_token_123", "down_token_456"]),
        "outcomes": json.dumps(["Up", "Down"]),
        "outcomePrices": json.dumps(["0.48", "0.52"]),
        "active": True,
        "closed": False,
        "resolved": False,
    }


@pytest.fixture
def sample_order_book():
    """Sample order book data."""
    return {
        "bids": [
            {"price": "0.45", "size": "150"},
            {"price": "0.44", "size": "300"},
            {"price": "0.43", "size": "500"},
        ],
        "asks": [
            {"price": "0.47", "size": "100"},
            {"price": "0.48", "size": "250"},
            {"price": "0.49", "size": "400"},
        ],
    }


# =============================================================================
# Gamma Client Integration Tests - Market Discovery
# =============================================================================


class TestGammaMarketDiscovery:
    """Integration tests for Gamma API market discovery."""

    @pytest.mark.asyncio
    async def test_get_markets_returns_list(self, polymarket_settings, sample_market_data):
        """Test fetching list of markets from Gamma API."""
        client = GammaClient(polymarket_settings)
        await client.connect()

        with patch.object(client._client, "get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = [sample_market_data, sample_market_data]
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            markets = await client.get_markets(limit=10, active=True)

            assert len(markets) == 2
            assert markets[0]["conditionId"] == "0x1234567890abcdef"
            mock_get.assert_called_once()
            call_args = mock_get.call_args
            assert call_args[0][0] == "/markets"
            assert call_args[1]["params"]["limit"] == 10
            assert call_args[1]["params"]["active"] == "true"

        await client.close()

    @pytest.mark.asyncio
    async def test_get_market_info_parses_correctly(self, polymarket_settings, sample_market_data):
        """Test that market info is parsed into MarketInfo dataclass."""
        client = GammaClient(polymarket_settings)
        await client.connect()

        with patch.object(client._client, "get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = sample_market_data
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            market_info = await client.get_market_info("0x1234567890abcdef")

            assert market_info is not None
            assert isinstance(market_info, MarketInfo)
            assert market_info.condition_id == "0x1234567890abcdef"
            assert market_info.question == "Will BTC reach $100k by end of 2025?"
            assert market_info.yes_token_id == "111222333"
            assert market_info.no_token_id == "444555666"
            assert market_info.yes_price == Decimal("0.65")
            assert market_info.no_price == Decimal("0.35")
            assert market_info.active is True
            assert market_info.volume == Decimal("1500000")

        await client.close()

    @pytest.mark.asyncio
    async def test_search_markets_with_query(self, polymarket_settings, sample_market_data):
        """Test market search functionality."""
        client = GammaClient(polymarket_settings)
        await client.connect()

        with patch.object(client._client, "get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = [sample_market_data]
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            results = await client.search_markets("BTC", limit=5)

            assert len(results) == 1
            assert "BTC" in results[0]["question"]
            # Verify query parameter was passed
            call_args = mock_get.call_args
            assert call_args[1]["params"]["_q"] == "BTC"
            assert call_args[1]["params"]["limit"] == 5

        await client.close()

    @pytest.mark.asyncio
    async def test_find_15min_markets_discovers_active(
        self, polymarket_settings, sample_15min_market_data
    ):
        """Test 15-minute market discovery for arbitrage."""
        client = GammaClient(polymarket_settings)
        await client.connect()

        with patch.object(client._client, "get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = [sample_15min_market_data]
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            markets = await client.find_15min_markets("BTC")

            # Should find at least one market
            assert len(markets) >= 1
            market = markets[0]
            assert isinstance(market, Market15Min)
            assert market.asset == "BTC"
            assert market.yes_token_id == "up_token_123"
            assert market.no_token_id == "down_token_456"
            assert market.yes_price == Decimal("0.48")
            assert market.no_price == Decimal("0.52")

        await client.close()

    @pytest.mark.asyncio
    async def test_15min_market_combined_price_calculation(
        self, polymarket_settings, sample_15min_market_data
    ):
        """Test that 15-minute market calculates combined price correctly."""
        client = GammaClient(polymarket_settings)
        await client.connect()

        with patch.object(client._client, "get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = [sample_15min_market_data]
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            markets = await client.find_15min_markets("BTC")
            market = markets[0]

            # Combined price should be close to 1.0 (0.48 + 0.52 = 1.0)
            assert market.combined_price == Decimal("1.0")
            # Spread in cents (no arb opportunity at exact 1.0)
            assert market.spread_cents == Decimal("0")

        await client.close()

    @pytest.mark.asyncio
    async def test_get_events_returns_market_groups(self, polymarket_settings):
        """Test fetching events (groups of related markets)."""
        client = GammaClient(polymarket_settings)
        await client.connect()

        event_data = {
            "slug": "2025-elections",
            "title": "2025 Elections",
            "markets": ["market1", "market2"],
        }

        with patch.object(client._client, "get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = [event_data]
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            events = await client.get_events(limit=50, active=True)

            assert len(events) == 1
            assert events[0]["slug"] == "2025-elections"

        await client.close()

    @pytest.mark.asyncio
    async def test_market_info_caching_behavior(self, polymarket_settings, sample_market_data):
        """Test that market info is properly cached and reused."""
        client = GammaClient(polymarket_settings, cache_ttl=60.0)
        await client.connect()

        with patch.object(client._client, "get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = sample_market_data
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            # First call - should hit API
            result1 = await client.get_market_info("test_condition")
            assert mock_get.call_count == 1

            # Second call - should use cache
            result2 = await client.get_market_info("test_condition")
            assert mock_get.call_count == 1  # No additional call

            # Both results should be identical
            assert result1.condition_id == result2.condition_id

            # Check cache stats
            stats = client.cache_stats
            assert stats["hits"] >= 1
            assert stats["size"] == 1

            # Bypass cache explicitly
            result3 = await client.get_market_info("test_condition", use_cache=False)
            assert mock_get.call_count == 2

        await client.close()

    @pytest.mark.asyncio
    async def test_market_not_found_returns_none(self, polymarket_settings):
        """Test handling of 404 (market not found) responses."""
        client = GammaClient(polymarket_settings)
        await client.connect()

        with patch.object(client._client, "get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_get.return_value = mock_response

            result = await client.get_market("nonexistent_market")

            assert result is None

        await client.close()


# =============================================================================
# CLOB Client Integration Tests - Order Book Fetch
# =============================================================================


class TestCLOBOrderBookFetch:
    """Integration tests for CLOB order book fetching."""

    @pytest.fixture
    def mock_py_clob_client(self, sample_order_book):
        """Create mock py-clob-client."""
        client = MagicMock()
        client.get_order_book.return_value = sample_order_book
        client.get_balance_allowance.return_value = {
            "balance": 1000000000,  # 1000 USDC
            "allowance": 1000000000,
        }
        client.get_positions.return_value = []
        client.get_orders.return_value = []
        return client

    @pytest.mark.asyncio
    async def test_get_order_book_parses_levels(
        self, polymarket_settings, mock_py_clob_client, sample_order_book
    ):
        """Test that order book is fetched and parsed correctly."""
        client = CLOBClient(polymarket_settings)
        client._client = mock_py_clob_client
        client._connected = True

        book = await client.get_order_book("test_token_123")

        assert isinstance(book, OrderBookData)
        assert book.token_id == "test_token_123"

        # Verify bids (sorted by price descending)
        assert len(book.bids) == 3
        assert book.bids[0].price == Decimal("0.45")
        assert book.bids[0].size == Decimal("150")

        # Verify asks (sorted by price ascending)
        assert len(book.asks) == 3
        assert book.asks[0].price == Decimal("0.47")
        assert book.asks[0].size == Decimal("100")

    @pytest.mark.asyncio
    async def test_best_bid_ask_properties(
        self, polymarket_settings, mock_py_clob_client
    ):
        """Test order book best bid/ask calculations."""
        client = CLOBClient(polymarket_settings)
        client._client = mock_py_clob_client
        client._connected = True

        book = await client.get_order_book("test_token")

        assert book.best_bid == Decimal("0.45")
        assert book.best_ask == Decimal("0.47")
        assert book.spread == Decimal("0.02")
        assert book.midpoint == Decimal("0.46")

    @pytest.mark.asyncio
    async def test_order_book_depth_calculation(
        self, polymarket_settings, mock_py_clob_client
    ):
        """Test order book depth calculation at multiple levels."""
        client = CLOBClient(polymarket_settings)
        client._client = mock_py_clob_client
        client._connected = True

        book = await client.get_order_book("test_token")

        # Depth at 3 levels = sum of first 3 bid sizes + first 3 ask sizes
        # Bids: 150 + 300 + 500 = 950
        # Asks: 100 + 250 + 400 = 750
        # Total = 1700
        depth = book.depth_at_levels(3)
        assert depth == Decimal("1700")

    @pytest.mark.asyncio
    async def test_get_price_for_buy_side(
        self, polymarket_settings, mock_py_clob_client
    ):
        """Test getting price for BUY side returns best ask."""
        from mercury.integrations.polymarket.types import OrderSide

        client = CLOBClient(polymarket_settings)
        client._client = mock_py_clob_client
        client._connected = True

        price = await client.get_price("test_token", OrderSide.BUY)

        # BUY gets ask price
        assert price == Decimal("0.47")

    @pytest.mark.asyncio
    async def test_get_price_for_sell_side(
        self, polymarket_settings, mock_py_clob_client
    ):
        """Test getting price for SELL side returns best bid."""
        from mercury.integrations.polymarket.types import OrderSide

        client = CLOBClient(polymarket_settings)
        client._client = mock_py_clob_client
        client._connected = True

        price = await client.get_price("test_token", OrderSide.SELL)

        # SELL gets bid price
        assert price == Decimal("0.45")

    @pytest.mark.asyncio
    async def test_get_spread_returns_all_info(
        self, polymarket_settings, mock_py_clob_client
    ):
        """Test get_spread returns complete spread info."""
        client = CLOBClient(polymarket_settings)
        client._client = mock_py_clob_client
        client._connected = True

        spread_info = await client.get_spread("test_token")

        assert spread_info["bid"] == Decimal("0.45")
        assert spread_info["ask"] == Decimal("0.47")
        assert spread_info["spread"] == Decimal("0.02")

    @pytest.mark.asyncio
    async def test_empty_order_book_handling(self, polymarket_settings):
        """Test handling of empty order book."""
        mock_client = MagicMock()
        mock_client.get_order_book.return_value = {"bids": [], "asks": []}

        client = CLOBClient(polymarket_settings)
        client._client = mock_client
        client._connected = True

        book = await client.get_order_book("illiquid_token")

        assert len(book.bids) == 0
        assert len(book.asks) == 0
        assert book.best_bid is None
        assert book.best_ask is None
        assert book.midpoint is None
        assert book.spread is None

    @pytest.mark.asyncio
    async def test_client_not_connected_raises_error(self, polymarket_settings):
        """Test that operations fail when client not connected."""
        client = CLOBClient(polymarket_settings)

        with pytest.raises(CLOBClientError, match="not connected"):
            await client.get_order_book("test_token")

    @pytest.mark.asyncio
    async def test_parallel_order_book_fetch(
        self, polymarket_settings, mock_py_clob_client
    ):
        """Test fetching multiple order books in parallel."""
        client = CLOBClient(polymarket_settings)
        client._client = mock_py_clob_client
        client._connected = True

        # Fetch both YES and NO books in parallel
        yes_book, no_book = await asyncio.gather(
            client.get_order_book("yes_token"),
            client.get_order_book("no_token"),
        )

        assert yes_book.token_id == "yes_token"
        assert no_book.token_id == "no_token"
        # Verify both called the underlying API
        assert mock_py_clob_client.get_order_book.call_count == 2


# =============================================================================
# WebSocket Integration Tests - Connection/Reconnection/Resubscription
# =============================================================================


class TestWebSocketConnection:
    """Integration tests for WebSocket connection management."""

    @pytest.fixture
    def mock_event_bus(self):
        """Create mock EventBus."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        bus.subscribe = AsyncMock()
        return bus

    @pytest.fixture
    def mock_metrics(self):
        """Create mock MetricsEmitter."""
        metrics = MagicMock()
        metrics.update_websocket_status = MagicMock()
        metrics.record_websocket_reconnect = MagicMock()
        return metrics

    @pytest.fixture
    def ws_client(self, polymarket_settings, mock_event_bus, mock_metrics):
        """Create WebSocket client for testing."""
        return PolymarketWebSocket(
            settings=polymarket_settings,
            event_bus=mock_event_bus,
            metrics=mock_metrics,
        )

    @pytest.mark.asyncio
    async def test_connection_establishes_successfully(
        self, ws_client, mock_event_bus, mock_metrics
    ):
        """Test WebSocket connection establishment."""
        with patch("websockets.connect", new_callable=AsyncMock) as mock_connect:
            mock_ws = MagicMock()
            mock_ws.open = True
            mock_connect.return_value = mock_ws

            await ws_client._connect()

            assert ws_client.is_connected is True
            mock_metrics.update_websocket_status.assert_called_with(True)

            # Verify connection event published
            publish_calls = mock_event_bus.publish.call_args_list
            channels_published = [call[0][0] for call in publish_calls]
            assert "market.ws.connected" in channels_published

    @pytest.mark.asyncio
    async def test_subscription_creates_pending_entries(self, ws_client):
        """Test that subscribing creates pending subscription entries."""
        token_ids = ["token_1", "token_2", "token_3"]

        await ws_client.subscribe(token_ids)

        assert len(ws_client._subscriptions) == 3
        for tid in token_ids:
            assert tid in ws_client._subscriptions
            assert ws_client._subscriptions[tid].state == SubscriptionState.PENDING
            assert ws_client._subscriptions[tid].subscribed_at is not None

    @pytest.mark.asyncio
    async def test_subscription_sends_message_when_connected(self, ws_client):
        """Test that subscription sends WebSocket message when connected."""
        mock_ws = MagicMock()
        mock_ws.open = True
        mock_ws.send = AsyncMock()
        ws_client._ws = mock_ws

        await ws_client.subscribe(["token_abc"])

        mock_ws.send.assert_called_once()
        sent_message = json.loads(mock_ws.send.call_args[0][0])
        assert sent_message["type"] == "market"
        assert "token_abc" in sent_message["assets_ids"]

    @pytest.mark.asyncio
    async def test_subscription_confirmation_updates_state(self, ws_client):
        """Test that subscription confirmation updates entry state to ACTIVE."""
        ws_client._subscriptions["token_xyz"] = MagicMock(
            token_id="token_xyz",
            state=SubscriptionState.PENDING,
            confirmed_at=None,
        )

        ws_client._handle_subscription_confirmed({
            "type": "subscribed",
            "assets_ids": ["token_xyz"],
        })

        assert ws_client._subscriptions["token_xyz"].state == SubscriptionState.ACTIVE
        assert ws_client._subscriptions["token_xyz"].confirmed_at is not None

    @pytest.mark.asyncio
    async def test_reconnection_resubscribes_all_tokens(
        self, ws_client, mock_event_bus, mock_metrics
    ):
        """Test that reconnection restores all subscriptions."""
        # Setup existing subscriptions (mix of active and pending)
        ws_client._subscriptions = {
            "token_a": MagicMock(
                token_id="token_a",
                state=SubscriptionState.ACTIVE,
            ),
            "token_b": MagicMock(
                token_id="token_b",
                state=SubscriptionState.PENDING,
            ),
        }

        with patch("websockets.connect", new_callable=AsyncMock) as mock_connect:
            mock_ws = MagicMock()
            mock_ws.open = True
            mock_ws.send = AsyncMock()
            mock_connect.return_value = mock_ws

            await ws_client._connect()

            # Should send subscription for all tokens
            mock_ws.send.assert_called()
            sent_message = json.loads(mock_ws.send.call_args[0][0])
            assert "token_a" in sent_message["assets_ids"]
            assert "token_b" in sent_message["assets_ids"]

            # All should be reset to PENDING until confirmed
            for entry in ws_client._subscriptions.values():
                assert entry.state == SubscriptionState.PENDING

    @pytest.mark.asyncio
    async def test_disconnect_increments_reconnect_counter(
        self, ws_client, mock_event_bus, mock_metrics
    ):
        """Test that disconnection increments reconnect counter."""
        ws_client._should_run = False  # Prevent actual reconnection attempt
        initial_count = ws_client._conn_metrics.reconnect_count

        await ws_client._handle_disconnect()

        assert ws_client._conn_metrics.reconnect_count == initial_count + 1
        mock_metrics.record_websocket_reconnect.assert_called_once()

        # Verify disconnection event published
        publish_calls = mock_event_bus.publish.call_args_list
        channels_published = [call[0][0] for call in publish_calls]
        assert "market.ws.disconnected" in channels_published

    @pytest.mark.asyncio
    async def test_unsubscribe_removes_entries(self, ws_client):
        """Test that unsubscribe removes subscription entries."""
        ws_client._subscriptions = {
            "token_1": MagicMock(token_id="token_1", state=SubscriptionState.ACTIVE),
            "token_2": MagicMock(token_id="token_2", state=SubscriptionState.ACTIVE),
        }

        await ws_client.unsubscribe(["token_1"])

        assert "token_1" not in ws_client._subscriptions
        assert "token_2" in ws_client._subscriptions

    @pytest.mark.asyncio
    async def test_health_check_healthy_when_receiving(self, ws_client):
        """Test health check reports HEALTHY when connected and receiving."""
        ws_client._should_run = True
        mock_ws = MagicMock()
        mock_ws.open = True
        ws_client._ws = mock_ws
        ws_client._heartbeat.last_message_received = time.time()
        ws_client._heartbeat.missed_pongs = 0

        result = await ws_client.health_check()

        assert result.status == HealthStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_health_check_degraded_when_stale(self, ws_client):
        """Test health check reports DEGRADED when connection is stale."""
        ws_client._should_run = True
        mock_ws = MagicMock()
        mock_ws.open = True
        ws_client._ws = mock_ws
        # Set last message time to beyond stale threshold
        ws_client._heartbeat.last_message_received = time.time() - STALE_THRESHOLD - 10

        result = await ws_client.health_check()

        assert result.status == HealthStatus.DEGRADED
        assert "no messages" in result.message.lower()

    @pytest.mark.asyncio
    async def test_health_check_degraded_when_heartbeat_unhealthy(self, ws_client):
        """Test health check reports DEGRADED when heartbeat fails."""
        ws_client._should_run = True
        mock_ws = MagicMock()
        mock_ws.open = True
        ws_client._ws = mock_ws
        ws_client._heartbeat.last_message_received = time.time()
        ws_client._heartbeat.missed_pongs = 2  # Unhealthy threshold

        result = await ws_client.health_check()

        assert result.status == HealthStatus.DEGRADED
        assert "heartbeat" in result.message.lower()

    @pytest.mark.asyncio
    async def test_pong_message_resets_missed_pongs(self, ws_client):
        """Test that PONG message resets missed pong counter."""
        ws_client._heartbeat.missed_pongs = 1
        initial_pong_count = ws_client._heartbeat.pong_count

        await ws_client._process_message("PONG")

        assert ws_client._heartbeat.pong_count == initial_pong_count + 1
        assert ws_client._heartbeat.missed_pongs == 0

    @pytest.mark.asyncio
    async def test_ping_message_sends_pong_response(self, ws_client):
        """Test that PING message triggers PONG response."""
        mock_ws = MagicMock()
        mock_ws.send = AsyncMock()
        ws_client._ws = mock_ws

        await ws_client._process_message("PING")

        mock_ws.send.assert_called_once_with("PONG")


class TestWebSocketMessageProcessing:
    """Tests for WebSocket message processing and EventBus publishing."""

    @pytest.fixture
    def mock_event_bus(self):
        """Create mock EventBus with tracking."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        bus.subscribe = AsyncMock()
        bus.published_events = []

        async def track_publish(channel, data):
            bus.published_events.append((channel, data))

        bus.publish.side_effect = track_publish
        return bus

    @pytest.fixture
    def ws_client(self, polymarket_settings, mock_event_bus):
        """Create WebSocket client for testing."""
        return PolymarketWebSocket(
            settings=polymarket_settings,
            event_bus=mock_event_bus,
            metrics=None,
        )

    @pytest.mark.asyncio
    async def test_price_change_publishes_to_eventbus(self, ws_client, mock_event_bus):
        """Test that price changes publish to EventBus."""
        message = json.dumps({
            "price_changes": [
                {
                    "asset_id": "12345",
                    "best_bid": "0.50",
                    "best_ask": "0.52",
                }
            ]
        })

        await ws_client._process_message(message)

        # Verify publish was called
        assert mock_event_bus.publish.called
        # Find the price update call
        price_calls = [
            call for call in mock_event_bus.published_events
            if call[0].startswith("market.price.")
        ]
        assert len(price_calls) == 1
        channel, data = price_calls[0]
        assert channel == "market.price.12345"
        assert data["bid"] == "0.50"
        assert data["ask"] == "0.52"

    @pytest.mark.asyncio
    async def test_book_snapshot_publishes_to_eventbus(self, ws_client, mock_event_bus):
        """Test that book snapshots publish to EventBus."""
        message = json.dumps({
            "asset_id": "67890",
            "bids": [{"price": "0.45", "size": "100"}],
            "asks": [{"price": "0.55", "size": "100"}],
        })

        await ws_client._process_message(message)

        # Find the book update call
        book_calls = [
            call for call in mock_event_bus.published_events
            if call[0].startswith("market.book.")
        ]
        assert len(book_calls) == 1
        channel, data = book_calls[0]
        assert channel == "market.book.67890"
        assert data["best_bid"] == "0.45"
        assert data["best_ask"] == "0.55"

    @pytest.mark.asyncio
    async def test_receiving_data_confirms_pending_subscription(
        self, ws_client, mock_event_bus
    ):
        """Test that receiving market data confirms pending subscription."""
        # Setup pending subscription
        from mercury.integrations.polymarket.websocket import SubscriptionEntry

        ws_client._subscriptions["token_999"] = SubscriptionEntry(
            token_id="token_999",
            state=SubscriptionState.PENDING,
        )

        await ws_client._handle_price_change({
            "asset_id": "token_999",
            "best_bid": "0.45",
        })

        assert ws_client._subscriptions["token_999"].state == SubscriptionState.ACTIVE
        assert ws_client._subscriptions["token_999"].last_message_at is not None

    @pytest.mark.asyncio
    async def test_batch_messages_processed_individually(
        self, ws_client, mock_event_bus
    ):
        """Test that batch messages are processed individually."""
        message = json.dumps([
            {"asset_id": "token_1", "bids": [], "asks": []},
            {"asset_id": "token_2", "bids": [], "asks": []},
        ])

        await ws_client._process_message(message)

        # Should have published for both tokens
        book_calls = [
            call for call in mock_event_bus.published_events
            if call[0].startswith("market.book.")
        ]
        assert len(book_calls) == 2


# =============================================================================
# EventBus Integration with Real Redis
# =============================================================================


class TestEventBusWithRedis:
    """Integration tests using real Redis for event bus.

    These tests require a running Redis instance.
    Skip if Redis is not available.
    """

    @pytest.fixture
    async def redis_event_bus(self):
        """Create real EventBus connected to Redis."""
        bus = EventBus(redis_url="redis://localhost:6379")
        try:
            await bus.connect()
            yield bus
        except Exception:
            pytest.skip("Redis not available")
        finally:
            try:
                await bus.disconnect()
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_eventbus_publishes_and_receives(self, redis_event_bus):
        """Test that EventBus can publish and receive events via Redis."""
        received_events = []

        async def handler(event):
            received_events.append(event)

        await redis_event_bus.subscribe("test.channel", handler)
        await asyncio.sleep(0.1)  # Allow subscription to establish

        await redis_event_bus.publish("test.channel", {"message": "hello"})
        await asyncio.sleep(0.2)  # Allow message to be received

        assert len(received_events) >= 1
        assert received_events[-1]["message"] == "hello"

    @pytest.mark.asyncio
    async def test_eventbus_pattern_subscription(self, redis_event_bus):
        """Test pattern-based subscription with wildcards."""
        received_events = []

        async def handler(event):
            received_events.append(event)

        await redis_event_bus.subscribe("market.*", handler)
        await asyncio.sleep(0.1)

        await redis_event_bus.publish("market.price", {"price": "0.50"})
        await redis_event_bus.publish("market.book", {"depth": 100})
        await asyncio.sleep(0.2)

        assert len(received_events) >= 2

    @pytest.mark.asyncio
    async def test_websocket_events_flow_through_redis(self, redis_event_bus):
        """Test that WebSocket events properly flow through Redis EventBus."""
        received_events = []

        async def handler(event):
            received_events.append(event)

        await redis_event_bus.subscribe("market.ws.*", handler)
        await asyncio.sleep(0.1)

        # Simulate WebSocket connection event
        await redis_event_bus.publish("market.ws.connected", {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reconnect_count": 0,
        })
        await asyncio.sleep(0.2)

        assert len(received_events) >= 1
        assert "timestamp" in received_events[-1]

    @pytest.mark.asyncio
    async def test_multiple_handlers_same_channel(self, redis_event_bus):
        """Test multiple handlers on the same channel."""
        handler1_events = []
        handler2_events = []

        async def handler1(event):
            handler1_events.append(event)

        async def handler2(event):
            handler2_events.append(event)

        await redis_event_bus.subscribe("multi.handler", handler1)
        await redis_event_bus.subscribe("multi.handler", handler2)
        await asyncio.sleep(0.1)

        await redis_event_bus.publish("multi.handler", {"value": 42})
        await asyncio.sleep(0.2)

        # Both handlers should receive the event
        assert len(handler1_events) >= 1
        assert len(handler2_events) >= 1
        assert handler1_events[-1]["value"] == 42
        assert handler2_events[-1]["value"] == 42


# =============================================================================
# Cross-Client Integration Tests
# =============================================================================


class TestCrossClientIntegration:
    """Tests verifying correct interaction between all Polymarket clients."""

    @pytest.mark.asyncio
    async def test_gamma_market_to_clob_book_flow(self, polymarket_settings, sample_market_data):
        """Test flow from Gamma market discovery to CLOB order book fetch."""
        # Step 1: Discover market via Gamma
        gamma_client = GammaClient(polymarket_settings)
        await gamma_client.connect()

        with patch.object(gamma_client._client, "get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = sample_market_data
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            market_info = await gamma_client.get_market_info("test_market")

        await gamma_client.close()

        # Step 2: Use token IDs from market info to fetch order book
        clob_client = CLOBClient(polymarket_settings)
        mock_clob = MagicMock()
        mock_clob.get_order_book.return_value = {
            "bids": [{"price": "0.45", "size": "100"}],
            "asks": [{"price": "0.47", "size": "100"}],
        }
        clob_client._client = mock_clob
        clob_client._connected = True

        # Fetch books for both YES and NO tokens
        yes_book = await clob_client.get_order_book(market_info.yes_token_id)
        no_book = await clob_client.get_order_book(market_info.no_token_id)

        assert yes_book.token_id == market_info.yes_token_id
        assert no_book.token_id == market_info.no_token_id

    @pytest.mark.asyncio
    async def test_websocket_subscription_with_gamma_tokens(self, polymarket_settings):
        """Test subscribing WebSocket to tokens discovered via Gamma."""
        # Discover market
        gamma_client = GammaClient(polymarket_settings)
        await gamma_client.connect()

        market_data = {
            "conditionId": "test_cond",
            "questionId": "q",
            "question": "Test?",
            "slug": "test",
            "clobTokenIds": json.dumps(["yes_123", "no_456"]),
            "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(["0.5", "0.5"]),
            "active": True,
            "closed": False,
            "resolved": False,
        }

        with patch.object(gamma_client._client, "get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = market_data
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            market_info = await gamma_client.get_market_info("test")

        await gamma_client.close()

        # Subscribe WebSocket to discovered tokens
        mock_event_bus = MagicMock()
        mock_event_bus.publish = AsyncMock()

        ws_client = PolymarketWebSocket(
            settings=polymarket_settings,
            event_bus=mock_event_bus,
            metrics=None,
        )

        mock_ws = MagicMock()
        mock_ws.open = True
        mock_ws.send = AsyncMock()
        ws_client._ws = mock_ws

        # Subscribe to both token IDs from market info
        await ws_client.subscribe([market_info.yes_token_id, market_info.no_token_id])

        assert market_info.yes_token_id in ws_client._subscriptions
        assert market_info.no_token_id in ws_client._subscriptions

        # Verify WebSocket message was sent with both tokens
        sent_message = json.loads(mock_ws.send.call_args[0][0])
        assert market_info.yes_token_id in sent_message["assets_ids"]
        assert market_info.no_token_id in sent_message["assets_ids"]

    @pytest.mark.asyncio
    async def test_arbitrage_flow_gamma_to_clob(self, polymarket_settings):
        """Test complete arbitrage discovery flow from Gamma to CLOB."""
        # Step 1: Discover 15-min market with potential arbitrage
        gamma_client = GammaClient(polymarket_settings)
        await gamma_client.connect()

        end_ts = int(time.time()) // 900 * 900 + 900
        market_data = {
            "conditionId": "arb_market",
            "questionId": "q",
            "question": "BTC Up?",
            "slug": f"btc-updown-15m-{end_ts}",
            "clobTokenIds": json.dumps(["up_token", "down_token"]),
            "outcomes": json.dumps(["Up", "Down"]),
            "outcomePrices": json.dumps(["0.45", "0.45"]),  # Sum = 0.90, arb!
            "active": True,
            "closed": False,
            "resolved": False,
        }

        with patch.object(gamma_client._client, "get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = [market_data]
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            markets = await gamma_client.find_15min_markets("BTC")

        await gamma_client.close()

        # Verify arbitrage opportunity detected
        market = markets[0]
        assert market.combined_price == Decimal("0.90")
        assert market.spread_cents == Decimal("10")  # 10 cent spread

        # Step 2: Check CLOB for actual order book liquidity
        clob_client = CLOBClient(polymarket_settings)
        mock_clob = MagicMock()
        mock_clob.get_order_book.side_effect = [
            {
                "bids": [{"price": "0.44", "size": "100"}],
                "asks": [{"price": "0.46", "size": "50"}],  # YES at 0.46
            },
            {
                "bids": [{"price": "0.44", "size": "100"}],
                "asks": [{"price": "0.46", "size": "50"}],  # NO at 0.46
            },
        ]
        clob_client._client = mock_clob
        clob_client._connected = True

        yes_book, no_book = await asyncio.gather(
            clob_client.get_order_book(market.yes_token_id),
            clob_client.get_order_book(market.no_token_id),
        )

        # Verify CLOB prices
        combined_ask = yes_book.best_ask + no_book.best_ask
        assert combined_ask == Decimal("0.92")  # Still arbitrage!
        arbitrage_spread = Decimal("1.0") - combined_ask
        assert arbitrage_spread == Decimal("0.08")  # 8 cent profit potential
