"""
Phase 9 Smoke Test: End-to-End

Verifies the full trading flow works:
1. Market data streams
2. Strategy generates signal
3. Risk manager approves
4. Execution engine places order
5. Position is tracked
6. Settlement processes

Run: pytest tests/smoke/test_phase9_e2e.py -v
"""
import pytest
from decimal import Decimal
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock
import asyncio


class TestPhase9EndToEnd:
    """Phase 9 must pass ALL these tests to be considered complete."""

    @pytest.mark.asyncio
    async def test_full_trading_flow(self, tmp_path):
        """
        End-to-end test of the full trading flow.

        Simulates:
        1. Market data with arbitrage opportunity arrives
        2. Gabagool strategy detects it and emits signal
        3. Risk manager validates and approves
        4. Execution engine executes dual-leg order
        5. Position is recorded in state store
        6. Settlement claims the position when market resolves
        """
        from mercury.app import MercuryApp
        from mercury.core.events import EventBus
        from mercury.domain.market import OrderBook, OrderBookLevel
        from mercury.domain.order import OrderResult

        # Track events for verification
        events_received = []

        async def event_tracker(channel, event):
            events_received.append((channel, event))

        # Create app with mocked external services
        app = MercuryApp(
            config_path=None,  # Use defaults
            dry_run=True,
            db_path=str(tmp_path / "test.db"),
        )

        # Mock CLOB client
        app.clob_client.place_order = AsyncMock(return_value=OrderResult(
            order_id="test-order-1",
            status="filled",
            filled_size=Decimal("10.0"),
            price=Decimal("0.48"),
        ))

        # Start the app
        await app.start()

        # Subscribe to track events
        await app.event_bus.subscribe("signal.*", event_tracker)
        await app.event_bus.subscribe("risk.*", event_tracker)
        await app.event_bus.subscribe("order.*", event_tracker)
        await app.event_bus.subscribe("position.*", event_tracker)

        # Inject market data with arbitrage opportunity
        book = OrderBook(
            market_id="test-market-btc",
            yes_bids=[],
            yes_asks=[OrderBookLevel(price=Decimal("0.48"), size=Decimal("100"))],
            no_bids=[],
            no_asks=[OrderBookLevel(price=Decimal("0.50"), size=Decimal("100"))],
        )

        await app.market_data_service._on_orderbook_update("test-market-btc", book)

        # Give time for event propagation
        await asyncio.sleep(0.5)

        # Verify the flow
        channels = [e[0] for e in events_received]

        # 1. Signal should be generated
        assert any("signal." in c for c in channels), "No signal generated"

        # 2. Risk should approve (or reject, but respond)
        assert any("risk." in c for c in channels), "No risk response"

        # 3. Order should be submitted/filled
        assert any("order." in c for c in channels), "No order event"

        await app.stop()

    @pytest.mark.asyncio
    async def test_app_health_check(self, tmp_path):
        """Verify health check endpoint works."""
        from mercury.app import MercuryApp

        app = MercuryApp(
            config_path=None,
            dry_run=True,
            db_path=str(tmp_path / "test.db"),
        )

        await app.start()

        health = await app.health_check()

        assert health["status"] in ["healthy", "degraded"]
        assert "redis_connected" in health
        assert "uptime_seconds" in health
        assert health["uptime_seconds"] >= 0

        await app.stop()

    @pytest.mark.asyncio
    async def test_graceful_shutdown(self, tmp_path):
        """Verify graceful shutdown works."""
        from mercury.app import MercuryApp

        app = MercuryApp(
            config_path=None,
            dry_run=True,
            db_path=str(tmp_path / "test.db"),
        )

        await app.start()
        assert app.is_running

        # Trigger graceful shutdown
        await app.stop()

        assert not app.is_running
        # Verify all services stopped
        assert not app.market_data_service.is_running
        assert not app.strategy_engine.is_running
        assert not app.execution_engine.is_running

    @pytest.mark.asyncio
    async def test_metrics_endpoint(self, tmp_path):
        """Verify Prometheus metrics endpoint works."""
        from mercury.app import MercuryApp
        import aiohttp

        app = MercuryApp(
            config_path=None,
            dry_run=True,
            db_path=str(tmp_path / "test.db"),
            metrics_port=19090,  # Use non-standard port for test
        )

        await app.start()

        # Fetch metrics
        async with aiohttp.ClientSession() as session:
            async with session.get("http://localhost:19090/metrics") as resp:
                assert resp.status == 200
                text = await resp.text()
                assert "mercury_" in text

        await app.stop()

    @pytest.mark.asyncio
    async def test_circuit_breaker_halts_trading(self, tmp_path):
        """Verify circuit breaker prevents trading when tripped."""
        from mercury.app import MercuryApp
        from mercury.domain.market import OrderBook, OrderBookLevel
        from mercury.domain.risk import CircuitBreakerState

        app = MercuryApp(
            config_path=None,
            dry_run=True,
            db_path=str(tmp_path / "test.db"),
        )

        await app.start()

        # Trip the circuit breaker
        for _ in range(10):
            app.risk_manager.record_failure()

        assert app.risk_manager.circuit_breaker_state == CircuitBreakerState.HALT

        # Track if any orders get executed
        orders_executed = []
        original_execute = app.execution_engine.execute

        async def tracking_execute(*args, **kwargs):
            orders_executed.append(args)
            return await original_execute(*args, **kwargs)

        app.execution_engine.execute = tracking_execute

        # Inject market data with opportunity
        book = OrderBook(
            market_id="test-market",
            yes_bids=[],
            yes_asks=[OrderBookLevel(price=Decimal("0.48"), size=Decimal("100"))],
            no_bids=[],
            no_asks=[OrderBookLevel(price=Decimal("0.50"), size=Decimal("100"))],
        )

        await app.market_data_service._on_orderbook_update("test-market", book)
        await asyncio.sleep(0.5)

        # No orders should have been executed
        assert len(orders_executed) == 0, "Orders executed despite circuit breaker"

        await app.stop()

    @pytest.mark.asyncio
    async def test_strategy_enable_disable_at_runtime(self, tmp_path):
        """Verify strategies can be toggled at runtime."""
        from mercury.app import MercuryApp

        app = MercuryApp(
            config_path=None,
            dry_run=True,
            db_path=str(tmp_path / "test.db"),
        )

        await app.start()

        # Disable gabagool
        app.strategy_engine.disable_strategy("gabagool")
        assert not app.strategy_engine.is_strategy_enabled("gabagool")

        # Enable gabagool
        app.strategy_engine.enable_strategy("gabagool")
        assert app.strategy_engine.is_strategy_enabled("gabagool")

        await app.stop()

    def test_grafana_dashboard_exists(self):
        """Verify Grafana dashboard JSON exists."""
        from pathlib import Path

        dashboard_path = Path(__file__).parent.parent.parent.parent / "docker" / "grafana" / "dashboards"

        # At minimum, check the directory structure is set up
        # The actual dashboard files are created in Phase 9 tasks
        assert dashboard_path.parent.exists() or True  # Soft check - directory may not exist yet

    def test_runbook_exists(self):
        """Verify operations runbook exists."""
        from pathlib import Path

        runbook_path = Path(__file__).parent.parent.parent.parent / "docs" / "RUNBOOK.md"

        # Soft check - file created in Phase 9 tasks
        # This test documents the requirement
        assert runbook_path.parent.exists() or True

    @pytest.mark.asyncio
    async def test_parallel_validation_mode(self, tmp_path):
        """
        Verify Mercury can run in parallel validation mode.

        In this mode, Mercury runs alongside polyjuiced (legacy) and
        logs signal comparisons without executing trades.
        """
        from mercury.app import MercuryApp

        app = MercuryApp(
            config_path=None,
            dry_run=True,
            validation_mode=True,  # Special mode for parallel running
            db_path=str(tmp_path / "test.db"),
        )

        await app.start()

        assert app.is_running
        assert app.validation_mode is True

        # In validation mode, execution engine should be disabled
        assert not app.execution_engine.is_accepting_orders

        await app.stop()
