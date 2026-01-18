"""Near-resolution strategy for high-confidence bets in final minute of markets.

This strategy bets on outcomes when a market is in its final minute and one side
has price between min_price and max_price (e.g., 94-97.5 cents). It catches
high-confidence markets just before resolution where the price hasn't fully
converged to $1.00 yet.

WARNING: This creates one-sided (unhedged) positions. If the outcome is wrong,
the entire position is lost. Use with appropriate risk management.
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import structlog

from ..client.polymarket import PolymarketClient
from ..client.websocket import PolymarketWebSocket
from ..config import AppConfig, NearResolutionConfig
from ..persistence import Database
from ..monitoring.market_finder import Market15Min, MarketFinder
from ..monitoring.order_book import MarketState, MultiMarketTracker
from ..dashboard import add_log, add_trade, add_decision, update_stats, update_markets
from .base import BaseStrategy

if TYPE_CHECKING:
    from ..liquidity.collector import LiquidityCollector

log = structlog.get_logger()


class PositionState(str, Enum):
    """Position lifecycle states."""
    PENDING = "PENDING"
    FILLED = "FILLED"
    RESOLVED = "RESOLVED"
    LOST = "LOST"


@dataclass
class NearResolutionPosition:
    """Tracks a near-resolution position."""

    id: str
    market: Market15Min
    strategy_id: str = "near_resolution"

    side: str = ""
    shares: float = 0.0
    entry_price: float = 0.0
    cost: float = 0.0
    filled_at: Optional[datetime] = None

    state: PositionState = PositionState.PENDING

    exit_proceeds: Optional[float] = None
    closed_at: Optional[datetime] = None

    @property
    def expected_profit(self) -> float:
        """Expected profit if position wins (resolves to $1)."""
        return self.shares - self.cost

    @property
    def realized_pnl(self) -> Optional[float]:
        """Realized P&L after closing."""
        if self.state == PositionState.RESOLVED:
            return self.expected_profit
        if self.state == PositionState.LOST:
            return -self.cost
        return None


class NearResolutionStrategy(BaseStrategy):
    """Near-resolution strategy for high-confidence final-minute bets.

    Key principles:
    1. Wait for market to be within time_threshold_seconds of resolution
    2. Check if either YES or NO price is in sweet spot (min_price to max_price)
    3. Bet on the high-confidence side
    4. Hold to resolution (no exit logic - one-sided bet)
    """

    STRATEGY_ID = "near_resolution"

    def __init__(
        self,
        client: PolymarketClient,
        ws_client: PolymarketWebSocket,
        market_finder: MarketFinder,
        config: AppConfig,
        db: Optional[Database] = None,
    ):
        super().__init__(client, config)
        self.ws = ws_client
        self.market_finder = market_finder
        self.near_resolution_config: NearResolutionConfig = config.near_resolution
        self._db: Optional[Database] = db

        self._tracker: Optional[MultiMarketTracker] = None
        self._active_markets: Dict[str, Market15Min] = {}
        self._positions: Dict[str, NearResolutionPosition] = {}
        self._executed_markets: Dict[str, bool] = {}

        self._daily_exposure: float = 0.0
        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0
        self._last_reset: Optional[datetime] = None

        self._monitor_task: Optional[asyncio.Task] = None
        self._resolution_checker_task: Optional[asyncio.Task] = None

    @property
    def name(self) -> str:
        return "NearResolutionStrategy"

    async def start(self) -> None:
        if self._running:
            log.warning("Near-resolution strategy already running")
            return

        if not self.near_resolution_config.enabled:
            log.info("Near-resolution strategy disabled")
            return

        log.info(
            "Starting near-resolution strategy",
            markets=self.near_resolution_config.markets,
            time_threshold=f"{self.near_resolution_config.time_threshold_seconds:.0f}s",
            price_range=f"${self.near_resolution_config.min_price:.2f}-${self.near_resolution_config.max_price:.3f}",
            trade_size=f"${self.near_resolution_config.trade_size_usd:.2f}",
            dry_run=self.near_resolution_config.dry_run,
        )

        self._running = True
        self._last_reset = datetime.utcnow()

        await self._init_market_tracker()

        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self._resolution_checker_task = asyncio.create_task(self._resolution_checker_loop())

        add_log("Near-resolution strategy started", "info")

    async def stop(self) -> None:
        if not self._running:
            return

        log.info("Stopping near-resolution strategy")
        self._running = False

        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        if self._resolution_checker_task:
            self._resolution_checker_task.cancel()
            try:
                await self._resolution_checker_task
            except asyncio.CancelledError:
                pass

        add_log("Near-resolution strategy stopped", "info")

    async def _init_market_tracker(self) -> None:
        markets = await self.market_finder.find_active_markets()
        filtered_markets = [
            m for m in markets
            if m.asset in self.near_resolution_config.markets
        ]

        self._active_markets = {m.condition_id: m for m in filtered_markets}

        log.info(
            "Near-resolution: Found markets",
            count=len(filtered_markets),
            markets=[m.asset for m in filtered_markets],
        )

        if filtered_markets:
            self._tracker = MultiMarketTracker(
                self.ws,
                min_spread_cents=2.0,
            )

            # Collect all tokens first to batch the subscription
            # (Polymarket WebSocket only accepts ONE subscription message)
            all_tokens = []
            for market in filtered_markets:
                all_tokens.extend([market.yes_token_id, market.no_token_id])
                # Add market to tracker (no longer subscribes individually)
                await self._tracker.add_market(market)
            
            # Subscribe to all tokens in a single message
            if all_tokens:
                await self.ws.subscribe(all_tokens)

        # Update dashboard with market data
        self._update_dashboard_markets()

    def _update_dashboard_markets(self) -> None:
        """Update dashboard with current market data."""
        try:
            if not self._active_markets:
                log.debug("No active markets for dashboard update")
                update_stats(active_markets=0)
                update_markets({})
                return

            # Build market data dict for dashboard
            markets_data = {}
            for condition_id, market in self._active_markets.items():
                # Get current prices from tracker if available
                up_price = None
                down_price = None
                if self._tracker:
                    market_state = self._tracker.get_market_state(condition_id)
                    if market_state and not market_state.is_stale:
                        up_price = market_state.yes_price
                        down_price = market_state.no_price

                markets_data[condition_id] = {
                    "asset": market.asset,
                    "end_time": market.end_time.strftime("%H:%M UTC") if market.end_time else "N/A",
                    "end_time_utc": market.end_time.isoformat() if market.end_time else None,
                    "seconds_remaining": market.seconds_remaining,
                    "up_price": up_price,
                    "down_price": down_price,
                    "is_tradeable": market.is_tradeable,
                    "question": market.question[:60] + "..." if len(market.question) > 60 else market.question,
                    "slug": market.slug,
                }

            log.info(
                "Updating dashboard with near-resolution markets",
                market_count=len(markets_data),
            )
            update_stats(active_markets=len(self._active_markets))
            update_markets(markets_data)
        except Exception as e:
            log.error("Failed to update dashboard markets", error=str(e))

    async def _monitor_loop(self) -> None:
        update_counter = 0
        while self._running:
            try:
                self._check_daily_reset()
                await self._check_opportunities()
                
                # Update dashboard every 2 seconds (every 4 iterations)
                update_counter += 1
                if update_counter >= 4:
                    self._update_dashboard_markets()
                    update_counter = 0
                
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Near-resolution monitor error", error=str(e))
                await asyncio.sleep(1.0)

    async def _resolution_checker_loop(self) -> None:
        while self._running:
            try:
                await self._check_resolutions()
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Near-resolution checker error", error=str(e))
                await asyncio.sleep(1.0)

    def _check_daily_reset(self) -> None:
        now = datetime.utcnow()
        if self._last_reset is None or now.date() > self._last_reset.date():
            log.info(
                "Near-resolution daily reset",
                previous_pnl=f"${self._daily_pnl:.2f}",
                previous_trades=self._daily_trades,
            )
            self._daily_exposure = 0.0
            self._daily_pnl = 0.0
            self._daily_trades = 0
            self._executed_markets.clear()
            self._last_reset = now

    async def _check_opportunities(self) -> None:
        if not self._tracker:
            return

        cfg = self.near_resolution_config

        if self._daily_pnl <= -cfg.max_daily_loss_usd:
            return

        for condition_id, market in list(self._active_markets.items()):
            if condition_id in self._executed_markets:
                continue

            if condition_id in self._positions:
                continue

            seconds_left = market.seconds_remaining
            if seconds_left > cfg.time_threshold_seconds:
                continue

            if seconds_left <= 0:
                continue

            state = self._tracker.get_market_state(condition_id)
            if not state or state.is_stale:
                continue

            await self._evaluate_opportunity(market, state)

    async def _evaluate_opportunity(self, market: Market15Min, state: MarketState) -> None:
        cfg = self.near_resolution_config

        if self._daily_exposure + cfg.trade_size_usd > cfg.max_daily_exposure_usd:
            return

        yes_price = state.yes_price
        no_price = state.no_price

        target_side = None
        target_price = None
        target_token_id = None

        if cfg.min_price <= yes_price <= cfg.max_price:
            target_side = "YES"
            target_price = yes_price
            target_token_id = market.yes_token_id
        elif cfg.min_price <= no_price <= cfg.max_price:
            target_side = "NO"
            target_price = no_price
            target_token_id = market.no_token_id

        if not target_side:
            if yes_price > cfg.max_price or no_price > cfg.max_price:
                reason = f"Price too high (YES=${yes_price:.2f}, NO=${no_price:.2f})"
            else:
                reason = f"Price too low (YES=${yes_price:.2f}, NO=${no_price:.2f})"
            add_decision(
                f"NR_SKIP: {market.asset} - {reason}",
                "rejected",
            )
            return

        seconds_left = market.seconds_remaining
        log.info(
            "Near-resolution opportunity found",
            asset=market.asset,
            side=target_side,
            price=f"${target_price:.2f}",
            seconds_left=int(seconds_left),
        )

        add_decision(
            f"NR_BET: {market.asset} {target_side} @ ${target_price:.2f} ({int(seconds_left)}s left)",
            "approved" if not cfg.dry_run else "dry_run",
        )

        await self._execute_trade(market, target_side, target_price, target_token_id)
        self._executed_markets[market.condition_id] = True

    async def _execute_trade(
        self,
        market: Market15Min,
        side: str,
        price: float,
        token_id: str,
    ) -> None:
        cfg = self.near_resolution_config
        position_id = str(uuid.uuid4())[:8]
        trade_size = cfg.trade_size_usd

        trade_size_d = Decimal(str(trade_size))
        price_d = Decimal(str(price))

        limit_price_d = min(price_d + Decimal("0.02"), Decimal("0.99"))
        limit_price_d = limit_price_d.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        shares_d = (trade_size_d / limit_price_d).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        maker_amount = shares_d * limit_price_d
        if maker_amount != maker_amount.quantize(Decimal("0.0001"), rounding=ROUND_DOWN):
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
                f"[DRY RUN] Near-res: {side} {shares:.2f} @ ${limit_price:.2f}",
                "info",
            )
            position = NearResolutionPosition(
                id=position_id,
                market=market,
                side=side,
                shares=shares,
                entry_price=limit_price,
                cost=trade_size,
                filled_at=datetime.utcnow(),
                state=PositionState.FILLED,
            )
            self._positions[market.condition_id] = position
            self._daily_exposure += trade_size
            self._daily_trades += 1
            return

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            order_args = OrderArgs(
                token_id=token_id,
                price=limit_price,
                size=shares,
                side="BUY",
            )

            signed_order = self.client._client.create_order(order_args)
            result = self.client._client.post_order(signed_order, orderType=OrderType.GTC)

            status = result.get("status", "").upper()
            if status in ("MATCHED", "FILLED", "LIVE"):
                self._daily_exposure += trade_size
                self._daily_trades += 1

                position = NearResolutionPosition(
                    id=position_id,
                    market=market,
                    side=side,
                    shares=shares,
                    entry_price=limit_price,
                    cost=trade_size,
                    filled_at=datetime.utcnow(),
                    state=PositionState.FILLED,
                )
                self._positions[market.condition_id] = position

                trade_id = add_trade(
                    asset=market.asset,
                    yes_price=price if side == "YES" else 0,
                    no_price=price if side == "NO" else 0,
                    yes_cost=trade_size if side == "YES" else 0,
                    no_cost=trade_size if side == "NO" else 0,
                    spread=0,
                    expected_profit=(1.0 - price) * shares,
                    market_end_time=market.end_time.strftime("%H:%M UTC") if market.end_time else "N/A",
                    market_slug=market.slug,
                    dry_run=False,
                )

                add_log(
                    f"Near-res trade executed: {side} @ ${price:.2f}",
                    "success",
                )

                log.info(
                    "Near-resolution trade filled",
                    asset=market.asset,
                    side=side,
                    shares=shares,
                    cost=trade_size,
                    trade_id=trade_id,
                )

                if self._db:
                    self._db.record_trade(
                        condition_id=market.condition_id,
                        side=side,
                        shares=shares,
                        price=limit_price,
                        cost=trade_size,
                        strategy_id=self.STRATEGY_ID,
                    )
            else:
                log.warning(
                    "Near-resolution order rejected",
                    asset=market.asset,
                    status=status,
                    result=result,
                )
                add_log(
                    f"Near-res order rejected: {status}",
                    "warning",
                )

        except Exception as e:
            log.error(
                "Near-resolution trade failed",
                asset=market.asset,
                error=str(e),
            )
            add_log(
                f"Near-res trade failed: {str(e)}",
                "error",
            )

    async def _check_resolutions(self) -> None:
        for condition_id, position in list(self._positions.items()):
            if position.state != PositionState.FILLED:
                continue

            market = position.market
            now = datetime.utcnow()

            if now >= market.end_time:
                position.state = PositionState.RESOLVED
                profit = position.expected_profit
                self._daily_pnl += profit

                log.info(
                    "Near-resolution: Position resolved",
                    position_id=position.id,
                    asset=market.asset,
                    side=position.side,
                    profit=f"${profit:.2f}",
                )

    async def on_opportunity(self, opportunity: Any) -> Optional[Dict[str, Any]]:
        return None

    def get_positions(self) -> List[NearResolutionPosition]:
        return list(self._positions.values())

    def get_active_positions(self) -> List[NearResolutionPosition]:
        return [
            p for p in self._positions.values()
            if p.state == PositionState.FILLED
        ]

    def get_stats(self) -> Dict[str, Any]:
        active = self.get_active_positions()
        return {
            "enabled": self.near_resolution_config.enabled,
            "running": self._running,
            "dry_run": self.near_resolution_config.dry_run,
            "positions_total": len(self._positions),
            "positions_active": len(active),
            "daily_exposure": self._daily_exposure,
            "daily_trades": self._daily_trades,
            "daily_pnl": self._daily_pnl,
        }
