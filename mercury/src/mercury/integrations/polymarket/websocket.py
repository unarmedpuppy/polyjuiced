"""Polymarket WebSocket client for real-time market data.

This client connects to Polymarket's WebSocket API and publishes
order book updates via the EventBus for consumption by other services.

Features:
- Heartbeat/ping-pong monitoring with explicit tracking
- Subscription state tracking for reliable reconnection
- Automatic reconnection with exponential backoff
- Publishes market data to EventBus (no callbacks)
- Connection health metrics via MetricsEmitter
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional, Set

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
)

# Use TYPE_CHECKING to avoid circular import
# services/__init__.py imports market_data which imports websocket
if TYPE_CHECKING:
    from mercury.services.metrics import MetricsEmitter

log = structlog.get_logger()


class SubscriptionState(str, Enum):
    """State of a token subscription."""

    PENDING = "pending"       # Subscription requested but not confirmed
    ACTIVE = "active"         # Subscription confirmed by server
    UNSUBSCRIBING = "unsubscribing"  # Unsubscribe requested


@dataclass
class HeartbeatState:
    """Tracks heartbeat/ping-pong health."""

    last_ping_sent: float = 0.0
    last_pong_received: float = 0.0
    last_message_received: float = 0.0
    ping_count: int = 0
    pong_count: int = 0
    missed_pongs: int = 0

    @property
    def is_healthy(self) -> bool:
        """Check if heartbeat is healthy (no missed pongs)."""
        return self.missed_pongs < 2

    @property
    def seconds_since_pong(self) -> float:
        """Seconds since last pong received."""
        if self.last_pong_received == 0:
            return 0.0
        return time.time() - self.last_pong_received

    @property
    def seconds_since_message(self) -> float:
        """Seconds since any message received."""
        if self.last_message_received == 0:
            return 0.0
        return time.time() - self.last_message_received


@dataclass
class ConnectionMetrics:
    """Connection health metrics."""

    messages_received: int = 0
    messages_parsed: int = 0
    parse_errors: int = 0
    reconnect_count: int = 0
    connect_time: float = 0.0
    price_updates: int = 0
    book_updates: int = 0

    def reset(self) -> None:
        """Reset counters (called on reconnect)."""
        self.messages_received = 0
        self.messages_parsed = 0
        self.parse_errors = 0
        self.price_updates = 0
        self.book_updates = 0


@dataclass
class SubscriptionEntry:
    """Tracks a single subscription."""

    token_id: str
    state: SubscriptionState = SubscriptionState.PENDING
    subscribed_at: Optional[float] = None
    confirmed_at: Optional[float] = None
    last_message_at: Optional[float] = None


# Connection parameters
PING_INTERVAL = 20.0  # Send ping every 20 seconds
PONG_TIMEOUT = 10.0   # Wait 10 seconds for pong
RECONNECT_MIN_WAIT = 1.0
RECONNECT_MAX_WAIT = 60.0
STALE_THRESHOLD = 60.0  # Consider connection stale if no message for 60s
HEARTBEAT_CHECK_INTERVAL = 15.0  # Check heartbeat health every 15 seconds


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
    - Monitors connection health with heartbeat tracking
    - Emits Prometheus metrics for observability

    Event channels published:
    - market.price.{token_id} - Price updates (TokenPrice)
    - market.book.{token_id} - Full book updates (OrderBookData)
    - market.ws.connected - Connection established
    - market.ws.disconnected - Connection lost
    - market.ws.stale - Connection became stale
    - market.ws.heartbeat_failed - Heartbeat check failed
    """

    def __init__(
        self,
        settings: PolymarketSettings,
        event_bus: EventBus,
        metrics: Optional["MetricsEmitter"] = None,
    ):
        """Initialize the WebSocket client.

        Args:
            settings: Polymarket connection settings.
            event_bus: EventBus for publishing updates.
            metrics: Optional MetricsEmitter for Prometheus metrics.
        """
        super().__init__()
        self._ws_url = settings.ws_url
        self._event_bus = event_bus
        self._metrics = metrics
        self._log = log.bind(component="polymarket_ws")

        self._ws: Optional[websockets.WebSocketClientProtocol] = None

        # Subscription tracking with state
        self._subscriptions: dict[str, SubscriptionEntry] = {}

        # Heartbeat state
        self._heartbeat = HeartbeatState()

        # Connection metrics
        self._conn_metrics = ConnectionMetrics()

        # Reconnection state
        self._reconnect_delay: float = RECONNECT_MIN_WAIT
        self._should_run: bool = False

        # Background tasks
        self._message_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    @property
    def is_connected(self) -> bool:
        """Whether currently connected to WebSocket."""
        return self._ws is not None and self._ws.open

    @property
    def active_subscriptions(self) -> set[str]:
        """Token IDs with active subscriptions."""
        return {
            entry.token_id
            for entry in self._subscriptions.values()
            if entry.state == SubscriptionState.ACTIVE
        }

    @property
    def pending_subscriptions(self) -> set[str]:
        """Token IDs with pending subscriptions."""
        return {
            entry.token_id
            for entry in self._subscriptions.values()
            if entry.state == SubscriptionState.PENDING
        }

    async def start(self) -> None:
        """Start the WebSocket client and begin receiving messages."""
        if self._should_run:
            return

        self._should_run = True
        self._start_time = time.time()
        self._log.info("starting_websocket_client", url=self._ws_url)

        # Start message loop and heartbeat monitor
        self._message_task = asyncio.create_task(self._message_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        """Stop the WebSocket client gracefully."""
        self._should_run = False
        self._log.info("stopping_websocket_client")

        # Cancel tasks
        for task in [self._message_task, self._heartbeat_task]:
            if task:
                task.cancel()
                try:
                    await task
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
                details={
                    "reconnect_count": self._conn_metrics.reconnect_count,
                }
            )

        # Check heartbeat health
        if not self._heartbeat.is_healthy:
            return HealthCheckResult(
                status=HealthStatus.DEGRADED,
                message=f"Heartbeat unhealthy: {self._heartbeat.missed_pongs} missed pongs",
                details={
                    "missed_pongs": self._heartbeat.missed_pongs,
                    "seconds_since_pong": self._heartbeat.seconds_since_pong,
                }
            )

        # Check for stale connection
        staleness = self._heartbeat.seconds_since_message
        if staleness > STALE_THRESHOLD:
            return HealthCheckResult(
                status=HealthStatus.DEGRADED,
                message=f"No messages for {staleness:.1f}s",
                details={
                    "seconds_since_message": staleness,
                    "active_subscriptions": len(self.active_subscriptions),
                }
            )

        return HealthCheckResult(
            status=HealthStatus.HEALTHY,
            message="Connected and receiving",
            details={
                "active_subscriptions": len(self.active_subscriptions),
                "pending_subscriptions": len(self.pending_subscriptions),
                "messages_received": self._conn_metrics.messages_received,
                "reconnects": self._conn_metrics.reconnect_count,
                "ping_count": self._heartbeat.ping_count,
                "pong_count": self._heartbeat.pong_count,
            }
        )

    async def subscribe(self, token_ids: list[str]) -> None:
        """Subscribe to market data for tokens.

        Args:
            token_ids: List of token IDs to subscribe to.
        """
        new_tokens = []
        for token_id in token_ids:
            tid = str(token_id)
            if tid not in self._subscriptions:
                entry = SubscriptionEntry(
                    token_id=tid,
                    state=SubscriptionState.PENDING,
                    subscribed_at=time.time(),
                )
                self._subscriptions[tid] = entry
                new_tokens.append(tid)
            elif self._subscriptions[tid].state == SubscriptionState.PENDING:
                # Already pending, no action needed
                pass

        if not new_tokens:
            return

        if self.is_connected:
            await self._send_subscribe(new_tokens)

    async def unsubscribe(self, token_ids: list[str]) -> None:
        """Unsubscribe from market data for tokens.

        Args:
            token_ids: List of token IDs to unsubscribe from.
        """
        tokens_to_remove = []
        for token_id in token_ids:
            tid = str(token_id)
            if tid in self._subscriptions:
                self._subscriptions[tid].state = SubscriptionState.UNSUBSCRIBING
                tokens_to_remove.append(tid)

        if tokens_to_remove and self.is_connected:
            await self._send_unsubscribe(tokens_to_remove)

        # Remove from tracking
        for tid in tokens_to_remove:
            del self._subscriptions[tid]

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

    async def _heartbeat_loop(self) -> None:
        """Monitor heartbeat health and force reconnect if unhealthy."""
        while self._should_run:
            await asyncio.sleep(HEARTBEAT_CHECK_INTERVAL)

            if not self.is_connected:
                continue

            # Check staleness
            staleness = self._heartbeat.seconds_since_message
            if staleness > STALE_THRESHOLD:
                self._log.warning(
                    "stale_connection_detected",
                    staleness_seconds=staleness,
                    active_subscriptions=len(self.active_subscriptions),
                )

                # Emit stale event
                await self._event_bus.publish("market.ws.stale", {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "staleness_seconds": staleness,
                })

                # Force reconnect
                await self._disconnect()

            # Check heartbeat health
            if not self._heartbeat.is_healthy:
                self._log.warning(
                    "heartbeat_failed",
                    missed_pongs=self._heartbeat.missed_pongs,
                    seconds_since_pong=self._heartbeat.seconds_since_pong,
                )

                # Emit heartbeat failure event
                await self._event_bus.publish("market.ws.heartbeat_failed", {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "missed_pongs": self._heartbeat.missed_pongs,
                })

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

        # Reset heartbeat state
        now = time.time()
        self._heartbeat = HeartbeatState(
            last_message_received=now,
            last_pong_received=now,
        )
        self._conn_metrics.connect_time = now
        self._reconnect_delay = RECONNECT_MIN_WAIT

        self._log.info("websocket_connected")

        # Update metrics
        if self._metrics:
            self._metrics.update_websocket_status(True)

        # Publish connection event
        await self._event_bus.publish("market.ws.connected", {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reconnect_count": self._conn_metrics.reconnect_count,
        })

        # Restore all subscriptions after reconnect
        tokens_to_resubscribe = [
            entry.token_id
            for entry in self._subscriptions.values()
            if entry.state in (SubscriptionState.PENDING, SubscriptionState.ACTIVE)
        ]

        if tokens_to_resubscribe:
            # Mark all as pending until confirmed
            for tid in tokens_to_resubscribe:
                self._subscriptions[tid].state = SubscriptionState.PENDING
                self._subscriptions[tid].subscribed_at = time.time()

            await self._send_subscribe(tokens_to_resubscribe)

    async def _disconnect(self) -> None:
        """Close WebSocket connection."""
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        # Update metrics
        if self._metrics:
            self._metrics.update_websocket_status(False)

    async def _handle_disconnect(self) -> None:
        """Handle disconnection with exponential backoff."""
        await self._disconnect()
        self._conn_metrics.reconnect_count += 1

        # Update metrics
        if self._metrics:
            self._metrics.record_websocket_reconnect()

        # Publish disconnection event
        await self._event_bus.publish("market.ws.disconnected", {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reconnect_count": self._conn_metrics.reconnect_count,
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
            self._heartbeat.last_message_received = time.time()
            self._conn_metrics.messages_received += 1

            try:
                await self._process_message(raw_message)
                self._conn_metrics.messages_parsed += 1
            except Exception as e:
                self._conn_metrics.parse_errors += 1
                self._log.warning("message_processing_error", error=str(e))

    async def _process_message(self, raw: str) -> None:
        """Process a raw WebSocket message.

        Polymarket sends various message formats:
        - {"price_changes": [...]} - Price updates
        - {"bids": [...], "asks": [...]} - Full book
        - {"event_type": "..."} - Typed events
        - [...] - Batch of messages
        - "PONG" / "PING" - Heartbeat responses (text, not JSON)

        Reference: legacy/src/client/websocket.py for message parsing.
        """
        # Handle text-based heartbeat messages
        if raw in ("PONG", "pong"):
            self._heartbeat.last_pong_received = time.time()
            self._heartbeat.pong_count += 1
            self._heartbeat.missed_pongs = 0
            return

        if raw in ("PING", "ping"):
            # Echo back pong if server sends ping
            if self._ws:
                await self._ws.send("PONG")
            return

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
        # Handle subscription confirmation
        msg_type = data.get("type") or data.get("event_type")

        if msg_type in ("subscribed", "connected"):
            self._handle_subscription_confirmed(data)
            return

        if msg_type == "error":
            self._log.error("websocket_error_message", data=data)
            return

        # Format 1: Price changes (most common from Polymarket)
        if "price_changes" in data:
            for change in data["price_changes"]:
                await self._handle_price_change(change)
            return

        # Format 2: Full book snapshot
        if "bids" in data and "asks" in data:
            await self._handle_book_snapshot(data)
            return

        # Format 3: Explicit event type
        if msg_type == "price_change":
            await self._handle_price_change(data)
        elif msg_type == "book":
            await self._handle_book_snapshot(data)
        elif msg_type == "last_trade_price":
            # Trade execution notification - log but don't emit
            self._log.debug("trade_executed", data=data)
        elif msg_type == "tick_size_change":
            self._log.debug("tick_size_changed", data=data)

    def _handle_subscription_confirmed(self, data: dict) -> None:
        """Handle subscription confirmation from server."""
        # Polymarket may confirm with assets_ids or individual token confirmations
        confirmed_tokens = data.get("assets_ids", [])

        if not confirmed_tokens:
            # Single token confirmation format
            token_id = data.get("asset_id") or data.get("token_id")
            if token_id:
                confirmed_tokens = [str(token_id)]

        now = time.time()
        for tid in confirmed_tokens:
            tid = str(tid)
            if tid in self._subscriptions:
                self._subscriptions[tid].state = SubscriptionState.ACTIVE
                self._subscriptions[tid].confirmed_at = now
                self._log.debug("subscription_confirmed", token_id=tid)

    async def _handle_price_change(self, data: dict) -> None:
        """Handle a price change message."""
        # Token IDs are large integers - always convert to string
        token_id = str(data.get("asset_id") or data.get("token_id") or "")
        if not token_id:
            return

        # Update subscription tracking
        if token_id in self._subscriptions:
            self._subscriptions[token_id].last_message_at = time.time()
            # If we receive data, subscription is confirmed active
            if self._subscriptions[token_id].state == SubscriptionState.PENDING:
                self._subscriptions[token_id].state = SubscriptionState.ACTIVE
                self._subscriptions[token_id].confirmed_at = time.time()

        # Parse prices - handle multiple formats from legacy parsing
        bid = None
        ask = None

        # Format 1: Separate bid/ask fields (best_bid/best_ask)
        if "best_bid" in data:
            bid = Decimal(str(data["best_bid"]))
        elif "bid" in data:
            bid = Decimal(str(data["bid"]))

        if "best_ask" in data:
            ask = Decimal(str(data["best_ask"]))
        elif "ask" in data:
            ask = Decimal(str(data["ask"]))

        # Format 2: Single price with side
        if "price" in data and bid is None and ask is None:
            price = Decimal(str(data["price"]))
            side = data.get("side", "")
            if side == "bid":
                bid = price
            elif side == "ask":
                ask = price

        price_update = TokenPrice(
            token_id=token_id,
            timestamp=datetime.now(timezone.utc),
            bid=bid,
            ask=ask,
        )

        self._conn_metrics.price_updates += 1

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
        # Token IDs are large integers - always convert to string
        token_id = str(data.get("asset_id") or data.get("token_id") or "")
        if not token_id:
            return

        # Update subscription tracking
        if token_id in self._subscriptions:
            self._subscriptions[token_id].last_message_at = time.time()
            if self._subscriptions[token_id].state == SubscriptionState.PENDING:
                self._subscriptions[token_id].state = SubscriptionState.ACTIVE
                self._subscriptions[token_id].confirmed_at = time.time()

        # Parse levels using legacy patterns
        bids = self._parse_levels(data.get("bids", []))
        asks = self._parse_levels(data.get("asks", []))

        book = OrderBookData(
            token_id=token_id,
            timestamp=datetime.now(timezone.utc),
            bids=tuple(sorted(bids, key=lambda x: x.price, reverse=True)),
            asks=tuple(sorted(asks, key=lambda x: x.price)),
        )

        self._conn_metrics.book_updates += 1

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
        """Parse order book levels from message.

        Handles multiple formats from legacy parsing:
        - [{"price": "0.50", "size": "100"}, ...]
        - [[0.50, 100], ...]
        """
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

        # Polymarket expects: {"type": "market", "assets_ids": [...]}
        message = {
            "type": "market",
            "assets_ids": token_ids,
        }

        await self._ws.send(json.dumps(message))
        self._log.debug("subscribe_sent", token_count=len(token_ids))

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
        self._log.debug("unsubscribe_sent", token_count=len(token_ids))

    def get_subscription_info(self) -> dict:
        """Get detailed subscription information for debugging."""
        return {
            "active": [
                {
                    "token_id": e.token_id,
                    "confirmed_at": e.confirmed_at,
                    "last_message_at": e.last_message_at,
                }
                for e in self._subscriptions.values()
                if e.state == SubscriptionState.ACTIVE
            ],
            "pending": [
                {
                    "token_id": e.token_id,
                    "subscribed_at": e.subscribed_at,
                }
                for e in self._subscriptions.values()
                if e.state == SubscriptionState.PENDING
            ],
            "connection_metrics": {
                "messages_received": self._conn_metrics.messages_received,
                "price_updates": self._conn_metrics.price_updates,
                "book_updates": self._conn_metrics.book_updates,
                "parse_errors": self._conn_metrics.parse_errors,
                "reconnect_count": self._conn_metrics.reconnect_count,
            },
            "heartbeat": {
                "ping_count": self._heartbeat.ping_count,
                "pong_count": self._heartbeat.pong_count,
                "missed_pongs": self._heartbeat.missed_pongs,
                "seconds_since_message": self._heartbeat.seconds_since_message,
            },
        }
