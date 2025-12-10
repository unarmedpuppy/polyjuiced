"""Real-time order book tracking and arbitrage opportunity detection."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional

import structlog

from ..client.websocket import OrderBookUpdate, PolymarketWebSocket
from .market_finder import Market15Min

log = structlog.get_logger()


@dataclass
class MarketState:
    """Current state of a 15-minute market's order books."""

    market: Market15Min
    yes_best_bid: float = 0.0
    yes_best_ask: float = 1.0
    no_best_bid: float = 0.0
    no_best_ask: float = 1.0
    last_update: Optional[datetime] = None

    @property
    def yes_price(self) -> float:
        """Cost to buy YES (best ask)."""
        return self.yes_best_ask

    @property
    def no_price(self) -> float:
        """Cost to buy NO (best ask)."""
        return self.no_best_ask

    @property
    def combined_cost(self) -> float:
        """Total cost to buy 1 YES + 1 NO."""
        return self.yes_price + self.no_price

    @property
    def spread(self) -> float:
        """Arbitrage spread: 1.0 - (YES + NO).

        Positive spread = guaranteed profit opportunity.
        """
        return 1.0 - self.combined_cost

    @property
    def spread_cents(self) -> float:
        """Spread in cents."""
        return self.spread * 100

    @property
    def is_arbitrage_opportunity(self) -> bool:
        """Check if there's a profitable arbitrage opportunity."""
        return self.spread > 0

    @property
    def profit_percentage(self) -> float:
        """Expected profit as percentage of investment."""
        if self.combined_cost <= 0:
            return 0.0
        return (self.spread / self.combined_cost) * 100

    @property
    def is_stale(self) -> bool:
        """Check if data is stale (> 10 seconds old)."""
        if not self.last_update:
            return True
        age = (datetime.utcnow() - self.last_update).total_seconds()
        return age > 10


@dataclass
class ArbitrageOpportunity:
    """Represents a detected arbitrage opportunity."""

    market: Market15Min
    yes_price: float
    no_price: float
    spread: float
    spread_cents: float
    profit_percentage: float
    detected_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def is_valid(self) -> bool:
        """Check if opportunity is still valid (recent)."""
        age = (datetime.utcnow() - self.detected_at).total_seconds()
        return age < 5  # Valid for 5 seconds


