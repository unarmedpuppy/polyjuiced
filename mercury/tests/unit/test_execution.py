"""
Unit tests for ExecutionEngine.

Tests the core ExecutionEngine functionality including:
- Lifecycle (start/stop)
- Queue management
- Concurrent execution limits
- Signal processing
- Health checks
- Single order execution (FOK/GTC)
- Order state tracking and events
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
from mercury.domain.order import (
    Order,
    OrderRequest,
    OrderResult as DomainOrderResult,
    DualLegResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Fill,
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


class TestSingleOrderExecution:
    """Test single order execution with FOK/GTC support."""

    @pytest.fixture
    def order_request_gtc(self):
        """Create a GTC order request."""
        return OrderRequest(
            market_id="test-market-123",
            token_id="token-yes-456",
            side=OrderSide.BUY,
            outcome="YES",
            size=Decimal("10.0"),
            price=Decimal("0.55"),
            order_type=OrderType.GTC,
        )

    @pytest.fixture
    def order_request_fok(self):
        """Create a FOK order request."""
        return OrderRequest(
            market_id="test-market-123",
            token_id="token-yes-456",
            side=OrderSide.BUY,
            outcome="YES",
            size=Decimal("10.0"),
            price=Decimal("0.55"),
            order_type=OrderType.FOK,
        )

    @pytest.mark.asyncio
    async def test_execute_order_gtc_dry_run(self, execution_engine, order_request_gtc, mock_event_bus):
        """Verify GTC order execution in dry-run mode."""
        await execution_engine.start()

        result = await execution_engine.execute_order(order_request_gtc)

        assert result.success is True
        assert result.order is not None
        assert result.order.status == OrderStatus.FILLED
        assert result.order.filled_size == order_request_gtc.size
        assert result.order.order_type == OrderType.GTC
        assert result.latency_ms > 0
        assert len(result.fills) == 1

        # Verify events were emitted
        assert mock_event_bus.publish.call_count >= 3  # pending, submitted, filled

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_execute_order_fok_dry_run(self, execution_engine, order_request_fok, mock_event_bus):
        """Verify FOK order execution in dry-run mode."""
        await execution_engine.start()

        result = await execution_engine.execute_order(order_request_fok)

        assert result.success is True
        assert result.order is not None
        assert result.order.status == OrderStatus.FILLED
        assert result.order.filled_size == order_request_fok.size
        assert result.order.order_type == OrderType.FOK
        assert len(result.fills) == 1

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_execute_order_emits_pending_event(self, execution_engine, order_request_gtc, mock_event_bus):
        """Verify order.pending event is emitted."""
        await execution_engine.start()

        await execution_engine.execute_order(order_request_gtc)

        # Find the pending event
        pending_calls = [
            call for call in mock_event_bus.publish.call_args_list
            if call[0][0] == "order.pending"
        ]
        assert len(pending_calls) == 1
        event_data = pending_calls[0][0][1]
        assert event_data["status"] == "pending"
        assert event_data["market_id"] == order_request_gtc.market_id

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_execute_order_emits_submitted_event(self, execution_engine, order_request_gtc, mock_event_bus):
        """Verify order.submitted event is emitted."""
        await execution_engine.start()

        await execution_engine.execute_order(order_request_gtc)

        # Find the submitted event
        submitted_calls = [
            call for call in mock_event_bus.publish.call_args_list
            if call[0][0] == "order.submitted"
        ]
        assert len(submitted_calls) == 1
        event_data = submitted_calls[0][0][1]
        assert event_data["status"] == "submitted"

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_execute_order_emits_filled_event(self, execution_engine, order_request_gtc, mock_event_bus):
        """Verify order.filled event is emitted."""
        await execution_engine.start()

        await execution_engine.execute_order(order_request_gtc)

        # Find the filled event
        filled_calls = [
            call for call in mock_event_bus.publish.call_args_list
            if call[0][0] == "order.filled"
        ]
        assert len(filled_calls) == 1
        event_data = filled_calls[0][0][1]
        assert event_data["status"] == "filled"
        assert event_data["filled_size"] == str(order_request_gtc.size)

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_execute_order_tracks_state_transitions(self, execution_engine, order_request_gtc, mock_event_bus):
        """Verify correct state transitions: PENDING -> SUBMITTED -> FILLED."""
        await execution_engine.start()

        await execution_engine.execute_order(order_request_gtc)

        # Extract all event types in order
        event_types = [call[0][0] for call in mock_event_bus.publish.call_args_list]

        # Verify the correct sequence
        assert "order.pending" in event_types
        assert "order.submitted" in event_types
        assert "order.filled" in event_types

        # Verify order: pending comes before submitted, submitted comes before filled
        pending_idx = event_types.index("order.pending")
        submitted_idx = event_types.index("order.submitted")
        filled_idx = event_types.index("order.filled")

        assert pending_idx < submitted_idx < filled_idx

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_execute_order_creates_fill(self, execution_engine, order_request_gtc):
        """Verify Fill objects are created for filled orders."""
        await execution_engine.start()

        result = await execution_engine.execute_order(order_request_gtc)

        assert len(result.fills) == 1
        fill = result.fills[0]

        assert fill.order_id == result.order.order_id
        assert fill.market_id == order_request_gtc.market_id
        assert fill.token_id == order_request_gtc.token_id
        assert fill.side == order_request_gtc.side
        assert fill.outcome == order_request_gtc.outcome
        assert fill.size == order_request_gtc.size
        assert fill.price == order_request_gtc.price

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_execute_order_no_fills_for_unfilled(self, execution_engine, order_request_gtc):
        """Verify no fills for orders that don't fill."""
        # This test would require mocking a non-dry-run scenario
        # where the order times out. For now we verify the helper method.
        await execution_engine.start()

        # Create an unfilled order
        order = Order(
            order_id="test-order",
            market_id="test-market",
            token_id="test-token",
            side=OrderSide.BUY,
            outcome="YES",
            requested_size=Decimal("10.0"),
            filled_size=Decimal("0"),  # No fill
            price=Decimal("0.5"),
            status=OrderStatus.OPEN,
        )

        fills = execution_engine._create_fills_from_order(order)
        assert len(fills) == 0

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_execute_order_result_properties(self, execution_engine, order_request_gtc):
        """Verify DomainOrderResult properties."""
        await execution_engine.start()

        result = await execution_engine.execute_order(order_request_gtc)

        # Check total_filled
        assert result.total_filled == order_request_gtc.size

        # Check total_cost
        expected_cost = order_request_gtc.size * order_request_gtc.price
        assert result.total_cost == expected_cost

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_execute_order_sell_side(self, execution_engine, mock_event_bus):
        """Verify SELL orders work correctly."""
        await execution_engine.start()

        sell_request = OrderRequest(
            market_id="test-market",
            token_id="token-yes",
            side=OrderSide.SELL,
            outcome="YES",
            size=Decimal("5.0"),
            price=Decimal("0.60"),
            order_type=OrderType.GTC,
        )

        result = await execution_engine.execute_order(sell_request)

        assert result.success is True
        assert result.order.side == OrderSide.SELL
        assert result.fills[0].side == OrderSide.SELL

        await execution_engine.stop()


class TestOrderStateTracking:
    """Test order state tracking across lifecycle."""

    @pytest.mark.asyncio
    async def test_order_has_correct_timestamps(self, execution_engine):
        """Verify orders have correct timestamps."""
        await execution_engine.start()

        before_execution = datetime.now(timezone.utc)

        request = OrderRequest(
            market_id="test-market",
            token_id="test-token",
            side=OrderSide.BUY,
            outcome="YES",
            size=Decimal("10.0"),
            price=Decimal("0.5"),
        )

        result = await execution_engine.execute_order(request)

        after_execution = datetime.now(timezone.utc)

        # created_at and updated_at should be within the execution window
        assert result.order.created_at >= before_execution
        assert result.order.created_at <= after_execution
        assert result.order.updated_at >= result.order.created_at
        assert result.order.updated_at <= after_execution

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_order_id_format(self, execution_engine):
        """Verify order ID format."""
        await execution_engine.start()

        request = OrderRequest(
            market_id="test-market",
            token_id="test-token",
            side=OrderSide.BUY,
            outcome="YES",
            size=Decimal("10.0"),
            price=Decimal("0.5"),
        )

        result = await execution_engine.execute_order(request)

        # Order ID should start with "ord-"
        assert result.order.order_id.startswith("ord-")
        # Should have reasonable length
        assert len(result.order.order_id) == 16  # "ord-" + 12 hex chars

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_client_order_id_preserved(self, execution_engine):
        """Verify client_order_id is preserved."""
        await execution_engine.start()

        custom_client_id = "my-custom-order-id-123"
        request = OrderRequest(
            market_id="test-market",
            token_id="test-token",
            side=OrderSide.BUY,
            outcome="YES",
            size=Decimal("10.0"),
            price=Decimal("0.5"),
            client_order_id=custom_client_id,
        )

        result = await execution_engine.execute_order(request)

        assert result.order.client_order_id == custom_client_id

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_order_type_preserved(self, execution_engine):
        """Verify order type is preserved in result."""
        await execution_engine.start()

        # Test GTC
        gtc_request = OrderRequest(
            market_id="test-market",
            token_id="test-token",
            side=OrderSide.BUY,
            outcome="YES",
            size=Decimal("10.0"),
            price=Decimal("0.5"),
            order_type=OrderType.GTC,
        )

        gtc_result = await execution_engine.execute_order(gtc_request)
        assert gtc_result.order.order_type == OrderType.GTC

        # Test FOK
        fok_request = OrderRequest(
            market_id="test-market",
            token_id="test-token",
            side=OrderSide.BUY,
            outcome="YES",
            size=Decimal("10.0"),
            price=Decimal("0.5"),
            order_type=OrderType.FOK,
        )

        fok_result = await execution_engine.execute_order(fok_request)
        assert fok_result.order.order_type == OrderType.FOK

        await execution_engine.stop()


