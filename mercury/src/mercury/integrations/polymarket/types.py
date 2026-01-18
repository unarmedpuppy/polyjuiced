"""Polymarket-specific types and data models.

This module contains all the type definitions needed for interacting with
Polymarket's CLOB, Gamma API, and WebSocket services.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional


class OrderSide(str, Enum):
    """Side of an order (buy or sell)."""

    BUY = "BUY"
    SELL = "SELL"


class TokenSide(str, Enum):
    """Token side in a binary market."""

    YES = "YES"
    NO = "NO"


class OrderStatus(str, Enum):
    """Status of an order on the CLOB."""

    LIVE = "LIVE"           # On book, waiting for fill
    MATCHED = "MATCHED"     # Fill in progress
    FILLED = "FILLED"       # Completely filled
    CANCELLED = "CANCELLED" # Cancelled by user
    EXPIRED = "EXPIRED"     # Expired without fill


class TimeInForce(str, Enum):
    """Order time-in-force policies."""

    GTC = "GTC"  # Good-till-cancelled
    FOK = "FOK"  # Fill-or-kill (all or nothing)
    GTD = "GTD"  # Good-till-date


@dataclass(frozen=True)
class PolymarketSettings:
    """Configuration settings for Polymarket connections.

    Attributes:
        private_key: Polygon wallet private key for signing transactions.
        proxy_wallet: Optional Polymarket proxy wallet address.
        signature_type: Signature type (0=EOA, 1=Magic, 2=Browser).
        api_key: CLOB API key for authenticated requests.
        api_secret: CLOB API secret.
        api_passphrase: CLOB API passphrase.
        clob_url: CLOB HTTP API base URL.
        gamma_url: Gamma API base URL for market discovery.
        ws_url: WebSocket URL for real-time market data.
        polygon_rpc_url: Polygon RPC URL for chain interactions.
        http_proxy: Optional HTTP proxy for routing requests.
    """

    private_key: str
    proxy_wallet: Optional[str] = None
    signature_type: int = 0  # 0=EOA by default

    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""

    clob_url: str = "https://clob.polymarket.com/"
    gamma_url: str = "https://gamma-api.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    polygon_rpc_url: str = "https://polygon-rpc.com"

    http_proxy: Optional[str] = None


@dataclass(frozen=True)
class MarketInfo:
    """Basic market information from Gamma API.

    Represents a Polymarket market (question) with its outcomes and tokens.
    """

    condition_id: str
    question_id: str
    question: str
    slug: str

    # Token IDs for each outcome
    yes_token_id: str
    no_token_id: str

    # Current prices (0.0 to 1.0)
    yes_price: Decimal
    no_price: Decimal

    # Market metadata
    active: bool
    closed: bool
    resolved: bool
    resolution: Optional[str] = None  # "YES", "NO", or None if unresolved

    end_date: Optional[datetime] = None
    volume: Decimal = Decimal("0")
    liquidity: Decimal = Decimal("0")

    # Parent event info
    event_slug: Optional[str] = None
    event_title: Optional[str] = None


@dataclass(frozen=True)
class Market15Min:
    """A 15-minute up/down binary market.

    These markets resolve based on whether an asset's price goes up or down
    within a 15-minute window.
    """

    condition_id: str
    asset: str  # e.g., "BTC", "ETH", "SOL"

    yes_token_id: str  # "Up" outcome token
    no_token_id: str   # "Down" outcome token

    yes_price: Decimal
    no_price: Decimal

    start_time: datetime
    end_time: datetime

    slug: str

    @property
    def combined_price(self) -> Decimal:
        """Sum of YES and NO prices. Should be ~1.0, <1.0 indicates arb."""
        return self.yes_price + self.no_price

    @property
    def spread_cents(self) -> Decimal:
        """Arbitrage spread in cents (100 = $1.00)."""
        return (Decimal("1.0") - self.combined_price) * 100


@dataclass(frozen=True)
class OrderBookLevel:
    """A single price level in an order book.

    Attributes:
        price: Price in dollars (0.0 to 1.0 for Polymarket).
        size: Number of shares available at this price.
    """

    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class OrderBookData:
    """Order book for a single token.

    Contains bids (buy orders) and asks (sell orders) sorted by price.
    """

    token_id: str
    timestamp: datetime

    # Sorted by price: bids highest-first, asks lowest-first
    bids: tuple[OrderBookLevel, ...] = field(default_factory=tuple)
    asks: tuple[OrderBookLevel, ...] = field(default_factory=tuple)

    @property
    def best_bid(self) -> Optional[Decimal]:
        """Highest bid price, or None if no bids."""
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[Decimal]:
        """Lowest ask price, or None if no asks."""
        return self.asks[0].price if self.asks else None

    @property
    def best_bid_size(self) -> Decimal:
        """Size at best bid, or 0 if no bids."""
        return self.bids[0].size if self.bids else Decimal("0")

    @property
    def best_ask_size(self) -> Decimal:
        """Size at best ask, or 0 if no asks."""
        return self.asks[0].size if self.asks else Decimal("0")

    @property
    def midpoint(self) -> Optional[Decimal]:
        """Midpoint between best bid and ask, or None if either missing."""
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    @property
    def spread(self) -> Optional[Decimal]:
        """Bid-ask spread, or None if either missing."""
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    def depth_at_levels(self, levels: int = 3) -> Decimal:
        """Calculate total available size in top N levels (bids + asks)."""
        bid_depth = sum(level.size for level in self.bids[:levels])
        ask_depth = sum(level.size for level in self.asks[:levels])
        return bid_depth + ask_depth


@dataclass(frozen=True)
class OrderBookSnapshot:
    """Combined order book snapshot for a binary market (YES + NO).

    Used for arbitrage detection where we need to see both sides simultaneously.
    """

    market_id: str  # condition_id
    timestamp: datetime

    yes_book: OrderBookData
    no_book: OrderBookData

    @property
    def combined_ask(self) -> Optional[Decimal]:
        """Sum of best ask for YES and NO. <1.0 indicates arbitrage opportunity."""
        if self.yes_book.best_ask is not None and self.no_book.best_ask is not None:
            return self.yes_book.best_ask + self.no_book.best_ask
        return None

    @property
    def arbitrage_spread_cents(self) -> Optional[Decimal]:
        """Arbitrage spread in cents. Positive = profitable arbitrage."""
        if self.combined_ask is not None:
            return (Decimal("1.0") - self.combined_ask) * 100
        return None

    @property
    def has_arbitrage(self) -> bool:
        """Whether there's a profitable arbitrage opportunity (spread > 0)."""
        spread = self.arbitrage_spread_cents
        return spread is not None and spread > Decimal("0")


