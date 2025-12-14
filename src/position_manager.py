"""Active Position Management for Polymarket Bot.

This module implements active trade management, moving from "fire-and-forget"
to continuous position monitoring and rebalancing.

Primary Goal: EQUAL SIZED positions for YES & NO with minimum $0.02 arbitrage spread.

See docs/REBALANCING_STRATEGY.md for full design documentation.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from .client.polymarket import PolymarketClient
    from .monitoring.market_finder import Market15Min
    from .monitoring.order_book import MarketState
    from .persistence import Database

log = structlog.get_logger()


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class RebalancingConfig:
    """Configuration for position rebalancing."""

    # Minimum hedge ratio before seeking rebalancing (80%)
    rebalance_threshold: float = 0.80

    # Minimum profit per share to execute rebalance ($0.02)
    min_profit_per_share: float = 0.02

    # Don't rebalance in last N seconds before resolution
    max_rebalance_wait_seconds: float = 60.0

    # When both sell and buy are profitable, prefer selling (capital efficient)
    prefer_sell_over_buy: bool = True

    # Allow partial rebalancing if full balance not available
    allow_partial_rebalance: bool = True

    # Maximum rebalancing trades per position
    max_rebalance_trades: int = 5

    # Maximum position size (total cost) for budget calculations
    max_position_size_usd: float = 25.0

    # Minimum spread to maintain during rebalancing ($0.02)
    min_spread_dollars: float = 0.02


# =============================================================================
# Telemetry Data Classes
# =============================================================================

class TelemetryEvent(str, Enum):
    """Event types for timing telemetry."""
    OPPORTUNITY_DETECTED = "telemetry.opportunity_detected"
    ORDER_PLACED = "telemetry.order_placed"
    ORDER_FILLED = "telemetry.order_filled"
    REBALANCE_STARTED = "telemetry.rebalance_started"
    REBALANCE_ATTEMPTED = "telemetry.rebalance_attempted"
    REBALANCE_SUCCEEDED = "telemetry.rebalance_succeeded"
    REBALANCE_FAILED = "telemetry.rebalance_failed"
    POSITION_BALANCED = "telemetry.position_balanced"
    TRADE_RESOLVED = "telemetry.trade_resolved"


@dataclass
class RebalanceTrade:
    """Record of a single rebalancing trade."""
    attempted_at: datetime
    action: str  # SELL_YES, BUY_NO, SELL_NO, BUY_YES
    shares: float
    price: float
    status: str  # SUCCESS, FAILED, PARTIAL
    filled_shares: float = 0.0
    profit: float = 0.0
    error: Optional[str] = None
    order_id: Optional[str] = None


@dataclass
class TradeTelemetry:
    """Timing telemetry for trade execution analysis.

    Tracks timing from opportunity detection through execution to understand:
    - How fast are we executing?
    - Are we missing opportunities due to latency?
    - How long does rebalancing take?
    - What's the average time to achieve balanced positions?
    """
    trade_id: str

    # Opportunity Phase
    opportunity_detected_at: datetime
    opportunity_spread: float  # Spread at detection time (in cents)
    opportunity_yes_price: float  # YES price at detection
    opportunity_no_price: float  # NO price at detection

    # Execution Phase
    order_placed_at: Optional[datetime] = None
    order_filled_at: Optional[datetime] = None
    execution_latency_ms: Optional[float] = None  # placed_at - detected_at
    fill_latency_ms: Optional[float] = None  # filled_at - placed_at

    # Position State at Fill
    initial_yes_shares: float = 0.0
    initial_no_shares: float = 0.0
    initial_hedge_ratio: float = 0.0

    # Rebalancing Phase
    rebalance_started_at: Optional[datetime] = None
    rebalance_attempts: int = 0
    rebalance_trades: List[RebalanceTrade] = field(default_factory=list)
    position_balanced_at: Optional[datetime] = None

    # Resolution
    resolved_at: Optional[datetime] = None
    final_yes_shares: float = 0.0
    final_no_shares: float = 0.0
    final_hedge_ratio: float = 0.0
    actual_profit: float = 0.0

    def record_order_placed(self) -> None:
        """Record that orders were placed."""
        self.order_placed_at = datetime.utcnow()
        if self.opportunity_detected_at:
            self.execution_latency_ms = (
                self.order_placed_at - self.opportunity_detected_at
            ).total_seconds() * 1000

    def record_order_filled(
        self,
        yes_shares: float,
        no_shares: float,
    ) -> None:
        """Record that orders were filled."""
        self.order_filled_at = datetime.utcnow()
        self.initial_yes_shares = yes_shares
        self.initial_no_shares = no_shares

        if yes_shares > 0 or no_shares > 0:
            max_shares = max(yes_shares, no_shares)
            min_shares = min(yes_shares, no_shares)
            self.initial_hedge_ratio = min_shares / max_shares if max_shares > 0 else 0.0

        if self.order_placed_at:
            self.fill_latency_ms = (
                self.order_filled_at - self.order_placed_at
            ).total_seconds() * 1000

    def record_rebalance_started(self) -> None:
        """Record that rebalancing has started."""
        self.rebalance_started_at = datetime.utcnow()

    def record_rebalance_attempt(self, trade: RebalanceTrade) -> None:
        """Record a rebalancing attempt."""
        self.rebalance_attempts += 1
        self.rebalance_trades.append(trade)

    def record_position_balanced(
        self,
        yes_shares: float,
        no_shares: float,
    ) -> None:
        """Record that position is now balanced."""
        self.position_balanced_at = datetime.utcnow()
        self.final_yes_shares = yes_shares
        self.final_no_shares = no_shares

        if yes_shares > 0 or no_shares > 0:
            max_shares = max(yes_shares, no_shares)
            min_shares = min(yes_shares, no_shares)
            self.final_hedge_ratio = min_shares / max_shares if max_shares > 0 else 0.0

    def record_resolved(self, profit: float) -> None:
        """Record trade resolution."""
        self.resolved_at = datetime.utcnow()
        self.actual_profit = profit

    @property
    def total_execution_time_ms(self) -> Optional[float]:
        """Time from detection to fill."""
        if self.order_filled_at and self.opportunity_detected_at:
            return (
                self.order_filled_at - self.opportunity_detected_at
            ).total_seconds() * 1000
        return None

    @property
    def time_to_balance_ms(self) -> Optional[float]:
        """Time from initial fill to balanced position."""
        if self.position_balanced_at and self.order_filled_at:
            return (
                self.position_balanced_at - self.order_filled_at
            ).total_seconds() * 1000
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for database storage."""
        return {
            "trade_id": self.trade_id,
            "opportunity_detected_at": self.opportunity_detected_at.isoformat() if self.opportunity_detected_at else None,
            "opportunity_spread": self.opportunity_spread,
            "opportunity_yes_price": self.opportunity_yes_price,
            "opportunity_no_price": self.opportunity_no_price,
            "order_placed_at": self.order_placed_at.isoformat() if self.order_placed_at else None,
            "order_filled_at": self.order_filled_at.isoformat() if self.order_filled_at else None,
            "execution_latency_ms": self.execution_latency_ms,
            "fill_latency_ms": self.fill_latency_ms,
            "initial_yes_shares": self.initial_yes_shares,
            "initial_no_shares": self.initial_no_shares,
            "initial_hedge_ratio": self.initial_hedge_ratio,
            "rebalance_started_at": self.rebalance_started_at.isoformat() if self.rebalance_started_at else None,
            "rebalance_attempts": self.rebalance_attempts,
            "position_balanced_at": self.position_balanced_at.isoformat() if self.position_balanced_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "final_yes_shares": self.final_yes_shares,
            "final_no_shares": self.final_no_shares,
            "final_hedge_ratio": self.final_hedge_ratio,
            "actual_profit": self.actual_profit,
        }


