"""In-memory order book state management.

This module provides efficient, mutable order book data structures optimized for:
- Fast best price lookups (O(1))
- Efficient incremental updates (O(log n))
- Depth queries at specified levels
- Thread-safe operations (via locks in service layer)

The InMemoryOrderBook maintains sorted price levels for both bids and asks,
supporting the common order book operations needed for trading strategies.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterator, Optional

from sortedcontainers import SortedDict


@dataclass
class PriceLevel:
    """A single price level in the order book.

    Attributes:
        price: The price at this level.
        size: Total size available at this price.
        order_count: Number of orders at this price (optional metadata).
    """

    price: Decimal
    size: Decimal
    order_count: int = 1

    def __post_init__(self) -> None:
        if self.price < 0 or self.price > 1:
            raise ValueError(f"price must be between 0 and 1, got {self.price}")
        if self.size < 0:
            raise ValueError(f"size must be non-negative, got {self.size}")


class SortedPriceLevels:
    """Sorted collection of price levels with efficient operations.

    Uses SortedDict for O(log n) insert/delete and O(1) best price access.
    For bids: highest price first (descending)
    For asks: lowest price first (ascending)
    """

    def __init__(self, ascending: bool = True) -> None:
        """Initialize sorted price levels.

        Args:
            ascending: If True, lowest price first (asks).
                       If False, highest price first (bids).
        """
        self._ascending = ascending
        # SortedDict maintains keys in ascending order
        # For bids (descending), we use negative prices as keys
        self._levels: SortedDict[Decimal, PriceLevel] = SortedDict()

    def _key(self, price: Decimal) -> Decimal:
        """Convert price to sort key."""
        return price if self._ascending else -price

    def _from_key(self, key: Decimal) -> Decimal:
        """Convert sort key back to price."""
        return key if self._ascending else -key

    def update(self, price: Decimal, size: Decimal, order_count: int = 1) -> None:
        """Update or insert a price level.

        If size is 0, the level is removed.
        If the level exists, it is replaced.

        Args:
            price: The price level.
            size: Total size at this price (0 to remove).
            order_count: Number of orders at this price.
        """
        key = self._key(price)
        if size <= 0:
            # Remove level
            if key in self._levels:
                del self._levels[key]
        else:
            # Update/insert level
            self._levels[key] = PriceLevel(price=price, size=size, order_count=order_count)

    def remove(self, price: Decimal) -> bool:
        """Remove a price level.

        Args:
            price: The price level to remove.

        Returns:
            True if the level existed and was removed.
        """
        key = self._key(price)
        if key in self._levels:
            del self._levels[key]
            return True
        return False

    def clear(self) -> None:
        """Remove all price levels."""
        self._levels.clear()

    def get(self, price: Decimal) -> Optional[PriceLevel]:
        """Get a specific price level.

        Args:
            price: The price to look up.

        Returns:
            The PriceLevel or None if not found.
        """
        key = self._key(price)
        return self._levels.get(key)

    @property
    def best(self) -> Optional[PriceLevel]:
        """Get the best price level (highest bid or lowest ask)."""
        if not self._levels:
            return None
        # First key in sorted order is the best
        key = self._levels.keys()[0]
        return self._levels[key]

    @property
    def best_price(self) -> Optional[Decimal]:
        """Get the best price value."""
        level = self.best
        return level.price if level else None

    @property
    def best_size(self) -> Decimal:
        """Get the size at the best price."""
        level = self.best
        return level.size if level else Decimal("0")

    def depth(self, levels: int = 10) -> list[PriceLevel]:
        """Get top N price levels.

        Args:
            levels: Number of levels to return.

        Returns:
            List of PriceLevel objects, best price first.
        """
        result: list[PriceLevel] = []
        for key in self._levels.keys()[:levels]:
            result.append(self._levels[key])
        return result

    def total_size(self, levels: Optional[int] = None) -> Decimal:
        """Get total size across price levels.

        Args:
            levels: Number of levels to include (None for all).

        Returns:
            Sum of sizes.
        """
        if levels is None:
            return sum(level.size for level in self._levels.values())
        return sum(level.size for level in self.depth(levels))

    def volume_at_price(self, target_price: Decimal) -> Decimal:
        """Get cumulative volume up to and including target price.

        For bids: sum of sizes for prices >= target_price
        For asks: sum of sizes for prices <= target_price

        Args:
            target_price: The target price.

        Returns:
            Cumulative volume.
        """
        total = Decimal("0")
        for level in self._levels.values():
            if self._ascending:
                # Asks: include if price <= target
                if level.price <= target_price:
                    total += level.size
                else:
                    break
            else:
                # Bids: include if price >= target
                if level.price >= target_price:
                    total += level.size
                else:
                    break
        return total

    def __len__(self) -> int:
        return len(self._levels)

    def __bool__(self) -> bool:
        return bool(self._levels)

    def __iter__(self) -> Iterator[PriceLevel]:
        return iter(self._levels.values())


@dataclass
class InMemoryOrderBook:
    """Mutable in-memory order book for a single token.

    Provides efficient operations for:
    - Incremental updates from WebSocket
    - Best bid/ask lookups (O(1))
    - Depth queries
    - Spread and midpoint calculations

    This is designed to be used within MarketDataService for each token,
    then combined into a market-level view.
    """

    token_id: str
    bids: SortedPriceLevels = field(default_factory=lambda: SortedPriceLevels(ascending=False))
    asks: SortedPriceLevels = field(default_factory=lambda: SortedPriceLevels(ascending=True))
    last_update: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sequence: int = 0  # For ordering updates

    def update_bid(self, price: Decimal, size: Decimal, order_count: int = 1) -> None:
        """Update a bid level.

        Args:
            price: Bid price.
            size: Total size at this price (0 to remove).
            order_count: Number of orders.
        """
        self.bids.update(price, size, order_count)
        self.last_update = datetime.now(timezone.utc)
        self.sequence += 1

    def update_ask(self, price: Decimal, size: Decimal, order_count: int = 1) -> None:
        """Update an ask level.

        Args:
            price: Ask price.
            size: Total size at this price (0 to remove).
            order_count: Number of orders.
        """
        self.asks.update(price, size, order_count)
        self.last_update = datetime.now(timezone.utc)
        self.sequence += 1

    def apply_snapshot(
        self,
        bids: list[tuple[Decimal, Decimal]],
        asks: list[tuple[Decimal, Decimal]],
    ) -> None:
        """Apply a full order book snapshot.

        Clears existing levels and replaces with snapshot data.

        Args:
            bids: List of (price, size) tuples for bid side.
            asks: List of (price, size) tuples for ask side.
        """
        self.bids.clear()
        self.asks.clear()

        for price, size in bids:
            if size > 0:
                self.bids.update(price, size)

        for price, size in asks:
            if size > 0:
                self.asks.update(price, size)

        self.last_update = datetime.now(timezone.utc)
        self.sequence += 1

    def apply_delta(
        self,
        bid_updates: Optional[list[tuple[Decimal, Decimal]]] = None,
        ask_updates: Optional[list[tuple[Decimal, Decimal]]] = None,
    ) -> None:
        """Apply incremental updates to the order book.

        Updates individual price levels without clearing the book.
        A size of 0 removes the level.

        Args:
            bid_updates: List of (price, size) tuples for bid updates.
            ask_updates: List of (price, size) tuples for ask updates.
        """
        if bid_updates:
            for price, size in bid_updates:
                self.bids.update(price, size)

        if ask_updates:
            for price, size in ask_updates:
                self.asks.update(price, size)

        self.last_update = datetime.now(timezone.utc)
        self.sequence += 1

    @property
    def best_bid(self) -> Optional[Decimal]:
        """Get best bid price."""
        return self.bids.best_price

    @property
    def best_ask(self) -> Optional[Decimal]:
        """Get best ask price."""
        return self.asks.best_price

    @property
    def best_bid_size(self) -> Decimal:
        """Get size at best bid."""
        return self.bids.best_size

    @property
    def best_ask_size(self) -> Decimal:
        """Get size at best ask."""
        return self.asks.best_size

    @property
    def midpoint(self) -> Optional[Decimal]:
        """Get midpoint between best bid and ask."""
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    @property
    def spread(self) -> Optional[Decimal]:
        """Get bid-ask spread."""
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    @property
    def spread_bps(self) -> Optional[Decimal]:
        """Get spread in basis points relative to midpoint."""
        mid = self.midpoint
        spread = self.spread
        if mid is not None and spread is not None and mid > 0:
            return (spread / mid) * Decimal("10000")
        return None

    def bid_depth(self, levels: int = 10) -> list[PriceLevel]:
        """Get top N bid levels."""
        return self.bids.depth(levels)

    def ask_depth(self, levels: int = 10) -> list[PriceLevel]:
        """Get top N ask levels."""
        return self.asks.depth(levels)

    def total_bid_size(self, levels: Optional[int] = None) -> Decimal:
        """Get total bid size."""
        return self.bids.total_size(levels)

    def total_ask_size(self, levels: Optional[int] = None) -> Decimal:
        """Get total ask size."""
        return self.asks.total_size(levels)

    def volume_weighted_bid(self, size: Decimal) -> Optional[Decimal]:
        """Calculate volume-weighted average price to sell a given size.

        Args:
            size: Size to sell (hitting bids).

        Returns:
            VWAP or None if insufficient liquidity.
        """
        remaining = size
        total_value = Decimal("0")

        for level in self.bids:
            if remaining <= 0:
                break
            fill_size = min(remaining, level.size)
            total_value += fill_size * level.price
            remaining -= fill_size

        if remaining > 0:
            return None  # Insufficient liquidity

        return total_value / size if size > 0 else None

    def volume_weighted_ask(self, size: Decimal) -> Optional[Decimal]:
        """Calculate volume-weighted average price to buy a given size.

        Args:
            size: Size to buy (lifting asks).

        Returns:
            VWAP or None if insufficient liquidity.
        """
        remaining = size
        total_value = Decimal("0")

        for level in self.asks:
            if remaining <= 0:
                break
            fill_size = min(remaining, level.size)
            total_value += fill_size * level.price
            remaining -= fill_size

        if remaining > 0:
            return None  # Insufficient liquidity

        return total_value / size if size > 0 else None

    def is_crossed(self) -> bool:
        """Check if order book is crossed (best bid >= best ask).

        A crossed book indicates a data error or arbitrage opportunity.
        """
        if self.best_bid is None or self.best_ask is None:
            return False
        return self.best_bid >= self.best_ask

    @property
    def is_empty(self) -> bool:
        """Check if order book has no levels."""
        return not self.bids and not self.asks

    def to_snapshot(self, levels: int = 10) -> dict:
        """Convert to serializable snapshot.

        Args:
            levels: Number of levels to include.

        Returns:
            Dictionary representation suitable for EventBus publishing.
        """
        return {
            "token_id": self.token_id,
            "timestamp": self.last_update.isoformat(),
            "sequence": self.sequence,
            "best_bid": str(self.best_bid) if self.best_bid else None,
            "best_ask": str(self.best_ask) if self.best_ask else None,
            "best_bid_size": str(self.best_bid_size),
            "best_ask_size": str(self.best_ask_size),
            "spread": str(self.spread) if self.spread else None,
            "midpoint": str(self.midpoint) if self.midpoint else None,
            "bid_depth": [
                {"price": str(l.price), "size": str(l.size)}
                for l in self.bid_depth(levels)
            ],
            "ask_depth": [
                {"price": str(l.price), "size": str(l.size)}
                for l in self.ask_depth(levels)
            ],
        }


@dataclass
class MarketOrderBook:
    """Combined order book for a binary market (YES + NO tokens).

    Provides a unified view of both sides of a Polymarket market,
    with arbitrage detection and combined metrics.
    """

    market_id: str
    yes_book: InMemoryOrderBook
    no_book: InMemoryOrderBook
    last_update: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def create(cls, market_id: str, yes_token_id: str, no_token_id: str) -> "MarketOrderBook":
        """Factory method to create a new MarketOrderBook.

        Args:
            market_id: The market's condition ID.
            yes_token_id: Token ID for YES outcome.
            no_token_id: Token ID for NO outcome.

        Returns:
            New MarketOrderBook instance.
        """
        return cls(
            market_id=market_id,
            yes_book=InMemoryOrderBook(token_id=yes_token_id),
            no_book=InMemoryOrderBook(token_id=no_token_id),
        )

    @property
    def yes_best_bid(self) -> Optional[Decimal]:
        """Get best YES bid."""
        return self.yes_book.best_bid

    @property
    def yes_best_ask(self) -> Optional[Decimal]:
        """Get best YES ask."""
        return self.yes_book.best_ask

    @property
    def no_best_bid(self) -> Optional[Decimal]:
        """Get best NO bid."""
        return self.no_book.best_bid

    @property
    def no_best_ask(self) -> Optional[Decimal]:
        """Get best NO ask."""
        return self.no_book.best_ask

    @property
    def combined_ask(self) -> Optional[Decimal]:
        """Get combined ask (YES ask + NO ask).

        In a binary market, if combined_ask < 1.0, there's an arbitrage opportunity.
        """
        if self.yes_best_ask is not None and self.no_best_ask is not None:
            return self.yes_best_ask + self.no_best_ask
        return None

    @property
    def combined_bid(self) -> Optional[Decimal]:
        """Get combined bid (YES bid + NO bid).

        If combined_bid > 1.0, there's a reverse arbitrage opportunity.
        """
        if self.yes_best_bid is not None and self.no_best_bid is not None:
            return self.yes_best_bid + self.no_best_bid
        return None

    @property
    def arbitrage_spread(self) -> Optional[Decimal]:
        """Get arbitrage spread (1.0 - combined_ask).

        Positive spread indicates profitable arbitrage opportunity.
        """
        combined = self.combined_ask
        if combined is not None:
            return Decimal("1") - combined
        return None

    @property
    def arbitrage_spread_cents(self) -> Optional[Decimal]:
        """Get arbitrage spread in cents."""
        spread = self.arbitrage_spread
        if spread is not None:
            return spread * Decimal("100")
        return None

    @property
    def has_arbitrage(self) -> bool:
        """Check if there's a profitable arbitrage opportunity."""
        spread = self.arbitrage_spread
        return spread is not None and spread > Decimal("0")

    def update_last_update(self) -> None:
        """Update the last_update timestamp to the most recent book update."""
        self.last_update = max(self.yes_book.last_update, self.no_book.last_update)

    def to_snapshot(self, levels: int = 5) -> dict:
        """Convert to serializable snapshot.

        Args:
            levels: Number of levels to include per side.

        Returns:
            Dictionary representation for EventBus publishing.
        """
        self.update_last_update()
        return {
            "market_id": self.market_id,
            "timestamp": self.last_update.isoformat(),
            "yes_best_bid": str(self.yes_best_bid) if self.yes_best_bid else None,
            "yes_best_ask": str(self.yes_best_ask) if self.yes_best_ask else None,
            "no_best_bid": str(self.no_best_bid) if self.no_best_bid else None,
            "no_best_ask": str(self.no_best_ask) if self.no_best_ask else None,
            "combined_ask": str(self.combined_ask) if self.combined_ask else None,
            "arbitrage_spread_cents": str(self.arbitrage_spread_cents) if self.arbitrage_spread_cents else None,
            "has_arbitrage": self.has_arbitrage,
            "yes_book": self.yes_book.to_snapshot(levels),
            "no_book": self.no_book.to_snapshot(levels),
        }
