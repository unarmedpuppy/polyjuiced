"""Gabagool arbitrage strategy implementation.

This module contains the core arbitrage detection logic ported from the legacy
gabagool.py. Only signal generation is implemented here - no dashboard,
metrics, or persistence coupling.

The strategy:
1. Monitors binary markets for YES + NO < $1.00
2. Detects arbitrage opportunities when spread exceeds threshold
3. Generates trading signals with calculated position sizes
4. Emits signals via the event-driven architecture
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import AsyncIterator, Optional

import structlog

from mercury.core.config import ConfigManager
from mercury.domain.market import OrderBook
from mercury.domain.signal import SignalPriority, SignalType, TradingSignal
from mercury.strategies.gabagool.config import GabagoolConfig

log = structlog.get_logger()


class GabagoolStrategy:
    """Gabagool asymmetric binary arbitrage strategy.

    Key principles:
    1. Never predict direction - always hedge both sides
    2. Only enter when spread > threshold (e.g., 1.5 cents)
    3. Buy equal shares of YES and NO
    4. Hold until market resolution (typically 15 minutes)

    This class implements the BaseStrategy protocol and can be registered
    with the StrategyEngine.
    """

    # Class-level defaults for protocol compliance (allows hasattr checks with __new__)
    _enabled: bool = True
    _running: bool = False
    _subscribed_markets: list = []
    _last_signal_time: dict = {}

    def __init__(
        self,
        config: ConfigManager,
        gabagool_config: Optional[GabagoolConfig] = None,
    ) -> None:
        """Initialize Gabagool strategy.

        Args:
            config: ConfigManager for general configuration.
            gabagool_config: Optional pre-built GabagoolConfig. If not provided,
                           will be loaded from ConfigManager.
        """
        self._config = config
        self._gabagool_config = gabagool_config or GabagoolConfig.from_config_manager(config)
        self._log = log.bind(strategy="gabagool")

        # Runtime state (instance-level, shadows class defaults)
        self._enabled = self._gabagool_config.enabled
        self._subscribed_markets: list[str] = []
        self._running = False

        # Cooldown tracking to avoid duplicate signals
        self._last_signal_time: dict[str, datetime] = {}
        self._signal_cooldown = timedelta(seconds=5)  # Min 5s between signals per market

    @property
    def name(self) -> str:
        """Strategy name for identification."""
        return "gabagool"

    @property
    def enabled(self) -> bool:
        """Whether the strategy is currently enabled."""
        return self._enabled

    async def start(self) -> None:
        """Initialize strategy resources."""
        self._running = True
        self._log.info(
            "gabagool_strategy_started",
            enabled=self._enabled,
            min_spread=f"{self._gabagool_config.min_spread_cents:.1f}¢",
            max_trade=f"${self._gabagool_config.max_trade_size_usd}",
            markets=self._gabagool_config.markets,
        )

    async def stop(self) -> None:
        """Cleanup strategy resources."""
        self._running = False
        self._log.info("gabagool_strategy_stopped")

    def enable(self) -> None:
        """Enable the strategy at runtime."""
        self._enabled = True
        self._log.info("gabagool_strategy_enabled")

    def disable(self) -> None:
        """Disable the strategy at runtime."""
        self._enabled = False
        self._log.info("gabagool_strategy_disabled")

    def get_subscribed_markets(self) -> list[str]:
        """Return list of market IDs this strategy wants data for.

        Returns:
            List of market condition IDs.
        """
        return self._subscribed_markets

    def subscribe_market(self, market_id: str) -> None:
        """Add a market to the subscription list.

        Args:
            market_id: Market condition ID to subscribe to.
        """
        if market_id not in self._subscribed_markets:
            self._subscribed_markets.append(market_id)
            self._log.info("market_subscribed", market_id=market_id)

    def unsubscribe_market(self, market_id: str) -> None:
        """Remove a market from the subscription list.

        Args:
            market_id: Market condition ID to unsubscribe from.
        """
        if market_id in self._subscribed_markets:
            self._subscribed_markets.remove(market_id)
            self._log.info("market_unsubscribed", market_id=market_id)

    async def on_market_data(
        self,
        market_id: str,
        book: OrderBook,
    ) -> AsyncIterator[TradingSignal]:
        """Process market data and yield trading signals.

        This is the core signal generation logic. It:
        1. Checks if arbitrage opportunity exists
        2. Validates the opportunity against entry criteria
        3. Calculates optimal position sizes
        4. Yields a TradingSignal if opportunity is valid

        Args:
            market_id: The market's condition ID.
            book: Current order book snapshot (YES + NO sides).

        Yields:
            TradingSignal for each trading opportunity detected.
        """
        if not self._enabled:
            return

        # Detect arbitrage opportunity
        opportunity = self._detect_arbitrage(book)
        if opportunity is None:
            return

        # Validate opportunity against entry criteria
        validation_result = self._validate_opportunity(opportunity, market_id)
        if not validation_result.is_valid:
            self._log.debug(
                "opportunity_rejected",
                market_id=market_id,
                reason=validation_result.reason,
                spread_cents=f"{opportunity.spread_cents:.1f}¢",
            )
            return

        # Check cooldown to avoid duplicate signals
        if self._is_on_cooldown(market_id):
            return

        # Calculate position sizes
        yes_amount, no_amount = self.calculate_position_sizes(
            budget=self._gabagool_config.max_trade_size_usd,
            yes_price=opportunity.yes_price,
            no_price=opportunity.no_price,
        )

        if yes_amount <= 0 or no_amount <= 0:
            self._log.debug(
                "position_size_zero",
                market_id=market_id,
                yes_price=str(opportunity.yes_price),
                no_price=str(opportunity.no_price),
            )
            return

        # Create and yield the trading signal
        signal = self._create_signal(
            market_id=market_id,
            opportunity=opportunity,
            yes_amount=yes_amount,
            no_amount=no_amount,
        )

        # Update cooldown
        self._last_signal_time[market_id] = datetime.now(timezone.utc)

        self._log.info(
            "arbitrage_signal_generated",
            market_id=market_id,
            signal_id=signal.signal_id,
            spread_cents=f"{opportunity.spread_cents:.1f}¢",
            yes_price=str(opportunity.yes_price),
            no_price=str(opportunity.no_price),
            target_size=str(signal.target_size_usd),
            expected_pnl=str(signal.expected_pnl),
        )

        yield signal

    def _detect_arbitrage(self, book: OrderBook) -> Optional["ArbitrageOpportunity"]:
        """Detect if an arbitrage opportunity exists.

        Args:
            book: Order book snapshot.

        Returns:
            ArbitrageOpportunity if detected, None otherwise.
        """
        # Need both YES and NO ask prices
        if book.yes_best_ask is None or book.no_best_ask is None:
            return None

        yes_price = book.yes_best_ask
        no_price = book.no_best_ask
        combined = yes_price + no_price

        # Arbitrage exists when combined < 1.0
        if combined >= Decimal("1"):
            return None

        spread = Decimal("1") - combined
        spread_cents = spread * Decimal("100")

        # Calculate profit percentage
        profit_pct = (spread / combined * Decimal("100")) if combined > 0 else Decimal("0")

        return ArbitrageOpportunity(
            market_id=book.market_id,
            yes_price=yes_price,
            no_price=no_price,
            combined_price=combined,
            spread=spread,
            spread_cents=spread_cents,
            profit_percentage=profit_pct,
            detected_at=datetime.now(timezone.utc),
        )

    def _validate_opportunity(
        self,
        opportunity: "ArbitrageOpportunity",
        market_id: str,
    ) -> "ValidationResult":
        """Validate an opportunity against entry criteria.

        Args:
            opportunity: The detected opportunity.
            market_id: Market identifier.

        Returns:
            ValidationResult with is_valid flag and reason.
        """
        min_spread = self._gabagool_config.min_spread_threshold

        # Check minimum spread threshold
        if opportunity.spread < min_spread:
            return ValidationResult(
                is_valid=False,
                reason=f"Spread {opportunity.spread_cents:.1f}¢ < {min_spread * 100:.1f}¢ threshold",
            )

        # Check that prices are valid (both > 0)
        if opportunity.yes_price <= 0 or opportunity.no_price <= 0:
            return ValidationResult(
                is_valid=False,
                reason="Invalid prices (zero or negative)",
            )

        # Check that sum < 1 (should be caught by detect, but double-check)
        if opportunity.combined_price >= Decimal("1"):
            return ValidationResult(
                is_valid=False,
                reason=f"No arbitrage (sum={opportunity.combined_price * 100:.1f}¢ >= 100¢)",
            )

        return ValidationResult(is_valid=True, reason="")

    def _is_on_cooldown(self, market_id: str) -> bool:
        """Check if a market is on cooldown to avoid duplicate signals.

        Args:
            market_id: Market identifier.

        Returns:
            True if still on cooldown.
        """
        last_time = self._last_signal_time.get(market_id)
        if last_time is None:
            return False

        elapsed = datetime.now(timezone.utc) - last_time
        return elapsed < self._signal_cooldown

    def calculate_position_sizes(
        self,
        budget: Decimal,
        yes_price: Decimal,
        no_price: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Calculate optimal position sizes for YES and NO.

        For arbitrage profit, we need EQUAL SHARES of YES and NO.
        At resolution, one side pays $1, one pays $0.
        Profit = num_shares * $1 - (num_shares * yes_price + num_shares * no_price)
               = num_shares * (1 - yes_price - no_price)
               = num_shares * spread

        Args:
            budget: Total USD budget for this trade.
            yes_price: Current YES ask price (0-1).
            no_price: Current NO ask price (0-1).

        Returns:
            Tuple of (yes_amount_usd, no_amount_usd).
        """
        # Check for invalid prices (zero or negative)
        if yes_price <= 0 or no_price <= 0:
            return (Decimal("0"), Decimal("0"))

        cost_per_pair = yes_price + no_price

        if cost_per_pair <= 0 or cost_per_pair >= Decimal("1"):
            return (Decimal("0"), Decimal("0"))

        # Calculate how many share pairs we can buy with our budget
        num_pairs = budget / cost_per_pair

        # Equal shares means different dollar amounts
        # Spend MORE on the expensive side to get equal shares
        yes_amount = num_pairs * yes_price
        no_amount = num_pairs * no_price

        # Ensure we don't exceed individual trade limits
        max_single = self._gabagool_config.max_trade_size_usd
        if yes_amount > max_single or no_amount > max_single:
            # Scale down proportionally
            scale = max_single / max(yes_amount, no_amount)
            yes_amount = yes_amount * scale
            no_amount = no_amount * scale

        return (yes_amount, no_amount)

    def calculate_expected_profit(
        self,
        yes_amount: Decimal,
        no_amount: Decimal,
        yes_price: Decimal,
        no_price: Decimal,
    ) -> Decimal:
        """Calculate expected profit from an arbitrage trade.

        Profit = guaranteed_payout - total_cost
        For equal shares of YES and NO, payout is always $1 per share pair.

        Args:
            yes_amount: USD to spend on YES.
            no_amount: USD to spend on NO.
            yes_price: YES price.
            no_price: NO price.

        Returns:
            Expected profit in USD.
        """
        if yes_price <= 0 or no_price <= 0:
            return Decimal("0")

        # Calculate shares we'll get
        yes_shares = yes_amount / yes_price
        no_shares = no_amount / no_price

        # Profit is the spread times minimum shares (hedged portion)
        min_shares = min(yes_shares, no_shares)
        total_cost = yes_amount + no_amount

        # At resolution, min_shares will pay out $1 each
        payout = min_shares * Decimal("1")

        return payout - total_cost

    def _create_signal(
        self,
        market_id: str,
        opportunity: "ArbitrageOpportunity",
        yes_amount: Decimal,
        no_amount: Decimal,
    ) -> TradingSignal:
        """Create a TradingSignal from an opportunity.

        Args:
            market_id: Market identifier.
            opportunity: The detected arbitrage opportunity.
            yes_amount: USD to spend on YES.
            no_amount: USD to spend on NO.

        Returns:
            TradingSignal ready for execution.
        """
        # Calculate expected profit
        expected_pnl = self.calculate_expected_profit(
            yes_amount=yes_amount,
            no_amount=no_amount,
            yes_price=opportunity.yes_price,
            no_price=opportunity.no_price,
        )

        # Determine confidence based on spread size
        # Higher spread = higher confidence
        confidence = self._calculate_confidence(opportunity.spread_cents)

        # Determine priority based on spread
        priority = self._determine_priority(opportunity.spread_cents)

        # Signal expires after 30 seconds (arbitrage is time-sensitive)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=30)

        return TradingSignal(
            strategy_name=self.name,
            market_id=market_id,
            signal_type=SignalType.ARBITRAGE,
            confidence=confidence,
            priority=priority,
            target_size_usd=yes_amount + no_amount,
            yes_price=opportunity.yes_price,
            no_price=opportunity.no_price,
            expected_pnl=expected_pnl,
            max_slippage=Decimal("0.01"),  # 1 cent slippage tolerance
            metadata={
                "spread_cents": float(opportunity.spread_cents),
                "profit_percentage": float(opportunity.profit_percentage),
                "yes_amount": str(yes_amount),
                "no_amount": str(no_amount),
            },
            expires_at=expires_at,
        )

    def _calculate_confidence(self, spread_cents: Decimal) -> float:
        """Calculate signal confidence based on spread size.

        Args:
            spread_cents: Spread in cents.

        Returns:
            Confidence value between 0.0 and 1.0.
        """
        # Base confidence starts at 0.5 for minimum spread
        # Scales up to 0.95 for spreads >= 5 cents
        min_spread = self._gabagool_config.min_spread_cents
        max_spread = Decimal("5.0")  # 5 cents = max confidence

        if spread_cents <= min_spread:
            return 0.5

        # Linear interpolation
        spread_range = max_spread - min_spread
        normalized = float((spread_cents - min_spread) / spread_range)
        confidence = 0.5 + (0.45 * min(normalized, 1.0))

        return min(confidence, 0.95)

    def _determine_priority(self, spread_cents: Decimal) -> SignalPriority:
        """Determine signal priority based on spread size.

        Args:
            spread_cents: Spread in cents.

        Returns:
            SignalPriority enum value.
        """
        if spread_cents >= Decimal("4.0"):
            return SignalPriority.CRITICAL
        elif spread_cents >= Decimal("3.0"):
            return SignalPriority.HIGH
        elif spread_cents >= Decimal("2.0"):
            return SignalPriority.MEDIUM
        else:
            return SignalPriority.LOW


