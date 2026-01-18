"""Execution Engine - handles order execution and lifecycle management.

This service:
- Executes trading signals as orders
- Handles dual-leg arbitrage execution
- Manages order lifecycle (submit, fill, cancel)
- Tracks execution latency and slippage
- Manages order queue with priority
- Limits concurrent executions
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

import structlog

from mercury.core.config import ConfigManager
from mercury.core.events import EventBus
from mercury.core.lifecycle import BaseComponent, HealthCheckResult, HealthStatus
from mercury.domain.order import (
    Order,
    OrderRequest,
    OrderResult as DomainOrderResult,
    DualLegResult,
    OrderSide as DomainOrderSide,
    OrderStatus as DomainOrderStatus,
    OrderType,
    Fill,
    Position,
    PositionStatus,
)
from mercury.domain.signal import ApprovedSignal, SignalType, SignalPriority
from mercury.integrations.polymarket.clob import CLOBClient, InsufficientLiquidityError
from mercury.integrations.polymarket.types import (
    DualLegOrderResult,
    OrderResult,
    OrderSide,
    OrderStatus,
    PolymarketSettings,
)

log = structlog.get_logger()


class QueuedSignalStatus(str, Enum):
    """Status of a queued signal."""

    PENDING = "pending"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass
class QueuedSignal:
    """A signal waiting in the execution queue.

    Signals are prioritized by priority level (CRITICAL > HIGH > MEDIUM > LOW)
    and then by queued time (FIFO within priority).
    """

    signal_id: str
    signal_data: dict[str, Any]
    priority: SignalPriority
    status: QueuedSignalStatus = QueuedSignalStatus.PENDING
    queued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None

    def __lt__(self, other: "QueuedSignal") -> bool:
        """Compare for priority queue ordering.

        Higher priority signals come first, then earlier queued signals.
        """
        priority_order = {
            SignalPriority.CRITICAL: 0,
            SignalPriority.HIGH: 1,
            SignalPriority.MEDIUM: 2,
            SignalPriority.LOW: 3,
        }
        self_priority = priority_order.get(self.priority, 2)
        other_priority = priority_order.get(other.priority, 2)

        if self_priority != other_priority:
            return self_priority < other_priority
        return self.queued_at < other.queued_at


@dataclass
class ExecutionSignal:
    """Signal data for execution.

    This is a flattened version of ApprovedSignal for internal use,
    since the domain ApprovedSignal wraps a TradingSignal.
    """

    signal_id: str
    original_signal_id: str
    market_id: str
    signal_type: SignalType
    target_size_usd: Decimal
    yes_price: Decimal
    no_price: Decimal
    yes_token_id: str
    no_token_id: str
    approved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ExecutionResult:
    """Result of executing a signal."""

    success: bool
    signal_id: str
    trade_id: Optional[str] = None
    position_id: Optional[str] = None
    yes_filled: Decimal = Decimal("0")
    no_filled: Decimal = Decimal("0")
    total_cost: Decimal = Decimal("0")
    guaranteed_pnl: Decimal = Decimal("0")
    execution_time_ms: Optional[float] = None
    error: Optional[str] = None


class ExecutionEngine(BaseComponent):
    """Executes approved trading signals as orders.

    This service:
    1. Listens for approved signals from RiskManager
    2. Queues signals for execution with priority ordering
    3. Manages concurrent execution limits
    4. Executes orders via CLOBClient
    5. Handles dual-leg arbitrage atomically
    6. Publishes execution results to EventBus

    Event channels subscribed:
    - risk.approved.* - Approved signals to execute

    Event channels published:
    - order.submitted - Order sent to exchange
    - order.filled - Order filled
    - order.rejected - Order rejected
    - position.opened - New position created
    - execution.complete - Execution finished
    - execution.queue.added - Signal added to queue
    - execution.queue.started - Signal execution started
    """

    # Default configuration values
    DEFAULT_MAX_CONCURRENT = 3
    DEFAULT_MAX_QUEUE_SIZE = 100
    DEFAULT_QUEUE_TIMEOUT_SECONDS = 60.0

    def __init__(
        self,
        config: ConfigManager,
        event_bus: EventBus,
        clob_client: Optional[CLOBClient] = None,
    ):
        """Initialize the execution engine.

        Args:
            config: Configuration manager.
            event_bus: EventBus for events.
            clob_client: Optional pre-configured CLOB client.
        """
        super().__init__()
        self._config = config
        self._event_bus = event_bus
        self._log = log.bind(component="execution_engine")

        # CLOB client
        if clob_client is None:
            settings = PolymarketSettings(
                private_key=config.get("polymarket.private_key", ""),
                api_key=config.get("polymarket.api_key", ""),
                api_secret=config.get("polymarket.api_secret", ""),
                api_passphrase=config.get("polymarket.api_passphrase", ""),
            )
            clob_client = CLOBClient(settings)

        self._clob = clob_client

        # Configuration
        self._dry_run = config.get_bool("mercury.dry_run", True)
        self._rebalance_enabled = config.get_bool("execution.rebalance_partial_fills", True)

        # Queue configuration
        self._max_concurrent = config.get_int(
            "execution.max_concurrent", self.DEFAULT_MAX_CONCURRENT
        )
        self._max_queue_size = config.get_int(
            "execution.max_queue_size", self.DEFAULT_MAX_QUEUE_SIZE
        )
        self._queue_timeout = config.get_float(
            "execution.queue_timeout_seconds", self.DEFAULT_QUEUE_TIMEOUT_SECONDS
        )

        # State
        self._pending_orders: dict[str, OrderResult] = {}
        self._open_orders: dict[str, Order] = {}  # Track open orders by order_id
        self._should_run = False

        # Queue management
        self._queue: asyncio.PriorityQueue[QueuedSignal] = asyncio.PriorityQueue(
            maxsize=self._max_queue_size
        )
        self._queue_items: dict[str, QueuedSignal] = {}  # Track queued items by ID
        self._active_executions: dict[str, asyncio.Task] = {}  # Currently executing
        self._execution_semaphore: Optional[asyncio.Semaphore] = None
        self._queue_processor_task: Optional[asyncio.Task] = None

        # Metrics
        self._total_queued = 0
        self._total_executed = 0
        self._total_failed = 0
        self._total_expired = 0

    async def _do_start(self) -> None:
        """Component-specific startup logic."""
        self._start_time = time.time()
        self._should_run = True
        self._log.info(
            "starting_execution_engine",
            dry_run=self._dry_run,
            max_concurrent=self._max_concurrent,
            max_queue_size=self._max_queue_size,
        )

        # Initialize semaphore for concurrent execution limits
        self._execution_semaphore = asyncio.Semaphore(self._max_concurrent)

        # Connect to CLOB
        if not self._dry_run:
            await self._clob.connect()

        # Subscribe to approved signals
        await self._event_bus.subscribe("risk.approved.*", self._on_approved_signal)

        # Start queue processor
        self._queue_processor_task = asyncio.create_task(self._process_queue())

        self._log.info("execution_engine_started")

    async def _do_stop(self) -> None:
        """Component-specific shutdown logic."""
        self._should_run = False
        self._log.info("stopping_execution_engine")

        # Stop queue processor
        if self._queue_processor_task and not self._queue_processor_task.done():
            self._queue_processor_task.cancel()
            try:
                await self._queue_processor_task
            except asyncio.CancelledError:
                pass

        # Wait for active executions to complete (with timeout)
        if self._active_executions:
            self._log.info(
                "waiting_for_active_executions",
                count=len(self._active_executions),
            )
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._active_executions.values(), return_exceptions=True),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                self._log.warning("active_executions_timeout")
                for task in self._active_executions.values():
                    task.cancel()

        # Cancel pending orders
        if not self._dry_run and self._pending_orders:
            self._log.info("cancelling_pending_orders", count=len(self._pending_orders))
            await self._clob.cancel_all_orders()

        # Close CLOB connection
        await self._clob.close()

        self._log.info(
            "execution_engine_stopped",
            total_queued=self._total_queued,
            total_executed=self._total_executed,
            total_failed=self._total_failed,
        )

    async def _do_health_check(self) -> HealthCheckResult:
        """Component-specific health check."""
        queue_size = self._queue.qsize()
        active_count = len(self._active_executions)

        details = {
            "pending_orders": len(self._pending_orders),
            "queue_size": queue_size,
            "active_executions": active_count,
            "max_concurrent": self._max_concurrent,
            "max_queue_size": self._max_queue_size,
            "total_queued": self._total_queued,
            "total_executed": self._total_executed,
            "total_failed": self._total_failed,
            "total_expired": self._total_expired,
            "dry_run": self._dry_run,
        }

        if self._dry_run:
            return HealthCheckResult(
                status=HealthStatus.HEALTHY,
                message="Running in dry-run mode",
                details=details,
            )

        if not self._clob._connected:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message="CLOB client not connected",
                details=details,
            )

        # Check for queue saturation
        if queue_size >= self._max_queue_size * 0.9:
            return HealthCheckResult(
                status=HealthStatus.DEGRADED,
                message=f"Queue nearly full ({queue_size}/{self._max_queue_size})",
                details=details,
            )

        return HealthCheckResult(
            status=HealthStatus.HEALTHY,
            message="Connected and ready",
            details=details,
        )

    async def execute(self, signal: ExecutionSignal) -> ExecutionResult:
        """Execute an approved trading signal.

        Args:
            signal: The approved signal to execute.

        Returns:
            ExecutionResult with execution details.
        """
        start_time = time.time() * 1000

        self._log.info(
            "executing_signal",
            signal_id=signal.signal_id,
            signal_type=signal.signal_type.value,
            target_size=str(signal.target_size_usd),
            dry_run=self._dry_run,
        )

        # Generate IDs
        trade_id = f"trade-{uuid.uuid4().hex[:8]}"
        position_id = f"pos-{uuid.uuid4().hex[:8]}"

        try:
            if self._dry_run:
                result = await self._execute_dry_run(signal, trade_id, position_id)
            elif signal.signal_type == SignalType.ARBITRAGE:
                result = await self._execute_dual_leg(signal, trade_id, position_id)
            else:
                result = await self._execute_single_leg(signal, trade_id, position_id)

            result.execution_time_ms = time.time() * 1000 - start_time

            # Publish completion
            await self._event_bus.publish("execution.complete", {
                "signal_id": signal.signal_id,
                "trade_id": result.trade_id,
                "success": result.success,
                "total_cost": str(result.total_cost),
                "guaranteed_pnl": str(result.guaranteed_pnl),
                "execution_ms": result.execution_time_ms,
            })

            return result

        except Exception as e:
            self._log.error("execution_failed", signal_id=signal.signal_id, error=str(e))
            return ExecutionResult(
                success=False,
                signal_id=signal.signal_id,
                error=str(e),
                execution_time_ms=time.time() * 1000 - start_time,
            )

    # =========================================================================
    # Single Order Execution (FOK/GTC Support)
    # =========================================================================

    async def execute_order(
        self,
        order_request: OrderRequest,
        timeout: float = 30.0,
    ) -> DomainOrderResult:
        """Execute a single order request with FOK or GTC order type support.

        This method handles the complete order lifecycle:
        1. Creates an Order from the request (status: PENDING)
        2. Submits to exchange (status: SUBMITTED)
        3. Tracks fill status (status: FILLED, PARTIALLY_FILLED, REJECTED, CANCELLED)
        4. Emits order.* events for each state transition

        Args:
            order_request: The order request containing market, token, price, size, and type.
            timeout: Maximum time to wait for order completion (seconds).

        Returns:
            DomainOrderResult with order details and any fills.

        Event channels published:
            - order.pending: Order created, awaiting submission
            - order.submitted: Order sent to exchange
            - order.filled: Order completely filled
            - order.partially_filled: Order partially filled (GTC only)
            - order.rejected: Order rejected by exchange
            - order.cancelled: Order was cancelled
            - order.expired: FOK order expired without fill
        """
        start_time = time.time()
        order_id = f"ord-{uuid.uuid4().hex[:12]}"

        self._log.info(
            "execute_order_start",
            order_id=order_id,
            client_order_id=order_request.client_order_id,
            market_id=order_request.market_id,
            token_id=order_request.token_id,
            side=order_request.side.value,
            order_type=order_request.order_type.value,
            size=str(order_request.size),
            price=str(order_request.price),
            dry_run=self._dry_run,
        )

        # Create initial order object with PENDING status
        order = Order(
            order_id=order_id,
            market_id=order_request.market_id,
            token_id=order_request.token_id,
            side=order_request.side,
            outcome=order_request.outcome,
            requested_size=order_request.size,
            filled_size=Decimal("0"),
            price=order_request.price,
            status=DomainOrderStatus.PENDING,
            order_type=order_request.order_type,
            client_order_id=order_request.client_order_id,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        # Emit order.pending event
        await self._emit_order_event("order.pending", order)

        try:
            # Submit order
            order = await self._submit_order(order, order_request)

            # For FOK orders, handle fill-or-kill logic
            if order_request.order_type == OrderType.FOK:
                order = await self._handle_fok_order(order, timeout)
            else:
                # GTC orders - wait for fill or timeout
                order = await self._handle_gtc_order(order, timeout)

            # Calculate latency
            latency_ms = (time.time() - start_time) * 1000

            # Build fills list
            fills = self._create_fills_from_order(order)

            result = DomainOrderResult(
                success=order.status == DomainOrderStatus.FILLED,
                order=order,
                fills=fills,
                error_message=None if order.status == DomainOrderStatus.FILLED else f"Order status: {order.status.value}",
                latency_ms=latency_ms,
            )

            self._log.info(
                "execute_order_complete",
                order_id=order.order_id,
                status=order.status.value,
                filled_size=str(order.filled_size),
                latency_ms=latency_ms,
            )

            return result

        except Exception as e:
            # Update order to REJECTED status
            order.status = DomainOrderStatus.REJECTED
            order.updated_at = datetime.now(timezone.utc)

            await self._emit_order_event("order.rejected", order, error=str(e))

            self._log.error(
                "execute_order_failed",
                order_id=order.order_id,
                error=str(e),
            )

            return DomainOrderResult(
                success=False,
                order=order,
                fills=[],
                error_message=str(e),
                latency_ms=(time.time() - start_time) * 1000,
            )

    async def _submit_order(
        self,
        order: Order,
        order_request: OrderRequest,
    ) -> Order:
        """Submit order to exchange and update status to SUBMITTED.

        Args:
            order: The order to submit.
            order_request: Original order request.

        Returns:
            Updated order with SUBMITTED status.
        """
        order.status = DomainOrderStatus.SUBMITTED
        order.updated_at = datetime.now(timezone.utc)

        await self._emit_order_event("order.submitted", order)

        if self._dry_run:
            # In dry-run mode, simulate immediate fill
            self._log.info("dry_run_order_submitted", order_id=order.order_id)
            return order

        # Submit to CLOB
        clob_side = OrderSide.BUY if order_request.side == DomainOrderSide.BUY else OrderSide.SELL

        clob_result = await self._clob.execute_order(
            token_id=order_request.token_id,
            side=clob_side,
            amount_shares=order_request.size,
            price=order_request.price,
        )

        # Update order with CLOB response
        if clob_result.order_id:
            # Keep our internal order_id but track exchange order_id in metadata
            self._log.info(
                "order_submitted_to_clob",
                internal_order_id=order.order_id,
                clob_order_id=clob_result.order_id,
            )

        return order

    async def _handle_fok_order(
        self,
        order: Order,
        timeout: float,
    ) -> Order:
        """Handle Fill-or-Kill order execution.

        FOK orders must be completely filled or not at all.
        If the order cannot be filled immediately, it is cancelled/expired.

        Args:
            order: The submitted order.
            timeout: Maximum time to wait for fill.

        Returns:
            Updated order with final status.
        """
        if self._dry_run:
            # Simulate immediate fill in dry-run mode
            order.filled_size = order.requested_size
            order.status = DomainOrderStatus.FILLED
            order.updated_at = datetime.now(timezone.utc)

            await self._emit_order_event("order.filled", order)
            return order

        # Wait briefly for immediate fill
        await asyncio.sleep(min(timeout, 2.0))

        # Check order status from CLOB
        try:
            open_orders = await self._clob.get_open_orders()
            order_still_open = any(
                o.get("id") == order.order_id or o.get("client_order_id") == order.client_order_id
                for o in open_orders
            )

            if order_still_open:
                # FOK not filled - cancel and mark as expired
                await self._clob.cancel_order(order.order_id)
                order.status = DomainOrderStatus.EXPIRED
                order.updated_at = datetime.now(timezone.utc)

                await self._emit_order_event("order.expired", order)
                self._log.info("fok_order_expired", order_id=order.order_id)
            else:
                # Order is no longer open - assume filled
                order.filled_size = order.requested_size
                order.status = DomainOrderStatus.FILLED
                order.updated_at = datetime.now(timezone.utc)

                await self._emit_order_event("order.filled", order)

        except Exception as e:
            self._log.error("fok_status_check_failed", order_id=order.order_id, error=str(e))
            order.status = DomainOrderStatus.REJECTED
            order.updated_at = datetime.now(timezone.utc)

            await self._emit_order_event("order.rejected", order, error=str(e))

        return order

    async def _handle_gtc_order(
        self,
        order: Order,
        timeout: float,
    ) -> Order:
        """Handle Good-Til-Cancelled order execution.

        GTC orders remain on the book until filled or explicitly cancelled.
        This method waits for the order to fill (with polling) or times out.

        Args:
            order: The submitted order.
            timeout: Maximum time to wait for fill.

        Returns:
            Updated order with final status.
        """
        if self._dry_run:
            # Simulate fill in dry-run mode
            await asyncio.sleep(0.05)  # Small delay to simulate latency
            order.filled_size = order.requested_size
            order.status = DomainOrderStatus.FILLED
            order.updated_at = datetime.now(timezone.utc)

            await self._emit_order_event("order.filled", order)
            return order

        start_time = time.time()
        poll_interval = 0.5  # Poll every 500ms

        while time.time() - start_time < timeout:
            try:
                open_orders = await self._clob.get_open_orders()
                order_found = None

                for o in open_orders:
                    oid = o.get("id") if isinstance(o, dict) else getattr(o, "id", None)
                    if oid == order.order_id:
                        order_found = o
                        break

                if order_found is None:
                    # Order no longer in open orders - assume filled
                    order.filled_size = order.requested_size
                    order.status = DomainOrderStatus.FILLED
                    order.updated_at = datetime.now(timezone.utc)

                    await self._emit_order_event("order.filled", order)
                    return order

                # Check for partial fills
                if isinstance(order_found, dict):
                    filled_size = Decimal(str(order_found.get("size_matched", 0) or 0))
                else:
                    filled_size = Decimal(str(getattr(order_found, "size_matched", 0) or 0))

                if filled_size > order.filled_size:
                    order.filled_size = filled_size
                    order.status = DomainOrderStatus.PARTIALLY_FILLED
                    order.updated_at = datetime.now(timezone.utc)

                    await self._emit_order_event("order.partially_filled", order)

            except Exception as e:
                self._log.warning("gtc_poll_error", order_id=order.order_id, error=str(e))

            await asyncio.sleep(poll_interval)

        # Timeout reached - order still open, mark as open/partially filled
        if order.filled_size == Decimal("0"):
            order.status = DomainOrderStatus.OPEN
        else:
            order.status = DomainOrderStatus.PARTIALLY_FILLED

        order.updated_at = datetime.now(timezone.utc)

        self._log.info(
            "gtc_order_timeout",
            order_id=order.order_id,
            filled_size=str(order.filled_size),
            status=order.status.value,
        )

        return order

    async def _emit_order_event(
        self,
        event_type: str,
        order: Order,
        error: Optional[str] = None,
    ) -> None:
        """Emit an order lifecycle event.

        Args:
            event_type: Event channel (e.g., "order.pending", "order.filled").
            order: The order being processed.
            error: Optional error message for rejection events.
        """
        event_data = {
            "order_id": order.order_id,
            "client_order_id": order.client_order_id,
            "market_id": order.market_id,
            "token_id": order.token_id,
            "side": order.side.value,
            "outcome": order.outcome,
            "order_type": order.order_type.value,
            "status": order.status.value,
            "requested_size": str(order.requested_size),
            "filled_size": str(order.filled_size),
            "price": str(order.price),
            "timestamp": order.updated_at.isoformat(),
        }

        if error:
            event_data["error"] = error

        await self._event_bus.publish(event_type, event_data)

        # Track open orders
        if event_type == "order.submitted" or order.status in (
            DomainOrderStatus.SUBMITTED,
            DomainOrderStatus.OPEN,
            DomainOrderStatus.PARTIALLY_FILLED,
        ):
            self._open_orders[order.order_id] = order

        # Remove from tracking on terminal states
        if order.status in (
            DomainOrderStatus.FILLED,
            DomainOrderStatus.CANCELLED,
            DomainOrderStatus.REJECTED,
            DomainOrderStatus.EXPIRED,
        ):
            self._open_orders.pop(order.order_id, None)

        self._log.debug(
            "order_event_emitted",
            event_type=event_type,
            order_id=order.order_id,
            status=order.status.value,
        )

    def _create_fills_from_order(self, order: Order) -> list[Fill]:
        """Create Fill objects from an order's filled size.

        In production this would come from exchange trade data.
        For now we create a single synthetic fill.

        Args:
            order: The order with fill information.

        Returns:
            List of Fill objects.
        """
        if order.filled_size == Decimal("0"):
            return []

        fill = Fill(
            fill_id=f"fill-{uuid.uuid4().hex[:8]}",
            order_id=order.order_id,
            market_id=order.market_id,
            token_id=order.token_id,
            side=order.side,
            outcome=order.outcome,
            size=order.filled_size,
            price=order.price,
            fee=Decimal("0"),  # Fees would come from exchange
            timestamp=order.updated_at,
        )

        return [fill]

    # =========================================================================
    # Dual-Leg Arbitrage Execution
    # =========================================================================

    async def execute_dual_leg(
        self,
        yes_order: OrderRequest,
        no_order: OrderRequest,
        timeout: float = 30.0,
    ) -> DualLegResult:
        """Execute a dual-leg arbitrage order (YES + NO) concurrently.

        Both legs are executed in parallel. If one leg fails while the other
        succeeds, the successful leg is unwound to avoid dangling positions.

        Args:
            yes_order: Order request for the YES side.
            no_order: Order request for the NO side.
            timeout: Maximum time to wait for order completion (seconds).

        Returns:
            DualLegResult with both order results.

        Event channels published:
            - order.dual_leg.started: Dual-leg execution started
            - order.dual_leg.completed: Both legs completed successfully
            - order.dual_leg.partial: One leg filled, attempting unwind
            - order.dual_leg.unwound: Dangling position unwound
            - order.dual_leg.failed: Both legs failed or unwind failed
        """
        start_time = time.time()

        self._log.info(
            "execute_dual_leg_start",
            yes_market_id=yes_order.market_id,
            no_market_id=no_order.market_id,
            yes_size=str(yes_order.size),
            no_size=str(no_order.size),
            yes_price=str(yes_order.price),
            no_price=str(no_order.price),
            dry_run=self._dry_run,
        )

        # Emit start event
        await self._event_bus.publish("order.dual_leg.started", {
            "yes_client_order_id": yes_order.client_order_id,
            "no_client_order_id": no_order.client_order_id,
            "yes_market_id": yes_order.market_id,
            "no_market_id": no_order.market_id,
            "yes_size": str(yes_order.size),
            "no_size": str(no_order.size),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        try:
            # Execute both legs concurrently
            yes_result, no_result = await asyncio.gather(
                self.execute_order(yes_order, timeout=timeout),
                self.execute_order(no_order, timeout=timeout),
                return_exceptions=True,
            )

            # Handle exceptions from gather
            if isinstance(yes_result, Exception):
                yes_result = self._create_failed_order_result(yes_order, yes_result, start_time)
            if isinstance(no_result, Exception):
                no_result = self._create_failed_order_result(no_order, no_result, start_time)

            # Calculate latency
            latency_ms = (time.time() - start_time) * 1000

            # Check outcomes
            yes_success = yes_result.success and yes_result.order.status == DomainOrderStatus.FILLED
            no_success = no_result.success and no_result.order.status == DomainOrderStatus.FILLED

            if yes_success and no_success:
                # Both legs filled - success!
                self._log.info(
                    "execute_dual_leg_success",
                    yes_filled=str(yes_result.order.filled_size),
                    no_filled=str(no_result.order.filled_size),
                    latency_ms=latency_ms,
                )

                await self._event_bus.publish("order.dual_leg.completed", {
                    "yes_order_id": yes_result.order.order_id,
                    "no_order_id": no_result.order.order_id,
                    "yes_filled": str(yes_result.order.filled_size),
                    "no_filled": str(no_result.order.filled_size),
                    "total_cost": str(yes_result.total_cost + no_result.total_cost),
                    "latency_ms": latency_ms,
                })

                return DualLegResult(
                    success=True,
                    yes_result=yes_result,
                    no_result=no_result,
                    error_message=None,
                    total_latency_ms=latency_ms,
                )

            elif yes_success and not no_success:
                # YES filled, NO failed - unwind YES
                self._log.warning(
                    "execute_dual_leg_partial_yes",
                    yes_filled=str(yes_result.order.filled_size),
                    no_error=no_result.error_message,
                )
                return await self._handle_partial_dual_leg(
                    filled_result=yes_result,
                    failed_result=no_result,
                    filled_side="YES",
                    start_time=start_time,
                    timeout=timeout,
                )

            elif no_success and not yes_success:
                # NO filled, YES failed - unwind NO
                self._log.warning(
                    "execute_dual_leg_partial_no",
                    no_filled=str(no_result.order.filled_size),
                    yes_error=yes_result.error_message,
                )
                return await self._handle_partial_dual_leg(
                    filled_result=no_result,
                    failed_result=yes_result,
                    filled_side="NO",
                    start_time=start_time,
                    timeout=timeout,
                )

            else:
                # Both failed
                self._log.error(
                    "execute_dual_leg_both_failed",
                    yes_error=yes_result.error_message,
                    no_error=no_result.error_message,
                )

                await self._event_bus.publish("order.dual_leg.failed", {
                    "yes_error": yes_result.error_message,
                    "no_error": no_result.error_message,
                    "latency_ms": latency_ms,
                })

                return DualLegResult(
                    success=False,
                    yes_result=yes_result,
                    no_result=no_result,
                    error_message=f"Both legs failed: YES={yes_result.error_message}, NO={no_result.error_message}",
                    total_latency_ms=latency_ms,
                )

        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            self._log.error("execute_dual_leg_error", error=str(e))

            await self._event_bus.publish("order.dual_leg.failed", {
                "error": str(e),
                "latency_ms": latency_ms,
            })

            return DualLegResult(
                success=False,
                yes_result=None,
                no_result=None,
                error_message=str(e),
                total_latency_ms=latency_ms,
            )

    async def _handle_partial_dual_leg(
        self,
        filled_result: DomainOrderResult,
        failed_result: DomainOrderResult,
        filled_side: str,
        start_time: float,
        timeout: float,
    ) -> DualLegResult:
        """Handle a partial fill by unwinding the successful leg.

        When one leg of an arbitrage fills but the other fails, we must
        unwind the filled position to avoid holding a dangling (unhedged)
        position.

        Args:
            filled_result: The successful order result.
            failed_result: The failed order result.
            filled_side: "YES" or "NO" indicating which side filled.
            start_time: Execution start time for latency calculation.
            timeout: Timeout for unwind order.

        Returns:
            DualLegResult indicating failure with unwind details.
        """
        await self._event_bus.publish("order.dual_leg.partial", {
            "filled_side": filled_side,
            "filled_order_id": filled_result.order.order_id,
            "filled_size": str(filled_result.order.filled_size),
            "failed_side": "NO" if filled_side == "YES" else "YES",
            "failed_error": failed_result.error_message,
        })

        # Create unwind order - sell what we bought
        unwind_order = OrderRequest(
            market_id=filled_result.order.market_id,
            token_id=filled_result.order.token_id,
            side=DomainOrderSide.SELL,  # Sell to unwind a buy
            outcome=filled_result.order.outcome,
            size=filled_result.order.filled_size,
            price=filled_result.order.price,  # Try to get same price
            order_type=filled_result.order.order_type,
        )

        self._log.info(
            "unwinding_partial_fill",
            side=filled_side,
            size=str(unwind_order.size),
            price=str(unwind_order.price),
        )

        try:
            unwind_result = await self.execute_order(unwind_order, timeout=timeout)

            latency_ms = (time.time() - start_time) * 1000

            if unwind_result.success and unwind_result.order.status == DomainOrderStatus.FILLED:
                self._log.info(
                    "unwind_successful",
                    unwind_order_id=unwind_result.order.order_id,
                    unwind_filled=str(unwind_result.order.filled_size),
                )

                await self._event_bus.publish("order.dual_leg.unwound", {
                    "filled_side": filled_side,
                    "unwind_order_id": unwind_result.order.order_id,
                    "unwind_filled": str(unwind_result.order.filled_size),
                    "original_order_id": filled_result.order.order_id,
                    "latency_ms": latency_ms,
                })

                # Return failure with unwind info
                return DualLegResult(
                    success=False,
                    yes_result=filled_result if filled_side == "YES" else failed_result,
                    no_result=failed_result if filled_side == "YES" else filled_result,
                    error_message=f"{filled_side} filled but other leg failed; position unwound successfully",
                    total_latency_ms=latency_ms,
                )
            else:
                # Unwind failed - dangling position!
                self._log.error(
                    "unwind_failed",
                    unwind_error=unwind_result.error_message,
                    dangling_side=filled_side,
                    dangling_size=str(filled_result.order.filled_size),
                )

                await self._event_bus.publish("order.dual_leg.failed", {
                    "error": "Unwind failed - DANGLING POSITION",
                    "dangling_side": filled_side,
                    "dangling_size": str(filled_result.order.filled_size),
                    "dangling_order_id": filled_result.order.order_id,
                    "unwind_error": unwind_result.error_message,
                    "latency_ms": latency_ms,
                })

                return DualLegResult(
                    success=False,
                    yes_result=filled_result if filled_side == "YES" else failed_result,
                    no_result=failed_result if filled_side == "YES" else filled_result,
                    error_message=f"CRITICAL: {filled_side} filled but unwind failed - dangling position of {filled_result.order.filled_size} shares",
                    total_latency_ms=latency_ms,
                )

        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            self._log.error("unwind_exception", error=str(e))

            await self._event_bus.publish("order.dual_leg.failed", {
                "error": f"Unwind exception - DANGLING POSITION: {e}",
                "dangling_side": filled_side,
                "dangling_size": str(filled_result.order.filled_size),
                "latency_ms": latency_ms,
            })

            return DualLegResult(
                success=False,
                yes_result=filled_result if filled_side == "YES" else failed_result,
                no_result=failed_result if filled_side == "YES" else filled_result,
                error_message=f"CRITICAL: {filled_side} filled but unwind exception - dangling position: {e}",
                total_latency_ms=latency_ms,
            )

    def _create_failed_order_result(
        self,
        order_request: OrderRequest,
        exception: Exception,
        start_time: float,
    ) -> DomainOrderResult:
        """Create a failed OrderResult from an exception.

        Args:
            order_request: The original order request.
            exception: The exception that occurred.
            start_time: When the order execution started.

        Returns:
            A DomainOrderResult representing the failure.
        """
        order = Order(
            order_id=f"ord-failed-{uuid.uuid4().hex[:8]}",
            market_id=order_request.market_id,
            token_id=order_request.token_id,
            side=order_request.side,
            outcome=order_request.outcome,
            requested_size=order_request.size,
            filled_size=Decimal("0"),
            price=order_request.price,
            status=DomainOrderStatus.REJECTED,
            order_type=order_request.order_type,
            client_order_id=order_request.client_order_id,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        return DomainOrderResult(
            success=False,
            order=order,
            fills=[],
            error_message=str(exception),
            latency_ms=(time.time() - start_time) * 1000,
        )

    async def _execute_dry_run(
        self,
        signal: ExecutionSignal,
        trade_id: str,
        position_id: str,
    ) -> ExecutionResult:
        """Simulate execution in dry-run mode."""
        await asyncio.sleep(0.1)  # Simulate latency

        # Simulate fill at expected prices
        yes_filled = signal.target_size_usd / 2 / signal.yes_price
        no_filled = signal.target_size_usd / 2 / signal.no_price
        total_cost = signal.target_size_usd
        guaranteed_pnl = min(yes_filled, no_filled) - total_cost

        self._log.info(
            "dry_run_execution",
            trade_id=trade_id,
            yes_filled=str(yes_filled),
            no_filled=str(no_filled),
            guaranteed_pnl=str(guaranteed_pnl),
        )

        # Publish position opened
        await self._event_bus.publish("position.opened", {
            "position_id": position_id,
            "trade_id": trade_id,
            "market_id": signal.market_id,
            "yes_shares": str(yes_filled),
            "no_shares": str(no_filled),
            "cost_basis": str(total_cost),
        })

        return ExecutionResult(
            success=True,
            signal_id=signal.signal_id,
            trade_id=trade_id,
            position_id=position_id,
            yes_filled=yes_filled,
            no_filled=no_filled,
            total_cost=total_cost,
            guaranteed_pnl=guaranteed_pnl,
        )

    async def _execute_dual_leg(
        self,
        signal: ExecutionSignal,
        trade_id: str,
        position_id: str,
    ) -> ExecutionResult:
        """Execute a dual-leg arbitrage order."""
        # Publish order submitted
        await self._event_bus.publish("order.submitted", {
            "trade_id": trade_id,
            "signal_id": signal.signal_id,
            "type": "dual_leg",
        })

        try:
            result = await self._clob.execute_dual_leg_order(
                yes_token_id=signal.yes_token_id,
                no_token_id=signal.no_token_id,
                amount_usd=signal.target_size_usd,
                yes_price=signal.yes_price,
                no_price=signal.no_price,
            )

            # Handle partial fills
            if result.has_partial_fill and self._rebalance_enabled:
                self._log.warning("partial_fill_detected", trade_id=trade_id)
                await self._handle_partial_fill(result, signal)

            # Publish fills
            await self._event_bus.publish("order.filled", {
                "trade_id": trade_id,
                "yes_order_id": result.yes_result.order_id,
                "no_order_id": result.no_result.order_id,
                "yes_filled": str(result.yes_result.filled_size),
                "no_filled": str(result.no_result.filled_size),
                "total_cost": str(result.total_cost),
            })

            # Create position if both filled
            if result.both_filled:
                await self._event_bus.publish("position.opened", {
                    "position_id": position_id,
                    "trade_id": trade_id,
                    "market_id": signal.market_id,
                    "yes_shares": str(result.yes_result.filled_size),
                    "no_shares": str(result.no_result.filled_size),
                    "cost_basis": str(result.total_cost),
                })

            return ExecutionResult(
                success=result.both_filled,
                signal_id=signal.signal_id,
                trade_id=trade_id,
                position_id=position_id if result.both_filled else None,
                yes_filled=result.yes_result.filled_size,
                no_filled=result.no_result.filled_size,
                total_cost=result.total_cost,
                guaranteed_pnl=result.guaranteed_pnl,
                execution_time_ms=result.execution_time_ms,
            )

        except InsufficientLiquidityError as e:
            self._log.warning("insufficient_liquidity", signal_id=signal.signal_id, error=str(e))
            await self._event_bus.publish("order.rejected", {
                "trade_id": trade_id,
                "signal_id": signal.signal_id,
                "reason": str(e),
            })
            return ExecutionResult(
                success=False,
                signal_id=signal.signal_id,
                error=str(e),
            )

    async def _execute_single_leg(
        self,
        signal: ExecutionSignal,
        trade_id: str,
        position_id: str,
    ) -> ExecutionResult:
        """Execute a single-leg directional order."""
        # Determine which token to trade
        if signal.signal_type == SignalType.BUY_YES:
            token_id = signal.yes_token_id
            price = signal.yes_price
        else:
            token_id = signal.no_token_id
            price = signal.no_price

        await self._event_bus.publish("order.submitted", {
            "trade_id": trade_id,
            "signal_id": signal.signal_id,
            "type": "single_leg",
            "token_id": token_id,
        })

        try:
            result = await self._clob.execute_order(
                token_id=token_id,
                side=OrderSide.BUY,
                amount_usd=signal.target_size_usd,
                price=price,
            )

            if result.status in (OrderStatus.FILLED, OrderStatus.MATCHED):
                await self._event_bus.publish("order.filled", {
                    "trade_id": trade_id,
                    "order_id": result.order_id,
                    "filled_size": str(result.filled_size),
                    "filled_cost": str(result.filled_cost),
                })

                return ExecutionResult(
                    success=True,
                    signal_id=signal.signal_id,
                    trade_id=trade_id,
                    yes_filled=result.filled_size if signal.signal_type == SignalType.BUY_YES else Decimal("0"),
                    no_filled=result.filled_size if signal.signal_type == SignalType.BUY_NO else Decimal("0"),
                    total_cost=result.filled_cost,
                )
            else:
                return ExecutionResult(
                    success=False,
                    signal_id=signal.signal_id,
                    error=f"Order status: {result.status.value}",
                )

        except Exception as e:
            await self._event_bus.publish("order.rejected", {
                "trade_id": trade_id,
                "signal_id": signal.signal_id,
                "reason": str(e),
            })
            return ExecutionResult(
                success=False,
                signal_id=signal.signal_id,
                error=str(e),
            )

    async def _handle_partial_fill(
        self,
        result: DualLegOrderResult,
        signal: ExecutionSignal,
    ) -> None:
        """Handle partial fill by attempting rebalance."""
        if result.yes_result.filled_size > 0 and result.no_result.filled_size == 0:
            # YES filled, NO didn't
            rebalance = await self._clob.rebalance_partial_fill(
                filled_token_id=signal.yes_token_id,
                unfilled_token_id=signal.no_token_id,
                filled_shares=result.yes_result.filled_size,
                filled_price=result.yes_result.requested_price,
                unfilled_price=result.no_result.requested_price,
            )
            self._log.info("rebalance_result", action=rebalance.get("action"))

        elif result.no_result.filled_size > 0 and result.yes_result.filled_size == 0:
            # NO filled, YES didn't
            rebalance = await self._clob.rebalance_partial_fill(
                filled_token_id=signal.no_token_id,
                unfilled_token_id=signal.yes_token_id,
                filled_shares=result.no_result.filled_size,
                filled_price=result.no_result.requested_price,
                unfilled_price=result.yes_result.requested_price,
            )
            self._log.info("rebalance_result", action=rebalance.get("action"))

    async def _on_approved_signal(self, data: dict) -> None:
        """Handle approved signal from RiskManager by queueing for execution."""
        signal_id = data.get("signal_id", str(uuid.uuid4()))

        # Get priority from signal data, default to MEDIUM
        priority_str = data.get("priority", "medium")
        try:
            priority = SignalPriority(priority_str.lower())
        except (ValueError, AttributeError):
            priority = SignalPriority.MEDIUM

        # Queue the signal for execution
        await self.queue_signal(signal_id, data, priority)

    async def queue_signal(
        self,
        signal_id: str,
        signal_data: dict[str, Any],
        priority: SignalPriority = SignalPriority.MEDIUM,
    ) -> bool:
        """Add a signal to the execution queue.

        Args:
            signal_id: Unique signal identifier.
            signal_data: Signal data dictionary.
            priority: Execution priority.

        Returns:
            True if queued successfully, False if queue is full.
        """
        if signal_id in self._queue_items:
            self._log.warning("signal_already_queued", signal_id=signal_id)
            return False

        queued_signal = QueuedSignal(
            signal_id=signal_id,
            signal_data=signal_data,
            priority=priority,
        )

        try:
            self._queue.put_nowait(queued_signal)
            self._queue_items[signal_id] = queued_signal
            self._total_queued += 1

            self._log.info(
                "signal_queued",
                signal_id=signal_id,
                priority=priority.value,
                queue_size=self._queue.qsize(),
            )

            await self._event_bus.publish("execution.queue.added", {
                "signal_id": signal_id,
                "priority": priority.value,
                "queue_size": self._queue.qsize(),
                "queued_at": queued_signal.queued_at.isoformat(),
            })

            return True

        except asyncio.QueueFull:
            self._log.error(
                "queue_full",
                signal_id=signal_id,
                max_size=self._max_queue_size,
            )
            await self._event_bus.publish("execution.queue.rejected", {
                "signal_id": signal_id,
                "reason": "queue_full",
            })
            return False

    async def _process_queue(self) -> None:
        """Background task that processes the execution queue.

        Continuously pulls signals from the queue and executes them,
        respecting the concurrent execution limit.
        """
        self._log.info("queue_processor_started")

        while self._should_run:
            try:
                # Wait for a signal from the queue (with timeout for checking should_run)
                try:
                    queued_signal = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    # Check for expired signals periodically
                    await self._cleanup_expired_signals()
                    continue

                # Check if signal has expired
                if self._is_signal_expired(queued_signal):
                    self._log.warning(
                        "signal_expired_in_queue",
                        signal_id=queued_signal.signal_id,
                        queued_at=queued_signal.queued_at.isoformat(),
                    )
                    queued_signal.status = QueuedSignalStatus.EXPIRED
                    self._total_expired += 1
                    self._queue_items.pop(queued_signal.signal_id, None)
                    self._queue.task_done()
                    continue

                # Acquire semaphore to limit concurrent executions
                await self._execution_semaphore.acquire()

                # Start execution task
                task = asyncio.create_task(
                    self._execute_queued_signal(queued_signal)
                )
                self._active_executions[queued_signal.signal_id] = task

                # Add callback to release semaphore when done
                task.add_done_callback(
                    lambda t, sig_id=queued_signal.signal_id: self._on_execution_complete(sig_id)
                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log.error("queue_processor_error", error=str(e))
                await asyncio.sleep(0.1)  # Brief pause on error

        self._log.info("queue_processor_stopped")

    def _is_signal_expired(self, queued_signal: QueuedSignal) -> bool:
        """Check if a queued signal has expired."""
        age_seconds = (datetime.now(timezone.utc) - queued_signal.queued_at).total_seconds()
        return age_seconds > self._queue_timeout

    async def _cleanup_expired_signals(self) -> None:
        """Remove expired signals from tracking."""
        expired_ids = []
        for signal_id, queued_signal in self._queue_items.items():
            if queued_signal.status == QueuedSignalStatus.PENDING and self._is_signal_expired(queued_signal):
                expired_ids.append(signal_id)

        for signal_id in expired_ids:
            self._queue_items.pop(signal_id, None)
            self._total_expired += 1
            self._log.debug("cleaned_expired_signal", signal_id=signal_id)

    def _on_execution_complete(self, signal_id: str) -> None:
        """Callback when an execution task completes."""
        self._active_executions.pop(signal_id, None)
        self._queue_items.pop(signal_id, None)
        if self._execution_semaphore:
            self._execution_semaphore.release()
        try:
            self._queue.task_done()
        except ValueError:
            pass  # task_done called too many times

    async def _execute_queued_signal(self, queued_signal: QueuedSignal) -> ExecutionResult:
        """Execute a signal from the queue."""
        queued_signal.status = QueuedSignalStatus.EXECUTING
        queued_signal.started_at = datetime.now(timezone.utc)

        self._log.info(
            "executing_queued_signal",
            signal_id=queued_signal.signal_id,
            priority=queued_signal.priority.value,
            wait_time_ms=(queued_signal.started_at - queued_signal.queued_at).total_seconds() * 1000,
        )

        await self._event_bus.publish("execution.queue.started", {
            "signal_id": queued_signal.signal_id,
            "priority": queued_signal.priority.value,
            "active_executions": len(self._active_executions),
        })

        try:
            # Reconstruct ApprovedSignal from event data
            data = queued_signal.signal_data
            signal = self._build_approved_signal(data)

            result = await self.execute(signal)

            if result.success:
                queued_signal.status = QueuedSignalStatus.COMPLETED
                self._total_executed += 1
            else:
                queued_signal.status = QueuedSignalStatus.FAILED
                queued_signal.error = result.error
                self._total_failed += 1

            queued_signal.completed_at = datetime.now(timezone.utc)
            return result

        except Exception as e:
            queued_signal.status = QueuedSignalStatus.FAILED
            queued_signal.error = str(e)
            queued_signal.completed_at = datetime.now(timezone.utc)
            self._total_failed += 1

            self._log.error(
                "queued_execution_failed",
                signal_id=queued_signal.signal_id,
                error=str(e),
            )

            return ExecutionResult(
                success=False,
                signal_id=queued_signal.signal_id,
                error=str(e),
            )

    def _build_approved_signal(self, data: dict[str, Any]) -> "ExecutionSignal":
        """Build an ExecutionSignal from event data."""
        return ExecutionSignal(
            signal_id=data["signal_id"],
            original_signal_id=data.get("original_signal_id", data["signal_id"]),
            market_id=data["market_id"],
            signal_type=SignalType(data["signal_type"]),
            target_size_usd=Decimal(str(data["target_size_usd"])),
            yes_price=Decimal(str(data.get("yes_price", 0))),
            no_price=Decimal(str(data.get("no_price", 0))),
            yes_token_id=data.get("yes_token_id", ""),
            no_token_id=data.get("no_token_id", ""),
            approved_at=datetime.now(timezone.utc),
        )

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order.

        Cancels an order tracked by this execution engine. Updates the order
        status to CANCELLED and emits an order.cancelled event.

        Args:
            order_id: The order ID to cancel.

        Returns:
            True if cancelled successfully, False if cancel failed or order not found.

        Event channels published:
            - order.cancelled: Order was successfully cancelled
            - order.cancel_failed: Order cancellation failed (with error details)
        """
        self._log.info("cancel_order_requested", order_id=order_id)

        # Check if order is tracked
        order = self._open_orders.get(order_id)

        if self._dry_run:
            # In dry-run mode, simulate successful cancellation
            self._log.info("dry_run_cancel", order_id=order_id)

            if order:
                order.status = DomainOrderStatus.CANCELLED
                order.updated_at = datetime.now(timezone.utc)
                await self._emit_order_event("order.cancelled", order)
            else:
                # Create synthetic order for event emission
                await self._event_bus.publish("order.cancelled", {
                    "order_id": order_id,
                    "status": "cancelled",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "reason": "dry_run_cancel",
                })

            return True

        # Attempt to cancel on exchange
        try:
            success = await self._clob.cancel_order(order_id)

            if success:
                self._log.info("order_cancelled", order_id=order_id)

                if order:
                    order.status = DomainOrderStatus.CANCELLED
                    order.updated_at = datetime.now(timezone.utc)
                    await self._emit_order_event("order.cancelled", order)
                else:
                    # Order not tracked locally but cancelled on exchange
                    await self._event_bus.publish("order.cancelled", {
                        "order_id": order_id,
                        "status": "cancelled",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })

                return True
            else:
                self._log.warning("cancel_failed", order_id=order_id, reason="exchange_rejected")
                await self._event_bus.publish("order.cancel_failed", {
                    "order_id": order_id,
                    "reason": "exchange_rejected",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                return False

        except Exception as e:
            self._log.error("cancel_error", order_id=order_id, error=str(e))
            await self._event_bus.publish("order.cancel_failed", {
                "order_id": order_id,
                "reason": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            return False

    async def cancel_all(self) -> dict[str, Any]:
        """Cancel all open orders.

        Attempts to cancel all orders tracked by this execution engine.
        Returns a summary of the cancellation results.

        Returns:
            Dictionary with:
                - total: Total number of orders attempted
                - cancelled: Number of orders successfully cancelled
                - failed: Number of orders that failed to cancel
                - errors: List of error details for failed cancellations

        Event channels published:
            - order.cancelled: For each successfully cancelled order
            - order.cancel_failed: For each failed cancellation
            - order.cancel_all.completed: Summary of bulk cancellation
        """
        self._log.info(
            "cancel_all_requested",
            open_orders_count=len(self._open_orders),
        )

        results = {
            "total": 0,
            "cancelled": 0,
            "failed": 0,
            "errors": [],
        }

        if not self._open_orders:
            self._log.info("cancel_all_no_orders")
            await self._event_bus.publish("order.cancel_all.completed", results)
            return results

        # Get a snapshot of order IDs to cancel
        order_ids_to_cancel = list(self._open_orders.keys())
        results["total"] = len(order_ids_to_cancel)

        # Cancel each order
        for order_id in order_ids_to_cancel:
            try:
                success = await self.cancel_order(order_id)
                if success:
                    results["cancelled"] += 1
                else:
                    results["failed"] += 1
                    results["errors"].append({
                        "order_id": order_id,
                        "error": "cancel_rejected",
                    })
            except Exception as e:
                results["failed"] += 1
                results["errors"].append({
                    "order_id": order_id,
                    "error": str(e),
                })
                self._log.error("cancel_all_error", order_id=order_id, error=str(e))

        self._log.info(
            "cancel_all_completed",
            total=results["total"],
            cancelled=results["cancelled"],
            failed=results["failed"],
        )

        await self._event_bus.publish("order.cancel_all.completed", results)
        return results

    def get_open_orders(self) -> list[Order]:
        """Get all open orders tracked by this execution engine.

        Returns:
            List of Order objects that are currently open.
        """
        return list(self._open_orders.values())

    def get_open_order(self, order_id: str) -> Optional[Order]:
        """Get a specific open order by ID.

        Args:
            order_id: The order ID to look up.

        Returns:
            The Order if found and open, None otherwise.
        """
        return self._open_orders.get(order_id)

    def get_open_order_count(self) -> int:
        """Get the count of currently open orders.

        Returns:
            Number of open orders.
        """
        return len(self._open_orders)

    # =========================================================================
    # Queue Management Public Methods
    # =========================================================================

    def get_queue_size(self) -> int:
        """Get the current number of signals in the queue."""
        return self._queue.qsize()

    def get_active_execution_count(self) -> int:
        """Get the number of currently executing signals."""
        return len(self._active_executions)

    def get_queue_stats(self) -> dict[str, Any]:
        """Get comprehensive queue statistics.

        Returns:
            Dictionary with queue metrics.
        """
        return {
            "queue_size": self._queue.qsize(),
            "active_executions": len(self._active_executions),
            "max_concurrent": self._max_concurrent,
            "max_queue_size": self._max_queue_size,
            "queue_timeout_seconds": self._queue_timeout,
            "total_queued": self._total_queued,
            "total_executed": self._total_executed,
            "total_failed": self._total_failed,
            "total_expired": self._total_expired,
        }

    def get_queued_signals(self) -> list[dict[str, Any]]:
        """Get information about all queued signals.

        Returns:
            List of dictionaries with signal info.
        """
        return [
            {
                "signal_id": qs.signal_id,
                "priority": qs.priority.value,
                "status": qs.status.value,
                "queued_at": qs.queued_at.isoformat(),
                "age_seconds": (datetime.now(timezone.utc) - qs.queued_at).total_seconds(),
            }
            for qs in self._queue_items.values()
        ]

    async def cancel_queued_signal(self, signal_id: str) -> bool:
        """Cancel a queued signal before execution.

        Args:
            signal_id: The signal ID to cancel.

        Returns:
            True if cancelled, False if not found or already executing.
        """
        queued_signal = self._queue_items.get(signal_id)
        if not queued_signal:
            return False

        if queued_signal.status != QueuedSignalStatus.PENDING:
            self._log.warning(
                "cannot_cancel_signal",
                signal_id=signal_id,
                status=queued_signal.status.value,
            )
            return False

        queued_signal.status = QueuedSignalStatus.CANCELLED
        self._queue_items.pop(signal_id, None)

        self._log.info("signal_cancelled", signal_id=signal_id)
        await self._event_bus.publish("execution.queue.cancelled", {
            "signal_id": signal_id,
        })

        return True

    def is_queue_full(self) -> bool:
        """Check if the queue is at capacity."""
        return self._queue.qsize() >= self._max_queue_size

    @property
    def last_latency_ms(self) -> Optional[float]:
        """Get the latency of the most recent execution (for testing)."""
        # This is tracked in ExecutionResult, not stored separately
        return None
