"""
Integration tests for MarketDataService with real Redis EventBus.

These tests require a running Redis instance. They verify:
- Order book updates flow through Redis pub/sub
- Staleness alerts are published via EventBus
- Multi-market subscriptions work with real event routing
- Event payloads are correctly serialized/deserialized

Skip these tests if Redis is not available by setting environment variable:
    SKIP_REDIS_TESTS=1

Run: pytest tests/integration/test_market_data_redis.py -v
"""
import asyncio
import os
import time
from decimal import Decimal
from typing import Any, Optional
from unittest.mock import MagicMock, AsyncMock

import pytest

# Skip all tests in this module if Redis tests are disabled
pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_REDIS_TESTS", "0") == "1",
    reason="Redis tests disabled via SKIP_REDIS_TESTS env var"
)


@pytest.fixture
async def redis_event_bus():
    """Create a real Redis EventBus for integration testing.

    Requires Redis running on localhost:6379.
    """
    from mercury.core.events import EventBus

    bus = EventBus(redis_url="redis://localhost:6379")
    try:
        await bus.connect()
        yield bus
    except Exception as e:
        pytest.skip(f"Redis not available: {e}")
    finally:
        try:
            await bus.disconnect()
        except Exception:
            pass


@pytest.fixture
def mock_config():
    """Create a mock ConfigManager for testing."""
    config = MagicMock()
    config.get.return_value = None
    config.get_decimal.return_value = Decimal("10.0")  # 10 second stale threshold
    return config


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


class TestMarketDataRedisIntegration:
    """Integration tests with real Redis EventBus."""

    @pytest.mark.asyncio
    async def test_orderbook_events_published_to_redis(
        self, redis_event_bus, mock_config, mock_websocket
    ):
        """Test that order book updates are published to Redis."""
        from mercury.services.market_data import MarketDataService

        # Collected events from Redis
        received_events: list[dict] = []

        async def event_handler(data: dict[str, Any]) -> None:
            received_events.append(data)

        # Subscribe to orderbook events
        await redis_event_bus.subscribe("market.orderbook.*", event_handler)

        # Create service with real EventBus
        service = MarketDataService(
            config=mock_config,
            event_bus=redis_event_bus,
            websocket=mock_websocket,
        )

        await service.start()
        await service.subscribe_market("redis-test-market", "redis-yes", "redis-no")

        # Trigger an order book update
        await service._on_book_update("redis-yes", {
            "bids": [{"price": "0.45", "size": "100"}],
            "asks": [{"price": "0.55", "size": "100"}],
        })
        await service._on_book_update("redis-no", {
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.60", "size": "100"}],
        })

        # Allow time for Redis pub/sub delivery
        await asyncio.sleep(0.2)

        await service.stop()

        # Verify events were received
        # Note: we should have received at least 2 orderbook events
        orderbook_events = [e for e in received_events if "market_id" in e]
        assert len(orderbook_events) >= 1, f"Expected orderbook events, got: {received_events}"

        # Check event content
        last_event = orderbook_events[-1]
        assert last_event["market_id"] == "redis-test-market"

    @pytest.mark.asyncio
    async def test_stale_alerts_published_to_redis(
        self, redis_event_bus, mock_config, mock_websocket
    ):
        """Test that stale alerts are published to Redis."""
        from mercury.services.market_data import MarketDataService, MarketState

        received_stale_events: list[dict] = []

        async def stale_handler(data: dict[str, Any]) -> None:
            received_stale_events.append(data)

        # Subscribe to stale events
        await redis_event_bus.subscribe("market.stale.*", stale_handler)

        service = MarketDataService(
            config=mock_config,
            event_bus=redis_event_bus,
            websocket=mock_websocket,
        )

        # Set up a stale market
        service._markets["stale-test"] = MarketState(
            market_id="stale-test",
            yes_token_id="yes",
            no_token_id="no",
        )
        service._last_update["stale-test"] = time.time() - 100  # 100 seconds ago
        service._stale_threshold = Decimal("10.0")

        # Trigger staleness check
        await service._check_staleness()

        # Allow time for Redis pub/sub delivery
        await asyncio.sleep(0.2)

        # Verify stale event received
        assert len(received_stale_events) >= 1, "Expected stale event to be published"
        event = received_stale_events[0]
        assert event["market_id"] == "stale-test"
        assert event["threshold_seconds"] == 10.0

    @pytest.mark.asyncio
    async def test_fresh_alerts_published_to_redis(
        self, redis_event_bus, mock_config, mock_websocket
    ):
        """Test that fresh alerts are published when market recovers."""
        from mercury.services.market_data import MarketDataService, MarketState

        received_fresh_events: list[dict] = []

        async def fresh_handler(data: dict[str, Any]) -> None:
            received_fresh_events.append(data)

        await redis_event_bus.subscribe("market.fresh.*", fresh_handler)

        service = MarketDataService(
            config=mock_config,
            event_bus=redis_event_bus,
            websocket=mock_websocket,
        )

        # Set up market marked as stale but now has fresh data
        service._markets["recovering-market"] = MarketState(
            market_id="recovering-market",
            yes_token_id="yes",
            no_token_id="no",
            is_marked_stale=True,  # Was stale
        )
        service._last_update["recovering-market"] = time.time()  # Now fresh
        service._stale_threshold = Decimal("10.0")

        # Trigger staleness check (should detect recovery)
        await service._check_staleness()

        await asyncio.sleep(0.2)

        assert len(received_fresh_events) >= 1, "Expected fresh event to be published"
        event = received_fresh_events[0]
        assert event["market_id"] == "recovering-market"

    @pytest.mark.asyncio
    async def test_multiple_subscribers_receive_events(
        self, redis_event_bus, mock_config, mock_websocket
    ):
        """Test that multiple subscribers receive the same events."""
        from mercury.services.market_data import MarketDataService

        events_subscriber_1: list[dict] = []
        events_subscriber_2: list[dict] = []

        async def handler_1(data: dict[str, Any]) -> None:
            events_subscriber_1.append(data)

        async def handler_2(data: dict[str, Any]) -> None:
            events_subscriber_2.append(data)

        # Two subscribers for same pattern
        await redis_event_bus.subscribe("market.orderbook.*", handler_1)
        await redis_event_bus.subscribe("market.orderbook.*", handler_2)

        service = MarketDataService(
            config=mock_config,
            event_bus=redis_event_bus,
            websocket=mock_websocket,
        )

        await service.start()
        await service.subscribe_market("multi-sub-market", "ms-yes", "ms-no")

        await service._on_book_update("ms-yes", {
            "bids": [{"price": "0.50", "size": "100"}],
            "asks": [{"price": "0.50", "size": "100"}],
        })
        await service._on_book_update("ms-no", {
            "bids": [{"price": "0.50", "size": "100"}],
            "asks": [{"price": "0.50", "size": "100"}],
        })

        await asyncio.sleep(0.2)
        await service.stop()

        # Both subscribers should receive events
        assert len(events_subscriber_1) >= 1
        assert len(events_subscriber_2) >= 1

    @pytest.mark.asyncio
    async def test_trade_events_published_to_redis(
        self, redis_event_bus, mock_config, mock_websocket
    ):
        """Test that trade events are published to Redis."""
        from mercury.services.market_data import MarketDataService

        received_trade_events: list[dict] = []

        async def trade_handler(data: dict[str, Any]) -> None:
            received_trade_events.append(data)

        await redis_event_bus.subscribe("market.trade.*", trade_handler)

        service = MarketDataService(
            config=mock_config,
            event_bus=redis_event_bus,
            websocket=mock_websocket,
        )

        # Publish a trade event
        await service.publish_trade(
            market_id="trade-test-market",
            token_id="trade-token",
            side="buy",
            price=Decimal("0.55"),
            size=Decimal("100"),
            trade_id="test-trade-001",
        )

        await asyncio.sleep(0.2)

        assert len(received_trade_events) >= 1, "Expected trade event to be published"
        event = received_trade_events[0]
        assert event["market_id"] == "trade-test-market"
        assert event["side"] == "buy"
        assert event["price"] == "0.55"


