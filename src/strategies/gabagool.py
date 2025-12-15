"""Gabagool arbitrage strategy for 15-minute up/down markets.

This strategy exploits temporary mispricing in binary markets where:
- YES + NO should sum to $1.00
- When sum < $1.00, buying both guarantees profit
- Profit = $1.00 - (YES_cost + NO_cost)

Named after the successful Polymarket trader @gabagool22.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import structlog

from ..client.polymarket import PolymarketClient
from ..persistence import Database
from ..events import trade_events, EventTypes

if TYPE_CHECKING:
    from ..liquidity.collector import LiquidityCollector
from ..client.websocket import PolymarketWebSocket
from ..config import AppConfig, GabagoolConfig
from .. import dashboard
from ..dashboard import add_log, add_trade, add_decision, resolve_trade, update_stats, update_markets, stats
from ..metrics import (
    ACTIVE_MARKETS,
    DAILY_EXPOSURE_USD,
    DAILY_PNL_USD,
    DAILY_TRADES,
    OPPORTUNITIES_DETECTED,
    OPPORTUNITIES_EXECUTED,
    OPPORTUNITIES_SKIPPED,
    ORDER_REJECTED_TOTAL,
    SPREAD_CENTS,
    TRADE_AMOUNT_USD,
    TRADE_ERRORS_TOTAL,
    TRADE_PROFIT_USD,
    TRADES_TOTAL,
    YES_PRICE,
    NO_PRICE,
)
from ..monitoring.market_finder import Market15Min, MarketFinder
from ..monitoring.order_book import (
    ArbitrageOpportunity,
    MarketState,
    MultiMarketTracker,
    OrderBookTracker,
)
from ..position_manager import (
    ActivePosition,
    ActivePositionManager,
    RebalancingConfig,
    TradeTelemetry,
    create_active_position,
    create_telemetry,
)
from .base import BaseStrategy

log = structlog.get_logger()


@dataclass
class TradeResult:
    """Result of an arbitrage trade."""

    market: Market15Min
    yes_shares: float
    no_shares: float
    yes_cost: float
    no_cost: float
    total_cost: float
    expected_profit: float
    profit_percentage: float
    executed_at: datetime
    dry_run: bool = False
    success: bool = True
    error: Optional[str] = None
    trade_id: Optional[str] = None  # Dashboard trade ID for resolution tracking


@dataclass
class TrackedPosition:
    """A position we're tracking for auto-settlement."""

    condition_id: str
    token_id: str  # The token we own (YES or NO)
    shares: float
    entry_price: float
    entry_cost: float
    market_end_time: datetime
    side: str  # "YES" or "NO"
    asset: str  # "BTC", "ETH", etc.
    trade_id: Optional[str] = None
    claimed: bool = False


@dataclass
class DirectionalPosition:
    """Tracks an open directional position."""

    market: Market15Min
    side: str  # "UP" or "DOWN"
    entry_price: float
    shares: float
    cost: float
    target_price: float  # Scaled take-profit target
    stop_price: float  # Hard stop loss
    entry_time: datetime
    highest_price: float  # For trailing stop
    trailing_active: bool = False
    trade_id: Optional[str] = None

    @property
    def current_pnl_pct(self) -> float:
        """Calculate current P&L percentage based on highest price seen."""
        if self.entry_price <= 0:
            return 0.0
        return ((self.highest_price - self.entry_price) / self.entry_price) * 100

    def update_price(self, current_price: float) -> None:
        """Update highest price seen (for trailing stop)."""
        if current_price > self.highest_price:
            self.highest_price = current_price

    def should_take_profit(self, current_price: float, trailing_distance: float) -> tuple:
        """Check if we should take profit.

        Returns:
            Tuple of (should_exit, reason)
        """
        # Target hit
        if current_price >= self.target_price:
            return True, f"Target ${self.target_price:.2f} reached"

        # Trailing stop triggered (once activated)
        if self.trailing_active:
            trailing_stop = self.highest_price - trailing_distance
            if current_price <= trailing_stop:
                return True, f"Trailing stop ${trailing_stop:.2f} triggered"

        return False, ""

    def should_stop_loss(self, current_price: float) -> tuple:
        """Check if we should stop loss.

        Returns:
            Tuple of (should_exit, reason)
        """
        if current_price <= self.stop_price:
            return True, f"Stop loss ${self.stop_price:.2f} hit"
        return False, ""


