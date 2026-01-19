"""E2E test fixtures with real Redis and SQLite, mocked external services."""

import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

# Skip if Redis not available
SKIP_REDIS = os.environ.get("SKIP_REDIS_TESTS", "0") == "1"

pytestmark = [
    pytest.mark.skipif(SKIP_REDIS, reason="Redis tests disabled"),
    pytest.mark.asyncio,
]


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def redis_event_bus():
    """Create a real Redis EventBus for e2e testing.

    This fixture provides an actual Redis connection for testing
    the full pub/sub flow.
    """
    from mercury.core.events import EventBus

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    bus = EventBus(redis_url=redis_url)

    try:
        await bus.connect()
        yield bus
    except Exception as e:
        pytest.skip(f"Redis not available: {e}")
    finally:
        if bus.is_connected:
            await bus.disconnect()


@pytest.fixture
def e2e_config(tmp_path):
    """Create a ConfigManager for e2e tests with real SQLite."""
    from mercury.core.config import ConfigManager

    config = ConfigManager(config_path=None)

    # Override with test values
    config._config = {
        "mercury": {
            "log_level": "DEBUG",
            "dry_run": True,
            "shutdown_timeout_seconds": 5.0,
            "drain_timeout_seconds": 10.0,
        },
        "database": {
            "path": str(tmp_path / "e2e_test.db"),
        },
        "redis": {
            "url": os.environ.get("REDIS_URL", "redis://localhost:6379"),
        },
        "risk": {
            "max_daily_loss_usd": Decimal("100.0"),
            "max_position_size_usd": Decimal("50.0"),
            "max_daily_trades": 50,
            "circuit_breaker_warning_failures": 3,
            "circuit_breaker_caution_failures": 4,
            "circuit_breaker_halt_failures": 5,
            "circuit_breaker_cooldown_seconds": 60,
        },
        "execution": {
            "max_concurrent": 5,
            "max_queue_size": 100,
            "queue_timeout_seconds": 30.0,
        },
        "settlement": {
            "check_interval_seconds": 1,  # Fast for testing
            "resolution_wait_seconds": 0,  # No wait in tests
            "max_claim_attempts": 3,
        },
        "strategies": {
            "gabagool": {
                "enabled": True,
                "min_spread_threshold": Decimal("0.015"),
                "max_trade_size_usd": Decimal("25.0"),
            },
        },
        "polymarket": {
            "clob_url": "https://clob.polymarket.com/",
            "gamma_url": "https://gamma-api.polymarket.com",
            "private_key": "0x" + "0" * 64,  # Dummy key for tests
        },
    }

    return config


@pytest.fixture
def mock_clob_client():
    """Create a mock CLOB client for e2e tests.

    Simulates successful order placement and fills.
    """
    from mercury.integrations.polymarket.types import (
        OrderResult, DualLegOrderResult, OrderStatus
    )

    client = MagicMock()
    client.connect = AsyncMock()
    client.close = AsyncMock()
    client.is_connected = True

    # Track placed orders for verification
    client.placed_orders = []

    async def mock_place_order(
        token_id: str,
        side: str,
        size: Decimal,
        price: Decimal,
        **kwargs
    ) -> OrderResult:
        """Simulate order placement with immediate fill."""
        order_id = f"test-order-{len(client.placed_orders)}"
        client.placed_orders.append({
            "order_id": order_id,
            "token_id": token_id,
            "side": side,
            "size": size,
            "price": price,
        })
        return OrderResult(
            order_id=order_id,
            status=OrderStatus.FILLED,
            filled_size=size,
            avg_fill_price=price,
            fee=size * price * Decimal("0.001"),  # 0.1% fee
        )

    async def mock_place_dual_leg_order(
        yes_token_id: str,
        no_token_id: str,
        size: Decimal,
        yes_price: Decimal,
        no_price: Decimal,
        **kwargs
    ) -> DualLegOrderResult:
        """Simulate dual-leg arbitrage order."""
        yes_order = await mock_place_order(yes_token_id, "BUY", size, yes_price)
        no_order = await mock_place_order(no_token_id, "BUY", size, no_price)

        return DualLegOrderResult(
            success=True,
            yes_order=yes_order,
            no_order=no_order,
            total_cost=(size * yes_price) + (size * no_price),
        )

    async def mock_cancel_order(order_id: str) -> bool:
        return True

    async def mock_get_order(order_id: str) -> Optional[OrderResult]:
        for order in client.placed_orders:
            if order["order_id"] == order_id:
                return OrderResult(
                    order_id=order_id,
                    status=OrderStatus.FILLED,
                    filled_size=order["size"],
                    avg_fill_price=order["price"],
                )
        return None

    client.place_order = mock_place_order
    client.place_dual_leg_order = mock_place_dual_leg_order
    client.cancel_order = mock_cancel_order
    client.get_order = mock_get_order

    return client