@dataclass(frozen=True)
class TokenPrice:
    """Real-time price update for a single token.

    Received via WebSocket when order book changes.
    """

    token_id: str
    timestamp: datetime

    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None

    @property
    def midpoint(self) -> Optional[Decimal]:
        """Midpoint price if both bid and ask available."""
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        return None


@dataclass(frozen=True)
class OrderResult:
    """Result of placing an order on the CLOB.

    Tracks the order lifecycle from submission to fill/cancellation.
    """

    order_id: str
    token_id: str
    side: OrderSide
    status: OrderStatus

    # Requested parameters
    requested_price: Decimal
    requested_size: Decimal

    # Fill information (if any)
    filled_size: Decimal = Decimal("0")
    filled_cost: Decimal = Decimal("0")

    # Execution metadata
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = None

    # Timing for latency tracking
    submit_time_ms: Optional[float] = None
    response_time_ms: Optional[float] = None

    @property
    def fill_ratio(self) -> Decimal:
        """Fraction of requested size that was filled (0.0 to 1.0)."""
        if self.requested_size == 0:
            return Decimal("0")
        return self.filled_size / self.requested_size

    @property
    def average_fill_price(self) -> Optional[Decimal]:
        """Average price per share filled, or None if nothing filled."""
        if self.filled_size > 0:
            return self.filled_cost / self.filled_size
        return None

    @property
    def is_complete(self) -> bool:
        """Whether order lifecycle is complete (filled, cancelled, or expired)."""
        return self.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.EXPIRED)

    @property
    def latency_ms(self) -> Optional[float]:
        """Round-trip latency in milliseconds, if timing data available."""
        if self.submit_time_ms is not None and self.response_time_ms is not None:
            return self.response_time_ms - self.submit_time_ms
        return None


@dataclass(frozen=True)
class DualLegOrderResult:
    """Result of placing a dual-leg arbitrage order (YES + NO).

    Both legs must fill for the arbitrage to be successful. Partial fills
    create unhedged exposure that must be managed.
    """

    yes_result: OrderResult
    no_result: OrderResult

    market_id: str
    timestamp: datetime

    # Pre-execution snapshots for analysis
    pre_execution_yes_depth: Optional[Decimal] = None
    pre_execution_no_depth: Optional[Decimal] = None

    # Total execution time
    execution_time_ms: Optional[float] = None

    @property
    def both_filled(self) -> bool:
        """Whether both legs completely filled."""
        return (
            self.yes_result.status == OrderStatus.FILLED and
            self.no_result.status == OrderStatus.FILLED
        )

    @property
    def has_partial_fill(self) -> bool:
        """Whether one leg filled but not the other."""
        yes_filled = self.yes_result.filled_size > 0
        no_filled = self.no_result.filled_size > 0
        return yes_filled != no_filled or (
            yes_filled and no_filled and not self.both_filled
        )

    @property
    def total_cost(self) -> Decimal:
        """Total cost of both legs."""
        return self.yes_result.filled_cost + self.no_result.filled_cost

    @property
    def total_shares(self) -> Decimal:
        """Minimum shares from both legs (the hedged amount)."""
        return min(self.yes_result.filled_size, self.no_result.filled_size)

    @property
    def guaranteed_pnl(self) -> Decimal:
        """Guaranteed P&L from hedged portion. Positive = profit."""
        if self.total_shares == 0:
            return Decimal("0")
        # Each fully hedged share guarantees $1.00 at resolution
        return self.total_shares - self.total_cost

    @property
    def unhedged_yes_shares(self) -> Decimal:
        """Number of YES shares without matching NO shares."""
        return max(Decimal("0"), self.yes_result.filled_size - self.no_result.filled_size)

    @property
    def unhedged_no_shares(self) -> Decimal:
        """Number of NO shares without matching YES shares."""
        return max(Decimal("0"), self.no_result.filled_size - self.yes_result.filled_size)


@dataclass(frozen=True)
class WebSocketMessage:
    """Parsed message from Polymarket WebSocket.

    Polymarket sends various message formats; this normalizes them.
    """

    message_type: str  # "price_change", "book", "trade", "heartbeat"
    token_id: str
    timestamp: datetime

    # For price changes
    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None

    # For full book updates
    bids: Optional[tuple[OrderBookLevel, ...]] = None
    asks: Optional[tuple[OrderBookLevel, ...]] = None

    # Raw data for debugging
    raw_data: Optional[dict] = None


@dataclass(frozen=True)
class PositionInfo:
    """Current position in a token from CLOB API."""

    token_id: str
    market_id: str
    size: Decimal
    average_price: Decimal
    side: TokenSide

    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")

    @property
    def cost_basis(self) -> Decimal:
        """Total cost paid for this position."""
        return self.size * self.average_price
