"""
Phase 5 Smoke Test: Execution Engine

Verifies that Phase 5 deliverables work:
- ExecutionEngine processes orders
- Queue management works
- Concurrent execution limits work
- Order cancellation works
- Latency tracking works
- Event emission works

Run: pytest tests/smoke/test_phase5_execution.py -v
"""
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from mercury.services.execution import ExecutionEngine, ExecutionSignal
from mercury.domain.signal import SignalType, SignalPriority


@pytest.fixture
def smoke_mock_config():
    """Mock config that returns proper values."""
    config = MagicMock()
    config.get.return_value = None
    config.get_bool.return_value = True  # dry_run = True
    config.get_int.side_effect = lambda key, default: {
        "execution.max_concurrent": 3,
        "execution.max_queue_size": 100,
    }.get(key, default)
    config.get_float.side_effect = lambda key, default: {
        "execution.queue_timeout_seconds": 60.0,
    }.get(key, default)
    return config


@pytest.fixture
def smoke_mock_event_bus():
    """Mock event bus."""
    bus = MagicMock()
    bus.publish = AsyncMock()
    bus.subscribe = AsyncMock()
    return bus


@pytest.fixture
def smoke_mock_clob():
    """Mock CLOB client."""
    clob = MagicMock()
    clob.connect = AsyncMock()
    clob.close = AsyncMock()
    clob.cancel_order = AsyncMock(return_value=True)
    clob.cancel_all_orders = AsyncMock()
    clob._connected = False
    return clob


class TestPhase5ExecutionEngine:
    """Phase 5 must pass ALL these tests to be considered complete."""

    def test_execution_engine_importable(self):
        """Verify ExecutionEngine can be imported."""
        from mercury.services.execution import ExecutionEngine
        assert ExecutionEngine is not None

    @pytest.mark.asyncio
    async def test_execution_engine_starts_stops(
        self, smoke_mock_config, smoke_mock_event_bus, smoke_mock_clob
    ):
        """Verify ExecutionEngine lifecycle works."""
        engine = ExecutionEngine(
            config=smoke_mock_config,
            event_bus=smoke_mock_event_bus,
            clob_client=smoke_mock_clob,
        )

        await engine.start()
        assert engine.is_running

        await engine.stop()
        assert not engine.is_running

    @pytest.mark.asyncio
    async def test_dry_run_execution(
        self, smoke_mock_config, smoke_mock_event_bus, smoke_mock_clob
    ):
        """Verify dry run signal execution works."""
        engine = ExecutionEngine(
            config=smoke_mock_config,
            event_bus=smoke_mock_event_bus,
            clob_client=smoke_mock_clob,
        )
        await engine.start()

        signal = ExecutionSignal(
            signal_id="test-signal",
            original_signal_id="test-signal",
            market_id="test-market",
            signal_type=SignalType.ARBITRAGE,
            target_size_usd=Decimal("100"),
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.50"),
            yes_token_id="yes-token",
            no_token_id="no-token",
        )

        result = await engine.execute(signal)

        assert result.success is True
        assert result.signal_id == "test-signal"
        assert result.yes_filled > 0
        assert result.no_filled > 0

        await engine.stop()

    @pytest.mark.asyncio
    async def test_queue_management(
        self, smoke_mock_config, smoke_mock_event_bus, smoke_mock_clob
    ):
        """Verify queue management works."""
        engine = ExecutionEngine(
            config=smoke_mock_config,
            event_bus=smoke_mock_event_bus,
            clob_client=smoke_mock_clob,
        )
        await engine.start()

        # Queue a signal
        signal_data = {
            "signal_id": "test-signal",
            "market_id": "test-market",
            "signal_type": "ARBITRAGE",
            "target_size_usd": "100.0",
            "yes_price": "0.48",
            "no_price": "0.50",
        }
        result = await engine.queue_signal("test-signal", signal_data, SignalPriority.HIGH)

        assert result is True
        assert engine.get_queue_size() == 1

        # Get queue stats
        stats = engine.get_queue_stats()
        assert stats["total_queued"] == 1
        assert stats["max_concurrent"] == 3

        await engine.stop()

    @pytest.mark.asyncio
    async def test_order_cancellation(
        self, smoke_mock_config, smoke_mock_event_bus, smoke_mock_clob
    ):
        """Verify order cancellation works."""
        engine = ExecutionEngine(
            config=smoke_mock_config,
            event_bus=smoke_mock_event_bus,
            clob_client=smoke_mock_clob,
        )
        await engine.start()

        # Use cancel_order method
        result = await engine.cancel_order("test-order-id")

        assert result is True

        await engine.stop()

    @pytest.mark.asyncio
    async def test_latency_tracking(
        self, smoke_mock_config, smoke_mock_event_bus, smoke_mock_clob
    ):
        """Verify execution latency is tracked."""
        engine = ExecutionEngine(
            config=smoke_mock_config,
            event_bus=smoke_mock_event_bus,
            clob_client=smoke_mock_clob,
        )
        await engine.start()

        signal = ExecutionSignal(
            signal_id="test-signal",
            original_signal_id="test-signal",
            market_id="test-market",
            signal_type=SignalType.ARBITRAGE,
            target_size_usd=Decimal("100"),
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.50"),
            yes_token_id="yes-token",
            no_token_id="no-token",
        )

        result = await engine.execute(signal)

        # Check latency was recorded in result
        assert result.execution_time_ms is not None
        assert result.execution_time_ms > 0

        await engine.stop()

    @pytest.mark.asyncio
    async def test_emits_order_events(
        self, smoke_mock_config, smoke_mock_event_bus, smoke_mock_clob
    ):
        """Verify order lifecycle events are emitted."""
        engine = ExecutionEngine(
            config=smoke_mock_config,
            event_bus=smoke_mock_event_bus,
            clob_client=smoke_mock_clob,
        )
        await engine.start()

        signal = ExecutionSignal(
            signal_id="test-signal",
            original_signal_id="test-signal",
            market_id="test-market",
            signal_type=SignalType.ARBITRAGE,
            target_size_usd=Decimal("100"),
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.50"),
            yes_token_id="yes-token",
            no_token_id="no-token",
        )

        await engine.execute(signal)

        # Verify events were published
        calls = smoke_mock_event_bus.publish.call_args_list
        channels = [call[0][0] for call in calls]

        # Should emit position.opened and execution.complete for dry run
        assert any("position.opened" in c for c in channels)
        assert any("execution.complete" in c for c in channels)

        await engine.stop()

    @pytest.mark.asyncio
    async def test_concurrent_execution_limit(
        self, smoke_mock_config, smoke_mock_event_bus, smoke_mock_clob
    ):
        """Verify concurrent execution limits are enforced."""
        engine = ExecutionEngine(
            config=smoke_mock_config,
            event_bus=smoke_mock_event_bus,
            clob_client=smoke_mock_clob,
        )
        await engine.start()

        # Semaphore should be initialized with max_concurrent
        assert engine._execution_semaphore._value == 3
        assert engine._max_concurrent == 3

        await engine.stop()

    @pytest.mark.asyncio
    async def test_health_check(
        self, smoke_mock_config, smoke_mock_event_bus, smoke_mock_clob
    ):
        """Verify health check provides queue info."""
        engine = ExecutionEngine(
            config=smoke_mock_config,
            event_bus=smoke_mock_event_bus,
            clob_client=smoke_mock_clob,
        )
        await engine.start()

        health = await engine.health_check()

        assert health.status.value == "healthy"
        assert "queue_size" in health.details
        assert "active_executions" in health.details
        assert "max_concurrent" in health.details

        await engine.stop()
