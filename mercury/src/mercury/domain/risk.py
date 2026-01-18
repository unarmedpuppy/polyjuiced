"""
Risk management domain models.

These models represent risk limits, circuit breaker states, and exposure tracking.
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional


class CircuitBreakerState(str, Enum):
    """Circuit breaker state indicating trading status.

    States progress from NORMAL -> WARNING -> CAUTION -> HALT
    based on consecutive failures or daily loss limits.
    """
    NORMAL = "NORMAL"    # Trading normally, all systems go
    WARNING = "WARNING"  # Near limits, extra caution advised
    CAUTION = "CAUTION"  # Very close to limits, reduce activity
    HALT = "HALT"        # Trading halted, circuit breaker triggered


# Keep CircuitBreakerLevel as an alias for backwards compatibility
CircuitBreakerLevel = CircuitBreakerState


@dataclass
class CircuitBreakerInfo:
    """Detailed circuit breaker information including timing."""
    state: CircuitBreakerState = CircuitBreakerState.NORMAL
    reason: str = ""
    triggered_at: Optional[datetime] = None
    cooldown_until: Optional[datetime] = None

    @property
    def is_trading_allowed(self) -> bool:
        """Check if trading is allowed in current state."""
        if self.state == CircuitBreakerState.HALT:
            if self.cooldown_until and datetime.utcnow() < self.cooldown_until:
                return False
            # If cooldown expired, allow trading
            return self.cooldown_until is not None and datetime.utcnow() >= self.cooldown_until
        return self.state != CircuitBreakerState.HALT

    @property
    def is_in_cooldown(self) -> bool:
        """Check if currently in cooldown period."""
        if self.cooldown_until is None:
            return False
        return datetime.utcnow() < self.cooldown_until

    def remaining_cooldown_seconds(self) -> float:
        """Get remaining cooldown time in seconds."""
        if self.cooldown_until is None:
            return 0.0
        remaining = (self.cooldown_until - datetime.utcnow()).total_seconds()
        return max(0.0, remaining)


@dataclass
class RiskLimits:
    """Risk limits configuration."""
    max_daily_loss_usd: Decimal = Decimal("100.0")
    max_daily_exposure_usd: Decimal = Decimal("500.0")
    max_position_size_usd: Decimal = Decimal("50.0")
    max_single_trade_usd: Decimal = Decimal("25.0")
    max_unhedged_exposure_usd: Decimal = Decimal("100.0")
    max_concurrent_positions: int = 20
    circuit_breaker_cooldown_minutes: int = 5

    # Warning thresholds (percentage of limits)
    warning_threshold: Decimal = Decimal("0.7")   # 70% of limit
    critical_threshold: Decimal = Decimal("0.9")  # 90% of limit

    def get_warning_threshold(self, limit: Decimal) -> Decimal:
        """Calculate warning threshold for a given limit."""
        return limit * self.warning_threshold

    def get_critical_threshold(self, limit: Decimal) -> Decimal:
        """Calculate critical threshold for a given limit."""
        return limit * self.critical_threshold


@dataclass
class ExposureSnapshot:
    """Snapshot of current risk exposure."""
    timestamp: datetime = field(default_factory=datetime.utcnow)
    daily_pnl: Decimal = Decimal("0")
    daily_exposure: Decimal = Decimal("0")
    total_position_value: Decimal = Decimal("0")
    unhedged_exposure: Decimal = Decimal("0")
    open_positions_count: int = 0
    pending_orders_count: int = 0
    trades_today: int = 0

    def check_against_limits(self, limits: RiskLimits) -> CircuitBreakerLevel:
        """Check exposure against limits and return circuit breaker level."""
        # Check daily loss
        if self.daily_pnl < -limits.max_daily_loss_usd:
            return CircuitBreakerLevel.TRIGGERED

        # Check if at critical levels
        loss_ratio = abs(self.daily_pnl) / limits.max_daily_loss_usd if limits.max_daily_loss_usd else Decimal("0")
        exposure_ratio = self.daily_exposure / limits.max_daily_exposure_usd if limits.max_daily_exposure_usd else Decimal("0")

        if loss_ratio >= limits.critical_threshold or exposure_ratio >= limits.critical_threshold:
            return CircuitBreakerLevel.CRITICAL

        if loss_ratio >= limits.warning_threshold or exposure_ratio >= limits.warning_threshold:
            return CircuitBreakerLevel.WARNING

        return CircuitBreakerLevel.NORMAL


@dataclass
class RiskCheckResult:
    """Result of a pre-trade risk check."""
    allowed: bool
    reason: str = ""
    adjusted_size: Optional[Decimal] = None
    circuit_breaker_level: CircuitBreakerLevel = CircuitBreakerLevel.NORMAL

    @classmethod
    def approve(cls, size: Optional[Decimal] = None) -> "RiskCheckResult":
        """Create an approved result."""
        return cls(allowed=True, adjusted_size=size)

    @classmethod
    def reject(cls, reason: str, level: CircuitBreakerLevel = CircuitBreakerLevel.NORMAL) -> "RiskCheckResult":
        """Create a rejected result."""
        return cls(allowed=False, reason=reason, circuit_breaker_level=level)