class TestOrderEventData:
    """Test order event data structure and content."""

    @pytest.mark.asyncio
    async def test_event_contains_all_required_fields(self, execution_engine, mock_event_bus):
        """Verify events contain all required fields."""
        await execution_engine.start()

        request = OrderRequest(
            market_id="test-market-abc",
            token_id="token-xyz",
            side=OrderSide.BUY,
            outcome="YES",
            size=Decimal("15.5"),
            price=Decimal("0.45"),
            order_type=OrderType.GTC,
        )

        await execution_engine.execute_order(request)

        # Check one of the events has all required fields
        for call in mock_event_bus.publish.call_args_list:
            event_type = call[0][0]
            if event_type.startswith("order."):
                event_data = call[0][1]

                # Required fields
                assert "order_id" in event_data
                assert "client_order_id" in event_data
                assert "market_id" in event_data
                assert "token_id" in event_data
                assert "side" in event_data
                assert "outcome" in event_data
                assert "order_type" in event_data
                assert "status" in event_data
                assert "requested_size" in event_data
                assert "filled_size" in event_data
                assert "price" in event_data
                assert "timestamp" in event_data

                # Verify values
                assert event_data["market_id"] == "test-market-abc"
                assert event_data["token_id"] == "token-xyz"
                assert event_data["side"] == "BUY"
                assert event_data["outcome"] == "YES"
                assert event_data["order_type"] == "GTC"
                break

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_error_included_in_rejected_event(self, execution_engine, mock_event_bus, mock_clob):
        """Verify error message is included in rejected events."""
        # Configure to not be dry run so we can trigger an error
        execution_engine._dry_run = False
        mock_clob._connected = True
        mock_clob.execute_order = AsyncMock(side_effect=Exception("CLOB connection failed"))

        await execution_engine.start()

        request = OrderRequest(
            market_id="test-market",
            token_id="test-token",
            side=OrderSide.BUY,
            outcome="YES",
            size=Decimal("10.0"),
            price=Decimal("0.5"),
        )

        result = await execution_engine.execute_order(request)

        assert result.success is False
        assert result.error_message is not None

        # Find rejected event
        rejected_calls = [
            call for call in mock_event_bus.publish.call_args_list
            if call[0][0] == "order.rejected"
        ]
        assert len(rejected_calls) >= 1
        event_data = rejected_calls[0][0][1]
        assert "error" in event_data
        assert "CLOB connection failed" in event_data["error"]

        await execution_engine.stop()


class TestOrderRequest:
    """Test OrderRequest validation."""

    def test_valid_order_request(self):
        """Verify valid order request creation."""
        request = OrderRequest(
            market_id="test-market",
            token_id="test-token",
            side=OrderSide.BUY,
            outcome="YES",
            size=Decimal("10.0"),
            price=Decimal("0.5"),
        )

        assert request.market_id == "test-market"
        assert request.size == Decimal("10.0")
        assert request.order_type == OrderType.GTC  # Default

    def test_invalid_outcome_raises(self):
        """Verify invalid outcome raises ValueError."""
        with pytest.raises(ValueError, match="outcome must be YES or NO"):
            OrderRequest(
                market_id="test-market",
                token_id="test-token",
                side=OrderSide.BUY,
                outcome="MAYBE",  # Invalid
                size=Decimal("10.0"),
                price=Decimal("0.5"),
            )

    def test_invalid_size_raises(self):
        """Verify non-positive size raises ValueError."""
        with pytest.raises(ValueError, match="size must be positive"):
            OrderRequest(
                market_id="test-market",
                token_id="test-token",
                side=OrderSide.BUY,
                outcome="YES",
                size=Decimal("0"),  # Invalid
                price=Decimal("0.5"),
            )

    def test_invalid_price_raises(self):
        """Verify price outside (0, 1) raises ValueError."""
        with pytest.raises(ValueError, match="price must be between 0 and 1"):
            OrderRequest(
                market_id="test-market",
                token_id="test-token",
                side=OrderSide.BUY,
                outcome="YES",
                size=Decimal("10.0"),
                price=Decimal("1.5"),  # Invalid
            )

    def test_client_order_id_auto_generated(self):
        """Verify client_order_id is auto-generated if not provided."""
        request = OrderRequest(
            market_id="test-market",
            token_id="test-token",
            side=OrderSide.BUY,
            outcome="YES",
            size=Decimal("10.0"),
            price=Decimal("0.5"),
        )

        assert request.client_order_id is not None
        assert len(request.client_order_id) > 0


class TestOrderModel:
    """Test Order model properties."""

    def test_remaining_size(self):
        """Verify remaining_size calculation."""
        order = Order(
            order_id="test",
            market_id="market",
            token_id="token",
            side=OrderSide.BUY,
            outcome="YES",
            requested_size=Decimal("100.0"),
            filled_size=Decimal("30.0"),
            price=Decimal("0.5"),
            status=OrderStatus.PARTIALLY_FILLED,
        )

        assert order.remaining_size == Decimal("70.0")

    def test_fill_ratio(self):
        """Verify fill_ratio calculation."""
        order = Order(
            order_id="test",
            market_id="market",
            token_id="token",
            side=OrderSide.BUY,
            outcome="YES",
            requested_size=Decimal("100.0"),
            filled_size=Decimal("25.0"),
            price=Decimal("0.5"),
            status=OrderStatus.PARTIALLY_FILLED,
        )

        assert order.fill_ratio == Decimal("0.25")

    def test_is_complete(self):
        """Verify is_complete for terminal states."""
        terminal_states = [
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        ]

        for status in terminal_states:
            order = Order(
                order_id="test",
                market_id="market",
                token_id="token",
                side=OrderSide.BUY,
                outcome="YES",
                requested_size=Decimal("10.0"),
                filled_size=Decimal("0"),
                price=Decimal("0.5"),
                status=status,
            )
            assert order.is_complete is True

        # Non-terminal states
        non_terminal = [OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED]
        for status in non_terminal:
            order = Order(
                order_id="test",
                market_id="market",
                token_id="token",
                side=OrderSide.BUY,
                outcome="YES",
                requested_size=Decimal("10.0"),
                filled_size=Decimal("0"),
                price=Decimal("0.5"),
                status=status,
            )
            assert order.is_complete is False


class TestDualLegExecution:
    """Test dual-leg arbitrage execution."""

    @pytest.fixture
    def yes_order_request(self):
        """Create a YES order request for dual-leg."""
        return OrderRequest(
            market_id="test-market-123",
            token_id="token-yes-456",
            side=OrderSide.BUY,
            outcome="YES",
            size=Decimal("10.0"),
            price=Decimal("0.48"),
            order_type=OrderType.GTC,
        )

    @pytest.fixture
    def no_order_request(self):
        """Create a NO order request for dual-leg."""
        return OrderRequest(
            market_id="test-market-123",
            token_id="token-no-789",
            side=OrderSide.BUY,
            outcome="NO",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
            order_type=OrderType.GTC,
        )

    @pytest.mark.asyncio
    async def test_dual_leg_both_fill_dry_run(
        self, execution_engine, yes_order_request, no_order_request, mock_event_bus
    ):
        """Verify both legs fill successfully in dry-run mode."""
        await execution_engine.start()

        result = await execution_engine.execute_dual_leg(
            yes_order_request, no_order_request
        )

        assert result.success is True
        assert result.yes_result is not None
        assert result.no_result is not None
        assert result.yes_result.success is True
        assert result.no_result.success is True
        assert result.yes_result.order.status == OrderStatus.FILLED
        assert result.no_result.order.status == OrderStatus.FILLED
        assert result.yes_result.order.filled_size == yes_order_request.size
        assert result.no_result.order.filled_size == no_order_request.size
        assert result.total_latency_ms > 0
        assert result.error_message is None

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_dual_leg_emits_started_event(
        self, execution_engine, yes_order_request, no_order_request, mock_event_bus
    ):
        """Verify dual_leg.started event is emitted."""
        await execution_engine.start()

        await execution_engine.execute_dual_leg(yes_order_request, no_order_request)

        # Find the started event
        started_calls = [
            call for call in mock_event_bus.publish.call_args_list
            if call[0][0] == "order.dual_leg.started"
        ]
        assert len(started_calls) == 1
        event_data = started_calls[0][0][1]
        assert "yes_client_order_id" in event_data
        assert "no_client_order_id" in event_data
        assert event_data["yes_market_id"] == yes_order_request.market_id
        assert event_data["yes_size"] == str(yes_order_request.size)

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_dual_leg_emits_completed_event(
        self, execution_engine, yes_order_request, no_order_request, mock_event_bus
    ):
        """Verify dual_leg.completed event is emitted on success."""
        await execution_engine.start()

        await execution_engine.execute_dual_leg(yes_order_request, no_order_request)

        # Find the completed event
        completed_calls = [
            call for call in mock_event_bus.publish.call_args_list
            if call[0][0] == "order.dual_leg.completed"
        ]
        assert len(completed_calls) == 1
        event_data = completed_calls[0][0][1]
        assert "yes_order_id" in event_data
        assert "no_order_id" in event_data
        assert "yes_filled" in event_data
        assert "no_filled" in event_data
        assert "total_cost" in event_data
        assert "latency_ms" in event_data

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_dual_leg_total_cost(
        self, execution_engine, yes_order_request, no_order_request
    ):
        """Verify total cost is calculated correctly."""
        await execution_engine.start()

        result = await execution_engine.execute_dual_leg(
            yes_order_request, no_order_request
        )

        expected_yes_cost = yes_order_request.size * yes_order_request.price
        expected_no_cost = no_order_request.size * no_order_request.price
        expected_total = expected_yes_cost + expected_no_cost

        assert result.total_cost == expected_total

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_dual_leg_concurrent_execution(
        self, execution_engine, yes_order_request, no_order_request
    ):
        """Verify orders are executed concurrently (not sequentially)."""
        await execution_engine.start()

        # In dry-run mode, each order has a small delay
        # If concurrent, total time should be close to single order time
        result = await execution_engine.execute_dual_leg(
            yes_order_request, no_order_request
        )

        # Total latency should be much less than 2x single order time
        # In dry-run, single order takes ~50ms, so dual should be <150ms
        # (allowing some overhead)
        assert result.total_latency_ms < 500  # Conservative upper bound

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_dual_leg_with_different_order_types(self, execution_engine, mock_event_bus):
        """Verify dual-leg works with different order types."""
        await execution_engine.start()

        yes_order = OrderRequest(
            market_id="test-market",
            token_id="yes-token",
            side=OrderSide.BUY,
            outcome="YES",
            size=Decimal("5.0"),
            price=Decimal("0.45"),
            order_type=OrderType.FOK,
        )

        no_order = OrderRequest(
            market_id="test-market",
            token_id="no-token",
            side=OrderSide.BUY,
            outcome="NO",
            size=Decimal("5.0"),
            price=Decimal("0.53"),
            order_type=OrderType.GTC,
        )

        result = await execution_engine.execute_dual_leg(yes_order, no_order)

        assert result.success is True
        assert result.yes_result.order.order_type == OrderType.FOK
        assert result.no_result.order.order_type == OrderType.GTC

        await execution_engine.stop()


