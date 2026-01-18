"""
Mercury application lifecycle and component wiring.

This is the main orchestrator that starts and stops all services.
"""
import asyncio
import signal
from pathlib import Path
from typing import Optional

import structlog

from mercury.core.config import ConfigManager
from mercury.core.events import EventBus
from mercury.core.lifecycle import BaseComponent, HealthCheckResult, HealthStatus
from mercury.core.logging import setup_logging
from mercury.services.metrics import MetricsEmitter


class MercuryApp(BaseComponent):
    """Main Mercury application.

    Orchestrates startup and shutdown of all components.

    Usage:
        app = MercuryApp()
        await app.start()
        # ... app runs ...
        await app.stop()
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

        # Shutdown event
        self._shutdown_event = asyncio.Event()

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

        # Set up signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._handle_shutdown)

        self._log.info(
            "mercury_started",
            dry_run=self._dry_run,
        )

    async def _do_stop(self) -> None:
        """Stop all components gracefully."""
        self._log.info("stopping_mercury")

        # Disconnect event bus
        if self._event_bus.is_connected:
            await self._event_bus.disconnect()
            self._log.info("event_bus_disconnected")

        self._log.info("mercury_stopped")

    async def _do_health_check(self) -> HealthCheckResult:
        """Check health of all components."""
        issues = []

        # Check event bus
        if not self._event_bus.is_connected:
            issues.append("event_bus_disconnected")

        if issues:
            return HealthCheckResult.degraded(
                message=f"Issues: {', '.join(issues)}",
                uptime_seconds=self.uptime_seconds,
            )

        return HealthCheckResult.healthy(
            uptime_seconds=self.uptime_seconds,
            dry_run=self._dry_run,
        )

    def _handle_shutdown(self) -> None:
        """Handle shutdown signal."""
        self._log.info("shutdown_signal_received")
        self._shutdown_event.set()

    async def run_forever(self) -> None:
        """Run the application until shutdown signal received."""
        await self.start()

        try:
            # Update uptime metric periodically
            while not self._shutdown_event.is_set():
                self._metrics.update_uptime(self.uptime_seconds)
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.stop()

    async def get_health(self) -> dict:
        """Get health status as dictionary.

        Returns:
            Health status dictionary
        """
        result = await self.health_check()
        return {
            "status": result.status.value,
            "message": result.message,
            "details": result.details,
            "checked_at": result.checked_at.isoformat(),
        }
