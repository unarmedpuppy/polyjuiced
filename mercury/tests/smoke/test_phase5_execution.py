"""
Phase 5 Smoke Test: Execution Engine

Verifies that Phase 5 deliverables work:
- ExecutionEngine processes orders
- Single order execution works
- Dual-leg arbitrage execution works
- Order cancellation works
- Retry logic works
- Latency tracking works

Run: pytest tests/smoke/test_phase5_execution.py -v
"""
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock


class TestPhase5ExecutionEngine:
    """Phase 5 must pass ALL these tests to be considered complete."""

    def test_execution_engine_importable(self):
        """Verify ExecutionEngine can be imported."""
        from mercury.services.execution import ExecutionEngine
        assert ExecutionEngine is not None

    @pytest.mark.asyncio
    async def test_execution_engine_starts_stops(self, mock_config, mock_event_bus):
        """Verify ExecutionEngine lifecycle works."""
        from mercury.services.execution import ExecutionEngine

        mock_clob = MagicMock()
        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )

        await engine.start()
        assert engine.is_running

        await engine.stop()
        assert not engine.is_running

    @pytest.mark.asyncio
    async def test_single_order_execution(self, mock_config, mock_event_bus):
        """Verify single order execution works."""
        from mercury.services.execution import ExecutionEngine
        from mercury.domain.order import OrderRequest, OrderResult

        mock_clob = MagicMock()
        mock_clob.place_order = AsyncMock(return_value=OrderResult(
            order_id="test-order-1",
            status="filled",
            filled_size=Decimal("10.0"),
            price=Decimal("0.50"),
        ))

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )

        order = OrderRequest(
            market_id="test-market",
            token_id="test-token",
            side="BUY",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
            order_type="FOK",
        )

        result = await engine.execute(order)

        assert result.status == "filled"
        assert result.filled_size == Decimal("10.0")
        mock_event_bus.publish.assert_called()

    @pytest.mark.asyncio
    async def test_dual_leg_execution(self, mock_config, mock_event_bus):
        """Verify dual-leg arbitrage execution works."""
        from mercury.services.execution import ExecutionEngine
        from mercury.domain.order import OrderRequest, OrderResult

        mock_clob = MagicMock()
        mock_clob.place_order = AsyncMock(return_value=OrderResult(
            order_id="test-order",
            status="filled",
            filled_size=Decimal("10.0"),
            price=Decimal("0.50"),
        ))

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )

        yes_order = OrderRequest(
            market_id="test-market",
            token_id="yes-token",
            side="BUY",
            size=Decimal("10.0"),
            price=Decimal("0.48"),
            order_type="FOK",
        )

        no_order = OrderRequest(
            market_id="test-market",
            token_id="no-token",
            side="BUY",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
            order_type="FOK",
        )

        result = await engine.execute_dual_leg(yes_order, no_order)

        assert result.yes_result.status == "filled"
        assert result.no_result.status == "filled"
        assert mock_clob.place_order.call_count == 2

    @pytest.mark.asyncio
    async def test_order_cancellation(self, mock_config, mock_event_bus):
        """Verify order cancellation works."""
        from mercury.services.execution import ExecutionEngine

        mock_clob = MagicMock()
        mock_clob.cancel_order = AsyncMock(return_value=True)

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )

        result = await engine.cancel("test-order-id")

        assert result is True
        mock_clob.cancel_order.assert_called_once_with("test-order-id")

    @pytest.mark.asyncio
    async def test_retry_on_transient_error(self, mock_config, mock_event_bus):
        """Verify retry logic works for transient errors."""
        from mercury.services.execution import ExecutionEngine
        from mercury.domain.order import OrderRequest, OrderResult

        call_count = 0

        async def flaky_place_order(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("Transient error")
            return OrderResult(
                order_id="test-order",
                status="filled",
                filled_size=Decimal("10.0"),
                price=Decimal("0.50"),
            )

        mock_clob = MagicMock()
        mock_clob.place_order = AsyncMock(side_effect=flaky_place_order)

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )

        order = OrderRequest(
            market_id="test-market",
            token_id="test-token",
            side="BUY",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
            order_type="FOK",
        )

        result = await engine.execute(order)

        assert result.status == "filled"
        assert call_count == 3  # Two failures, then success

    @pytest.mark.asyncio
    async def test_latency_tracking(self, mock_config, mock_event_bus):
        """Verify execution latency is tracked."""
        from mercury.services.execution import ExecutionEngine
        from mercury.domain.order import OrderRequest, OrderResult

        mock_clob = MagicMock()
        mock_clob.place_order = AsyncMock(return_value=OrderResult(
            order_id="test-order",
            status="filled",
            filled_size=Decimal("10.0"),
            price=Decimal("0.50"),
        ))

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )

        order = OrderRequest(
            market_id="test-market",
            token_id="test-token",
            side="BUY",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
            order_type="FOK",
        )

        result = await engine.execute(order)

        # Check latency was recorded
        assert hasattr(result, 'latency_ms') or engine.last_latency_ms is not None

    @pytest.mark.asyncio
    async def test_emits_order_events(self, mock_config, mock_event_bus):
        """Verify order lifecycle events are emitted."""
        from mercury.services.execution import ExecutionEngine
        from mercury.domain.order import OrderRequest, OrderResult

        mock_clob = MagicMock()
        mock_clob.place_order = AsyncMock(return_value=OrderResult(
            order_id="test-order",
            status="filled",
            filled_size=Decimal("10.0"),
            price=Decimal("0.50"),
        ))

        engine = ExecutionEngine(
            config=mock_config,
            event_bus=mock_event_bus,
            clob_client=mock_clob,
        )

        order = OrderRequest(
            market_id="test-market",
            token_id="test-token",
            side="BUY",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
            order_type="FOK",
        )

        await engine.execute(order)

        # Should emit order.submitted and order.filled
        calls = mock_event_bus.publish.call_args_list
        channels = [call[0][0] for call in calls]

        assert any("order.submitted" in c for c in channels)
        assert any("order.filled" in c for c in channels)