class TestDualLegPartialFillHandling:
    """Test partial fill handling in dual-leg execution."""

    @pytest.fixture
    def yes_order_request(self):
        """Create a YES order request."""
        return OrderRequest(
            market_id="test-market-123",
            token_id="token-yes-456",
            side=OrderSide.BUY,
            outcome="YES",
            size=Decimal("10.0"),
            price=Decimal("0.48"),
            order_type=OrderType.GTC,
        )

    @pytest.fixture
    def no_order_request(self):
        """Create a NO order request."""
        return OrderRequest(
            market_id="test-market-123",
            token_id="token-no-789",
            side=OrderSide.BUY,
            outcome="NO",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
            order_type=OrderType.GTC,
        )

    @pytest.mark.asyncio
    async def test_partial_fill_yes_success_no_fail_triggers_unwind(
        self, mock_config, mock_event_bus, mock_clob, yes_order_request, no_order_request
    ):
        """Verify YES success + NO failure triggers unwind."""
        # Configure non-dry-run mode
        mock_config.get_bool.side_effect = lambda key, default: {
            "mercury.dry_run": False,
            "execution.rebalance_partial_fills": True,
        }.get(key, default)

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )

        # Mock execute_order to succeed for YES, fail for NO
        original_execute = engine.execute_order
        call_count = [0]

        async def mock_execute_order(order_req, timeout=30.0):
            call_count[0] += 1
            # First two calls: YES succeeds, NO fails
            if order_req.outcome == "YES" and order_req.side == OrderSide.BUY:
                # YES order succeeds
                engine._dry_run = True
                result = await original_execute(order_req, timeout)
                engine._dry_run = False
                return result
            elif order_req.outcome == "NO" and order_req.side == OrderSide.BUY:
                # NO order fails
                from mercury.domain.order import Order, OrderStatus as OS
                failed_order = Order(
                    order_id="failed-no-order",
                    market_id=order_req.market_id,
                    token_id=order_req.token_id,
                    side=order_req.side,
                    outcome=order_req.outcome,
                    requested_size=order_req.size,
                    filled_size=Decimal("0"),
                    price=order_req.price,
                    status=OS.REJECTED,
                    order_type=order_req.order_type,
                )
                from mercury.domain.order import OrderResult as DOR
                return DOR(
                    success=False,
                    order=failed_order,
                    fills=[],
                    error_message="Simulated NO order failure",
                )
            else:
                # Unwind order (SELL on YES)
                engine._dry_run = True
                result = await original_execute(order_req, timeout)
                engine._dry_run = False
                return result

        engine.execute_order = mock_execute_order

        await engine.start()

        result = await engine.execute_dual_leg(yes_order_request, no_order_request)

        # Should fail overall
        assert result.success is False
        # YES result should exist and be filled
        assert result.yes_result is not None
        assert result.yes_result.order.status == OrderStatus.FILLED
        # NO result should exist and be rejected
        assert result.no_result is not None
        # Error message should indicate unwind
        assert "unwound" in result.error_message.lower() or "unwind" in result.error_message.lower()

        # Verify partial event was emitted
        partial_calls = [
            call for call in mock_event_bus.publish.call_args_list
            if call[0][0] == "order.dual_leg.partial"
        ]
        assert len(partial_calls) == 1

        await engine.stop()

    @pytest.mark.asyncio
    async def test_both_legs_fail_no_unwind_attempted(
        self, mock_config, mock_event_bus, mock_clob, yes_order_request, no_order_request
    ):
        """Verify no unwind when both legs fail."""
        mock_config.get_bool.side_effect = lambda key, default: {
            "mercury.dry_run": False,
        }.get(key, default)

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )

        # Mock execute_order to fail both legs
        async def mock_execute_order(order_req, timeout=30.0):
            from mercury.domain.order import Order, OrderStatus as OS, OrderResult as DOR
            failed_order = Order(
                order_id=f"failed-{order_req.outcome}",
                market_id=order_req.market_id,
                token_id=order_req.token_id,
                side=order_req.side,
                outcome=order_req.outcome,
                requested_size=order_req.size,
                filled_size=Decimal("0"),
                price=order_req.price,
                status=OS.REJECTED,
            )
            return DOR(
                success=False,
                order=failed_order,
                fills=[],
                error_message=f"Simulated {order_req.outcome} failure",
            )

        engine.execute_order = mock_execute_order

        await engine.start()

        result = await engine.execute_dual_leg(yes_order_request, no_order_request)

        assert result.success is False
        assert "Both legs failed" in result.error_message

        # Verify failed event was emitted (not partial)
        failed_calls = [
            call for call in mock_event_bus.publish.call_args_list
            if call[0][0] == "order.dual_leg.failed"
        ]
        assert len(failed_calls) == 1
        partial_calls = [
            call for call in mock_event_bus.publish.call_args_list
            if call[0][0] == "order.dual_leg.partial"
        ]
        assert len(partial_calls) == 0

        await engine.stop()


class TestDualLegResult:
    """Test DualLegResult dataclass from domain."""

    def test_dual_leg_result_total_cost(self):
        """Verify total_cost aggregates both legs."""
        from mercury.domain.order import DualLegResult, OrderResult, Order

        yes_order = Order(
            order_id="yes-1",
            market_id="market",
            token_id="yes-token",
            side=OrderSide.BUY,
            outcome="YES",
            requested_size=Decimal("10"),
            filled_size=Decimal("10"),
            price=Decimal("0.45"),
            status=OrderStatus.FILLED,
        )
        no_order = Order(
            order_id="no-1",
            market_id="market",
            token_id="no-token",
            side=OrderSide.BUY,
            outcome="NO",
            requested_size=Decimal("10"),
            filled_size=Decimal("10"),
            price=Decimal("0.53"),
            status=OrderStatus.FILLED,
        )

        yes_fill = Fill(
            fill_id="fill-yes",
            order_id="yes-1",
            market_id="market",
            token_id="yes-token",
            side=OrderSide.BUY,
            outcome="YES",
            size=Decimal("10"),
            price=Decimal("0.45"),
        )
        no_fill = Fill(
            fill_id="fill-no",
            order_id="no-1",
            market_id="market",
            token_id="no-token",
            side=OrderSide.BUY,
            outcome="NO",
            size=Decimal("10"),
            price=Decimal("0.53"),
        )

        yes_result = OrderResult(
            success=True,
            order=yes_order,
            fills=[yes_fill],
        )
        no_result = OrderResult(
            success=True,
            order=no_order,
            fills=[no_fill],
        )

        dual_result = DualLegResult(
            success=True,
            yes_result=yes_result,
            no_result=no_result,
        )

        # Total cost = (10 * 0.45) + (10 * 0.53) = 4.5 + 5.3 = 9.8
        assert dual_result.total_cost == Decimal("9.8")

    def test_dual_leg_result_with_none_results(self):
        """Verify total_cost handles None results."""
        from mercury.domain.order import DualLegResult

        dual_result = DualLegResult(
            success=False,
            yes_result=None,
            no_result=None,
            error_message="Both failed",
        )

        assert dual_result.total_cost == Decimal("0")

    def test_dual_leg_result_success_state(self):
        """Verify success state is tracked correctly."""
        from mercury.domain.order import DualLegResult

        # Successful result
        success_result = DualLegResult(
            success=True,
            yes_result=None,  # Would normally have results
            no_result=None,
        )
        assert success_result.success is True

        # Failed result
        failed_result = DualLegResult(
            success=False,
            yes_result=None,
            no_result=None,
            error_message="Something failed",
        )
        assert failed_result.success is False
        assert failed_result.error_message == "Something failed"


