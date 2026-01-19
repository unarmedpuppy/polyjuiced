"""
HTTP health check endpoint for Mercury.

Provides a /health endpoint returning JSON status used by Docker health checks
and monitoring systems.

Response format:
{
    "status": "healthy" | "degraded" | "unhealthy",
    "redis_connected": bool,
    "websocket_connected": bool,
    "circuit_breaker_state": "NORMAL" | "WARNING" | "CAUTION" | "HALT",
    "uptime_seconds": float,
    "active_strategies": ["strategy1", "strategy2"],
    "open_positions_count": int
}
"""

import asyncio
from typing import TYPE_CHECKING, Any, Callable, Optional

import structlog
from aiohttp import web

from mercury.core.lifecycle import BaseComponent, HealthCheckResult, HealthStatus

if TYPE_CHECKING:
    from mercury.core.config import ConfigManager
    from mercury.core.events import EventBus
    from mercury.domain.risk import CircuitBreakerState
    from mercury.services.metrics import MetricsEmitter

log = structlog.get_logger()


class HealthServer(BaseComponent):
    """HTTP server providing health check endpoint.

    Exposes /health and /metrics endpoints for Docker health checks,
    Kubernetes probes, and Prometheus scraping.

    Usage:
        server = HealthServer(
            port=9090,
            health_provider=app.get_health_status,
            metrics_provider=metrics.get_metrics,
        )
        await server.start()
        # Server runs on http://localhost:9090/health
        await server.stop()
    """

    def __init__(
        self,
        port: int = 9090,
        host: str = "0.0.0.0",
        health_provider: Optional[Callable[[], Any]] = None,
        metrics_provider: Optional[Callable[[], str]] = None,
    ) -> None:
        """Initialize the health server.

        Args:
            port: Port to listen on.
            host: Host to bind to.
            health_provider: Async callable that returns health status dict.
            metrics_provider: Callable that returns Prometheus metrics text.
        """
        super().__init__(name="HealthServer")
        self._port = port
        self._host = host
        self._health_provider = health_provider
        self._metrics_provider = metrics_provider
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._log = log.bind(component="health_server")

    @property
    def port(self) -> int:
        """Get the configured port."""
        return self._port

    async def _do_start(self) -> None:
        """Start the HTTP server."""
        self._app = web.Application()
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_get("/metrics", self._handle_metrics)
        self._app.router.add_get("/", self._handle_root)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()

        self._log.info(
            "health_server_started",
            host=self._host,
            port=self._port,
            endpoints=["/health", "/metrics"],
        )

    async def _do_stop(self) -> None:
        """Stop the HTTP server."""
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        self._app = None
        self._runner = None
        self._site = None
        self._log.info("health_server_stopped")

    async def _do_health_check(self) -> HealthCheckResult:
        """Check health server's own health."""
        if self._site is None:
            return HealthCheckResult.unhealthy("Server not running")
        return HealthCheckResult.healthy(
            uptime_seconds=self.uptime_seconds,
            port=self._port,
        )

    async def _handle_root(self, request: web.Request) -> web.Response:
        """Handle root endpoint with service info."""
        return web.json_response({
            "service": "mercury",
            "version": "0.1.0",
            "endpoints": ["/health", "/metrics"],
        })

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Handle /health endpoint."""
        try:
            if self._health_provider is None:
                return web.json_response(
                    {"status": "unknown", "error": "No health provider configured"},
                    status=503,
                )

            # Get health status from provider
            health_data = await self._health_provider()

            # Determine HTTP status code based on health status
            status_code = 200
            if health_data.get("status") == "unhealthy":
                status_code = 503
            elif health_data.get("status") == "degraded":
                status_code = 200  # Degraded is still considered "up"

            return web.json_response(health_data, status=status_code)

        except Exception as e:
            self._log.error("health_check_error", error=str(e))
            return web.json_response(
                {"status": "unhealthy", "error": str(e)},
                status=503,
            )

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        """Handle /metrics endpoint for Prometheus."""
        try:
            if self._metrics_provider is None:
                return web.Response(
                    text="# No metrics provider configured\n",
                    content_type="text/plain",
                )

            metrics_text = self._metrics_provider()
            return web.Response(
                text=metrics_text,
                content_type="text/plain",
            )

        except Exception as e:
            self._log.error("metrics_error", error=str(e))
            return web.Response(
                text=f"# Error: {e}\n",
                content_type="text/plain",
                status=500,
            )


class HealthStatusCollector:
    """Collects health status from various Mercury components.

    This class aggregates health information from different services
    to provide a complete health status response.

    Usage:
        collector = HealthStatusCollector(
            event_bus=event_bus,
            get_circuit_breaker_state=risk_manager.circuit_breaker_state,
            get_active_strategies=strategy_engine.get_active_strategy_names,
            get_open_positions_count=state_store.get_open_positions_count,
            get_websocket_connected=websocket.is_connected,
            get_uptime_seconds=app.uptime_seconds,
        )
        status = await collector.get_health_status()
    """

    def __init__(
        self,
        event_bus: "EventBus",
        get_circuit_breaker_state: Optional[Callable[[], "CircuitBreakerState"]] = None,
        get_active_strategies: Optional[Callable[[], list[str]]] = None,
        get_open_positions_count: Optional[Callable[[], int]] = None,
        get_websocket_connected: Optional[Callable[[], bool]] = None,
        get_uptime_seconds: Optional[Callable[[], float]] = None,
    ) -> None:
        """Initialize the health status collector.

        Args:
            event_bus: EventBus instance to check Redis connection.
            get_circuit_breaker_state: Callable returning CircuitBreakerState.
            get_active_strategies: Callable returning list of active strategy names.
            get_open_positions_count: Async callable returning open positions count.
            get_websocket_connected: Callable returning WebSocket connection status.
            get_uptime_seconds: Callable returning uptime in seconds.
        """
        self._event_bus = event_bus
        self._get_circuit_breaker_state = get_circuit_breaker_state
        self._get_active_strategies = get_active_strategies
        self._get_open_positions_count = get_open_positions_count
        self._get_websocket_connected = get_websocket_connected
        self._get_uptime_seconds = get_uptime_seconds
        self._log = log.bind(component="health_collector")

    async def get_health_status(self) -> dict[str, Any]:
        """Get aggregated health status.

        Returns:
            Dictionary containing:
            - status: "healthy", "degraded", or "unhealthy"
            - redis_connected: bool
            - websocket_connected: bool
            - circuit_breaker_state: str
            - uptime_seconds: float
            - active_strategies: list[str]
            - open_positions_count: int
        """
        issues: list[str] = []

        # Check Redis connection
        redis_connected = self._event_bus.is_connected if self._event_bus else False
        if not redis_connected:
            issues.append("redis_disconnected")

        # Check WebSocket connection
        websocket_connected = False
        if self._get_websocket_connected:
            try:
                websocket_connected = self._get_websocket_connected()
            except Exception as e:
                self._log.warning("websocket_status_error", error=str(e))
                issues.append("websocket_status_error")

        # Get circuit breaker state
        circuit_breaker_state = "NORMAL"
        if self._get_circuit_breaker_state:
            try:
                state = self._get_circuit_breaker_state()
                circuit_breaker_state = state.value if hasattr(state, "value") else str(state)
                if circuit_breaker_state == "HALT":
                    issues.append("circuit_breaker_halt")
            except Exception as e:
                self._log.warning("circuit_breaker_state_error", error=str(e))
                circuit_breaker_state = "UNKNOWN"

        # Get uptime
        uptime_seconds = 0.0
        if self._get_uptime_seconds:
            try:
                uptime_seconds = self._get_uptime_seconds()
            except Exception as e:
                self._log.warning("uptime_error", error=str(e))

        # Get active strategies
        active_strategies: list[str] = []
        if self._get_active_strategies:
            try:
                active_strategies = self._get_active_strategies()
            except Exception as e:
                self._log.warning("active_strategies_error", error=str(e))

        # Get open positions count
        open_positions_count = 0
        if self._get_open_positions_count:
            try:
                result = self._get_open_positions_count()
                # Handle both sync and async callables
                if asyncio.iscoroutine(result):
                    open_positions_count = await result
                else:
                    open_positions_count = result
            except Exception as e:
                self._log.warning("open_positions_count_error", error=str(e))

        # Determine overall status
        if not redis_connected:
            status = "unhealthy"
        elif issues:
            status = "degraded"
        else:
            status = "healthy"

        return {
            "status": status,
            "redis_connected": redis_connected,
            "websocket_connected": websocket_connected,
            "circuit_breaker_state": circuit_breaker_state,
            "uptime_seconds": uptime_seconds,
            "active_strategies": active_strategies,
            "open_positions_count": open_positions_count,
        }
