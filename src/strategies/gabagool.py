"""Gabagool arbitrage strategy for 15-minute up/down markets.

This strategy exploits temporary mispricing in binary markets where:
- YES + NO should sum to $1.00
- When sum < $1.00, buying both guarantees profit
- Profit = $1.00 - (YES_cost + NO_cost)

Named after the successful Polymarket trader @gabagool22.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

import structlog

from ..client.polymarket import PolymarketClient
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
    ):
        """Initialize Gabagool strategy.

        Args:
            client: Polymarket CLOB client
            ws_client: WebSocket client for streaming
            market_finder: Market discovery service
            config: Application configuration
        """
        super().__init__(client, config)
        self.ws = ws_client
        self.market_finder = market_finder
        self.gabagool_config: GabagoolConfig = config.gabagool

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

        # Update dashboard with strategy status
        update_stats(
            arbitrage_enabled=self.gabagool_config.enabled,
            directional_enabled=self.gabagool_config.directional_enabled,
            near_resolution_enabled=self.gabagool_config.near_resolution_enabled,
        )

        # Start the main loop
        await self._run_loop()

    async def stop(self) -> None:
        """Stop the strategy."""
        self._running = False
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

    async def _run_loop(self) -> None:
        """Main strategy loop."""
        last_balance_update = 0
        last_market_update = 0
        balance_update_interval = 30  # Update balance every 30 seconds
        market_update_interval = 30  # Update markets every 30 seconds (don't block opportunities)

        while self._running:
            try:
                # PRIORITY 1: Process queued opportunities IMMEDIATELY
                # These come from WebSocket callbacks and need instant execution
                while not self._opportunity_queue.empty():
                    try:
                        opportunity = self._opportunity_queue.get_nowait()
                        if opportunity.is_valid:
                            log.info(
                                "EXECUTING queued opportunity",
                                asset=opportunity.market.asset,
                                spread_cents=f"{opportunity.spread_cents:.1f}¢",
                            )
                            await self.on_opportunity(opportunity)
                        else:
                            log.debug("Queued opportunity expired", asset=opportunity.market.asset)
                    except asyncio.QueueEmpty:
                        break

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

                # Check directional strategy (runs alongside arbitrage)
                if self.gabagool_config.directional_enabled:
                    await self._check_directional_opportunities()
                    await self._manage_directional_positions()

                # Check near-resolution opportunities (high-confidence bets in final minute)
                if self.gabagool_config.near_resolution_enabled:
                    await self._check_near_resolution_opportunities()

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

        # Calculate position sizes
        yes_amount, no_amount = self.calculate_position_sizes(
            budget=self.gabagool_config.max_trade_size_usd,
            yes_price=opportunity.yes_price,
            no_price=opportunity.no_price,
        )

        # Check exposure limits
        total_cost = yes_amount + no_amount
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

        if self.gabagool_config.dry_run:
            # Simulate trade
            log.info(
                "DRY RUN: Would execute trade",
                asset=market.asset,
                yes_amount=f"${yes_amount:.2f}",
                no_amount=f"${no_amount:.2f}",
                expected_profit=f"${expected_profit:.2f}",
            )

            # Add to dashboard trade history
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
        try:
            api_result = await self.client.execute_dual_leg_order(
                yes_token_id=market.yes_token_id,
                no_token_id=market.no_token_id,
                yes_amount_usd=yes_amount,
                no_amount_usd=no_amount,
                timeout_seconds=self.gabagool_config.order_timeout_seconds,
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
                else:
                    # Normal rejection (FOK didn't fill) - this is fine
                    add_decision(
                        asset=market.asset,
                        action="REJECT",
                        reason=f"Order rejected: {error_msg}",
                        up_price=opportunity.yes_price,
                        down_price=opportunity.no_price,
                        spread=opportunity.spread_cents,
                    )

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

            # Add to dashboard trade history
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

    async def _check_resolved_trades(self) -> None:
        """Check for resolved markets and update dashboard with results.

        For 15-minute markets, we check if the market has ended and
        then query for the resolution to determine win/loss.
        """
        if not self._pending_trades:
            return

        now = datetime.utcnow()
        resolved_ids = []

        for trade_id, trade_result in self._pending_trades.items():
            market = trade_result.market

            # Check if market has ended (give a 30-second buffer)
            if not hasattr(market, "end_time") or market.end_time is None:
                # For dry run simulations without end_time, auto-resolve after 15 mins
                if trade_result.dry_run:
                    elapsed = (now - trade_result.executed_at).total_seconds()
                    if elapsed > 900:  # 15 minutes
                        # Simulate a win (arbitrage should always profit if executed correctly)
                        actual_profit = trade_result.expected_profit
                        resolve_trade(trade_id, won=True, actual_profit=actual_profit)
                        resolved_ids.append(trade_id)
                        log.info(
                            "DRY RUN: Simulated resolution",
                            asset=market.asset,
                            profit=f"${actual_profit:.2f}",
                        )
                continue

            # Market has end_time - check if resolved
            if now < market.end_time:
                continue

            # Market ended - query for resolution
            try:
                # Get market resolution from API
                resolution = await self.client.get_market_resolution(
                    condition_id=market.condition_id
                )

                if resolution is None:
                    # Not yet resolved, wait
                    continue

                # Calculate actual profit based on resolution
                # In arbitrage, we hold both YES and NO
                # One side pays $1, the other pays $0
                # Profit = $1 * min(yes_shares, no_shares) - total_cost
                actual_profit = min(trade_result.yes_shares, trade_result.no_shares) - trade_result.total_cost

                # We "win" if we made profit (which should always happen in arbitrage)
                won = actual_profit > 0

                resolve_trade(trade_id, won=won, actual_profit=actual_profit)
                resolved_ids.append(trade_id)

                log.info(
                    "Market resolved",
                    asset=market.asset,
                    won=won,
                    profit=f"${actual_profit:.2f}",
                )

            except Exception as e:
                log.error(
                    "Error checking resolution",
                    trade_id=trade_id,
                    error=str(e),
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

        # Calculate shares
        shares = round(trade_size / price, 2)

        log.info(
            "Executing near-resolution trade",
            asset=market.asset,
            side=side,
            price=f"${price:.2f}",
            shares=shares,
            cost=f"${trade_size:.2f}",
        )

        if cfg.dry_run:
            add_log(
                "info",
                f"[DRY RUN] Near-res: {side} {shares:.2f} @ ${price:.2f}",
                asset=market.asset,
            )
            return

        try:
            # Place FOK order for the target side only (not a dual-leg arb)
            from py_clob_client.clob_types import OrderArgs, OrderType

            # Slightly aggressive price to ensure fill
            limit_price = round(min(price + 0.02, 0.99), 2)

            order_args = OrderArgs(
                token_id=token_id,
                price=limit_price,
                size=shares,
                side="BUY",
                order_type=OrderType.FOK,
            )

            signed_order = self.client._client.create_order(order_args)
            result = self.client._client.post_order(signed_order)

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

        # Check daily exposure limit
        if self._daily_exposure >= self.gabagool_config.max_daily_exposure_usd:
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