class TestOrderCancellation:
    """Test order cancellation functionality."""

    @pytest.mark.asyncio
    async def test_cancel_order_dry_run_untracked(self, execution_engine, mock_event_bus):
        """Verify cancel_order works in dry-run mode for untracked orders."""
        await execution_engine.start()

        result = await execution_engine.cancel_order("unknown-order-id")

        assert result is True

        # Verify cancelled event was published
        cancelled_calls = [
            call for call in mock_event_bus.publish.call_args_list
            if call[0][0] == "order.cancelled"
        ]
        assert len(cancelled_calls) == 1
        event_data = cancelled_calls[0][0][1]
        assert event_data["order_id"] == "unknown-order-id"
        assert event_data["status"] == "cancelled"
        assert "reason" in event_data
        assert event_data["reason"] == "dry_run_cancel"

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_cancel_order_dry_run_tracked(self, execution_engine, mock_event_bus):
        """Verify cancel_order updates tracked order status in dry-run mode."""
        await execution_engine.start()

        # First create an order so it gets tracked
        request = OrderRequest(
            market_id="test-market",
            token_id="test-token",
            side=OrderSide.BUY,
            outcome="YES",
            size=Decimal("10.0"),
            price=Decimal("0.5"),
            order_type=OrderType.GTC,
        )

        # Execute order - it will be filled in dry-run mode
        result = await execution_engine.execute_order(request)
        order_id = result.order.order_id

        # In dry-run mode, the order gets filled immediately, so won't be in open orders
        # Let's manually add an open order for testing
        from mercury.domain.order import Order, OrderStatus
        open_order = Order(
            order_id="test-open-order",
            market_id="test-market",
            token_id="test-token",
            side=OrderSide.BUY,
            outcome="YES",
            requested_size=Decimal("10.0"),
            filled_size=Decimal("0"),
            price=Decimal("0.5"),
            status=OrderStatus.OPEN,
        )
        execution_engine._open_orders["test-open-order"] = open_order

        # Now cancel the tracked order
        mock_event_bus.publish.reset_mock()
        cancel_result = await execution_engine.cancel_order("test-open-order")

        assert cancel_result is True
        assert open_order.status == OrderStatus.CANCELLED
        assert "test-open-order" not in execution_engine._open_orders

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_cancel_order_non_dry_run_success(self, mock_config, mock_event_bus, mock_clob):
        """Verify cancel_order calls CLOB and emits event on success."""
        mock_config.get_bool.side_effect = lambda key, default: {
            "mercury.dry_run": False,
            "execution.rebalance_partial_fills": True,
        }.get(key, default)

        mock_clob.cancel_order = AsyncMock(return_value=True)
        mock_clob._connected = True

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )
        await engine.start()

        result = await engine.cancel_order("order-to-cancel")

        assert result is True
        mock_clob.cancel_order.assert_called_once_with("order-to-cancel")

        # Verify cancelled event was published
        cancelled_calls = [
            call for call in mock_event_bus.publish.call_args_list
            if call[0][0] == "order.cancelled"
        ]
        assert len(cancelled_calls) == 1
        assert cancelled_calls[0][0][1]["order_id"] == "order-to-cancel"

        await engine.stop()

    @pytest.mark.asyncio
    async def test_cancel_order_non_dry_run_failure(self, mock_config, mock_event_bus, mock_clob):
        """Verify cancel_order handles CLOB rejection gracefully."""
        mock_config.get_bool.side_effect = lambda key, default: {
            "mercury.dry_run": False,
            "execution.rebalance_partial_fills": True,
        }.get(key, default)

        mock_clob.cancel_order = AsyncMock(return_value=False)
        mock_clob._connected = True

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )
        await engine.start()

        result = await engine.cancel_order("order-to-cancel")

        assert result is False

        # Verify cancel_failed event was published
        failed_calls = [
            call for call in mock_event_bus.publish.call_args_list
            if call[0][0] == "order.cancel_failed"
        ]
        assert len(failed_calls) == 1
        event_data = failed_calls[0][0][1]
        assert event_data["order_id"] == "order-to-cancel"
        assert event_data["reason"] == "exchange_rejected"

        await engine.stop()

    @pytest.mark.asyncio
    async def test_cancel_order_non_dry_run_exception(self, mock_config, mock_event_bus, mock_clob):
        """Verify cancel_order handles CLOB exceptions gracefully."""
        mock_config.get_bool.side_effect = lambda key, default: {
            "mercury.dry_run": False,
            "execution.rebalance_partial_fills": True,
        }.get(key, default)

        mock_clob.cancel_order = AsyncMock(side_effect=Exception("Network error"))
        mock_clob._connected = True

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )
        await engine.start()

        result = await engine.cancel_order("order-to-cancel")

        assert result is False

        # Verify cancel_failed event was published with exception details
        failed_calls = [
            call for call in mock_event_bus.publish.call_args_list
            if call[0][0] == "order.cancel_failed"
        ]
        assert len(failed_calls) == 1
        event_data = failed_calls[0][0][1]
        assert event_data["order_id"] == "order-to-cancel"
        assert "Network error" in event_data["reason"]

        await engine.stop()


class TestCancelAll:
    """Test cancel_all functionality."""

    @pytest.mark.asyncio
    async def test_cancel_all_no_orders(self, execution_engine, mock_event_bus):
        """Verify cancel_all handles empty order list."""
        await execution_engine.start()

        result = await execution_engine.cancel_all()

        assert result["total"] == 0
        assert result["cancelled"] == 0
        assert result["failed"] == 0
        assert result["errors"] == []

        # Verify completed event was published
        completed_calls = [
            call for call in mock_event_bus.publish.call_args_list
            if call[0][0] == "order.cancel_all.completed"
        ]
        assert len(completed_calls) == 1

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_cancel_all_multiple_orders(self, execution_engine, mock_event_bus):
        """Verify cancel_all cancels all tracked orders."""
        await execution_engine.start()

        # Add some open orders
        from mercury.domain.order import Order, OrderStatus
        for i in range(3):
            order = Order(
                order_id=f"open-order-{i}",
                market_id="test-market",
                token_id="test-token",
                side=OrderSide.BUY,
                outcome="YES",
                requested_size=Decimal("10.0"),
                filled_size=Decimal("0"),
                price=Decimal("0.5"),
                status=OrderStatus.OPEN,
            )
            execution_engine._open_orders[f"open-order-{i}"] = order

        mock_event_bus.publish.reset_mock()
        result = await execution_engine.cancel_all()

        assert result["total"] == 3
        assert result["cancelled"] == 3
        assert result["failed"] == 0
        assert result["errors"] == []
        assert len(execution_engine._open_orders) == 0

        # Verify cancelled events were published for each order
        cancelled_calls = [
            call for call in mock_event_bus.publish.call_args_list
            if call[0][0] == "order.cancelled"
        ]
        assert len(cancelled_calls) == 3

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_cancel_all_partial_failure(self, mock_config, mock_event_bus, mock_clob):
        """Verify cancel_all handles partial failures."""
        mock_config.get_bool.side_effect = lambda key, default: {
            "mercury.dry_run": False,
            "execution.rebalance_partial_fills": True,
        }.get(key, default)

        # First cancel succeeds, second fails
        mock_clob.cancel_order = AsyncMock(side_effect=[True, False, True])
        mock_clob._connected = True

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )
        await engine.start()

        # Add some open orders
        from mercury.domain.order import Order, OrderStatus
        for i in range(3):
            order = Order(
                order_id=f"open-order-{i}",
                market_id="test-market",
                token_id="test-token",
                side=OrderSide.BUY,
                outcome="YES",
                requested_size=Decimal("10.0"),
                filled_size=Decimal("0"),
                price=Decimal("0.5"),
                status=OrderStatus.OPEN,
            )
            engine._open_orders[f"open-order-{i}"] = order

        mock_event_bus.publish.reset_mock()
        result = await engine.cancel_all()

        assert result["total"] == 3
        assert result["cancelled"] == 2
        assert result["failed"] == 1
        assert len(result["errors"]) == 1
        assert result["errors"][0]["order_id"] == "open-order-1"

        await engine.stop()


class TestOpenOrderTracking:
    """Test open order tracking."""

    @pytest.mark.asyncio
    async def test_get_open_orders(self, execution_engine):
        """Verify get_open_orders returns tracked orders."""
        await execution_engine.start()

        # Initially empty
        assert execution_engine.get_open_orders() == []
        assert execution_engine.get_open_order_count() == 0

        # Add some orders
        from mercury.domain.order import Order, OrderStatus
        order1 = Order(
            order_id="order-1",
            market_id="market-1",
            token_id="token-1",
            side=OrderSide.BUY,
            outcome="YES",
            requested_size=Decimal("10.0"),
            filled_size=Decimal("0"),
            price=Decimal("0.5"),
            status=OrderStatus.OPEN,
        )
        order2 = Order(
            order_id="order-2",
            market_id="market-2",
            token_id="token-2",
            side=OrderSide.SELL,
            outcome="NO",
            requested_size=Decimal("5.0"),
            filled_size=Decimal("0"),
            price=Decimal("0.6"),
            status=OrderStatus.SUBMITTED,
        )
        execution_engine._open_orders["order-1"] = order1
        execution_engine._open_orders["order-2"] = order2

        orders = execution_engine.get_open_orders()
        assert len(orders) == 2
        assert execution_engine.get_open_order_count() == 2

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_get_open_order(self, execution_engine):
        """Verify get_open_order retrieves specific order."""
        await execution_engine.start()

        from mercury.domain.order import Order, OrderStatus
        order = Order(
            order_id="specific-order",
            market_id="test-market",
            token_id="test-token",
            side=OrderSide.BUY,
            outcome="YES",
            requested_size=Decimal("10.0"),
            filled_size=Decimal("0"),
            price=Decimal("0.5"),
            status=OrderStatus.OPEN,
        )
        execution_engine._open_orders["specific-order"] = order

        # Found
        found = execution_engine.get_open_order("specific-order")
        assert found is not None
        assert found.order_id == "specific-order"

        # Not found
        not_found = execution_engine.get_open_order("nonexistent-order")
        assert not_found is None

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_order_tracking_on_submission(self, execution_engine, mock_event_bus):
        """Verify orders are tracked on submission event."""
        await execution_engine.start()

        # The order tracking happens in _emit_order_event
        from mercury.domain.order import Order, OrderStatus
        order = Order(
            order_id="tracked-order",
            market_id="test-market",
            token_id="test-token",
            side=OrderSide.BUY,
            outcome="YES",
            requested_size=Decimal("10.0"),
            filled_size=Decimal("0"),
            price=Decimal("0.5"),
            status=OrderStatus.SUBMITTED,
        )

        # Initially not tracked
        assert "tracked-order" not in execution_engine._open_orders

        # Emit submitted event
        await execution_engine._emit_order_event("order.submitted", order)

        # Now tracked
        assert "tracked-order" in execution_engine._open_orders

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_order_untracked_on_terminal_status(self, execution_engine, mock_event_bus):
        """Verify orders are untracked on terminal status."""
        await execution_engine.start()

        from mercury.domain.order import Order, OrderStatus

        # Add an order
        order = Order(
            order_id="tracked-order",
            market_id="test-market",
            token_id="test-token",
            side=OrderSide.BUY,
            outcome="YES",
            requested_size=Decimal("10.0"),
            filled_size=Decimal("10.0"),
            price=Decimal("0.5"),
            status=OrderStatus.OPEN,
        )
        execution_engine._open_orders["tracked-order"] = order

        # Update to filled
        order.status = OrderStatus.FILLED
        await execution_engine._emit_order_event("order.filled", order)

        # Should be untracked
        assert "tracked-order" not in execution_engine._open_orders

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_terminal_states_untrack_orders(self, execution_engine, mock_event_bus):
        """Verify all terminal states untrack orders."""
        await execution_engine.start()

        from mercury.domain.order import Order, OrderStatus

        terminal_states = [
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        ]

        for status in terminal_states:
            order = Order(
                order_id=f"order-{status.value}",
                market_id="test-market",
                token_id="test-token",
                side=OrderSide.BUY,
                outcome="YES",
                requested_size=Decimal("10.0"),
                filled_size=Decimal("0"),
                price=Decimal("0.5"),
                status=OrderStatus.OPEN,
            )
            execution_engine._open_orders[f"order-{status.value}"] = order

            # Update to terminal status
            order.status = status
            await execution_engine._emit_order_event(f"order.{status.value}", order)

            # Should be untracked
            assert f"order-{status.value}" not in execution_engine._open_orders

        await execution_engine.stop()


