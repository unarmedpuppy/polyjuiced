"""Position sizing calculations for Gabagool strategy."""

from dataclasses import dataclass
from typing import Optional

import structlog

from ..config import GabagoolConfig

log = structlog.get_logger()


@dataclass
class PositionSize:
    """Calculated position sizes for a trade."""

    yes_amount_usd: float
    no_amount_usd: float
    yes_shares: float
    no_shares: float
    total_cost: float
    expected_profit: float
    profit_percentage: float
    size_multiplier: float = 1.0

    @property
    def is_valid(self) -> bool:
        """Check if position size is valid (non-zero)."""
        return self.total_cost > 0 and self.expected_profit > 0


class PositionSizer:
    """Calculates optimal position sizes for arbitrage trades."""

    def __init__(self, config: GabagoolConfig):
        """Initialize position sizer.

        Args:
            config: Gabagool strategy configuration
        """
        self.config = config

    def calculate(
        self,
        yes_price: float,
        no_price: float,
        available_budget: Optional[float] = None,
        size_multiplier: float = 1.0,
    ) -> PositionSize:
        """Calculate optimal position sizes.

        Uses inverse weighting: buy more of the cheaper side.

        Args:
            yes_price: Current YES price (0-1)
            no_price: Current NO price (0-1)
            available_budget: Available budget (default: max_trade_size_usd)
            size_multiplier: Multiplier for position size (e.g., 0.5 for reduced)

        Returns:
            PositionSize with calculated amounts
        """
        # Default to max trade size
        if available_budget is None:
            available_budget = self.config.max_trade_size_usd

        # Apply size multiplier (from circuit breaker)
        budget = available_budget * size_multiplier

        # Validate prices
        if yes_price <= 0 or no_price <= 0:
            return self._zero_position()

        total_price = yes_price + no_price
        spread = 1.0 - total_price

        # Check minimum spread
        if spread < self.config.min_spread_threshold:
            return self._zero_position()

        # Calculate inverse weights
        # If YES is $0.40 and NO is $0.55, total = $0.95
        # YES weight = 0.55/0.95 = 0.58 (buy more YES because it's cheaper)
        # NO weight = 0.40/0.95 = 0.42
        yes_weight = no_price / total_price
        no_weight = yes_price / total_price

        # Calculate USD amounts
        yes_amount = budget * yes_weight
        no_amount = budget * no_weight

        # Cap at max trade size
        yes_amount = min(yes_amount, self.config.max_trade_size_usd)
        no_amount = min(no_amount, self.config.max_trade_size_usd)

        # Calculate shares
        yes_shares = yes_amount / yes_price
        no_shares = no_amount / no_price

        # Calculate expected profit
        # At resolution, we get $1 for each hedged pair
        min_shares = min(yes_shares, no_shares)
        total_cost = yes_amount + no_amount
        expected_profit = min_shares - total_cost

        # Profit percentage
        profit_pct = (expected_profit / total_cost * 100) if total_cost > 0 else 0

        return PositionSize(
            yes_amount_usd=yes_amount,
            no_amount_usd=no_amount,
            yes_shares=yes_shares,
            no_shares=no_shares,
            total_cost=total_cost,
            expected_profit=expected_profit,
            profit_percentage=profit_pct,
            size_multiplier=size_multiplier,
        )

    def calculate_spread_scaled(
        self,
        yes_price: float,
        no_price: float,
        available_budget: Optional[float] = None,
    ) -> PositionSize:
        """Calculate position sizes scaled by spread quality.

        Larger spreads = larger positions (up to max).
        Smaller spreads = smaller positions.

        Args:
            yes_price: Current YES price
            no_price: Current NO price
            available_budget: Available budget

        Returns:
            PositionSize with spread-scaled amounts
        """
        spread = 1.0 - (yes_price + no_price)

        # Define spread-to-size mapping
        # 5 cent spread or higher = full size
        # 2 cent spread = minimum size (threshold)
        # Scale linearly between
        min_spread = self.config.min_spread_threshold
        full_spread = 0.05  # 5 cents = full position

        if spread < min_spread:
            return self._zero_position()

        # Calculate scale factor (0.0 to 1.0)
        if spread >= full_spread:
            scale = 1.0
        else:
            scale = (spread - min_spread) / (full_spread - min_spread)

        return self.calculate(
            yes_price=yes_price,
            no_price=no_price,
            available_budget=available_budget,
            size_multiplier=scale,
        )

    def calculate_kelly(
        self,
        yes_price: float,
        no_price: float,
        win_probability: float = 0.95,
        kelly_fraction: float = 0.25,
        bankroll: Optional[float] = None,
    ) -> PositionSize:
        """Calculate position size using Kelly criterion.

        For binary arbitrage, this is a modified Kelly formula since
        the win probability is very high (if properly hedged).

        Args:
            yes_price: Current YES price
            no_price: Current NO price
            win_probability: Historical win rate (default 95%)
            kelly_fraction: Fraction of Kelly to use (default 0.25 = quarter Kelly)
            bankroll: Total bankroll for Kelly calculation

        Returns:
            PositionSize with Kelly-optimal amounts
        """
        if bankroll is None:
            bankroll = self.config.max_daily_exposure_usd

        spread = 1.0 - (yes_price + no_price)
        if spread <= 0:
            return self._zero_position()

        # Kelly formula for binary outcome
        # f* = (bp - q) / b
        # where b = profit odds, p = win prob, q = 1 - p
        b = spread / (1 - spread)  # Odds ratio
        p = win_probability
        q = 1 - p

        kelly = (b * p - q) / b if b > 0 else 0
        kelly = max(0, kelly)

        # Apply fractional Kelly for safety
        safe_kelly = kelly * kelly_fraction

        # Calculate position size
        position_budget = bankroll * safe_kelly

        # Cap at max trade size
        position_budget = min(position_budget, self.config.max_trade_size_usd)

        return self.calculate(
            yes_price=yes_price,
            no_price=no_price,
            available_budget=position_budget,
        )

    def _zero_position(self) -> PositionSize:
        """Return a zero position."""
        return PositionSize(
            yes_amount_usd=0.0,
            no_amount_usd=0.0,
            yes_shares=0.0,
            no_shares=0.0,
            total_cost=0.0,
            expected_profit=0.0,
            profit_percentage=0.0,
            size_multiplier=0.0,
        )

    def validate_position(
        self,
        position: PositionSize,
        current_daily_exposure: float = 0.0,
    ) -> tuple:
        """Validate a calculated position against limits.

        Args:
            position: Position to validate
            current_daily_exposure: Current daily exposure in USD

        Returns:
            Tuple of (is_valid, reason)
        """
        if not position.is_valid:
            return (False, "Position has zero value")

        # Check max trade size
        if position.yes_amount_usd > self.config.max_trade_size_usd:
            return (False, "YES amount exceeds max trade size")

        if position.no_amount_usd > self.config.max_trade_size_usd:
            return (False, "NO amount exceeds max trade size")

        # Check per-window limit
        if position.total_cost > self.config.max_per_window_usd:
            return (False, "Total cost exceeds per-window limit")

        # Check daily exposure (0 = unlimited)
        if self.config.max_daily_exposure_usd > 0:
            projected_exposure = current_daily_exposure + position.total_cost
            if projected_exposure > self.config.max_daily_exposure_usd:
                return (False, "Would exceed daily exposure limit")

        # Check minimum profit
        if position.expected_profit < 0:
            return (False, "Expected profit is negative")

        return (True, "OK")