class GabagoolStrategy(BaseStrategy):
    """Gabagool asymmetric binary arbitrage strategy.

    Key principles:
    1. Never predict direction - always hedge both sides
    2. Only enter when spread > threshold (e.g., 2 cents)
    3. Buy more of the cheaper side (inverse weighting)
    4. Hold until market resolution (15 minutes)
    """

    def __init__(
        self,
        client: PolymarketClient,
        ws_client: PolymarketWebSocket,
        market_finder: MarketFinder,
        config: AppConfig,
        db: Optional[Database] = None,
    ):
        """Initialize Gabagool strategy.

        Args:
            client: Polymarket CLOB client
            ws_client: WebSocket client for streaming
            market_finder: Market discovery service
            config: Application configuration
            db: Database instance for trade persistence (Phase 2)
        """
        super().__init__(client, config)
        self.ws = ws_client
        self.market_finder = market_finder
        self.gabagool_config: GabagoolConfig = config.gabagool
        self._db: Optional[Database] = db

        self._tracker: Optional[MultiMarketTracker] = None
        self._active_markets: Dict[str, Market15Min] = {}
        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0
        self._daily_exposure: float = 0.0
        self._opportunities_detected: int = 0
        self._last_reset: datetime = datetime.utcnow()
        # Track pending trades for resolution: {trade_id: TradeResult}
        self._pending_trades: Dict[str, TradeResult] = {}
        # Track directional positions: {condition_id: DirectionalPosition}
        self._directional_positions: Dict[str, DirectionalPosition] = {}
        self._directional_daily_exposure: float = 0.0
        # Queue for opportunities detected by WebSocket (callback is sync, execution is async)
        self._opportunity_queue: asyncio.Queue = asyncio.Queue()
        # Throttling for real-time price updates: {condition_id: last_broadcast_timestamp}
        self._last_price_broadcast: Dict[str, float] = {}
        # Track near-resolution trades executed: {condition_id: True} to avoid duplicates
        self._near_resolution_executed: Dict[str, bool] = {}
        # Track arbitrage positions: {condition_id: True} - markets with dual-leg positions
        self._arbitrage_positions: Dict[str, bool] = {}
        # Track positions for auto-settlement: {condition_id: [TrackedPosition, ...]}
        self._tracked_positions: Dict[str, List[TrackedPosition]] = {}
        # Last time we ran settlement check
        self._last_settlement_check: float = 0
        # Settlement check interval (every 60 seconds)
        self._settlement_check_interval: float = 60.0
        # Dedicated queue processor task (runs independently of main loop)
        self._queue_processor_task: Optional[asyncio.Task] = None
        # Liquidity collector for fill/depth logging (optional)
        self._liquidity_collector: Optional["LiquidityCollector"] = None
        # Last time we took depth snapshots
        self._last_snapshot_time: float = 0
        # Snapshot interval (every 30 seconds)
        self._snapshot_interval: float = 30.0
        # Active position manager for rebalancing
        self._position_manager: Optional[ActivePositionManager] = None
        # Pending telemetry for trades being executed: {condition_id: TradeTelemetry}
        self._pending_telemetry: Dict[str, TradeTelemetry] = {}
        # Circuit breaker state (loaded from DB on startup)
        self._circuit_breaker_hit: bool = False
        self._realized_pnl: float = 0.0
        # Blackout state (server restart protection)
        # This flag is updated by a background task every minute - trades just read it
        self._in_blackout: bool = False
        self._blackout_checker_task: Optional[asyncio.Task] = None

    def set_liquidity_collector(self, collector: "LiquidityCollector") -> None:
        """Set the liquidity collector for data collection.

        Args:
            collector: LiquidityCollector instance
        """
        self._liquidity_collector = collector
        # Also attach to client for fill logging
        self.client.set_liquidity_collector(collector)
        log.info("Liquidity collector attached to strategy")

    def set_database(self, db: Database) -> None:
        """Set the database instance for trade persistence.

        Phase 2: Strategy owns persistence - dashboard is read-only.

        Args:
            db: Database instance
        """
        self._db = db
        log.info("Database attached to strategy for trade persistence")

    async def _record_trade(
        self,
        trade_id: str,
        market: Market15Min,
        opportunity: ArbitrageOpportunity,
        yes_amount: float,
        no_amount: float,
        actual_yes_shares: float,
        actual_no_shares: float,
        hedge_ratio: float,
        execution_status: str,
        yes_order_status: str,
        no_order_status: str,
        expected_profit: float,
        dry_run: bool = False,
        pre_fill_yes_depth: float = None,
        pre_fill_no_depth: float = None,
    ) -> None:
        """Record a trade to the database.

        Phase 2: Strategy owns persistence - this is the authoritative record.
        Dashboard receives updates via events (Phase 6) or polling.

        Args:
            trade_id: Unique trade identifier
            market: Market the trade was executed on
            opportunity: Original opportunity that triggered the trade
            yes_amount: USD spent on YES leg
            no_amount: USD spent on NO leg
            actual_yes_shares: Actual YES shares filled
            actual_no_shares: Actual NO shares filled
            hedge_ratio: min(yes,no)/max(yes,no)
            execution_status: 'full_fill', 'partial_fill', 'one_leg_only', 'failed'
            yes_order_status: 'MATCHED', 'LIVE', 'FAILED'
            no_order_status: 'MATCHED', 'LIVE', 'FAILED'
            expected_profit: Expected profit based on spread
            dry_run: Whether this is a dry run
            pre_fill_yes_depth: Liquidity depth available on YES side before execution (Phase 5)
            pre_fill_no_depth: Liquidity depth available on NO side before execution (Phase 5)
        """
        if not self._db:
            log.warning("No database configured - trade not persisted", trade_id=trade_id)
            return

        market_end_time = None
        if hasattr(market, "end_time") and market.end_time:
            market_end_time = market.end_time.strftime("%H:%M UTC")

        try:
            await self._db.save_arbitrage_trade(
                trade_id=trade_id,
                asset=market.asset,
                condition_id=market.condition_id,
                yes_price=opportunity.yes_price,
                no_price=opportunity.no_price,
                yes_cost=yes_amount,
                no_cost=no_amount,
                spread=opportunity.spread_cents,
                expected_profit=expected_profit,
                yes_shares=actual_yes_shares,
                no_shares=actual_no_shares,
                hedge_ratio=hedge_ratio,
                execution_status=execution_status,
                yes_order_status=yes_order_status,
                no_order_status=no_order_status,
                market_end_time=market_end_time,
                market_slug=market.slug,
                dry_run=dry_run,
                # Phase 5: Pre-fill liquidity data
                yes_book_depth_total=pre_fill_yes_depth,
                no_book_depth_total=pre_fill_no_depth,
            )

            # Also update daily stats
            await self._db.update_daily_stats(
                trades_delta=1,
                exposure_delta=yes_amount + no_amount,
            )

            log.info(
                "Trade recorded to database",
                trade_id=trade_id,
                asset=market.asset,
                execution_status=execution_status,
                hedge_ratio=f"{hedge_ratio:.0%}" if hedge_ratio else "N/A",
            )

            # Phase 6: Emit event for dashboard (strategy owns events)
            await trade_events.emit(EventTypes.TRADE_CREATED, {
                "trade_id": trade_id,
                "asset": market.asset,
                "condition_id": market.condition_id,
                "yes_price": opportunity.yes_price,
                "no_price": opportunity.no_price,
                "yes_cost": yes_amount,
                "no_cost": no_amount,
                "spread": opportunity.spread_cents,
                "expected_profit": expected_profit,
                "yes_shares": actual_yes_shares,
                "no_shares": actual_no_shares,
                "hedge_ratio": hedge_ratio,
                "execution_status": execution_status,
                "yes_order_status": yes_order_status,
                "no_order_status": no_order_status,
                "market_end_time": market_end_time,
                "market_slug": market.slug,
                "dry_run": dry_run,
            })

        except Exception as e:
            log.error(
                "Failed to record trade to database",
                trade_id=trade_id,
                error=str(e),
            )

    async def start(self) -> None:
        """Start the Gabagool strategy."""
        if not self.gabagool_config.enabled:
            log.info("Gabagool strategy is disabled")
            return

        self._running = True
        log.info(
            "Starting Gabagool strategy",
            dry_run=self.gabagool_config.dry_run,
            min_spread=f"{self.gabagool_config.min_spread_threshold * 100:.1f}¢",
            max_trade=f"${self.gabagool_config.max_trade_size_usd:.2f}",
        )

        # Initialize tracker
        self._tracker = MultiMarketTracker(
            self.ws,
            min_spread_cents=self.gabagool_config.min_spread_threshold * 100,
        )

        # Register callback for IMMEDIATE opportunity detection
        # This fires synchronously from WebSocket handler, so we queue for async execution
        self._tracker._tracker.on_opportunity(self._queue_opportunity)

        # Register callback for real-time price updates to dashboard
        self._tracker._tracker.on_state_change(self._on_market_state_change)

        # Initialize active position manager for rebalancing
        rebalancing_config = RebalancingConfig(
            rebalance_threshold=self.gabagool_config.min_hedge_ratio,
            min_profit_per_share=0.02,  # $0.02 minimum profit per share
            max_rebalance_wait_seconds=60.0,  # Don't rebalance in last minute
            prefer_sell_over_buy=True,  # Capital efficient
            allow_partial_rebalance=True,
            max_rebalance_trades=5,
            max_position_size_usd=self.gabagool_config.max_trade_size_usd,
            min_spread_dollars=self.gabagool_config.min_spread_threshold,
        )
        self._position_manager = ActivePositionManager(
            client=self.client,
            db=self._db,
            config=rebalancing_config,
        )
        log.info("Active position manager initialized for rebalancing")

        # Load unclaimed positions from database (from previous sessions)
        await self._load_unclaimed_positions()

        # Load circuit breaker state from database
        await self._load_circuit_breaker_state()

        # Update dashboard with strategy status and circuit breaker state
        update_stats(
            arbitrage_enabled=self.gabagool_config.enabled,
            directional_enabled=self.gabagool_config.directional_enabled,
            near_resolution_enabled=self.gabagool_config.near_resolution_enabled,
            dry_run=self.gabagool_config.dry_run,
            circuit_breaker_hit=self._circuit_breaker_hit,
            realized_pnl=self._realized_pnl,
            trading_mode=self._get_trading_mode(),
        )

        # Start DEDICATED queue processor task - runs independently of main loop
        # This ensures opportunities are processed immediately without being blocked
        # by market refresh, balance updates, or other slow operations
        self._queue_processor_task = asyncio.create_task(
            self._queue_processor_loop(),
            name="opportunity-queue-processor"
        )
        log.info("Started dedicated opportunity queue processor task")

        # Check blackout status immediately on startup (don't wait for first interval)
        self._in_blackout = self._check_blackout_window()
        if self._in_blackout:
            log.warning(
                "BOT STARTED IN BLACKOUT PERIOD - trading disabled",
                until=f"{self.gabagool_config.blackout_end_hour:02d}:{self.gabagool_config.blackout_end_minute:02d}",
                timezone=self.gabagool_config.blackout_timezone,
            )
            update_stats(in_blackout=True, trading_mode="BLACKOUT")

        # Start blackout checker background task
        if self.gabagool_config.blackout_enabled:
            self._blackout_checker_task = asyncio.create_task(
                self._blackout_checker_loop(),
                name="blackout-checker"
            )

        # Start the main loop
        await self._run_loop()

    async def stop(self) -> None:
        """Stop the strategy."""
        self._running = False

        # Cancel the dedicated queue processor task
        if self._queue_processor_task and not self._queue_processor_task.done():
            self._queue_processor_task.cancel()
            try:
                await self._queue_processor_task
            except asyncio.CancelledError:
                pass
            log.info("Stopped opportunity queue processor task")

        # Cancel the blackout checker task
        if self._blackout_checker_task and not self._blackout_checker_task.done():
            self._blackout_checker_task.cancel()
            try:
                await self._blackout_checker_task
            except asyncio.CancelledError:
                pass
            log.info("Stopped blackout checker task")

        log.info(
            "Stopping Gabagool strategy",
            daily_pnl=f"${self._daily_pnl:.2f}",
            daily_trades=self._daily_trades,
        )

    def _queue_opportunity(self, opportunity: ArbitrageOpportunity) -> None:
        """Queue an opportunity for async execution (called from sync WebSocket handler)."""
        try:
            # Use put_nowait since we're in a sync context
            self._opportunity_queue.put_nowait(opportunity)
            log.info(
                "QUEUED opportunity for immediate execution",
                asset=opportunity.market.asset,
                spread_cents=f"{opportunity.spread_cents:.1f}¢",
            )
        except asyncio.QueueFull:
            log.warning("Opportunity queue full, dropping opportunity")

    def _on_market_state_change(self, state: MarketState) -> None:
        """Handle real-time price updates from WebSocket (called from sync context).

        Broadcasts price updates to dashboard with throttling (max 2 updates/sec per market).
        Also triggers rebalancing evaluation for active positions (real-time, WebSocket-driven).
        """
        import time as time_module

        condition_id = state.market.condition_id
        now = time_module.time()

        # Throttle: only broadcast every 500ms per market to avoid flooding
        last_broadcast = self._last_price_broadcast.get(condition_id, 0)
        if now - last_broadcast < 0.5:
            return

        self._last_price_broadcast[condition_id] = now

        # Update the active_markets dict if this market is in it
        # IMPORTANT: Must use dashboard.active_markets to get current reference (not imported copy)
        if condition_id in dashboard.active_markets:
            dashboard.active_markets[condition_id]["up_price"] = state.yes_price
            dashboard.active_markets[condition_id]["down_price"] = state.no_price
            # Also update time remaining (recalculated from end_time)
            dashboard.active_markets[condition_id]["seconds_remaining"] = state.market.seconds_remaining

            # Broadcast the update via SSE
            update_markets(dashboard.active_markets)

        # Trigger rebalancing evaluation for active positions (WebSocket-driven, real-time)
        # Queue for async execution since this callback is synchronous
        if self._position_manager and self._position_manager.positions:
            try:
                asyncio.create_task(
                    self._position_manager.on_price_update(condition_id, state)
                )
            except RuntimeError:
                # No event loop running - skip rebalancing check
                pass

    async def _queue_processor_loop(self) -> None:
        """Dedicated async task for processing opportunity queue.

        This runs INDEPENDENTLY of the main loop to ensure opportunities
        are executed immediately without being blocked by:
        - Market refresh (Gamma API calls)
        - Balance updates
        - Settlement checks
        - Other slow operations

        The task uses asyncio.Queue.get() with a short timeout to be responsive
        while allowing clean shutdown.
        """
        log.info("Opportunity queue processor started")

        while self._running:
            try:
                # Wait for an opportunity with a short timeout
                # This allows the task to check _running flag regularly for clean shutdown
                try:
                    opportunity = await asyncio.wait_for(
                        self._opportunity_queue.get(),
                        timeout=0.1  # 100ms timeout - very responsive
                    )
                except asyncio.TimeoutError:
                    # No opportunity in queue, loop back to check _running
                    continue

                # Log the opportunity details
                age = opportunity.age_seconds
                if opportunity.is_valid:
                    log.info(
                        "EXECUTING queued opportunity (dedicated task)",
                        asset=opportunity.market.asset,
                        spread_cents=f"{opportunity.spread_cents:.1f}¢",
                        age_seconds=f"{age:.2f}s",
                        yes_price=f"${opportunity.yes_price:.3f}",
                        no_price=f"${opportunity.no_price:.3f}",
                    )
                    await self.on_opportunity(opportunity)
                else:
                    # Opportunity expired - this is a bug we need to track
                    log.warning(
                        "OPPORTUNITY EXPIRED before execution",
                        asset=opportunity.market.asset,
                        spread_cents=f"{opportunity.spread_cents:.1f}¢",
                        age_seconds=f"{age:.2f}s",
                        validity_window=f"{opportunity.VALIDITY_SECONDS}s",
                    )
                    OPPORTUNITIES_SKIPPED.labels(reason="expired").inc()

            except asyncio.CancelledError:
                log.info("Opportunity queue processor cancelled")
                raise
            except Exception as e:
                log.error(
                    "Error in opportunity queue processor",
                    error=str(e),
                    exc_info=True,
                )
                # Brief sleep to avoid tight loop on repeated errors
                await asyncio.sleep(0.5)

        log.info("Opportunity queue processor stopped")

    async def _run_loop(self) -> None:
        """Main strategy loop.

        Note: Opportunity queue processing is handled by _queue_processor_loop
        which runs as a dedicated async task, ensuring immediate execution
        without being blocked by operations in this loop.
        """
        last_balance_update = 0
        last_market_update = 0
        balance_update_interval = 30  # Update balance every 30 seconds
        market_update_interval = 30  # Update markets every 30 seconds

        while self._running:
            try:
                # Note: Queue processing moved to dedicated _queue_processor_loop task
                # This loop now focuses on periodic maintenance tasks

                # Reset daily counters if new day
                self._check_daily_reset()

                # Update wallet balance periodically
                import time
                now = time.time()
                if now - last_balance_update >= balance_update_interval:
                    try:
                        balance_info = self.client.get_balance()
                        update_stats(wallet_balance=balance_info.get("balance", 0.0))
                        last_balance_update = now
                    except Exception as e:
                        log.debug("Failed to update balance", error=str(e))

                # Check for resolved markets and update dashboard
                await self._check_resolved_trades()

                # Check if we've hit daily limits
                if self._is_daily_limit_reached():
                    log.warning("Daily limit reached, pausing")
                    await asyncio.sleep(60)
                    continue

                # Find and track active markets (only every 30s to not block opportunities)
                if now - last_market_update >= market_update_interval:
                    await self._update_active_markets()
                    last_market_update = now

                # Fallback: Also poll for opportunities in case callback missed any
                opportunity = self._tracker.get_best_opportunity()

                if opportunity and opportunity.is_valid:
                    log.info(
                        "Opportunity found by polling (fallback)",
                        asset=opportunity.market.asset,
                        spread_cents=f"{opportunity.spread_cents:.1f}¢",
                    )
                    await self.on_opportunity(opportunity)

                # =============================================================
                # DISABLED: Directional and Near-Resolution strategies
                # These create ONE-SIDED positions (not arbitrage) which can cause
                # unbalanced trades and exceed position limits.
                # Commented out 2025-12-14 after untracked trade incident.
                # See: POST_MORTEM_2025-12-13.md
                # =============================================================
                #
                # # Check directional strategy (runs alongside arbitrage)
                # if self.gabagool_config.directional_enabled:
                #     await self._check_directional_opportunities()
                #     await self._manage_directional_positions()
                #
                # # Check near-resolution opportunities (high-confidence bets in final minute)
                # if self.gabagool_config.near_resolution_enabled:
                #     await self._check_near_resolution_opportunities()
                #
                # =============================================================

                # Check for positions to settle (every 60 seconds)
                if now - self._last_settlement_check >= self._settlement_check_interval:
                    await self._check_settlement()
                    self._last_settlement_check = now

                # Take liquidity snapshots (every 30 seconds)
                if self._liquidity_collector and now - self._last_snapshot_time >= self._snapshot_interval:
                    await self._take_liquidity_snapshots()
                    self._last_snapshot_time = now

                # Short sleep to prevent busy loop but stay responsive
                await asyncio.sleep(0.05)

            except Exception as e:
                log.error("Error in strategy loop", error=str(e))
                await asyncio.sleep(1)

    async def _update_active_markets(self) -> None:
        """Update the list of active markets being tracked."""
        markets = await self.market_finder.find_active_markets(
            assets=self.gabagool_config.markets
        )

        # Add new markets
        new_count = 0
        for market in markets:
            if market.condition_id not in self._active_markets:
                await self._tracker.add_market(market)
                self._active_markets[market.condition_id] = market
                new_count += 1
                add_log(
                    "info",
                    f"Found new market: {market.asset}",
                    question=market.question[:50] + "..." if len(market.question) > 50 else market.question,
                    seconds_remaining=int(market.seconds_remaining),
                )

        # Remove expired markets
        to_remove = []
        for cid, market in self._active_markets.items():
            if not market.is_tradeable:
                to_remove.append(cid)
                await self._tracker.remove_market(market)
                add_log("info", f"Market expired: {market.asset}")

        for cid in to_remove:
            del self._active_markets[cid]

        # Update active markets metric
        ACTIVE_MARKETS.set(len(self._active_markets))
        update_stats(active_markets=len(self._active_markets))

        # Build market data for dashboard display
        # Include ALL discovered markets (not just tradeable) so dashboard shows status
        markets_data = {}
        all_markets = self.market_finder.all_discovered_markets
        for market in all_markets:
            # Get current prices from tracker if available (WebSocket real-time)
            up_price = None
            down_price = None
            # Use the shared tracker's get_market_state method
            market_state = self._tracker.get_market_state(market.condition_id)
            if market_state and not market_state.is_stale:
                up_price = market_state.yes_price
                down_price = market_state.no_price

            # Fall back to Gamma API prices if WebSocket prices not available
            if up_price is None or up_price >= 1.0:
                up_price = market.up_price
            if down_price is None or down_price >= 1.0:
                down_price = market.down_price

            markets_data[market.condition_id] = {
                "asset": market.asset,
                "end_time": market.end_time.strftime("%H:%M UTC") if market.end_time else "N/A",
                "end_time_utc": market.end_time.isoformat() if market.end_time else None,  # For recalculating seconds_remaining
                "seconds_remaining": market.seconds_remaining,
                "up_price": up_price,
                "down_price": down_price,
                "is_tradeable": market.is_tradeable,
                "question": market.question[:60] + "..." if len(market.question) > 60 else market.question,
                "slug": market.slug,
            }

            # Log evaluation for tradeable markets with valid prices
            if market.is_tradeable and up_price and down_price:
                spread_cents = (1.0 - up_price - down_price) * 100
                min_spread = self.gabagool_config.min_spread_threshold * 100
                if spread_cents >= min_spread:
                    # Positive spread = arbitrage opportunity
                    action = "YES"
                    reason = f"Spread {spread_cents:.1f}¢ >= {min_spread:.0f}¢ threshold"
                else:
                    # No opportunity - spread too small or negative
                    action = "NO"
                    reason = f"Spread {spread_cents:.1f}¢ < {min_spread:.0f}¢ threshold"
                add_decision(
                    asset=market.asset,
                    action=action,
                    reason=reason,
                    up_price=up_price,
                    down_price=down_price,
                    spread=spread_cents,
                )

        # Send to dashboard
        update_markets(markets_data)

        # Log status periodically
        if new_count > 0 or len(to_remove) > 0:
            log.info(
                "Market update",
                active=len(self._active_markets),
                new=new_count,
                expired=len(to_remove),
            )

    async def on_opportunity(
        self,
        opportunity: ArbitrageOpportunity,
    ) -> Optional[Dict[str, Any]]:
        """Handle an arbitrage opportunity.

        Args:
            opportunity: Detected arbitrage opportunity

        Returns:
            Trade result or None
        """
        # Record opportunity detection
        OPPORTUNITIES_DETECTED.labels(market=opportunity.market.asset).inc()
        self._opportunities_detected += 1
        update_stats(opportunities_detected=self._opportunities_detected)

        # Create telemetry for timing analysis
        telemetry = create_telemetry(
            trade_id=f"pending-{opportunity.market.condition_id[:8]}",  # Temp ID
            opportunity_spread=opportunity.spread_cents,
            yes_price=opportunity.yes_price,
            no_price=opportunity.no_price,
        )
        self._pending_telemetry[opportunity.market.condition_id] = telemetry

        # Update price metrics
        YES_PRICE.labels(
            market=opportunity.market.condition_id,
            asset=opportunity.market.asset,
        ).set(opportunity.yes_price)
        NO_PRICE.labels(
            market=opportunity.market.condition_id,
            asset=opportunity.market.asset,
        ).set(opportunity.no_price)
        SPREAD_CENTS.labels(
            market=opportunity.market.condition_id,
            asset=opportunity.market.asset,
        ).set(opportunity.spread_cents)

        # Validate opportunity
        if not self._validate_opportunity(opportunity):
            OPPORTUNITIES_SKIPPED.labels(
                market=opportunity.market.asset,
                reason="validation_failed",
            ).inc()
            return None

        # Calculate position budget
        # If balance sizing is enabled, use 25% of available balance
        # Otherwise use the fixed max_trade_size_usd
        budget = self.gabagool_config.max_trade_size_usd
        if self.gabagool_config.balance_sizing_enabled:
            try:
                balance_info = self.client.get_balance()
                available_balance = balance_info.get("balance", 0.0)
                if available_balance > 0:
                    # Use configured percentage of balance
                    balance_budget = available_balance * self.gabagool_config.balance_sizing_pct
                    # Still cap at max_trade_size if configured (acts as upper bound)
                    if self.gabagool_config.max_trade_size_usd > 0:
                        budget = min(balance_budget, self.gabagool_config.max_trade_size_usd)
                    else:
                        budget = balance_budget
                    log.debug(
                        "Using balance-based sizing",
                        balance=f"${available_balance:.2f}",
                        budget=f"${budget:.2f}",
                        pct=f"{self.gabagool_config.balance_sizing_pct*100:.0f}%",
                    )
            except Exception as e:
                log.warning("Failed to get balance for sizing, using fixed max", error=str(e))

        # Enforce minimum trade size - skip if budget is too small
        # This prevents tiny trades that aren't worth the fees/effort
        min_budget_required = self.gabagool_config.min_trade_size_usd * 2  # Need min for both legs
        if budget < min_budget_required:
            log.info(
                "Budget below minimum trade size threshold",
                budget=f"${budget:.2f}",
                min_required=f"${min_budget_required:.2f}",
                min_per_leg=f"${self.gabagool_config.min_trade_size_usd:.2f}",
            )
            return None

        # Calculate position sizes
        yes_amount, no_amount = self.calculate_position_sizes(
            budget=budget,
            yes_price=opportunity.yes_price,
            no_price=opportunity.no_price,
        )

        # Pre-trade liquidity check - adjust sizes if necessary
        yes_amount, no_amount = await self._adjust_for_liquidity(
            opportunity=opportunity,
            yes_amount=yes_amount,
            no_amount=no_amount,
        )

        # Skip if liquidity check zeroed out the trade
        if yes_amount <= 0 or no_amount <= 0:
            add_decision(
                asset=opportunity.market.asset,
                action="SKIP",
                reason="Insufficient liquidity for minimum trade size",
                up_price=opportunity.yes_price,
                down_price=opportunity.no_price,
                spread=opportunity.spread_cents,
            )
            OPPORTUNITIES_SKIPPED.labels(
                market=opportunity.market.asset,
                reason="insufficient_liquidity",
            ).inc()
            return None

        # Check exposure limits (0 = unlimited)
        total_cost = yes_amount + no_amount
        if self.gabagool_config.max_daily_exposure_usd > 0:
            if self._daily_exposure + total_cost > self.gabagool_config.max_daily_exposure_usd:
                add_decision(
                    asset=opportunity.market.asset,
                    action="SKIP",
                    reason=f"Exposure limit (${self._daily_exposure:.0f}+${total_cost:.0f} > ${self.gabagool_config.max_daily_exposure_usd:.0f})",
                    up_price=opportunity.yes_price,
                    down_price=opportunity.no_price,
                    spread=opportunity.spread_cents,
                )
                log.warning("Would exceed daily exposure limit")
                OPPORTUNITIES_SKIPPED.labels(
                    market=opportunity.market.asset,
                    reason="exposure_limit",
                ).inc()
                return None

        # Execute or simulate trade
        result = await self._execute_trade(
            opportunity=opportunity,
            yes_amount=yes_amount,
            no_amount=no_amount,
        )

        if result and result.success:
            # Update tracking
            self._daily_trades += 1
            self._daily_exposure += result.total_cost
            self._daily_pnl += result.expected_profit

            # Record metrics
            dry_run_str = str(result.dry_run).lower()
            TRADES_TOTAL.labels(
                market=opportunity.market.asset,
                side="both",
                dry_run=dry_run_str,
            ).inc()
            OPPORTUNITIES_EXECUTED.labels(market=opportunity.market.asset).inc()
            TRADE_AMOUNT_USD.labels(
                market=opportunity.market.asset,
                side="yes",
            ).observe(result.yes_cost)
            TRADE_AMOUNT_USD.labels(
                market=opportunity.market.asset,
                side="no",
            ).observe(result.no_cost)
            TRADE_PROFIT_USD.labels(
                market=opportunity.market.asset,
            ).observe(result.expected_profit)

            # Update gauges
            DAILY_PNL_USD.set(self._daily_pnl)
            DAILY_TRADES.set(self._daily_trades)
            DAILY_EXPOSURE_USD.set(self._daily_exposure)

            # Update dashboard
            update_stats(
                daily_pnl=self._daily_pnl,
                daily_trades=self._daily_trades,
                daily_exposure=self._daily_exposure,
                opportunities_executed=self._daily_trades,
            )
            add_log(
                "info",
                f"TRADE: {opportunity.market.asset} +${result.expected_profit:.2f}",
                spread=f"{opportunity.spread_cents:.1f}¢",
                yes=f"${result.yes_cost:.2f}",
                no=f"${result.no_cost:.2f}",
                dry_run=result.dry_run,
            )
            add_decision(
                asset=opportunity.market.asset,
                action="TRADE",
                reason=f"Executed! +${result.expected_profit:.2f} profit {'(DRY RUN)' if result.dry_run else ''}",
                up_price=opportunity.yes_price,
                down_price=opportunity.no_price,
                spread=opportunity.spread_cents,
            )

            self.log_trade(
                action="ARBITRAGE",
                details={
                    "asset": opportunity.market.asset,
                    "yes_cost": f"${result.yes_cost:.2f}",
                    "no_cost": f"${result.no_cost:.2f}",
                    "spread": f"{opportunity.spread_cents:.1f}¢",
                    "expected_profit": f"${result.expected_profit:.2f}",
                    "dry_run": result.dry_run,
                },
            )
        elif result and not result.success:
            add_log("error", f"Trade failed: {result.error}", market=opportunity.market.asset)
            TRADE_ERRORS_TOTAL.labels(
                market=opportunity.market.asset,
                error_type=result.error or "unknown",
            ).inc()

        return result.__dict__ if result else None

    def _validate_opportunity(self, opportunity: ArbitrageOpportunity) -> bool:
        """Validate an opportunity before trading.

        Args:
            opportunity: Opportunity to validate

        Returns:
            True if opportunity is valid
        """
        min_spread_cents = self.gabagool_config.min_spread_threshold * 100

        # Check minimum spread
        if opportunity.spread_cents < min_spread_cents:
            add_decision(
                asset=opportunity.market.asset,
                action="SKIP",
                reason=f"Spread {opportunity.spread_cents:.1f}¢ < {min_spread_cents:.0f}¢ threshold",
                up_price=opportunity.yes_price,
                down_price=opportunity.no_price,
                spread=opportunity.spread_cents,
            )
            return False

        # Check market is still tradeable
        if not opportunity.market.is_tradeable:
            add_decision(
                asset=opportunity.market.asset,
                action="SKIP",
                reason="Market no longer tradeable",
                up_price=opportunity.yes_price,
                down_price=opportunity.no_price,
                spread=opportunity.spread_cents,
            )
            log.debug("Market no longer tradeable")
            return False

        # Check time remaining (at least 60 seconds)
        if opportunity.market.seconds_remaining < 60:
            add_decision(
                asset=opportunity.market.asset,
                action="SKIP",
                reason=f"Only {opportunity.market.seconds_remaining:.0f}s remaining",
                up_price=opportunity.yes_price,
                down_price=opportunity.no_price,
                spread=opportunity.spread_cents,
            )
            log.debug("Not enough time remaining")
            return False

        # Check that prices are valid (both > 0, sum < 1)
        if opportunity.yes_price <= 0 or opportunity.no_price <= 0:
            add_decision(
                asset=opportunity.market.asset,
                action="SKIP",
                reason="Invalid prices (zero or negative)",
                up_price=opportunity.yes_price,
                down_price=opportunity.no_price,
                spread=opportunity.spread_cents,
            )
            return False

        if opportunity.yes_price + opportunity.no_price >= 1.0:
            add_decision(
                asset=opportunity.market.asset,
                action="SKIP",
                reason=f"No arbitrage (sum={((opportunity.yes_price + opportunity.no_price) * 100):.1f}¢ >= 100¢)",
                up_price=opportunity.yes_price,
                down_price=opportunity.no_price,
                spread=opportunity.spread_cents,
            )
            return False

        return True

    def calculate_position_sizes(
        self,
        budget: float,
        yes_price: float,
        no_price: float,
    ) -> tuple:
        """Calculate optimal position sizes for YES and NO.

        For arbitrage profit, we need EQUAL SHARES of YES and NO.
        At resolution, one side pays $1, one pays $0.
        Profit = num_shares * $1 - (num_shares * yes_price + num_shares * no_price)
               = num_shares * (1 - yes_price - no_price)
               = num_shares * spread

        Args:
            budget: Total USD budget for this trade
            yes_price: Current YES price (0-1)
            no_price: Current NO price (0-1)

        Returns:
            Tuple of (yes_amount_usd, no_amount_usd)
        """
        cost_per_pair = yes_price + no_price

        if cost_per_pair <= 0 or cost_per_pair >= 1.0:
            return (0.0, 0.0)

        # Calculate how many share pairs we can buy with our budget
        num_pairs = budget / cost_per_pair

        # Equal shares means different dollar amounts
        # Spend MORE on the expensive side to get equal shares
        yes_amount = num_pairs * yes_price
        no_amount = num_pairs * no_price

        # Ensure we don't exceed individual trade limits
        max_single = self.gabagool_config.max_trade_size_usd
        if yes_amount > max_single or no_amount > max_single:
            # Scale down proportionally
            scale = max_single / max(yes_amount, no_amount)
            yes_amount *= scale
            no_amount *= scale

        return (yes_amount, no_amount)

    async def _adjust_for_liquidity(
        self,
        opportunity: ArbitrageOpportunity,
        yes_amount: float,
        no_amount: float,
    ) -> tuple:
        """Adjust trade sizes based on available order book liquidity.

        Queries current order book depth and scales down trade size if
        there isn't enough liquidity to fill our FOK order.

        Args:
            opportunity: The arbitrage opportunity
            yes_amount: Intended USD for YES side
            no_amount: Intended USD for NO side

        Returns:
            Tuple of (adjusted_yes_amount, adjusted_no_amount)
        """
        market = opportunity.market

        # Get current order book for both sides
        try:
            yes_book = self.client.get_order_book(market.yes_token_id)
            no_book = self.client.get_order_book(market.no_token_id)
        except Exception as e:
            log.warning(
                "Failed to get order book for liquidity check, using original sizes",
                asset=market.asset,
                error=str(e),
            )
            return (yes_amount, no_amount)

        # Calculate intended share sizes
        yes_shares = yes_amount / opportunity.yes_price if opportunity.yes_price > 0 else 0
        no_shares = no_amount / opportunity.no_price if opportunity.no_price > 0 else 0

        # Get available liquidity at our price levels
        # For BUY orders, we look at ASK side (what's being sold)
        # Handle both dict and OrderBookSummary object from py-clob-client
        if hasattr(yes_book, "asks"):
            yes_asks = yes_book.asks or []
        else:
            yes_asks = yes_book.get("asks", [])

        if hasattr(no_book, "asks"):
            no_asks = no_book.asks or []
        else:
            no_asks = no_book.get("asks", [])

        # Calculate depth at or below our target price
        yes_available = 0.0
        for ask in yes_asks:
            price = float(ask.get("price", 0) if isinstance(ask, dict) else getattr(ask, "price", 0))
            size = float(ask.get("size", 0) if isinstance(ask, dict) else getattr(ask, "size", 0))
            if price <= opportunity.yes_price:
                yes_available += size

        no_available = 0.0
        for ask in no_asks:
            price = float(ask.get("price", 0) if isinstance(ask, dict) else getattr(ask, "price", 0))
            size = float(ask.get("size", 0) if isinstance(ask, dict) else getattr(ask, "size", 0))
            if price <= opportunity.no_price:
                no_available += size

        # Use max_liquidity_consumption_pct to avoid taking all available liquidity
        max_consumption = self.gabagool_config.max_liquidity_consumption_pct
        yes_fillable = yes_available * max_consumption
        no_fillable = no_available * max_consumption

        # Check if we need to scale down
        yes_scale = 1.0 if yes_shares <= yes_fillable else (yes_fillable / yes_shares if yes_shares > 0 else 0)
        no_scale = 1.0 if no_shares <= no_fillable else (no_fillable / no_shares if no_shares > 0 else 0)

        # Use the more restrictive scale (we need BOTH sides to fill)
        scale = min(yes_scale, no_scale)

        if scale < 1.0:
            adjusted_yes = yes_amount * scale
            adjusted_no = no_amount * scale

            # Check minimum trade size (don't bother with tiny trades)
            # Use configurable min_trade_size_usd (default $3, was hardcoded at $1)
            min_trade = self.gabagool_config.min_trade_size_usd
            if adjusted_yes < min_trade or adjusted_no < min_trade:
                log.info(
                    "Liquidity too low for minimum trade size",
                    asset=market.asset,
                    yes_available=f"{yes_available:.1f} shares",
                    no_available=f"{no_available:.1f} shares",
                    intended_yes=f"${yes_amount:.2f}",
                    intended_no=f"${no_amount:.2f}",
                    adjusted_yes=f"${adjusted_yes:.2f}",
                    adjusted_no=f"${adjusted_no:.2f}",
                )
                return (0.0, 0.0)

            log.info(
                "Scaled trade size based on liquidity",
                asset=market.asset,
                scale=f"{scale:.1%}",
                yes_available=f"{yes_available:.1f} shares",
                no_available=f"{no_available:.1f} shares",
                original_yes=f"${yes_amount:.2f}",
                original_no=f"${no_amount:.2f}",
                adjusted_yes=f"${adjusted_yes:.2f}",
                adjusted_no=f"${adjusted_no:.2f}",
            )
            add_log(
                "info",
                f"Scaled trade {market.asset} to {scale:.0%} for liquidity",
                yes_liq=f"{yes_available:.0f}",
                no_liq=f"{no_available:.0f}",
            )

            return (adjusted_yes, adjusted_no)

        # Sufficient liquidity - use original sizes
        log.debug(
            "Liquidity check passed",
            asset=market.asset,
            yes_available=f"{yes_available:.1f}",
            no_available=f"{no_available:.1f}",
            yes_needed=f"{yes_shares:.1f}",
            no_needed=f"{no_shares:.1f}",
        )
        return (yes_amount, no_amount)

    async def _execute_trade(
        self,
        opportunity: ArbitrageOpportunity,
        yes_amount: float,
        no_amount: float,
    ) -> Optional[TradeResult]:
        """Execute or simulate an arbitrage trade.

        Args:
            opportunity: The arbitrage opportunity
            yes_amount: USD to spend on YES
            no_amount: USD to spend on NO

        Returns:
            Trade result
        """
        market = opportunity.market

        # Calculate expected outcomes
        yes_shares = yes_amount / opportunity.yes_price
        no_shares = no_amount / opportunity.no_price
        total_cost = yes_amount + no_amount

        # Minimum shares we'll receive at resolution
        min_shares = min(yes_shares, no_shares)
        expected_profit = min_shares - total_cost

        # CRITICAL: Validate expected profit is positive before executing
        if expected_profit <= 0:
            log.warning(
                "Rejecting trade with non-positive expected profit",
                asset=market.asset,
                expected_profit=f"${expected_profit:.4f}",
                yes_shares=yes_shares,
                no_shares=no_shares,
                total_cost=total_cost,
            )
            add_decision(
                asset=market.asset,
                action="REJECT",
                reason=f"Expected profit ${expected_profit:.2f} <= $0 (math error)",
                up_price=opportunity.yes_price,
                down_price=opportunity.no_price,
                spread=opportunity.spread_cents,
            )
            return None

        # Get market end time for dashboard display
        market_end_time = None
        if hasattr(market, "end_time") and market.end_time:
            market_end_time = market.end_time.strftime("%H:%M")

        # Check if trading is disabled (dry_run OR circuit_breaker_hit)
        trading_disabled = self._is_trading_disabled()
        trading_mode = self._get_trading_mode()

        if trading_disabled:
            # Simulate trade (dry run, circuit breaker, or blackout mode)
            if self._in_blackout:
                mode_label = "BLACKOUT"
            elif self._circuit_breaker_hit:
                mode_label = "CIRCUIT BREAKER"
            else:
                mode_label = "DRY RUN"
            log.info(
                f"{mode_label}: Would execute trade",
                asset=market.asset,
                yes_amount=f"${yes_amount:.2f}",
                no_amount=f"${no_amount:.2f}",
                expected_profit=f"${expected_profit:.2f}",
                mode=trading_mode,
            )

            # Add to dashboard trade history (kept for backward compat during transition)
            trade_id = add_trade(
                asset=market.asset,
                yes_price=opportunity.yes_price,
                no_price=opportunity.no_price,
                yes_cost=yes_amount,
                no_cost=no_amount,
                spread=opportunity.spread_cents,
                expected_profit=expected_profit,
                market_end_time=market_end_time,
                market_slug=market.slug,
                dry_run=True,  # Both dry_run and circuit_breaker show as simulated
                trading_mode=trading_mode,  # Pass actual mode for dashboard
            )

            # Phase 2: Strategy owns persistence - record simulated trade to DB
            await self._record_trade(
                trade_id=trade_id,
                market=market,
                opportunity=opportunity,
                yes_amount=yes_amount,
                no_amount=no_amount,
                actual_yes_shares=yes_shares,  # In simulated mode, intended = actual
                actual_no_shares=no_shares,
                hedge_ratio=1.0,  # Perfect hedge assumed in simulation
                execution_status="full_fill",  # Assumed success in simulation
                yes_order_status="SIMULATED",
                no_order_status="SIMULATED",
                expected_profit=expected_profit,
                dry_run=True,
            )

            result = TradeResult(
                market=market,
                yes_shares=yes_shares,
                no_shares=no_shares,
                yes_cost=yes_amount,
                no_cost=no_amount,
                total_cost=total_cost,
                expected_profit=expected_profit,
                profit_percentage=opportunity.profit_percentage,
                executed_at=datetime.utcnow(),
                dry_run=True,
                success=True,
                trade_id=trade_id,
            )

            # Track for resolution
            self._pending_trades[trade_id] = result
            return result

        # Execute real trade
        # Phase 3: Use parallel execution if enabled for better atomicity

        # ========== CRITICAL: LOG BEFORE ORDER PLACEMENT ==========
        # This log MUST appear BEFORE we attempt any real money operations
        # so we have a record even if the process crashes mid-trade
        log.warning(
            "💰 ATTEMPTING REAL TRADE - SUBMITTING ORDERS",
            asset=market.asset,
            condition_id=market.condition_id[:20] + "...",
            yes_token_id=market.yes_token_id[:16] + "...",
            no_token_id=market.no_token_id[:16] + "...",
            yes_price=f"${opportunity.yes_price:.4f}",
            no_price=f"${opportunity.no_price:.4f}",
            yes_amount_usd=f"${yes_amount:.2f}",
            no_amount_usd=f"${no_amount:.2f}",
            total_cost=f"${total_cost:.2f}",
            expected_profit=f"${expected_profit:.4f}",
            parallel_mode=self.gabagool_config.parallel_execution_enabled,
        )
        add_log(
            "warning",
            f"💰 ATTEMPTING TRADE: {market.asset}",
            yes_price=f"${opportunity.yes_price:.2f}",
            no_price=f"${opportunity.no_price:.2f}",
            total=f"${total_cost:.2f}",
        )

        # Record order_placed telemetry
        telemetry = self._pending_telemetry.get(market.condition_id)
        if telemetry:
            telemetry.record_order_placed()

        try:
            if self.gabagool_config.parallel_execution_enabled:
                log.info(
                    "Using PARALLEL execution mode with EXACT pricing (no slippage)",
                    asset=market.asset,
                    yes_price=f"${opportunity.yes_price:.2f}",
                    no_price=f"${opportunity.no_price:.2f}",
                    total_cost=f"${opportunity.yes_price + opportunity.no_price:.2f}",
                    timeout=self.gabagool_config.parallel_fill_timeout_seconds,
                    max_liquidity_pct=f"{self.gabagool_config.max_liquidity_consumption_pct*100:.0f}%",
                )
                api_result = await self.client.execute_dual_leg_order_parallel(
                    yes_token_id=market.yes_token_id,
                    no_token_id=market.no_token_id,
                    yes_amount_usd=yes_amount,
                    no_amount_usd=no_amount,
                    yes_price=opportunity.yes_price,  # EXACT price from opportunity, no slippage
                    no_price=opportunity.no_price,    # EXACT price from opportunity, no slippage
                    timeout_seconds=self.gabagool_config.parallel_fill_timeout_seconds,
                    max_liquidity_consumption_pct=self.gabagool_config.max_liquidity_consumption_pct,
                    condition_id=market.condition_id,
                    asset=market.asset,
                )
            else:
                # Legacy sequential execution
                api_result = await self.client.execute_dual_leg_order(
                    yes_token_id=market.yes_token_id,
                    no_token_id=market.no_token_id,
                    yes_amount_usd=yes_amount,
                    no_amount_usd=no_amount,
                    timeout_seconds=self.gabagool_config.order_timeout_seconds,
                    condition_id=market.condition_id,
                    asset=market.asset,
                )

            if not api_result.get("success"):
                error_msg = api_result.get("error", "Unknown error")

                # Check for partial fill (critical issue!)
                if api_result.get("partial_fill"):
                    add_log(
                        "error",
                        f"PARTIAL FILL on {market.asset}! One leg filled, other didn't.",
                        error=error_msg,
                    )
                    add_decision(
                        asset=market.asset,
                        action="PARTIAL",
                        reason=f"PARTIAL FILL - manual intervention needed!",
                        up_price=opportunity.yes_price,
                        down_price=opportunity.no_price,
                        spread=opportunity.spread_cents,
                    )

                    # Phase 2: Record partial fills - these are CRITICAL to track!
                    # Extract what actually filled from the API result
                    yes_order = api_result.get("yes_order", {})
                    no_order = api_result.get("no_order", {})
                    partial_yes = float(yes_order.get("size_matched", 0) or 0)
                    partial_no = float(no_order.get("size_matched", 0) or 0)

                    # Generate a trade_id for this partial fill
                    partial_trade_id = f"partial-{uuid.uuid4().hex[:8]}"

                    await self._record_trade(
                        trade_id=partial_trade_id,
                        market=market,
                        opportunity=opportunity,
                        yes_amount=partial_yes * opportunity.yes_price if partial_yes > 0 else 0,
                        no_amount=partial_no * opportunity.no_price if partial_no > 0 else 0,
                        actual_yes_shares=partial_yes,
                        actual_no_shares=partial_no,
                        hedge_ratio=min(partial_yes, partial_no) / max(partial_yes, partial_no) if max(partial_yes, partial_no) > 0 else 0,
                        execution_status="one_leg_only" if (partial_yes == 0 or partial_no == 0) else "partial_fill",
                        yes_order_status=yes_order.get("status", "UNKNOWN"),
                        no_order_status=no_order.get("status", "UNKNOWN"),
                        expected_profit=0,  # No expected profit on partial fill
                        dry_run=False,
                        # Phase 5: Pre-fill liquidity data
                        pre_fill_yes_depth=api_result.get("pre_fill_yes_depth"),
                        pre_fill_no_depth=api_result.get("pre_fill_no_depth"),
                    )
                else:
                    # Normal rejection (FOK didn't fill) - log details for analysis
                    # Extract liquidity info if available
                    pre_yes_depth = api_result.get("pre_fill_yes_depth", "N/A")
                    pre_no_depth = api_result.get("pre_fill_no_depth", "N/A")

                    log.warning(
                        "FOK order rejected - insufficient liquidity",
                        asset=market.asset,
                        intended_yes_shares=f"{yes_shares:.2f}",
                        intended_no_shares=f"{no_shares:.2f}",
                        intended_yes_usd=f"${yes_amount:.2f}",
                        intended_no_usd=f"${no_amount:.2f}",
                        yes_price=f"${opportunity.yes_price:.4f}",
                        no_price=f"${opportunity.no_price:.4f}",
                        pre_fill_yes_depth=pre_yes_depth,
                        pre_fill_no_depth=pre_no_depth,
                        error=error_msg,
                    )
                    add_log(
                        "warning",
                        f"FOK rejected: {market.asset} - liquidity insufficient",
                        intended=f"${total_cost:.2f}",
                        yes_depth=pre_yes_depth,
                        no_depth=pre_no_depth,
                    )
                    add_decision(
                        asset=market.asset,
                        action="REJECT",
                        reason=f"FOK rejected: insufficient liquidity",
                        up_price=opportunity.yes_price,
                        down_price=opportunity.no_price,
                        spread=opportunity.spread_cents,
                    )

                    # Record rejection metric
                    ORDER_REJECTED_TOTAL.labels(
                        market=market.asset,
                        side="DUAL",
                        reason="fok_insufficient_liquidity",
                    ).inc()

                return TradeResult(
                    market=market,
                    yes_shares=0,
                    no_shares=0,
                    yes_cost=0,
                    no_cost=0,
                    total_cost=0,
                    expected_profit=0,
                    profit_percentage=0,
                    executed_at=datetime.utcnow(),
                    dry_run=False,
                    success=False,
                    error=error_msg,
                )

            # Post-trade hedge verification (Phase 2 enforcement)
            # Extract actual filled sizes from API result
            yes_order = api_result.get("yes_order", {})
            no_order = api_result.get("no_order", {})

            # Get actual filled sizes (may differ from intended due to partial fills)
            actual_yes_shares = float(
                yes_order.get("size_matched", 0) or
                yes_order.get("matched_size", 0) or
                yes_order.get("size", 0) or
                yes_shares
            )
            actual_no_shares = float(
                no_order.get("size_matched", 0) or
                no_order.get("matched_size", 0) or
                no_order.get("size", 0) or
                no_shares
            )

            # Calculate actual hedge ratio
            min_shares = min(actual_yes_shares, actual_no_shares)
            max_shares = max(actual_yes_shares, actual_no_shares)
            actual_hedge_ratio = min_shares / max_shares if max_shares > 0 else 0
            position_imbalance = max_shares - min_shares

            log.info(
                "Post-trade hedge verification",
                asset=market.asset,
                yes_shares=actual_yes_shares,
                no_shares=actual_no_shares,
                hedge_ratio=f"{actual_hedge_ratio:.1%}",
                imbalance_shares=f"{position_imbalance:.2f}",
                min_required=f"{self._config.gabagool.min_hedge_ratio:.0%}",
            )

            # Phase 2: ENFORCE minimum hedge ratio
            # If hedge ratio is below minimum, this is a failed trade
            if actual_hedge_ratio < self._config.gabagool.min_hedge_ratio:
                error_msg = (
                    f"Hedge ratio {actual_hedge_ratio:.0%} below minimum "
                    f"{self._config.gabagool.min_hedge_ratio:.0%} - "
                    f"YES: {actual_yes_shares:.2f}, NO: {actual_no_shares:.2f}"
                )
                log.error(
                    "HEDGE RATIO ENFORCEMENT: Trade rejected due to poor hedge",
                    asset=market.asset,
                    hedge_ratio=f"{actual_hedge_ratio:.1%}",
                    min_required=f"{self._config.gabagool.min_hedge_ratio:.0%}",
                    yes_shares=actual_yes_shares,
                    no_shares=actual_no_shares,
                )
                add_log(
                    "error",
                    f"REJECTED: {market.asset} hedge ratio {actual_hedge_ratio:.0%} < {self._config.gabagool.min_hedge_ratio:.0%}",
                    yes_shares=actual_yes_shares,
                    no_shares=actual_no_shares,
                )
                TRADE_ERRORS_TOTAL.labels(error_type="hedge_ratio_violation").inc()

                # Check if we hit critical threshold (circuit breaker)
                if actual_hedge_ratio < self._config.gabagool.critical_hedge_ratio:
                    log.critical(
                        "CRITICAL: Hedge ratio below critical threshold! Consider halting.",
                        hedge_ratio=f"{actual_hedge_ratio:.1%}",
                        critical_threshold=f"{self._config.gabagool.critical_hedge_ratio:.0%}",
                    )
                    add_log(
                        "error",
                        f"CRITICAL: {market.asset} hedge ratio {actual_hedge_ratio:.0%} below {self._config.gabagool.critical_hedge_ratio:.0%}",
                    )

                # Return failed trade result
                return TradeResult(
                    market=market,
                    yes_shares=actual_yes_shares,
                    no_shares=actual_no_shares,
                    yes_cost=yes_amount,
                    no_cost=no_amount,
                    total_cost=yes_amount + no_amount,
                    expected_profit=0,  # No profit on failed hedge
                    profit_percentage=0,
                    executed_at=datetime.utcnow(),
                    dry_run=False,
                    success=False,
                    error=error_msg,
                )

            # Also check position imbalance in absolute terms
            if position_imbalance > self._config.gabagool.max_position_imbalance_shares:
                log.warning(
                    "Position imbalance exceeds maximum",
                    asset=market.asset,
                    imbalance=f"{position_imbalance:.2f} shares",
                    max_allowed=f"{self._config.gabagool.max_position_imbalance_shares:.2f} shares",
                )
                add_log(
                    "warning",
                    f"High imbalance on {market.asset}: {position_imbalance:.1f} shares unhedged",
                    yes_shares=actual_yes_shares,
                    no_shares=actual_no_shares,
                )

            # Use actual filled sizes for the trade record
            yes_shares = actual_yes_shares
            no_shares = actual_no_shares
            total_cost = yes_amount + no_amount
            expected_profit = min_shares - total_cost

            # Determine execution status for recording
            yes_order_status = yes_order.get("status", "UNKNOWN")
            no_order_status = no_order.get("status", "UNKNOWN")

            if actual_yes_shares > 0 and actual_no_shares > 0:
                if actual_hedge_ratio >= 0.95:  # Near-perfect hedge
                    execution_status = "full_fill"
                else:
                    execution_status = "partial_fill"
            elif actual_yes_shares > 0 or actual_no_shares > 0:
                execution_status = "one_leg_only"
            else:
                execution_status = "failed"

            # ========== CRITICAL: LOG ALL TRADE EXECUTIONS ==========
            log.warning(
                "🚨 TRADE EXECUTED - REAL MONEY",
                asset=market.asset,
                condition_id=market.condition_id[:20] + "...",
                yes_shares=f"{actual_yes_shares:.4f}",
                no_shares=f"{actual_no_shares:.4f}",
                yes_cost=f"${yes_amount:.2f}",
                no_cost=f"${no_amount:.2f}",
                total_cost=f"${total_cost:.2f}",
                expected_profit=f"${expected_profit:.4f}",
                hedge_ratio=f"{actual_hedge_ratio:.2%}",
                execution_status=execution_status,
                yes_order_status=yes_order_status,
                no_order_status=no_order_status,
            )
            # Also log to dashboard for visibility
            add_log(
                "warning" if execution_status != "full_fill" else "info",
                f"🚨 TRADE COMPLETE: {market.asset}",
                status=execution_status,
                yes_shares=f"{actual_yes_shares:.2f}",
                no_shares=f"{actual_no_shares:.2f}",
                total=f"${total_cost:.2f}",
            )

            # Add to dashboard trade history (kept for backward compat during transition)
            trade_id = add_trade(
                asset=market.asset,
                yes_price=opportunity.yes_price,
                no_price=opportunity.no_price,
                yes_cost=yes_amount,
                no_cost=no_amount,
                spread=opportunity.spread_cents,
                expected_profit=expected_profit,
                market_end_time=market_end_time,
                market_slug=market.slug,
                dry_run=False,
            )

            # Phase 2: Strategy owns persistence - record trade directly to DB
            await self._record_trade(
                trade_id=trade_id,
                market=market,
                opportunity=opportunity,
                yes_amount=yes_amount,
                no_amount=no_amount,
                actual_yes_shares=actual_yes_shares,
                actual_no_shares=actual_no_shares,
                hedge_ratio=actual_hedge_ratio,
                execution_status=execution_status,
                yes_order_status=yes_order_status,
                no_order_status=no_order_status,
                expected_profit=expected_profit,
                dry_run=False,
                # Phase 5: Pre-fill liquidity data
                pre_fill_yes_depth=api_result.get("pre_fill_yes_depth"),
                pre_fill_no_depth=api_result.get("pre_fill_no_depth"),
            )

            result = TradeResult(
                market=market,
                yes_shares=yes_shares,
                no_shares=no_shares,
                yes_cost=yes_amount,
                no_cost=no_amount,
                total_cost=total_cost,
                expected_profit=expected_profit,
                profit_percentage=opportunity.profit_percentage,
                executed_at=datetime.utcnow(),
                dry_run=False,
                success=True,
                trade_id=trade_id,
            )

            # Track for resolution
            self._pending_trades[trade_id] = result

            # Track positions for auto-settlement
            await self._track_position(
                market=market,
                token_id=market.yes_token_id,
                shares=yes_shares,
                entry_price=opportunity.yes_price,
                entry_cost=yes_amount,
                side="YES",
                trade_id=trade_id,
            )
            await self._track_position(
                market=market,
                token_id=market.no_token_id,
                shares=no_shares,
                entry_price=opportunity.no_price,
                entry_cost=no_amount,
                side="NO",
                trade_id=trade_id,
            )

            # Mark this market as having an arbitrage position
            # (prevents near-resolution from stacking additional single-leg trades)
            self._arbitrage_positions[market.condition_id] = True
            log.debug(
                "Marked arbitrage position",
                condition_id=market.condition_id,
                asset=market.asset,
            )

            # Update telemetry with fill info and add to active position manager
            telemetry = self._pending_telemetry.pop(market.condition_id, None)
            if telemetry and self._position_manager:
                # Update telemetry with actual trade ID and fill data
                telemetry.trade_id = trade_id
                telemetry.record_order_filled(actual_yes_shares, actual_no_shares)

                # Create active position for rebalancing management
                active_position = create_active_position(
                    trade_id=trade_id,
                    market=market,
                    yes_shares=actual_yes_shares,
                    no_shares=actual_no_shares,
                    yes_price=opportunity.yes_price,
                    no_price=opportunity.no_price,
                    telemetry=telemetry,
                    budget=yes_amount + no_amount,
                )

                # Add to position manager for active monitoring
                await self._position_manager.add_position(active_position)

                log.info(
                    "Position added to active management",
                    trade_id=trade_id,
                    asset=market.asset,
                    hedge_ratio=f"{active_position.hedge_ratio:.0%}",
                    needs_rebalancing=active_position.needs_rebalancing,
                    execution_latency_ms=telemetry.execution_latency_ms,
                )

            return result

        except Exception as e:
            log.error("Trade execution failed", error=str(e))
            return TradeResult(
                market=market,
                yes_shares=0,
                no_shares=0,
                yes_cost=0,
                no_cost=0,
                total_cost=0,
                expected_profit=0,
                profit_percentage=0,
                executed_at=datetime.utcnow(),
                dry_run=False,
                success=False,
                error=str(e),
            )

    async def _track_position(
        self,
        market: Market15Min,
        token_id: str,
        shares: float,
        entry_price: float,
        entry_cost: float,
        side: str,
        trade_id: Optional[str] = None,
    ) -> None:
        """Track a position for auto-settlement.

        Saves to both in-memory dict AND database for persistence across restarts.

        Args:
            market: Market the position is in
            token_id: Token ID of the position
            shares: Number of shares
            entry_price: Price we paid per share
            entry_cost: Total cost
            side: "YES" or "NO"
            trade_id: Associated trade ID
        """
        position = TrackedPosition(
            condition_id=market.condition_id,
            token_id=token_id,
            shares=shares,
            entry_price=entry_price,
            entry_cost=entry_cost,
            market_end_time=market.end_time,
            side=side,
            asset=market.asset,
            trade_id=trade_id,
        )

        # Track in-memory for current session
        if market.condition_id not in self._tracked_positions:
            self._tracked_positions[market.condition_id] = []

        self._tracked_positions[market.condition_id].append(position)

        # Persist to database for survival across restarts
        if self._db and trade_id:
            try:
                await self._db.add_to_settlement_queue(
                    trade_id=trade_id,
                    condition_id=market.condition_id,
                    token_id=token_id,
                    side=side,
                    asset=market.asset,
                    shares=shares,
                    entry_price=entry_price,
                    entry_cost=entry_cost,
                    market_end_time=market.end_time,
                )
            except Exception as e:
                log.error("Failed to persist position to settlement queue", error=str(e))

        log.debug(
            "Position tracked for settlement",
            asset=market.asset,
            side=side,
            shares=shares,
            condition_id=market.condition_id[:20] + "...",
        )

    async def _load_unclaimed_positions(self) -> None:
        """Load unclaimed positions from database on startup.

        This recovers positions from previous sessions that still need to be claimed.
        These positions are loaded into memory for processing by _check_settlement.
        """
        if not self._db:
            log.debug("No database configured, skipping position load")
            return

        try:
            unclaimed = await self._db.get_unclaimed_positions()

            if not unclaimed:
                log.info("No unclaimed positions found in database")
                return

            loaded_count = 0
            for pos in unclaimed:
                condition_id = pos["condition_id"]

                # Skip if already in memory (shouldn't happen on fresh start)
                if condition_id in self._tracked_positions:
                    for existing in self._tracked_positions[condition_id]:
                        if existing.token_id == pos["token_id"]:
                            continue

                # Parse market_end_time from string if needed
                market_end_time = pos["market_end_time"]
                if isinstance(market_end_time, str):
                    market_end_time = datetime.fromisoformat(market_end_time.replace("Z", "+00:00"))

                # Create TrackedPosition from database record
                position = TrackedPosition(
                    condition_id=condition_id,
                    token_id=pos["token_id"],
                    shares=pos["shares"],
                    entry_price=pos["entry_price"],
                    entry_cost=pos["entry_cost"],
                    market_end_time=market_end_time,
                    side=pos["side"],
                    asset=pos["asset"],
                    trade_id=pos["trade_id"],
                    claimed=False,
                )

                # Add to in-memory tracking
                if condition_id not in self._tracked_positions:
                    self._tracked_positions[condition_id] = []
                self._tracked_positions[condition_id].append(position)
                loaded_count += 1

            log.info(
                "Loaded unclaimed positions from database",
                count=loaded_count,
                unique_markets=len(set(p["condition_id"] for p in unclaimed)),
            )

            # Log stats
            stats = await self._db.get_settlement_stats()
            log.info(
                "Settlement queue stats",
                total=stats["total"],
                unclaimed=stats["unclaimed"],
                claimed=stats["claimed"],
                total_proceeds=f"${stats['total_proceeds']:.2f}",
                total_profit=f"${stats['total_profit']:.2f}",
            )

        except Exception as e:
            log.error("Failed to load unclaimed positions from database", error=str(e))

    async def _load_circuit_breaker_state(self) -> None:
        """Load circuit breaker state from database on startup.

        This recovers the realized PnL and circuit breaker hit status from
        the database, ensuring we maintain loss limits across restarts.
        """
        if not self._db:
            log.debug("No database configured, skipping circuit breaker load")
            return

        try:
            state = await self._db.get_circuit_breaker_state()

            self._realized_pnl = state["realized_pnl"]
            self._circuit_breaker_hit = state["circuit_breaker_hit"]

            mode = "CIRCUIT BREAKER HIT" if self._circuit_breaker_hit else "NORMAL"
            log.info(
                "Loaded circuit breaker state from database",
                mode=mode,
                realized_pnl=f"${self._realized_pnl:.2f}",
                trades_today=state["total_trades_today"],
                hit_reason=state.get("hit_reason"),
            )

            if self._circuit_breaker_hit:
                log.warning(
                    "CIRCUIT BREAKER IS ACTIVE - trading disabled until reset",
                    hit_at=state["hit_at"],
                    reason=state["hit_reason"],
                )

        except Exception as e:
            log.error("Failed to load circuit breaker state", error=str(e))
            # Default to safe state
            self._circuit_breaker_hit = False
            self._realized_pnl = 0.0

    def _is_trading_disabled(self) -> bool:
        """Check if trading is disabled (dry_run OR circuit_breaker_hit OR blackout).

        Returns:
            True if trading should be simulated, False if real trading is allowed
        """
        return self.gabagool_config.dry_run or self._circuit_breaker_hit or self._in_blackout

    def _get_trading_mode(self) -> str:
        """Get current trading mode for display.

        Returns:
            'LIVE', 'DRY_RUN', 'CIRCUIT_BREAKER', or 'BLACKOUT'
        """
        if self._in_blackout:
            return "BLACKOUT"
        elif self._circuit_breaker_hit:
            return "CIRCUIT_BREAKER"
        elif self.gabagool_config.dry_run:
            return "DRY_RUN"
        else:
            return "LIVE"

    def _check_blackout_window(self) -> bool:
        """Check if current time is within the blackout window.

        This is called by the background task - NOT during trade execution.
        Trade execution just reads self._in_blackout flag.

        Returns:
            True if in blackout window, False otherwise
        """
        if not self.gabagool_config.blackout_enabled:
            return False

        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo

        # Get current time in configured timezone (e.g., America/Chicago)
        tz = ZoneInfo(self.gabagool_config.blackout_timezone)
        now = datetime.now(tz)

        # Calculate blackout start and end times for today
        blackout_start = now.replace(
            hour=self.gabagool_config.blackout_start_hour,
            minute=self.gabagool_config.blackout_start_minute,
            second=0,
            microsecond=0,
        )
        blackout_end = now.replace(
            hour=self.gabagool_config.blackout_end_hour,
            minute=self.gabagool_config.blackout_end_minute,
            second=59,
            microsecond=999999,
        )

        return blackout_start <= now <= blackout_end

    async def _blackout_checker_loop(self) -> None:
        """Background task that checks blackout window every minute.

        Updates self._in_blackout flag which trades read.
        This design keeps blackout check off the critical trade path.
        """
        log.info(
            "Blackout checker started",
            enabled=self.gabagool_config.blackout_enabled,
            window=f"{self.gabagool_config.blackout_start_hour:02d}:{self.gabagool_config.blackout_start_minute:02d}-{self.gabagool_config.blackout_end_hour:02d}:{self.gabagool_config.blackout_end_minute:02d}",
            timezone=self.gabagool_config.blackout_timezone,
        )

        while self._running:
            try:
                was_in_blackout = self._in_blackout
                self._in_blackout = self._check_blackout_window()

                # Log state transitions
                if self._in_blackout and not was_in_blackout:
                    log.warning(
                        "BLACKOUT PERIOD STARTED - trading disabled for server restart",
                        until=f"{self.gabagool_config.blackout_end_hour:02d}:{self.gabagool_config.blackout_end_minute:02d}",
                    )
                    update_stats(
                        in_blackout=True,
                        trading_mode="BLACKOUT",
                    )

                elif not self._in_blackout and was_in_blackout:
                    # Exiting blackout - only resume if circuit breaker hasn't been hit
                    if self._circuit_breaker_hit:
                        log.info(
                            "Blackout ended but circuit breaker still active - remaining in simulation mode",
                            realized_pnl=f"${self._realized_pnl:.2f}",
                        )
                        update_stats(
                            in_blackout=False,
                            trading_mode="CIRCUIT_BREAKER",
                        )
                    else:
                        log.info(
                            "BLACKOUT PERIOD ENDED - trading resumed",
                            mode=self._get_trading_mode(),
                        )
                        update_stats(
                            in_blackout=False,
                            trading_mode=self._get_trading_mode(),
                        )

            except Exception as e:
                log.error("Blackout checker error", error=str(e))

            # Check every 60 seconds
            await asyncio.sleep(60)

    async def _check_settlement(self) -> None:
        """Check for positions that need settlement (claiming winnings).

        This method:
        1. Cancels stale GTC orders for markets that have ended
        2. Loads claimable positions from database (survives restarts)
        3. Attempts to claim winnings by selling resolved positions at 0.99
        4. Updates database with claim results
        """
        if self._is_trading_disabled():
            # Skip settlement in dry run or circuit breaker mode
            return

        now = datetime.utcnow()

        # 1. Cancel stale orders for ended markets
        active_market_ids = set(self._active_markets.keys())
        try:
            cancel_result = await self.client.cancel_stale_orders(active_market_ids)
            if cancel_result["cancelled"] > 0:
                add_log(
                    "info",
                    f"Cancelled {cancel_result['cancelled']} stale orders",
                )
        except Exception as e:
            log.error("Failed to cancel stale orders", error=str(e))

        # 2. Load claimable positions from database (positions from previous runs)
        db_positions = []
        if self._db:
            try:
                db_positions = await self._db.get_claimable_positions(wait_minutes=10)
                if db_positions:
                    log.debug(
                        "Found claimable positions in database",
                        count=len(db_positions),
                    )
            except Exception as e:
                log.error("Failed to load positions from database", error=str(e))

        # 3. Process in-memory positions (current session)
        positions_to_remove = []

        for condition_id, positions in self._tracked_positions.items():
            for position in positions:
                # Skip already claimed
                if position.claimed:
                    continue

                # Check if market has ended (with 15-min buffer for resolution)
                if position.market_end_time is None:
                    continue

                time_since_end = (now - position.market_end_time).total_seconds()

                # Wait at least 10 minutes after market end for prices to reach 0.99
                # (Per GitHub issue #117, prices reach 0.99 about 10-15 min after close)
                if time_since_end < 600:  # 10 minutes
                    continue

                # Try to claim this position
                await self._attempt_claim_position(
                    token_id=position.token_id,
                    shares=position.shares,
                    entry_cost=position.entry_cost,
                    asset=position.asset,
                    side=position.side,
                    condition_id=condition_id,
                    trade_id=position.trade_id,
                    position_obj=position,
                )

        # 4. Process database positions (from previous runs/restarts)
        for db_pos in db_positions:
            # Skip if already in memory (already processed above)
            condition_id = db_pos["condition_id"]
            token_id = db_pos["token_id"]

            # Check if this position is already tracked in memory
            already_tracked = False
            if condition_id in self._tracked_positions:
                for mem_pos in self._tracked_positions[condition_id]:
                    if mem_pos.token_id == token_id:
                        already_tracked = True
                        break

            if already_tracked:
                continue

            # Try to claim this database position
            await self._attempt_claim_position(
                token_id=token_id,
                shares=db_pos["shares"],
                entry_cost=db_pos["entry_cost"],
                asset=db_pos["asset"],
                side=db_pos["side"],
                condition_id=condition_id,
                trade_id=db_pos["trade_id"],
                position_obj=None,  # No in-memory object
                db_position_id=db_pos["id"],
            )

        # Clean up fully claimed positions (all positions for a market claimed)
        for condition_id, positions in list(self._tracked_positions.items()):
            if all(p.claimed for p in positions):
                positions_to_remove.append(condition_id)

        for condition_id in positions_to_remove:
            del self._tracked_positions[condition_id]
            log.info(
                "Removed fully settled market",
                condition_id=condition_id[:20] + "...",
            )

    async def _attempt_claim_position(
        self,
        token_id: str,
        shares: float,
        entry_cost: float,
        asset: str,
        side: str,
        condition_id: str,
        trade_id: Optional[str] = None,
        position_obj: Optional[TrackedPosition] = None,
        db_position_id: Optional[int] = None,
    ) -> bool:
        """Attempt to claim a resolved position.

        Returns True if claim was successful.
        """
        log.info(
            "Attempting to claim resolved position",
            asset=asset,
            side=side,
            shares=shares,
            condition_id=condition_id[:20] + "...",
        )

        try:
            claim_result = await self.client.claim_resolved_position(
                token_id=token_id,
                shares=shares,
                timeout_seconds=self.gabagool_config.order_timeout_seconds,
            )

            if claim_result["success"]:
                proceeds = claim_result["proceeds"]
                profit = proceeds - entry_cost

                # Mark in-memory position as claimed
                if position_obj:
                    position_obj.claimed = True

                # Update database
                if self._db and trade_id:
                    try:
                        await self._db.mark_position_claimed(
                            trade_id=trade_id,
                            token_id=token_id,
                            proceeds=proceeds,
                            profit=profit,
                        )
                    except Exception as e:
                        log.error("Failed to update claim in database", error=str(e))

                add_log(
                    "success",
                    f"Claimed {asset} {side}: +${proceeds:.2f}",
                    profit=f"${profit:.2f}",
                )

                # Update wallet balance
                try:
                    balance_info = self.client.get_balance()
                    update_stats(wallet_balance=balance_info.get("balance", 0.0))
                except Exception:
                    pass

                return True

            else:
                # Log failure and record in database for retry
                error_msg = claim_result.get("error", "Unknown error")
                log.warning(
                    "Failed to claim position, will retry",
                    error=error_msg,
                    asset=asset,
                )

                if self._db and trade_id:
                    try:
                        await self._db.record_claim_attempt(
                            trade_id=trade_id,
                            token_id=token_id,
                            error=error_msg,
                        )
                    except Exception as e:
                        log.error("Failed to record claim attempt", error=str(e))

                return False

        except Exception as e:
            log.error(
                "Error claiming position",
                error=str(e),
                asset=asset,
            )

            if self._db and trade_id:
                try:
                    await self._db.record_claim_attempt(
                        trade_id=trade_id,
                        token_id=token_id,
                        error=str(e),
                    )
                except Exception:
                    pass

            return False

    async def _take_liquidity_snapshots(self) -> None:
        """Take periodic order book depth snapshots for all active markets.

        This builds historical depth data for persistence/slippage analysis.
        See docs/LIQUIDITY_SIZING.md for the roadmap.
        """
        if not self._liquidity_collector:
            return

        snapshot_count = 0
        for condition_id, market in self._active_markets.items():
            try:
                # Snapshot YES token
                await self._liquidity_collector.take_snapshot(
                    token_id=market.yes_token_id,
                    condition_id=condition_id,
                    asset=market.asset,
                )
                snapshot_count += 1

                # Snapshot NO token
                await self._liquidity_collector.take_snapshot(
                    token_id=market.no_token_id,
                    condition_id=condition_id,
                    asset=market.asset,
                )
                snapshot_count += 1

            except Exception as e:
                log.debug("Failed to take snapshot", asset=market.asset, error=str(e))

        if snapshot_count > 0:
            log.debug(
                "Liquidity snapshots taken",
                count=snapshot_count,
                markets=len(self._active_markets),
            )

    async def _record_and_check_circuit_breaker(
        self,
        trade_id: str,
        actual_profit: float,
        pnl_type: str = "resolution",
        dry_run: bool = False,
    ) -> None:
        """Record realized PnL and check if circuit breaker should trigger.

        Args:
            trade_id: Trade ID
            actual_profit: Actual realized profit/loss
            pnl_type: Type of P&L entry
            dry_run: Whether this was a simulated trade
        """
        # Don't record PnL for simulated trades - they're not real money
        if dry_run:
            return

        if not self._db:
            return

        try:
            state = await self._db.record_realized_pnl(
                trade_id=trade_id,
                pnl_amount=actual_profit,
                pnl_type=pnl_type,
                max_daily_loss=self.gabagool_config.max_daily_loss_usd,
            )

            # Update local state
            self._realized_pnl = state["realized_pnl"]

            # Check if circuit breaker was just triggered
            if state["circuit_breaker_hit"] and not self._circuit_breaker_hit:
                self._circuit_breaker_hit = True
                log.warning(
                    "CIRCUIT BREAKER TRIGGERED - switching to simulation mode",
                    realized_pnl=f"${self._realized_pnl:.2f}",
                    max_loss=f"${self.gabagool_config.max_daily_loss_usd:.2f}",
                    trigger_trade=trade_id,
                )
                # Update dashboard immediately
                update_stats(
                    circuit_breaker_hit=True,
                    realized_pnl=self._realized_pnl,
                    trading_mode="CIRCUIT_BREAKER",
                )
            else:
                # Always update realized PnL on dashboard
                update_stats(realized_pnl=self._realized_pnl)

        except Exception as e:
            log.error(
                "Failed to record realized PnL",
                trade_id=trade_id,
                pnl=actual_profit,
                error=str(e),
            )

    async def _check_resolved_trades(self) -> None:
        """Check for resolved markets and update dashboard with results.

        For 15-minute binary markets with hedged arbitrage positions,
        we can calculate the profit deterministically:
        - At resolution, one side pays $1, the other $0
        - Profit = min(yes_shares, no_shares) - total_cost

        We use a timeout-based approach since the API resolution endpoint
        may not always return data promptly for 15-min markets.
        """
        if not self._pending_trades:
            return

        now = datetime.utcnow()
        resolved_ids = []

        # Resolution timeout: resolve 60 seconds after market ends
        # This gives Polymarket time to settle, but ensures we don't wait forever
        RESOLUTION_TIMEOUT_SECONDS = 60

        for trade_id, trade_result in list(self._pending_trades.items()):
            market = trade_result.market

            # Check if market has end_time
            if not hasattr(market, "end_time") or market.end_time is None:
                # No end_time - use execution time + 15 mins as fallback
                elapsed = (now - trade_result.executed_at).total_seconds()
                if elapsed > 900 + RESOLUTION_TIMEOUT_SECONDS:  # 15 minutes + timeout
                    # Auto-resolve based on expected profit (arbitrage is deterministic)
                    actual_profit = trade_result.expected_profit
                    won = actual_profit > 0
                    resolve_trade(trade_id, won=won, actual_profit=actual_profit)
                    resolved_ids.append(trade_id)

                    # Record realized PnL and check circuit breaker
                    await self._record_and_check_circuit_breaker(
                        trade_id, actual_profit, "resolution", trade_result.dry_run
                    )

                    log.info(
                        "Trade resolved (timeout, no end_time)",
                        asset=market.asset,
                        profit=f"${actual_profit:.2f}",
                        dry_run=trade_result.dry_run,
                    )
                continue

            # Market has end_time - check if enough time has passed
            time_since_end = (now - market.end_time).total_seconds()

            if time_since_end < 0:
                # Market hasn't ended yet
                continue

            if time_since_end < RESOLUTION_TIMEOUT_SECONDS:
                # Market ended but we're still in the grace period
                # Try API resolution first
                try:
                    resolution = await self.client.get_market_resolution(
                        condition_id=market.condition_id
                    )
                    if resolution is not None:
                        # API confirmed resolution
                        actual_profit = min(trade_result.yes_shares, trade_result.no_shares) - trade_result.total_cost
                        won = actual_profit > 0
                        resolve_trade(trade_id, won=won, actual_profit=actual_profit)
                        resolved_ids.append(trade_id)

                        # Record realized PnL and check circuit breaker
                        await self._record_and_check_circuit_breaker(
                            trade_id, actual_profit, "resolution", trade_result.dry_run
                        )

                        log.info(
                            "Trade resolved (API confirmed)",
                            asset=market.asset,
                            won=won,
                            profit=f"${actual_profit:.2f}",
                        )
                except Exception as e:
                    log.debug(
                        "Resolution API check failed, will retry or timeout",
                        trade_id=trade_id,
                        error=str(e),
                    )
                continue

            # Timeout reached - auto-resolve
            # For hedged arbitrage, profit is deterministic: min(shares) - cost
            actual_profit = min(trade_result.yes_shares, trade_result.no_shares) - trade_result.total_cost
            won = actual_profit > 0

            resolve_trade(trade_id, won=won, actual_profit=actual_profit)
            resolved_ids.append(trade_id)

            # Record realized PnL and check circuit breaker
            await self._record_and_check_circuit_breaker(
                trade_id, actual_profit, "resolution", trade_result.dry_run
            )

            log.info(
                "Trade resolved (timeout)",
                asset=market.asset,
                won=won,
                profit=f"${actual_profit:.2f}",
                time_since_end=f"{time_since_end:.0f}s",
            )

        # Remove resolved trades from tracking
        for trade_id in resolved_ids:
            del self._pending_trades[trade_id]

    def _check_daily_reset(self) -> None:
        """Reset daily counters if it's a new day."""
        now = datetime.utcnow()
        if now.date() > self._last_reset.date():
            log.info(
                "Resetting daily counters",
                previous_pnl=f"${self._daily_pnl:.2f}",
                previous_trades=self._daily_trades,
            )
            self._daily_pnl = 0.0
            self._daily_trades = 0
            self._daily_exposure = 0.0
            self._directional_daily_exposure = 0.0
            self._near_resolution_executed.clear()  # Reset near-resolution tracking
            self._arbitrage_positions.clear()  # Reset arbitrage position tracking
            self._last_reset = now

    async def _check_near_resolution_opportunities(self) -> None:
        """Check for near-resolution trading opportunities.

        Strategy: When a market is in its final minute and one side has
        price between 0.94 and 0.975, bet on that side winning.

        This catches high-confidence markets just before resolution where
        the price hasn't fully converged to $1.00 yet.
        """
        cfg = self.gabagool_config

        for condition_id, market in self._active_markets.items():
            # Skip if we already executed a near-resolution trade on this market
            if condition_id in self._near_resolution_executed:
                continue

            # Skip if we have an existing arbitrage position on this market
            # (prevents unbalanced positions from stacking arb + near-res)
            if condition_id in self._arbitrage_positions:
                log.debug(
                    "Near-res skipped: existing arbitrage position",
                    asset=market.asset,
                    condition_id=condition_id,
                )
                continue

            # Check time remaining - must be under threshold (default 60s)
            seconds_left = market.seconds_remaining
            if seconds_left > cfg.near_resolution_time_threshold:
                continue  # Too much time left

            if seconds_left <= 0:
                continue  # Already resolved

            # Get current prices from tracker
            market_state = self._tracker.get_market_state(condition_id)
            if not market_state or market_state.is_stale:
                continue

            up_price = market_state.yes_price
            down_price = market_state.no_price

            # Check if either side is in the sweet spot (0.94 to 0.975)
            target_side = None
            target_price = None
            target_token_id = None

            if cfg.near_resolution_min_price <= up_price <= cfg.near_resolution_max_price:
                target_side = "YES"
                target_price = up_price
                target_token_id = market.yes_token_id
            elif cfg.near_resolution_min_price <= down_price <= cfg.near_resolution_max_price:
                target_side = "NO"
                target_price = down_price
                target_token_id = market.no_token_id

            if not target_side:
                # Neither side is in the sweet spot - log why
                if up_price > cfg.near_resolution_max_price or down_price > cfg.near_resolution_max_price:
                    reason = f"Price too high (YES=${up_price:.2f}, NO=${down_price:.2f})"
                else:
                    reason = f"Price too low (YES=${up_price:.2f}, NO=${down_price:.2f})"
                add_decision(
                    asset=market.asset,
                    action="NR_SKIP",
                    reason=f"Near-res: {reason}",
                    up_price=up_price,
                    down_price=down_price,
                    spread=(1.0 - up_price - down_price) * 100,
                )
                continue

            # Found a near-resolution opportunity!
            log.info(
                "Near-resolution opportunity found",
                asset=market.asset,
                side=target_side,
                price=f"${target_price:.2f}",
                seconds_left=int(seconds_left),
            )

            add_decision(
                asset=market.asset,
                action="NR_BET",
                reason=f"Near-res: Betting {target_side} @ ${target_price:.2f} ({int(seconds_left)}s left)",
                up_price=up_price,
                down_price=down_price,
                spread=(1.0 - up_price - down_price) * 100,
            )

            # Execute the trade
            await self._execute_near_resolution_trade(
                market=market,
                side=target_side,
                price=target_price,
                token_id=target_token_id,
            )

            # Mark this market as traded (prevent duplicates)
            self._near_resolution_executed[condition_id] = True

    async def _execute_near_resolution_trade(
        self,
        market: Market15Min,
        side: str,
        price: float,
        token_id: str,
    ) -> None:
        """Execute a near-resolution trade.

        Args:
            market: Market to trade
            side: "YES" or "NO"
            price: Current price of the side we're betting on
            token_id: Token ID to buy
        """
        cfg = self.gabagool_config
        trade_size = cfg.near_resolution_size_usd

        # Check daily limits
        if self._daily_exposure + trade_size > cfg.max_daily_exposure_usd:
            log.warning(
                "Near-res trade skipped: daily exposure limit",
                current=self._daily_exposure,
                trade_size=trade_size,
                limit=cfg.max_daily_exposure_usd,
            )
            return

        # Calculate shares with proper decimal truncation using Decimal for precision
        # Polymarket API requires: maker amount (size) max 2 decimals
        # The internal calculation (shares * price) must also be clean
        from decimal import Decimal, ROUND_DOWN

        # Use Decimal for precise calculations
        trade_size_d = Decimal(str(trade_size))
        price_d = Decimal(str(price))

        # Calculate aggressive limit price (add 2 cents, cap at 0.99)
        limit_price_d = min(price_d + Decimal("0.02"), Decimal("0.99"))
        limit_price_d = limit_price_d.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        # Calculate shares ensuring the product (shares * price) is also clean
        # We need: shares * limit_price to have max 4 decimals (taker amount limit)
        # Since limit_price has 2 decimals, shares should have max 2 decimals
        shares_d = (trade_size_d / limit_price_d).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        # Verify the product is clean
        maker_amount = shares_d * limit_price_d
        if maker_amount != maker_amount.quantize(Decimal("0.0001"), rounding=ROUND_DOWN):
            # Round shares down further to ensure clean product
            shares_d = shares_d - Decimal("0.01")

        shares = float(shares_d)
        limit_price = float(limit_price_d)

        log.info(
            "Executing near-resolution trade",
            asset=market.asset,
            side=side,
            price=f"${price:.2f}",
            limit_price=f"${limit_price:.2f}",
            shares=f"{shares:.2f}",
            cost=f"${trade_size:.2f}",
        )

        if cfg.dry_run:
            add_log(
                "info",
                f"[DRY RUN] Near-res: {side} {shares:.2f} @ ${limit_price:.2f}",
                asset=market.asset,
            )
            return

        try:
            # Place FOK order for the target side only (not a dual-leg arb)
            from py_clob_client.clob_types import OrderArgs, OrderType

            order_args = OrderArgs(
                token_id=token_id,
                price=limit_price,
                size=shares,
                side="BUY",
            )

            # Use GTC instead of FOK - FOK has decimal precision bugs in py-clob-client
            # See: https://github.com/Polymarket/py-clob-client/issues/121
            signed_order = self.client._client.create_order(order_args)
            result = self.client._client.post_order(signed_order, orderType=OrderType.GTC)

            status = result.get("status", "").upper()
            if status in ("MATCHED", "FILLED", "LIVE"):
                # Success!
                self._daily_exposure += trade_size
                self._daily_trades += 1
                update_stats(
                    daily_exposure=self._daily_exposure,
                    daily_trades=self._daily_trades,
                )

                # Record trade in dashboard
                trade_id = add_trade(
                    asset=market.asset,
                    yes_price=price if side == "YES" else 0,
                    no_price=price if side == "NO" else 0,
                    yes_cost=trade_size if side == "YES" else 0,
                    no_cost=trade_size if side == "NO" else 0,
                    spread=0,  # Not an arb trade
                    expected_profit=(1.0 - price) * shares,  # Profit if we win
                    market_end_time=market.end_time.strftime("%H:%M UTC") if market.end_time else "N/A",
                    market_slug=market.slug,
                    dry_run=False,
                )

                add_log(
                    "success",
                    f"Near-res trade executed: {side} @ ${price:.2f}",
                    asset=market.asset,
                    trade_id=trade_id,
                    shares=shares,
                )

                log.info(
                    "Near-resolution trade filled",
                    asset=market.asset,
                    side=side,
                    shares=shares,
                    cost=trade_size,
                    trade_id=trade_id,
                )

                # Track position for auto-settlement
                await self._track_position(
                    market=market,
                    token_id=token_id,
                    shares=shares,
                    entry_price=limit_price,
                    entry_cost=trade_size,
                    side=side,
                    trade_id=trade_id,
                )
            else:
                log.warning(
                    "Near-resolution order rejected",
                    asset=market.asset,
                    status=status,
                    result=result,
                )
                add_log(
                    "warning",
                    f"Near-res order rejected: {status}",
                    asset=market.asset,
                )

        except Exception as e:
            log.error(
                "Near-resolution trade failed",
                asset=market.asset,
                error=str(e),
            )
            add_log(
                "error",
                f"Near-res trade failed: {str(e)}",
                asset=market.asset,
            )

    async def _check_directional_opportunities(self) -> None:
        """Check for directional trading opportunities on each market."""
        cfg = self.gabagool_config

        for condition_id, market in self._active_markets.items():
            # Skip if already have a position in this market
            if condition_id in self._directional_positions:
                continue

            # Get current prices from tracker
            market_state = self._tracker.get_market_state(condition_id)
            if not market_state or market_state.is_stale:
                continue

            up_price = market_state.yes_price
            down_price = market_state.no_price

            # Calculate time remaining percentage
            total_duration = 15 * 60  # 15 minutes in seconds
            time_remaining_pct = market.seconds_remaining / total_duration

            # Check entry conditions
            # 1. Must have > 80% time remaining
            if time_remaining_pct < cfg.directional_time_threshold:
                add_decision(
                    asset=market.asset,
                    action="DIR_NO",
                    reason=f"Time {time_remaining_pct*100:.0f}% < {cfg.directional_time_threshold*100:.0f}% threshold",
                    up_price=up_price,
                    down_price=down_price,
                    spread=(1.0 - up_price - down_price) * 100,
                )
                continue

            # 2. Check if either side is cheap enough
            cheaper_side = None
            cheaper_price = None

            if up_price < cfg.directional_entry_threshold and up_price <= down_price:
                cheaper_side = "UP"
                cheaper_price = up_price
            elif down_price < cfg.directional_entry_threshold and down_price < up_price:
                cheaper_side = "DOWN"
                cheaper_price = down_price

            if not cheaper_side:
                add_decision(
                    asset=market.asset,
                    action="DIR_NO",
                    reason=f"No side < ${cfg.directional_entry_threshold:.2f} (UP=${up_price:.2f}, DOWN=${down_price:.2f})",
                    up_price=up_price,
                    down_price=down_price,
                    spread=(1.0 - up_price - down_price) * 100,
                )
                continue

            # Calculate position size (1/3 of arb size)
            directional_size = cfg.max_trade_size_usd * cfg.directional_size_ratio

            # Check exposure limits
            if self._directional_daily_exposure + directional_size > cfg.max_daily_exposure_usd * 0.5:
                add_decision(
                    asset=market.asset,
                    action="DIR_NO",
                    reason=f"Directional exposure limit reached",
                    up_price=up_price,
                    down_price=down_price,
                    spread=(1.0 - up_price - down_price) * 100,
                )
                continue

            # Calculate scaled target based on entry price
            # Lower entry = higher target (better risk/reward)
            target_price = self._calculate_scaled_target(cheaper_price)

            # Entry conditions met - execute directional trade
            add_decision(
                asset=market.asset,
                action="DIR_YES",
                reason=f"{cheaper_side} ${cheaper_price:.2f} < ${cfg.directional_entry_threshold:.2f}, {time_remaining_pct*100:.0f}% time",
                up_price=up_price,
                down_price=down_price,
                spread=(1.0 - up_price - down_price) * 100,
            )

            await self._execute_directional_trade(
                market=market,
                side=cheaper_side,
                entry_price=cheaper_price,
                size_usd=directional_size,
                target_price=target_price,
            )

    def _calculate_scaled_target(self, entry_price: float) -> float:
        """Calculate scaled take-profit target based on entry price.

        Lower entry prices get more aggressive targets since risk/reward is better.

        Args:
            entry_price: Entry price (0-1)

        Returns:
            Target price for take-profit
        """
        cfg = self.gabagool_config

        # Scale target: entry $0.20 -> target $0.40 (100% gain)
        #               entry $0.25 -> target $0.45 (80% gain)
        #               entry $0.30 -> target $0.50 (67% gain)
        # Formula: target = entry * 2 (capped at base target)
        scaled_target = entry_price * 2.0

        # But don't go below base target
        return max(scaled_target, cfg.directional_target_base)

    async def _execute_directional_trade(
        self,
        market: Market15Min,
        side: str,
        entry_price: float,
        size_usd: float,
        target_price: float,
    ) -> None:
        """Execute a directional trade.

        Args:
            market: Market to trade
            side: "UP" or "DOWN"
            entry_price: Entry price
            size_usd: Size in USD
            target_price: Take-profit target
        """
        cfg = self.gabagool_config
        shares = size_usd / entry_price

        # Get market end time for display
        market_end_time = None
        if hasattr(market, "end_time") and market.end_time:
            market_end_time = market.end_time.strftime("%H:%M")

        if cfg.dry_run:
            log.info(
                "DRY RUN: Would execute directional trade",
                asset=market.asset,
                side=side,
                entry_price=f"${entry_price:.2f}",
                size=f"${size_usd:.2f}",
                target=f"${target_price:.2f}",
                stop=f"${cfg.directional_stop_loss:.2f}",
            )

            # Track the position
            position = DirectionalPosition(
                market=market,
                side=side,
                entry_price=entry_price,
                shares=shares,
                cost=size_usd,
                target_price=target_price,
                stop_price=cfg.directional_stop_loss,
                entry_time=datetime.utcnow(),
                highest_price=entry_price,
            )
            self._directional_positions[market.condition_id] = position
            self._directional_daily_exposure += size_usd

            # Log to dashboard
            add_log(
                "trade",
                f"DIRECTIONAL: {market.asset} {side} @ ${entry_price:.2f}",
                size=f"${size_usd:.2f}",
                target=f"${target_price:.2f}",
                stop=f"${cfg.directional_stop_loss:.2f}",
                dry_run=True,
            )
            return

        # Execute real trade
        try:
            token_id = market.yes_token_id if side == "UP" else market.no_token_id

            api_result = await self.client.execute_single_order(
                token_id=token_id,
                side="BUY",
                amount_usd=size_usd,
                timeout_seconds=cfg.order_timeout_seconds,
            )

            if not api_result.get("success"):
                add_log(
                    "error",
                    f"Directional trade failed: {api_result.get('error', 'Unknown')}",
                    asset=market.asset,
                    side=side,
                )
                return

            # Track the position
            position = DirectionalPosition(
                market=market,
                side=side,
                entry_price=entry_price,
                shares=shares,
                cost=size_usd,
                target_price=target_price,
                stop_price=cfg.directional_stop_loss,
                entry_time=datetime.utcnow(),
                highest_price=entry_price,
            )
            self._directional_positions[market.condition_id] = position
            self._directional_daily_exposure += size_usd

            add_log(
                "trade",
                f"DIRECTIONAL: {market.asset} {side} @ ${entry_price:.2f}",
                size=f"${size_usd:.2f}",
                target=f"${target_price:.2f}",
                stop=f"${cfg.directional_stop_loss:.2f}",
                dry_run=False,
            )

        except Exception as e:
            log.error("Directional trade execution failed", error=str(e))
            add_log("error", f"Directional trade error: {str(e)}", asset=market.asset)

    async def _manage_directional_positions(self) -> None:
        """Manage open directional positions - check for exits."""
        cfg = self.gabagool_config
        positions_to_close = []

        for condition_id, position in self._directional_positions.items():
            market = position.market

            # Get current price
            market_state = self._tracker.get_market_state(condition_id)
            if not market_state:
                continue

            current_price = market_state.yes_price if position.side == "UP" else market_state.no_price

            # Update highest price seen
            position.update_price(current_price)

            # Check trailing stop activation
            trailing_activation_price = position.target_price - cfg.directional_trailing_activation
            if current_price >= trailing_activation_price and not position.trailing_active:
                position.trailing_active = True
                add_log(
                    "info",
                    f"Trailing stop activated for {market.asset} {position.side}",
                    current=f"${current_price:.2f}",
                    highest=f"${position.highest_price:.2f}",
                )

            # Calculate time remaining
            total_duration = 15 * 60
            time_remaining_pct = market.seconds_remaining / total_duration

            # Check exit conditions
            should_exit = False
            exit_reason = ""
            exit_profit = 0.0

            # 1. Take profit check
            take_profit, tp_reason = position.should_take_profit(
                current_price, cfg.directional_trailing_distance
            )
            if take_profit:
                should_exit = True
                exit_reason = tp_reason
                exit_profit = (current_price - position.entry_price) * position.shares

            # 2. Stop loss check
            if not should_exit:
                stop_loss, sl_reason = position.should_stop_loss(current_price)
                if stop_loss:
                    should_exit = True
                    exit_reason = sl_reason
                    exit_profit = (current_price - position.entry_price) * position.shares

            # 3. Time-based exit (< 20% time remaining)
            if not should_exit and time_remaining_pct < 0.20:
                # Check if profitable
                current_pnl = (current_price - position.entry_price) * position.shares

                if current_pnl > 0:
                    # Profitable near expiry - HOLD TO RESOLUTION
                    add_log(
                        "info",
                        f"HOLDING to resolution: {market.asset} {position.side}",
                        pnl=f"+${current_pnl:.2f}",
                        time_left=f"{time_remaining_pct*100:.0f}%",
                    )
                    # Don't exit - let it ride to resolution
                else:
                    # Unprofitable near expiry - cut losses
                    should_exit = True
                    exit_reason = f"Time exit (<20%), unprofitable"
                    exit_profit = current_pnl

            # 4. Market expired - force resolution
            if not should_exit and market.seconds_remaining <= 0:
                should_exit = True
                exit_reason = "Market resolved"
                # At resolution, price is either 1.00 or 0.00
                # For dry run, simulate based on current price trend
                if current_price > 0.5:
                    exit_profit = (1.0 - position.entry_price) * position.shares
                else:
                    exit_profit = (0.0 - position.entry_price) * position.shares

            if should_exit:
                positions_to_close.append((condition_id, exit_reason, exit_profit, current_price))

        # Close positions
        for condition_id, reason, profit, exit_price in positions_to_close:
            position = self._directional_positions[condition_id]
            await self._close_directional_position(position, reason, profit, exit_price)
            del self._directional_positions[condition_id]

    async def _close_directional_position(
        self,
        position: DirectionalPosition,
        reason: str,
        profit: float,
        exit_price: float,
    ) -> None:
        """Close a directional position.

        Args:
            position: Position to close
            reason: Exit reason
            profit: P&L amount
            exit_price: Price at exit
        """
        cfg = self.gabagool_config
        won = profit > 0

        if cfg.dry_run:
            log.info(
                "DRY RUN: Would close directional position",
                asset=position.market.asset,
                side=position.side,
                entry=f"${position.entry_price:.2f}",
                exit=f"${exit_price:.2f}",
                profit=f"${profit:.2f}",
                reason=reason,
            )
        else:
            # Execute real sell order
            try:
                token_id = (
                    position.market.yes_token_id
                    if position.side == "UP"
                    else position.market.no_token_id
                )

                await self.client.execute_single_order(
                    token_id=token_id,
                    side="SELL",
                    amount_shares=position.shares,
                    timeout_seconds=cfg.order_timeout_seconds,
                )
            except Exception as e:
                log.error("Failed to close directional position", error=str(e))

        # Update P&L
        self._daily_pnl += profit

        # Log the exit
        status = "WIN" if won else "LOSS"
        add_log(
            "resolution",
            f"DIRECTIONAL {status}: {position.market.asset} {position.side}",
            entry=f"${position.entry_price:.2f}",
            exit=f"${exit_price:.2f}",
            profit=f"${profit:+.2f}",
            reason=reason,
            dry_run=cfg.dry_run,
        )

        # Update dashboard stats
        if won:
            stats["wins"] = stats.get("wins", 0) + 1
        else:
            stats["losses"] = stats.get("losses", 0) + 1

        update_stats(
            daily_pnl=self._daily_pnl,
            wins=stats.get("wins", 0),
            losses=stats.get("losses", 0),
        )

    def _is_daily_limit_reached(self) -> bool:
        """Check if daily limits have been reached."""
        # Check daily loss limit
        if self._daily_pnl < -self.gabagool_config.max_daily_loss_usd:
            log.warning(
                "Daily loss limit reached",
                loss=f"${abs(self._daily_pnl):.2f}",
            )
            return True

        # Check daily exposure limit (0 = unlimited)
        if self.gabagool_config.max_daily_exposure_usd > 0 and self._daily_exposure >= self.gabagool_config.max_daily_exposure_usd:
            log.info("Daily exposure limit reached")
            return True

        return False

    @staticmethod
    def calculate_mispricing(yes_price: float, no_price: float) -> float:
        """Calculate the mispricing/spread in a binary market.

        Args:
            yes_price: YES price (0-1)
            no_price: NO price (0-1)

        Returns:
            Spread (positive = profit opportunity)
        """
        return 1.0 - (yes_price + no_price)

    @staticmethod
    def should_enter(spread: float, threshold: float = 0.02) -> bool:
        """Determine if spread is large enough to enter.

        Args:
            spread: Current spread
            threshold: Minimum spread to enter (default 2 cents)

        Returns:
            True if should enter position
        """
        return spread >= threshold
