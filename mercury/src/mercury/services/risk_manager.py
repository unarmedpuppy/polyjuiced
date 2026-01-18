"""Risk Manager - pre-trade validation and risk controls.

This service:
- Validates trading signals against risk limits
- Tracks exposure and daily P&L
- Manages circuit breaker state based on failures and losses
- Approves or rejects signals via event bus
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Tuple

import structlog

from mercury.core.config import ConfigManager
from mercury.core.events import EventBus
from mercury.core.lifecycle import BaseComponent, HealthCheckResult, HealthStatus
from mercury.domain.order import Fill
from mercury.domain.risk import CircuitBreakerState, RiskLimits
from mercury.domain.signal import ApprovedSignal, RejectedSignal, SignalType, TradingSignal

log = structlog.get_logger()


class RiskManager(BaseComponent):
    """Pre-trade risk validation and controls.

    This service:
    1. Receives trading signals from strategies
    2. Validates against configured risk limits
    3. Tracks exposure and daily P&L
    4. Approves or rejects signals
    5. Manages circuit breaker state based on consecutive failures and daily loss

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
            max_daily_loss_usd=self._get_decimal("risk.max_daily_loss_usd", Decimal("100")),
            max_position_size_usd=self._get_decimal("risk.max_position_size_usd", Decimal("25")),
            max_unhedged_exposure_usd=self._get_decimal("risk.max_unhedged_exposure_usd", Decimal("50")),
        )

        # Circuit breaker thresholds
        self._warning_failures = self._get_int("risk.circuit_breaker_warning_failures", 3)
        self._halt_failures = self._get_int("risk.circuit_breaker_halt_failures", 5)
        self._warning_loss = self._get_decimal("risk.circuit_breaker_warning_loss", Decimal("50"))
        self._halt_loss = self._get_decimal("risk.circuit_breaker_halt_loss", Decimal("100"))
        self._cooldown_minutes = self._get_int("risk.circuit_breaker_cooldown_minutes", 5)

        # State tracking
        self._daily_pnl: Decimal = Decimal("0")
        self._daily_volume: Decimal = Decimal("0")
        self._daily_trades: int = 0
        self._current_exposure: Decimal = Decimal("0")
        self._unhedged_exposure: Decimal = Decimal("0")
        self._consecutive_failures: int = 0
        self._circuit_breaker_state: CircuitBreakerState = CircuitBreakerState.NORMAL
        self._circuit_breaker_triggered_at: Optional[datetime] = None
        self._last_reset: datetime = datetime.now(timezone.utc)

    def _get_decimal(self, key: str, default: Decimal) -> Decimal:
        """Get a decimal config value."""
        value = self._config.get(key)
        if value is None:
            return default
        return Decimal(str(value))

    def _get_int(self, key: str, default: int) -> int:
        """Get an int config value."""
        value = self._config.get(key)
        if value is None:
            return default
        return int(value)

    async def _do_start(self) -> None:
        """Start the risk manager."""
        self._log.info(
            "starting_risk_manager",
            max_daily_loss=str(self._limits.max_daily_loss_usd),
            max_position_size=str(self._limits.max_position_size_usd),
        )

        # Subscribe to events
        await self._event_bus.subscribe("signal.*", self._on_signal)
        await self._event_bus.subscribe("order.filled", self._on_order_filled)
        await self._event_bus.subscribe("position.closed", self._on_position_closed)

        self._log.info("risk_manager_started")

    async def _do_stop(self) -> None:
        """Stop the risk manager."""
        self._log.info("risk_manager_stopped")

    async def _do_health_check(self) -> HealthCheckResult:
        """Check risk manager health."""
        if self._circuit_breaker_state == CircuitBreakerState.HALT:
            return HealthCheckResult(
                status=HealthStatus.DEGRADED,
                message="Circuit breaker triggered",
                details={
                    "state": self._circuit_breaker_state.value,
                    "triggered_at": (
                        self._circuit_breaker_triggered_at.isoformat()
                        if self._circuit_breaker_triggered_at
                        else None
                    ),
                },
            )

        return HealthCheckResult(
            status=HealthStatus.HEALTHY,
            message="Risk checks active",
            details={
                "daily_pnl": str(self._daily_pnl),
                "daily_trades": self._daily_trades,
                "circuit_breaker_state": self._circuit_breaker_state.value,
                "current_exposure": str(self._current_exposure),
            },
        )

    async def check_pre_trade(self, signal: TradingSignal) -> Tuple[bool, Optional[str]]:
        """Validate a signal against risk limits.

        Args:
            signal: Trading signal to validate.

        Returns:
            Tuple of (allowed, reason). Reason is None if allowed.
        """
        # Check circuit breaker
        if self._circuit_breaker_state == CircuitBreakerState.HALT:
            if not self._is_cooldown_expired():
                return False, "Circuit breaker triggered"

        # Check daily loss limit
        if self._daily_pnl <= -self._limits.max_daily_loss_usd:
            return False, f"Daily loss limit reached: ${-self._daily_pnl:.2f}"

        # Check position size
        if signal.target_size_usd > self._limits.max_position_size_usd:
            return False, f"Position size ${signal.target_size_usd:.2f} exceeds limit ${self._limits.max_position_size_usd:.2f}"

        # Check unhedged exposure for non-arbitrage
        if signal.signal_type != SignalType.ARBITRAGE:
            new_exposure = self._unhedged_exposure + signal.target_size_usd
            if new_exposure > self._limits.max_unhedged_exposure_usd:
                return False, "Unhedged exposure would exceed limit"

        return True, None

    async def validate_signal(self, signal: TradingSignal) -> Optional[ApprovedSignal]:
        """Validate and potentially approve a trading signal.

        Args:
            signal: Signal to validate.

        Returns:
            ApprovedSignal if approved, None if rejected.
        """
        allowed, reason = await self.check_pre_trade(signal)

        if not allowed:
            self._log.info(
                "signal_rejected",
                signal_id=signal.signal_id,
                reason=reason,
            )

            rejected = RejectedSignal(
                signal=signal,
                rejection_reason=reason or "Unknown reason",
            )

            await self._event_bus.publish(
                f"risk.rejected.{signal.signal_id}",
                {
                    "signal_id": signal.signal_id,
                    "reason": reason,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )

            return None

        # Create approved signal
        approved = ApprovedSignal(
            signal=signal,
            approved_size_usd=signal.target_size_usd,
        )

        self._log.info(
            "signal_approved",
            signal_id=signal.signal_id,
            approved_size=str(approved.approved_size_usd),
        )

        # Publish approved signal
        await self._event_bus.publish(
            f"risk.approved.{signal.signal_id}",
            {
                "signal_id": signal.signal_id,
                "market_id": signal.market_id,
                "signal_type": signal.signal_type.value,
                "approved_size_usd": str(approved.approved_size_usd),
                "yes_price": str(signal.yes_price),
                "no_price": str(signal.no_price),
                "timestamp": approved.approved_at.isoformat(),
            },
        )

        return approved

    def record_fill(self, fill: Fill) -> None:
        """Record a fill for exposure tracking.

        Args:
            fill: The fill to record.
        """
        self._daily_trades += 1
        self._daily_volume += fill.cost
        self._current_exposure += fill.cost

        self._log.debug(
            "fill_recorded",
            order_id=fill.order_id,
            market_id=fill.market_id,
            cost=str(fill.cost),
            current_exposure=str(self._current_exposure),
        )

    def record_pnl(self, pnl: Decimal) -> None:
        """Record realized P&L.

        Args:
            pnl: P&L amount (positive = profit, negative = loss).
        """
        self._daily_pnl += pnl

        self._log.info(
            "pnl_recorded",
            pnl=str(pnl),
            daily_pnl=str(self._daily_pnl),
        )

        # Update circuit breaker based on loss
        self._update_circuit_breaker_for_loss()

    def record_failure(self) -> None:
        """Record a trading failure for circuit breaker tracking.

        Consecutive failures trigger circuit breaker state changes.
        """
        self._consecutive_failures += 1

        old_state = self._circuit_breaker_state
        new_state = self._compute_circuit_breaker_state()

        if new_state != old_state:
            self._circuit_breaker_state = new_state
            if new_state == CircuitBreakerState.HALT:
                self._circuit_breaker_triggered_at = datetime.now(timezone.utc)

            self._log.warning(
                "circuit_breaker_changed",
                old_state=old_state.value,
                new_state=new_state.value,
                consecutive_failures=self._consecutive_failures,
                daily_pnl=str(self._daily_pnl),
            )

    def record_success(self) -> None:
        """Record a successful trade, resetting consecutive failure count."""
        self._consecutive_failures = 0

    def _compute_circuit_breaker_state(self) -> CircuitBreakerState:
        """Compute circuit breaker state based on failures and loss."""
        # Check failure-based thresholds
        if self._consecutive_failures >= self._halt_failures:
            return CircuitBreakerState.HALT
        elif self._consecutive_failures >= self._warning_failures:
            return CircuitBreakerState.WARNING

        # Check loss-based thresholds
        loss = -self._daily_pnl
        if loss >= self._halt_loss:
            return CircuitBreakerState.HALT
        elif loss >= self._warning_loss:
            return CircuitBreakerState.WARNING

        return CircuitBreakerState.NORMAL

    def _update_circuit_breaker_for_loss(self) -> None:
        """Update circuit breaker state based on daily P&L."""
        old_state = self._circuit_breaker_state
        new_state = self._compute_circuit_breaker_state()

        if new_state != old_state:
            self._circuit_breaker_state = new_state
            if new_state == CircuitBreakerState.HALT:
                self._circuit_breaker_triggered_at = datetime.now(timezone.utc)

            self._log.warning(
                "circuit_breaker_changed",
                old_state=old_state.value,
                new_state=new_state.value,
                daily_pnl=str(self._daily_pnl),
            )

    def _is_cooldown_expired(self) -> bool:
        """Check if circuit breaker cooldown has expired."""
        if self._circuit_breaker_triggered_at is None:
            return True

        from datetime import timedelta

        cooldown = timedelta(minutes=self._cooldown_minutes)
        return datetime.now(timezone.utc) >= self._circuit_breaker_triggered_at + cooldown

    def reset_daily(self) -> None:
        """Reset daily counters (called at midnight or on demand)."""
        self._log.info(
            "resetting_daily_limits",
            final_pnl=str(self._daily_pnl),
            final_trades=self._daily_trades,
            final_volume=str(self._daily_volume),
        )

        self._daily_pnl = Decimal("0")
        self._daily_volume = Decimal("0")
        self._daily_trades = 0
        self._current_exposure = Decimal("0")
        self._unhedged_exposure = Decimal("0")
        self._consecutive_failures = 0
        self._circuit_breaker_state = CircuitBreakerState.NORMAL
        self._circuit_breaker_triggered_at = None
        self._last_reset = datetime.now(timezone.utc)

    @property
    def circuit_breaker_state(self) -> CircuitBreakerState:
        """Get current circuit breaker state."""
        return self._circuit_breaker_state

    @property
    def current_exposure(self) -> Decimal:
        """Get current total exposure."""
        return self._current_exposure

    @property
    def daily_pnl(self) -> Decimal:
        """Get current daily P&L."""
        return self._daily_pnl

    @property
    def daily_trades(self) -> int:
        """Get number of trades today."""
        return self._daily_trades

    async def _on_signal(self, data: dict) -> None:
        """Handle incoming trading signal from event bus."""
        try:
            signal = TradingSignal(
                signal_id=data["signal_id"],
                strategy_name=data.get("strategy", ""),
                market_id=data["market_id"],
                signal_type=SignalType(data["signal_type"]),
                target_size_usd=Decimal(str(data["target_size_usd"])),
                yes_price=Decimal(str(data.get("yes_price", 0))),
                no_price=Decimal(str(data.get("no_price", 0))),
                confidence=data.get("confidence", 0.5),
                metadata=data.get("metadata", {}),
            )

            await self.validate_signal(signal)
        except Exception as e:
            self._log.error("signal_processing_error", error=str(e), data=data)

    async def _on_order_filled(self, data: dict) -> None:
        """Handle order filled event from event bus."""
        try:
            from mercury.domain.order import Fill, OrderSide
            import uuid

            fill = Fill(
                fill_id=data.get("fill_id", str(uuid.uuid4())),
                order_id=data.get("order_id", ""),
                market_id=data.get("market_id", ""),
                token_id=data.get("token_id", ""),
                side=OrderSide(data.get("side", "BUY")),
                outcome=data.get("outcome", "YES"),
                size=Decimal(str(data.get("size", 0))),
                price=Decimal(str(data.get("price", 0))),
                fee=Decimal(str(data.get("fee", 0))),
            )

            self.record_fill(fill)
        except Exception as e:
            self._log.error("fill_processing_error", error=str(e), data=data)

    async def _on_position_closed(self, data: dict) -> None:
        """Handle position closed event from event bus."""
        try:
            pnl = Decimal(str(data.get("realized_pnl", 0)))
            self.record_pnl(pnl)
        except Exception as e:
            self._log.error("position_closed_processing_error", error=str(e), data=data)
