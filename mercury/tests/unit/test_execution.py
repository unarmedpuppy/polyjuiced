"""
Unit tests for ExecutionEngine.

Tests the core ExecutionEngine functionality including:
- Lifecycle (start/stop)
- Queue management
- Concurrent execution limits
- Signal processing
- Health checks
"""
import asyncio
import pytest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from mercury.services.execution import (
    ExecutionEngine,
    ExecutionResult,
    ExecutionSignal,
    QueuedSignal,
    QueuedSignalStatus,
)
from mercury.domain.signal import SignalType, SignalPriority


@pytest.fixture
def mock_config():
    """Create mock config with queue settings."""
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
def mock_event_bus():
    """Create mock event bus."""
    bus = MagicMock()
    bus.publish = AsyncMock()
    bus.subscribe = AsyncMock()
    bus.unsubscribe = AsyncMock()
    return bus


@pytest.fixture
def mock_clob():
    """Create mock CLOB client."""
    clob = MagicMock()
    clob.connect = AsyncMock()
    clob.close = AsyncMock()
    clob.cancel_all_orders = AsyncMock()
    clob._connected = False
    return clob


@pytest.fixture
def execution_engine(mock_config, mock_event_bus, mock_clob):
    """Create ExecutionEngine instance for testing."""
    return ExecutionEngine(
        config=mock_config,
        event_bus=mock_event_bus,
        clob_client=mock_clob,
    )


