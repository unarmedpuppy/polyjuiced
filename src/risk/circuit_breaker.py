"""Circuit breaker for trading safety controls."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum
from typing import List, Optional

import structlog

from ..config import GabagoolConfig
from ..metrics import (
    CIRCUIT_BREAKER_LEVEL,
    CIRCUIT_BREAKER_TRIPS,
    POSITION_SIZE_MULTIPLIER,
    UNHEDGED_EXPOSURE_USD,
)

log = structlog.get_logger()


class CircuitBreakerLevel(IntEnum):
    """Circuit breaker severity levels."""

    NORMAL = 0  # Normal operation
    WARNING = 1  # Reduce position sizes by 50%
    CAUTION = 2  # Only close existing positions
    HALT = 3  # No trading, full system pause


@dataclass
class CircuitBreakerState:
    """Current state of the circuit breaker."""

    level: CircuitBreakerLevel = CircuitBreakerLevel.NORMAL
    reasons: List[str] = field(default_factory=list)
    tripped_at: Optional[datetime] = None
    cooldown_until: Optional[datetime] = None

    @property
    def is_tripped(self) -> bool:
        """Check if circuit breaker is tripped."""
        return self.level >= CircuitBreakerLevel.CAUTION

    @property
    def can_trade(self) -> bool:
        """Check if trading is allowed."""
        # Check cooldown
        if self.cooldown_until and datetime.utcnow() < self.cooldown_until:
            return False
        return self.level < CircuitBreakerLevel.HALT

    @property
    def size_multiplier(self) -> float:
        """Get position size multiplier based on level."""
        if self.level == CircuitBreakerLevel.WARNING:
            return 0.5
        elif self.level >= CircuitBreakerLevel.CAUTION:
            return 0.0
        return 1.0


class CircuitBreaker:
    """Multi-level circuit breaker for trading safety.

    Monitors various risk conditions and trips at different levels:
    - NORMAL: All systems go
    - WARNING: Reduce position sizes
    - CAUTION: Only close positions
    - HALT: Stop all trading
    """

    def __init__(self, config: GabagoolConfig):
        """Initialize circuit breaker.

        Args:
            config: Gabagool strategy configuration
        """
        self.config = config
        self._state = CircuitBreakerState()

        # Tracking counters
        self._consecutive_failures = 0
        self._daily_loss = 0.0
        self._daily_exposure = 0.0
        self._unhedged_exposure = 0.0

        # Thresholds
        self._max_consecutive_failures = 3
        self._cooldown_duration = timedelta(minutes=5)

    @property
    def state(self) -> CircuitBreakerState:
        """Get current circuit breaker state."""
        return self._state

    @property
    def can_trade(self) -> bool:
        """Check if trading is currently allowed."""
        return self._state.can_trade

    @property
    def size_multiplier(self) -> float:
        """Get current position size multiplier."""
        return self._state.size_multiplier

    def check_pre_trade(
        self,
        yes_price: float,
        no_price: float,
        trade_amount: float,
        time_remaining_seconds: float,
    ) -> tuple:
        """Perform pre-trade validation.

        Args:
            yes_price: Current YES price
            no_price: Current NO price
            trade_amount: Proposed trade amount in USD
            time_remaining_seconds: Seconds until market resolution

        Returns:
            Tuple of (can_proceed, reason)
        """
        reasons = []

        # Check if circuit breaker is tripped
        if not self.can_trade:
            return (False, f"Circuit breaker at level {self._state.level.name}")

        # Validate prices sum to less than $1.00
        if yes_price + no_price >= 1.0:
            reasons.append("Prices sum >= $1.00 (no profit)")
            self._trip(CircuitBreakerLevel.CAUTION, reasons)
            return (False, reasons[0])

        # Check minimum spread
        spread = 1.0 - (yes_price + no_price)
        if spread < self.config.min_spread_threshold:
            return (False, f"Spread {spread:.3f} below threshold")

        # Check time remaining
        if time_remaining_seconds < 60:
            return (False, "Less than 60 seconds until resolution")

        # Check daily exposure limit
        if self._daily_exposure + trade_amount > self.config.max_daily_exposure_usd:
            return (False, "Would exceed daily exposure limit")

        # Check if daily loss limit reached
        if self._daily_loss <= -self.config.max_daily_loss_usd:
            reasons.append("Daily loss limit reached")
            self._trip(CircuitBreakerLevel.HALT, reasons)
            return (False, reasons[0])

        # Check unhedged exposure
        if self._unhedged_exposure > self.config.max_unhedged_exposure_usd:
            reasons.append("Unhedged exposure too high")
            self._trip(CircuitBreakerLevel.CAUTION, reasons)
            return (False, reasons[0])

        return (True, "OK")

    def check_post_trade(
        self,
        success: bool,
        yes_filled: float,
        no_filled: float,
        yes_cost: float,
        no_cost: float,
    ) -> None:
        """Perform post-trade validation and update state.

        Args:
            success: Whether trade executed successfully
            yes_filled: YES shares filled
            no_filled: NO shares filled
            yes_cost: Total cost for YES
            no_cost: Total cost for NO
        """
        if not success:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._max_consecutive_failures:
                self._trip(
                    CircuitBreakerLevel.WARNING,
                    [f"Consecutive failures: {self._consecutive_failures}"],
                )
            return

        # Reset failure counter on success
        self._consecutive_failures = 0

        # Update exposure tracking
        total_cost = yes_cost + no_cost
        self._daily_exposure += total_cost

        # Calculate unhedged exposure
        min_shares = min(yes_filled, no_filled)
        hedged_value = min_shares * 1.0  # Each hedged pair is worth $1
        self._unhedged_exposure = abs(total_cost - hedged_value)

        # Update metric
        UNHEDGED_EXPOSURE_USD.set(self._unhedged_exposure)

        # Check if this created concerning unhedged exposure
        if self._unhedged_exposure > self.config.max_unhedged_exposure_usd:
            log.warning(
                "High unhedged exposure after trade",
                unhedged=f"${self._unhedged_exposure:.2f}",
            )

    def record_pnl(self, pnl: float) -> None:
        """Record realized P&L.

        Args:
            pnl: Profit or loss amount
        """
        self._daily_loss += pnl

        if self._daily_loss <= -self.config.max_daily_loss_usd:
            self._trip(
                CircuitBreakerLevel.HALT,
                [f"Daily loss limit: ${abs(self._daily_loss):.2f}"],
            )

    def record_slippage(self, expected_price: float, actual_price: float) -> None:
        """Record slippage on an order.

        Args:
            expected_price: Expected execution price
            actual_price: Actual execution price
        """
        slippage_cents = abs(actual_price - expected_price) * 100

        if slippage_cents > self.config.max_slippage_cents:
            log.warning(
                "High slippage detected",
                slippage_cents=f"{slippage_cents:.1f}¢",
                expected=f"${expected_price:.3f}",
                actual=f"${actual_price:.3f}",
            )
            # Don't trip, but log for monitoring

    def _trip(self, level: CircuitBreakerLevel, reasons: List[str]) -> None:
        """Trip the circuit breaker.

        Args:
            level: Level to trip to
            reasons: Reasons for tripping
        """
        if level <= self._state.level:
            return  # Already at this level or higher

        self._state.level = level
        self._state.reasons = reasons
        self._state.tripped_at = datetime.utcnow()
        self._state.cooldown_until = datetime.utcnow() + self._cooldown_duration

        # Update metrics
        CIRCUIT_BREAKER_LEVEL.set(int(level))
        CIRCUIT_BREAKER_TRIPS.labels(level=level.name).inc()
        POSITION_SIZE_MULTIPLIER.set(self._state.size_multiplier)

        log.warning(
            "Circuit breaker tripped",
            level=level.name,
            reasons=reasons,
        )

    def reset(self) -> None:
        """Reset circuit breaker to normal state."""
        if self._state.cooldown_until and datetime.utcnow() < self._state.cooldown_until:
            log.info("Cannot reset during cooldown")
            return

        self._state = CircuitBreakerState()
        self._consecutive_failures = 0

        # Update metrics
        CIRCUIT_BREAKER_LEVEL.set(0)
        POSITION_SIZE_MULTIPLIER.set(1.0)

        log.info("Circuit breaker reset")

    def reset_daily(self) -> None:
        """Reset daily counters (call at start of new day)."""
        self._daily_loss = 0.0
        self._daily_exposure = 0.0

        # If we were halted due to daily loss, reset
        if self._state.level == CircuitBreakerLevel.HALT:
            self.reset()

        log.info("Daily counters reset")
