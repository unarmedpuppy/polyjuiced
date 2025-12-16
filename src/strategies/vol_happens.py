"""Vol Happens volatility/mean reversion strategy for 15-minute markets.

This strategy builds hedged positions over time by buying each side when it hits
a target price. It bets that prices will oscillate enough within the 15-minute
window for both sides to become cheap at some point.

Entry Logic:
1. Wait for one side to hit entry_price_threshold ($0.48)
2. Trend filter: other side must be <= trend_filter_threshold ($0.52)
3. Place first leg limit order
4. Wait for other side to also hit entry_price_threshold
5. Complete hedge with equal shares (not dollars)

Exit Logic:
- Hedged positions settle at resolution (guaranteed $1 payout)
- Unhedged positions hard exit at exit_time_remaining_seconds
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import structlog

from ..client.polymarket import PolymarketClient
from ..client.websocket import PolymarketWebSocket
from ..config import AppConfig, VolHappensConfig
from ..persistence import Database
from ..monitoring.market_finder import Market15Min, MarketFinder
from ..monitoring.order_book import MarketState, MultiMarketTracker
from ..dashboard import add_log, add_trade, add_decision, update_stats
from .base import BaseStrategy

if TYPE_CHECKING:
    from ..liquidity.collector import LiquidityCollector

log = structlog.get_logger()


class PositionState(str, Enum):
    """Position lifecycle states."""
    WAITING_FOR_HEDGE = "WAITING_FOR_HEDGE"  # First leg filled, waiting for second
    HEDGED = "HEDGED"  # Both legs filled, holding to resolution
    FORCE_EXIT = "FORCE_EXIT"  # Exiting unhedged position
    RESOLVED = "RESOLVED"  # Market resolved, profit locked
    CLOSED = "CLOSED"  # Exited early (loss taken)


@dataclass
class VolHappensPosition:
    """Tracks a Vol Happens position."""

    # Identification
    id: str
    market: Market15Min
    strategy_id: str = "vol_happens"

    # First leg
    first_leg_side: str = ""  # "YES" or "NO"
    first_leg_shares: float = 0.0
    first_leg_price: float = 0.0
    first_leg_cost: float = 0.0
    first_leg_filled_at: Optional[datetime] = None

    # Second leg (optional until hedged)
    second_leg_shares: Optional[float] = None
    second_leg_price: Optional[float] = None
    second_leg_cost: Optional[float] = None
    second_leg_filled_at: Optional[datetime] = None

    # State
    state: PositionState = PositionState.WAITING_FOR_HEDGE

    # Exit tracking
    exit_price: Optional[float] = None
    exit_proceeds: Optional[float] = None
    closed_at: Optional[datetime] = None

    @property
    def is_hedged(self) -> bool:
        """Check if position is fully hedged."""
        return self.second_leg_shares is not None and self.second_leg_shares > 0

    @property
    def total_cost(self) -> float:
        """Total cost of position."""
        cost = self.first_leg_cost
        if self.second_leg_cost:
            cost += self.second_leg_cost
        return cost

    @property
    def spread_captured(self) -> float:
        """Spread in dollars (only valid when hedged)."""
        if not self.is_hedged:
            return 0.0
        return 1.0 - (self.first_leg_price + (self.second_leg_price or 0))

    @property
    def expected_profit(self) -> float:
        """Expected profit when hedged."""
        if not self.is_hedged:
            return 0.0
        # Each share pair pays $1.00
        payout = self.first_leg_shares * 1.0
        return payout - self.total_cost

    @property
    def realized_pnl(self) -> Optional[float]:
        """Realized P&L after closing."""
        if self.state not in (PositionState.RESOLVED, PositionState.CLOSED):
            return None
        if self.state == PositionState.RESOLVED and self.is_hedged:
            return self.expected_profit
        if self.exit_proceeds is not None:
            return self.exit_proceeds - self.first_leg_cost
        return None


@dataclass
class VolHappensTradeResult:
    """Result of a Vol Happens trade attempt."""
    success: bool
    position_id: Optional[str] = None
    shares: float = 0.0
    price: float = 0.0
    cost: float = 0.0
    side: str = ""
    leg: str = ""  # "first" or "second"
    error: Optional[str] = None
    dry_run: bool = False


class VolHappensStrategy(BaseStrategy):
    """Vol Happens volatility/mean reversion strategy.

    Key principles:
    1. Wait for price to drop to entry threshold before entering
    2. Use trend filter to avoid strong directional moves
    3. Build hedge over time as prices oscillate
    4. Hard exit if unhedged near resolution
    """

    STRATEGY_ID = "vol_happens"

    def __init__(
        self,
        client: PolymarketClient,
        ws_client: PolymarketWebSocket,
        market_finder: MarketFinder,
        config: AppConfig,
        db: Optional[Database] = None,
    ):
        """Initialize Vol Happens strategy.

        Args:
            client: Polymarket CLOB client
            ws_client: WebSocket client for streaming
            market_finder: Market discovery service
            config: Application configuration
            db: Database instance for trade persistence
        """
        super().__init__(client, config)
        self.ws = ws_client
        self.market_finder = market_finder
        self.vol_happens_config: VolHappensConfig = config.vol_happens
        self._db: Optional[Database] = db

        # Active tracking
        self._tracker: Optional[MultiMarketTracker] = None
        self._active_markets: Dict[str, Market15Min] = {}
        self._positions: Dict[str, VolHappensPosition] = {}  # condition_id -> position

        # Daily stats
        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0

        # Background tasks
        self._monitor_task: Optional[asyncio.Task] = None
        self._position_checker_task: Optional[asyncio.Task] = None

    @property
    def name(self) -> str:
        return "VolHappensStrategy"

    async def start(self) -> None:
        """Start the strategy."""
        if self._running:
            log.warning("Vol Happens strategy already running")
            return

        if not self.vol_happens_config.enabled:
            log.info("Vol Happens strategy disabled")
            return

        log.info(
            "Starting Vol Happens strategy",
            markets=self.vol_happens_config.markets,
            entry_threshold=f"${self.vol_happens_config.entry_price_threshold:.2f}",
            trend_filter=f"${self.vol_happens_config.trend_filter_threshold:.2f}",
            first_leg_size=f"${self.vol_happens_config.first_leg_size_usd:.2f}",
            dry_run=self.vol_happens_config.dry_run,
        )

        self._running = True

        # Initialize market tracker
        await self._init_market_tracker()

        # Start background tasks
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self._position_checker_task = asyncio.create_task(self._position_checker_loop())

        add_log("Vol Happens strategy started", "info")

    async def stop(self) -> None:
        """Stop the strategy."""
        if not self._running:
            return

        log.info("Stopping Vol Happens strategy")
        self._running = False

        # Cancel background tasks
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        if self._position_checker_task:
            self._position_checker_task.cancel()
            try:
                await self._position_checker_task
            except asyncio.CancelledError:
                pass

        add_log("Vol Happens strategy stopped", "info")

    async def _init_market_tracker(self) -> None:
        """Initialize the multi-market tracker."""
        # Find active 15-minute markets
        markets = await self.market_finder.find_active_markets()
        filtered_markets = [
            m for m in markets
            if m.asset in self.vol_happens_config.markets
        ]

        self._active_markets = {m.condition_id: m for m in filtered_markets}

        log.info(
            "Vol Happens: Found markets",
            count=len(filtered_markets),
            markets=[m.asset for m in filtered_markets],
        )

        if filtered_markets:
            # Initialize tracker with websocket client (same pattern as Gabagool)
            # Use a tight spread for Vol Happens - we want to catch all price movements
            self._tracker = MultiMarketTracker(
                self.ws,
                min_spread_cents=2.0,  # 2 cent min spread for opportunity detection
            )

            # Add markets to tracker
            for market in filtered_markets:
                await self._tracker.add_market(market)

    async def _monitor_loop(self) -> None:
        """Main monitoring loop - checks for entry opportunities."""
        while self._running:
            try:
                await self._check_entry_opportunities()
                await asyncio.sleep(0.5)  # Check every 500ms
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Vol Happens monitor error", error=str(e))
                await asyncio.sleep(1.0)

    async def _position_checker_loop(self) -> None:
        """Check positions for hedge completion and force exits."""
        while self._running:
            try:
                await self._check_positions()
                await asyncio.sleep(1.0)  # Check every second
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Vol Happens position checker error", error=str(e))
                await asyncio.sleep(1.0)

    async def _check_entry_opportunities(self) -> None:
        """Check all markets for entry opportunities."""
        if not self._tracker:
            return

        for condition_id, market in list(self._active_markets.items()):
            # Skip if we already have a position
            if condition_id in self._positions:
                continue

            # Get current market state
            state = self._tracker.get_market_state(condition_id)
            if not state:
                continue

            # Check entry conditions
            await self._evaluate_entry(market, state)

    async def _evaluate_entry(self, market: Market15Min, state: MarketState) -> None:
        """Evaluate if we should enter a position."""
        config = self.vol_happens_config

        # Check time remaining
        time_remaining = (market.end_time - datetime.utcnow()).total_seconds()
        if time_remaining < config.min_time_to_enter_seconds:
            return

        # Check daily loss limit
        if self._daily_pnl <= -config.max_daily_loss_usd:
            return

        # Check max positions
        active_positions = sum(
            1 for p in self._positions.values()
            if p.state in (PositionState.WAITING_FOR_HEDGE, PositionState.HEDGED)
        )
        if active_positions >= config.max_positions:
            return

        yes_price = state.yes_price
        no_price = state.no_price

        # Log price evaluation periodically (every ~10 seconds per market)
        # Only log if we have actual prices (not defaults)
        if state.last_update and yes_price < 1.0 and no_price < 1.0:
            log.debug(
                "Vol Happens: Evaluating entry",
                asset=market.asset,
                yes_price=f"${yes_price:.2f}",
                no_price=f"${no_price:.2f}",
                entry_threshold=f"${config.entry_price_threshold:.2f}",
                trend_filter=f"${config.trend_filter_threshold:.2f}",
            )

        # Check if either side meets entry threshold
        # With trend filter: other side must be <= trend_filter_threshold
        entry_side = None
        entry_price = 0.0

        if yes_price <= config.entry_price_threshold and no_price <= config.trend_filter_threshold:
            entry_side = "YES"
            entry_price = yes_price
        elif no_price <= config.entry_price_threshold and yes_price <= config.trend_filter_threshold:
            entry_side = "NO"
            entry_price = no_price

        if not entry_side:
            return

        # Execute entry
        log.info(
            "Vol Happens: Entry signal",
            asset=market.asset,
            side=entry_side,
            price=f"${entry_price:.2f}",
            other_side=f"${no_price if entry_side == 'YES' else yes_price:.2f}",
        )

        await self._execute_first_leg(market, entry_side, entry_price)

    async def _execute_first_leg(
        self,
        market: Market15Min,
        side: str,
        price: float,
    ) -> None:
        """Execute the first leg of a position."""
        config = self.vol_happens_config
        position_id = str(uuid.uuid4())[:8]

        # Calculate shares
        shares = config.first_leg_size_usd / price
        cost = shares * price

        add_decision(
            f"VOL_HAPPENS_ENTRY: {market.asset} {side} "
            f"{shares:.2f} shares @ ${price:.2f} = ${cost:.2f}",
            "approved" if not config.dry_run else "dry_run",
        )

        if config.dry_run:
            log.info(
                "Vol Happens: DRY RUN - First leg",
                asset=market.asset,
                side=side,
                shares=f"{shares:.2f}",
                price=f"${price:.2f}",
                cost=f"${cost:.2f}",
            )
            # Create position anyway for tracking
            position = VolHappensPosition(
                id=position_id,
                market=market,
                first_leg_side=side,
                first_leg_shares=shares,
                first_leg_price=price,
                first_leg_cost=cost,
                first_leg_filled_at=datetime.utcnow(),
                state=PositionState.WAITING_FOR_HEDGE,
            )
            self._positions[market.condition_id] = position
            self._daily_trades += 1
            return

        # Execute real trade
        try:
            token_id = market.yes_token_id if side == "YES" else market.no_token_id
            result = await self.client.execute_market_buy(
                token_id=token_id,
                amount_usd=cost,
                max_price=price + 0.01,  # Small slippage buffer
            )

            if result.get("success"):
                filled_shares = result.get("shares", shares)
                filled_price = result.get("avg_price", price)
                filled_cost = result.get("total_cost", cost)

                position = VolHappensPosition(
                    id=position_id,
                    market=market,
                    first_leg_side=side,
                    first_leg_shares=filled_shares,
                    first_leg_price=filled_price,
                    first_leg_cost=filled_cost,
                    first_leg_filled_at=datetime.utcnow(),
                    state=PositionState.WAITING_FOR_HEDGE,
                )
                self._positions[market.condition_id] = position
                self._daily_trades += 1

                log.info(
                    "Vol Happens: First leg filled",
                    position_id=position_id,
                    asset=market.asset,
                    side=side,
                    shares=f"{filled_shares:.2f}",
                    price=f"${filled_price:.2f}",
                    cost=f"${filled_cost:.2f}",
                )

                # Record trade
                if self._db:
                    self._db.record_trade(
                        condition_id=market.condition_id,
                        side=side,
                        shares=filled_shares,
                        price=filled_price,
                        cost=filled_cost,
                        strategy_id=self.STRATEGY_ID,
                    )
            else:
                log.warning(
                    "Vol Happens: First leg failed",
                    asset=market.asset,
                    side=side,
                    error=result.get("error", "Unknown"),
                )

        except Exception as e:
            log.error(
                "Vol Happens: First leg exception",
                asset=market.asset,
                side=side,
                error=str(e),
            )

    async def _check_positions(self) -> None:
        """Check all open positions for hedge completion or force exit."""
        for condition_id, position in list(self._positions.items()):
            if position.state == PositionState.WAITING_FOR_HEDGE:
                await self._check_hedge_opportunity(position)
            elif position.state == PositionState.HEDGED:
                await self._check_resolution(position)

    async def _check_hedge_opportunity(self, position: VolHappensPosition) -> None:
        """Check if we can complete the hedge."""
        config = self.vol_happens_config
        market = position.market

        # Check time remaining
        time_remaining = (market.end_time - datetime.utcnow()).total_seconds()

        # Force exit if near resolution
        if time_remaining <= config.exit_time_remaining_seconds:
            await self._force_exit(position)
            return

        # Get current prices
        state = self._tracker.get_market_state(market.condition_id) if self._tracker else None
        if not state:
            return

        # Determine which side we need
        hedge_side = "NO" if position.first_leg_side == "YES" else "YES"
        hedge_price = state.yes_price if hedge_side == "YES" else state.no_price

        # Check if hedge side hit threshold
        if hedge_price <= config.entry_price_threshold:
            await self._execute_second_leg(position, hedge_side, hedge_price)

    async def _execute_second_leg(
        self,
        position: VolHappensPosition,
        side: str,
        price: float,
    ) -> None:
        """Execute the second leg to complete the hedge."""
        config = self.vol_happens_config

        # Match shares from first leg (not dollars)
        shares = position.first_leg_shares
        cost = shares * price

        # Check if within budget
        total_cost = position.first_leg_cost + cost
        if total_cost > config.max_position_usd:
            log.warning(
                "Vol Happens: Second leg would exceed budget",
                position_id=position.id,
                total_cost=f"${total_cost:.2f}",
                max=f"${config.max_position_usd:.2f}",
            )
            return

        add_decision(
            f"VOL_HAPPENS_HEDGE: {position.market.asset} {side} "
            f"{shares:.2f} shares @ ${price:.2f} = ${cost:.2f}",
            "approved" if not config.dry_run else "dry_run",
        )

        if config.dry_run:
            log.info(
                "Vol Happens: DRY RUN - Second leg (hedge)",
                position_id=position.id,
                asset=position.market.asset,
                side=side,
                shares=f"{shares:.2f}",
                price=f"${price:.2f}",
                cost=f"${cost:.2f}",
                spread=f"${1.0 - position.first_leg_price - price:.3f}",
            )
            # Update position
            position.second_leg_shares = shares
            position.second_leg_price = price
            position.second_leg_cost = cost
            position.second_leg_filled_at = datetime.utcnow()
            position.state = PositionState.HEDGED
            return

        # Execute real trade
        try:
            market = position.market
            token_id = market.yes_token_id if side == "YES" else market.no_token_id
            result = await self.client.execute_market_buy(
                token_id=token_id,
                amount_usd=cost,
                max_price=price + 0.01,
            )

            if result.get("success"):
                filled_shares = result.get("shares", shares)
                filled_price = result.get("avg_price", price)
                filled_cost = result.get("total_cost", cost)

                position.second_leg_shares = filled_shares
                position.second_leg_price = filled_price
                position.second_leg_cost = filled_cost
                position.second_leg_filled_at = datetime.utcnow()
                position.state = PositionState.HEDGED

                log.info(
                    "Vol Happens: HEDGED",
                    position_id=position.id,
                    asset=market.asset,
                    total_cost=f"${position.total_cost:.2f}",
                    spread=f"${position.spread_captured:.3f}",
                    expected_profit=f"${position.expected_profit:.2f}",
                )

                # Record trade
                if self._db:
                    self._db.record_trade(
                        condition_id=market.condition_id,
                        side=side,
                        shares=filled_shares,
                        price=filled_price,
                        cost=filled_cost,
                        strategy_id=self.STRATEGY_ID,
                    )
            else:
                log.warning(
                    "Vol Happens: Second leg failed",
                    position_id=position.id,
                    error=result.get("error", "Unknown"),
                )

        except Exception as e:
            log.error(
                "Vol Happens: Second leg exception",
                position_id=position.id,
                error=str(e),
            )

    async def _force_exit(self, position: VolHappensPosition) -> None:
        """Force exit an unhedged position."""
        config = self.vol_happens_config
        market = position.market

        log.warning(
            "Vol Happens: FORCE EXIT - Time threshold reached",
            position_id=position.id,
            asset=market.asset,
            side=position.first_leg_side,
            shares=f"{position.first_leg_shares:.2f}",
        )

        position.state = PositionState.FORCE_EXIT

        add_decision(
            f"VOL_HAPPENS_EXIT: {market.asset} {position.first_leg_side} "
            f"{position.first_leg_shares:.2f} shares (unhedged)",
            "approved" if not config.dry_run else "dry_run",
        )

        if config.dry_run:
            # Estimate exit price (use current price or worse)
            state = self._tracker.get_market_state(market.condition_id) if self._tracker else None
            exit_price = 0.30  # Assume worst case
            if state:
                exit_price = state.yes_price if position.first_leg_side == "YES" else state.no_price

            proceeds = position.first_leg_shares * exit_price
            loss = proceeds - position.first_leg_cost

            position.exit_price = exit_price
            position.exit_proceeds = proceeds
            position.closed_at = datetime.utcnow()
            position.state = PositionState.CLOSED

            self._daily_pnl += loss

            log.info(
                "Vol Happens: DRY RUN - Force exit",
                position_id=position.id,
                exit_price=f"${exit_price:.2f}",
                proceeds=f"${proceeds:.2f}",
                loss=f"${loss:.2f}",
            )
            return

        # Execute real exit
        try:
            token_id = market.yes_token_id if position.first_leg_side == "YES" else market.no_token_id
            result = await self.client.execute_market_sell(
                token_id=token_id,
                shares=position.first_leg_shares,
            )

            if result.get("success"):
                exit_price = result.get("avg_price", 0.0)
                proceeds = result.get("total_proceeds", 0.0)
                loss = proceeds - position.first_leg_cost

                position.exit_price = exit_price
                position.exit_proceeds = proceeds
                position.closed_at = datetime.utcnow()
                position.state = PositionState.CLOSED

                self._daily_pnl += loss

                log.info(
                    "Vol Happens: Force exit complete",
                    position_id=position.id,
                    exit_price=f"${exit_price:.2f}",
                    proceeds=f"${proceeds:.2f}",
                    loss=f"${loss:.2f}",
                )
            else:
                log.error(
                    "Vol Happens: Force exit failed",
                    position_id=position.id,
                    error=result.get("error", "Unknown"),
                )

        except Exception as e:
            log.error(
                "Vol Happens: Force exit exception",
                position_id=position.id,
                error=str(e),
            )

    async def _check_resolution(self, position: VolHappensPosition) -> None:
        """Check if a hedged position has resolved."""
        market = position.market
        now = datetime.utcnow()

        if now >= market.end_time:
            # Market resolved - lock in profit
            position.state = PositionState.RESOLVED
            profit = position.expected_profit
            self._daily_pnl += profit

            log.info(
                "Vol Happens: RESOLVED",
                position_id=position.id,
                asset=market.asset,
                profit=f"${profit:.2f}",
                total_cost=f"${position.total_cost:.2f}",
            )

            # Remove from active positions after some delay
            # (keep for dashboard display)

    async def on_opportunity(self, opportunity: Any) -> Optional[Dict[str, Any]]:
        """Handle opportunity (not used - strategy is self-contained)."""
        # Vol Happens manages its own opportunity detection
        return None

    def get_positions(self) -> List[VolHappensPosition]:
        """Get all tracked positions."""
        return list(self._positions.values())

    def get_active_positions(self) -> List[VolHappensPosition]:
        """Get active (non-closed) positions."""
        return [
            p for p in self._positions.values()
            if p.state in (PositionState.WAITING_FOR_HEDGE, PositionState.HEDGED)
        ]

    def get_stats(self) -> Dict[str, Any]:
        """Get strategy statistics."""
        active = self.get_active_positions()
        waiting = [p for p in active if p.state == PositionState.WAITING_FOR_HEDGE]
        hedged = [p for p in active if p.state == PositionState.HEDGED]

        return {
            "enabled": self.vol_happens_config.enabled,
            "running": self._running,
            "dry_run": self.vol_happens_config.dry_run,
            "positions_total": len(self._positions),
            "positions_waiting": len(waiting),
            "positions_hedged": len(hedged),
            "daily_trades": self._daily_trades,
            "daily_pnl": self._daily_pnl,
        }
