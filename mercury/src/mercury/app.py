"""
Mercury application lifecycle and component wiring.

This is the main orchestrator that starts and stops all services.
Handles graceful shutdown with proper ordering:
1. Stop accepting new signals
2. Wait for in-flight orders to complete
3. Close WebSocket connections
4. Flush metrics
5. Close database connections
"""
import asyncio
from pathlib import Path
from typing import Any, Callable, Optional

import structlog

from mercury.core.config import ConfigManager
from mercury.core.events import EventBus
from mercury.core.lifecycle import BaseComponent, HealthCheckResult, HealthStatus
from mercury.core.logging import setup_logging
from mercury.core.shutdown import ShutdownManager, ShutdownProgress
from mercury.services.metrics import MetricsEmitter


class MercuryApp(BaseComponent):
    """Main Mercury application.

    Orchestrates startup and shutdown of all components with graceful shutdown
    handling for SIGTERM/SIGINT signals.

    Shutdown sequence:
    1. Stop accepting new trading signals
    2. Wait for in-flight orders to complete (with configurable timeout)
    3. Close WebSocket connections
    4. Flush metrics and pending data
    5. Close database connections

    Usage:
        app = MercuryApp()
        await app.start()
        # ... app runs ...
        await app.stop()  # Or send SIGTERM/SIGINT
    """

    def __init__(
        self,
        config_path: Optional[Path] = None,
        dry_run: bool = True,
    ) -> None:
        """Initialize Mercury application.

        Args:
            config_path: Path to TOML configuration file
            dry_run: If True, don't execute real trades
        """
        super().__init__(name="MercuryApp")

        # Determine config path
        if config_path is None:
            config_path = Path("config/default.toml")
            if not config_path.exists():
                config_path = None

        # Core components
        self._config = ConfigManager(config_path)
        self._dry_run = dry_run or self._config.get_bool("mercury.dry_run", True)

        # Set up logging
        log_level = self._config.get("mercury.log_level", "INFO")
        log_json = self._config.get_bool("mercury.log_json", False)
        setup_logging(level=log_level, json_output=log_json)
        self._log = structlog.get_logger("mercury.app")

        # Initialize event bus
        redis_url = self._config.get("redis.url", "redis://localhost:6379")
        self._event_bus = EventBus(redis_url=redis_url)

        # Initialize metrics
        self._metrics = MetricsEmitter()

        # Shutdown manager with configurable timeouts
        shutdown_timeout = self._config.get_float("mercury.shutdown_timeout_seconds", 30.0)
        drain_timeout = self._config.get_float("mercury.drain_timeout_seconds", 60.0)
        self._shutdown_manager = ShutdownManager(
            timeout_seconds=shutdown_timeout,
            drain_timeout_seconds=drain_timeout,
        )

        # Registered service components (for shutdown coordination)
        self._services: dict[str, BaseComponent] = {}
        self._stop_new_work_callbacks: list[Callable[[], Any]] = []
        self._get_in_flight_count: Optional[Callable[[], int]] = None
        self._force_cancel_orders: Optional[Callable[[], Any]] = None

    @property
    def config(self) -> ConfigManager:
        """Get configuration manager."""
        return self._config

    @property
    def event_bus(self) -> EventBus:
        """Get event bus."""
        return self._event_bus

    @property
    def metrics(self) -> MetricsEmitter:
        """Get metrics emitter."""
        return self._metrics

    @property
    def dry_run(self) -> bool:
        """Check if running in dry-run mode."""
        return self._dry_run

    @property
    def shutdown_manager(self) -> ShutdownManager:
        """Get the shutdown manager."""
        return self._shutdown_manager

    @property
    def shutdown_progress(self) -> ShutdownProgress:
        """Get current shutdown progress."""
        return self._shutdown_manager.progress

    @property
    def is_shutting_down(self) -> bool:
        """Check if shutdown is in progress."""
        return self._shutdown_manager.is_shutting_down

    def register_service(self, name: str, service: BaseComponent) -> None:
        """Register a service component for lifecycle management.

        Registered services will be stopped during graceful shutdown.

        Args:
            name: Service name (e.g., "market_data", "execution_engine").
            service: The service component.
        """
        self._services[name] = service
        self._log.debug("service_registered", name=name)

    def register_stop_new_work_callback(self, callback: Callable[[], Any]) -> None:
        """Register a callback to stop accepting new work during shutdown.

        Args:
            callback: Async callable to stop new work (e.g., strategy_engine.stop).
        """
        self._stop_new_work_callbacks.append(callback)

    def register_in_flight_tracker(
        self,
        get_count: Callable[[], int],
        force_cancel: Optional[Callable[[], Any]] = None,
    ) -> None:
        """Register functions for tracking in-flight orders during shutdown.

        Args:
            get_count: Function returning count of in-flight orders.
            force_cancel: Optional async function to force-cancel remaining orders.
        """
        self._get_in_flight_count = get_count
        self._force_cancel_orders = force_cancel

    async def _do_start(self) -> None:
        """Start all components."""
        self._log.info(
            "starting_mercury",
            dry_run=self._dry_run,
            version="0.1.0",
        )

        # Connect to Redis event bus
        try:
            await self._event_bus.connect()
            self._log.info("event_bus_connected")
        except Exception as e:
            self._log.warning(
                "event_bus_connection_failed",
                error=str(e),
                message="Running without event bus",
            )

        # Configure shutdown manager with registered callbacks
        self._configure_shutdown_manager()

        # Install signal handlers for graceful shutdown
        self._shutdown_manager.install_signal_handlers()

        self._log.info(
            "mercury_started",
            dry_run=self._dry_run,
        )

    def _configure_shutdown_manager(self) -> None:
        """Configure shutdown manager with all registered callbacks."""
        # Phase 1: Stop accepting new work
        for callback in self._stop_new_work_callbacks:
            self._shutdown_manager.on_stop_new_work(self._wrap_callback(callback))

        # Phase 2: Drain in-flight orders
        if self._get_in_flight_count:
            self._shutdown_manager.set_in_flight_tracker(
                get_count=self._get_in_flight_count,
                force_cancel=self._wrap_callback(self._force_cancel_orders) if self._force_cancel_orders else None,
            )

        # Phase 3: Close connections (WebSocket, etc.)
        for name, service in self._services.items():
            if hasattr(service, "stop"):
                self._shutdown_manager.on_close_connections(
                    self._wrap_callback(service.stop)
                )

        # Phase 4: Flush data (metrics)
        self._shutdown_manager.on_flush_data(self._flush_metrics)

        # Phase 5: Cleanup (event bus disconnect)
        self._shutdown_manager.on_cleanup(self._cleanup_event_bus)

    def _wrap_callback(self, callback: Callable[[], Any]) -> Callable[[], Any]:
        """Wrap a callback to handle both sync and async functions."""
        async def wrapped() -> None:
            result = callback()
            if asyncio.iscoroutine(result):
                await result
        return wrapped

    async def _flush_metrics(self) -> None:
        """Flush final metrics before shutdown."""
        self._log.info("flushing_metrics")
        # Update final uptime
        self._metrics.update_uptime(self.uptime_seconds)
        # Metrics are already available via get_metrics() - nothing to flush to disk
        self._log.info("metrics_flushed")

    async def _cleanup_event_bus(self) -> None:
        """Disconnect from event bus."""
        if self._event_bus.is_connected:
            await self._event_bus.disconnect()
            self._log.info("event_bus_disconnected")

    async def _do_stop(self) -> None:
        """Stop all components gracefully via shutdown manager.

        This method triggers the full graceful shutdown sequence:
        1. Stop accepting new signals
        2. Wait for in-flight orders to complete
        3. Close WebSocket connections
        4. Flush metrics
        5. Close database connections
        """
        self._log.info("stopping_mercury")

        # If shutdown manager hasn't been triggered yet, run full shutdown
        if not self._shutdown_manager.progress.is_shutting_down:
            await self._shutdown_manager.shutdown()
        else:
            # Wait for ongoing shutdown to complete
            await self._shutdown_manager.wait_for_shutdown()

        # Remove signal handlers
        self._shutdown_manager.remove_signal_handlers()

        self._log.info(
            "mercury_stopped",
            shutdown_progress=self._shutdown_manager.progress.to_dict(),
        )

    async def _do_health_check(self) -> HealthCheckResult:
        """Check health of all components."""
        issues = []

        # Check if shutting down
        if self._shutdown_manager.is_shutting_down:
            return HealthCheckResult.degraded(
                message=f"Shutting down: {self._shutdown_manager.progress.phase.value}",
                uptime_seconds=self.uptime_seconds,
                shutdown_phase=self._shutdown_manager.progress.phase.value,
            )

        # Check event bus
        if not self._event_bus.is_connected:
            issues.append("event_bus_disconnected")

        # Check registered services
        for name, service in self._services.items():
            if hasattr(service, "health_check"):
                try:
                    service_health = await service.health_check()
                    if service_health.status == HealthStatus.UNHEALTHY:
                        issues.append(f"{name}_unhealthy")
                except Exception:
                    issues.append(f"{name}_health_check_failed")

        if issues:
            return HealthCheckResult.degraded(
                message=f"Issues: {', '.join(issues)}",
                uptime_seconds=self.uptime_seconds,
            )

        return HealthCheckResult.healthy(
            uptime_seconds=self.uptime_seconds,
            dry_run=self._dry_run,
        )

    async def run_forever(self) -> None:
        """Run the application until shutdown signal received.

        The application runs until SIGTERM/SIGINT is received, then executes
        graceful shutdown sequence.
        """
        await self.start()

        try:
            # Update uptime metric periodically while waiting for shutdown
            while not self._shutdown_manager.shutdown_event.is_set():
                self._metrics.update_uptime(self.uptime_seconds)
                try:
                    await asyncio.wait_for(
                        self._shutdown_manager.shutdown_event.wait(),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.stop()

    async def get_health(self) -> dict:
        """Get health status as dictionary.

        Returns:
            Health status dictionary including shutdown status if shutting down.
        """
        result = await self.health_check()
        health_dict = {
            "status": result.status.value,
            "message": result.message,
            "details": result.details,
            "checked_at": result.checked_at.isoformat(),
        }

        # Include shutdown status if shutting down
        if self._shutdown_manager.is_shutting_down:
            health_dict["shutting_down"] = True
            health_dict["shutdown_progress"] = self._shutdown_manager.progress.to_dict()

        return health_dict

    async def request_shutdown(self) -> None:
        """Programmatically request graceful shutdown.

        Use this method to trigger shutdown from code (e.g., from a REST endpoint
        or after a critical error).
        """
        self._log.info("shutdown_requested_programmatically")
        await self._shutdown_manager.shutdown()
