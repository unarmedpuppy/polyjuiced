"""
Market domain models.

These models represent Polymarket markets, tokens, and order books.
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional


class MarketStatus(str, Enum):
    """Market lifecycle status."""
    ACTIVE = "active"
    CLOSED = "closed"
    RESOLVED = "resolved"


@dataclass(frozen=True)
class Token:
    """Represents a YES or NO token in a market."""
    token_id: str
    outcome: str  # "YES" or "NO"
    price: Decimal = Decimal("0.5")

    def __post_init__(self) -> None:
        if self.outcome not in ("YES", "NO"):
            raise ValueError(f"outcome must be YES or NO, got {self.outcome}")


@dataclass(frozen=True)
class OrderBookLevel:
    """Single level in an order book (price + size)."""
    price: Decimal
    size: Decimal

    def __post_init__(self) -> None:
        if self.price < 0 or self.price > 1:
            raise ValueError(f"price must be between 0 and 1, got {self.price}")
        if self.size < 0:
            raise ValueError(f"size must be non-negative, got {self.size}")


@dataclass
class OrderBook:
    """Order book state for a token."""
    token_id: str
    timestamp: datetime
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)

    @property
    def best_bid(self) -> Optional[Decimal]:
        """Get best bid price, or None if no bids."""
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[Decimal]:
        """Get best ask price, or None if no asks."""
        return self.asks[0].price if self.asks else None

    @property
    def mid_price(self) -> Optional[Decimal]:
        """Get mid price between best bid and ask."""
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    @property
    def spread(self) -> Optional[Decimal]:
        """Get bid-ask spread."""
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None


@dataclass
class Market:
    """Represents a Polymarket prediction market."""
    market_id: str
    condition_id: str
    question: str
    description: str
    yes_token: Token
    no_token: Token
    status: MarketStatus = MarketStatus.ACTIVE
    end_date: Optional[datetime] = None
    resolution_date: Optional[datetime] = None
    winning_outcome: Optional[str] = None
    volume_24h: Decimal = Decimal("0")
    liquidity: Decimal = Decimal("0")
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def is_active(self) -> bool:
        """Check if market is actively trading."""
        return self.status == MarketStatus.ACTIVE

    @property
    def is_resolved(self) -> bool:
        """Check if market has resolved."""
        return self.status == MarketStatus.RESOLVED

    def get_token(self, outcome: str) -> Token:
        """Get token by outcome (YES or NO)."""
        if outcome == "YES":
            return self.yes_token
        elif outcome == "NO":
            return self.no_token
        else:
            raise ValueError(f"Invalid outcome: {outcome}")


@dataclass
class OrderBookSnapshot:
    """Complete order book snapshot for both sides of a market."""
    market_id: str
    timestamp: datetime
    yes_best_bid: Optional[Decimal]
    yes_best_ask: Optional[Decimal]
    no_best_bid: Optional[Decimal]
    no_best_ask: Optional[Decimal]
    yes_depth: list[OrderBookLevel] = field(default_factory=list)
    no_depth: list[OrderBookLevel] = field(default_factory=list)

    @property
    def combined_ask(self) -> Optional[Decimal]:
        """Get combined ask price (yes_ask + no_ask) for arbitrage detection."""
        if self.yes_best_ask is not None and self.no_best_ask is not None:
            return self.yes_best_ask + self.no_best_ask
        return None

    @property
    def has_arbitrage_opportunity(self) -> bool:
        """Check if combined ask < 1 (arbitrage opportunity)."""
        combined = self.combined_ask
        return combined is not None and combined < Decimal("1")

    @property
    def arbitrage_spread(self) -> Optional[Decimal]:
        """Get arbitrage spread (1 - combined_ask) if opportunity exists."""
        combined = self.combined_ask
        if combined is not None:
            return Decimal("1") - combined
        return None
