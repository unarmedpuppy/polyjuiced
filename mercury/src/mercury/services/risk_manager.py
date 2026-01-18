"""Risk Manager - pre-trade validation and risk controls.

This service:
- Validates trading signals against risk limits
- Tracks exposure and daily P&L
- Manages 4-level circuit breaker state based on failures and losses
- Publishes risk.circuit_breaker events on state changes
- Approves or rejects signals via event bus
- Enforces position limits via StateStore queries
- Schedules automatic daily reset at configurable time

Circuit Breaker Levels (ported from legacy/src/risk/circuit_breaker.py):
- NORMAL: All systems go, full position sizes
- WARNING: Near limits, reduce position sizes by 50%
- CAUTION: Only close existing positions, no new positions
- HALT: No trading at all, full system pause

Position Limits:
- max_position_size_usd: Maximum size for any single trade
- max_unhedged_exposure_usd: Maximum total unhedged exposure across all markets
- max_per_market_exposure_usd: Maximum exposure in a single market

Daily Loss Tracking:
- Tracks realized P&L throughout the day
- Trips circuit breaker at warning/caution/halt thresholds
- Resets at configurable time (midnight UTC by default)
- Publishes risk.daily_stats events with current P&L state
"""

import asyncio
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, List, Optional, Tuple

import structlog

from mercury.core.config import ConfigManager
from mercury.core.events import EventBus
from mercury.core.lifecycle import BaseComponent, HealthCheckResult, HealthStatus
from mercury.domain.order import Fill
from mercury.domain.risk import CircuitBreakerState, RiskLimits
from mercury.domain.signal import ApprovedSignal, RejectedSignal, SignalType, TradingSignal