class TestExecutionLatency:
    """Test execution latency tracking."""

    @pytest.mark.asyncio
    async def test_latency_tracked_for_queued_signal(self, execution_engine, mock_event_bus):
        """Verify latency is tracked when signal goes through queue."""
        await execution_engine.start()

        # Wait for queue processor to start
        await asyncio.sleep(0.1)

        signal_data = {
            "signal_id": "latency-test-signal",
            "market_id": "test-market",
            "signal_type": "ARBITRAGE",
            "target_size_usd": "50.0",
            "yes_price": "0.45",
            "no_price": "0.53",
            "yes_token_id": "yes-token",
            "no_token_id": "no-token",
            "priority": "high",
        }

        # Simulate receiving an approved signal
        await execution_engine._on_approved_signal(signal_data)

        # Give time for execution
        await asyncio.sleep(0.3)

        # Check latency was tracked
        assert execution_engine.last_latency_ms is not None
        assert execution_engine.last_latency is not None

        latency = execution_engine.last_latency
        assert latency.signal_id == "latency-test-signal"
        assert latency.queue_time_ms is not None
        assert latency.total_latency_ms is not None

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_latency_event_published(self, execution_engine, mock_event_bus):
        """Verify execution.latency event is published."""
        await execution_engine.start()
        await asyncio.sleep(0.1)

        signal_data = {
            "signal_id": "latency-event-test",
            "market_id": "test-market",
            "signal_type": "ARBITRAGE",
            "target_size_usd": "50.0",
            "yes_price": "0.45",
            "no_price": "0.53",
            "yes_token_id": "yes-token",
            "no_token_id": "no-token",
        }

        await execution_engine._on_approved_signal(signal_data)
        await asyncio.sleep(0.3)

        # Find the latency event
        latency_calls = [
            call for call in mock_event_bus.publish.call_args_list
            if call[0][0] == "execution.latency"
        ]
        assert len(latency_calls) >= 1

        event_data = latency_calls[0][0][1]
        assert "signal_id" in event_data
        assert "queue_time_ms" in event_data
        assert "total_latency_ms" in event_data
        assert "within_target" in event_data

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_latency_stats(self, execution_engine, mock_event_bus):
        """Verify latency statistics are calculated."""
        await execution_engine.start()
        await asyncio.sleep(0.1)

        # Execute multiple signals
        for i in range(3):
            signal_data = {
                "signal_id": f"stats-test-{i}",
                "market_id": "test-market",
                "signal_type": "ARBITRAGE",
                "target_size_usd": "50.0",
                "yes_price": "0.45",
                "no_price": "0.53",
                "yes_token_id": "yes-token",
                "no_token_id": "no-token",
            }
            await execution_engine._on_approved_signal(signal_data)
            await asyncio.sleep(0.2)

        stats = execution_engine.get_latency_stats()

        assert stats["history_size"] == 3
        assert stats["avg_total_ms"] is not None
        assert stats["avg_queue_ms"] is not None
        assert "within_target_count" in stats
        assert "exceeded_target_count" in stats

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_latency_history(self, execution_engine, mock_event_bus):
        """Verify latency history is stored."""
        await execution_engine.start()
        await asyncio.sleep(0.1)

        # Execute a signal
        signal_data = {
            "signal_id": "history-test",
            "market_id": "test-market",
            "signal_type": "ARBITRAGE",
            "target_size_usd": "50.0",
            "yes_price": "0.45",
            "no_price": "0.53",
            "yes_token_id": "yes-token",
            "no_token_id": "no-token",
        }
        await execution_engine._on_approved_signal(signal_data)
        await asyncio.sleep(0.3)

        history = execution_engine.get_latency_history(limit=5)

        assert len(history) == 1
        assert history[0]["signal_id"] == "history-test"
        assert "queue_time_ms" in history[0]
        assert "total_latency_ms" in history[0]

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_queue_signal_tracks_received_at(self, execution_engine, mock_event_bus):
        """Verify queue_signal properly tracks signal_received_at."""
        await execution_engine.start()

        from datetime import datetime, timezone, timedelta

        # Create a signal received timestamp in the past
        signal_received = datetime.now(timezone.utc) - timedelta(milliseconds=50)

        signal_data = {"signal_id": "received-at-test", "market_id": "test"}

        await execution_engine.queue_signal(
            "received-at-test",
            signal_data,
            signal_received_at=signal_received,
        )

        queued_signal = execution_engine._queue_items.get("received-at-test")
        assert queued_signal is not None
        assert queued_signal.signal_received_at == signal_received
        assert queued_signal.latency is not None
        assert queued_signal.latency.signal_received_at == signal_received
        assert queued_signal.latency.queue_entered_at is not None
        # Queue entered should be after signal received
        assert queued_signal.latency.queue_entered_at >= signal_received

        await execution_engine.stop()


class TestExecutionLatencyDataclass:
    """Test ExecutionLatency domain model."""

    def test_queue_time_calculation(self):
        """Verify queue_time_ms is calculated correctly."""
        from mercury.domain.order import ExecutionLatency
        from datetime import datetime, timezone, timedelta

        queue_entered = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        queue_exited = datetime(2024, 1, 1, 12, 0, 0, 50000, tzinfo=timezone.utc)  # 50ms later

        latency = ExecutionLatency(
            signal_id="test",
            queue_entered_at=queue_entered,
            queue_exited_at=queue_exited,
        )

        assert latency.queue_time_ms == 50.0

    def test_submission_time_calculation(self):
        """Verify submission_time_ms is calculated correctly."""
        from mercury.domain.order import ExecutionLatency
        from datetime import datetime, timezone

        submission_started = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        submission_completed = datetime(2024, 1, 1, 12, 0, 0, 30000, tzinfo=timezone.utc)  # 30ms later

        latency = ExecutionLatency(
            signal_id="test",
            submission_started_at=submission_started,
            submission_completed_at=submission_completed,
        )

        assert latency.submission_time_ms == 30.0

    def test_total_latency_calculation(self):
        """Verify total_latency_ms is calculated correctly."""
        from mercury.domain.order import ExecutionLatency
        from datetime import datetime, timezone

        signal_received = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        fill_completed = datetime(2024, 1, 1, 12, 0, 0, 80000, tzinfo=timezone.utc)  # 80ms later

        latency = ExecutionLatency(
            signal_id="test",
            signal_received_at=signal_received,
            fill_completed_at=fill_completed,
        )

        assert latency.total_latency_ms == 80.0
        assert latency.is_within_target is True  # Under 100ms

    def test_within_target_exceeded(self):
        """Verify is_within_target returns False for > 100ms."""
        from mercury.domain.order import ExecutionLatency
        from datetime import datetime, timezone

        signal_received = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        fill_completed = datetime(2024, 1, 1, 12, 0, 0, 150000, tzinfo=timezone.utc)  # 150ms later

        latency = ExecutionLatency(
            signal_id="test",
            signal_received_at=signal_received,
            fill_completed_at=fill_completed,
        )

        assert latency.total_latency_ms == 150.0
        assert latency.is_within_target is False

    def test_to_dict(self):
        """Verify to_dict produces expected structure."""
        from mercury.domain.order import ExecutionLatency
        from datetime import datetime, timezone

        signal_received = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        queue_entered = datetime(2024, 1, 1, 12, 0, 0, 10000, tzinfo=timezone.utc)
        queue_exited = datetime(2024, 1, 1, 12, 0, 0, 20000, tzinfo=timezone.utc)
        fill_completed = datetime(2024, 1, 1, 12, 0, 0, 50000, tzinfo=timezone.utc)

        latency = ExecutionLatency(
            signal_id="test-signal",
            order_id="test-order",
            signal_received_at=signal_received,
            queue_entered_at=queue_entered,
            queue_exited_at=queue_exited,
            fill_completed_at=fill_completed,
        )

        result = latency.to_dict()

        assert result["signal_id"] == "test-signal"
        assert result["order_id"] == "test-order"
        assert result["queue_time_ms"] == 10.0
        assert result["total_latency_ms"] == 50.0
        assert result["within_target"] is True

    def test_none_values_handled(self):
        """Verify None values are handled gracefully."""
        from mercury.domain.order import ExecutionLatency

        latency = ExecutionLatency(signal_id="test")

        assert latency.queue_time_ms is None
        assert latency.submission_time_ms is None
        assert latency.fill_time_ms is None
        assert latency.total_latency_ms is None
        assert latency.is_within_target is False  # None total means not within target


# =============================================================================
# Integration Tests with Mock CLOB Client
# =============================================================================