# =============================================================================
# Rebalancing Option
# =============================================================================

@dataclass
class RebalanceOption:
    """A potential rebalancing action."""
    action: str  # SELL_YES, BUY_NO, SELL_NO, BUY_YES
    shares: float
    price: float
    profit: float  # Expected profit/savings from this action

    @property
    def profit_per_share(self) -> float:
        """Profit per share."""
        return self.profit / self.shares if self.shares > 0 else 0.0

    def __repr__(self) -> str:
        return f"RebalanceOption({self.action}, {self.shares:.2f} @ ${self.price:.3f}, profit=${self.profit:.4f})"


# =============================================================================
# Active Position
# =============================================================================

@dataclass
class ActivePosition:
    """Tracks an active position throughout its lifecycle.

    This is the core data structure for active trade management.
    It tracks position state, handles rebalancing updates, and maintains telemetry.
    """
    trade_id: str
    market: "Market15Min"

    # Position details (mutable as rebalancing occurs)
    yes_shares: float
    no_shares: float
    yes_avg_price: float  # Weighted average entry price
    no_avg_price: float

    # Telemetry
    telemetry: TradeTelemetry

    # Tracking
    created_at: datetime = field(default_factory=datetime.utcnow)
    status: str = "ACTIVE"  # ACTIVE, BALANCED, RESOLVED

    # Rebalancing history
    rebalance_history: List[RebalanceTrade] = field(default_factory=list)

    # Budget tracking
    original_budget: float = 0.0  # Original trade budget for rebalancing capacity

    @property
    def hedge_ratio(self) -> float:
        """Calculate current hedge ratio.

        hedge_ratio = min(yes, no) / max(yes, no)
        1.0 = perfectly balanced
        0.0 = completely one-sided
        """
        if max(self.yes_shares, self.no_shares) == 0:
            return 0.0
        return min(self.yes_shares, self.no_shares) / max(self.yes_shares, self.no_shares)

    @property
    def is_balanced(self) -> bool:
        """Check if position is balanced (hedge ratio >= 80%)."""
        return self.hedge_ratio >= 0.80

    @property
    def needs_rebalancing(self) -> bool:
        """Check if position needs rebalancing."""
        return not self.is_balanced

    @property
    def excess_side(self) -> str:
        """Get the side with excess shares."""
        return "YES" if self.yes_shares > self.no_shares else "NO"

    @property
    def deficit_side(self) -> str:
        """Get the side with fewer shares."""
        return "NO" if self.yes_shares > self.no_shares else "YES"

    @property
    def excess_shares(self) -> float:
        """Get the number of excess shares."""
        return abs(self.yes_shares - self.no_shares)

    @property
    def total_cost(self) -> float:
        """Calculate total cost of position."""
        return (self.yes_shares * self.yes_avg_price) + (self.no_shares * self.no_avg_price)

    @property
    def remaining_budget(self) -> float:
        """Calculate remaining budget for rebalancing.

        Uses remaining capacity from original trade budget.
        """
        return max(0, self.original_budget - self.total_cost)

    @property
    def guaranteed_return(self) -> float:
        """Calculate guaranteed return at resolution.

        At resolution, one side wins and pays $1 per share.
        With hedged position, we're guaranteed min(yes, no) shares paying out.
        """
        return min(self.yes_shares, self.no_shares) * 1.0

    @property
    def expected_profit(self) -> float:
        """Calculate expected profit from current position."""
        return self.guaranteed_return - self.total_cost

    @property
    def resolution_time(self) -> Optional[datetime]:
        """Get market resolution time."""
        return self.market.end_time if hasattr(self.market, "end_time") else None

    @property
    def seconds_to_resolution(self) -> float:
        """Get seconds until market resolution."""
        if self.resolution_time:
            return (self.resolution_time - datetime.utcnow()).total_seconds()
        return float("inf")

    def update_after_sell(
        self,
        side: str,
        shares_sold: float,
        price: float,
    ) -> float:
        """Update position after selling shares.

        Args:
            side: "YES" or "NO"
            shares_sold: Number of shares sold
            price: Sale price per share

        Returns:
            Profit from the sale (price - avg_cost) * shares
        """
        if side == "YES":
            profit = (price - self.yes_avg_price) * shares_sold
            self.yes_shares -= shares_sold
        else:
            profit = (price - self.no_avg_price) * shares_sold
            self.no_shares -= shares_sold

        # Check if now balanced
        if self.is_balanced:
            self.status = "BALANCED"
            self.telemetry.record_position_balanced(self.yes_shares, self.no_shares)

        return profit

    def update_after_buy(
        self,
        side: str,
        shares_bought: float,
        price: float,
    ) -> None:
        """Update position after buying shares.

        Updates weighted average price for the side.

        Args:
            side: "YES" or "NO"
            shares_bought: Number of shares bought
            price: Purchase price per share
        """
        if side == "YES":
            # Update weighted average price
            total_cost = (self.yes_shares * self.yes_avg_price) + (shares_bought * price)
            self.yes_shares += shares_bought
            self.yes_avg_price = total_cost / self.yes_shares if self.yes_shares > 0 else 0
        else:
            total_cost = (self.no_shares * self.no_avg_price) + (shares_bought * price)
            self.no_shares += shares_bought
            self.no_avg_price = total_cost / self.no_shares if self.no_shares > 0 else 0

        # Check if now balanced
        if self.is_balanced:
            self.status = "BALANCED"
            self.telemetry.record_position_balanced(self.yes_shares, self.no_shares)

    def record_rebalance_trade(self, trade: RebalanceTrade) -> None:
        """Record a rebalancing trade."""
        self.rebalance_history.append(trade)
        self.telemetry.record_rebalance_attempt(trade)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/storage."""
        return {
            "trade_id": self.trade_id,
            "asset": self.market.asset,
            "condition_id": self.market.condition_id,
            "yes_shares": self.yes_shares,
            "no_shares": self.no_shares,
            "yes_avg_price": self.yes_avg_price,
            "no_avg_price": self.no_avg_price,
            "hedge_ratio": self.hedge_ratio,
            "status": self.status,
            "total_cost": self.total_cost,
            "expected_profit": self.expected_profit,
            "remaining_budget": self.remaining_budget,
            "rebalance_count": len(self.rebalance_history),
        }


# =============================================================================
# Active Position Manager
# =============================================================================

class ActivePositionManager:
    """Manages all active positions and handles rebalancing.

    Key Features:
    - Tracks ALL open positions (not just imbalanced)
    - Receives real-time price updates via WebSocket callback
    - Immediately evaluates rebalancing opportunities on price change
    - Records full telemetry for analysis

    This is the core of the ACTIVE trade management system.
    """

    def __init__(
        self,
        client: "PolymarketClient",
        db: Optional["Database"] = None,
        config: Optional[RebalancingConfig] = None,
    ):
        """Initialize the position manager.

        Args:
            client: Polymarket client for executing orders
            db: Database for telemetry storage
            config: Rebalancing configuration
        """
        self._client = client
        self._db = db
        self.config = config or RebalancingConfig()

        # Active positions: {trade_id: ActivePosition}
        self.positions: Dict[str, ActivePosition] = {}

        # Index by condition_id for fast lookup on price updates
        self._positions_by_market: Dict[str, List[str]] = {}

    # =========================================================================
    # Position Lifecycle
    # =========================================================================

    async def add_position(self, position: ActivePosition) -> None:
        """Add a new position to track.

        Args:
            position: The position to track
        """
        self.positions[position.trade_id] = position

        # Add to market index
        cid = position.market.condition_id
        if cid not in self._positions_by_market:
            self._positions_by_market[cid] = []
        self._positions_by_market[cid].append(position.trade_id)

        log.info(
            "Position added to active management",
            trade_id=position.trade_id,
            asset=position.market.asset,
            yes_shares=f"{position.yes_shares:.2f}",
            no_shares=f"{position.no_shares:.2f}",
            hedge_ratio=f"{position.hedge_ratio:.0%}",
            status="BALANCED" if position.is_balanced else "NEEDS_REBALANCING",
        )

        # If imbalanced, start seeking rebalancing immediately
        if position.needs_rebalancing:
            position.telemetry.record_rebalance_started()

    async def remove_position(
        self,
        trade_id: str,
        profit: float = 0.0,
        reason: str = "resolved",
    ) -> Optional[ActivePosition]:
        """Remove a position (usually after resolution).

        Args:
            trade_id: ID of the trade to remove
            profit: Actual profit from the position
            reason: Reason for removal

        Returns:
            The removed position, or None if not found
        """
        if trade_id not in self.positions:
            return None

        position = self.positions.pop(trade_id)
        position.status = "RESOLVED"
        position.telemetry.record_resolved(profit)

        # Remove from market index
        cid = position.market.condition_id
        if cid in self._positions_by_market:
            self._positions_by_market[cid] = [
                tid for tid in self._positions_by_market[cid] if tid != trade_id
            ]
            if not self._positions_by_market[cid]:
                del self._positions_by_market[cid]

        # Save telemetry to database
        if self._db:
            await self._save_telemetry(position.telemetry)

        log.info(
            "Position removed from active management",
            trade_id=trade_id,
            asset=position.market.asset,
            reason=reason,
            final_profit=f"${profit:.2f}",
            hedge_ratio=f"{position.hedge_ratio:.0%}",
        )

        return position

    def get_position(self, trade_id: str) -> Optional[ActivePosition]:
        """Get a position by trade ID."""
        return self.positions.get(trade_id)

    def get_positions_for_market(self, condition_id: str) -> List[ActivePosition]:
        """Get all positions for a specific market."""
        trade_ids = self._positions_by_market.get(condition_id, [])
        return [self.positions[tid] for tid in trade_ids if tid in self.positions]

    def get_positions_needing_rebalancing(self) -> List[ActivePosition]:
        """Get all positions that need rebalancing."""
        return [p for p in self.positions.values() if p.needs_rebalancing]

    # =========================================================================
    # Real-Time Price Updates (WebSocket Integration)
    # =========================================================================

    async def on_price_update(
        self,
        condition_id: str,
        market_state: "MarketState",
    ) -> None:
        """Called on every WebSocket price update.

        This is the key to ACTIVE management - we check for rebalancing
        opportunities immediately when prices change, not on a timer.

        Args:
            condition_id: Market condition ID
            market_state: Current market state with prices
        """
        # Find positions for this market that need rebalancing
        positions = [
            p for p in self.get_positions_for_market(condition_id)
            if p.needs_rebalancing
        ]

        for position in positions:
            await self._evaluate_rebalancing(position, market_state)

    # =========================================================================
    # Rebalancing Logic
    # =========================================================================

    async def _evaluate_rebalancing(
        self,
        position: ActivePosition,
        market_state: "MarketState",
    ) -> None:
        """Evaluate and potentially execute rebalancing for a position.

        Args:
            position: Position to evaluate
            market_state: Current market prices
        """
        # Skip if too close to resolution
        if position.seconds_to_resolution < self.config.max_rebalance_wait_seconds:
            return

        # Skip if max rebalance attempts reached
        if position.telemetry.rebalance_attempts >= self.config.max_rebalance_trades:
            log.debug(
                "Max rebalance attempts reached",
                trade_id=position.trade_id,
                attempts=position.telemetry.rebalance_attempts,
            )
            return

        # Get rebalancing options
        options = self._get_rebalancing_options(position, market_state)

        if not options:
            return

        # Select best option
        best_option = self._select_best_option(options)

        if best_option and self._should_execute(best_option, position, market_state):
            await self._execute_rebalance(position, best_option, market_state)

    def _get_rebalancing_options(
        self,
        position: ActivePosition,
        market_state: "MarketState",
    ) -> List[RebalanceOption]:
        """Get available rebalancing options.

        Args:
            position: Position to rebalance
            market_state: Current market prices

        Returns:
            List of viable rebalancing options
        """
        options = []

        yes_bid = market_state.yes_best_bid
        yes_ask = market_state.yes_best_ask
        no_bid = market_state.no_best_bid
        no_ask = market_state.no_best_ask

        if position.yes_shares > position.no_shares:
            # Excess YES - need to either sell YES or buy NO
            excess = position.yes_shares - position.no_shares

            # Option A: Sell excess YES at current bid
            if yes_bid > 0 and yes_bid > position.yes_avg_price:
                sell_profit = excess * (yes_bid - position.yes_avg_price)
                options.append(RebalanceOption(
                    action="SELL_YES",
                    shares=excess,
                    price=yes_bid,
                    profit=sell_profit,
                ))

            # Option B: Buy more NO at current ask (within budget)
            if no_ask > 0 and no_ask < 1.0:
                max_shares = position.remaining_budget / no_ask if no_ask > 0 else 0
                shares_to_buy = min(excess, max_shares)

                if shares_to_buy > 0:
                    buy_cost = shares_to_buy * no_ask
                    # New guaranteed return after buying
                    new_min_shares = min(position.yes_shares, position.no_shares + shares_to_buy)
                    new_return = new_min_shares * 1.0
                    new_total_cost = position.total_cost + buy_cost
                    buy_profit = new_return - new_total_cost - position.expected_profit

                    if buy_profit > 0:
                        options.append(RebalanceOption(
                            action="BUY_NO",
                            shares=shares_to_buy,
                            price=no_ask,
                            profit=buy_profit,
                        ))

        else:  # no_shares > yes_shares
            # Excess NO - need to either sell NO or buy YES
            excess = position.no_shares - position.yes_shares

            # Option A: Sell excess NO at current bid
            if no_bid > 0 and no_bid > position.no_avg_price:
                sell_profit = excess * (no_bid - position.no_avg_price)
                options.append(RebalanceOption(
                    action="SELL_NO",
                    shares=excess,
                    price=no_bid,
                    profit=sell_profit,
                ))

            # Option B: Buy more YES at current ask (within budget)
            if yes_ask > 0 and yes_ask < 1.0:
                max_shares = position.remaining_budget / yes_ask if yes_ask > 0 else 0
                shares_to_buy = min(excess, max_shares)

                if shares_to_buy > 0:
                    buy_cost = shares_to_buy * yes_ask
                    new_min_shares = min(position.yes_shares + shares_to_buy, position.no_shares)
                    new_return = new_min_shares * 1.0
                    new_total_cost = position.total_cost + buy_cost
                    buy_profit = new_return - new_total_cost - position.expected_profit

                    if buy_profit > 0:
                        options.append(RebalanceOption(
                            action="BUY_YES",
                            shares=shares_to_buy,
                            price=yes_ask,
                            profit=buy_profit,
                        ))

        return options

    def _select_best_option(
        self,
        options: List[RebalanceOption],
    ) -> Optional[RebalanceOption]:
        """Select the best rebalancing option.

        Args:
            options: Available options

        Returns:
            Best option, or None if no viable options
        """
        if not options:
            return None

        # Filter by minimum profit per share
        viable = [
            o for o in options
            if o.profit_per_share >= self.config.min_profit_per_share
        ]

        if not viable:
            return None

        # Prefer selling if configured (capital efficient)
        if self.config.prefer_sell_over_buy:
            sell_options = [o for o in viable if o.action.startswith("SELL")]
            if sell_options:
                return max(sell_options, key=lambda o: o.profit)

        return max(viable, key=lambda o: o.profit)

    def _should_execute(
        self,
        option: RebalanceOption,
        position: ActivePosition,
        market_state: "MarketState",
    ) -> bool:
        """Final check before executing rebalance.

        Validates that the trade maintains minimum spread.

        Args:
            option: Option to execute
            position: Current position
            market_state: Current market prices

        Returns:
            True if should execute
        """
        # Check minimum profit threshold
        if option.profit_per_share < self.config.min_profit_per_share:
            return False

        # Check if partial rebalancing is allowed
        if not self.config.allow_partial_rebalance:
            if option.shares < position.excess_shares:
                return False

        # Verify minimum spread is maintained after trade
        # For sell: spread = 1.0 - (avg_yes_price + avg_no_price) should stay >= min_spread
        # For buy: need to check that new avg price doesn't compress spread too much
        yes_price = position.yes_avg_price
        no_price = position.no_avg_price

        if option.action == "BUY_YES":
            # New weighted average YES price
            total_yes_cost = (position.yes_shares * yes_price) + (option.shares * option.price)
            new_yes_shares = position.yes_shares + option.shares
            new_yes_price = total_yes_cost / new_yes_shares if new_yes_shares > 0 else option.price
            new_spread = 1.0 - (new_yes_price + no_price)
            if new_spread < self.config.min_spread_dollars:
                log.debug(
                    "Rebalance rejected: spread would be too small",
                    action=option.action,
                    new_spread=f"${new_spread:.4f}",
                    min_spread=f"${self.config.min_spread_dollars:.2f}",
                )
                return False

        elif option.action == "BUY_NO":
            total_no_cost = (position.no_shares * no_price) + (option.shares * option.price)
            new_no_shares = position.no_shares + option.shares
            new_no_price = total_no_cost / new_no_shares if new_no_shares > 0 else option.price
            new_spread = 1.0 - (yes_price + new_no_price)
            if new_spread < self.config.min_spread_dollars:
                log.debug(
                    "Rebalance rejected: spread would be too small",
                    action=option.action,
                    new_spread=f"${new_spread:.4f}",
                    min_spread=f"${self.config.min_spread_dollars:.2f}",
                )
                return False

        return True

    async def _execute_rebalance(
        self,
        position: ActivePosition,
        option: RebalanceOption,
        market_state: "MarketState",
    ) -> None:
        """Execute a rebalancing trade.

        Args:
            position: Position to rebalance
            option: Rebalancing option to execute
            market_state: Current market state
        """
        log.info(
            "Executing rebalance",
            trade_id=position.trade_id,
            asset=position.market.asset,
            action=option.action,
            shares=f"{option.shares:.2f}",
            price=f"${option.price:.3f}",
            expected_profit=f"${option.profit:.4f}",
            current_hedge_ratio=f"{position.hedge_ratio:.0%}",
        )

        # Create trade record
        trade = RebalanceTrade(
            attempted_at=datetime.utcnow(),
            action=option.action,
            shares=option.shares,
            price=option.price,
            status="PENDING",
        )

        try:
            # Determine token and side
            if option.action == "SELL_YES":
                token_id = position.market.yes_token_id
                side = "SELL"
            elif option.action == "SELL_NO":
                token_id = position.market.no_token_id
                side = "SELL"
            elif option.action == "BUY_YES":
                token_id = position.market.yes_token_id
                side = "BUY"
            else:  # BUY_NO
                token_id = position.market.no_token_id
                side = "BUY"

            # Execute order via FOK to ensure atomicity
            from py_clob_client.clob_types import OrderArgs, OrderType

            order_args = OrderArgs(
                token_id=token_id,
                price=option.price,
                size=option.shares,
                side=side,
            )

            signed_order = self._client._client.create_order(order_args)
            result = self._client._client.post_order(signed_order, orderType=OrderType.FOK)

            status = result.get("status", "").upper()

            if status in ("MATCHED", "FILLED"):
                # Success - update position
                filled_shares = float(result.get("size_matched", option.shares) or option.shares)

                if option.action.startswith("SELL"):
                    sell_side = "YES" if option.action == "SELL_YES" else "NO"
                    profit = position.update_after_sell(sell_side, filled_shares, option.price)
                    trade.profit = profit
                else:
                    buy_side = "YES" if option.action == "BUY_YES" else "NO"
                    position.update_after_buy(buy_side, filled_shares, option.price)

                trade.status = "SUCCESS"
                trade.filled_shares = filled_shares
                trade.order_id = result.get("id")

                log.info(
                    "Rebalance successful",
                    trade_id=position.trade_id,
                    action=option.action,
                    filled_shares=f"{filled_shares:.2f}",
                    new_hedge_ratio=f"{position.hedge_ratio:.0%}",
                    is_balanced=position.is_balanced,
                )

            elif status == "LIVE":
                # Order placed but not immediately filled - for FOK this shouldn't happen
                trade.status = "PARTIAL"
                trade.error = "Order went LIVE (expected FOK to fill immediately)"
                log.warning(
                    "Rebalance order went LIVE instead of filling",
                    trade_id=position.trade_id,
                    order_id=result.get("id"),
                )

            else:
                # Failed
                trade.status = "FAILED"
                trade.error = f"Order status: {status}"
                log.warning(
                    "Rebalance order rejected",
                    trade_id=position.trade_id,
                    status=status,
                    result=result,
                )

        except Exception as e:
            trade.status = "FAILED"
            trade.error = str(e)
            log.error(
                "Rebalance execution failed",
                trade_id=position.trade_id,
                error=str(e),
            )

        # Record the trade regardless of outcome
        position.record_rebalance_trade(trade)

    # =========================================================================
    # Telemetry Storage
    # =========================================================================

    async def _save_telemetry(self, telemetry: TradeTelemetry) -> None:
        """Save telemetry to database.

        Args:
            telemetry: Telemetry data to save
        """
        if not self._db:
            return

        try:
            await self._db.save_trade_telemetry(telemetry.to_dict())
        except Exception as e:
            log.error("Failed to save telemetry", trade_id=telemetry.trade_id, error=str(e))

    # =========================================================================
    # Utility Methods
    # =========================================================================

    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about active positions."""
        total = len(self.positions)
        balanced = sum(1 for p in self.positions.values() if p.is_balanced)
        needing_rebalance = total - balanced

        total_yes_exposure = sum(p.yes_shares * p.yes_avg_price for p in self.positions.values())
        total_no_exposure = sum(p.no_shares * p.no_avg_price for p in self.positions.values())
        total_expected_profit = sum(p.expected_profit for p in self.positions.values())

        return {
            "total_positions": total,
            "balanced_positions": balanced,
            "needing_rebalance": needing_rebalance,
            "total_yes_exposure": total_yes_exposure,
            "total_no_exposure": total_no_exposure,
            "total_expected_profit": total_expected_profit,
            "markets_tracked": len(self._positions_by_market),
        }


