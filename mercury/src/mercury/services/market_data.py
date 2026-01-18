"""Market Data Service - manages market data streams and order book state.

This service:
- Subscribes to WebSocket market data
- Maintains current order book state for each market using efficient in-memory structures
- Handles incremental updates and full snapshots from WebSocket
- Detects stale/missing data
- Publishes order book snapshots to EventBus

The service uses InMemoryOrderBook and MarketOrderBook from the domain layer
for efficient order book state management with O(log n) updates and O(1) best
price lookups.
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Dict, Optional, Set

import structlog

from mercury.core.config import ConfigManager
from mercury.core.events import EventBus
from mercury.core.lifecycle import BaseComponent, HealthCheckResult, HealthStatus
from mercury.domain.market import OrderBook, OrderBookLevel
from mercury.domain.orderbook import InMemoryOrderBook, MarketOrderBook
from mercury.integrations.polymarket.types import (
    OrderBookData,
    OrderBookLevel as PolymarketOrderBookLevel,
    OrderBookSnapshot,
    PolymarketSettings,
)
from mercury.integrations.polymarket.websocket import PolymarketWebSocket

if TYPE_CHECKING:
    from mercury.integrations.polymarket.gamma import GammaClient

log = structlog.get_logger()

# Default configuration values
DEFAULT_STALE_THRESHOLD_SECONDS = 30.0
DEFAULT_REFRESH_INTERVAL_SECONDS = 5.0
DEFAULT_MAX_MARKETS = 100
DEFAULT_ORDER_BOOK_DEPTH = 10


@dataclass
class MarketState:
    """Internal state for a tracked market.

    Uses the new InMemoryOrderBook structure for efficient order book management.
    Maintains both the new in-memory books and legacy OrderBookData for compatibility.
    """

    market_id: str
    yes_token_id: str
    no_token_id: str

    # New efficient order book state
    market_book: Optional[MarketOrderBook] = None

    # Legacy compatibility fields
    yes_book: Optional[OrderBookData] = None
    no_book: Optional[OrderBookData] = None

    last_yes_update: float = 0
    last_no_update: float = 0

    def __post_init__(self) -> None:
        """Initialize the market order book."""
        if self.market_book is None:
            self.market_book = MarketOrderBook.create(
                market_id=self.market_id,
                yes_token_id=self.yes_token_id,
                no_token_id=self.no_token_id,
            )

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
        gamma_client: Optional["GammaClient"] = None,
    ):
        """Initialize the market data service.

        Args:
            config: Configuration manager.
            event_bus: EventBus for publishing updates.
            websocket: Optional pre-configured WebSocket client.
            gamma_client: Optional GammaClient for market token resolution.
        """
        super().__init__()
        self._config = config
        self._event_bus = event_bus
        self._gamma_client = gamma_client
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

        # Market state (new-style using MarketState)
        self._markets: Dict[str, MarketState] = {}
        self._token_to_market: Dict[str, str] = {}  # token_id -> market_id
        self._subscribed_tokens: Set[str] = set()

        # Order book state (for compatibility with smoke tests)
        self._order_books: Dict[str, OrderBook] = {}
        self._last_update: Dict[str, float] = {}

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

    @property
    def subscribed_markets(self) -> Set[str]:
        """Set of market IDs currently subscribed."""
        return set(self._markets.keys())

    async def start(self) -> None:
        """Start the market data service."""
        if self._should_run:
            return

        self._should_run = True
        self._running = True  # Set BaseComponent running flag
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
        self._running = False  # Set BaseComponent running flag
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
        yes_token_id: Optional[str] = None,
        no_token_id: Optional[str] = None,
    ) -> None:
        """Subscribe to market data for a market.

        Args:
            market_id: Market's condition ID.
            yes_token_id: Optional YES outcome token ID (resolved via GammaClient if not provided).
            no_token_id: Optional NO outcome token ID (resolved via GammaClient if not provided).
        """
        if market_id in self._markets:
            self._log.debug("market_already_subscribed", market_id=market_id)
            return

        # Resolve token IDs if not provided
        if yes_token_id is None or no_token_id is None:
            if self._gamma_client is not None:
                try:
                    market_info = await self._gamma_client.get_market_info(market_id)
                    if market_info:
                        yes_token_id = market_info.yes_token_id
                        no_token_id = market_info.no_token_id
                except Exception as e:
                    self._log.warning(
                        "failed_to_resolve_tokens",
                        market_id=market_id,
                        error=str(e)
                    )

            # If still no tokens, use market_id as placeholder (for testing)
            if yes_token_id is None:
                yes_token_id = f"{market_id}_yes"
            if no_token_id is None:
                no_token_id = f"{market_id}_no"

        # Create market state
        state = MarketState(
            market_id=market_id,
            yes_token_id=str(yes_token_id),
            no_token_id=str(no_token_id),
        )
        self._markets[market_id] = state

        # Initialize order book and last update for compatibility
        self._order_books[market_id] = OrderBook(market_id=market_id)
        self._last_update[market_id] = 0

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
            lambda data, tid=yes_token_id: self._on_price_update(tid, data)
        )
        await self._event_bus.subscribe(
            f"market.price.{no_token_id}",
            lambda data, tid=no_token_id: self._on_price_update(tid, data)
        )
        await self._event_bus.subscribe(
            f"market.book.{yes_token_id}",
            lambda data, tid=yes_token_id: self._on_book_update(tid, data)
        )
        await self._event_bus.subscribe(
            f"market.book.{no_token_id}",
            lambda data, tid=no_token_id: self._on_book_update(tid, data)
        )

        self._log.info(
            "market_subscribed",
            market_id=market_id,
            yes_token=yes_token_id[:16] + "..." if len(str(yes_token_id)) > 16 else yes_token_id,
            no_token=no_token_id[:16] + "..." if len(str(no_token_id)) > 16 else no_token_id,
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

        # Remove order book and last update
        if market_id in self._order_books:
            del self._order_books[market_id]
        if market_id in self._last_update:
            del self._last_update[market_id]

        self._log.info("market_unsubscribed", market_id=market_id)

    def get_order_book(self, market_id: str) -> Optional[OrderBook]:
        """Get current order book for a market.

        Args:
            market_id: Market's condition ID.

        Returns:
            OrderBook or None if not available.
        """
        return self._order_books.get(market_id)

    def get_market_order_book(self, market_id: str) -> Optional[MarketOrderBook]:
        """Get the efficient in-memory market order book.

        This is the preferred method for accessing order book state as it
        provides O(1) best price lookups and O(log n) updates.

        Args:
            market_id: Market's condition ID.

        Returns:
            MarketOrderBook or None if market not subscribed.
        """
        state = self._markets.get(market_id)
        if state is None or state.market_book is None:
            return None
        return state.market_book

    def get_yes_order_book(self, market_id: str) -> Optional[InMemoryOrderBook]:
        """Get the YES token order book.

        Args:
            market_id: Market's condition ID.

        Returns:
            InMemoryOrderBook for YES token or None.
        """
        market_book = self.get_market_order_book(market_id)
        return market_book.yes_book if market_book else None

    def get_no_order_book(self, market_id: str) -> Optional[InMemoryOrderBook]:
        """Get the NO token order book.

        Args:
            market_id: Market's condition ID.

        Returns:
            InMemoryOrderBook for NO token or None.
        """
        market_book = self.get_market_order_book(market_id)
        return market_book.no_book if market_book else None

    def get_best_prices(self, market_id: str) -> Optional[tuple[Decimal, Decimal]]:
        """Get best bid/ask for YES side.

        Args:
            market_id: Market's condition ID.

        Returns:
            Tuple of (yes_bid, yes_ask) or None.
        """
        # Use the efficient in-memory order book if available
        market_book = self.get_market_order_book(market_id)
        if market_book is not None:
            yes_bid = market_book.yes_best_bid
            yes_ask = market_book.yes_best_ask
            if yes_bid is not None and yes_ask is not None:
                return (yes_bid, yes_ask)
            return None

        # Fall back to legacy order book
        book = self._order_books.get(market_id)
        if book is None:
            return None

        yes_bid = book.yes_best_bid
        yes_ask = book.yes_best_ask

        if yes_bid is None or yes_ask is None:
            return None

        return (yes_bid, yes_ask)

    def get_depth(
        self,
        market_id: str,
        levels: int = DEFAULT_ORDER_BOOK_DEPTH,
    ) -> Optional[dict]:
        """Get order book depth for a market.

        Args:
            market_id: Market's condition ID.
            levels: Number of price levels to return.

        Returns:
            Dictionary with depth information or None if not available.
        """
        market_book = self.get_market_order_book(market_id)
        if market_book is None:
            return None

        return {
            "market_id": market_id,
            "yes_bids": [
                {"price": str(l.price), "size": str(l.size)}
                for l in market_book.yes_book.bid_depth(levels)
            ],
            "yes_asks": [
                {"price": str(l.price), "size": str(l.size)}
                for l in market_book.yes_book.ask_depth(levels)
            ],
            "no_bids": [
                {"price": str(l.price), "size": str(l.size)}
                for l in market_book.no_book.bid_depth(levels)
            ],
            "no_asks": [
                {"price": str(l.price), "size": str(l.size)}
                for l in market_book.no_book.ask_depth(levels)
            ],
            "yes_total_bid_size": str(market_book.yes_book.total_bid_size(levels)),
            "yes_total_ask_size": str(market_book.yes_book.total_ask_size(levels)),
            "no_total_bid_size": str(market_book.no_book.total_bid_size(levels)),
            "no_total_ask_size": str(market_book.no_book.total_ask_size(levels)),
        }

    def get_arbitrage_info(self, market_id: str) -> Optional[dict]:
        """Get arbitrage information for a market.

        Args:
            market_id: Market's condition ID.

        Returns:
            Dictionary with arbitrage info or None.
        """
        market_book = self.get_market_order_book(market_id)
        if market_book is None:
            return None

        return {
            "market_id": market_id,
            "yes_best_ask": str(market_book.yes_best_ask) if market_book.yes_best_ask else None,
            "no_best_ask": str(market_book.no_best_ask) if market_book.no_best_ask else None,
            "combined_ask": str(market_book.combined_ask) if market_book.combined_ask else None,
            "arbitrage_spread": str(market_book.arbitrage_spread) if market_book.arbitrage_spread else None,
            "arbitrage_spread_cents": str(market_book.arbitrage_spread_cents) if market_book.arbitrage_spread_cents else None,
            "has_arbitrage": market_book.has_arbitrage,
        }

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

    async def _check_staleness(self) -> None:
        """Check all markets for stale data and publish alerts."""
        # Check all markets in _last_update for staleness
        for market_id, last_update_time in list(self._last_update.items()):
            # Skip if this market doesn't exist in _markets (unless testing)
            # Use very old timestamp (0) as stale by definition
            if last_update_time == 0:
                age = float("inf")
            else:
                age = time.time() - last_update_time

            if age > float(self._stale_threshold):
                self._log.warning(
                    "stale_market_detected",
                    market_id=market_id,
                    age_seconds=age if age != float("inf") else -1,
                )

                # Publish stale alert
                await self._event_bus.publish(
                    f"market.stale.{market_id}",
                    {
                        "market_id": market_id,
                        "age_seconds": age if age != float("inf") else -1,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )

    async def _on_subscribe_request(self, data: dict) -> None:
        """Handle subscribe request from EventBus."""
        market_id = data.get("market_id")
        yes_token = data.get("yes_token_id")
        no_token = data.get("no_token_id")

        if market_id:
            await self.subscribe_market(market_id, yes_token, no_token)

    async def _on_price_update(self, token_id: str, data: dict) -> None:
        """Handle price update from WebSocket via EventBus.

        Updates both the legacy OrderBookData and the new InMemoryOrderBook.
        Price updates typically only include best bid/ask, so we update
        only those levels with a default size of 1.
        """
        market_id = self._token_to_market.get(token_id)
        if not market_id:
            return

        state = self._markets.get(market_id)
        if not state:
            return

        now = time.time()
        bid = Decimal(data["bid"]) if data.get("bid") else None
        ask = Decimal(data["ask"]) if data.get("ask") else None

        # Default size for price-only updates
        default_size = Decimal("1")

        # Update the new InMemoryOrderBook (incremental update)
        if state.market_book is not None:
            if token_id == state.yes_token_id:
                if bid is not None:
                    state.market_book.yes_book.update_bid(bid, default_size)
                if ask is not None:
                    state.market_book.yes_book.update_ask(ask, default_size)
            else:
                if bid is not None:
                    state.market_book.no_book.update_bid(bid, default_size)
                if ask is not None:
                    state.market_book.no_book.update_ask(ask, default_size)

        # Update legacy OrderBookData (for compatibility)
        if token_id == state.yes_token_id:
            if state.yes_book:
                # Update existing book with new prices
                new_bids = state.yes_book.bids
                new_asks = state.yes_book.asks
                if bid is not None:
                    new_bids = (PolymarketOrderBookLevel(price=bid, size=default_size),)
                if ask is not None:
                    new_asks = (PolymarketOrderBookLevel(price=ask, size=default_size),)
                state.yes_book = OrderBookData(
                    token_id=token_id,
                    timestamp=datetime.now(timezone.utc),
                    bids=new_bids,
                    asks=new_asks,
                )
            else:
                # Initialize book if it doesn't exist
                state.yes_book = OrderBookData(
                    token_id=token_id,
                    timestamp=datetime.now(timezone.utc),
                    bids=(PolymarketOrderBookLevel(price=bid, size=default_size),) if bid else (),
                    asks=(PolymarketOrderBookLevel(price=ask, size=default_size),) if ask else (),
                )
            state.last_yes_update = now
        else:
            if state.no_book:
                new_bids = state.no_book.bids
                new_asks = state.no_book.asks
                if bid is not None:
                    new_bids = (PolymarketOrderBookLevel(price=bid, size=default_size),)
                if ask is not None:
                    new_asks = (PolymarketOrderBookLevel(price=ask, size=default_size),)
                state.no_book = OrderBookData(
                    token_id=token_id,
                    timestamp=datetime.now(timezone.utc),
                    bids=new_bids,
                    asks=new_asks,
                )
            else:
                # Initialize book if it doesn't exist
                state.no_book = OrderBookData(
                    token_id=token_id,
                    timestamp=datetime.now(timezone.utc),
                    bids=(PolymarketOrderBookLevel(price=bid, size=default_size),) if bid else (),
                    asks=(PolymarketOrderBookLevel(price=ask, size=default_size),) if ask else (),
                )
            state.last_no_update = now

        # Update the _last_update dict
        self._last_update[market_id] = now

        # Update the domain OrderBook
        self._update_order_book(market_id, state)

        # Publish snapshot if both sides available
        await self._publish_snapshot(state)

    async def _on_book_update(self, token_id: str, data: dict) -> None:
        """Handle full book update from WebSocket via EventBus.

        Full book updates can include multiple price levels. This method
        applies the update as a snapshot, replacing all levels for the token.
        """
        market_id = self._token_to_market.get(token_id)
        if not market_id:
            return

        state = self._markets.get(market_id)
        if not state:
            return

        now = time.time()

        # Parse bids and asks from event data
        # Handle both simple best_bid/best_ask format and full depth format
        bids_data = data.get("bids", [])
        asks_data = data.get("asks", [])

        # If no full depth, check for best bid/ask
        bid = Decimal(data["best_bid"]) if data.get("best_bid") else None
        ask = Decimal(data["best_ask"]) if data.get("best_ask") else None

        # Parse full depth levels if available
        parsed_bids: list[tuple[Decimal, Decimal]] = []
        parsed_asks: list[tuple[Decimal, Decimal]] = []

        for level in bids_data:
            if isinstance(level, dict):
                price = Decimal(str(level.get("price", 0)))
                size = Decimal(str(level.get("size", 0)))
            elif isinstance(level, (list, tuple)) and len(level) >= 2:
                price = Decimal(str(level[0]))
                size = Decimal(str(level[1]))
            else:
                continue
            if size > 0:
                parsed_bids.append((price, size))

        for level in asks_data:
            if isinstance(level, dict):
                price = Decimal(str(level.get("price", 0)))
                size = Decimal(str(level.get("size", 0)))
            elif isinstance(level, (list, tuple)) and len(level) >= 2:
                price = Decimal(str(level[0]))
                size = Decimal(str(level[1]))
            else:
                continue
            if size > 0:
                parsed_asks.append((price, size))

        # If no full depth, use best bid/ask with default size
        default_size = Decimal("1")
        if not parsed_bids and bid is not None:
            parsed_bids = [(bid, default_size)]
        if not parsed_asks and ask is not None:
            parsed_asks = [(ask, default_size)]

        # Update the new InMemoryOrderBook with full snapshot
        if state.market_book is not None:
            if token_id == state.yes_token_id:
                state.market_book.yes_book.apply_snapshot(parsed_bids, parsed_asks)
            else:
                state.market_book.no_book.apply_snapshot(parsed_bids, parsed_asks)

        # Build legacy OrderBookData
        legacy_bids = tuple(
            PolymarketOrderBookLevel(price=p, size=s) for p, s in parsed_bids
        )
        legacy_asks = tuple(
            PolymarketOrderBookLevel(price=p, size=s) for p, s in parsed_asks
        )

        book = OrderBookData(
            token_id=token_id,
            timestamp=datetime.now(timezone.utc),
            bids=legacy_bids,
            asks=legacy_asks,
        )

        # Update appropriate side
        if token_id == state.yes_token_id:
            state.yes_book = book
            state.last_yes_update = now
        else:
            state.no_book = book
            state.last_no_update = now

        # Update the _last_update dict
        self._last_update[market_id] = now

        # Update the domain OrderBook
        self._update_order_book(market_id, state)

        # Publish snapshot if both sides available
        await self._publish_snapshot(state)

    def _update_order_book(self, market_id: str, state: MarketState) -> None:
        """Update the domain OrderBook from market state."""
        yes_bids: list[OrderBookLevel] = []
        yes_asks: list[OrderBookLevel] = []
        no_bids: list[OrderBookLevel] = []
        no_asks: list[OrderBookLevel] = []

        if state.yes_book:
            yes_bids = [
                OrderBookLevel(price=level.price, size=level.size)
                for level in state.yes_book.bids
            ]
            yes_asks = [
                OrderBookLevel(price=level.price, size=level.size)
                for level in state.yes_book.asks
            ]

        if state.no_book:
            no_bids = [
                OrderBookLevel(price=level.price, size=level.size)
                for level in state.no_book.bids
            ]
            no_asks = [
                OrderBookLevel(price=level.price, size=level.size)
                for level in state.no_book.asks
            ]

        self._order_books[market_id] = OrderBook(
            market_id=market_id,
            yes_bids=yes_bids,
            yes_asks=yes_asks,
            no_bids=no_bids,
            no_asks=no_asks,
            timestamp=datetime.now(timezone.utc),
        )

    async def _publish_order_book(self, book: OrderBook) -> None:
        """Publish order book update to EventBus.

        Args:
            book: The OrderBook to publish.
        """
        await self._event_bus.publish(
            f"market.orderbook.{book.market_id}",
            {
                "market_id": book.market_id,
                "timestamp": book.timestamp.isoformat() if book.timestamp else datetime.now(timezone.utc).isoformat(),
                "yes_bid": str(book.yes_best_bid) if book.yes_best_bid else None,
                "yes_ask": str(book.yes_best_ask) if book.yes_best_ask else None,
                "no_bid": str(book.no_best_bid) if book.no_best_bid else None,
                "no_ask": str(book.no_best_ask) if book.no_best_ask else None,
                "combined_ask": str(book.combined_ask) if book.combined_ask else None,
            }
        )

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
            await self._check_staleness()