@pytest.fixture
def mock_gamma_client():
    """Create a mock Gamma client for market info."""
    from mercury.integrations.polymarket.types import MarketInfo

    client = MagicMock()
    client.connect = AsyncMock()
    client.close = AsyncMock()

    # Default market info
    default_market = MarketInfo(
        condition_id="test-condition-123",
        question="Will BTC reach $100k?",
        end_date=datetime.now(timezone.utc),
        resolved=False,
        resolution=None,
        yes_token_id="yes-token-123",
        no_token_id="no-token-123",
        slug="btc-100k",
    )

    resolved_market = MarketInfo(
        condition_id="test-condition-123",
        question="Will BTC reach $100k?",
        end_date=datetime.now(timezone.utc),
        resolved=True,
        resolution="YES",
        yes_token_id="yes-token-123",
        no_token_id="no-token-123",
        slug="btc-100k",
    )

    # Track resolution state for testing
    client._resolved = False
    client._resolution = "YES"

    async def mock_get_market_info(condition_id: str, use_cache: bool = True) -> Optional[MarketInfo]:
        if client._resolved:
            return MarketInfo(
                condition_id=condition_id,
                question="Test market",
                end_date=datetime.now(timezone.utc),
                resolved=True,
                resolution=client._resolution,
                yes_token_id="yes-token-123",
                no_token_id="no-token-123",
                slug="test-market",
            )
        return MarketInfo(
            condition_id=condition_id,
            question="Test market",
            end_date=datetime.now(timezone.utc),
            resolved=False,
            resolution=None,
            yes_token_id="yes-token-123",
            no_token_id="no-token-123",
            slug="test-market",
        )

    def set_resolved(resolved: bool, resolution: str = "YES"):
        client._resolved = resolved
        client._resolution = resolution

    client.get_market_info = mock_get_market_info
    client.set_resolved = set_resolved

    return client


@pytest.fixture
def mock_polygon_client():
    """Create a mock Polygon (chain) client for settlement."""
    from dataclasses import dataclass

    @dataclass
    class TxReceipt:
        status: bool
        tx_hash: str
        gas_used: int

    client = MagicMock()
    client.connect = AsyncMock()
    client.close = AsyncMock()

    async def mock_redeem(condition_id: str) -> TxReceipt:
        return TxReceipt(
            status=True,
            tx_hash="0x" + "a" * 64,
            gas_used=150000,
        )

    client.redeem_ctf_positions = mock_redeem

    return client


@pytest.fixture
def arbitrage_orderbook():
    """Create an OrderBook with arbitrage opportunity."""
    from mercury.domain.orderbook import MarketOrderBook, InMemoryOrderBook, PriceLevel

    book = MarketOrderBook.create(
        market_id="test-market-btc",
        yes_token_id="yes-token-123",
        no_token_id="no-token-123",
    )

    # Set up an arbitrage opportunity: YES ask 0.48 + NO ask 0.50 = 0.98 < 1.0
    # Profit = 1.0 - 0.98 = 0.02 (2 cents per share)
    book.yes_book.update_ask(Decimal("0.48"), Decimal("100"))
    book.yes_book.update_bid(Decimal("0.46"), Decimal("100"))
    book.no_book.update_ask(Decimal("0.50"), Decimal("100"))
    book.no_book.update_bid(Decimal("0.48"), Decimal("100"))

    return book


@pytest.fixture
def no_arbitrage_orderbook():
    """Create an OrderBook without arbitrage opportunity."""
    from mercury.domain.orderbook import MarketOrderBook

    book = MarketOrderBook.create(
        market_id="test-market-eth",
        yes_token_id="yes-token-456",
        no_token_id="no-token-456",
    )

    # No arbitrage: YES ask 0.52 + NO ask 0.50 = 1.02 > 1.0
    book.yes_book.update_ask(Decimal("0.52"), Decimal("100"))
    book.yes_book.update_bid(Decimal("0.50"), Decimal("100"))
    book.no_book.update_ask(Decimal("0.50"), Decimal("100"))
    book.no_book.update_bid(Decimal("0.48"), Decimal("100"))

    return book


class EventCollector:
    """Collects events from the EventBus for test verification."""

    def __init__(self):
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._lock = asyncio.Lock()

    async def collect(self, channel: str, data: dict[str, Any]) -> None:
        """Collect an event."""
        async with self._lock:
            self.events.append((channel, data))

    def get_events(self, channel_pattern: str = "") -> list[tuple[str, dict]]:
        """Get events matching a channel pattern."""
        if not channel_pattern:
            return list(self.events)
        return [(c, d) for c, d in self.events if channel_pattern in c]

    def get_channels(self) -> list[str]:
        """Get list of event channels received."""
        return [c for c, _ in self.events]

    def clear(self) -> None:
        """Clear collected events."""
        self.events.clear()

    async def wait_for_event(
        self,
        channel_pattern: str,
        timeout: float = 5.0,
        count: int = 1,
    ) -> list[tuple[str, dict]]:
        """Wait for specific events to arrive."""
        start = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - start) < timeout:
            events = self.get_events(channel_pattern)
            if len(events) >= count:
                return events[:count]
            await asyncio.sleep(0.05)

        # Return what we have, let the test fail if not enough
        return self.get_events(channel_pattern)


@pytest.fixture
def event_collector():
    """Create an EventCollector for tracking events."""
    return EventCollector()
