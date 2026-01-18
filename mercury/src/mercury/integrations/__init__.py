"""External system adapters - Polymarket, price feeds, chain interactions."""

# Re-export commonly used components
from mercury.integrations.polymarket.types import (
    PolymarketSettings,
    MarketInfo,
    Market15Min,
    OrderBookData,
)
from mercury.integrations.polymarket.gamma import GammaClient
from mercury.integrations.polymarket.clob import CLOBClient
from mercury.integrations.polymarket.websocket import PolymarketWebSocket
from mercury.integrations.chain.client import PolygonClient

__all__ = [
    # Polymarket
    "PolymarketSettings",
    "MarketInfo",
    "Market15Min",
    "OrderBookData",
    "GammaClient",
    "CLOBClient",
    "PolymarketWebSocket",
    # Chain
    "PolygonClient",
]
