"""Domain models - pure data structures with no I/O dependencies."""

from mercury.domain.events import (
    FreshAlert,
    OrderBookSnapshotEvent,
    StaleAlert,
    TradeEvent,
)
from mercury.domain.market import Market, OrderBook, OrderBookLevel, Token
from mercury.domain.order import Order, OrderRequest, OrderResult, Fill, Position, OrderSide, OrderStatus
from mercury.domain.orderbook import InMemoryOrderBook, MarketOrderBook, PriceLevel, SortedPriceLevels
from mercury.domain.signal import TradingSignal, SignalType
from mercury.domain.risk import RiskLimits, CircuitBreakerState, CircuitBreakerLevel

__all__ = [
    # Event payloads for EventBus publishing
    "OrderBookSnapshotEvent",
    "TradeEvent",
    "StaleAlert",
    "FreshAlert",
    # Market models
    "Market",
    "OrderBook",
    "OrderBookLevel",
    "Token",
    # Order models
    "Order",
    "OrderRequest",
    "OrderResult",
    "Fill",
    "Position",
    "OrderSide",
    "OrderStatus",
    # Signal models
    "TradingSignal",
    "SignalType",
    # Risk models
    "RiskLimits",
    "CircuitBreakerState",
    "CircuitBreakerLevel",
    # Order book state management
    "InMemoryOrderBook",
    "MarketOrderBook",
    "PriceLevel",
    "SortedPriceLevels",
]
