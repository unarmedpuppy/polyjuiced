"""
Graceful shutdown handling for Mercury.

Coordinates orderly shutdown of all components when SIGTERM/SIGINT received:
1. Stop accepting new signals
2. Wait for in-flight orders to complete (with timeout)
3. Close WebSocket connections
4. Flush metrics
5. Close database connections
6. Log shutdown progress
"""

import asyncio
import signal
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine, Optional

import structlog

log = structlog.get_logger()


class ShutdownPhase(str, Enum):
    """Phases of graceful shutdown."""

    RUNNING = "running"
    SIGNAL_RECEIVED = "signal_received"
    STOPPING_NEW_WORK = "stopping_new_work"
    DRAINING_ORDERS = "draining_orders"
    CLOSING_CONNECTIONS = "closing_connections"
    FLUSHING_DATA = "flushing_data"
    CLEANUP = "cleanup"
    COMPLETED = "completed"


@dataclass
class ShutdownProgress:
    """Tracks progress of graceful shutdown."""

    phase: ShutdownPhase = ShutdownPhase.RUNNING
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    signal_received: Optional[str] = None
    in_flight_orders: int = 0
    orders_drained: bool = False
    websocket_closed: bool = False
    metrics_flushed: bool = False
    database_closed: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def is_shutting_down(self) -> bool:
        """Check if shutdown is in progress."""
        return self.phase not in (ShutdownPhase.RUNNING, ShutdownPhase.COMPLETED)

    @property
    def duration_seconds(self) -> Optional[float]:
        """Get shutdown duration in seconds."""
        if self.started_at is None:
            return None
        end = self.completed_at or datetime.now(timezone.utc)
        return (end - self.started_at).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for logging/metrics."""
        return {
            "phase": self.phase.value,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "signal_received": self.signal_received,
            "in_flight_orders": self.in_flight_orders,
            "orders_drained": self.orders_drained,
            "websocket_closed": self.websocket_closed,
            "metrics_flushed": self.metrics_flushed,
            "database_closed": self.database_closed,
            "duration_seconds": self.duration_seconds,
            "errors": self.errors,
        }


# Type alias for shutdown callbacks
ShutdownCallback = Callable[[], Coroutine[Any, Any, None]]


class ShutdownManager:
    """Manages graceful shutdown of Mercury application.

    Coordinates shutdown sequence across all components:
    1. Stop accepting new signals (strategy engine stops generating)
    2. Drain in-flight orders (execution engine completes pending orders)
    3. Close WebSocket connections (market data service disconnects)
    4. Flush metrics (ensure final metrics are recorded)
    5. Close database connections (state store commits and closes)

    Usage:
        manager = ShutdownManager(timeout_seconds=30.0)

        # Register callbacks for each phase
        manager.on_stop_new_work(strategy_engine.stop_generating)
        manager.on_drain_orders(execution_engine.drain_orders)
        manager.on_close_connections(market_data.stop)
        manager.on_flush_data(metrics.flush)
        manager.on_cleanup(state_store.close)

        # Install signal handlers
        manager.install_signal_handlers()

        # Or trigger shutdown programmatically
        await manager.shutdown()
    """

    DEFAULT_TIMEOUT_SECONDS = 30.0
    DEFAULT_DRAIN_TIMEOUT_SECONDS = 60.0

    def __init__(
        self,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        drain_timeout_seconds: float = DEFAULT_DRAIN_TIMEOUT_SECONDS,
    ) -> None:
        """Initialize shutdown manager.

        Args:
            timeout_seconds: Total shutdown timeout (default 30s).
            drain_timeout_seconds: Timeout for draining in-flight orders (default 60s).
        """
        self._timeout = timeout_seconds
        self._drain_timeout = drain_timeout_seconds
        self._progress = ShutdownProgress()
        self._shutdown_event = asyncio.Event()
        self._log = log.bind(component="shutdown_manager")

        # Callbacks for each phase
        self._stop_new_work_callbacks: list[ShutdownCallback] = []
        self._drain_orders_callbacks: list[ShutdownCallback] = []
        self._close_connections_callbacks: list[ShutdownCallback] = []
        self._flush_data_callbacks: list[ShutdownCallback] = []
        self._cleanup_callbacks: list[ShutdownCallback] = []

        # In-flight order tracking
        self._get_in_flight_count: Optional[Callable[[], int]] = None
        self._force_cancel_orders: Optional[ShutdownCallback] = None

    @property
    def progress(self) -> ShutdownProgress:
        """Get current shutdown progress."""
        return self._progress

    @property
    def is_shutting_down(self) -> bool:
        """Check if shutdown is in progress."""
        return self._progress.is_shutting_down

    @property
    def shutdown_event(self) -> asyncio.Event:
        """Get the shutdown event for waiting."""
        return self._shutdown_event

    def install_signal_handlers(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Install SIGTERM and SIGINT handlers.

        Args:
            loop: Event loop to install handlers on (uses current if not provided).
        """
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self._log.warning("no_event_loop_for_signal_handlers")
                return

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.create_task(self._handle_signal(s)),
            )

        self._log.info("signal_handlers_installed", signals=["SIGTERM", "SIGINT"])

    def remove_signal_handlers(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Remove installed signal handlers.

        Args:
            loop: Event loop to remove handlers from.
        """
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.remove_signal_handler(sig)
            except (ValueError, RuntimeError):
                pass

    async def _handle_signal(self, sig: signal.Signals) -> None:
        """Handle shutdown signal.

        Args:
            sig: The signal received.
        """
        signal_name = sig.name if hasattr(sig, "name") else str(sig)
        self._log.info("shutdown_signal_received", signal=signal_name)

        self._progress.signal_received = signal_name
        await self.shutdown()

    def on_stop_new_work(self, callback: ShutdownCallback) -> None:
        """Register callback for stop-new-work phase.

        Called first - stop accepting new signals/work.
        """
        self._stop_new_work_callbacks.append(callback)

    def on_drain_orders(self, callback: ShutdownCallback) -> None:
        """Register callback for drain-orders phase.

        Called second - wait for in-flight orders to complete.
        """
        self._drain_orders_callbacks.append(callback)

    def on_close_connections(self, callback: ShutdownCallback) -> None:
        """Register callback for close-connections phase.

        Called third - close WebSocket and network connections.
        """
        self._close_connections_callbacks.append(callback)

    def on_flush_data(self, callback: ShutdownCallback) -> None:
        """Register callback for flush-data phase.

        Called fourth - flush metrics, logs, and pending data.
        """
        self._flush_data_callbacks.append(callback)

    def on_cleanup(self, callback: ShutdownCallback) -> None:
        """Register callback for cleanup phase.

        Called last - close database connections and release resources.
        """
        self._cleanup_callbacks.append(callback)

    def set_in_flight_tracker(
        self,
        get_count: Callable[[], int],
        force_cancel: Optional[ShutdownCallback] = None,
    ) -> None:
        """Set functions for tracking in-flight orders.

        Args:
            get_count: Function returning count of in-flight orders.
            force_cancel: Optional async function to force-cancel remaining orders.
        """
        self._get_in_flight_count = get_count
        self._force_cancel_orders = force_cancel

    async def shutdown(self) -> None:
        """Execute graceful shutdown sequence.

        Runs through all shutdown phases in order with proper error handling.
        Times out if shutdown takes too long.
        """
        if self._progress.is_shutting_down:
            self._log.warning("shutdown_already_in_progress")
            return

        self._progress.started_at = datetime.now(timezone.utc)
        self._progress.phase = ShutdownPhase.SIGNAL_RECEIVED

        self._log.info(
            "graceful_shutdown_starting",
            timeout_seconds=self._timeout,
            drain_timeout_seconds=self._drain_timeout,
        )

        try:
            # Phase 1: Stop accepting new work
            await self._run_phase(
                ShutdownPhase.STOPPING_NEW_WORK,
                self._stop_new_work_callbacks,
                "Stopping new work",
            )

            # Phase 2: Drain in-flight orders
            await self._drain_in_flight_orders()

            # Phase 3: Close connections
            await self._run_phase(
                ShutdownPhase.CLOSING_CONNECTIONS,
                self._close_connections_callbacks,
                "Closing connections",
            )

            # Phase 4: Flush data
            await self._run_phase(
                ShutdownPhase.FLUSHING_DATA,
                self._flush_data_callbacks,
                "Flushing data",
            )

            # Phase 5: Cleanup
            await self._run_phase(
                ShutdownPhase.CLEANUP,
                self._cleanup_callbacks,
                "Cleanup",
            )

        except asyncio.TimeoutError:
            self._log.error(
                "shutdown_timeout",
                phase=self._progress.phase.value,
                timeout_seconds=self._timeout,
            )
            self._progress.errors.append(f"Timeout in phase: {self._progress.phase.value}")

        except Exception as e:
            self._log.error(
                "shutdown_error",
                phase=self._progress.phase.value,
                error=str(e),
            )
            self._progress.errors.append(f"Error in {self._progress.phase.value}: {str(e)}")

        finally:
            self._progress.phase = ShutdownPhase.COMPLETED
            self._progress.completed_at = datetime.now(timezone.utc)
            self._shutdown_event.set()

            self._log.info(
                "graceful_shutdown_completed",
                duration_seconds=self._progress.duration_seconds,
                errors=len(self._progress.errors),
                progress=self._progress.to_dict(),
            )

    async def _run_phase(
        self,
        phase: ShutdownPhase,
        callbacks: list[ShutdownCallback],
        description: str,
    ) -> None:
        """Run a shutdown phase with all registered callbacks.

        Args:
            phase: The shutdown phase.
            callbacks: List of async callbacks to run.
            description: Human-readable phase description.
        """
        self._progress.phase = phase
        self._log.info(
            "shutdown_phase_starting",
            phase=phase.value,
            description=description,
            callback_count=len(callbacks),
        )

        for i, callback in enumerate(callbacks):
            callback_name = getattr(callback, "__name__", f"callback_{i}")
            try:
                await asyncio.wait_for(callback(), timeout=self._timeout)
                self._log.debug(
                    "shutdown_callback_completed",
                    phase=phase.value,
                    callback=callback_name,
                )
            except asyncio.TimeoutError:
                self._log.warning(
                    "shutdown_callback_timeout",
                    phase=phase.value,
                    callback=callback_name,
                )
                self._progress.errors.append(f"Timeout: {callback_name}")
            except Exception as e:
                self._log.warning(
                    "shutdown_callback_error",
                    phase=phase.value,
                    callback=callback_name,
                    error=str(e),
                )
                self._progress.errors.append(f"Error in {callback_name}: {str(e)}")

        # Update progress flags based on phase
        if phase == ShutdownPhase.CLOSING_CONNECTIONS:
            self._progress.websocket_closed = True
        elif phase == ShutdownPhase.FLUSHING_DATA:
            self._progress.metrics_flushed = True
        elif phase == ShutdownPhase.CLEANUP:
            self._progress.database_closed = True

        self._log.info(
            "shutdown_phase_completed",
            phase=phase.value,
            description=description,
        )

    async def _drain_in_flight_orders(self) -> None:
        """Wait for in-flight orders to complete with timeout."""
        self._progress.phase = ShutdownPhase.DRAINING_ORDERS

        # Run drain callbacks first
        for callback in self._drain_orders_callbacks:
            try:
                await asyncio.wait_for(callback(), timeout=self._timeout)
            except asyncio.TimeoutError:
                self._log.warning("drain_callback_timeout")
            except Exception as e:
                self._log.warning("drain_callback_error", error=str(e))

        # Wait for in-flight orders to complete
        if self._get_in_flight_count is None:
            self._log.debug("no_in_flight_tracker_configured")
            self._progress.orders_drained = True
            return

        start_time = asyncio.get_event_loop().time()
        poll_interval = 0.5  # Check every 500ms

        while True:
            try:
                count = self._get_in_flight_count()
                self._progress.in_flight_orders = count

                if count == 0:
                    self._log.info("all_orders_drained")
                    self._progress.orders_drained = True
                    break

                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed >= self._drain_timeout:
                    self._log.warning(
                        "drain_timeout_reached",
                        remaining_orders=count,
                        timeout_seconds=self._drain_timeout,
                    )

                    # Force cancel remaining orders if possible
                    if self._force_cancel_orders:
                        self._log.info("force_cancelling_remaining_orders", count=count)
                        try:
                            await asyncio.wait_for(
                                self._force_cancel_orders(),
                                timeout=5.0,
                            )
                        except Exception as e:
                            self._log.error("force_cancel_failed", error=str(e))
                            self._progress.errors.append(f"Force cancel failed: {str(e)}")

                    self._progress.errors.append(f"Drain timeout: {count} orders remaining")
                    break

                self._log.debug(
                    "waiting_for_orders_to_drain",
                    in_flight=count,
                    elapsed_seconds=elapsed,
                )
                await asyncio.sleep(poll_interval)

            except Exception as e:
                self._log.error("drain_monitoring_error", error=str(e))
                self._progress.errors.append(f"Drain monitoring error: {str(e)}")
                break

        self._progress.orders_drained = True

    async def wait_for_shutdown(self) -> None:
        """Wait until shutdown is complete."""
        await self._shutdown_event.wait()

    def trigger_shutdown(self) -> None:
        """Trigger shutdown from synchronous code.

        Creates a task to run the shutdown coroutine.
        """
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.shutdown())
        except RuntimeError:
            self._log.warning("no_event_loop_for_shutdown_trigger")
