"""Mock WebSocket for testing real-time price updates.

Provides a controllable test double for PolymarketWebSocket that:
- Allows programmatic emission of price updates
- Tracks subscriptions
- Simulates connection/disconnection
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set
import asyncio


@dataclass
class OrderBookUpdate:
    """Represents an order book update from WebSocket."""
    token_id: str
    timestamp: datetime
    bids: List[Dict[str, float]]  # [{"price": 0.50, "size": 100}, ...]
    asks: List[Dict[str, float]]
    best_bid: float
    best_ask: float

    @property
    def midpoint(self) -> float:
        return (self.best_bid + self.best_ask) / 2


@dataclass
class PriceUpdate:
    """Represents a price change event."""
    token_id: str
    timestamp: datetime
    best_bid: float
    best_ask: float
    last_trade_price: Optional[float] = None


@dataclass
class WebSocketEvent:
    """Record of a WebSocket event for testing."""
    event_type: str  # "price_change", "book_update", "subscribe", etc.
    data: Any
    timestamp: datetime = field(default_factory=datetime.utcnow)


class MockPolymarketWebSocket:
    """Controllable test double for PolymarketWebSocket.

    Usage:
        ws = MockPolymarketWebSocket()

        # Register callbacks (strategy will do this)
        ws.on_book_update(my_callback)
        ws.on_price_change(my_price_callback)

        # In test, emit updates
        ws.emit_price_update("yes-token", bid=0.52, ask=0.53)

        # Assert subscription state
        ws.assert_subscribed("yes-token")
    """

    def __init__(self):
        self.is_connected = True
        self._subscriptions: Set[str] = set()
        self._callbacks: Dict[str, List[Callable]] = {
            "book_update": [],
            "price_change": [],
            "connect": [],
            "disconnect": [],
            "error": [],
        }
        self._event_history: List[WebSocketEvent] = []
        self._auto_emit_on_subscribe: bool = False
        self._default_prices: Dict[str, Dict[str, float]] = {}

    # =========================================================================
    # Configuration Methods (for test setup)
    # =========================================================================

    def set_connected(self, connected: bool) -> None:
        """Set connection state."""
        self.is_connected = connected

    def set_auto_emit_on_subscribe(self, enabled: bool = True) -> None:
        """If enabled, automatically emit a price update when subscribed.

        Useful for simulating real WebSocket behavior where you get
        an initial snapshot on subscription.
        """
        self._auto_emit_on_subscribe = enabled

    def set_default_prices(self, token_id: str, bid: float, ask: float) -> None:
        """Set default prices to emit on subscription.

        Args:
            token_id: Token to configure
            bid: Default bid price
            ask: Default ask price
        """
        self._default_prices[token_id] = {"bid": bid, "ask": ask}

    def reset(self) -> None:
        """Reset all state (between tests)."""
        self._subscriptions.clear()
        self._event_history.clear()
        self._default_prices.clear()
        for callback_list in self._callbacks.values():
            callback_list.clear()
        self.is_connected = True

    # =========================================================================
    # Test Control Methods (emit events during test)
    # =========================================================================

    def emit_price_update(
        self,
        token_id: str,
        bid: float,
        ask: float,
        last_trade_price: float = None,
    ) -> None:
        """Emit a price update to registered callbacks.

        Args:
            token_id: Token that changed
            bid: New best bid
            ask: New best ask
            last_trade_price: Optional last trade price
        """
        update = PriceUpdate(
            token_id=token_id,
            timestamp=datetime.utcnow(),
            best_bid=bid,
            best_ask=ask,
            last_trade_price=last_trade_price,
        )

        self._event_history.append(WebSocketEvent(
            event_type="price_change",
            data=update,
        ))

        # Call registered callbacks
        for callback in self._callbacks["price_change"]:
            try:
                callback(update)
            except Exception:
                pass  # Don't let callback errors break tests

    def emit_book_update(
        self,
        token_id: str,
        bids: List[Dict[str, float]],
        asks: List[Dict[str, float]],
    ) -> None:
        """Emit a full order book update.

        Args:
            token_id: Token that updated
            bids: List of {"price": float, "size": float}
            asks: List of {"price": float, "size": float}
        """
        best_bid = bids[0]["price"] if bids else 0.0
        best_ask = asks[0]["price"] if asks else 1.0

        update = OrderBookUpdate(
            token_id=token_id,
            timestamp=datetime.utcnow(),
            bids=bids,
            asks=asks,
            best_bid=best_bid,
            best_ask=best_ask,
        )

        self._event_history.append(WebSocketEvent(
            event_type="book_update",
            data=update,
        ))

        for callback in self._callbacks["book_update"]:
            try:
                callback(update)
            except Exception:
                pass

    def emit_book_update_simple(
        self,
        token_id: str,
        bid: float,
        bid_size: float,
        ask: float,
        ask_size: float,
    ) -> None:
        """Emit a simple order book update with single level.

        Convenience method for common test case.

        Args:
            token_id: Token
            bid: Best bid price
            bid_size: Bid size
            ask: Best ask price
            ask_size: Ask size
        """
        self.emit_book_update(
            token_id=token_id,
            bids=[{"price": bid, "size": bid_size}],
            asks=[{"price": ask, "size": ask_size}],
        )

    def simulate_disconnect(self) -> None:
        """Simulate WebSocket disconnection."""
        self.is_connected = False

        self._event_history.append(WebSocketEvent(
            event_type="disconnect",
            data={"reason": "simulated"},
        ))

        for callback in self._callbacks["disconnect"]:
            try:
                callback()
            except Exception:
                pass

    def simulate_reconnect(self) -> None:
        """Simulate WebSocket reconnection."""
        self.is_connected = True

        self._event_history.append(WebSocketEvent(
            event_type="connect",
            data={"reconnected": True},
        ))

        for callback in self._callbacks["connect"]:
            try:
                callback()
            except Exception:
                pass

    def simulate_error(self, error_message: str) -> None:
        """Simulate WebSocket error."""
        self._event_history.append(WebSocketEvent(
            event_type="error",
            data={"error": error_message},
        ))

        for callback in self._callbacks["error"]:
            try:
                callback(error_message)
            except Exception:
                pass

    async def emit_price_sequence(
        self,
        token_id: str,
        prices: List[Dict[str, float]],
        interval_seconds: float = 0.1,
    ) -> None:
        """Emit a sequence of price updates over time.

        Useful for testing rebalancing response to price movements.

        Args:
            token_id: Token to update
            prices: List of {"bid": float, "ask": float}
            interval_seconds: Time between updates
        """
        for price in prices:
            self.emit_price_update(
                token_id=token_id,
                bid=price["bid"],
                ask=price["ask"],
            )
            await asyncio.sleep(interval_seconds)

    # =========================================================================
    # Real Interface Methods (called by strategy)
    # =========================================================================

    def connect(self) -> bool:
        """Simulate connection."""
        self.is_connected = True
        self._event_history.append(WebSocketEvent(
            event_type="connect",
            data={},
        ))
        return True

    def disconnect(self) -> None:
        """Simulate disconnection."""
        self.is_connected = False
        self._event_history.append(WebSocketEvent(
            event_type="disconnect",
            data={"intentional": True},
        ))

    def subscribe(self, token_ids: List[str]) -> None:
        """Subscribe to tokens.

        Args:
            token_ids: Tokens to subscribe to
        """
        for token_id in token_ids:
            self._subscriptions.add(token_id)

        self._event_history.append(WebSocketEvent(
            event_type="subscribe",
            data={"token_ids": token_ids},
        ))

        # Auto-emit initial prices if configured
        if self._auto_emit_on_subscribe:
            for token_id in token_ids:
                prices = self._default_prices.get(token_id, {"bid": 0.49, "ask": 0.51})
                self.emit_price_update(token_id, **prices)

    def unsubscribe(self, token_ids: List[str]) -> None:
        """Unsubscribe from tokens.

        Args:
            token_ids: Tokens to unsubscribe from
        """
        for token_id in token_ids:
            self._subscriptions.discard(token_id)

        self._event_history.append(WebSocketEvent(
            event_type="unsubscribe",
            data={"token_ids": token_ids},
        ))

    def on_book_update(self, callback: Callable[[OrderBookUpdate], None]) -> None:
        """Register callback for book updates.

        Args:
            callback: Function to call on book updates
        """
        self._callbacks["book_update"].append(callback)

    def on_price_change(self, callback: Callable[[PriceUpdate], None]) -> None:
        """Register callback for price changes.

        Args:
            callback: Function to call on price changes
        """
        self._callbacks["price_change"].append(callback)

    def on_connect(self, callback: Callable[[], None]) -> None:
        """Register callback for connection events."""
        self._callbacks["connect"].append(callback)

    def on_disconnect(self, callback: Callable[[], None]) -> None:
        """Register callback for disconnection events."""
        self._callbacks["disconnect"].append(callback)

    def on_error(self, callback: Callable[[str], None]) -> None:
        """Register callback for errors."""
        self._callbacks["error"].append(callback)

    async def run(self) -> None:
        """Run the WebSocket (no-op in mock, real one has event loop)."""
        pass

    # =========================================================================
    # Assertion Helpers (for test validation)
    # =========================================================================

    def assert_subscribed(self, token_id: str) -> None:
        """Assert that a token is subscribed.

        Raises:
            AssertionError: If token not subscribed
        """
        if token_id not in self._subscriptions:
            raise AssertionError(
                f"Token {token_id} not subscribed. "
                f"Current subscriptions: {self._subscriptions}"
            )

    def assert_not_subscribed(self, token_id: str) -> None:
        """Assert that a token is not subscribed."""
        if token_id in self._subscriptions:
            raise AssertionError(f"Token {token_id} should not be subscribed")

    def assert_connected(self) -> None:
        """Assert WebSocket is connected."""
        if not self.is_connected:
            raise AssertionError("WebSocket is not connected")

    def assert_disconnected(self) -> None:
        """Assert WebSocket is disconnected."""
        if self.is_connected:
            raise AssertionError("WebSocket should be disconnected")

    def get_subscriptions(self) -> Set[str]:
        """Get current subscriptions."""
        return self._subscriptions.copy()

    def get_event_history(self, event_type: str = None) -> List[WebSocketEvent]:
        """Get event history, optionally filtered.

        Args:
            event_type: Filter to specific event type

        Returns:
            List of WebSocketEvent
        """
        if event_type:
            return [e for e in self._event_history if e.event_type == event_type]
        return self._event_history.copy()

    def get_price_updates(self, token_id: str = None) -> List[PriceUpdate]:
        """Get price updates emitted, optionally filtered by token.

        Args:
            token_id: Filter to specific token

        Returns:
            List of PriceUpdate
        """
        updates = []
        for event in self._event_history:
            if event.event_type == "price_change":
                update = event.data
                if token_id is None or update.token_id == token_id:
                    updates.append(update)
        return updates