class TestMultiMarketRedisIntegration:
    """Integration tests for multi-market scenarios with Redis."""

    @pytest.mark.asyncio
    async def test_events_routed_to_correct_market_channel(
        self, redis_event_bus, mock_config, mock_websocket
    ):
        """Test that events are routed to market-specific channels."""
        from mercury.services.market_data import MarketDataService

        market_a_events: list[dict] = []
        market_b_events: list[dict] = []

        async def handler_a(data: dict[str, Any]) -> None:
            if data.get("market_id") == "market-a":
                market_a_events.append(data)

        async def handler_b(data: dict[str, Any]) -> None:
            if data.get("market_id") == "market-b":
                market_b_events.append(data)

        await redis_event_bus.subscribe("market.orderbook.market-a", handler_a)
        await redis_event_bus.subscribe("market.orderbook.market-b", handler_b)

        service = MarketDataService(
            config=mock_config,
            event_bus=redis_event_bus,
            websocket=mock_websocket,
        )

        await service.start()
        await service.subscribe_market("market-a", "yes-a", "no-a")
        await service.subscribe_market("market-b", "yes-b", "no-b")

        # Update market A
        await service._on_book_update("yes-a", {
            "bids": [{"price": "0.45", "size": "100"}],
            "asks": [{"price": "0.55", "size": "100"}],
        })
        await service._on_book_update("no-a", {
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.60", "size": "100"}],
        })

        # Update market B
        await service._on_book_update("yes-b", {
            "bids": [{"price": "0.35", "size": "200"}],
            "asks": [{"price": "0.65", "size": "200"}],
        })
        await service._on_book_update("no-b", {
            "bids": [{"price": "0.30", "size": "200"}],
            "asks": [{"price": "0.70", "size": "200"}],
        })

        await asyncio.sleep(0.3)
        await service.stop()

        # Each market's events should be on its specific channel
        assert len(market_a_events) >= 1, "Expected events for market-a"
        assert len(market_b_events) >= 1, "Expected events for market-b"

        # Verify correct prices in events
        assert any(
            e.get("yes_best_bid") == "0.45"
            for e in market_a_events if e.get("yes_best_bid")
        )
        assert any(
            e.get("yes_best_bid") == "0.35"
            for e in market_b_events if e.get("yes_best_bid")
        )

    @pytest.mark.asyncio
    async def test_independent_staleness_via_redis(
        self, redis_event_bus, mock_config, mock_websocket
    ):
        """Test staleness tracking is independent per market via Redis."""
        from mercury.services.market_data import MarketDataService, MarketState

        stale_events: list[dict] = []

        async def stale_handler(data: dict[str, Any]) -> None:
            stale_events.append(data)

        await redis_event_bus.subscribe("market.stale.*", stale_handler)

        service = MarketDataService(
            config=mock_config,
            event_bus=redis_event_bus,
            websocket=mock_websocket,
        )

        # Set up fresh and stale markets
        service._markets["fresh-market"] = MarketState(
            market_id="fresh-market",
            yes_token_id="yes-fresh",
            no_token_id="no-fresh",
        )
        service._last_update["fresh-market"] = time.time()

        service._markets["stale-market-1"] = MarketState(
            market_id="stale-market-1",
            yes_token_id="yes-stale-1",
            no_token_id="no-stale-1",
        )
        service._last_update["stale-market-1"] = time.time() - 100

        service._markets["stale-market-2"] = MarketState(
            market_id="stale-market-2",
            yes_token_id="yes-stale-2",
            no_token_id="no-stale-2",
        )
        service._last_update["stale-market-2"] = time.time() - 200

        service._stale_threshold = Decimal("10.0")

        await service._check_staleness()
        await asyncio.sleep(0.3)

        # Should have stale events for both stale markets, not fresh
        stale_market_ids = {e["market_id"] for e in stale_events}
        assert "fresh-market" not in stale_market_ids
        assert "stale-market-1" in stale_market_ids
        assert "stale-market-2" in stale_market_ids


class TestEventSerializationIntegration:
    """Integration tests for event serialization through Redis."""

    @pytest.mark.asyncio
    async def test_decimal_precision_preserved(
        self, redis_event_bus, mock_config, mock_websocket
    ):
        """Test that Decimal precision is preserved through Redis."""
        from mercury.services.market_data import MarketDataService

        received_events: list[dict] = []

        async def handler(data: dict[str, Any]) -> None:
            received_events.append(data)

        await redis_event_bus.subscribe("market.trade.*", handler)

        service = MarketDataService(
            config=mock_config,
            event_bus=redis_event_bus,
            websocket=mock_websocket,
        )

        # Publish trade with precise decimal values
        await service.publish_trade(
            market_id="precision-test",
            token_id="precision-token",
            side="buy",
            price=Decimal("0.123456789"),
            size=Decimal("999.999999"),
        )

        await asyncio.sleep(0.2)

        assert len(received_events) >= 1
        event = received_events[0]

        # Price and size should preserve full precision
        assert event["price"] == "0.123456789"
        assert event["size"] == "999.999999"

    @pytest.mark.asyncio
    async def test_timestamp_serialization(
        self, redis_event_bus, mock_config, mock_websocket
    ):
        """Test that timestamps are correctly serialized/deserialized."""
        from mercury.services.market_data import MarketDataService
        from datetime import datetime, timezone

        received_events: list[dict] = []

        async def handler(data: dict[str, Any]) -> None:
            received_events.append(data)

        await redis_event_bus.subscribe("market.trade.*", handler)

        service = MarketDataService(
            config=mock_config,
            event_bus=redis_event_bus,
            websocket=mock_websocket,
        )

        before = datetime.now(timezone.utc)

        await service.publish_trade(
            market_id="timestamp-test",
            token_id="ts-token",
            side="sell",
            price=Decimal("0.5"),
            size=Decimal("1"),
        )

        await asyncio.sleep(0.2)

        assert len(received_events) >= 1
        event = received_events[0]

        # Timestamp should be ISO format and parseable
        ts_str = event["timestamp"]
        assert "T" in ts_str  # ISO format contains T

        # Parse it back
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        assert ts >= before
