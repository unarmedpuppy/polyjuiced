"""Binance price feed adapter.

Provides real-time cryptocurrency prices from Binance via WebSocket.
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Dict, Optional, Set

import structlog
import websockets
from websockets.exceptions import ConnectionClosed

from mercury.core.events import EventBus
from mercury.core.lifecycle import BaseComponent, HealthCheckResult, HealthStatus
from mercury.integrations.price_feeds.base import PriceFeed, PriceUpdate

log = structlog.get_logger()

# Binance WebSocket endpoints
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"
BINANCE_REST_URL = "https://api.binance.com/api/v3"

# Connection parameters
PING_INTERVAL = 30.0
RECONNECT_MIN_WAIT = 1.0
RECONNECT_MAX_WAIT = 60.0


class BinancePriceFeed(BaseComponent):
    """Binance WebSocket price feed.

    Connects to Binance's WebSocket streams for real-time
    cryptocurrency prices. Publishes updates to EventBus.

    Event channels:
    - price.binance.{symbol} - Price updates for symbol
    """

    def __init__(self, event_bus: Optional[EventBus] = None):
        """Initialize the Binance price feed.

        Args:
            event_bus: Optional EventBus for publishing updates.
        """
        super().__init__()
        self._event_bus = event_bus
        self._log = log.bind(component="binance_feed")

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._subscriptions: Dict[str, Set[Callable[[PriceUpdate], None]]] = {}
        self._prices: Dict[str, Decimal] = {}

        self._should_run: bool = False
        self._message_task: Optional[asyncio.Task] = None
        self._last_message_time: float = 0
        self._reconnect_delay: float = RECONNECT_MIN_WAIT

    @property
    def name(self) -> str:
        """Feed identifier."""
        return "binance"

    @property
    def is_connected(self) -> bool:
        """Whether connected to WebSocket."""
        return self._ws is not None and self._ws.open

    async def connect(self) -> None:
        """Establish connection to Binance WebSocket."""
        await self.start()

    async def close(self) -> None:
        """Close connection."""
        await self.stop()

    async def start(self) -> None:
        """Start the price feed."""
        if self._should_run:
            return

        self._should_run = True
        self._start_time = time.time()
        self._log.info("starting_binance_feed")

        self._message_task = asyncio.create_task(self._message_loop())

    async def stop(self) -> None:
        """Stop the price feed."""
        self._should_run = False

        if self._message_task:
            self._message_task.cancel()
            try:
                await self._message_task
            except asyncio.CancelledError:
                pass

        await self._disconnect()

    async def health_check(self) -> HealthCheckResult:
        """Check feed health."""
        if not self._should_run:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message="Feed not running",
            )

        if not self.is_connected:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message="Not connected",
            )

        return HealthCheckResult(
            status=HealthStatus.HEALTHY,
            message="Connected",
            details={
                "subscriptions": len(self._subscriptions),
                "cached_prices": len(self._prices),
            }
        )

    async def get_price(self, symbol: str) -> Optional[Decimal]:
        """Get current price for a symbol.

        Args:
            symbol: Trading symbol (e.g., "BTCUSDT").

        Returns:
            Current price or None if not available.
        """
        symbol_lower = symbol.lower()

        # Return cached price if available
        if symbol_lower in self._prices:
            return self._prices[symbol_lower]

        # Fetch via REST if not subscribed
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{BINANCE_REST_URL}/ticker/price",
                    params={"symbol": symbol.upper()}
                )
                response.raise_for_status()
                data = response.json()
                price = Decimal(str(data["price"]))
                self._prices[symbol_lower] = price
                return price
        except Exception as e:
            self._log.warning("price_fetch_failed", symbol=symbol, error=str(e))
            return None

    async def subscribe(
        self,
        symbol: str,
        callback: Callable[[PriceUpdate], None],
    ) -> None:
        """Subscribe to real-time price updates.

        Args:
            symbol: Trading symbol (e.g., "BTCUSDT").
            callback: Function called on each price update.
        """
        symbol_lower = symbol.lower()

        if symbol_lower not in self._subscriptions:
            self._subscriptions[symbol_lower] = set()

        self._subscriptions[symbol_lower].add(callback)

        # Subscribe via WebSocket if connected
        if self.is_connected:
            await self._send_subscribe([symbol_lower])

        self._log.debug("subscribed", symbol=symbol)

    async def unsubscribe(self, symbol: str) -> None:
        """Unsubscribe from price updates.

        Args:
            symbol: Trading symbol to unsubscribe from.
        """
        symbol_lower = symbol.lower()

        if symbol_lower in self._subscriptions:
            del self._subscriptions[symbol_lower]

            if self.is_connected:
                await self._send_unsubscribe([symbol_lower])

        self._log.debug("unsubscribed", symbol=symbol)

    async def _message_loop(self) -> None:
        """Main message receiving loop."""
        while self._should_run:
            try:
                await self._connect()
                await self._receive_messages()
            except asyncio.CancelledError:
                break
            except ConnectionClosed:
                self._log.warning("connection_closed")
                await self._handle_disconnect()
            except Exception as e:
                self._log.error("ws_error", error=str(e))
                await self._handle_disconnect()

    async def _connect(self) -> None:
        """Establish WebSocket connection."""
        if self.is_connected:
            return

        self._log.info("connecting_to_binance")

        self._ws = await websockets.connect(
            BINANCE_WS_URL,
            ping_interval=PING_INTERVAL,
        )

        self._last_message_time = time.time()
        self._reconnect_delay = RECONNECT_MIN_WAIT

        # Restore subscriptions
        if self._subscriptions:
            await self._send_subscribe(list(self._subscriptions.keys()))

        self._log.info("binance_connected")

    async def _disconnect(self) -> None:
        """Close WebSocket connection."""
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def _handle_disconnect(self) -> None:
        """Handle disconnection with backoff."""
        await self._disconnect()

        if self._should_run:
            self._log.info("reconnecting", delay=self._reconnect_delay)
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(
                self._reconnect_delay * 2,
                RECONNECT_MAX_WAIT
            )

    async def _receive_messages(self) -> None:
        """Receive and process messages."""
        if not self._ws:
            return

        async for raw in self._ws:
            self._last_message_time = time.time()

            try:
                data = json.loads(raw)
                await self._process_message(data)
            except Exception as e:
                self._log.warning("message_error", error=str(e))

    async def _process_message(self, data: dict) -> None:
        """Process a WebSocket message."""
        # Handle trade stream: {symbol}@trade
        if "s" in data and "p" in data:
            symbol = data["s"].lower()
            price = Decimal(str(data["p"]))

            self._prices[symbol] = price

            update = PriceUpdate(
                symbol=symbol.upper(),
                price=price,
                timestamp=datetime.now(timezone.utc),
                source="binance",
            )

            # Call registered callbacks
            if symbol in self._subscriptions:
                for callback in self._subscriptions[symbol]:
                    try:
                        callback(update)
                    except Exception as e:
                        self._log.warning("callback_error", error=str(e))

            # Publish to EventBus
            if self._event_bus:
                await self._event_bus.publish(
                    f"price.binance.{symbol}",
                    {
                        "symbol": symbol.upper(),
                        "price": str(price),
                        "timestamp": update.timestamp.isoformat(),
                    }
                )

    async def _send_subscribe(self, symbols: list[str]) -> None:
        """Send subscription request."""
        if not self._ws or not symbols:
            return

        # Subscribe to trade streams
        streams = [f"{s}@trade" for s in symbols]

        message = {
            "method": "SUBSCRIBE",
            "params": streams,
            "id": int(time.time() * 1000),
        }

        await self._ws.send(json.dumps(message))
        self._log.debug("sent_subscribe", count=len(symbols))

    async def _send_unsubscribe(self, symbols: list[str]) -> None:
        """Send unsubscribe request."""
        if not self._ws or not symbols:
            return

        streams = [f"{s}@trade" for s in symbols]

        message = {
            "method": "UNSUBSCRIBE",
            "params": streams,
            "id": int(time.time() * 1000),
        }

        await self._ws.send(json.dumps(message))
