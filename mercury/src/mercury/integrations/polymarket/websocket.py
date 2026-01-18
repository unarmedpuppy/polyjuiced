"""Polymarket WebSocket client for real-time market data.

This client connects to Polymarket's WebSocket API and publishes
order book updates via the EventBus for consumption by other services.
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Set

import structlog
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from mercury.core.events import EventBus
from mercury.core.lifecycle import BaseComponent, HealthCheckResult, HealthStatus
from mercury.integrations.polymarket.types import (
    OrderBookData,
    OrderBookLevel,
    PolymarketSettings,
    TokenPrice,
    WebSocketMessage,
)

log = structlog.get_logger()

# Connection parameters
PING_INTERVAL = 20.0  # Send ping every 20 seconds
PONG_TIMEOUT = 10.0   # Wait 10 seconds for pong
RECONNECT_MIN_WAIT = 1.0
RECONNECT_MAX_WAIT = 60.0
STALE_THRESHOLD = 60.0  # Consider connection stale if no message for 60s


class PolymarketWebSocketError(Exception):
    """Error from WebSocket client."""

    pass


class PolymarketWebSocket(BaseComponent):
    """WebSocket client for real-time Polymarket market data.

    This component:
    - Connects to Polymarket's WebSocket API
    - Subscribes to market data for specified tokens
    - Publishes order book updates to EventBus channels
    - Handles reconnection on connection loss
    - Monitors connection health

    Event channels published:
    - market.price.{token_id} - Price updates (TokenPrice)
    - market.book.{token_id} - Full book updates (OrderBookData)
    - market.ws.connected - Connection established
    - market.ws.disconnected - Connection lost
    """

    def __init__(
        self,
        settings: PolymarketSettings,
        event_bus: EventBus,
    ):
        """Initialize the WebSocket client.

        Args:
            settings: Polymarket connection settings.
            event_bus: EventBus for publishing updates.
        """
        super().__init__()
        self._ws_url = settings.ws_url
        self._event_bus = event_bus
        self._log = log.bind(component="polymarket_ws")

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._subscriptions: Set[str] = set()
        self._pending_subscriptions: Set[str] = set()

        self._last_message_time: float = 0
        self._reconnect_delay: float = RECONNECT_MIN_WAIT
        self._should_run: bool = False
        self._message_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None

        # Stats
        self._messages_received: int = 0
        self._reconnect_count: int = 0

    @property
    def is_connected(self) -> bool:
        """Whether currently connected to WebSocket."""
        return self._ws is not None and self._ws.open

    async def start(self) -> None:
        """Start the WebSocket client and begin receiving messages."""
        if self._should_run:
            return

        self._should_run = True
        self._start_time = time.time()
        self._log.info("starting_websocket_client", url=self._ws_url)

        # Start message loop and health check
        self._message_task = asyncio.create_task(self._message_loop())
        self._health_task = asyncio.create_task(self._health_check_loop())

    async def stop(self) -> None:
        """Stop the WebSocket client gracefully."""
        self._should_run = False
        self._log.info("stopping_websocket_client")

        # Cancel tasks
        if self._message_task:
            self._message_task.cancel()
            try:
                await self._message_task
            except asyncio.CancelledError:
                pass

        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        # Close connection
        await self._disconnect()

    async def health_check(self) -> HealthCheckResult:
        """Check WebSocket connection health."""
        if not self._should_run:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message="Client not running",
            )

        if not self.is_connected:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message="Not connected to WebSocket",
            )

        # Check for stale connection
        if self._last_message_time > 0:
            staleness = time.time() - self._last_message_time
            if staleness > STALE_THRESHOLD:
                return HealthCheckResult(
                    status=HealthStatus.DEGRADED,
                    message=f"No messages for {staleness:.1f}s",
                    details={
                        "last_message_ago": staleness,
                        "subscriptions": len(self._subscriptions),
                    }
                )

        return HealthCheckResult(
            status=HealthStatus.HEALTHY,
            message="Connected and receiving",
            details={
                "subscriptions": len(self._subscriptions),
                "messages_received": self._messages_received,
                "reconnects": self._reconnect_count,
            }
        )

    async def subscribe(self, token_ids: list[str]) -> None:
        """Subscribe to market data for tokens.

        Args:
            token_ids: List of token IDs to subscribe to.
        """
        new_tokens = set(str(t) for t in token_ids) - self._subscriptions
        if not new_tokens:
            return

        self._pending_subscriptions.update(new_tokens)

        if self.is_connected:
            await self._send_subscribe(list(new_tokens))

    async def unsubscribe(self, token_ids: list[str]) -> None:
        """Unsubscribe from market data for tokens.

        Args:
            token_ids: List of token IDs to unsubscribe from.
        """
        tokens_to_remove = set(str(t) for t in token_ids) & self._subscriptions

        if tokens_to_remove and self.is_connected:
            await self._send_unsubscribe(list(tokens_to_remove))

        self._subscriptions -= tokens_to_remove
        self._pending_subscriptions -= tokens_to_remove

    async def _message_loop(self) -> None:
        """Main message receiving loop with auto-reconnect."""
        while self._should_run:
            try:
                await self._connect()
                await self._receive_messages()
            except asyncio.CancelledError:
                break
            except ConnectionClosed as e:
                self._log.warning("connection_closed", code=e.code, reason=e.reason)
                await self._handle_disconnect()
            except WebSocketException as e:
                self._log.warning("websocket_error", error=str(e))
                await self._handle_disconnect()
            except Exception as e:
                self._log.error("unexpected_error", error=str(e))
                await self._handle_disconnect()

    async def _connect(self) -> None:
        """Establish WebSocket connection."""
        if self.is_connected:
            return

        self._log.info("connecting_to_websocket", url=self._ws_url)

        self._ws = await websockets.connect(
            self._ws_url,
            ping_interval=PING_INTERVAL,
            ping_timeout=PONG_TIMEOUT,
            close_timeout=5.0,
        )

        self._last_message_time = time.time()
        self._reconnect_delay = RECONNECT_MIN_WAIT
        self._log.info("websocket_connected")

        # Publish connection event
        await self._event_bus.publish("market.ws.connected", {
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        # Restore subscriptions
        all_subs = self._subscriptions | self._pending_subscriptions
        if all_subs:
            await self._send_subscribe(list(all_subs))

    async def _disconnect(self) -> None:
        """Close WebSocket connection."""
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def _handle_disconnect(self) -> None:
        """Handle disconnection with exponential backoff."""
        await self._disconnect()
        self._reconnect_count += 1

        # Publish disconnection event
        await self._event_bus.publish("market.ws.disconnected", {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reconnect_count": self._reconnect_count,
        })

        if self._should_run:
            self._log.info("reconnecting", delay=self._reconnect_delay)
            await asyncio.sleep(self._reconnect_delay)

            # Exponential backoff
            self._reconnect_delay = min(
                self._reconnect_delay * 2,
                RECONNECT_MAX_WAIT
            )

    async def _receive_messages(self) -> None:
        """Receive and process messages from WebSocket."""
        if self._ws is None:
            return

        async for raw_message in self._ws:
            self._last_message_time = time.time()
            self._messages_received += 1

            try:
                await self._process_message(raw_message)
            except Exception as e:
                self._log.warning("message_processing_error", error=str(e))

    async def _process_message(self, raw: str) -> None:
        """Process a raw WebSocket message.

        Polymarket sends various message formats:
        - {"price_changes": [...]} - Price updates
        - {"bids": [...], "asks": [...]} - Full book
        - {"event_type": "..."} - Typed events
        - [...] - Batch of messages
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Handle batch messages
        if isinstance(data, list):
            for item in data:
                await self._process_single_message(item)
        else:
            await self._process_single_message(data)

    async def _process_single_message(self, data: dict) -> None:
        """Process a single parsed message."""
        # Format 1: Price changes
        if "price_changes" in data:
            for change in data["price_changes"]:
                await self._handle_price_change(change)
            return

        # Format 2: Full book snapshot
        if "bids" in data and "asks" in data:
            await self._handle_book_snapshot(data)
            return

        # Format 3: Explicit event type
        event_type = data.get("event_type")
        if event_type == "price_change":
            await self._handle_price_change(data)
        elif event_type == "book":
            await self._handle_book_snapshot(data)

    async def _handle_price_change(self, data: dict) -> None:
        """Handle a price change message."""
        token_id = str(data.get("asset_id", data.get("token_id", "")))
        if not token_id:
            return

        # Parse prices
        bid = None
        ask = None

        if "price" in data:
            price = Decimal(str(data["price"]))
            side = data.get("side", "")
            if side == "bid":
                bid = price
            elif side == "ask":
                ask = price
        else:
            if "bid" in data:
                bid = Decimal(str(data["bid"]))
            if "ask" in data:
                ask = Decimal(str(data["ask"]))

        price_update = TokenPrice(
            token_id=token_id,
            timestamp=datetime.now(timezone.utc),
            bid=bid,
            ask=ask,
        )

        # Publish to EventBus
        await self._event_bus.publish(
            f"market.price.{token_id}",
            {
                "token_id": token_id,
                "bid": str(bid) if bid else None,
                "ask": str(ask) if ask else None,
                "timestamp": price_update.timestamp.isoformat(),
            }
        )

    async def _handle_book_snapshot(self, data: dict) -> None:
        """Handle a full order book snapshot."""
        token_id = str(data.get("asset_id", data.get("token_id", "")))
        if not token_id:
            return

        # Parse levels
        bids = self._parse_levels(data.get("bids", []))
        asks = self._parse_levels(data.get("asks", []))

        book = OrderBookData(
            token_id=token_id,
            timestamp=datetime.now(timezone.utc),
            bids=tuple(sorted(bids, key=lambda x: x.price, reverse=True)),
            asks=tuple(sorted(asks, key=lambda x: x.price)),
        )

        # Publish to EventBus
        await self._event_bus.publish(
            f"market.book.{token_id}",
            {
                "token_id": token_id,
                "best_bid": str(book.best_bid) if book.best_bid else None,
                "best_ask": str(book.best_ask) if book.best_ask else None,
                "bid_depth": len(bids),
                "ask_depth": len(asks),
                "timestamp": book.timestamp.isoformat(),
            }
        )

    def _parse_levels(self, levels: list) -> list[OrderBookLevel]:
        """Parse order book levels from message."""
        result = []
        for level in levels:
            if isinstance(level, dict):
                price = Decimal(str(level.get("price", 0)))
                size = Decimal(str(level.get("size", 0)))
            elif isinstance(level, (list, tuple)) and len(level) >= 2:
                price = Decimal(str(level[0]))
                size = Decimal(str(level[1]))
            else:
                continue

            if size > 0:
                result.append(OrderBookLevel(price=price, size=size))

        return result

    async def _send_subscribe(self, token_ids: list[str]) -> None:
        """Send subscription message."""
        if not self._ws or not token_ids:
            return

        message = {
            "type": "market",
            "assets_ids": token_ids,
        }

        await self._ws.send(json.dumps(message))
        self._subscriptions.update(token_ids)
        self._pending_subscriptions -= set(token_ids)

        self._log.debug("subscribed", token_count=len(token_ids))

    async def _send_unsubscribe(self, token_ids: list[str]) -> None:
        """Send unsubscribe message."""
        if not self._ws or not token_ids:
            return

        message = {
            "type": "unsubscribe",
            "channel": "market",
            "assets_ids": token_ids,
        }

        await self._ws.send(json.dumps(message))
        self._log.debug("unsubscribed", token_count=len(token_ids))

    async def _health_check_loop(self) -> None:
        """Periodic health check to detect stale connections."""
        while self._should_run:
            await asyncio.sleep(STALE_THRESHOLD / 2)

            if not self.is_connected:
                continue

            staleness = time.time() - self._last_message_time
            if staleness > STALE_THRESHOLD:
                self._log.warning(
                    "stale_connection_detected",
                    staleness=staleness,
                    subscriptions=len(self._subscriptions),
                )

                # Force reconnect
                await self._disconnect()

                # Publish stale event
                await self._event_bus.publish("market.ws.stale", {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "staleness_seconds": staleness,
                })