class OrderBookTracker:
    """Tracks order books and detects arbitrage opportunities."""

    def __init__(
        self,
        ws_client: PolymarketWebSocket,
        min_spread_cents: float = 2.0,
    ):
        """Initialize order book tracker.

        Args:
            ws_client: WebSocket client for streaming
            min_spread_cents: Minimum spread in cents to trigger opportunity
        """
        self.ws = ws_client
        self.min_spread_cents = min_spread_cents

        self._markets: Dict[str, MarketState] = {}
        self._token_to_market: Dict[str, str] = {}  # token_id -> condition_id
        self._token_side: Dict[str, str] = {}  # token_id -> "yes" or "no"

        # Callbacks
        self._on_opportunity: Optional[
            Callable[[ArbitrageOpportunity], None]
        ] = None
        self._on_state_change: Optional[
            Callable[[MarketState], None]
        ] = None

    def on_opportunity(
        self,
        callback: Callable[[ArbitrageOpportunity], None],
    ) -> None:
        """Register callback for arbitrage opportunities.

        Args:
            callback: Function called when opportunity detected
        """
        self._on_opportunity = callback

    def on_state_change(
        self,
        callback: Callable[[MarketState], None],
    ) -> None:
        """Register callback for any market state change.

        Args:
            callback: Function called when market state updates
        """
        self._on_state_change = callback

    async def track_market(self, market: Market15Min) -> None:
        """Start tracking a market's order books.

        Args:
            market: Market to track
        """
        condition_id = market.condition_id

        # Initialize market state
        self._markets[condition_id] = MarketState(market=market)

        # Map tokens to market
        self._token_to_market[market.yes_token_id] = condition_id
        self._token_to_market[market.no_token_id] = condition_id
        self._token_side[market.yes_token_id] = "yes"
        self._token_side[market.no_token_id] = "no"

        # Register handler and subscribe
        self.ws.on_book_update(self._handle_book_update)
        await self.ws.subscribe([market.yes_token_id, market.no_token_id])

        log.info(
            "Tracking market",
            condition_id=condition_id,
            asset=market.asset,
            question=market.question[:50],
        )

    async def untrack_market(self, market: Market15Min) -> None:
        """Stop tracking a market.

        Args:
            market: Market to stop tracking
        """
        condition_id = market.condition_id

        # Unsubscribe from tokens
        await self.ws.unsubscribe([market.yes_token_id, market.no_token_id])

        # Clean up mappings
        self._token_to_market.pop(market.yes_token_id, None)
        self._token_to_market.pop(market.no_token_id, None)
        self._token_side.pop(market.yes_token_id, None)
        self._token_side.pop(market.no_token_id, None)
        self._markets.pop(condition_id, None)

        log.info("Stopped tracking market", condition_id=condition_id)

    def get_market_state(self, condition_id: str) -> Optional[MarketState]:
        """Get current state of a tracked market.

        Args:
            condition_id: Market condition ID

        Returns:
            Current market state or None
        """
        return self._markets.get(condition_id)

    def get_all_opportunities(self) -> List[ArbitrageOpportunity]:
        """Get all current arbitrage opportunities.

        Returns:
            List of opportunities sorted by spread (best first)
        """
        opportunities = []

        for state in self._markets.values():
            if state.is_stale:
                continue

            if state.spread_cents >= self.min_spread_cents:
                opp = ArbitrageOpportunity(
                    market=state.market,
                    yes_price=state.yes_price,
                    no_price=state.no_price,
                    spread=state.spread,
                    spread_cents=state.spread_cents,
                    profit_percentage=state.profit_percentage,
                )
                opportunities.append(opp)

        # Sort by spread descending (best opportunities first)
        opportunities.sort(key=lambda o: o.spread, reverse=True)
        return opportunities

    def _handle_book_update(self, update: OrderBookUpdate) -> None:
        """Handle incoming order book update.

        Args:
            update: Order book update from WebSocket
        """
        token_id = update.token_id

        # Find the market this token belongs to
        # The WebSocket may return full token IDs (77+ chars) while Gamma API
        # returns truncated ones (20 chars). Use prefix matching as fallback.
        condition_id = self._token_to_market.get(token_id)
        if not condition_id and token_id:
            # Try prefix matching - check if any stored token is a prefix of the incoming token
            for stored_token, cid in self._token_to_market.items():
                if token_id.startswith(stored_token):
                    condition_id = cid
                    # Also store the full token for future lookups
                    self._token_to_market[token_id] = cid
                    if stored_token in self._token_side:
                        self._token_side[token_id] = self._token_side[stored_token]
                    log.debug("Token matched by prefix", short=stored_token[:16], full=token_id[:20])
                    break

        if not condition_id:
            # Only log once per unique token to avoid spam
            if not hasattr(self, '_unknown_tokens'):
                self._unknown_tokens = set()
            token_prefix = token_id[:20] if token_id else "None"
            if token_prefix not in self._unknown_tokens:
                self._unknown_tokens.add(token_prefix)
                log.debug("Ignoring unknown token", token_id=token_prefix)
            return

        state = self._markets.get(condition_id)
        if not state:
            return

        # Update the appropriate side
        side = self._token_side.get(token_id)
        if side == "yes":
            state.yes_best_bid = update.best_bid
            state.yes_best_ask = update.best_ask
        elif side == "no":
            state.no_best_bid = update.best_bid
            state.no_best_ask = update.best_ask

        state.last_update = update.timestamp

        # Emit state change
        if self._on_state_change:
            self._on_state_change(state)

        # Check for arbitrage opportunity
        if state.spread_cents >= self.min_spread_cents:
            opportunity = ArbitrageOpportunity(
                market=state.market,
                yes_price=state.yes_price,
                no_price=state.no_price,
                spread=state.spread,
                spread_cents=state.spread_cents,
                profit_percentage=state.profit_percentage,
            )

            log.info(
                "Arbitrage opportunity detected",
                asset=state.market.asset,
                yes_price=f"${state.yes_price:.3f}",
                no_price=f"${state.no_price:.3f}",
                spread_cents=f"{state.spread_cents:.1f}¢",
                profit_pct=f"{state.profit_percentage:.2f}%",
            )

            if self._on_opportunity:
                self._on_opportunity(opportunity)


class MultiMarketTracker:
    """Tracks multiple markets simultaneously with a single shared token mapping."""

    def __init__(
        self,
        ws_client: PolymarketWebSocket,
        min_spread_cents: float = 2.0,
    ):
        """Initialize multi-market tracker.

        Args:
            ws_client: WebSocket client
            min_spread_cents: Minimum spread threshold
        """
        self.ws = ws_client
        self.min_spread_cents = min_spread_cents

        # Use a single tracker that manages ALL markets
        # This ensures one callback handles all token mappings
        self._tracker = OrderBookTracker(ws_client, min_spread_cents)

        # Legacy compatibility: expose trackers dict for existing code
        self._trackers: Dict[str, OrderBookTracker] = {}

        # Track if we've registered the callback
        self._callback_registered = False

    async def add_market(self, market: Market15Min) -> None:
        """Add a market to track.

        Args:
            market: Market to track
        """
        if market.condition_id in self._trackers:
            return

        # Use the shared tracker for all markets
        await self._tracker.track_market(market)

        # Store reference for compatibility
        self._trackers[market.condition_id] = self._tracker

    async def remove_market(self, market: Market15Min) -> None:
        """Remove a market from tracking.

        Args:
            market: Market to remove
        """
        if market.condition_id not in self._trackers:
            return

        await self._tracker.untrack_market(market)
        del self._trackers[market.condition_id]

    def get_best_opportunity(self) -> Optional[ArbitrageOpportunity]:
        """Get the best current opportunity across all markets.

        Returns:
            Best opportunity or None
        """
        # Use the shared tracker to get all opportunities
        all_opportunities = self._tracker.get_all_opportunities()

        if not all_opportunities:
            return None

        return max(all_opportunities, key=lambda o: o.spread)

    def get_market_state(self, condition_id: str):
        """Get market state for a specific condition ID.

        Args:
            condition_id: Market condition ID

        Returns:
            MarketState or None
        """
        return self._tracker.get_market_state(condition_id)
