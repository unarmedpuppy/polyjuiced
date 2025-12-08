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
from ..monitoring.market_finder import Market15Min, MarketFinder
from ..monitoring.order_book import (
    ArbitrageOpportunity,
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
        self._last_reset: datetime = datetime.utcnow()

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

    async def _run_loop(self) -> None:
        """Main strategy loop."""
        while self._running:
            try:
                # Reset daily counters if new day
                self._check_daily_reset()

                # Check if we've hit daily limits
                if self._is_daily_limit_reached():
                    log.warning("Daily limit reached, pausing")
                    await asyncio.sleep(60)
                    continue

                # Find and track active markets
                await self._update_active_markets()

                # Get best opportunity
                opportunity = self._tracker.get_best_opportunity()

                if opportunity and opportunity.is_valid:
                    await self.on_opportunity(opportunity)

                # Short sleep to prevent busy loop
                await asyncio.sleep(0.1)

            except Exception as e:
                log.error("Error in strategy loop", error=str(e))
                await asyncio.sleep(1)

    async def _update_active_markets(self) -> None:
        """Update the list of active markets being tracked."""
        markets = await self.market_finder.find_active_markets(
            assets=self.gabagool_config.markets
        )

        # Add new markets
        for market in markets:
            if market.condition_id not in self._active_markets:
                await self._tracker.add_market(market)
                self._active_markets[market.condition_id] = market

        # Remove expired markets
        to_remove = []
        for cid, market in self._active_markets.items():
            if not market.is_tradeable:
                to_remove.append(cid)
                await self._tracker.remove_market(market)

        for cid in to_remove:
            del self._active_markets[cid]

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
        # Validate opportunity
        if not self._validate_opportunity(opportunity):
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
            log.warning("Would exceed daily exposure limit")
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

        return result.__dict__ if result else None

    def _validate_opportunity(self, opportunity: ArbitrageOpportunity) -> bool:
        """Validate an opportunity before trading.

        Args:
            opportunity: Opportunity to validate

        Returns:
            True if opportunity is valid
        """
        # Check minimum spread
        if opportunity.spread_cents < self.gabagool_config.min_spread_threshold * 100:
            return False

        # Check market is still tradeable
        if not opportunity.market.is_tradeable:
            log.debug("Market no longer tradeable")
            return False

        # Check time remaining (at least 60 seconds)
        if opportunity.market.seconds_remaining < 60:
            log.debug("Not enough time remaining")
            return False

        # Check that prices are valid (both > 0, sum < 1)
        if opportunity.yes_price <= 0 or opportunity.no_price <= 0:
            return False

        if opportunity.yes_price + opportunity.no_price >= 1.0:
            return False

        return True

    def calculate_position_sizes(
        self,
        budget: float,
        yes_price: float,
        no_price: float,
    ) -> tuple:
        """Calculate optimal position sizes for YES and NO.

        Uses inverse weighting: buy more of the cheaper side.

        Args:
            budget: Total USD budget for this trade
            yes_price: Current YES price (0-1)
            no_price: Current NO price (0-1)

        Returns:
            Tuple of (yes_amount_usd, no_amount_usd)
        """
        total_price = yes_price + no_price

        if total_price <= 0:
            return (0.0, 0.0)

        # Inverse weighting: allocate more to cheaper side
        # If YES is cheaper (0.40), we want more YES
        # If NO is cheaper (0.45), we want more NO
        yes_weight = no_price / total_price  # Higher when YES is cheaper
        no_weight = yes_price / total_price  # Higher when NO is cheaper

        yes_amount = budget * yes_weight
        no_amount = budget * no_weight

        # Ensure we don't exceed individual trade limits
        max_single = self.gabagool_config.max_trade_size_usd
        yes_amount = min(yes_amount, max_single)
        no_amount = min(no_amount, max_single)

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

        if self.gabagool_config.dry_run:
            # Simulate trade
            log.info(
                "DRY RUN: Would execute trade",
                asset=market.asset,
                yes_amount=f"${yes_amount:.2f}",
                no_amount=f"${no_amount:.2f}",
                expected_profit=f"${expected_profit:.2f}",
            )

            return TradeResult(
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
            )

        # Execute real trade
        try:
            result = await self.client.execute_dual_leg_order(
                yes_token_id=market.yes_token_id,
                no_token_id=market.no_token_id,
                yes_amount_usd=yes_amount,
                no_amount_usd=no_amount,
                timeout_seconds=self.gabagool_config.order_timeout_seconds,
            )

            if not result.get("success"):
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
                    error=result.get("error", "Unknown error"),
                )

            return TradeResult(
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
            )

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
            self._last_reset = now

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
