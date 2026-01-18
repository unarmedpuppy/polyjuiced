# Polymarket Integration Layer
# CLOB client, Gamma API, and WebSocket streaming

from mercury.integrations.polymarket.types import (
    DualLegOrderResult,
    Market15Min,
    MarketInfo,
    OrderBookData,
    OrderBookLevel,
    OrderBookSnapshot,
    OrderResult,
    OrderSide,
    OrderStatus,
    PolymarketSettings,
    PositionInfo,
    TimeInForce,
    TokenPrice,
    TokenSide,
    WebSocketMessage,
)

from mercury.integrations.polymarket.clob import (
    CLOBClient,
    CLOBClientError,
    OrderRejectedError,
    OrderTimeoutError,
    InsufficientLiquidityError,
    InsufficientBalanceError,
    ArbitrageInvalidError,
    OrderSigningError,
    BatchOrderError,
)

__all__ = [
    # Enums
    "OrderSide",
    "OrderStatus",
    "TimeInForce",
    "TokenSide",
    # Settings
    "PolymarketSettings",
    # Market data types
    "MarketInfo",
    "Market15Min",
    "OrderBookLevel",
    "OrderBookData",
    "OrderBookSnapshot",
    "TokenPrice",
    "WebSocketMessage",
    # Order types
    "OrderResult",
    "DualLegOrderResult",
    "PositionInfo",
    # CLOB Client
    "CLOBClient",
    # Errors
    "CLOBClientError",
    "OrderRejectedError",
    "OrderTimeoutError",
    "InsufficientLiquidityError",
    "InsufficientBalanceError",
    "ArbitrageInvalidError",
    "OrderSigningError",
    "BatchOrderError",
]
