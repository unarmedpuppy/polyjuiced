"""HTTP server for Prometheus metrics."""

import asyncio
from aiohttp import web
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

import structlog

log = structlog.get_logger()


class MetricsServer:
    """Async HTTP server for Prometheus metrics endpoint."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8000):
        """Initialize metrics server.

        Args:
            host: Host to bind to
            port: Port to listen on
        """
        self.host = host
        self.port = port
        self._app: web.Application = None
        self._runner: web.AppRunner = None
        self._site: web.TCPSite = None

    async def start(self) -> None:
        """Start the metrics server."""
        self._app = web.Application()
        self._app.router.add_get("/metrics", self._handle_metrics)
        self._app.router.add_get("/health", self._handle_health)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()

        log.info(
            "Metrics server started",
            host=self.host,
            port=self.port,
            metrics_url=f"http://{self.host}:{self.port}/metrics",
        )

    async def stop(self) -> None:
        """Stop the metrics server."""
        if self._runner:
            await self._runner.cleanup()
            log.info("Metrics server stopped")

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        """Handle /metrics endpoint.

        Args:
            request: HTTP request

        Returns:
            HTTP response with Prometheus metrics
        """
        metrics = generate_latest()
        return web.Response(
            body=metrics,
            content_type=CONTENT_TYPE_LATEST,
        )

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Handle /health endpoint.

        Args:
            request: HTTP request

        Returns:
            HTTP response with health status
        """
        return web.json_response({"status": "healthy"})
