"""Risk Manager - pre-trade validation and risk controls.

This service:
- Validates trading signals against risk limits
- Tracks exposure and daily P&L
- Manages 4-level circuit breaker state based on failures and losses
- Publishes risk.circuit_breaker events on state changes
- Approves or rejects signals via event bus

Circuit Breaker Levels (ported from legacy/src/risk/circuit_breaker.py):
- NORMAL: All systems go, full position sizes
- WARNING: Near limits, reduce position sizes by 50%
- CAUTION: Only close existing positions, no new positions
- HALT: No trading at all, full system pause
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional, Tuple

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

        # Circuit breaker thresholds - 4 levels: NORMAL -> WARNING -> CAUTION -> HALT
        # Failure thresholds
        self._warning_failures = self._get_int("risk.circuit_breaker_warning_failures", 3)
        self._caution_failures = self._get_int("risk.circuit_breaker_caution_failures", 4)
        self._halt_failures = self._get_int("risk.circuit_breaker_halt_failures", 5)

        # Loss thresholds (in USD)
        self._warning_loss = self._get_decimal("risk.circuit_breaker_warning_loss", Decimal("50"))
        self._caution_loss = self._get_decimal("risk.circuit_breaker_caution_loss", Decimal("75"))
        self._halt_loss = self._get_decimal("risk.circuit_breaker_halt_loss", Decimal("100"))

        # Cooldown and timing
        self._cooldown_minutes = self._get_int("risk.circuit_breaker_cooldown_minutes", 5)
        self._cooldown_duration = timedelta(minutes=self._cooldown_minutes)

        # State tracking
        self._daily_pnl: Decimal = Decimal("0")
        self._daily_volume: Decimal = Decimal("0")
        self._daily_trades: int = 0
        self._current_exposure: Decimal = Decimal("0")
        self._unhedged_exposure: Decimal = Decimal("0")
        self._consecutive_failures: int = 0
        self._circuit_breaker_state: CircuitBreakerState = CircuitBreakerState.NORMAL
        self._circuit_breaker_triggered_at: Optional[datetime] = None
        self._circuit_breaker_reasons: List[str] = []
        self._cooldown_until: Optional[datetime] = None
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
        state = self._circuit_breaker_state

        # Check HALT level - no trading at all
        if state == CircuitBreakerState.HALT:
            if not self._is_cooldown_expired():
                return False, f"Circuit breaker triggered (HALT): {', '.join(self._circuit_breaker_reasons)}"

        # Check CAUTION level - only closing positions allowed
        if state == CircuitBreakerState.CAUTION:
            # CAUTION only allows closing existing positions, not opening new ones
            # For now, reject all signals at CAUTION level
            # TODO: Allow CLOSE signals when signal types support it
            return False, f"Circuit breaker at CAUTION: only position closes allowed. Reasons: {', '.join(self._circuit_breaker_reasons)}"

        # Check daily loss limit
        if self._daily_pnl <= -self._limits.max_daily_loss_usd:
            return False, f"Daily loss limit reached: ${-self._daily_pnl:.2f}"

        # Check position size (apply size multiplier for WARNING level)
        effective_max_size = self._limits.max_position_size_usd * Decimal(str(state.size_multiplier))
        if signal.target_size_usd > effective_max_size:
            if state == CircuitBreakerState.WARNING:
                return False, f"Position size ${signal.target_size_usd:.2f} exceeds WARNING-adjusted limit ${effective_max_size:.2f}"
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
        self._update_circuit_breaker_state()

    def record_success(self) -> None:
        """Record a successful trade, resetting consecutive failure count."""
        if self._consecutive_failures > 0:
            self._consecutive_failures = 0
            # Recompute state - may recover from WARNING to NORMAL if losses permit
            self._update_circuit_breaker_state()

    def _compute_circuit_breaker_state(self) -> Tuple[CircuitBreakerState, List[str]]:
        """Compute circuit breaker state based on failures and loss.

        Returns:
            Tuple of (state, reasons) where reasons explain why the state was set.
        """
        reasons: List[str] = []

        # Determine state from failures
        failure_state = CircuitBreakerState.NORMAL
        if self._consecutive_failures >= self._halt_failures:
            failure_state = CircuitBreakerState.HALT
            reasons.append(f"Consecutive failures: {self._consecutive_failures} >= {self._halt_failures}")
        elif self._consecutive_failures >= self._caution_failures:
            failure_state = CircuitBreakerState.CAUTION
            reasons.append(f"Consecutive failures: {self._consecutive_failures} >= {self._caution_failures}")
        elif self._consecutive_failures >= self._warning_failures:
            failure_state = CircuitBreakerState.WARNING
            reasons.append(f"Consecutive failures: {self._consecutive_failures} >= {self._warning_failures}")

        # Determine state from loss
        loss = -self._daily_pnl
        loss_state = CircuitBreakerState.NORMAL
        if loss >= self._halt_loss:
            loss_state = CircuitBreakerState.HALT
            reasons.append(f"Daily loss: ${loss:.2f} >= ${self._halt_loss}")
        elif loss >= self._caution_loss:
            loss_state = CircuitBreakerState.CAUTION
            reasons.append(f"Daily loss: ${loss:.2f} >= ${self._caution_loss}")
        elif loss >= self._warning_loss:
            loss_state = CircuitBreakerState.WARNING
            reasons.append(f"Daily loss: ${loss:.2f} >= ${self._warning_loss}")

        # Take the more severe state
        state_order = [
            CircuitBreakerState.NORMAL,
            CircuitBreakerState.WARNING,
            CircuitBreakerState.CAUTION,
            CircuitBreakerState.HALT,
        ]
        failure_idx = state_order.index(failure_state)
        loss_idx = state_order.index(loss_state)
        final_state = state_order[max(failure_idx, loss_idx)]

        return final_state, reasons

    def _update_circuit_breaker_state(self) -> None:
        """Update circuit breaker state and publish event if state changed."""
        old_state = self._circuit_breaker_state
        new_state, reasons = self._compute_circuit_breaker_state()

        if new_state != old_state:
            self._trip_circuit_breaker(new_state, reasons)

    def _update_circuit_breaker_for_loss(self) -> None:
        """Update circuit breaker state based on daily P&L.

        Alias for _update_circuit_breaker_state for backward compatibility.
        """
        self._update_circuit_breaker_state()

    def _trip_circuit_breaker(self, level: CircuitBreakerState, reasons: List[str]) -> None:
        """Trip the circuit breaker to a new level.

        Args:
            level: New circuit breaker level.
            reasons: List of reasons for tripping.
        """
        old_state = self._circuit_breaker_state

        # Only trip to higher (more severe) levels, never downgrade via trip
        # (recovery happens through reset_daily or manual reset)
        state_order = [
            CircuitBreakerState.NORMAL,
            CircuitBreakerState.WARNING,
            CircuitBreakerState.CAUTION,
            CircuitBreakerState.HALT,
        ]
        if state_order.index(level) <= state_order.index(old_state):
            return

        now = datetime.now(timezone.utc)
        self._circuit_breaker_state = level
        self._circuit_breaker_reasons = reasons
        self._circuit_breaker_triggered_at = now
        self._cooldown_until = now + self._cooldown_duration

        self._log.warning(
            "circuit_breaker_tripped",
            old_state=old_state.value,
            new_state=level.value,
            reasons=reasons,
            size_multiplier=level.size_multiplier,
            cooldown_until=self._cooldown_until.isoformat(),
        )

        # Publish event asynchronously (fire and forget)
        import asyncio

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._publish_circuit_breaker_event(old_state, level, reasons))
        except RuntimeError:
            # No event loop running (e.g., in sync tests) - skip publishing
            pass

    async def _publish_circuit_breaker_event(
        self,
        old_state: CircuitBreakerState,
        new_state: CircuitBreakerState,
        reasons: List[str],
    ) -> None:
        """Publish risk.circuit_breaker event when state changes.

        Args:
            old_state: Previous circuit breaker state.
            new_state: New circuit breaker state.
            reasons: List of reasons for the state change.
        """
        try:
            await self._event_bus.publish(
                "risk.circuit_breaker",
                {
                    "old_state": old_state.value,
                    "new_state": new_state.value,
                    "reasons": reasons,
                    "size_multiplier": new_state.size_multiplier,
                    "can_trade": new_state.can_trade,
                    "can_open_positions": new_state.can_open_positions,
                    "consecutive_failures": self._consecutive_failures,
                    "daily_pnl": str(self._daily_pnl),
                    "cooldown_until": (
                        self._cooldown_until.isoformat() if self._cooldown_until else None
                    ),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as e:
            self._log.error("failed_to_publish_circuit_breaker_event", error=str(e))

    def _is_cooldown_expired(self) -> bool:
        """Check if circuit breaker cooldown has expired."""
        if self._cooldown_until is None:
            return True
        return datetime.now(timezone.utc) >= self._cooldown_until

    def reset_daily(self) -> None:
        """Reset daily counters (called at midnight or on demand)."""
        self._log.info(
            "resetting_daily_limits",
            final_pnl=str(self._daily_pnl),
            final_trades=self._daily_trades,
            final_volume=str(self._daily_volume),
            final_circuit_breaker_state=self._circuit_breaker_state.value,
        )

        self._daily_pnl = Decimal("0")
        self._daily_volume = Decimal("0")
        self._daily_trades = 0
        self._current_exposure = Decimal("0")
        self._unhedged_exposure = Decimal("0")
        self._consecutive_failures = 0
        self._circuit_breaker_state = CircuitBreakerState.NORMAL
        self._circuit_breaker_triggered_at = None
        self._circuit_breaker_reasons = []
        self._cooldown_until = None
        self._last_reset = datetime.now(timezone.utc)

    @property
    def circuit_breaker_state(self) -> CircuitBreakerState:
        """Get current circuit breaker state."""
        return self._circuit_breaker_state

    @property
    def circuit_breaker_reasons(self) -> List[str]:
        """Get reasons for current circuit breaker state."""
        return self._circuit_breaker_reasons.copy()

    @property
    def size_multiplier(self) -> float:
        """Get current position size multiplier based on circuit breaker state."""
        return self._circuit_breaker_state.size_multiplier

    @property
    def can_trade(self) -> bool:
        """Check if trading is allowed in current state."""
        if self._circuit_breaker_state == CircuitBreakerState.HALT:
            return self._is_cooldown_expired()
        return self._circuit_breaker_state.can_trade

    @property
    def can_open_positions(self) -> bool:
        """Check if new positions can be opened in current state."""
        if self._circuit_breaker_state == CircuitBreakerState.HALT:
            return self._is_cooldown_expired()
        return self._circuit_breaker_state.can_open_positions

    @property
    def cooldown_until(self) -> Optional[datetime]:
        """Get cooldown expiration time, if in cooldown."""
        return self._cooldown_until

    @property
    def is_in_cooldown(self) -> bool:
        """Check if currently in cooldown period."""
        return not self._is_cooldown_expired()

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

    @property
    def consecutive_failures(self) -> int:
        """Get current consecutive failure count."""
        return self._consecutive_failures

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
