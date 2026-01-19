"""Unit tests for graceful shutdown handling.

Tests the ShutdownManager class and its integration with MercuryApp.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mercury.core.shutdown import (
    ShutdownManager,
    ShutdownPhase,
    ShutdownProgress,
)


class TestShutdownProgress:
    """Tests for ShutdownProgress dataclass."""

    def test_initial_state(self):
        """ShutdownProgress starts in RUNNING phase."""
        progress = ShutdownProgress()
        assert progress.phase == ShutdownPhase.RUNNING
        assert progress.started_at is None
        assert progress.completed_at is None
        assert progress.signal_received is None
        assert progress.in_flight_orders == 0
        assert not progress.orders_drained
        assert not progress.websocket_closed
        assert not progress.metrics_flushed
        assert not progress.database_closed
        assert progress.errors == []

    def test_is_shutting_down_false_when_running(self):
        """is_shutting_down returns False when in RUNNING phase."""
        progress = ShutdownProgress()
        assert not progress.is_shutting_down

    def test_is_shutting_down_true_during_shutdown(self):
        """is_shutting_down returns True during shutdown phases."""
        progress = ShutdownProgress()
        progress.phase = ShutdownPhase.DRAINING_ORDERS
        assert progress.is_shutting_down

    def test_is_shutting_down_false_when_completed(self):
        """is_shutting_down returns False when COMPLETED."""
        progress = ShutdownProgress()
        progress.phase = ShutdownPhase.COMPLETED
        assert not progress.is_shutting_down

    def test_duration_seconds_none_when_not_started(self):
        """duration_seconds is None when shutdown hasn't started."""
        progress = ShutdownProgress()
        assert progress.duration_seconds is None

    def test_duration_seconds_calculated_when_started(self):
        """duration_seconds is calculated from started_at."""
        progress = ShutdownProgress()
        progress.started_at = datetime.now(timezone.utc)
        # Duration should be very small (just created)
        assert progress.duration_seconds is not None
        assert progress.duration_seconds >= 0
        assert progress.duration_seconds < 1.0

    def test_to_dict(self):
        """to_dict returns expected structure."""
        progress = ShutdownProgress()
        progress.phase = ShutdownPhase.DRAINING_ORDERS
        progress.signal_received = "SIGTERM"
        progress.in_flight_orders = 5

        result = progress.to_dict()

        assert result["phase"] == "draining_orders"
        assert result["signal_received"] == "SIGTERM"
        assert result["in_flight_orders"] == 5
        assert "started_at" in result
        assert "errors" in result