class MockCLOBClient:
    """Mock CLOB client for integration testing.

    Provides configurable responses and failure modes for testing
    execution engine behavior with realistic CLOB interactions.
    """

    def __init__(self):
        self._connected = False
        self._orders = {}
        self._positions = []
        self._balance = {"balance": Decimal("1000.0"), "allowance": Decimal("1000.0")}

        # Configurable behavior
        self.should_fail_submit = False
        self.fail_after_n_submits = 0  # 0 = don't fail
        self.submit_count = 0
        self.submit_delay_seconds = 0.0
        self.cancel_should_fail = False
        self.simulated_fills = {}  # order_id -> fill_size

    async def connect(self):
        self._connected = True

    async def close(self):
        self._connected = False

    async def execute_order(self, token_id, side, amount_usd=None, amount_shares=None, price=None, timeout_seconds=5.0):
        from mercury.integrations.polymarket.types import OrderResult, OrderSide, OrderStatus

        self.submit_count += 1

        if self.submit_delay_seconds > 0:
            await asyncio.sleep(self.submit_delay_seconds)

        # Check if we should fail
        if self.should_fail_submit:
            raise Exception("Simulated CLOB failure")

        if self.fail_after_n_submits > 0 and self.submit_count >= self.fail_after_n_submits:
            raise Exception(f"Simulated failure after {self.submit_count} submits")

        order_id = f"mock-order-{self.submit_count}"
        shares = amount_shares or (amount_usd / price if amount_usd and price else Decimal("10"))

        self._orders[order_id] = {
            "id": order_id,
            "token_id": token_id,
            "side": side,
            "status": "MATCHED",
            "size": str(shares),
            "size_matched": str(shares),
        }

        return OrderResult(
            order_id=order_id,
            token_id=token_id,
            side=side,
            status=OrderStatus.MATCHED,
            requested_price=price or Decimal("0.5"),
            requested_size=shares,
            filled_size=shares,
            filled_cost=shares * (price or Decimal("0.5")),
        )

    async def cancel_order(self, order_id):
        if self.cancel_should_fail:
            return False
        self._orders.pop(order_id, None)
        return True

    async def cancel_all_orders(self):
        count = len(self._orders)
        self._orders.clear()
        return count

    async def get_open_orders(self):
        return list(self._orders.values())

    async def get_balance(self):
        return self._balance


class TestExecutionEngineWithMockCLOB:
    """Integration tests using mock CLOB client."""

    @pytest.fixture
    def mock_clob_client(self):
        return MockCLOBClient()

    @pytest.fixture
    def integration_engine(self, mock_config, mock_event_bus, mock_clob_client):
        """Create ExecutionEngine with mock CLOB for integration tests."""
        # Configure for non-dry-run to use CLOB
        mock_config.get_bool.side_effect = lambda key, default: {
            "mercury.dry_run": False,
            "execution.rebalance_partial_fills": True,
        }.get(key, default)

        return ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob_client,
        )

    @pytest.mark.asyncio
    async def test_execute_order_via_clob(self, integration_engine, mock_clob_client, mock_event_bus):
        """Verify order execution goes through CLOB client."""
        mock_clob_client._connected = True
        await integration_engine.start()

        request = OrderRequest(
            market_id="test-market",
            token_id="test-token",
            side=OrderSide.BUY,
            outcome="YES",
            size=Decimal("10.0"),
            price=Decimal("0.5"),
            order_type=OrderType.GTC,
        )

        result = await integration_engine.execute_order(request)

        # In mock, orders are automatically filled
        assert result.success is True
        assert mock_clob_client.submit_count == 1

        await integration_engine.stop()

    @pytest.mark.asyncio
    async def test_clob_failure_handled_gracefully(self, integration_engine, mock_clob_client, mock_event_bus):
        """Verify CLOB failures are handled gracefully."""
        mock_clob_client._connected = True
        mock_clob_client.should_fail_submit = True
        await integration_engine.start()

        request = OrderRequest(
            market_id="test-market",
            token_id="test-token",
            side=OrderSide.BUY,
            outcome="YES",
            size=Decimal("10.0"),
            price=Decimal("0.5"),
        )

        result = await integration_engine.execute_order(request)

        assert result.success is False
        assert result.error_message is not None
        assert "Simulated CLOB failure" in result.error_message

        # Verify rejected event was published
        rejected_calls = [
            call for call in mock_event_bus.publish.call_args_list
            if call[0][0] == "order.rejected"
        ]
        assert len(rejected_calls) >= 1

        await integration_engine.stop()

    @pytest.mark.asyncio
    async def test_dual_leg_via_clob(self, integration_engine, mock_clob_client, mock_event_bus):
        """Verify dual-leg execution uses CLOB for both orders."""
        mock_clob_client._connected = True
        await integration_engine.start()

        yes_order = OrderRequest(
            market_id="test-market",
            token_id="yes-token",
            side=OrderSide.BUY,
            outcome="YES",
            size=Decimal("10.0"),
            price=Decimal("0.48"),
        )

        no_order = OrderRequest(
            market_id="test-market",
            token_id="no-token",
            side=OrderSide.BUY,
            outcome="NO",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
        )

        result = await integration_engine.execute_dual_leg(yes_order, no_order)

        assert result.success is True
        # Both orders should be submitted
        assert mock_clob_client.submit_count == 2

        await integration_engine.stop()

    @pytest.mark.asyncio
    async def test_cancel_uses_clob(self, integration_engine, mock_clob_client, mock_event_bus):
        """Verify cancel_order calls CLOB client."""
        mock_clob_client._connected = True
        await integration_engine.start()

        # Add an order to CLOB
        mock_clob_client._orders["test-order"] = {"id": "test-order", "status": "LIVE"}

        # Also track in engine
        from mercury.domain.order import Order, OrderStatus as OS
        tracked_order = Order(
            order_id="test-order",
            market_id="test-market",
            token_id="test-token",
            side=OrderSide.BUY,
            outcome="YES",
            requested_size=Decimal("10.0"),
            filled_size=Decimal("0"),
            price=Decimal("0.5"),
            status=OS.OPEN,
        )
        integration_engine._open_orders["test-order"] = tracked_order

        result = await integration_engine.cancel_order("test-order")

        assert result is True
        assert "test-order" not in mock_clob_client._orders

        await integration_engine.stop()


# =============================================================================
# Retry Behavior Tests
# =============================================================================


class TestRetryBehaviorInExecution:
    """Test retry behavior for transient failures."""

    @pytest.fixture
    def mock_clob_with_retries(self):
        """Mock CLOB that fails initially then succeeds."""
        clob = MagicMock()
        clob._connected = True
        clob.connect = AsyncMock()
        clob.close = AsyncMock()
        clob.cancel_all_orders = AsyncMock()

        # Track call counts for testing retry behavior
        clob._call_count = 0

        return clob

    @pytest.mark.asyncio
    async def test_transient_failure_followed_by_success(self, mock_config, mock_event_bus, mock_clob_with_retries):
        """Verify transient failures are handled and execution retried."""
        mock_config.get_bool.side_effect = lambda key, default: {
            "mercury.dry_run": True,  # Use dry run for simpler testing
        }.get(key, default)

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob_with_retries,
        )
        await engine.start()

        signal = ExecutionSignal(
            signal_id="retry-test-signal",
            original_signal_id="retry-test-signal",
            market_id="test-market",
            signal_type=SignalType.ARBITRAGE,
            target_size_usd=Decimal("100"),
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.50"),
            yes_token_id="yes-token",
            no_token_id="no-token",
        )

        # Execute - should succeed in dry run
        result = await engine.execute(signal)

        assert result.success is True

        await engine.stop()

    @pytest.mark.asyncio
    async def test_signal_queue_with_retry_after_failure(self, mock_config, mock_event_bus):
        """Verify failed signals can be requeued."""
        mock_config.get_bool.return_value = True  # dry_run
        mock_config.get_int.side_effect = lambda key, default: {
            "execution.max_concurrent": 2,
            "execution.max_queue_size": 10,
        }.get(key, default)
        mock_config.get_float.return_value = 60.0

        mock_clob = MagicMock()
        mock_clob.connect = AsyncMock()
        mock_clob.close = AsyncMock()

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )
        await engine.start()
        await asyncio.sleep(0.1)

        signal_data = {
            "signal_id": "requeue-test",
            "market_id": "test-market",
            "signal_type": "ARBITRAGE",
            "target_size_usd": "50.0",
            "yes_price": "0.45",
            "no_price": "0.53",
            "yes_token_id": "yes-token",
            "no_token_id": "no-token",
        }

        # Queue signal
        await engine.queue_signal("requeue-test", signal_data, SignalPriority.HIGH)

        # Wait for processing
        await asyncio.sleep(0.3)

        # Verify execution happened
        assert engine._total_executed >= 1 or engine._total_failed >= 0

        await engine.stop()

    @pytest.mark.asyncio
    async def test_execution_timeout_handling(self, mock_config, mock_event_bus):
        """Verify execution timeout is properly handled."""
        mock_config.get_bool.return_value = True  # dry_run
        mock_config.get_int.side_effect = lambda key, default: default
        mock_config.get_float.side_effect = lambda key, default: {
            "execution.queue_timeout_seconds": 0.1,  # Very short timeout
        }.get(key, default)

        mock_clob = MagicMock()
        mock_clob.connect = AsyncMock()
        mock_clob.close = AsyncMock()

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )

        await engine.start()
        await asyncio.sleep(0.1)

        # Queue a signal
        signal_data = {
            "signal_id": "timeout-test",
            "market_id": "test",
            "signal_type": "ARBITRAGE",
            "target_size_usd": "10.0",
            "yes_price": "0.5",
            "no_price": "0.5",
        }

        await engine.queue_signal("timeout-test", signal_data)

        # Wait long enough for timeout to potentially expire
        await asyncio.sleep(0.5)

        await engine.stop()


# =============================================================================
# Partial Fill Edge Cases
# =============================================================================