# =============================================================================
# Factory Functions
# =============================================================================

def create_telemetry(
    trade_id: str,
    opportunity_spread: float,
    yes_price: float,
    no_price: float,
) -> TradeTelemetry:
    """Create a new TradeTelemetry instance.

    Args:
        trade_id: Unique trade identifier
        opportunity_spread: Spread in cents at detection
        yes_price: YES price at detection
        no_price: NO price at detection

    Returns:
        New TradeTelemetry instance
    """
    return TradeTelemetry(
        trade_id=trade_id,
        opportunity_detected_at=datetime.utcnow(),
        opportunity_spread=opportunity_spread,
        opportunity_yes_price=yes_price,
        opportunity_no_price=no_price,
    )


def create_active_position(
    trade_id: str,
    market: "Market15Min",
    yes_shares: float,
    no_shares: float,
    yes_price: float,
    no_price: float,
    telemetry: TradeTelemetry,
    budget: float = 0.0,
) -> ActivePosition:
    """Create a new ActivePosition instance.

    Args:
        trade_id: Unique trade identifier
        market: Market the position is in
        yes_shares: Number of YES shares
        no_shares: Number of NO shares
        yes_price: Average YES entry price
        no_price: Average NO entry price
        telemetry: Associated telemetry data
        budget: Original trade budget

    Returns:
        New ActivePosition instance
    """
    return ActivePosition(
        trade_id=trade_id,
        market=market,
        yes_shares=yes_shares,
        no_shares=no_shares,
        yes_avg_price=yes_price,
        no_avg_price=no_price,
        telemetry=telemetry,
        original_budget=budget,
    )
