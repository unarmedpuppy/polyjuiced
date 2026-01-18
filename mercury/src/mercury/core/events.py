"""
Event bus implementation using Redis pub/sub.

Provides decoupled communication between components via message passing.
"""
import asyncio
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Coroutine, Optional

import redis.asyncio as redis


class EventEncoder(json.JSONEncoder):
    """Custom JSON encoder for event payloads."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if is_dataclass(obj) and not isinstance(obj, type):
            return asdict(obj)
        return super().default(obj)


def decode_event(data: str) -> dict[str, Any]:
    """Decode JSON event data."""
    return json.loads(data)


EventHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class EventBus:
    """Redis-backed event bus for component communication.

    Usage:
        bus = EventBus(redis_url="redis://localhost:6379")
        await bus.connect()

        async def handler(event):
            print(f"Received: {event}")

        await bus.subscribe("market.orderbook.*", handler)
        await bus.publish("market.orderbook.btc", {"price": 0.5})
    """

    def __init__(self, redis_url: str = "redis://localhost:6379") -> None:
        """Initialize EventBus.

        Args:
            redis_url: Redis connection URL
        """
        self._redis_url = redis_url
        self._redis: Optional[redis.Redis] = None
        self._pubsub: Optional[redis.client.PubSub] = None
        self._handlers: dict[str, list[EventHandler]] = {}
        self._subscriber_task: Optional[asyncio.Task] = None
        self._running = False

    async def connect(self) -> None:
        """Establish connection to Redis."""
        self._redis = redis.from_url(
            self._redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        # Verify connection
        await self._redis.ping()
        self._pubsub = self._redis.pubsub()
        self._running = True
        # Start subscriber loop
        self._subscriber_task = asyncio.create_task(self._subscriber_loop())

    async def disconnect(self) -> None:
        """Disconnect from Redis."""
        self._running = False
        if self._subscriber_task:
            self._subscriber_task.cancel()
            try:
                await self._subscriber_task
            except asyncio.CancelledError:
                pass
        if self._pubsub:
            await self._pubsub.unsubscribe()
            await self._pubsub.close()
        if self._redis:
            await self._redis.close()
        self._redis = None
        self._pubsub = None

    @property
    def is_connected(self) -> bool:
        """Check if connected to Redis."""
        return self._redis is not None and self._running

    async def publish(self, channel: str, event: dict[str, Any] | Any) -> None:
        """Publish event to channel.

        Args:
            channel: Channel name (e.g., "market.orderbook.btc")
            event: Event data (dict or dataclass)
        """
        if not self._redis:
            raise RuntimeError("EventBus not connected")

        # Convert dataclass to dict if needed
        if is_dataclass(event) and not isinstance(event, type):
            event = asdict(event)

        data = json.dumps(event, cls=EventEncoder)
        await self._redis.publish(channel, data)

    async def subscribe(self, pattern: str, handler: EventHandler) -> None:
        """Subscribe to channel pattern with callback.

        Supports glob patterns like "market.*" or "market.orderbook.*".

        Args:
            pattern: Channel pattern (supports * wildcards)
            handler: Async callback function
        """
        if not self._pubsub:
            raise RuntimeError("EventBus not connected")

        if pattern not in self._handlers:
            self._handlers[pattern] = []
            # Use psubscribe for pattern matching
            if "*" in pattern:
                await self._pubsub.psubscribe(pattern)
            else:
                await self._pubsub.subscribe(pattern)

        self._handlers[pattern].append(handler)

    async def unsubscribe(self, pattern: str) -> None:
        """Unsubscribe from channel pattern.

        Args:
            pattern: Channel pattern to unsubscribe from
        """
        if not self._pubsub:
            return

        if pattern in self._handlers:
            del self._handlers[pattern]
            if "*" in pattern:
                await self._pubsub.punsubscribe(pattern)
            else:
                await self._pubsub.unsubscribe(pattern)

    async def _subscriber_loop(self) -> None:
        """Main loop for processing incoming messages."""
        if not self._pubsub:
            return

        try:
            async for message in self._pubsub.listen():
                if not self._running:
                    break

                if message["type"] not in ("message", "pmessage"):
                    continue

                try:
                    channel = message.get("channel", message.get("pattern", ""))
                    data = decode_event(message["data"])

                    # Find matching handlers
                    await self._dispatch_event(channel, data)
                except json.JSONDecodeError:
                    # Skip malformed messages
                    continue
                except Exception:
                    # Log and continue on handler errors
                    continue
        except asyncio.CancelledError:
            pass

    async def _dispatch_event(self, channel: str, data: dict[str, Any]) -> None:
        """Dispatch event to matching handlers."""
        for pattern, handlers in self._handlers.items():
            if self._pattern_matches(pattern, channel):
                for handler in handlers:
                    try:
                        await handler(data)
                    except Exception:
                        # Individual handler errors shouldn't stop other handlers
                        pass

    def _pattern_matches(self, pattern: str, channel: str) -> bool:
        """Check if channel matches subscription pattern."""
        if pattern == channel:
            return True

        if "*" not in pattern:
            return False

        # Simple glob matching
        parts = pattern.split("*")
        if len(parts) == 2:
            return channel.startswith(parts[0]) and channel.endswith(parts[1])

        # More complex patterns - do simple prefix match for now
        return channel.startswith(parts[0])