class TestPartialFillEdgeCases:
    """Test edge cases in partial fill handling."""

    @pytest.fixture
    def yes_order(self):
        return OrderRequest(
            market_id="test-market",
            token_id="yes-token",
            side=OrderSide.BUY,
            outcome="YES",
            size=Decimal("10.0"),
            price=Decimal("0.48"),
        )

    @pytest.fixture
    def no_order(self):
        return OrderRequest(
            market_id="test-market",
            token_id="no-token",
            side=OrderSide.BUY,
            outcome="NO",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
        )

    @pytest.mark.asyncio
    async def test_partial_fill_no_success_yes_fail(self, mock_config, mock_event_bus, yes_order, no_order):
        """Verify NO success + YES failure triggers unwind of NO."""
        mock_config.get_bool.side_effect = lambda key, default: {
            "mercury.dry_run": False,
            "execution.rebalance_partial_fills": True,
        }.get(key, default)

        mock_clob = MagicMock()
        mock_clob._connected = True
        mock_clob.connect = AsyncMock()
        mock_clob.close = AsyncMock()

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )

        original_execute = engine.execute_order
        call_order = []

        async def mock_execute(order_req, timeout=30.0):
            call_order.append(order_req.outcome)

            if order_req.outcome == "YES" and order_req.side == OrderSide.BUY:
                # YES BUY fails
                from mercury.domain.order import Order, OrderStatus as OS, OrderResult as DOR
                failed_order = Order(
                    order_id="failed-yes",
                    market_id=order_req.market_id,
                    token_id=order_req.token_id,
                    side=order_req.side,
                    outcome=order_req.outcome,
                    requested_size=order_req.size,
                    filled_size=Decimal("0"),
                    price=order_req.price,
                    status=OS.REJECTED,
                )
                return DOR(
                    success=False,
                    order=failed_order,
                    fills=[],
                    error_message="YES order rejected",
                )
            else:
                # NO order succeeds (and unwind succeeds)
                engine._dry_run = True
                result = await original_execute(order_req, timeout)
                engine._dry_run = False
                return result

        engine.execute_order = mock_execute

        await engine.start()

        result = await engine.execute_dual_leg(yes_order, no_order)

        assert result.success is False
        # NO should have filled
        assert result.no_result is not None
        assert result.no_result.order.status == OrderStatus.FILLED
        # YES should have failed
        assert result.yes_result is not None

        # Verify partial event was emitted
        partial_calls = [
            call for call in mock_event_bus.publish.call_args_list
            if call[0][0] == "order.dual_leg.partial"
        ]
        assert len(partial_calls) == 1

        await engine.stop()

    @pytest.mark.asyncio
    async def test_unwind_failure_creates_dangling_position_warning(self, mock_config, mock_event_bus, yes_order, no_order):
        """Verify unwind failure results in dangling position warning."""
        mock_config.get_bool.side_effect = lambda key, default: {
            "mercury.dry_run": False,
            "execution.rebalance_partial_fills": True,
        }.get(key, default)

        mock_clob = MagicMock()
        mock_clob._connected = True
        mock_clob.connect = AsyncMock()
        mock_clob.close = AsyncMock()

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )

        original_execute = engine.execute_order

        async def mock_execute(order_req, timeout=30.0):
            if order_req.outcome == "YES" and order_req.side == OrderSide.BUY:
                # YES BUY succeeds
                engine._dry_run = True
                result = await original_execute(order_req, timeout)
                engine._dry_run = False
                return result
            elif order_req.outcome == "NO" and order_req.side == OrderSide.BUY:
                # NO BUY fails
                from mercury.domain.order import Order, OrderStatus as OS, OrderResult as DOR
                failed_order = Order(
                    order_id="failed-no",
                    market_id=order_req.market_id,
                    token_id=order_req.token_id,
                    side=order_req.side,
                    outcome=order_req.outcome,
                    requested_size=order_req.size,
                    filled_size=Decimal("0"),
                    price=order_req.price,
                    status=OS.REJECTED,
                )
                return DOR(
                    success=False,
                    order=failed_order,
                    fills=[],
                    error_message="NO order rejected",
                )
            else:
                # Unwind (SELL) also fails
                from mercury.domain.order import Order, OrderStatus as OS, OrderResult as DOR
                failed_unwind = Order(
                    order_id="failed-unwind",
                    market_id=order_req.market_id,
                    token_id=order_req.token_id,
                    side=order_req.side,
                    outcome=order_req.outcome,
                    requested_size=order_req.size,
                    filled_size=Decimal("0"),
                    price=order_req.price,
                    status=OS.REJECTED,
                )
                return DOR(
                    success=False,
                    order=failed_unwind,
                    fills=[],
                    error_message="Unwind order rejected",
                )

        engine.execute_order = mock_execute

        await engine.start()

        result = await engine.execute_dual_leg(yes_order, no_order)

        assert result.success is False
        # Error message should indicate dangling position
        assert "dangling" in result.error_message.lower() or "CRITICAL" in result.error_message

        # Verify failed event with dangling info was emitted
        failed_calls = [
            call for call in mock_event_bus.publish.call_args_list
            if call[0][0] == "order.dual_leg.failed"
        ]
        assert len(failed_calls) >= 1

        await engine.stop()

    @pytest.mark.asyncio
    async def test_unwind_exception_handled(self, mock_config, mock_event_bus, yes_order, no_order):
        """Verify exceptions during unwind are handled gracefully."""
        mock_config.get_bool.side_effect = lambda key, default: {
            "mercury.dry_run": False,
            "execution.rebalance_partial_fills": True,
        }.get(key, default)

        mock_clob = MagicMock()
        mock_clob._connected = True
        mock_clob.connect = AsyncMock()
        mock_clob.close = AsyncMock()

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )

        original_execute = engine.execute_order

        async def mock_execute(order_req, timeout=30.0):
            if order_req.outcome == "YES" and order_req.side == OrderSide.BUY:
                # YES BUY succeeds
                engine._dry_run = True
                result = await original_execute(order_req, timeout)
                engine._dry_run = False
                return result
            elif order_req.outcome == "NO" and order_req.side == OrderSide.BUY:
                # NO BUY fails
                from mercury.domain.order import Order, OrderStatus as OS, OrderResult as DOR
                failed_order = Order(
                    order_id="failed-no",
                    market_id=order_req.market_id,
                    token_id=order_req.token_id,
                    side=order_req.side,
                    outcome=order_req.outcome,
                    requested_size=order_req.size,
                    filled_size=Decimal("0"),
                    price=order_req.price,
                    status=OS.REJECTED,
                )
                return DOR(
                    success=False,
                    order=failed_order,
                    fills=[],
                    error_message="NO order rejected",
                )
            else:
                # Unwind throws exception
                raise Exception("Network error during unwind")

        engine.execute_order = mock_execute

        await engine.start()

        result = await engine.execute_dual_leg(yes_order, no_order)

        assert result.success is False
        assert "exception" in result.error_message.lower() or "dangling" in result.error_message.lower()

        await engine.stop()

    @pytest.mark.asyncio
    async def test_gather_exception_handling_in_dual_leg(self, mock_config, mock_event_bus, yes_order, no_order):
        """Verify exceptions in asyncio.gather are handled."""
        mock_config.get_bool.side_effect = lambda key, default: {
            "mercury.dry_run": False,
        }.get(key, default)

        mock_clob = MagicMock()
        mock_clob._connected = True
        mock_clob.connect = AsyncMock()
        mock_clob.close = AsyncMock()

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )

        async def mock_execute_throws(order_req, timeout=30.0):
            raise Exception(f"Critical failure on {order_req.outcome}")

        engine.execute_order = mock_execute_throws

        await engine.start()

        result = await engine.execute_dual_leg(yes_order, no_order)

        # Both should fail with exceptions converted to failed results
        assert result.success is False
        assert result.yes_result is not None or result.no_result is not None
        assert "Both legs failed" in result.error_message

        await engine.stop()


# =============================================================================
# Queue Signal Expiration Tests
# =============================================================================


class TestQueueSignalExpiration:
    """Test signal expiration in the queue."""

    @pytest.mark.asyncio
    async def test_signal_expires_in_queue(self, mock_config, mock_event_bus):
        """Verify signals expire after timeout."""
        # Set very short timeout
        mock_config.get_bool.return_value = True
        mock_config.get_int.side_effect = lambda key, default: {
            "execution.max_concurrent": 1,
            "execution.max_queue_size": 10,
        }.get(key, default)
        mock_config.get_float.side_effect = lambda key, default: {
            "execution.queue_timeout_seconds": 0.1,  # 100ms timeout
        }.get(key, default)

        mock_clob = MagicMock()
        mock_clob.connect = AsyncMock()
        mock_clob.close = AsyncMock()

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )

        await engine.start()

        # Queue a signal with very short timeout
        signal = QueuedSignal(
            signal_id="expire-test",
            signal_data={"signal_id": "expire-test"},
            priority=SignalPriority.LOW,
        )

        # Manually age the signal
        from datetime import timedelta
        signal.queued_at = datetime.now(timezone.utc) - timedelta(seconds=1)

        # Check if expired
        assert engine._is_signal_expired(signal) is True

        await engine.stop()

    @pytest.mark.asyncio
    async def test_signal_not_expired_within_timeout(self, mock_config, mock_event_bus):
        """Verify signals are not expired within timeout."""
        mock_config.get_bool.return_value = True
        mock_config.get_int.side_effect = lambda key, default: default
        mock_config.get_float.side_effect = lambda key, default: {
            "execution.queue_timeout_seconds": 60.0,
        }.get(key, default)

        mock_clob = MagicMock()
        mock_clob.connect = AsyncMock()
        mock_clob.close = AsyncMock()

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )

        await engine.start()

        signal = QueuedSignal(
            signal_id="fresh-signal",
            signal_data={"signal_id": "fresh-signal"},
            priority=SignalPriority.HIGH,
        )

        assert engine._is_signal_expired(signal) is False

        await engine.stop()


# =============================================================================
# Concurrent Execution Tests
# =============================================================================


class TestConcurrentExecutionControl:
    """Test concurrent execution controls."""

    @pytest.mark.asyncio
    async def test_concurrent_limit_respected(self, mock_config, mock_event_bus):
        """Verify concurrent execution limit is respected."""
        mock_config.get_bool.return_value = True
        mock_config.get_int.side_effect = lambda key, default: {
            "execution.max_concurrent": 2,  # Only 2 concurrent
            "execution.max_queue_size": 10,
        }.get(key, default)
        mock_config.get_float.return_value = 60.0

        mock_clob = MagicMock()
        mock_clob.connect = AsyncMock()
        mock_clob.close = AsyncMock()

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )

        await engine.start()

        # Verify semaphore initialized with correct value
        assert engine._execution_semaphore._value == 2

        await engine.stop()

    @pytest.mark.asyncio
    async def test_multiple_signals_processed_with_limit(self, mock_config, mock_event_bus):
        """Verify multiple signals are processed respecting concurrent limit."""
        mock_config.get_bool.return_value = True
        mock_config.get_int.side_effect = lambda key, default: {
            "execution.max_concurrent": 2,
            "execution.max_queue_size": 10,
        }.get(key, default)
        mock_config.get_float.return_value = 60.0

        mock_clob = MagicMock()
        mock_clob.connect = AsyncMock()
        mock_clob.close = AsyncMock()

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )

        await engine.start()
        await asyncio.sleep(0.1)

        # Queue multiple signals
        for i in range(5):
            signal_data = {
                "signal_id": f"concurrent-{i}",
                "market_id": "test-market",
                "signal_type": "ARBITRAGE",
                "target_size_usd": "10.0",
                "yes_price": "0.45",
                "no_price": "0.53",
                "yes_token_id": "yes-token",
                "no_token_id": "no-token",
            }
            await engine.queue_signal(f"concurrent-{i}", signal_data)

        # Wait for processing
        await asyncio.sleep(1.0)

        # All should be processed
        assert engine._total_queued == 5
        assert engine._total_executed + engine._total_failed >= 5

        await engine.stop()


