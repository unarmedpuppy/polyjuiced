"""Data models for liquidity collection.

These models capture fill records and depth snapshots for building
persistence and slippage models.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple


@dataclass
class DepthLevel:
    """A single level in the order book."""

    price: float
    size: float

    def to_tuple(self) -> Tuple[float, float]:
        """Convert to (price, size) tuple."""
        return (self.price, self.size)


@dataclass
class LiquiditySnapshot:
    """Point-in-time snapshot of order book depth.

    Used to measure depth persistence over time. By comparing snapshots
    before and after fills, we can calculate how much displayed depth
    actually persists when touched.

    Attributes:
        timestamp: When the snapshot was taken (UTC)
        token_id: The token (YES or NO) this snapshot is for
        condition_id: Market condition ID
        asset: Asset symbol (BTC, ETH, SOL)
        bid_levels: List of (price, size) tuples for bids
        ask_levels: List of (price, size) tuples for asks
        total_bid_depth: Sum of all bid sizes (for quick lookup)
        total_ask_depth: Sum of all ask sizes (for quick lookup)
    """

    timestamp: datetime
    token_id: str
    condition_id: str
    asset: str
    bid_levels: List[DepthLevel] = field(default_factory=list)
    ask_levels: List[DepthLevel] = field(default_factory=list)
    total_bid_depth: float = 0.0
    total_ask_depth: float = 0.0

    def __post_init__(self):
        """Calculate totals if not provided."""
        if self.total_bid_depth == 0.0 and self.bid_levels:
            self.total_bid_depth = sum(level.size for level in self.bid_levels)
        if self.total_ask_depth == 0.0 and self.ask_levels:
            self.total_ask_depth = sum(level.size for level in self.ask_levels)

    @classmethod
    def from_order_book(
        cls,
        order_book: dict,
        token_id: str,
        condition_id: str,
        asset: str,
        max_levels: int = 10,
    ) -> "LiquiditySnapshot":
        """Create a snapshot from raw order book data.

        Args:
            order_book: Raw order book dict from API
            token_id: Token ID
            condition_id: Market condition ID
            asset: Asset symbol
            max_levels: Maximum number of levels to capture

        Returns:
            LiquiditySnapshot instance
        """
        bids = order_book.get("bids", [])
        asks = order_book.get("asks", [])

        bid_levels = []
        for bid in bids[:max_levels]:
            price = float(bid.get("price", 0))
            size = float(bid.get("size", 0))
            if price > 0 and size > 0:
                bid_levels.append(DepthLevel(price=price, size=size))

        ask_levels = []
        for ask in asks[:max_levels]:
            price = float(ask.get("price", 0))
            size = float(ask.get("size", 0))
            if price > 0 and size > 0:
                ask_levels.append(DepthLevel(price=price, size=size))

        return cls(
            timestamp=datetime.utcnow(),
            token_id=token_id,
            condition_id=condition_id,
            asset=asset,
            bid_levels=bid_levels,
            ask_levels=ask_levels,
        )

    def get_depth_at_price(self, price: float, side: str = "ask") -> float:
        """Get cumulative depth up to a given price.

        Args:
            price: Price threshold
            side: "bid" or "ask"

        Returns:
            Total size available at or better than price
        """
        levels = self.ask_levels if side == "ask" else self.bid_levels
        total = 0.0

        for level in levels:
            if side == "ask" and level.price <= price:
                total += level.size
            elif side == "bid" and level.price >= price:
                total += level.size

        return total


@dataclass
class FillRecord:
    """Record of an order fill with slippage data.

    Used to build slippage curves by correlating intended vs actual execution.
    Each fill captures pre-fill depth, intended parameters, and actual results.

    Attributes:
        timestamp: When the fill occurred (UTC)
        token_id: Token that was traded
        condition_id: Market condition ID
        asset: Asset symbol (BTC, ETH, SOL)
        side: "BUY" or "SELL"
        intended_size: Shares we intended to trade
        filled_size: Shares actually filled
        intended_price: Price we tried to get
        actual_avg_price: Average fill price achieved
        time_to_fill_ms: Milliseconds from order submit to fill
        slippage: actual_price - intended_price (positive = worse for buyer)
        pre_fill_depth: Depth available before our order
        post_fill_depth: Depth remaining after our order (if captured)
        order_type: "GTC", "FOK", etc.
        order_id: Exchange order ID
        fill_ratio: filled_size / intended_size (1.0 = complete fill)
        persistence_ratio: filled_size / pre_fill_depth (how much depth we consumed)
    """

    timestamp: datetime
    token_id: str
    condition_id: str
    asset: str
    side: str
    intended_size: float
    filled_size: float
    intended_price: float
    actual_avg_price: float
    time_to_fill_ms: int
    slippage: float
    pre_fill_depth: float
    post_fill_depth: Optional[float] = None
    order_type: str = "GTC"
    order_id: Optional[str] = None
    fill_ratio: float = 0.0
    persistence_ratio: float = 0.0

    def __post_init__(self):
        """Calculate derived fields."""
        if self.intended_size > 0:
            self.fill_ratio = self.filled_size / self.intended_size

        if self.pre_fill_depth > 0:
            self.persistence_ratio = self.filled_size / self.pre_fill_depth

    @classmethod
    def from_execution(
        cls,
        token_id: str,
        condition_id: str,
        asset: str,
        side: str,
        intended_size: float,
        intended_price: float,
        pre_fill_depth: float,
        order_result: dict,
        start_time_ms: int,
    ) -> "FillRecord":
        """Create a fill record from execution result.

        Args:
            token_id: Token traded
            condition_id: Market condition ID
            asset: Asset symbol
            side: "BUY" or "SELL"
            intended_size: Shares we wanted
            intended_price: Price we wanted
            pre_fill_depth: Depth before our order
            order_result: Result dict from exchange
            start_time_ms: Timestamp when order was submitted

        Returns:
            FillRecord instance
        """
        import time

        now_ms = int(time.time() * 1000)
        time_to_fill = now_ms - start_time_ms

        # Extract fill info from order result
        # Note: py-clob-client returns different structures depending on order type
        filled_size = float(order_result.get("size", 0) or order_result.get("matched_size", 0) or 0)
        avg_price = float(order_result.get("price", intended_price) or intended_price)

        # Handle case where fill info is nested
        if "fills" in order_result:
            fills = order_result["fills"]
            if fills:
                total_value = sum(float(f.get("price", 0)) * float(f.get("size", 0)) for f in fills)
                total_size = sum(float(f.get("size", 0)) for f in fills)
                if total_size > 0:
                    avg_price = total_value / total_size
                    filled_size = total_size

        # Calculate slippage (positive = worse for buyer)
        if side.upper() == "BUY":
            slippage = avg_price - intended_price
        else:
            slippage = intended_price - avg_price

        return cls(
            timestamp=datetime.utcnow(),
            token_id=token_id,
            condition_id=condition_id,
            asset=asset,
            side=side.upper(),
            intended_size=intended_size,
            filled_size=filled_size,
            intended_price=intended_price,
            actual_avg_price=avg_price,
            time_to_fill_ms=time_to_fill,
            slippage=slippage,
            pre_fill_depth=pre_fill_depth,
            order_type=order_result.get("order_type", "GTC"),
            order_id=order_result.get("id") or order_result.get("order_id"),
        )


@dataclass
class LiquidityMetrics:
    """Aggregated liquidity metrics for a token/market.

    Computed from historical snapshots and fills.
    """

    token_id: str
    condition_id: str
    asset: str
    # From snapshots
    avg_ask_depth: float = 0.0
    avg_bid_depth: float = 0.0
    depth_volatility: float = 0.0  # std dev of depth over time
    # From fills
    avg_slippage_per_10_shares: float = 0.0
    persistence_estimate: float = 0.4  # default conservative
    fill_rate: float = 1.0  # fraction of orders that fill
    # Computed
    sample_count: int = 0
    last_updated: Optional[datetime] = None
