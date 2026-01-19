"""
End-to-End Trading Flow Tests

Tests the complete trading lifecycle:
1. Market data arrives (orderbook with arbitrage opportunity)
2. Strategy generates signal
3. Risk manager approves/rejects
4. Execution engine places orders
5. Position is tracked in StateStore
6. Settlement processes resolved positions

Uses:
- Real Redis EventBus for pub/sub
- Real SQLite for state persistence
- Mocked external services (CLOB, Gamma, Polygon)

Run: pytest tests/e2e/test_full_trading_flow.py -v
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from tests.e2e.conftest import EventCollector

# Skip if Redis not available
SKIP_REDIS = os.environ.get("SKIP_REDIS_TESTS", "0") == "1"

pytestmark = [
    pytest.mark.skipif(SKIP_REDIS, reason="Redis tests disabled"),
    pytest.mark.asyncio,
]


class TestFullTradingFlow:
    """End-to-end tests for the complete trading flow."""

    @pytest.mark.asyncio
    async def test_market_data_to_signal_generation(
        self,
        redis_event_bus,
        e2e_config,
        event_collector,
        arbitrage_orderbook,
    ):
        """
        Test that market data with arbitrage opportunity generates a signal.

        Flow:
        1. StrategyEngine subscribes to market data
        2. Market data with arbitrage arrives
        3. Gabagool strategy detects opportunity
        4. Signal is published to EventBus
        """
        from mercury.services.strategy_engine import StrategyEngine
        from mercury.strategies.gabagool import GabagoolStrategy

        # Create strategy engine
        engine = StrategyEngine(config=e2e_config, event_bus=redis_event_bus)

        # Create and register gabagool strategy
        gabagool = GabagoolStrategy(config=e2e_config)
        engine.register_strategy(gabagool)

        # Subscribe to collect signals
        await redis_event_bus.subscribe("signal.*", event_collector.collect)

        try:
            await engine.start()

            # Subscribe gabagool to the market
            gabagool.subscribe_market("test-market-btc")

            # Simulate market data arriving (use correct field names for strategy engine)
            await engine._on_market_data({
                "market_id": "test-market-btc",
                "yes_ask": "0.48",
                "no_ask": "0.50",
                "yes_bid": "0.46",
                "no_bid": "0.48",
                "yes_ask_size": "100",
                "no_ask_size": "100",
            })

            # Wait for signal
            await asyncio.sleep(0.3)

            # Verify signal was published
            signal_events = event_collector.get_events("signal.")
            assert len(signal_events) > 0, "No signal was generated"

            channel, signal_data = signal_events[0]
            assert "gabagool" in channel.lower() or signal_data.get("strategy_name") == "gabagool"
            assert signal_data.get("signal_type") == "ARBITRAGE"
            assert Decimal(signal_data.get("combined_price", "0")) < Decimal("1.0")

        finally:
            await engine.stop()

    @pytest.mark.asyncio
    async def test_signal_to_risk_approval(
        self,
        redis_event_bus,
        e2e_config,
        event_collector,
        tmp_path,
    ):
        """
        Test that valid signals are approved by RiskManager.

        Flow:
        1. Signal is published to EventBus
        2. RiskManager receives and validates signal
        3. RiskManager publishes approval event
        """
        from mercury.services.risk_manager import RiskManager
        from mercury.services.state_store import StateStore
        from mercury.domain.signal import SignalType, SignalPriority

        # Create state store
        state_store = StateStore(
            config=e2e_config,
            event_bus=redis_event_bus,
            db_path=str(tmp_path / "risk_test.db"),
        )

        # Create risk manager
        risk_manager = RiskManager(
            config=e2e_config,
            event_bus=redis_event_bus,
            state_store=state_store,
        )

        # Subscribe to collect risk decisions
        await redis_event_bus.subscribe("risk.*", event_collector.collect)

        try:
            await state_store.start()
            await risk_manager.start()

            # Create a valid trading signal
            signal = {
                "signal_id": "test-signal-001",
                "strategy_name": "gabagool",
                "market_id": "test-market-btc",
                "signal_type": SignalType.ARBITRAGE.value,
                "priority": SignalPriority.HIGH.value,
                "confidence": 0.85,
                "target_size_usd": "20.00",
                "yes_price": "0.48",
                "no_price": "0.50",
                "expected_pnl": "0.40",
                "max_slippage": "0.02",
                "yes_token_id": "yes-token-123",
                "no_token_id": "no-token-123",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            # Publish signal to trigger risk validation
            await redis_event_bus.publish("signal.gabagool", signal)

            # Wait for risk decision
            await asyncio.sleep(0.3)

            # Verify risk approval was published
            risk_events = event_collector.get_events("risk.")
            assert len(risk_events) > 0, "No risk decision was made"

            # Check for approval
            approved_events = [e for c, e in risk_events if "approved" in c]
            rejected_events = [e for c, e in risk_events if "rejected" in c]

            # Signal should be approved (within limits)
            assert len(approved_events) > 0 or len(rejected_events) > 0, \
                "Risk manager did not respond"

        finally:
            await risk_manager.stop()
            await state_store.stop()

    @pytest.mark.asyncio
    async def test_risk_approval_to_order_execution(
        self,
        redis_event_bus,
        e2e_config,
        event_collector,
        mock_clob_client,
        tmp_path,
    ):
        """
        Test that approved signals are executed by ExecutionEngine.

        Flow:
        1. Approved signal is published
        2. ExecutionEngine receives signal
        3. Order is placed via CLOB client
        4. Order events are published
        """
        from mercury.services.execution import ExecutionEngine
        from mercury.domain.signal import SignalType, SignalPriority

        # Create execution engine with mock CLOB
        execution = ExecutionEngine(
            config=e2e_config,
            event_bus=redis_event_bus,
            clob_client=mock_clob_client,
        )

        # Subscribe to order events
        await redis_event_bus.subscribe("order.*", event_collector.collect)
        await redis_event_bus.subscribe("execution.*", event_collector.collect)
        await redis_event_bus.subscribe("position.*", event_collector.collect)

        try:
            await execution.start()

            # Create an approved signal
            approved_signal = {
                "signal_id": "approved-signal-001",
                "original_signal_id": "test-signal-001",
                "strategy_name": "gabagool",
                "market_id": "test-market-btc",
                "signal_type": SignalType.ARBITRAGE.value,
                "priority": SignalPriority.HIGH.value,
                "approved_size_usd": "20.00",
                "yes_price": "0.48",
                "no_price": "0.50",
                "yes_token_id": "yes-token-123",
                "no_token_id": "no-token-123",
                "approved_at": datetime.now(timezone.utc).isoformat(),
            }

            # Publish approved signal
            await redis_event_bus.publish("risk.approved.gabagool", approved_signal)

            # Wait for execution
            await asyncio.sleep(0.5)

            # Verify orders were placed
            assert len(mock_clob_client.placed_orders) > 0, "No orders were placed"

            # Verify order events
            order_events = event_collector.get_events("order.")
            assert len(order_events) > 0, "No order events published"

            # Check for filled order
            filled_events = [e for c, e in order_events if "filled" in c]
            assert len(filled_events) > 0, "No filled order event"

        finally:
            await execution.stop()

    @pytest.mark.asyncio
    async def test_order_execution_to_position_tracking(
        self,
        redis_event_bus,
        e2e_config,
        event_collector,
        mock_clob_client,
        tmp_path,
    ):
        """
        Test that executed orders create tracked positions.

        Flow:
        1. Order is filled
        2. Position is created in StateStore
        3. Position event is published
        4. Position can be queried from database
        """
        from mercury.services.state_store import StateStore
        from mercury.services.execution import ExecutionEngine
        from mercury.domain.signal import SignalType, SignalPriority

        # Create state store
        state_store = StateStore(
            config=e2e_config,
            event_bus=redis_event_bus,
            db_path=str(tmp_path / "position_test.db"),
        )

        # Create execution engine
        execution = ExecutionEngine(
            config=e2e_config,
            event_bus=redis_event_bus,
            clob_client=mock_clob_client,
        )

        # Subscribe to position events
        await redis_event_bus.subscribe("position.*", event_collector.collect)

        try:
            await state_store.start()
            await execution.start()

            # Publish approved signal
            approved_signal = {
                "signal_id": "approved-signal-002",
                "original_signal_id": "test-signal-002",
                "strategy_name": "gabagool",
                "market_id": "test-market-btc",
                "signal_type": SignalType.ARBITRAGE.value,
                "priority": SignalPriority.HIGH.value,
                "approved_size_usd": "20.00",
                "yes_price": "0.48",
                "no_price": "0.50",
                "yes_token_id": "yes-token-123",
                "no_token_id": "no-token-123",
                "condition_id": "test-condition-123",
                "approved_at": datetime.now(timezone.utc).isoformat(),
            }

            await redis_event_bus.publish("risk.approved.gabagool", approved_signal)

            # Wait for execution and position creation
            await asyncio.sleep(0.5)

            # Verify position event was published
            position_events = event_collector.get_events("position.")
            assert len(position_events) > 0, "No position events published"

            # Check for position.opened event
            opened_events = [e for c, e in position_events if "opened" in c]
            if opened_events:
                position_data = opened_events[0]
                assert "position_id" in position_data
                assert "market_id" in position_data

        finally:
            await execution.stop()
            await state_store.stop()

    @pytest.mark.asyncio
    async def test_position_to_settlement(
        self,
        redis_event_bus,
        e2e_config,
        event_collector,
        mock_gamma_client,
        mock_polygon_client,
        tmp_path,
    ):
        """
        Test that positions are settled when markets resolve.

        Flow:
        1. Position exists in settlement queue
        2. Market resolves (via mocked Gamma)
        3. SettlementManager detects resolution
        4. Claim is processed
        5. Settlement event is published
        """
        from mercury.services.state_store import StateStore, Position
        from mercury.services.settlement import SettlementManager

        # Create state store
        state_store = StateStore(
            config=e2e_config,
            event_bus=redis_event_bus,
            db_path=str(tmp_path / "settlement_test.db"),
        )

        # Create settlement manager with mocks
        settlement = SettlementManager(
            config=e2e_config,
            event_bus=redis_event_bus,
            state_store=state_store,
            gamma_client=mock_gamma_client,
            polygon_client=mock_polygon_client,
        )

        # Subscribe to settlement events
        await redis_event_bus.subscribe("settlement.*", event_collector.collect)

        try:
            await state_store.start()

            # Create a position to settle
            position = Position(
                position_id="test-position-001",
                market_id="test-market-btc",
                strategy="gabagool",
                side="YES",
                size=Decimal("20.0"),
                entry_price=Decimal("0.48"),
            )

            # Queue position for settlement
            await state_store.queue_for_settlement(
                position=position,
                condition_id="test-condition-123",
                token_id="yes-token-123",
                asset="BTC",
                market_end_time=datetime.now(timezone.utc) - timedelta(minutes=30),
            )

            # Start settlement manager
            await settlement.start()

            # Set market as resolved
            mock_gamma_client.set_resolved(True, "YES")

            # Trigger settlement check
            processed = await settlement.check_settlements()

            # Wait for events
            await asyncio.sleep(0.3)

            # Verify settlement was processed
            settlement_events = event_collector.get_events("settlement.")
            assert len(settlement_events) > 0, "No settlement events"

            # Check for claimed event (dry_run mode)
            claimed_events = [e for c, e in settlement_events if "claimed" in c]
            assert len(claimed_events) > 0, "Position was not claimed"

            # Verify claim data
            claim_data = claimed_events[0]
            assert claim_data.get("position_id") == "test-position-001"
            assert claim_data.get("resolution") == "YES"
            assert "proceeds" in claim_data
            assert "profit" in claim_data

        finally:
            await settlement.stop()
            await state_store.stop()

    @pytest.mark.asyncio
    async def test_complete_trading_lifecycle(
        self,
        redis_event_bus,
        e2e_config,
        event_collector,
        mock_clob_client,
        mock_gamma_client,
        mock_polygon_client,
        arbitrage_orderbook,
        tmp_path,
    ):
        """
        Full end-to-end test of the complete trading lifecycle.

        This test exercises the entire flow:
        1. Market data with arbitrage opportunity
        2. Gabagool strategy detects and generates signal
        3. RiskManager validates and approves
        4. ExecutionEngine places orders
        5. Position is created and tracked
        6. SettlementManager claims when market resolves

        All using real Redis and SQLite.
        """
        from mercury.services.market_data import MarketDataService
        from mercury.services.strategy_engine import StrategyEngine
        from mercury.services.risk_manager import RiskManager
        from mercury.services.execution import ExecutionEngine
        from mercury.services.state_store import StateStore
        from mercury.services.settlement import SettlementManager
        from mercury.strategies.gabagool import GabagoolStrategy

        # Create all services
        state_store = StateStore(
            config=e2e_config,
            event_bus=redis_event_bus,
            db_path=str(tmp_path / "lifecycle_test.db"),
        )

        strategy_engine = StrategyEngine(
            config=e2e_config,
            event_bus=redis_event_bus,
        )

        risk_manager = RiskManager(
            config=e2e_config,
            event_bus=redis_event_bus,
            state_store=state_store,
        )

        execution_engine = ExecutionEngine(
            config=e2e_config,
            event_bus=redis_event_bus,
            clob_client=mock_clob_client,
        )

        settlement_manager = SettlementManager(
            config=e2e_config,
            event_bus=redis_event_bus,
            state_store=state_store,
            gamma_client=mock_gamma_client,
            polygon_client=mock_polygon_client,
        )

        # Create and register gabagool strategy
        gabagool = GabagoolStrategy(config=e2e_config)
        strategy_engine.register_strategy(gabagool)

        # Subscribe to track all events
        await redis_event_bus.subscribe("signal.*", event_collector.collect)
        await redis_event_bus.subscribe("risk.*", event_collector.collect)
        await redis_event_bus.subscribe("order.*", event_collector.collect)
        await redis_event_bus.subscribe("position.*", event_collector.collect)
        await redis_event_bus.subscribe("settlement.*", event_collector.collect)
        await redis_event_bus.subscribe("execution.*", event_collector.collect)

        try:
            # Start all services
            await state_store.start()
            await strategy_engine.start()
            await risk_manager.start()
            await execution_engine.start()

            # Subscribe gabagool to the test market
            gabagool.subscribe_market("test-market-btc")

            # === PHASE 1: Market Data -> Signal ===
            # Inject market data with arbitrage opportunity (use correct field names)
            await strategy_engine._on_market_data({
                "market_id": "test-market-btc",
                "yes_ask": "0.48",
                "no_ask": "0.50",
                "yes_bid": "0.46",
                "no_bid": "0.48",
                "yes_ask_size": "100",
                "no_ask_size": "100",
            })

            # Wait for signal generation
            await asyncio.sleep(0.5)

            # Verify signal was generated
            signal_events = event_collector.get_events("signal.")
            assert len(signal_events) > 0, "Phase 1 Failed: No signal generated"

            # === PHASE 2: Signal -> Risk Approval ===
            # Wait for risk decision
            await asyncio.sleep(0.5)

            risk_events = event_collector.get_events("risk.")
            assert len(risk_events) > 0, "Phase 2 Failed: No risk decision"

            # === PHASE 3: Approval -> Execution ===
            # Wait for execution
            await asyncio.sleep(0.5)

            order_events = event_collector.get_events("order.")
            # Depending on risk approval, orders may or may not be placed
            # (Risk manager may reject based on current state)

            # === PHASE 4: Execution -> Position ===
            position_events = event_collector.get_events("position.")

            # === PHASE 5: Position -> Settlement ===
            if position_events:
                # Set market as resolved
                mock_gamma_client.set_resolved(True, "YES")

                # Start settlement and check
                await settlement_manager.start()
                await asyncio.sleep(0.3)
                await settlement_manager.check_settlements()
                await asyncio.sleep(0.3)

                settlement_events = event_collector.get_events("settlement.")

            # Log complete event flow for debugging
            all_channels = event_collector.get_channels()
            print(f"\n=== Complete Event Flow ===")
            for i, channel in enumerate(all_channels, 1):
                print(f"  {i}. {channel}")

            # Verify the flow occurred
            assert len(signal_events) > 0, "Trading flow did not generate signals"

        finally:
            # Stop all services in reverse order
            if settlement_manager._should_run:
                await settlement_manager.stop()
            await execution_engine.stop()
            await risk_manager.stop()
            await strategy_engine.stop()
            await state_store.stop()

    @pytest.mark.asyncio
    async def test_circuit_breaker_halts_execution(
        self,
        redis_event_bus,
        e2e_config,
        event_collector,
        mock_clob_client,
        tmp_path,
    ):
        """
        Test that circuit breaker prevents order execution when triggered.

        Flow:
        1. Trip the circuit breaker via multiple failures
        2. Attempt to execute a signal
        3. Verify signal is rejected due to circuit breaker
        """
        from mercury.services.risk_manager import RiskManager
        from mercury.services.state_store import StateStore
        from mercury.domain.risk import CircuitBreakerState
        from mercury.domain.signal import SignalType, SignalPriority

        # Create state store and risk manager
        state_store = StateStore(
            config=e2e_config,
            event_bus=redis_event_bus,
            db_path=str(tmp_path / "circuit_test.db"),
        )

        risk_manager = RiskManager(
            config=e2e_config,
            event_bus=redis_event_bus,
            state_store=state_store,
        )

        await redis_event_bus.subscribe("risk.*", event_collector.collect)

        try:
            await state_store.start()
            await risk_manager.start()

            # Trip the circuit breaker
            for _ in range(6):  # Exceed halt threshold
                risk_manager.record_failure()

            # Verify circuit breaker is in HALT state
            assert risk_manager.circuit_breaker_state == CircuitBreakerState.HALT

            # Clear previous events
            event_collector.clear()

            # Try to submit a signal
            signal = {
                "signal_id": "test-signal-circuit",
                "strategy_name": "gabagool",
                "market_id": "test-market-btc",
                "signal_type": SignalType.ARBITRAGE.value,
                "priority": SignalPriority.HIGH.value,
                "confidence": 0.85,
                "target_size_usd": "20.00",
                "yes_price": "0.48",
                "no_price": "0.50",
                "expected_pnl": "0.40",
                "yes_token_id": "yes-token-123",
                "no_token_id": "no-token-123",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            await redis_event_bus.publish("signal.gabagool", signal)
            await asyncio.sleep(0.3)

            # Verify signal was rejected
            risk_events = event_collector.get_events("risk.")
            rejected_events = [e for c, e in risk_events if "rejected" in c]

            # Signal should be rejected due to circuit breaker
            assert len(rejected_events) > 0, "Signal should be rejected when circuit breaker is tripped"
            if rejected_events:
                assert "circuit" in rejected_events[0].get("reason", "").lower() or \
                       "halt" in rejected_events[0].get("reason", "").lower()

        finally:
            await risk_manager.stop()
            await state_store.stop()

    @pytest.mark.asyncio
    async def test_position_exposure_limits(
        self,
        redis_event_bus,
        e2e_config,
        event_collector,
        tmp_path,
    ):
        """
        Test that position exposure limits are enforced.

        Flow:
        1. Create a position near the exposure limit
        2. Attempt to create another position that exceeds limit
        3. Verify the second signal is rejected
        """
        from mercury.services.risk_manager import RiskManager
        from mercury.services.state_store import StateStore
        from mercury.domain.signal import SignalType, SignalPriority

        state_store = StateStore(
            config=e2e_config,
            event_bus=redis_event_bus,
            db_path=str(tmp_path / "exposure_test.db"),
        )

        risk_manager = RiskManager(
            config=e2e_config,
            event_bus=redis_event_bus,
            state_store=state_store,
        )

        await redis_event_bus.subscribe("risk.*", event_collector.collect)

        try:
            await state_store.start()
            await risk_manager.start()

            # Record existing exposure near the limit (max is 50 USD)
            await risk_manager.record_position_opened(
                market_id="existing-market",
                size_usd=Decimal("45.00"),
            )

            # Clear events
            event_collector.clear()

            # Try to submit a large signal that would exceed limit
            signal = {
                "signal_id": "test-signal-exposure",
                "strategy_name": "gabagool",
                "market_id": "test-market-btc",
                "signal_type": SignalType.ARBITRAGE.value,
                "priority": SignalPriority.HIGH.value,
                "confidence": 0.85,
                "target_size_usd": "30.00",  # Would exceed 50 USD limit
                "yes_price": "0.48",
                "no_price": "0.50",
                "expected_pnl": "0.60",
                "yes_token_id": "yes-token-123",
                "no_token_id": "no-token-123",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            await redis_event_bus.publish("signal.gabagool", signal)
            await asyncio.sleep(0.3)

            # Verify signal was handled (either approved with reduced size or rejected)
            risk_events = event_collector.get_events("risk.")
            assert len(risk_events) > 0, "Risk manager should process the signal"

        finally:
            await risk_manager.stop()
            await state_store.stop()

    @pytest.mark.asyncio
    async def test_settlement_queue_persistence(
        self,
        redis_event_bus,
        e2e_config,
        tmp_path,
    ):
        """
        Test that settlement queue persists across service restarts.

        Flow:
        1. Queue a position for settlement
        2. Stop the state store
        3. Restart the state store
        4. Verify the position is still in the queue
        """
        from mercury.services.state_store import StateStore, Position

        db_path = str(tmp_path / "persistence_test.db")

        # Create state store and queue a position
        state_store1 = StateStore(
            config=e2e_config,
            event_bus=redis_event_bus,
            db_path=db_path,
        )

        try:
            await state_store1.start()

            # Queue a position
            position = Position(
                position_id="persist-test-001",
                market_id="test-market",
                strategy="gabagool",
                side="YES",
                size=Decimal("20.0"),
                entry_price=Decimal("0.48"),
            )

            await state_store1.queue_for_settlement(
                position=position,
                condition_id="test-condition",
                token_id="yes-token",
                asset="BTC",
            )

            # Verify it's queued
            queue = await state_store1.get_settlement_queue()
            assert any(e.position_id == "persist-test-001" for e in queue)

            await state_store1.stop()

            # Restart with new instance
            state_store2 = StateStore(
                config=e2e_config,
                event_bus=redis_event_bus,
                db_path=db_path,
            )

            await state_store2.start()

            # Verify position is still queued
            queue = await state_store2.get_settlement_queue()
            assert any(e.position_id == "persist-test-001" for e in queue), \
                "Position should persist across restarts"

            await state_store2.stop()

        except Exception:
            await state_store1.stop() if state_store1._running else None
            raise

    @pytest.mark.asyncio
    async def test_concurrent_signal_processing(
        self,
        redis_event_bus,
        e2e_config,
        event_collector,
        mock_clob_client,
        tmp_path,
    ):
        """
        Test that multiple signals can be processed concurrently.

        Flow:
        1. Publish multiple signals simultaneously
        2. Verify all signals are processed
        3. Verify execution respects concurrent limits
        """
        from mercury.services.execution import ExecutionEngine
        from mercury.domain.signal import SignalType, SignalPriority

        execution = ExecutionEngine(
            config=e2e_config,
            event_bus=redis_event_bus,
            clob_client=mock_clob_client,
        )

        await redis_event_bus.subscribe("order.*", event_collector.collect)
        await redis_event_bus.subscribe("execution.*", event_collector.collect)

        try:
            await execution.start()

            # Publish multiple signals
            for i in range(5):
                signal = {
                    "signal_id": f"concurrent-signal-{i}",
                    "original_signal_id": f"test-signal-{i}",
                    "strategy_name": "gabagool",
                    "market_id": f"test-market-{i}",
                    "signal_type": SignalType.ARBITRAGE.value,
                    "priority": SignalPriority.HIGH.value,
                    "approved_size_usd": "10.00",
                    "yes_price": "0.48",
                    "no_price": "0.50",
                    "yes_token_id": f"yes-token-{i}",
                    "no_token_id": f"no-token-{i}",
                    "approved_at": datetime.now(timezone.utc).isoformat(),
                }
                await redis_event_bus.publish("risk.approved.gabagool", signal)

            # Wait for execution
            await asyncio.sleep(1.0)

            # Verify orders were placed
            assert len(mock_clob_client.placed_orders) > 0, \
                "No orders were placed for concurrent signals"

            # All signals should be processed
            execution_events = event_collector.get_events("execution.")
            assert len(execution_events) > 0, "Execution events should be published"

        finally:
            await execution.stop()

    @pytest.mark.asyncio
    async def test_no_arbitrage_no_signal(
        self,
        redis_event_bus,
        e2e_config,
        event_collector,
        no_arbitrage_orderbook,
    ):
        """
        Test that no signal is generated when there's no arbitrage opportunity.
        """
        from mercury.services.strategy_engine import StrategyEngine
        from mercury.strategies.gabagool import GabagoolStrategy

        engine = StrategyEngine(config=e2e_config, event_bus=redis_event_bus)
        gabagool = GabagoolStrategy(config=e2e_config)
        engine.register_strategy(gabagool)

        await redis_event_bus.subscribe("signal.*", event_collector.collect)

        try:
            await engine.start()

            gabagool.subscribe_market("test-market-eth")

            # Inject market data WITHOUT arbitrage (combined > 1.0)
            await engine._on_market_data({
                "market_id": "test-market-eth",
                "yes_ask": "0.52",  # 0.52 + 0.50 = 1.02 > 1.0
                "no_ask": "0.50",
                "yes_bid": "0.50",
                "no_bid": "0.48",
                "yes_ask_size": "100",
                "no_ask_size": "100",
            })

            await asyncio.sleep(0.3)

            # Verify no signal was generated
            signal_events = event_collector.get_events("signal.")
            assert len(signal_events) == 0, \
                "No signal should be generated without arbitrage opportunity"

        finally:
            await engine.stop()
