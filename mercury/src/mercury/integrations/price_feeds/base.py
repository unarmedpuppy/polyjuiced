"""Base protocol for price feeds.

Price feeds provide reference pricing from external sources
(like Binance) for use in strategy decisions.
"""

from abc import abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable, Optional, Protocol


@dataclass(frozen=True)
class PriceUpdate:
    """A price update from an external feed.

    Attributes:
        symbol: Trading symbol (e.g., "BTCUSDT").
        price: Current price.
        timestamp: When the price was observed.
        source: Name of the price feed source.
    """

    symbol: str
    price: Decimal
    timestamp: datetime
    source: str


class PriceFeed(Protocol):
    """Protocol for external price feeds.

    Implementations should:
    - Connect to external price source
    - Provide current price on demand
    - Support subscription to real-time updates
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Feed identifier (e.g., 'binance', 'coinbase')."""
        ...

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Whether feed is connected and receiving data."""
        ...

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the price source."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close connection to the price source."""
        ...

    @abstractmethod
    async def get_price(self, symbol: str) -> Optional[Decimal]:
        """Get current price for a symbol.

        Args:
            symbol: Trading symbol (e.g., "BTCUSDT").

        Returns:
            Current price or None if unavailable.
        """
        ...

    @abstractmethod
    async def subscribe(
        self,
        symbol: str,
        callback: Callable[[PriceUpdate], None],
    ) -> None:
        """Subscribe to real-time price updates.

        Args:
            symbol: Trading symbol to subscribe to.
            callback: Function called on each price update.
        """
        ...

    @abstractmethod
    async def unsubscribe(self, symbol: str) -> None:
        """Unsubscribe from price updates for a symbol.

        Args:
            symbol: Trading symbol to unsubscribe from.
        """
        ...
