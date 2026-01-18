"""
Order domain models.

These models represent orders, fills, and positions.
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional
import uuid


class OrderSide(str, Enum):
    """Order side (buy or sell)."""
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    """Order lifecycle status."""
    PENDING = "pending"
    SUBMITTED = "submitted"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class OrderType(str, Enum):
    """Order type."""
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    FOK = "FOK"  # Fill or Kill
    GTC = "GTC"  # Good til Cancelled


@dataclass
class OrderRequest:
    """Request to place an order."""
    market_id: str
    token_id: str
    side: OrderSide
    outcome: str  # "YES" or "NO"
    size: Decimal
    price: Decimal
    order_type: OrderType = OrderType.GTC
    client_order_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        if self.outcome not in ("YES", "NO"):
            raise ValueError(f"outcome must be YES or NO, got {self.outcome}")
        if self.size <= 0:
            raise ValueError(f"size must be positive, got {self.size}")
        if not (0 < self.price < 1):
            raise ValueError(f"price must be between 0 and 1 exclusive, got {self.price}")


@dataclass
class Order:
    """An order in the system."""
    order_id: str
    market_id: str
    token_id: str
    side: OrderSide
    outcome: str
    requested_size: Decimal
    filled_size: Decimal
    price: Decimal
    status: OrderStatus
    order_type: OrderType = OrderType.GTC
    client_order_id: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def remaining_size(self) -> Decimal:
        """Get unfilled size."""
        return self.requested_size - self.filled_size

    @property
    def fill_ratio(self) -> Decimal:
        """Get percentage filled."""
        if self.requested_size == 0:
            return Decimal("0")
        return self.filled_size / self.requested_size

    @property
    def is_complete(self) -> bool:
        """Check if order is in a terminal state."""
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        )


@dataclass
class Fill:
    """A fill (partial or complete) of an order."""
    fill_id: str
    order_id: str
    market_id: str
    token_id: str
    side: OrderSide
    outcome: str
    size: Decimal
    price: Decimal
    fee: Decimal = Decimal("0")
    timestamp: datetime = field(default_factory=datetime.utcnow)

    @property
    def cost(self) -> Decimal:
        """Get total cost including fee."""
        return (self.size * self.price) + self.fee


@dataclass
class OrderResult:
    """Result of an order execution attempt."""
    success: bool
    order: Optional[Order]
    fills: list[Fill] = field(default_factory=list)
    error_message: Optional[str] = None
    latency_ms: float = 0.0

    @property
    def total_filled(self) -> Decimal:
        """Get total size filled across all fills."""
        return sum((f.size for f in self.fills), Decimal("0"))

    @property
    def total_cost(self) -> Decimal:
        """Get total cost including fees."""
        return sum((f.cost for f in self.fills), Decimal("0"))


@dataclass
class DualLegResult:
    """Result of a dual-leg (arbitrage) order execution."""
    success: bool
    yes_result: Optional[OrderResult]
    no_result: Optional[OrderResult]
    error_message: Optional[str] = None
    total_latency_ms: float = 0.0

    @property
    def total_cost(self) -> Decimal:
        """Get combined cost of both legs."""
        cost = Decimal("0")
        if self.yes_result:
            cost += self.yes_result.total_cost
        if self.no_result:
            cost += self.no_result.total_cost
        return cost


class PositionStatus(str, Enum):
    """Position lifecycle status."""
    OPEN = "open"
    CLOSED = "closed"
    PENDING_SETTLEMENT = "pending_settlement"
    SETTLED = "settled"


@dataclass
class Position:
    """A position in a market."""
    position_id: str
    market_id: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    yes_size: Decimal
    no_size: Decimal
    yes_avg_price: Decimal
    no_avg_price: Decimal
    status: PositionStatus = PositionStatus.OPEN
    strategy_name: str = ""
    opened_at: datetime = field(default_factory=datetime.utcnow)
    closed_at: Optional[datetime] = None
    realized_pnl: Decimal = Decimal("0")
    settlement_proceeds: Decimal = Decimal("0")

    @property
    def total_cost(self) -> Decimal:
        """Get total cost basis of position."""
        return (self.yes_size * self.yes_avg_price) + (self.no_size * self.no_avg_price)

    @property
    def is_hedged(self) -> bool:
        """Check if position is fully hedged (equal YES and NO)."""
        return self.yes_size == self.no_size and self.yes_size > 0

    @property
    def net_exposure(self) -> Decimal:
        """Get net exposure (positive = long YES, negative = long NO)."""
        return self.yes_size - self.no_size

    @property
    def guaranteed_pnl(self) -> Decimal:
        """Calculate guaranteed P&L for hedged positions.

        If YES=NO size, one side always pays out 1.0 per share.
        Guaranteed PnL = min(yes_size, no_size) * 1.0 - total_cost
        """
        hedged_size = min(self.yes_size, self.no_size)
        if hedged_size == 0:
            return Decimal("0")
        payout = hedged_size * Decimal("1")
        cost = (hedged_size * self.yes_avg_price) + (hedged_size * self.no_avg_price)
        return payout - cost
