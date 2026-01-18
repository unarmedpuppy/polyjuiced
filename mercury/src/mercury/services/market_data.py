"""Market Data Service - manages market data streams and order book state.

This service:
- Subscribes to WebSocket market data
- Maintains current order book state for each market
- Detects stale/missing data
- Publishes order book snapshots to EventBus
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Optional, Set

import structlog

from mercury.core.config import ConfigManager
from mercury.core.events import EventBus
from mercury.core.lifecycle import BaseComponent, HealthCheckResult, HealthStatus
from mercury.domain.market import OrderBook
from mercury.integrations.polymarket.types import (
    OrderBookData,
    OrderBookLevel,
    OrderBookSnapshot,
    PolymarketSettings,
)
from mercury.integrations.polymarket.websocket import PolymarketWebSocket

log = structlog.get_logger()

# Default configuration values
DEFAULT_STALE_THRESHOLD_SECONDS = 30.0
DEFAULT_REFRESH_INTERVAL_SECONDS = 5.0
DEFAULT_MAX_MARKETS = 100


@dataclass
class MarketState:
    """Internal state for a tracked market."""

    market_id: str
    yes_token_id: str
    no_token_id: str

    yes_book: Optional[OrderBookData] = None
    no_book: Optional[OrderBookData] = None

    last_yes_update: float = 0
    last_no_update: float = 0

    @property
    def last_update(self) -> float:
        """Timestamp of most recent update (either side)."""
        return max(self.last_yes_update, self.last_no_update)

    @property
    def is_stale(self) -> bool:
        """Whether data is stale (no updates for threshold period)."""
        if self.last_update == 0:
            return True
        # Will be checked by the service with its configured threshold
        return False

    def get_snapshot(self) -> Optional[OrderBookSnapshot]:
        """Get current order book snapshot if both sides available."""
        if self.yes_book is None or self.no_book is None:
            return None

        return OrderBookSnapshot(
            market_id=self.market_id,
            timestamp=datetime.now(timezone.utc),
            yes_book=self.yes_book,
            no_book=self.no_book,
        )


class MarketDataService(BaseComponent):
    """Service for managing real-time market data streams.

    This service:
    1. Connects to Polymarket WebSocket for real-time order book data
    2. Maintains current order book state for subscribed markets
    3. Publishes order book snapshots to EventBus
    4. Detects and reports stale market data

    Event channels subscribed:
    - market.price.{token_id} - Price updates from WebSocket
    - market.book.{token_id} - Full book updates from WebSocket
    - system.market.subscribe - Subscribe to new market

    Event channels published:
    - market.orderbook.{market_id} - Order book snapshots
    - market.stale.{market_id} - Stale data alerts
    - market.data.connected - Service connected
    - market.data.disconnected - Service disconnected
    """

    def __init__(
        self,
        config: ConfigManager,
        event_bus: EventBus,
        websocket: Optional[PolymarketWebSocket] = None,
    ):
        """Initialize the market data service.

        Args:
            config: Configuration manager.
            event_bus: EventBus for publishing updates.
            websocket: Optional pre-configured WebSocket client.
        """
        super().__init__()
        self._config = config
        self._event_bus = event_bus
        self._log = log.bind(component="market_data_service")

        # Configuration
        self._stale_threshold = config.get_decimal(
            "market_data.stale_threshold_seconds",
            Decimal(str(DEFAULT_STALE_THRESHOLD_SECONDS))
        )
        self._refresh_interval = config.get_decimal(
            "market_data.refresh_interval_seconds",
            Decimal(str(DEFAULT_REFRESH_INTERVAL_SECONDS))
        )

        # WebSocket client
        if websocket is None:
            settings = PolymarketSettings(
                private_key=config.get("polymarket.private_key", ""),
                ws_url=config.get(
                    "polymarket.ws_url",
                    "wss://ws-subscriptions-clob.polymarket.com/ws/market"
                ),
            )
            websocket = PolymarketWebSocket(settings, event_bus)

        self._websocket = websocket

        # Market state
        self._markets: Dict[str, MarketState] = {}
        self._token_to_market: Dict[str, str] = {}  # token_id -> market_id
        self._subscribed_tokens: Set[str] = set()

        # Tasks
        self._monitor_task: Optional[asyncio.Task] = None
        self._should_run: bool = False

    @property
    def market_count(self) -> int:
        """Number of markets being tracked."""
        return len(self._markets)

    @property
    def connected_tokens(self) -> int:
        """Number of tokens subscribed to WebSocket."""
        return len(self._subscribed_tokens)

    async def start(self) -> None:
        """Start the market data service."""
        if self._should_run:
            return

        self._should_run = True
        self._start_time = time.time()
        self._log.info("starting_market_data_service")

        # Start WebSocket
        await self._websocket.start()

        # Subscribe to EventBus events
        await self._event_bus.subscribe("system.market.subscribe", self._on_subscribe_request)

        # Start monitoring task
        self._monitor_task = asyncio.create_task(self._monitor_loop())

        # Publish connected event
        await self._event_bus.publish("market.data.connected", {
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        self._log.info("market_data_service_started")

    async def stop(self) -> None:
        """Stop the market data service."""
        self._should_run = False
        self._log.info("stopping_market_data_service")

        # Cancel monitor task
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        # Stop WebSocket
        await self._websocket.stop()

        # Unsubscribe from EventBus
        await self._event_bus.unsubscribe("system.market.subscribe")

        # Publish disconnected event
        await self._event_bus.publish("market.data.disconnected", {
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def health_check(self) -> HealthCheckResult:
        """Check service health."""
        ws_health = await self._websocket.health_check()

        if ws_health.status == HealthStatus.UNHEALTHY:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message=f"WebSocket: {ws_health.message}",
            )

        # Check for stale markets
        stale_count = sum(1 for m in self._markets.values() if self._is_stale(m))
        if stale_count > 0:
            return HealthCheckResult(
                status=HealthStatus.DEGRADED,
                message=f"{stale_count} markets have stale data",
                details={
                    "total_markets": len(self._markets),
                    "stale_markets": stale_count,
                }
            )

        return HealthCheckResult(
            status=HealthStatus.HEALTHY,
            message="All markets receiving data",
            details={
                "total_markets": len(self._markets),
                "subscribed_tokens": len(self._subscribed_tokens),
            }
        )

    async def subscribe_market(
        self,
        market_id: str,
        yes_token_id: str,
        no_token_id: str,
    ) -> None:
        """Subscribe to market data for a market.

        Args:
            market_id: Market's condition ID.
            yes_token_id: YES outcome token ID.
            no_token_id: NO outcome token ID.
        """
        if market_id in self._markets:
            self._log.debug("market_already_subscribed", market_id=market_id)
            return

        # Create market state
        state = MarketState(
            market_id=market_id,
            yes_token_id=str(yes_token_id),
            no_token_id=str(no_token_id),
        )
        self._markets[market_id] = state

        # Map tokens to market
        self._token_to_market[str(yes_token_id)] = market_id
        self._token_to_market[str(no_token_id)] = market_id

        # Subscribe to WebSocket
        tokens = [str(yes_token_id), str(no_token_id)]
        await self._websocket.subscribe(tokens)
        self._subscribed_tokens.update(tokens)

        # Subscribe to EventBus for this market's updates
        await self._event_bus.subscribe(
            f"market.price.{yes_token_id}",
            lambda data: self._on_price_update(yes_token_id, data)
        )
        await self._event_bus.subscribe(
            f"market.price.{no_token_id}",
            lambda data: self._on_price_update(no_token_id, data)
        )
        await self._event_bus.subscribe(
            f"market.book.{yes_token_id}",
            lambda data: self._on_book_update(yes_token_id, data)
        )
        await self._event_bus.subscribe(
            f"market.book.{no_token_id}",
            lambda data: self._on_book_update(no_token_id, data)
        )

        self._log.info(
            "market_subscribed",
            market_id=market_id,
            yes_token=yes_token_id[:16] + "...",
            no_token=no_token_id[:16] + "...",
        )

    async def unsubscribe_market(self, market_id: str) -> None:
        """Unsubscribe from market data.

        Args:
            market_id: Market's condition ID.
        """
        if market_id not in self._markets:
            return

        state = self._markets[market_id]

        # Unsubscribe from WebSocket
        tokens = [state.yes_token_id, state.no_token_id]
        await self._websocket.unsubscribe(tokens)
        self._subscribed_tokens -= set(tokens)

        # Remove token mappings
        del self._token_to_market[state.yes_token_id]
        del self._token_to_market[state.no_token_id]

        # Remove market state
        del self._markets[market_id]

        self._log.info("market_unsubscribed", market_id=market_id)

    def get_order_book(self, market_id: str) -> Optional[OrderBookSnapshot]:
        """Get current order book snapshot for a market.

        Args:
            market_id: Market's condition ID.

        Returns:
            OrderBookSnapshot or None if not available.
        """
        if market_id not in self._markets:
            return None

        return self._markets[market_id].get_snapshot()

    def get_best_prices(self, market_id: str) -> Optional[tuple[Decimal, Decimal, Decimal, Decimal]]:
        """Get best bid/ask for YES and NO.

        Args:
            market_id: Market's condition ID.

        Returns:
            Tuple of (yes_bid, yes_ask, no_bid, no_ask) or None.
        """
        if market_id not in self._markets:
            return None

        state = self._markets[market_id]
        if state.yes_book is None or state.no_book is None:
            return None

        return (
            state.yes_book.best_bid or Decimal("0"),
            state.yes_book.best_ask or Decimal("0"),
            state.no_book.best_bid or Decimal("0"),
            state.no_book.best_ask or Decimal("0"),
        )

    def is_market_stale(self, market_id: str) -> bool:
        """Check if a market's data is stale.

        Args:
            market_id: Market's condition ID.

        Returns:
            True if stale or not subscribed.
        """
        if market_id not in self._markets:
            return True

        return self._is_stale(self._markets[market_id])

    def _is_stale(self, state: MarketState) -> bool:
        """Check if a market state is stale."""
        if state.last_update == 0:
            return True
        age = time.time() - state.last_update
        return age > float(self._stale_threshold)

    async def _on_subscribe_request(self, data: dict) -> None:
        """Handle subscribe request from EventBus."""
        market_id = data.get("market_id")
        yes_token = data.get("yes_token_id")
        no_token = data.get("no_token_id")

        if market_id and yes_token and no_token:
            await self.subscribe_market(market_id, yes_token, no_token)

    async def _on_price_update(self, token_id: str, data: dict) -> None:
        """Handle price update from WebSocket via EventBus."""
        market_id = self._token_to_market.get(token_id)
        if not market_id:
            return

        state = self._markets.get(market_id)
        if not state:
            return

        now = time.time()
        bid = Decimal(data["bid"]) if data.get("bid") else None
        ask = Decimal(data["ask"]) if data.get("ask") else None

        # Update appropriate book
        if token_id == state.yes_token_id:
            if state.yes_book:
                # Update existing book with new prices
                new_bids = state.yes_book.bids
                new_asks = state.yes_book.asks
                if bid is not None:
                    new_bids = (OrderBookLevel(price=bid, size=Decimal("1")),)
                if ask is not None:
                    new_asks = (OrderBookLevel(price=ask, size=Decimal("1")),)
                state.yes_book = OrderBookData(
                    token_id=token_id,
                    timestamp=datetime.now(timezone.utc),
                    bids=new_bids,
                    asks=new_asks,
                )
            state.last_yes_update = now
        else:
            if state.no_book:
                new_bids = state.no_book.bids
                new_asks = state.no_book.asks
                if bid is not None:
                    new_bids = (OrderBookLevel(price=bid, size=Decimal("1")),)
                if ask is not None:
                    new_asks = (OrderBookLevel(price=ask, size=Decimal("1")),)
                state.no_book = OrderBookData(
                    token_id=token_id,
                    timestamp=datetime.now(timezone.utc),
                    bids=new_bids,
                    asks=new_asks,
                )
            state.last_no_update = now

        # Publish snapshot if both sides available
        await self._publish_snapshot(state)

    async def _on_book_update(self, token_id: str, data: dict) -> None:
        """Handle full book update from WebSocket via EventBus."""
        market_id = self._token_to_market.get(token_id)
        if not market_id:
            return

        state = self._markets.get(market_id)
        if not state:
            return

        now = time.time()

        # Parse bids and asks from event data
        bid = Decimal(data["best_bid"]) if data.get("best_bid") else None
        ask = Decimal(data["best_ask"]) if data.get("best_ask") else None

        book = OrderBookData(
            token_id=token_id,
            timestamp=datetime.now(timezone.utc),
            bids=(OrderBookLevel(price=bid, size=Decimal("1")),) if bid else (),
            asks=(OrderBookLevel(price=ask, size=Decimal("1")),) if ask else (),
        )

        # Update appropriate side
        if token_id == state.yes_token_id:
            state.yes_book = book
            state.last_yes_update = now
        else:
            state.no_book = book
            state.last_no_update = now

        # Publish snapshot if both sides available
        await self._publish_snapshot(state)

    async def _publish_snapshot(self, state: MarketState) -> None:
        """Publish order book snapshot to EventBus."""
        snapshot = state.get_snapshot()
        if snapshot is None:
            return

        await self._event_bus.publish(
            f"market.orderbook.{state.market_id}",
            {
                "market_id": state.market_id,
                "timestamp": snapshot.timestamp.isoformat(),
                "yes_bid": str(snapshot.yes_book.best_bid) if snapshot.yes_book.best_bid else None,
                "yes_ask": str(snapshot.yes_book.best_ask) if snapshot.yes_book.best_ask else None,
                "no_bid": str(snapshot.no_book.best_bid) if snapshot.no_book.best_bid else None,
                "no_ask": str(snapshot.no_book.best_ask) if snapshot.no_book.best_ask else None,
                "combined_ask": str(snapshot.combined_ask) if snapshot.combined_ask else None,
                "arbitrage_spread_cents": str(snapshot.arbitrage_spread_cents) if snapshot.arbitrage_spread_cents else None,
            }
        )

    async def _monitor_loop(self) -> None:
        """Monitor for stale markets."""
        while self._should_run:
            await asyncio.sleep(float(self._refresh_interval))

            for market_id, state in list(self._markets.items()):
                if self._is_stale(state):
                    age = time.time() - state.last_update if state.last_update > 0 else float("inf")

                    self._log.warning(
                        "stale_market_detected",
                        market_id=market_id,
                        age_seconds=age,
                    )

                    # Publish stale alert
                    await self._event_bus.publish(
                        f"market.stale.{market_id}",
                        {
                            "market_id": market_id,
                            "age_seconds": age,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )
