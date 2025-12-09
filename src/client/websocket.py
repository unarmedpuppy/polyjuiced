"""WebSocket client for real-time Polymarket data streaming."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set

import orjson
import structlog
import websockets
from websockets.exceptions import ConnectionClosed

from ..metrics import WEBSOCKET_CONNECTED, WEBSOCKET_RECONNECTS

log = structlog.get_logger()


@dataclass
class OrderBookUpdate:
    """Represents an order book update from WebSocket."""

    token_id: str
    timestamp: datetime
    bids: List[Dict[str, float]]  # [{"price": 0.50, "size": 100}, ...]
    asks: List[Dict[str, float]]
    best_bid: float
    best_ask: float
    midpoint: float


@dataclass
class PriceUpdate:
    """Represents a price change event."""

    token_id: str
    timestamp: datetime
    price: float
    side: str  # "bid" or "ask"


class PolymarketWebSocket:
    """WebSocket client for real-time Polymarket order book streaming."""

    def __init__(
        self,
        ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 60.0,
    ):
        """Initialize WebSocket client.

        Args:
            ws_url: WebSocket server URL (Polymarket CLOB WebSocket)
            reconnect_delay: Initial delay between reconnection attempts
            max_reconnect_delay: Maximum delay between reconnection attempts
        """
        # Ensure we use the correct Polymarket WebSocket URL
        # The market channel endpoint is: wss://ws-subscriptions-clob.polymarket.com/ws/market
        if "ws-live-data" in ws_url or ws_url.endswith("/ws/"):
            ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        self.ws_url = ws_url
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._connected = False
        self._running = False
        self._subscribed_tokens: Set[str] = set()

        # Callbacks
        self._on_book_update: Optional[Callable[[OrderBookUpdate], None]] = None
        self._on_price_change: Optional[Callable[[PriceUpdate], None]] = None
        self._on_connect: Optional[Callable[[], None]] = None
        self._on_disconnect: Optional[Callable[[], None]] = None

    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is connected."""
        return self._connected and self._ws is not None

    def on_book_update(self, callback: Callable[[OrderBookUpdate], None]) -> None:
        """Register callback for order book updates."""
        self._on_book_update = callback

    def on_price_change(self, callback: Callable[[PriceUpdate], None]) -> None:
        """Register callback for price changes."""
        self._on_price_change = callback

    def on_connect(self, callback: Callable[[], None]) -> None:
        """Register callback for connection events."""
        self._on_connect = callback

    def on_disconnect(self, callback: Callable[[], None]) -> None:
        """Register callback for disconnection events."""
        self._on_disconnect = callback

    async def connect(self) -> bool:
        """Establish WebSocket connection.

        Returns:
            True if connection successful
        """
        try:
            self._ws = await websockets.connect(
                self.ws_url,
                ping_interval=20,
                ping_timeout=10,
            )
            self._connected = True
            WEBSOCKET_CONNECTED.set(1)
            log.info("WebSocket connected", url=self.ws_url)

            if self._on_connect:
                self._on_connect()

            return True

        except Exception as e:
            log.error("WebSocket connection failed", error=str(e))
            self._connected = False
            WEBSOCKET_CONNECTED.set(0)
            return False

    async def disconnect(self) -> None:
        """Close WebSocket connection."""
        self._running = False
        self._connected = False
        WEBSOCKET_CONNECTED.set(0)

        if self._ws:
            await self._ws.close()
            self._ws = None

        log.info("WebSocket disconnected")

        if self._on_disconnect:
            self._on_disconnect()

    async def subscribe(self, token_ids: List[str]) -> None:
        """Subscribe to order book updates for specific tokens.

        Uses the Polymarket market channel format:
        {"assets_ids": ["token1", "token2"], "type": "market"}

        Args:
            token_ids: List of token IDs to subscribe to
        """
        if not self.is_connected:
            raise RuntimeError("WebSocket not connected")

        # Filter out already subscribed tokens
        new_tokens = [t for t in token_ids if t not in self._subscribed_tokens]
        if not new_tokens:
            return

        # Subscribe to market channel with all tokens at once
        # Polymarket expects: {"assets_ids": [...], "type": "market"}
        subscribe_msg = {
            "type": "market",
            "assets_ids": new_tokens,
        }
        await self._ws.send(orjson.dumps(subscribe_msg).decode())

        for token_id in new_tokens:
            self._subscribed_tokens.add(token_id)
            log.debug("Subscribed to token", token_id=token_id)

        log.info("Subscribed to market channel", token_count=len(new_tokens))

    async def unsubscribe(self, token_ids: List[str]) -> None:
        """Unsubscribe from token updates.

        Args:
            token_ids: List of token IDs to unsubscribe from
        """
        if not self.is_connected:
            return

        for token_id in token_ids:
            if token_id in self._subscribed_tokens:
                unsubscribe_msg = {
                    "type": "unsubscribe",
                    "channel": "market",
                    "assets_id": token_id,
                }
                await self._ws.send(orjson.dumps(unsubscribe_msg).decode())
                self._subscribed_tokens.discard(token_id)
                log.debug("Unsubscribed from token", token_id=token_id)

    async def run(self) -> None:
        """Main event loop - connect and process messages with auto-reconnect."""
        self._running = True
        current_delay = self.reconnect_delay

        while self._running:
            try:
                if not self.is_connected:
                    WEBSOCKET_RECONNECTS.inc()
                    connected = await self.connect()
                    if not connected:
                        log.warning(
                            "Reconnecting in seconds",
                            delay=current_delay,
                        )
                        await asyncio.sleep(current_delay)
                        current_delay = min(
                            current_delay * 2,
                            self.max_reconnect_delay,
                        )
                        continue

                    # Re-subscribe to all tokens after reconnect
                    tokens_to_resubscribe = list(self._subscribed_tokens)
                    self._subscribed_tokens.clear()
                    if tokens_to_resubscribe:
                        await self.subscribe(tokens_to_resubscribe)

                    # Reset delay on successful connection
                    current_delay = self.reconnect_delay

                # Process incoming messages
                await self._process_messages()

            except ConnectionClosed:
                log.warning("WebSocket connection closed")
                self._connected = False
                if self._on_disconnect:
                    self._on_disconnect()

            except Exception as e:
                log.error("WebSocket error", error=str(e))
                self._connected = False

    async def _process_messages(self) -> None:
        """Process incoming WebSocket messages."""
        async for message in self._ws:
            try:
                # Handle PONG/PING text messages (not JSON)
                if message in ("PONG", "PING"):
                    continue

                data = orjson.loads(message)

                # Polymarket sends arrays of events for batch updates
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            await self._handle_message(item)
                else:
                    await self._handle_message(data)
            except orjson.JSONDecodeError:
                log.warning("Invalid JSON received", message=message[:100])
            except Exception as e:
                log.error("Error processing message", error=str(e))

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """Handle a parsed WebSocket message.

        Polymarket uses "event_type" field for message types:
        - "book": Order book snapshot
        - "price_change": Price level changes
        - "last_trade_price": Trade execution

        Args:
            data: Parsed JSON message
        """
        msg_type = data.get("event_type", data.get("type"))

        if msg_type == "book":
            await self._handle_book_update(data)
        elif msg_type == "price_change":
            await self._handle_price_change(data)
        elif msg_type == "last_trade_price":
            log.debug("Trade executed", data=data)
        elif msg_type == "tick_size_change":
            log.debug("Tick size changed", data=data)
        elif msg_type == "subscribed" or msg_type == "connected":
            log.info("WebSocket subscription confirmed", data=data)
        elif msg_type == "error":
            log.error("WebSocket error message", data=data)
        elif msg_type is None:
            # Log raw message for debugging unknown formats
            log.debug("Message without type", keys=list(data.keys())[:5])
        else:
            log.debug("Unknown message type", type=msg_type)

    async def _handle_book_update(self, data: Dict[str, Any]) -> None:
        """Handle order book update message."""
        if not self._on_book_update:
            return

        try:
            token_id = data.get("asset_id", data.get("token_id"))
            bids = data.get("bids", [])
            asks = data.get("asks", [])

            # Parse bids and asks
            parsed_bids = [
                {"price": float(b.get("price", 0)), "size": float(b.get("size", 0))}
                for b in bids
            ]
            parsed_asks = [
                {"price": float(a.get("price", 0)), "size": float(a.get("size", 0))}
                for a in asks
            ]

            # Calculate best bid/ask and midpoint
            best_bid = max((b["price"] for b in parsed_bids), default=0.0)
            best_ask = min((a["price"] for a in parsed_asks), default=1.0)
            midpoint = (best_bid + best_ask) / 2 if best_bid and best_ask else 0.5

            update = OrderBookUpdate(
                token_id=token_id,
                timestamp=datetime.utcnow(),
                bids=parsed_bids,
                asks=parsed_asks,
                best_bid=best_bid,
                best_ask=best_ask,
                midpoint=midpoint,
            )

            self._on_book_update(update)

        except Exception as e:
            log.error("Error parsing book update", error=str(e))

    async def _handle_price_change(self, data: Dict[str, Any]) -> None:
        """Handle price change message."""
        if not self._on_price_change:
            return

        try:
            update = PriceUpdate(
                token_id=data.get("asset_id", data.get("token_id")),
                timestamp=datetime.utcnow(),
                price=float(data.get("price", 0)),
                side=data.get("side", "unknown"),
            )

            self._on_price_change(update)

        except Exception as e:
            log.error("Error parsing price change", error=str(e))


class MarketPriceStream:
    """High-level interface for streaming YES/NO prices for a market."""

    def __init__(self, ws_client: PolymarketWebSocket):
        """Initialize price stream.

        Args:
            ws_client: WebSocket client instance
        """
        self.ws = ws_client
        self._yes_price: float = 0.5
        self._no_price: float = 0.5
        self._yes_token_id: Optional[str] = None
        self._no_token_id: Optional[str] = None
        self._on_spread_change: Optional[Callable[[float, float, float], None]] = None

    @property
    def yes_price(self) -> float:
        """Current YES price."""
        return self._yes_price

    @property
    def no_price(self) -> float:
        """Current NO price."""
        return self._no_price

    @property
    def spread(self) -> float:
        """Current arbitrage spread (1.0 - YES - NO)."""
        return 1.0 - self._yes_price - self._no_price

    def on_spread_change(
        self,
        callback: Callable[[float, float, float], None],
    ) -> None:
        """Register callback for spread changes.

        Callback receives (yes_price, no_price, spread).
        """
        self._on_spread_change = callback

    async def start(
        self,
        yes_token_id: str,
        no_token_id: str,
    ) -> None:
        """Start streaming prices for a market.

        Args:
            yes_token_id: YES token ID
            no_token_id: NO token ID
        """
        self._yes_token_id = yes_token_id
        self._no_token_id = no_token_id

        # Register book update handler
        self.ws.on_book_update(self._handle_book_update)

        # Subscribe to both tokens
        await self.ws.subscribe([yes_token_id, no_token_id])

    def _handle_book_update(self, update: OrderBookUpdate) -> None:
        """Handle order book update and emit spread changes."""
        old_spread = self.spread

        if update.token_id == self._yes_token_id:
            self._yes_price = update.best_ask  # Cost to buy YES
        elif update.token_id == self._no_token_id:
            self._no_price = update.best_ask  # Cost to buy NO

        new_spread = self.spread

        # Emit if spread changed significantly (> 0.1 cent)
        if abs(new_spread - old_spread) > 0.001 and self._on_spread_change:
            self._on_spread_change(self._yes_price, self._no_price, new_spread)
