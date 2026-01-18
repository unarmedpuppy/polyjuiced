# Polymarket Integration Layer
# CLOB client, Gamma API, and WebSocket streaming

from mercury.integrations.polymarket.types import (
    BalanceInfo,
    CLOBOrderBook,
    DualLegOrderResult,
    Market15Min,
    MarketInfo,
    MarketStatus,
    OpenOrder,
    OrderBookData,
    OrderBookLevel,
    OrderBookSnapshot,
    OrderResult,
    OrderSide,
    OrderStatus,
    PolymarketSettings,
    PositionInfo,
    TimeInForce,
    TokenPair,
    TokenPrice,
    TokenSide,
    TradeInfo,
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

from mercury.integrations.polymarket.market_finder import (
    MarketFinder,
    MarketFinderCache,
    MarketFinderWithSubscription,
)

__all__ = [
    # Enums
    "OrderSide",
    "OrderStatus",
    "TimeInForce",
    "TokenSide",
    "MarketStatus",
    # Settings
    "PolymarketSettings",
    # Market data types
    "MarketInfo",
    "Market15Min",
    "TokenPair",
    "OrderBookLevel",
    "OrderBookData",
    "OrderBookSnapshot",
    "CLOBOrderBook",
    "TokenPrice",
    "WebSocketMessage",
    # Order types
    "OrderResult",
    "DualLegOrderResult",
    "OpenOrder",
    "TradeInfo",
    # Account types
    "PositionInfo",
    "BalanceInfo",
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
    # Market finder
    "MarketFinder",
    "MarketFinderCache",
    "MarketFinderWithSubscription",
]