class TestShutdownManager:
    """Tests for ShutdownManager."""

    @pytest.fixture
    def manager(self):
        """Create a shutdown manager for testing."""
        return ShutdownManager(timeout_seconds=5.0, drain_timeout_seconds=10.0)

    def test_initial_state(self, manager):
        """ShutdownManager starts in not-shutting-down state."""
        assert not manager.is_shutting_down
        assert manager.progress.phase == ShutdownPhase.RUNNING

    def test_register_callbacks(self, manager):
        """Callbacks can be registered for each phase."""
        stop_work = AsyncMock()
        drain = AsyncMock()
        close_conn = AsyncMock()
        flush = AsyncMock()
        cleanup = AsyncMock()

        manager.on_stop_new_work(stop_work)
        manager.on_drain_orders(drain)
        manager.on_close_connections(close_conn)
        manager.on_flush_data(flush)
        manager.on_cleanup(cleanup)

        assert len(manager._stop_new_work_callbacks) == 1
        assert len(manager._drain_orders_callbacks) == 1
        assert len(manager._close_connections_callbacks) == 1
        assert len(manager._flush_data_callbacks) == 1
        assert len(manager._cleanup_callbacks) == 1

    @pytest.mark.asyncio
    async def test_shutdown_runs_all_phases(self, manager):
        """Shutdown runs through all phases in order."""
        phase_order = []

        async def track_phase(name):
            phase_order.append(name)

        manager.on_stop_new_work(lambda: track_phase("stop_work"))
        manager.on_drain_orders(lambda: track_phase("drain"))
        manager.on_close_connections(lambda: track_phase("close_conn"))
        manager.on_flush_data(lambda: track_phase("flush"))
        manager.on_cleanup(lambda: track_phase("cleanup"))

        await manager.shutdown()

        assert phase_order == ["stop_work", "drain", "close_conn", "flush", "cleanup"]
        assert manager.progress.phase == ShutdownPhase.COMPLETED

    @pytest.mark.asyncio
    async def test_shutdown_sets_progress_flags(self, manager):
        """Shutdown sets progress flags for each phase."""
        await manager.shutdown()

        assert manager.progress.orders_drained
        assert manager.progress.websocket_closed
        assert manager.progress.metrics_flushed
        assert manager.progress.database_closed

    @pytest.mark.asyncio
    async def test_shutdown_records_start_and_end_times(self, manager):
        """Shutdown records start and completion timestamps."""
        await manager.shutdown()

        assert manager.progress.started_at is not None
        assert manager.progress.completed_at is not None
        assert manager.progress.completed_at >= manager.progress.started_at

    @pytest.mark.asyncio
    async def test_shutdown_callback_error_continues(self, manager):
        """Errors in callbacks don't stop shutdown."""
        async def failing_callback():
            raise RuntimeError("Test error")

        async def success_callback():
            pass

        manager.on_stop_new_work(failing_callback)
        manager.on_cleanup(success_callback)

        await manager.shutdown()

        # Should complete despite error
        assert manager.progress.phase == ShutdownPhase.COMPLETED
        assert len(manager.progress.errors) > 0
        assert any("Test error" in e for e in manager.progress.errors)

    @pytest.mark.asyncio
    async def test_shutdown_callback_timeout(self, manager):
        """Callbacks that take too long are timed out."""
        manager._timeout = 0.1  # Very short timeout

        async def slow_callback():
            await asyncio.sleep(10.0)  # Way longer than timeout

        manager.on_stop_new_work(slow_callback)

        await manager.shutdown()

        # Should complete despite timeout
        assert manager.progress.phase == ShutdownPhase.COMPLETED
        assert len(manager.progress.errors) > 0

    @pytest.mark.asyncio
    async def test_shutdown_idempotent(self, manager):
        """Calling shutdown multiple times only runs once."""
        call_count = 0

        async def count_calls():
            nonlocal call_count
            call_count += 1

        manager.on_cleanup(count_calls)

        # Start two shutdowns
        await asyncio.gather(
            manager.shutdown(),
            manager.shutdown(),
        )

        # Cleanup should only run once
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_shutdown_event_is_set(self, manager):
        """shutdown_event is set when shutdown completes."""
        assert not manager.shutdown_event.is_set()

        await manager.shutdown()

        assert manager.shutdown_event.is_set()

    @pytest.mark.asyncio
    async def test_wait_for_shutdown(self, manager):
        """wait_for_shutdown blocks until shutdown completes."""
        completed = False

        async def waiter():
            nonlocal completed
            await manager.wait_for_shutdown()
            completed = True

        # Start waiter
        wait_task = asyncio.create_task(waiter())

        # Give waiter time to start
        await asyncio.sleep(0.01)
        assert not completed

        # Trigger shutdown
        await manager.shutdown()

        # Waiter should complete
        await asyncio.wait_for(wait_task, timeout=1.0)
        assert completed

    @pytest.mark.asyncio
    async def test_in_flight_order_tracking(self, manager):
        """In-flight orders are tracked during drain phase."""
        order_count = 5

        def get_count():
            nonlocal order_count
            order_count -= 1
            return order_count

        manager.set_in_flight_tracker(get_count=get_count)

        await manager.shutdown()

        assert manager.progress.orders_drained

    @pytest.mark.asyncio
    async def test_drain_timeout_calls_force_cancel(self, manager):
        """Drain timeout triggers force cancel if configured."""
        manager._drain_timeout = 0.1  # Very short timeout
        force_cancel_called = False

        def get_count():
            return 5  # Always have orders

        async def force_cancel():
            nonlocal force_cancel_called
            force_cancel_called = True

        manager.set_in_flight_tracker(
            get_count=get_count,
            force_cancel=force_cancel,
        )

        await manager.shutdown()

        assert force_cancel_called
        assert any("Drain timeout" in e for e in manager.progress.errors)

    def test_trigger_shutdown_creates_task(self, manager):
        """trigger_shutdown creates an async task."""
        # Need an event loop
        async def test():
            triggered = False

            async def mark_triggered():
                nonlocal triggered
                triggered = True

            manager.on_cleanup(mark_triggered)
            manager.trigger_shutdown()

            # Give task time to run
            await asyncio.sleep(0.1)
            await manager.wait_for_shutdown()

            assert triggered

        asyncio.run(test())


class TestShutdownManagerSignalHandlers:
    """Tests for signal handler installation."""

    @pytest.fixture
    def manager(self):
        return ShutdownManager()

    @pytest.mark.asyncio
    async def test_install_signal_handlers(self, manager):
        """Signal handlers can be installed."""
        loop = asyncio.get_running_loop()

        # Install handlers
        manager.install_signal_handlers(loop)

        # Handlers should be installed (can't easily verify without signals)
        # Just verify no exceptions

        # Cleanup
        manager.remove_signal_handlers(loop)

    @pytest.mark.asyncio
    async def test_remove_signal_handlers(self, manager):
        """Signal handlers can be removed."""
        loop = asyncio.get_running_loop()

        manager.install_signal_handlers(loop)
        manager.remove_signal_handlers(loop)

        # Should not raise


class TestShutdownPhases:
    """Tests for shutdown phase enumeration."""

    def test_all_phases_defined(self):
        """All expected shutdown phases are defined."""
        phases = [
            ShutdownPhase.RUNNING,
            ShutdownPhase.SIGNAL_RECEIVED,
            ShutdownPhase.STOPPING_NEW_WORK,
            ShutdownPhase.DRAINING_ORDERS,
            ShutdownPhase.CLOSING_CONNECTIONS,
            ShutdownPhase.FLUSHING_DATA,
            ShutdownPhase.CLEANUP,
            ShutdownPhase.COMPLETED,
        ]

        for phase in phases:
            assert phase.value is not None

    def test_phases_are_strings(self):
        """Shutdown phases have string values."""
        assert ShutdownPhase.RUNNING.value == "running"
        assert ShutdownPhase.DRAINING_ORDERS.value == "draining_orders"
        assert ShutdownPhase.COMPLETED.value == "completed"
