"""Trade Event System

Phase 6: Dashboard Read-Only Mode (2025-12-14)

This module provides a simple event emitter for trade updates.
Strategy emits events when trades are created/updated, and
dashboard subscribes to update its display state.

Architecture:
    Strategy (mega_marble.py) --> events.py --> Dashboard (dashboard.py)
                                  |
                                  v
                              Database (persistence.py)

The key insight: Strategy owns persistence AND event emission.
Dashboard is purely a display layer that subscribes to events.
"""

import asyncio
from typing import Any, Callable, Dict, List, Optional

import structlog

log = structlog.get_logger()


class TradeEventEmitter:
    """Simple event emitter for trade updates.

    Events:
        - trade_created: New trade recorded
        - trade_resolved: Trade resolved (win/loss)
        - trade_updated: Trade data updated
        - stats_updated: Daily stats changed

    Usage:
        # Subscribe
        trade_events.subscribe(my_callback)

        # Emit (async)
        await trade_events.emit("trade_created", {"trade_id": "123", ...})

        # Unsubscribe
        trade_events.unsubscribe(my_callback)
    """

    def __init__(self):
        self._listeners: List[Callable] = []
        self._event_log: List[Dict[str, Any]] = []  # For debugging
        self._max_log_size: int = 100

    def subscribe(self, callback: Callable) -> None:
        """Subscribe to trade events.

        Args:
            callback: Function to call on events. Can be sync or async.
                      Signature: callback(event_type: str, data: dict)
        """
        if callback not in self._listeners:
            self._listeners.append(callback)
            log.debug("Event subscriber added", total_subscribers=len(self._listeners))

    def unsubscribe(self, callback: Callable) -> None:
        """Unsubscribe from trade events.

        Args:
            callback: Previously subscribed callback
        """
        if callback in self._listeners:
            self._listeners.remove(callback)
            log.debug("Event subscriber removed", total_subscribers=len(self._listeners))

    async def emit(self, event_type: str, data: Dict[str, Any]) -> None:
        """Emit event to all listeners.

        Args:
            event_type: Type of event (trade_created, trade_resolved, etc.)
            data: Event data payload
        """
        # Log event for debugging
        self._event_log.append({
            "type": event_type,
            "data": data,
        })
        if len(self._event_log) > self._max_log_size:
            self._event_log = self._event_log[-self._max_log_size:]

        log.debug(
            "Emitting event",
            event_type=event_type,
            listeners=len(self._listeners),
        )

        # Notify all listeners
        for listener in self._listeners:
            try:
                if asyncio.iscoroutinefunction(listener):
                    await listener(event_type, data)
                else:
                    listener(event_type, data)
            except Exception as e:
                log.error(
                    "Event listener error",
                    event_type=event_type,
                    error=str(e),
                )

    def emit_sync(self, event_type: str, data: Dict[str, Any]) -> None:
        """Emit event synchronously (fire-and-forget).

        Creates an async task to emit the event. Use when calling from
        sync code that can't await.

        Args:
            event_type: Type of event
            data: Event data payload
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(self.emit(event_type, data))
            else:
                loop.run_until_complete(self.emit(event_type, data))
        except RuntimeError:
            # No event loop available
            log.warning(
                "Cannot emit event - no event loop",
                event_type=event_type,
            )

    @property
    def subscriber_count(self) -> int:
        """Get number of subscribers."""
        return len(self._listeners)

    @property
    def recent_events(self) -> List[Dict[str, Any]]:
        """Get recent events for debugging."""
        return self._event_log.copy()


# Global event emitter instance
trade_events = TradeEventEmitter()


# Event type constants
class EventTypes:
    """Event type constants for type safety."""

    TRADE_CREATED = "trade_created"
    TRADE_RESOLVED = "trade_resolved"
    TRADE_UPDATED = "trade_updated"
    STATS_UPDATED = "stats_updated"
    PARTIAL_FILL = "partial_fill"
    ORDER_REJECTED = "order_rejected"