if TYPE_CHECKING:
    from mercury.services.state_store import StateStore

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
        state_store: Optional["StateStore"] = None,
    ):
        """Initialize the risk manager.

        Args:
            config: Configuration manager.
            event_bus: EventBus for events.
            state_store: Optional StateStore for querying current positions.
                         If not provided, position limit checks will use in-memory tracking only.
        """
        super().__init__()
        self._config = config
        self._event_bus = event_bus
        self._state_store = state_store
        self._log = log.bind(component="risk_manager")

        # Load limits from config
        self._limits = RiskLimits(
            max_daily_loss_usd=self._get_decimal("risk.max_daily_loss_usd", Decimal("100")),
            max_position_size_usd=self._get_decimal("risk.max_position_size_usd", Decimal("25")),
            max_unhedged_exposure_usd=self._get_decimal("risk.max_unhedged_exposure_usd", Decimal("50")),
            max_per_market_exposure_usd=self._get_decimal("risk.max_per_market_exposure_usd", Decimal("100")),
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

        # Daily reset configuration
        self._daily_reset_enabled = self._config.get("risk.daily_reset_enabled", True)
        self._daily_reset_time_utc = self._parse_reset_time(
            self._config.get("risk.daily_reset_time_utc", "00:00")
        )
        self._reset_task: Optional[asyncio.Task[None]] = None

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
        # Per-market exposure tracking (in-memory cache, updated from fills)
        self._market_exposures: dict[str, Decimal] = {}
        # Peak/max tracking for daily stats
        self._daily_peak_pnl: Decimal = Decimal("0")
        self._daily_max_drawdown: Decimal = Decimal("0")

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

    def _parse_reset_time(self, time_str: str) -> time:
        """Parse reset time from HH:MM string format.

        Args:
            time_str: Time in "HH:MM" format (24-hour).

        Returns:
            time object representing the reset time.
        """
        try:
            parts = time_str.split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
            return time(hour=hour, minute=minute, tzinfo=timezone.utc)
        except (ValueError, IndexError):
            self._log.warning(
                "invalid_reset_time_format",
                time_str=time_str,
                using_default="00:00",
            )
            return time(hour=0, minute=0, tzinfo=timezone.utc)

    def _get_next_reset_datetime(self) -> datetime:
        """Calculate the next daily reset datetime.

        Returns:
            datetime of the next scheduled reset in UTC.
        """
        now = datetime.now(timezone.utc)
        today_reset = datetime.combine(
            now.date(),
            self._daily_reset_time_utc,
            tzinfo=timezone.utc,
        )

        # If today's reset time has already passed, schedule for tomorrow
        if now >= today_reset:
            tomorrow = now.date() + timedelta(days=1)
            return datetime.combine(
                tomorrow,
                self._daily_reset_time_utc,
                tzinfo=timezone.utc,
            )

        return today_reset

    def _get_seconds_until_reset(self) -> float:
        """Get seconds until the next daily reset.

        Returns:
            Number of seconds until the next reset.
        """
        next_reset = self._get_next_reset_datetime()
        now = datetime.now(timezone.utc)
        return (next_reset - now).total_seconds()

    async def _do_start(self) -> None:
        """Start the risk manager."""
        self._log.info(
            "starting_risk_manager",
            max_daily_loss=str(self._limits.max_daily_loss_usd),
            max_position_size=str(self._limits.max_position_size_usd),
            max_unhedged_exposure=str(self._limits.max_unhedged_exposure_usd),
            max_per_market_exposure=str(self._limits.max_per_market_exposure_usd),
            state_store_enabled=self._state_store is not None,
            daily_reset_enabled=self._daily_reset_enabled,
            daily_reset_time_utc=self._daily_reset_time_utc.isoformat(),
        )

        # Subscribe to events
        await self._event_bus.subscribe("signal.*", self._on_signal)
        await self._event_bus.subscribe("order.filled", self._on_order_filled)
        await self._event_bus.subscribe("position.closed", self._on_position_closed)

        # Start daily reset scheduler if enabled
        if self._daily_reset_enabled:
            self._reset_task = asyncio.create_task(self._daily_reset_scheduler())
            self._log.info(
                "daily_reset_scheduler_started",
                next_reset=self._get_next_reset_datetime().isoformat(),
                seconds_until_reset=self._get_seconds_until_reset(),
            )

        self._log.info("risk_manager_started")

    async def _do_stop(self) -> None:
        """Stop the risk manager."""
        # Cancel daily reset scheduler if running
        if self._reset_task is not None:
            self._reset_task.cancel()
            try:
                await self._reset_task
            except asyncio.CancelledError:
                pass
            self._reset_task = None
            self._log.info("daily_reset_scheduler_stopped")

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

        Checks performed:
        1. Circuit breaker state (HALT blocks all, CAUTION blocks new positions)
        2. Daily loss limit
        3. Per-trade position size limit (with WARNING state multiplier)
        4. Total unhedged exposure limit
        5. Per-market exposure limit (queries StateStore if available)

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
            # Allow CLOSE_POSITION signals at CAUTION level
            if signal.signal_type == SignalType.CLOSE_POSITION:
                return True, None
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

        # Check total unhedged exposure for non-arbitrage signals
        if signal.signal_type != SignalType.ARBITRAGE:
            # Get current unhedged exposure from StateStore if available, else use in-memory tracking
            total_unhedged = await self._get_total_unhedged_exposure()
            new_exposure = total_unhedged + signal.target_size_usd
            if new_exposure > self._limits.max_unhedged_exposure_usd:
                return False, f"Unhedged exposure would exceed limit: ${new_exposure:.2f} > ${self._limits.max_unhedged_exposure_usd:.2f}"

        # Check per-market exposure limit
        market_exposure = await self._get_market_exposure(signal.market_id)
        new_market_exposure = market_exposure + signal.target_size_usd
        if new_market_exposure > self._limits.max_per_market_exposure_usd:
            return False, f"Per-market exposure would exceed limit for {signal.market_id}: ${new_market_exposure:.2f} > ${self._limits.max_per_market_exposure_usd:.2f}"

        return True, None

    async def _get_total_unhedged_exposure(self) -> Decimal:
        """Get total unhedged exposure across all markets.

        Queries StateStore for open positions if available, otherwise
        uses in-memory tracking.

        Returns:
            Total unhedged exposure in USD.
        """
        if self._state_store is not None and self._state_store.is_connected:
            try:
                positions = await self._state_store.get_open_positions()
                # Sum up exposure from non-hedged positions
                # For arbitrage positions (both YES and NO), exposure is hedged
                # For single-sided positions, the full cost is unhedged exposure
                total = Decimal("0")
                for position in positions:
                    # Position cost basis is size * entry_price
                    total += position.size * position.entry_price
                return total
            except Exception as e:
                self._log.warning(
                    "failed_to_query_positions_for_exposure",
                    error=str(e),
                    fallback="in-memory",
                )
                return self._unhedged_exposure
        return self._unhedged_exposure

    async def _get_market_exposure(self, market_id: str) -> Decimal:
        """Get current exposure in a specific market.

        Queries StateStore for open positions in the market if available,
        otherwise uses in-memory tracking.

        Args:
            market_id: The market to check exposure for.

        Returns:
            Current exposure in USD for the market.
        """
        if self._state_store is not None and self._state_store.is_connected:
            try:
                positions = await self._state_store.get_open_positions(market_id=market_id)
                # Sum up all position values for this market
                total = Decimal("0")
                for position in positions:
                    total += position.size * position.entry_price
                return total
            except Exception as e:
                self._log.warning(
                    "failed_to_query_positions_for_market_exposure",
                    market_id=market_id,
                    error=str(e),
                    fallback="in-memory",
                )
                return self._market_exposures.get(market_id, Decimal("0"))
        return self._market_exposures.get(market_id, Decimal("0"))

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

        Updates:
        - Daily trade count
        - Daily volume
        - Current total exposure
        - Per-market exposure

        Args:
            fill: The fill to record.
        """
        self._daily_trades += 1
        self._daily_volume += fill.cost
        self._current_exposure += fill.cost

        # Track per-market exposure
        current_market_exposure = self._market_exposures.get(fill.market_id, Decimal("0"))
        self._market_exposures[fill.market_id] = current_market_exposure + fill.cost

        self._log.debug(
            "fill_recorded",
            order_id=fill.order_id,
            market_id=fill.market_id,
            cost=str(fill.cost),
            current_exposure=str(self._current_exposure),
            market_exposure=str(self._market_exposures[fill.market_id]),
        )

    def record_pnl(self, pnl: Decimal) -> None:
        """Record realized P&L.

        Updates daily P&L, peak P&L, and max drawdown tracking.
        Triggers circuit breaker state transitions based on loss thresholds.
        Publishes risk.daily_stats event with updated metrics.

        Args:
            pnl: P&L amount (positive = profit, negative = loss).
        """
        self._daily_pnl += pnl

        # Update peak P&L tracking (high water mark)
        if self._daily_pnl > self._daily_peak_pnl:
            self._daily_peak_pnl = self._daily_pnl

        # Calculate and track max drawdown from peak
        drawdown = self._daily_peak_pnl - self._daily_pnl
        if drawdown > self._daily_max_drawdown:
            self._daily_max_drawdown = drawdown

        self._log.info(
            "pnl_recorded",
            pnl=str(pnl),
            daily_pnl=str(self._daily_pnl),
            peak_pnl=str(self._daily_peak_pnl),
            max_drawdown=str(self._daily_max_drawdown),
        )

        # Update circuit breaker based on loss
        self._update_circuit_breaker_for_loss()

        # Publish daily stats event asynchronously
        self._publish_daily_stats_event()

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

    def _publish_daily_stats_event(self) -> None:
        """Publish risk.daily_stats event with current P&L metrics.

        This is called after each P&L update to provide real-time tracking.
        The event is published asynchronously (fire and forget).
        """
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._async_publish_daily_stats())
        except RuntimeError:
            # No event loop running (e.g., in sync tests) - skip publishing
            pass

    async def _async_publish_daily_stats(self) -> None:
        """Async implementation of daily stats event publishing."""
        try:
            # Calculate loss percentage relative to limit
            loss_pct = Decimal("0")
            if self._limits.max_daily_loss_usd > 0:
                current_loss = -self._daily_pnl if self._daily_pnl < 0 else Decimal("0")
                loss_pct = (current_loss / self._limits.max_daily_loss_usd) * 100

            await self._event_bus.publish(
                "risk.daily_stats",
                {
                    "daily_pnl": str(self._daily_pnl),
                    "daily_peak_pnl": str(self._daily_peak_pnl),
                    "daily_max_drawdown": str(self._daily_max_drawdown),
                    "daily_volume": str(self._daily_volume),
                    "daily_trades": self._daily_trades,
                    "current_exposure": str(self._current_exposure),
                    "loss_limit_pct": str(loss_pct),
                    "loss_limit_usd": str(self._limits.max_daily_loss_usd),
                    "circuit_breaker_state": self._circuit_breaker_state.value,
                    "warning_threshold_usd": str(self._warning_loss),
                    "caution_threshold_usd": str(self._caution_loss),
                    "halt_threshold_usd": str(self._halt_loss),
                    "last_reset": self._last_reset.isoformat(),
                    "next_reset": self._get_next_reset_datetime().isoformat(),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as e:
            self._log.error("failed_to_publish_daily_stats_event", error=str(e))

    async def _publish_exposure_update_event(self, fill: Fill, is_hedged: bool) -> None:
        """Publish risk.exposure.updated event after a fill is processed.

        Args:
            fill: The fill that triggered the update.
            is_hedged: Whether this was a hedged (arbitrage) trade.
        """
        try:
            await self._event_bus.publish(
                "risk.exposure.updated",
                {
                    "event_type": "fill",
                    "fill_id": fill.fill_id,
                    "order_id": fill.order_id,
                    "market_id": fill.market_id,
                    "fill_cost": str(fill.cost),
                    "is_hedged": is_hedged,
                    "current_exposure": str(self._current_exposure),
                    "unhedged_exposure": str(self._unhedged_exposure),
                    "market_exposure": str(self._market_exposures.get(fill.market_id, Decimal("0"))),
                    "daily_volume": str(self._daily_volume),
                    "daily_trades": self._daily_trades,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as e:
            self._log.error("failed_to_publish_exposure_event", error=str(e))

    async def _daily_reset_scheduler(self) -> None:
        """Background task that schedules daily resets at configured time.

        This task runs continuously and waits until the next reset time,
        then performs the reset and publishes a reset event.
        """
        self._log.info("daily_reset_scheduler_loop_started")

        while True:
            try:
                # Calculate time until next reset
                seconds_until_reset = self._get_seconds_until_reset()

                self._log.debug(
                    "daily_reset_scheduled",
                    next_reset=self._get_next_reset_datetime().isoformat(),
                    seconds_until_reset=seconds_until_reset,
                )

                # Wait until reset time
                await asyncio.sleep(seconds_until_reset)

                # Perform the reset
                self._log.info(
                    "performing_scheduled_daily_reset",
                    reset_time=datetime.now(timezone.utc).isoformat(),
                )

                # Capture pre-reset stats for the event
                pre_reset_stats = {
                    "final_pnl": str(self._daily_pnl),
                    "final_peak_pnl": str(self._daily_peak_pnl),
                    "final_max_drawdown": str(self._daily_max_drawdown),
                    "final_volume": str(self._daily_volume),
                    "final_trades": self._daily_trades,
                    "final_circuit_breaker_state": self._circuit_breaker_state.value,
                }

                # Do the reset
                self.reset_daily()

                # Publish daily reset event
                await self._event_bus.publish(
                    "risk.daily_reset",
                    {
                        **pre_reset_stats,
                        "reset_time": self._last_reset.isoformat(),
                        "next_reset": self._get_next_reset_datetime().isoformat(),
                        "reset_type": "scheduled",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                )

                self._log.info(
                    "daily_reset_completed",
                    next_reset=self._get_next_reset_datetime().isoformat(),
                )

            except asyncio.CancelledError:
                self._log.info("daily_reset_scheduler_cancelled")
                raise
            except Exception as e:
                self._log.error(
                    "daily_reset_scheduler_error",
                    error=str(e),
                    retry_in_seconds=60,
                )
                # Wait a bit before retrying on error
                await asyncio.sleep(60)

    def _is_cooldown_expired(self) -> bool:
        """Check if circuit breaker cooldown has expired."""
        if self._cooldown_until is None:
            return True
        return datetime.now(timezone.utc) >= self._cooldown_until

    def reset_daily(self) -> None:
        """Reset daily counters (called at midnight or on demand).

        Resets:
        - Daily P&L, volume, and trade count
        - Current and per-market exposure
        - Peak P&L and max drawdown tracking
        - Consecutive failure count
        - Circuit breaker state to NORMAL
        """
        self._log.info(
            "resetting_daily_limits",
            final_pnl=str(self._daily_pnl),
            final_trades=self._daily_trades,
            final_volume=str(self._daily_volume),
            final_peak_pnl=str(self._daily_peak_pnl),
            final_max_drawdown=str(self._daily_max_drawdown),
            final_circuit_breaker_state=self._circuit_breaker_state.value,
            markets_with_exposure=len(self._market_exposures),
        )

        self._daily_pnl = Decimal("0")
        self._daily_volume = Decimal("0")
        self._daily_trades = 0
        self._current_exposure = Decimal("0")
        self._unhedged_exposure = Decimal("0")
        self._market_exposures = {}
        self._consecutive_failures = 0
        self._circuit_breaker_state = CircuitBreakerState.NORMAL
        self._circuit_breaker_triggered_at = None
        self._circuit_breaker_reasons = []
        self._cooldown_until = None
        self._last_reset = datetime.now(timezone.utc)
        # Reset peak/drawdown tracking
        self._daily_peak_pnl = Decimal("0")
        self._daily_max_drawdown = Decimal("0")

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
    def unhedged_exposure(self) -> Decimal:
        """Get current unhedged (directional) exposure."""
        return self._unhedged_exposure

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

    @property
    def market_exposures(self) -> dict[str, Decimal]:
        """Get per-market exposure snapshot (copy)."""
        return self._market_exposures.copy()

    @property
    def limits(self) -> RiskLimits:
        """Get configured risk limits."""
        return self._limits

    @property
    def daily_peak_pnl(self) -> Decimal:
        """Get daily peak P&L (high water mark)."""
        return self._daily_peak_pnl

    @property
    def daily_max_drawdown(self) -> Decimal:
        """Get daily maximum drawdown from peak."""
        return self._daily_max_drawdown

    @property
    def daily_volume(self) -> Decimal:
        """Get daily trading volume."""
        return self._daily_volume

    @property
    def last_reset(self) -> datetime:
        """Get timestamp of last daily reset."""
        return self._last_reset

    @property
    def next_reset(self) -> datetime:
        """Get timestamp of next scheduled daily reset."""
        return self._get_next_reset_datetime()

    @property
    def daily_reset_enabled(self) -> bool:
        """Check if automatic daily reset is enabled."""
        return self._daily_reset_enabled

    @property
    def daily_reset_time_utc(self) -> time:
        """Get configured daily reset time in UTC."""
        return self._daily_reset_time_utc

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
        """Handle order filled event from event bus.

        Updates:
        - Daily trade count, volume, and exposure via record_fill()
        - Unhedged exposure based on signal type (arbitrage vs directional)
        - Per-market exposure tracking
        - Publishes risk.exposure.updated event

        Expected event data:
        - fill_id: Optional fill identifier
        - order_id: Order identifier
        - market_id: Market identifier
        - token_id: Token identifier
        - side: BUY or SELL
        - outcome: YES or NO
        - size: Fill size in shares
        - price: Fill price
        - fee: Optional fee amount
        - signal_type: Optional - ARBITRAGE means hedged, else unhedged
        - cost: Optional - pre-computed cost, else computed from size * price + fee
        """
        try:
            import uuid

            # Build fill from event data
            fill = Fill(
                fill_id=data.get("fill_id", str(uuid.uuid4())),
                order_id=data.get("order_id", ""),
                market_id=data.get("market_id", ""),
                token_id=data.get("token_id", ""),
                side=data.get("side", "BUY"),
                outcome=data.get("outcome", "YES"),
                size=Decimal(str(data.get("size", 0))),
                price=Decimal(str(data.get("price", 0))),
                fee=Decimal(str(data.get("fee", 0))),
                cost=Decimal(str(data["cost"])) if "cost" in data else None,
            )

            # Record the fill (updates daily stats and per-market exposure)
            self.record_fill(fill)

            # Update unhedged exposure based on signal type
            # Arbitrage trades are hedged (both YES and NO), so no unhedged exposure change
            # Directional trades add to unhedged exposure
            signal_type = data.get("signal_type", "").upper()
            is_hedged = signal_type == "ARBITRAGE"

            if not is_hedged:
                # Directional trade - add to unhedged exposure
                self._unhedged_exposure += fill.cost
                self._log.debug(
                    "unhedged_exposure_updated",
                    market_id=fill.market_id,
                    fill_cost=str(fill.cost),
                    unhedged_exposure=str(self._unhedged_exposure),
                    is_buy=data.get("side", "BUY").upper() == "BUY",
                )

            # Publish exposure update event for observability
            await self._publish_exposure_update_event(fill, is_hedged=is_hedged)

        except Exception as e:
            self._log.error("fill_processing_error", error=str(e), data=data)

    async def _on_position_closed(self, data: dict) -> None:
        """Handle position closed event from event bus.

        Updates:
        - Realized P&L via record_pnl()
        - Reduces current exposure by position cost basis
        - Reduces per-market exposure
        - Reduces unhedged exposure for non-arbitrage positions
        - Publishes risk.exposure.updated event

        Expected event data:
        - market_id: Market identifier
        - position_id: Position identifier
        - realized_pnl: P&L from closing the position
        - cost_basis: Optional - original cost of the position
        - is_hedged: Optional - whether this was an arbitrage (hedged) position
        - size: Optional - position size that was closed
        - entry_price: Optional - entry price (to compute cost_basis if not provided)
        """
        try:
            market_id = data.get("market_id", "")
            position_id = data.get("position_id", "")

            # Record the realized P&L
            pnl = Decimal(str(data.get("realized_pnl", 0)))
            self.record_pnl(pnl)

            # Calculate cost basis for exposure reduction
            # Try cost_basis directly, or compute from size * entry_price
            if "cost_basis" in data:
                cost_basis = Decimal(str(data["cost_basis"]))
            elif "size" in data and "entry_price" in data:
                size = Decimal(str(data["size"]))
                entry_price = Decimal(str(data["entry_price"]))
                cost_basis = size * entry_price
            else:
                # Cannot determine cost basis - skip exposure update
                self._log.warning(
                    "position_closed_missing_cost_basis",
                    position_id=position_id,
                    market_id=market_id,
                )
                return

            # Reduce current total exposure
            self._current_exposure = max(Decimal("0"), self._current_exposure - cost_basis)

            # Reduce per-market exposure
            if market_id and market_id in self._market_exposures:
                current_market_exposure = self._market_exposures[market_id]
                new_market_exposure = max(Decimal("0"), current_market_exposure - cost_basis)
                if new_market_exposure == Decimal("0"):
                    del self._market_exposures[market_id]
                else:
                    self._market_exposures[market_id] = new_market_exposure

            # Reduce unhedged exposure for non-arbitrage positions
            is_hedged = data.get("is_hedged", False)
            if not is_hedged:
                self._unhedged_exposure = max(Decimal("0"), self._unhedged_exposure - cost_basis)

            self._log.info(
                "position_closed_exposure_updated",
                position_id=position_id,
                market_id=market_id,
                realized_pnl=str(pnl),
                cost_basis=str(cost_basis),
                is_hedged=is_hedged,
                current_exposure=str(self._current_exposure),
                unhedged_exposure=str(self._unhedged_exposure),
            )

            # Publish exposure update event
            await self._event_bus.publish(
                "risk.exposure.updated",
                {
                    "event_type": "position_closed",
                    "position_id": position_id,
                    "market_id": market_id,
                    "cost_basis_removed": str(cost_basis),
                    "realized_pnl": str(pnl),
                    "current_exposure": str(self._current_exposure),
                    "unhedged_exposure": str(self._unhedged_exposure),
                    "market_exposure": str(self._market_exposures.get(market_id, Decimal("0"))),
                    "is_hedged": is_hedged,
                    "daily_pnl": str(self._daily_pnl),
                    "daily_trades": self._daily_trades,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )

        except Exception as e:
            self._log.error("position_closed_processing_error", error=str(e), data=data)
