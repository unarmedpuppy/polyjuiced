"""
Unit tests for the health check endpoint.

Tests:
- HealthServer HTTP endpoints
- HealthStatusCollector aggregation
- Health status determination logic
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientSession

from mercury.core.events import EventBus
from mercury.domain.risk import CircuitBreakerState
from mercury.services.health import HealthServer, HealthStatusCollector


class TestHealthStatusCollector:
    """Tests for HealthStatusCollector."""

    @pytest.fixture
    def mock_event_bus(self):
        """Create mock EventBus."""
        bus = MagicMock(spec=EventBus)
        bus.is_connected = True
        return bus

    @pytest.fixture
    def collector(self, mock_event_bus):
        """Create HealthStatusCollector with mocks."""
        return HealthStatusCollector(
            event_bus=mock_event_bus,
            get_circuit_breaker_state=lambda: CircuitBreakerState.NORMAL,
            get_active_strategies=lambda: ["gabagool", "arbitrage"],
            get_open_positions_count=lambda: 5,
            get_websocket_connected=lambda: True,
            get_uptime_seconds=lambda: 3600.0,
        )

    @pytest.mark.asyncio
    async def test_get_health_status_all_healthy(self, collector):
        """Test health status when all components are healthy."""
        status = await collector.get_health_status()

        assert status["status"] == "healthy"
        assert status["redis_connected"] is True
        assert status["websocket_connected"] is True
        assert status["circuit_breaker_state"] == "NORMAL"
        assert status["uptime_seconds"] == 3600.0
        assert status["active_strategies"] == ["gabagool", "arbitrage"]
        assert status["open_positions_count"] == 5

    @pytest.mark.asyncio
    async def test_get_health_status_redis_disconnected(self, mock_event_bus):
        """Test health status when Redis is disconnected."""
        mock_event_bus.is_connected = False
        collector = HealthStatusCollector(
            event_bus=mock_event_bus,
            get_uptime_seconds=lambda: 100.0,
        )

        status = await collector.get_health_status()

        assert status["status"] == "unhealthy"
        assert status["redis_connected"] is False

    @pytest.mark.asyncio
    async def test_get_health_status_circuit_breaker_halt(self, mock_event_bus):
        """Test health status when circuit breaker is in HALT state."""
        collector = HealthStatusCollector(
            event_bus=mock_event_bus,
            get_circuit_breaker_state=lambda: CircuitBreakerState.HALT,
            get_uptime_seconds=lambda: 100.0,
        )

        status = await collector.get_health_status()

        assert status["status"] == "degraded"
        assert status["circuit_breaker_state"] == "HALT"

    @pytest.mark.asyncio
    async def test_get_health_status_websocket_disconnected(self, mock_event_bus):
        """Test health status when WebSocket is disconnected."""
        collector = HealthStatusCollector(
            event_bus=mock_event_bus,
            get_websocket_connected=lambda: False,
            get_uptime_seconds=lambda: 100.0,
        )

        status = await collector.get_health_status()

        # WebSocket disconnection doesn't make the service unhealthy
        assert status["status"] == "healthy"
        assert status["websocket_connected"] is False

    @pytest.mark.asyncio
    async def test_get_health_status_with_async_positions_count(self, mock_event_bus):
        """Test health status with async open positions count provider."""
        async def async_get_positions_count():
            return 10

        collector = HealthStatusCollector(
            event_bus=mock_event_bus,
            get_open_positions_count=async_get_positions_count,
            get_uptime_seconds=lambda: 100.0,
        )

        status = await collector.get_health_status()

        assert status["open_positions_count"] == 10

    @pytest.mark.asyncio
    async def test_get_health_status_handles_provider_errors(self, mock_event_bus):
        """Test health status handles errors from providers gracefully."""
        def raise_error():
            raise RuntimeError("Provider error")

        collector = HealthStatusCollector(
            event_bus=mock_event_bus,
            get_circuit_breaker_state=raise_error,
            get_active_strategies=raise_error,
            get_open_positions_count=raise_error,
            get_websocket_connected=raise_error,
            get_uptime_seconds=lambda: 100.0,
        )

        # Should not raise, returns degraded status with defaults
        status = await collector.get_health_status()

        assert status["status"] == "degraded"  # Due to websocket_status_error
        assert status["circuit_breaker_state"] == "UNKNOWN"
        assert status["active_strategies"] == []
        assert status["open_positions_count"] == 0
        assert status["websocket_connected"] is False

    @pytest.mark.asyncio
    async def test_get_health_status_no_providers(self, mock_event_bus):
        """Test health status with no optional providers."""
        collector = HealthStatusCollector(event_bus=mock_event_bus)

        status = await collector.get_health_status()

        assert status["status"] == "healthy"
        assert status["redis_connected"] is True
        assert status["websocket_connected"] is False
        assert status["circuit_breaker_state"] == "NORMAL"
        assert status["uptime_seconds"] == 0.0
        assert status["active_strategies"] == []
        assert status["open_positions_count"] == 0


class TestHealthServer:
    """Tests for HealthServer."""

    @pytest.fixture
    def health_data(self):
        """Sample health data."""
        return {
            "status": "healthy",
            "redis_connected": True,
            "websocket_connected": True,
            "circuit_breaker_state": "NORMAL",
            "uptime_seconds": 3600.0,
            "active_strategies": ["gabagool"],
            "open_positions_count": 5,
        }

    @pytest.fixture
    async def server(self, health_data):
        """Create and start HealthServer."""
        async def health_provider():
            return health_data

        def metrics_provider():
            return "# HELP mercury_uptime_seconds Uptime\nmercury_uptime_seconds 3600\n"

        server = HealthServer(
            port=19091,  # Use non-standard port for testing
            health_provider=health_provider,
            metrics_provider=metrics_provider,
        )
        await server.start()
        yield server
        await server.stop()

    @pytest.mark.asyncio
    async def test_health_endpoint_returns_json(self, server, health_data):
        """Test /health endpoint returns expected JSON."""
        async with ClientSession() as session:
            async with session.get(f"http://localhost:{server.port}/health") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data == health_data

    @pytest.mark.asyncio
    async def test_health_endpoint_returns_503_when_unhealthy(self):
        """Test /health endpoint returns 503 when unhealthy."""
        async def unhealthy_provider():
            return {"status": "unhealthy", "redis_connected": False}

        server = HealthServer(
            port=19092,
            health_provider=unhealthy_provider,
        )
        await server.start()

        try:
            async with ClientSession() as session:
                async with session.get(f"http://localhost:{server.port}/health") as resp:
                    assert resp.status == 503
                    data = await resp.json()
                    assert data["status"] == "unhealthy"
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_health_endpoint_returns_200_when_degraded(self):
        """Test /health endpoint returns 200 when degraded (still available)."""
        async def degraded_provider():
            return {"status": "degraded", "circuit_breaker_state": "WARNING"}

        server = HealthServer(
            port=19093,
            health_provider=degraded_provider,
        )
        await server.start()

        try:
            async with ClientSession() as session:
                async with session.get(f"http://localhost:{server.port}/health") as resp:
                    assert resp.status == 200
                    data = await resp.json()
                    assert data["status"] == "degraded"
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_metrics_endpoint_returns_prometheus_format(self, server):
        """Test /metrics endpoint returns Prometheus format."""
        async with ClientSession() as session:
            async with session.get(f"http://localhost:{server.port}/metrics") as resp:
                assert resp.status == 200
                text = await resp.text()
                assert "mercury_uptime_seconds" in text
                assert resp.content_type.startswith("text/plain")

    @pytest.mark.asyncio
    async def test_root_endpoint_returns_service_info(self, server):
        """Test / endpoint returns service info."""
        async with ClientSession() as session:
            async with session.get(f"http://localhost:{server.port}/") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["service"] == "mercury"
                assert "/health" in data["endpoints"]
                assert "/metrics" in data["endpoints"]

    @pytest.mark.asyncio
    async def test_health_endpoint_handles_provider_error(self):
        """Test /health endpoint handles provider errors gracefully."""
        async def failing_provider():
            raise RuntimeError("Provider failed")

        server = HealthServer(
            port=19094,
            health_provider=failing_provider,
        )
        await server.start()

        try:
            async with ClientSession() as session:
                async with session.get(f"http://localhost:{server.port}/health") as resp:
                    assert resp.status == 503
                    data = await resp.json()
                    assert data["status"] == "unhealthy"
                    assert "error" in data
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_health_endpoint_no_provider(self):
        """Test /health endpoint with no provider configured."""
        server = HealthServer(port=19095)
        await server.start()

        try:
            async with ClientSession() as session:
                async with session.get(f"http://localhost:{server.port}/health") as resp:
                    assert resp.status == 503
                    data = await resp.json()
                    assert data["status"] == "unknown"
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_server_health_check(self):
        """Test HealthServer's own health check."""
        server = HealthServer(port=19096)

        # Before starting
        health = await server.health_check()
        assert health.status.value == "unhealthy"

        # After starting
        await server.start()
        health = await server.health_check()
        assert health.status.value == "healthy"

        # After stopping
        await server.stop()
        health = await server.health_check()
        assert health.status.value == "unhealthy"


