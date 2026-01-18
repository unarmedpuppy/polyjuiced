"""Risk Manager - pre-trade validation and risk controls.

This service:
- Validates trading signals against risk limits
- Tracks exposure and daily P&L
- Manages circuit breaker state
- Approves or rejects signals
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Optional, Tuple

import structlog

from mercury.core.config import ConfigManager
from mercury.core.events import EventBus
from mercury.core.lifecycle import BaseComponent, HealthCheckResult, HealthStatus
from mercury.domain.risk import CircuitBreakerState, RiskLimits
from mercury.domain.signal import ApprovedSignal, RejectedSignal, SignalType, TradingSignal

log = structlog.get_logger()


@dataclass
class RiskState:
    """Current risk state tracking."""

    daily_pnl: Decimal = Decimal("0")
    daily_volume: Decimal = Decimal("0")
    daily_trades: int = 0

    unhedged_exposure: Decimal = Decimal("0")
    total_exposure: Decimal = Decimal("0")

    circuit_breaker_level: int = 0  # 0=NORMAL, 1=WARNING, 2=CRITICAL, 3=TRIGGERED
    circuit_breaker_until: Optional[datetime] = None

    last_reset: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class RiskManager(BaseComponent):
    """Pre-trade risk validation and controls.

    This service:
    1. Receives trading signals from strategies
    2. Validates against configured risk limits
    3. Tracks exposure and daily P&L
    4. Approves or rejects signals
    5. Manages circuit breaker state

    Event channels subscribed:
    - signal.* - Trading signals to validate
    - order.filled - Fill events for exposure tracking
    - position.closed - P&L events

    Event channels published:
    - risk.approved.{signal_id} - Approved signals
    - risk.rejected.{signal_id} - Rejected signals
    - risk.circuit_breaker - Circuit breaker state changes
    """

    def __init__(
        self,
        config: ConfigManager,
        event_bus: EventBus,
    ):
        """Initialize the risk manager.

        Args:
            config: Configuration manager.
            event_bus: EventBus for events.
        """
        super().__init__()
        self._config = config
        self._event_bus = event_bus
        self._log = log.bind(component="risk_manager")

        # Load limits from config
        self._limits = RiskLimits(
            max_daily_loss=config.get_decimal("risk.max_daily_loss_usd", Decimal("100")),
            max_position_size=config.get_decimal("risk.max_position_size_usd", Decimal("25")),
            max_unhedged_exposure=config.get_decimal("risk.max_unhedged_exposure_usd", Decimal("50")),
            max_daily_trades=config.get_int("risk.max_daily_trades", 100),
            circuit_breaker_cooldown_minutes=config.get_int("risk.circuit_breaker_cooldown_minutes", 5),
        )

        # State
        self._state = RiskState()
        self._position_exposure: Dict[str, Decimal] = {}  # market_id -> exposure

    async def start(self) -> None:
        """Start the risk manager."""
        self._start_time = time.time()
        self._log.info(
            "starting_risk_manager",
            max_daily_loss=str(self._limits.max_daily_loss),
            max_position_size=str(self._limits.max_position_size),
        )

        # Subscribe to events
        await self._event_bus.subscribe("signal.*", self._on_signal)
        await self._event_bus.subscribe("order.filled", self._on_order_filled)
        await self._event_bus.subscribe("position.closed", self._on_position_closed)

        self._log.info("risk_manager_started")

    async def stop(self) -> None:
        """Stop the risk manager."""
        self._log.info("risk_manager_stopped")

    async def health_check(self) -> HealthCheckResult:
        """Check risk manager health."""
        if self._state.circuit_breaker_level >= 3:
            return HealthCheckResult(
                status=HealthStatus.DEGRADED,
                message="Circuit breaker triggered",
                details={
                    "level": self._state.circuit_breaker_level,
                    "until": self._state.circuit_breaker_until.isoformat() if self._state.circuit_breaker_until else None,
                },
            )

        return HealthCheckResult(
            status=HealthStatus.HEALTHY,
            message="Risk checks active",
            details={
                "daily_pnl": str(self._state.daily_pnl),
                "daily_trades": self._state.daily_trades,
                "circuit_breaker_level": self._state.circuit_breaker_level,
            },
        )

    def check_pre_trade(self, signal: TradingSignal) -> Tuple[bool, str]:
        """Validate a signal against risk limits.

        Args:
            signal: Trading signal to validate.

        Returns:
            Tuple of (allowed, reason).
        """
        # Check circuit breaker
        if self._is_circuit_breaker_triggered():
            return False, "Circuit breaker triggered"

        # Check daily loss limit
        if self._state.daily_pnl <= -self._limits.max_daily_loss:
            return False, f"Daily loss limit reached: ${-self._state.daily_pnl:.2f}"

        # Check position size
        if signal.target_size_usd > self._limits.max_position_size:
            return False, f"Position size ${signal.target_size_usd:.2f} exceeds limit ${self._limits.max_position_size:.2f}"

        # Check unhedged exposure for non-arbitrage
        if signal.signal_type != SignalType.ARBITRAGE:
            new_exposure = self._state.unhedged_exposure + signal.target_size_usd
            if new_exposure > self._limits.max_unhedged_exposure:
                return False, f"Unhedged exposure would exceed limit"

        # Check daily trade limit
        if self._state.daily_trades >= self._limits.max_daily_trades:
            return False, f"Daily trade limit reached: {self._limits.max_daily_trades}"

        return True, "Approved"

    async def validate_signal(self, signal: TradingSignal) -> Optional[ApprovedSignal]:
        """Validate and potentially adjust a trading signal.

        Args:
            signal: Signal to validate.

        Returns:
            ApprovedSignal if approved, None if rejected.
        """
        allowed, reason = self.check_pre_trade(signal)

        if not allowed:
            self._log.info(
                "signal_rejected",
                signal_id=signal.signal_id,
                reason=reason,
            )

            await self._event_bus.publish(
                f"risk.rejected.{signal.signal_id}",
                {
                    "signal_id": signal.signal_id,
                    "reason": reason,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )

            return None

        # Create approved signal
        approved = ApprovedSignal(
            signal_id=f"approved-{signal.signal_id}",
            original_signal_id=signal.signal_id,
            market_id=signal.market_id,
            signal_type=signal.signal_type,
            target_size_usd=signal.target_size_usd,
            yes_price=signal.yes_price,
            no_price=signal.no_price,
            yes_token_id=signal.yes_token_id,
            no_token_id=signal.no_token_id,
            approved_at=datetime.now(timezone.utc),
        )

        self._log.info(
            "signal_approved",
            signal_id=approved.signal_id,
            original_id=signal.signal_id,
        )

        # Publish approved signal
        await self._event_bus.publish(
            f"risk.approved.{approved.signal_id}",
            {
                "signal_id": approved.signal_id,
                "original_signal_id": signal.signal_id,
                "market_id": approved.market_id,
                "signal_type": approved.signal_type.value,
                "target_size_usd": str(approved.target_size_usd),
                "yes_price": str(approved.yes_price),
                "no_price": str(approved.no_price),
                "yes_token_id": approved.yes_token_id,
                "no_token_id": approved.no_token_id,
                "timestamp": approved.approved_at.isoformat(),
            }
        )

        return approved

    def record_fill(self, market_id: str, size: Decimal, is_hedged: bool) -> None:
        """Record a fill for exposure tracking.

        Args:
            market_id: Market ID.
            size: Fill size in USD.
            is_hedged: Whether this is part of a hedged position.
        """
        self._state.daily_trades += 1
        self._state.daily_volume += size

        if not is_hedged:
            self._state.unhedged_exposure += size

        self._position_exposure[market_id] = self._position_exposure.get(market_id, Decimal("0")) + size

    def record_pnl(self, pnl: Decimal) -> None:
        """Record realized P&L.

        Args:
            pnl: P&L amount (positive = profit).
        """
        self._state.daily_pnl += pnl

        # Update circuit breaker
        self._update_circuit_breaker()

    def _update_circuit_breaker(self) -> None:
        """Update circuit breaker state based on P&L."""
        loss = -self._state.daily_pnl
        limit = self._limits.max_daily_loss

        old_level = self._state.circuit_breaker_level

        if loss >= limit:
            self._state.circuit_breaker_level = 3  # TRIGGERED
            self._state.circuit_breaker_until = datetime.now(timezone.utc)
        elif loss >= limit * Decimal("0.75"):
            self._state.circuit_breaker_level = 2  # CRITICAL
        elif loss >= limit * Decimal("0.50"):
            self._state.circuit_breaker_level = 1  # WARNING
        else:
            self._state.circuit_breaker_level = 0  # NORMAL

        # Publish if changed
        if self._state.circuit_breaker_level != old_level:
            self._log.warning(
                "circuit_breaker_changed",
                old_level=old_level,
                new_level=self._state.circuit_breaker_level,
                daily_pnl=str(self._state.daily_pnl),
            )

    def _is_circuit_breaker_triggered(self) -> bool:
        """Check if circuit breaker is triggered."""
        if self._state.circuit_breaker_level < 3:
            return False

        # Check cooldown
        if self._state.circuit_breaker_until:
            from datetime import timedelta
            cooldown = timedelta(minutes=self._limits.circuit_breaker_cooldown_minutes)
            if datetime.now(timezone.utc) < self._state.circuit_breaker_until + cooldown:
                return True

        # Reset after cooldown
        self._state.circuit_breaker_level = 0
        self._state.circuit_breaker_until = None
        return False

    def reset_daily(self) -> None:
        """Reset daily counters (called at midnight)."""
        self._log.info(
            "resetting_daily_limits",
            final_pnl=str(self._state.daily_pnl),
            final_trades=self._state.daily_trades,
        )

        self._state.daily_pnl = Decimal("0")
        self._state.daily_volume = Decimal("0")
        self._state.daily_trades = 0
        self._state.unhedged_exposure = Decimal("0")
        self._state.circuit_breaker_level = 0
        self._state.circuit_breaker_until = None
        self._state.last_reset = datetime.now(timezone.utc)

    @property
    def circuit_breaker_state(self) -> CircuitBreakerState:
        """Get current circuit breaker state."""
        return CircuitBreakerState(
            level=self._state.circuit_breaker_level,
            triggered_at=self._state.circuit_breaker_until,
            cooldown_minutes=self._limits.circuit_breaker_cooldown_minutes,
        )

    async def _on_signal(self, data: dict) -> None:
        """Handle incoming trading signal."""
        signal = TradingSignal(
            signal_id=data["signal_id"],
            strategy_name=data["strategy"],
            market_id=data["market_id"],
            signal_type=SignalType(data["signal_type"]),
            target_size_usd=Decimal(str(data["target_size_usd"])),
            yes_price=Decimal(str(data.get("yes_price", 0))),
            no_price=Decimal(str(data.get("no_price", 0))),
            yes_token_id=data.get("yes_token_id", ""),
            no_token_id=data.get("no_token_id", ""),
            confidence=data.get("confidence", 0.5),
            metadata=data.get("metadata", {}),
        )

        await self.validate_signal(signal)

    async def _on_order_filled(self, data: dict) -> None:
        """Handle order filled event."""
        market_id = data.get("market_id", "")
        total_cost = Decimal(str(data.get("total_cost", 0)))

        # Check if hedged (dual-leg)
        is_hedged = data.get("yes_filled") and data.get("no_filled")

        self.record_fill(market_id, total_cost, is_hedged)

    async def _on_position_closed(self, data: dict) -> None:
        """Handle position closed event."""
        pnl = Decimal(str(data.get("realized_pnl", 0)))
        self.record_pnl(pnl)