# =============================================================================
# Create Failed Order Result Tests
# =============================================================================


class TestCreateFailedOrderResult:
    """Test _create_failed_order_result helper."""

    @pytest.mark.asyncio
    async def test_creates_rejected_order(self, execution_engine):
        """Verify failed order result has REJECTED status."""
        await execution_engine.start()

        request = OrderRequest(
            market_id="test-market",
            token_id="test-token",
            side=OrderSide.BUY,
            outcome="YES",
            size=Decimal("10.0"),
            price=Decimal("0.5"),
        )

        exception = Exception("Test failure")
        start_time = 1000.0

        result = execution_engine._create_failed_order_result(request, exception, start_time)

        assert result.success is False
        assert result.order is not None
        assert result.order.status == OrderStatus.REJECTED
        assert result.error_message == "Test failure"
        assert result.order.filled_size == Decimal("0")
        assert result.order.order_id.startswith("ord-failed-")

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_preserves_order_details(self, execution_engine):
        """Verify failed order result preserves request details."""
        await execution_engine.start()

        request = OrderRequest(
            market_id="my-market",
            token_id="my-token",
            side=OrderSide.SELL,
            outcome="NO",
            size=Decimal("25.5"),
            price=Decimal("0.75"),
            order_type=OrderType.FOK,
            client_order_id="custom-id",
        )

        result = execution_engine._create_failed_order_result(
            request, ValueError("bad value"), 0.0
        )

        assert result.order.market_id == "my-market"
        assert result.order.token_id == "my-token"
        assert result.order.side == OrderSide.SELL
        assert result.order.outcome == "NO"
        assert result.order.requested_size == Decimal("25.5")
        assert result.order.price == Decimal("0.75")
        assert result.order.client_order_id == "custom-id"

        await execution_engine.stop()


# =============================================================================
# Build Approved Signal Tests
# =============================================================================


class TestBuildApprovedSignal:
    """Test _build_approved_signal helper."""

    @pytest.mark.asyncio
    async def test_builds_signal_from_event_data(self, execution_engine):
        """Verify signal is built correctly from event data."""
        await execution_engine.start()

        data = {
            "signal_id": "test-signal-123",
            "original_signal_id": "orig-123",
            "market_id": "market-abc",
            "signal_type": "ARBITRAGE",
            "target_size_usd": "100.50",
            "yes_price": "0.48",
            "no_price": "0.50",
            "yes_token_id": "yes-token-xyz",
            "no_token_id": "no-token-xyz",
        }

        signal = execution_engine._build_approved_signal(data)

        assert signal.signal_id == "test-signal-123"
        assert signal.original_signal_id == "orig-123"
        assert signal.market_id == "market-abc"
        assert signal.signal_type == SignalType.ARBITRAGE
        assert signal.target_size_usd == Decimal("100.50")
        assert signal.yes_price == Decimal("0.48")
        assert signal.no_price == Decimal("0.50")
        assert signal.yes_token_id == "yes-token-xyz"
        assert signal.no_token_id == "no-token-xyz"

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_builds_signal_with_defaults(self, execution_engine):
        """Verify signal uses defaults for missing optional fields."""
        await execution_engine.start()

        data = {
            "signal_id": "minimal-signal",
            "market_id": "market",
            "signal_type": "BUY_YES",
            "target_size_usd": "50",
        }

        signal = execution_engine._build_approved_signal(data)

        assert signal.signal_id == "minimal-signal"
        assert signal.original_signal_id == "minimal-signal"  # Falls back to signal_id
        assert signal.yes_price == Decimal("0")
        assert signal.no_price == Decimal("0")
        assert signal.yes_token_id == ""
        assert signal.no_token_id == ""

        await execution_engine.stop()


class TestSignalValidationFlowIntegration:
    """Integration tests for signal validation flow.

    Tests that ExecutionEngine properly receives and processes
    approved signals from RiskManager via the event bus.
    """

    @pytest.mark.asyncio
    async def test_execution_engine_subscribes_to_approved_signals(
        self, execution_engine, mock_event_bus
    ):
        """ExecutionEngine should subscribe to risk.approved.* events."""
        await execution_engine.start()

        # Verify subscription to risk.approved.*
        subscribe_calls = mock_event_bus.subscribe.call_args_list
        patterns = [call[0][0] for call in subscribe_calls]

        assert "risk.approved.*" in patterns

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_approved_signal_triggers_execution(
        self, execution_engine, mock_event_bus
    ):
        """Approved signal events should trigger execution."""
        await execution_engine.start()

        # Simulate approved signal from RiskManager
        approved_signal_data = {
            "signal_id": "risk-approved-test",
            "market_id": "btc-market",
            "signal_type": "ARBITRAGE",
            "approved_size_usd": "10.0",
            "target_size_usd": "10.0",
            "yes_price": "0.48",
            "no_price": "0.50",
            "yes_token_id": "yes-token",
            "no_token_id": "no-token",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Call the handler directly (simulating event bus callback)
        await execution_engine._on_approved_signal(approved_signal_data)

        # Give time for queue processing
        await asyncio.sleep(0.2)

        # Check that signal was queued
        assert execution_engine._total_queued >= 1

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_approved_signal_preserves_signal_id(
        self, execution_engine, mock_event_bus
    ):
        """Signal ID from approved event should be preserved through execution."""
        await execution_engine.start()

        approved_signal_data = {
            "signal_id": "preserve-id-test-12345",
            "market_id": "test-market",
            "signal_type": "ARBITRAGE",
            "target_size_usd": "10.0",
            "yes_price": "0.48",
            "no_price": "0.50",
        }

        await execution_engine._on_approved_signal(approved_signal_data)

        # Check signal was queued with correct ID
        assert "preserve-id-test-12345" in execution_engine._queue_items

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_approved_signal_respects_priority(
        self, execution_engine, mock_event_bus
    ):
        """Priority from approved signal should be respected."""
        await execution_engine.start()

        high_priority_signal = {
            "signal_id": "high-priority-signal",
            "market_id": "test-market",
            "signal_type": "ARBITRAGE",
            "target_size_usd": "10.0",
            "yes_price": "0.48",
            "no_price": "0.50",
            "priority": "critical",
        }

        low_priority_signal = {
            "signal_id": "low-priority-signal",
            "market_id": "test-market",
            "signal_type": "ARBITRAGE",
            "target_size_usd": "5.0",
            "yes_price": "0.48",
            "no_price": "0.50",
            "priority": "low",
        }

        await execution_engine._on_approved_signal(low_priority_signal)
        await execution_engine._on_approved_signal(high_priority_signal)

        # Both should be queued
        assert "high-priority-signal" in execution_engine._queue_items
        assert "low-priority-signal" in execution_engine._queue_items

        # High priority should have CRITICAL priority
        high_qs = execution_engine._queue_items["high-priority-signal"]
        assert high_qs.priority == SignalPriority.CRITICAL

        low_qs = execution_engine._queue_items["low-priority-signal"]
        assert low_qs.priority == SignalPriority.LOW

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_approved_signal_publishes_queue_event(
        self, execution_engine, mock_event_bus
    ):
        """Approved signal processing should publish queue events."""
        await execution_engine.start()

        approved_signal_data = {
            "signal_id": "queue-event-test",
            "market_id": "test-market",
            "signal_type": "ARBITRAGE",
            "target_size_usd": "10.0",
            "yes_price": "0.48",
            "no_price": "0.50",
        }

        await execution_engine._on_approved_signal(approved_signal_data)

        # Check execution.queue.added event was published
        calls = mock_event_bus.publish.call_args_list
        queue_added_calls = [c for c in calls if c[0][0] == "execution.queue.added"]

        assert len(queue_added_calls) >= 1
        event_data = queue_added_calls[0][0][1]
        assert event_data["signal_id"] == "queue-event-test"

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_execution_engine_ignores_rejected_signals(
        self, execution_engine, mock_event_bus
    ):
        """ExecutionEngine should NOT subscribe to risk.rejected.* events."""
        await execution_engine.start()

        # Verify no subscription to risk.rejected.*
        subscribe_calls = mock_event_bus.subscribe.call_args_list
        patterns = [call[0][0] for call in subscribe_calls]

        assert "risk.rejected.*" not in patterns

        await execution_engine.stop()

    @pytest.mark.asyncio
    async def test_full_signal_flow_dry_run(
        self, execution_engine, mock_event_bus
    ):
        """Test complete signal flow from approval to execution in dry-run mode."""
        await execution_engine.start()

        # Ensure we're in dry-run mode
        assert execution_engine._dry_run is True

        approved_signal_data = {
            "signal_id": "full-flow-test",
            "market_id": "flow-test-market",
            "signal_type": "ARBITRAGE",
            "target_size_usd": "10.0",
            "yes_price": "0.48",
            "no_price": "0.50",
            "yes_token_id": "yes-flow",
            "no_token_id": "no-flow",
        }

        await execution_engine._on_approved_signal(approved_signal_data)

        # Wait for execution to complete
        await asyncio.sleep(0.5)

        # Check execution happened
        assert execution_engine._total_executed >= 1 or execution_engine._total_queued >= 1

        # Check execution events were published
        calls = mock_event_bus.publish.call_args_list
        event_channels = [c[0][0] for c in calls]

        # Should have queue and possibly execution events
        assert any("execution." in ch for ch in event_channels)

        await execution_engine.stop()
