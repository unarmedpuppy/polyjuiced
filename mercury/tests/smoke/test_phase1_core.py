"""
Phase 1 Smoke Test: Core Infrastructure

Verifies that Phase 1 deliverables work together:
- ConfigManager loads TOML and env vars
- EventBus connects to Redis and can pub/sub
- Metrics endpoint responds
- Domain models are importable
- App entry point initializes

Run: pytest tests/smoke/test_phase1_core.py -v
"""
import pytest


class TestPhase1CoreInfrastructure:
    """Phase 1 must pass ALL these tests to be considered complete."""

    def test_project_structure_exists(self):
        """Verify mercury package is importable."""
        import mercury
        assert mercury is not None

    def test_config_manager_importable(self):
        """Verify ConfigManager can be imported."""
        from mercury.core.config import ConfigManager
        assert ConfigManager is not None

    def test_config_loads_toml(self, tmp_path):
        """Verify ConfigManager loads TOML files."""
        from mercury.core.config import ConfigManager

        # Create test config
        config_file = tmp_path / "test.toml"
        config_file.write_text("""
[mercury]
log_level = "DEBUG"
dry_run = true

[strategies.gabagool]
enabled = true
min_spread_threshold = 0.015
""")

        config = ConfigManager(config_file)
        assert config.get("mercury.log_level") == "DEBUG"
        assert config.get("mercury.dry_run") is True
        assert config.get("strategies.gabagool.enabled") is True
        assert config.get("strategies.gabagool.min_spread_threshold") == 0.015

    def test_config_env_override(self, tmp_path, monkeypatch):
        """Verify environment variables override TOML."""
        from mercury.core.config import ConfigManager

        config_file = tmp_path / "test.toml"
        config_file.write_text("""
[mercury]
dry_run = true
""")

        monkeypatch.setenv("MERCURY_DRY_RUN", "false")
        config = ConfigManager(config_file)
        assert config.get("mercury.dry_run") is False

    def test_event_bus_importable(self):
        """Verify EventBus can be imported."""
        from mercury.core.events import EventBus
        assert EventBus is not None

    @pytest.mark.asyncio
    async def test_event_bus_connects_to_redis(self):
        """Verify EventBus can connect to Redis."""
        from mercury.core.events import EventBus

        bus = EventBus(redis_url="redis://localhost:6379")
        await bus.connect()
        assert bus.is_connected
        await bus.disconnect()

    @pytest.mark.asyncio
    async def test_event_bus_pub_sub(self):
        """Verify EventBus can publish and subscribe."""
        from mercury.core.events import EventBus

        bus = EventBus(redis_url="redis://localhost:6379")
        await bus.connect()

        received = []

        async def handler(event):
            received.append(event)

        await bus.subscribe("test.channel", handler)
        await bus.publish("test.channel", {"message": "hello"})

        # Give time for message to arrive
        import asyncio
        await asyncio.sleep(0.1)

        assert len(received) == 1
        assert received[0]["message"] == "hello"

        await bus.disconnect()

    def test_logging_importable(self):
        """Verify logging setup can be imported."""
        from mercury.core.logging import setup_logging
        assert setup_logging is not None

    def test_lifecycle_protocol_importable(self):
        """Verify lifecycle protocols can be imported."""
        from mercury.core.lifecycle import Startable, HealthCheckable
        assert Startable is not None
        assert HealthCheckable is not None

    def test_domain_models_importable(self):
        """Verify all domain models can be imported."""
        from mercury.domain.market import Market, OrderBook, OrderBookLevel
        from mercury.domain.order import Order, OrderRequest, OrderResult, Fill, Position
        from mercury.domain.signal import TradingSignal, SignalType
        from mercury.domain.risk import RiskLimits, CircuitBreakerState

        assert Market is not None
        assert OrderBook is not None
        assert TradingSignal is not None
        assert CircuitBreakerState is not None

    def test_metrics_endpoint_responds(self):
        """Verify Prometheus metrics endpoint works."""
        from mercury.services.metrics import MetricsEmitter

        emitter = MetricsEmitter()
        metrics_output = emitter.get_metrics()

        assert "mercury_" in metrics_output  # Has mercury prefix
        assert "uptime" in metrics_output.lower() or "info" in metrics_output.lower()

    def test_app_entry_point_importable(self):
        """Verify app can be imported and instantiated."""
        from mercury.app import MercuryApp

        app = MercuryApp()
        assert app is not None

    @pytest.mark.asyncio
    async def test_app_starts_and_stops(self):
        """Verify app lifecycle works."""
        from mercury.app import MercuryApp

        app = MercuryApp(dry_run=True)
        await app.start()
        assert app.is_running
        await app.stop()
        assert not app.is_running