class TestHealthIntegration:
    """Integration tests for health system components."""

    @pytest.mark.asyncio
    async def test_health_server_with_collector(self):
        """Test HealthServer with HealthStatusCollector integration."""
        # Create mock event bus
        mock_event_bus = MagicMock(spec=EventBus)
        mock_event_bus.is_connected = True

        # Create collector
        collector = HealthStatusCollector(
            event_bus=mock_event_bus,
            get_circuit_breaker_state=lambda: CircuitBreakerState.WARNING,
            get_active_strategies=lambda: ["gabagool"],
            get_open_positions_count=lambda: 3,
            get_websocket_connected=lambda: True,
            get_uptime_seconds=lambda: 1800.0,
        )

        # Create server with collector
        server = HealthServer(
            port=19097,
            health_provider=collector.get_health_status,
        )
        await server.start()

        try:
            async with ClientSession() as session:
                async with session.get(f"http://localhost:{server.port}/health") as resp:
                    assert resp.status == 200
                    data = await resp.json()

                    assert data["status"] == "healthy"
                    assert data["redis_connected"] is True
                    assert data["websocket_connected"] is True
                    assert data["circuit_breaker_state"] == "WARNING"
                    assert data["uptime_seconds"] == 1800.0
                    assert data["active_strategies"] == ["gabagool"]
                    assert data["open_positions_count"] == 3
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_health_response_schema(self):
        """Test that health response matches expected schema."""
        mock_event_bus = MagicMock(spec=EventBus)
        mock_event_bus.is_connected = True

        collector = HealthStatusCollector(
            event_bus=mock_event_bus,
            get_circuit_breaker_state=lambda: CircuitBreakerState.NORMAL,
            get_active_strategies=lambda: [],
            get_open_positions_count=lambda: 0,
            get_websocket_connected=lambda: False,
            get_uptime_seconds=lambda: 0.0,
        )

        server = HealthServer(
            port=19098,
            health_provider=collector.get_health_status,
        )
        await server.start()

        try:
            async with ClientSession() as session:
                async with session.get(f"http://localhost:{server.port}/health") as resp:
                    data = await resp.json()

                    # Verify all required fields are present
                    required_fields = [
                        "status",
                        "redis_connected",
                        "websocket_connected",
                        "circuit_breaker_state",
                        "uptime_seconds",
                        "active_strategies",
                        "open_positions_count",
                    ]
                    for field in required_fields:
                        assert field in data, f"Missing required field: {field}"

                    # Verify types
                    assert isinstance(data["status"], str)
                    assert isinstance(data["redis_connected"], bool)
                    assert isinstance(data["websocket_connected"], bool)
                    assert isinstance(data["circuit_breaker_state"], str)
                    assert isinstance(data["uptime_seconds"], (int, float))
                    assert isinstance(data["active_strategies"], list)
                    assert isinstance(data["open_positions_count"], int)
        finally:
            await server.stop()