class ArbitrageOpportunity:
    """Represents a detected arbitrage opportunity.

    This is a simple data class to hold opportunity details.
    Unlike the legacy version, it does not include market metadata
    or callbacks - just the core arbitrage data.
    """

    # Validity window for opportunities (seconds)
    VALIDITY_SECONDS: float = 30.0

    def __init__(
        self,
        market_id: str,
        yes_price: Decimal,
        no_price: Decimal,
        combined_price: Decimal,
        spread: Decimal,
        spread_cents: Decimal,
        profit_percentage: Decimal,
        detected_at: datetime,
    ) -> None:
        """Initialize an arbitrage opportunity.

        Args:
            market_id: Market condition ID.
            yes_price: YES best ask price.
            no_price: NO best ask price.
            combined_price: YES + NO price.
            spread: Arbitrage spread (1 - combined).
            spread_cents: Spread in cents.
            profit_percentage: Expected profit percentage.
            detected_at: When the opportunity was detected.
        """
        self.market_id = market_id
        self.yes_price = yes_price
        self.no_price = no_price
        self.combined_price = combined_price
        self.spread = spread
        self.spread_cents = spread_cents
        self.profit_percentage = profit_percentage
        self.detected_at = detected_at

    @property
    def is_valid(self) -> bool:
        """Check if opportunity is still valid (recent enough).

        Opportunities are valid for VALIDITY_SECONDS to account for:
        - API latency when placing orders
        - Async queue processing delays
        """
        age = (datetime.now(timezone.utc) - self.detected_at).total_seconds()
        return age < self.VALIDITY_SECONDS

    @property
    def age_seconds(self) -> float:
        """Get the age of this opportunity in seconds."""
        return (datetime.now(timezone.utc) - self.detected_at).total_seconds()


class ValidationResult:
    """Result of opportunity validation."""

    def __init__(self, is_valid: bool, reason: str = "") -> None:
        """Initialize validation result.

        Args:
            is_valid: Whether the opportunity passed validation.
            reason: Reason for rejection (if is_valid is False).
        """
        self.is_valid = is_valid
        self.reason = reason