class TestExecutionEngineLifecycle:
    """Test start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_initializes_components(self, execution_engine, mock_event_bus):
        """Verify start() initializes all components."""
        await execution_engine.start()

        assert execution_engine.is_running
        assert execution_engine._execution_semaphore is not None
        assert execution_engine._queue_processor_task is not None
        mock_event_bus.subscribe.assert_called_once()

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_stop_cleans_up(self, execution_engine):
        """Verify stop() cleans up properly."""
        await execution_engine.start()
        await execution_engine.stop()

        assert not execution_engine.is_running

    @pytest.mark.asyncio
    async def test_double_start_is_safe(self, execution_engine):
        """Verify calling start() twice is safe."""
        await execution_engine.start()
        await execution_engine.start()  # Should not raise

        assert execution_engine.is_running
        await execution_engine.stop()


class TestQueueManagement:
    """Test queue operations."""

    @pytest.mark.asyncio
    async def test_queue_signal(self, execution_engine, mock_event_bus):
        """Verify signals can be queued."""
        await execution_engine.start()

        signal_data = {
            "signal_id": "test-signal-1",
            "market_id": "test-market",
            "signal_type": "ARBITRAGE",
            "target_size_usd": "100.0",
            "yes_price": "0.48",
            "no_price": "0.50",
        }

        result = await execution_engine.queue_signal(
            "test-signal-1",
            signal_data,
            SignalPriority.HIGH,
        )

        assert result is True
        assert execution_engine.get_queue_size() == 1
        assert execution_engine._total_queued == 1

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_duplicate_signal_rejected(self, execution_engine, mock_event_bus):
        """Verify duplicate signals are rejected."""
        await execution_engine.start()

        signal_data = {"signal_id": "test-signal-1", "market_id": "test"}

        await execution_engine.queue_signal("test-signal-1", signal_data)
        result = await execution_engine.queue_signal("test-signal-1", signal_data)

        assert result is False
        assert execution_engine.get_queue_size() == 1

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_get_queue_stats(self, execution_engine):
        """Verify queue stats are accurate."""
        await execution_engine.start()

        stats = execution_engine.get_queue_stats()

        assert "queue_size" in stats
        assert "active_executions" in stats
        assert "max_concurrent" in stats
        assert "total_queued" in stats
        assert stats["max_concurrent"] == 3

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_cancel_queued_signal(self, execution_engine):
        """Verify signals can be cancelled before execution."""
        await execution_engine.start()

        signal_data = {"signal_id": "test-signal-1", "market_id": "test"}
        await execution_engine.queue_signal("test-signal-1", signal_data)

        result = await execution_engine.cancel_queued_signal("test-signal-1")

        assert result is True
        assert "test-signal-1" not in execution_engine._queue_items

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_signal(self, execution_engine):
        """Verify cancelling nonexistent signal returns False."""
        await execution_engine.start()

        result = await execution_engine.cancel_queued_signal("nonexistent")

        assert result is False

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_is_queue_full(self, mock_config):
        """Verify queue full detection."""
        # Override config to have tiny queue
        mock_config.get_int.side_effect = lambda key, default: {
            "execution.max_concurrent": 1,
            "execution.max_queue_size": 2,
        }.get(key, default)
        mock_config.get_float.return_value = 60.0

        mock_clob = MagicMock()
        mock_clob.close = AsyncMock()
        mock_clob.connect = AsyncMock()

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=MagicMock(publish=AsyncMock(), subscribe=AsyncMock()),
            clob_client=mock_clob,
        )
        await engine.start()

        await engine.queue_signal("sig-1", {"signal_id": "sig-1"})
        await engine.queue_signal("sig-2", {"signal_id": "sig-2"})

        assert engine.is_queue_full() is True

        await engine.stop()


class TestPriorityOrdering:
    """Test priority-based queue ordering."""

    def test_queued_signal_priority_comparison(self):
        """Verify QueuedSignal ordering by priority."""
        critical = QueuedSignal(
            signal_id="1",
            signal_data={},
            priority=SignalPriority.CRITICAL,
        )
        high = QueuedSignal(
            signal_id="2",
            signal_data={},
            priority=SignalPriority.HIGH,
        )
        medium = QueuedSignal(
            signal_id="3",
            signal_data={},
            priority=SignalPriority.MEDIUM,
        )
        low = QueuedSignal(
            signal_id="4",
            signal_data={},
            priority=SignalPriority.LOW,
        )

        # Critical < High < Medium < Low (lower = higher priority)
        assert critical < high
        assert high < medium
        assert medium < low

    def test_same_priority_fifo(self):
        """Verify same priority signals ordered by queue time."""
        import time

        first = QueuedSignal(
            signal_id="1",
            signal_data={},
            priority=SignalPriority.HIGH,
        )
        time.sleep(0.01)
        second = QueuedSignal(
            signal_id="2",
            signal_data={},
            priority=SignalPriority.HIGH,
        )

        assert first < second


class TestConcurrentExecutionLimits:
    """Test concurrent execution limiting."""

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrent(self, mock_config, mock_event_bus):
        """Verify semaphore limits concurrent executions."""
        # Set max concurrent to 2
        mock_config.get_int.side_effect = lambda key, default: {
            "execution.max_concurrent": 2,
            "execution.max_queue_size": 100,
        }.get(key, default)
        mock_config.get_float.return_value = 60.0

        mock_clob = MagicMock()
        mock_clob.close = AsyncMock()
        mock_clob.connect = AsyncMock()

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )

        await engine.start()

        # Semaphore should be initialized with max_concurrent=2
        assert engine._execution_semaphore._value == 2

        await engine.stop()


class TestHealthCheck:
    """Test health check functionality."""

    @pytest.mark.asyncio
    async def test_healthy_in_dry_run(self, execution_engine):
        """Verify healthy status in dry run mode."""
        await execution_engine.start()

        result = await execution_engine.health_check()

        assert result.status.value == "healthy"
        assert "dry-run" in result.message.lower()
        assert "queue_size" in result.details
        assert "active_executions" in result.details

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_health_includes_queue_metrics(self, execution_engine):
        """Verify health check includes queue metrics."""
        await execution_engine.start()

        await execution_engine.queue_signal("test-1", {"signal_id": "test-1"})

        result = await execution_engine.health_check()

        assert result.details["queue_size"] == 1
        assert result.details["total_queued"] == 1

        await execution_engine.stop()


class TestSignalExecution:
    """Test signal execution."""

    @pytest.mark.asyncio
    async def test_dry_run_execution(self, execution_engine, mock_event_bus):
        """Verify dry run execution simulates successfully."""
        await execution_engine.start()

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

        result = await execution_engine.execute(signal)

        assert result.success is True
        assert result.signal_id == "test-signal"
        assert result.yes_filled > 0
        assert result.no_filled > 0
        assert result.execution_time_ms is not None

        # Verify events published
        mock_event_bus.publish.assert_called()

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_event_driven_execution(self, execution_engine, mock_event_bus):
        """Verify signals from events are processed."""
        await execution_engine.start()

        # Wait briefly for queue processor to start
        await asyncio.sleep(0.1)

        # Simulate receiving an approved signal event
        signal_data = {
            "signal_id": "event-signal",
            "market_id": "test-market",
            "signal_type": "ARBITRAGE",
            "target_size_usd": "50.0",
            "yes_price": "0.45",
            "no_price": "0.53",
            "yes_token_id": "yes-token",
            "no_token_id": "no-token",
            "priority": "high",
        }

        await execution_engine._on_approved_signal(signal_data)

        # Signal should be queued
        assert execution_engine._total_queued == 1

        # Give time for execution (dry run is fast)
        await asyncio.sleep(0.3)

        await execution_engine.stop()


class TestExecutionResult:
    """Test ExecutionResult dataclass."""

    def test_execution_result_defaults(self):
        """Verify ExecutionResult default values."""
        result = ExecutionResult(
            success=True,
            signal_id="test",
        )

        assert result.success is True
        assert result.signal_id == "test"
        assert result.trade_id is None
        assert result.yes_filled == Decimal("0")
        assert result.no_filled == Decimal("0")
        assert result.error is None


class TestQueuedSignalStatus:
    """Test QueuedSignal status tracking."""

    def test_status_transitions(self):
        """Verify status can be changed."""
        signal = QueuedSignal(
            signal_id="test",
            signal_data={},
            priority=SignalPriority.MEDIUM,
        )

        assert signal.status == QueuedSignalStatus.PENDING

        signal.status = QueuedSignalStatus.EXECUTING
        assert signal.status == QueuedSignalStatus.EXECUTING

        signal.status = QueuedSignalStatus.COMPLETED
        assert signal.status == QueuedSignalStatus.COMPLETED
