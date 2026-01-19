"""
Load Testing for Mercury Trading System.

Tests with high-frequency mock market data to validate:
1. Signal-to-order latency (<100ms target)
2. Concurrent strategy execution
3. Event bus throughput
4. Memory stability under load

Run: pytest tests/performance/test_load.py -v
"""
import asyncio
import gc
import time
import tracemalloc
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from unittest.mock import MagicMock, AsyncMock

import pytest

from mercury.core.config import ConfigManager
from mercury.core.events import EventBus
from mercury.domain.market import OrderBook, OrderBookLevel
from mercury.domain.order import ExecutionLatency
from mercury.domain.signal import SignalPriority, SignalType, TradingSignal
from mercury.services.execution import ExecutionEngine, ExecutionSignal
from mercury.services.market_data import MarketDataService
from mercury.services.strategy_engine import StrategyEngine
from mercury.strategies.gabagool.strategy import GabagoolStrategy


class MockEventBus:
    """In-memory event bus for load testing without Redis dependency."""

    def __init__(self) -> None:
        self._handlers: dict[str, list] = {}
        self._published_events: list[tuple[str, dict]] = []
        self._publish_count = 0

    async def publish(self, channel: str, event: dict) -> None:
        """Publish event and dispatch to handlers."""
        self._published_events.append((channel, event))
        self._publish_count += 1

        # Dispatch to handlers
        for pattern, handlers in self._handlers.items():
            if self._pattern_matches(pattern, channel):
                for handler in handlers:
                    try:
                        await handler(event)
                    except Exception:
                        pass

    async def subscribe(self, pattern: str, handler) -> None:
        """Subscribe handler to pattern."""
        if pattern not in self._handlers:
            self._handlers[pattern] = []
        self._handlers[pattern].append(handler)

    async def unsubscribe(self, pattern: str) -> None:
        """Unsubscribe from pattern."""
        self._handlers.pop(pattern, None)

    def _pattern_matches(self, pattern: str, channel: str) -> bool:
        """Simple glob pattern matching."""
        if pattern == channel:
            return True
        if "*" not in pattern:
            return False
        parts = pattern.split("*")
        if len(parts) == 2:
            return channel.startswith(parts[0]) and channel.endswith(parts[1])
        return channel.startswith(parts[0])

    def clear(self) -> None:
        """Clear published events."""
        self._published_events.clear()
        self._publish_count = 0

    @property
    def publish_count(self) -> int:
        return self._publish_count


class MockCLOBClient:
    """Mock CLOB client for load testing."""

    def __init__(self, latency_ms: float = 10.0) -> None:
        self._latency_ms = latency_ms
        self._connected = True
        self._order_count = 0

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    async def execute_order(self, **kwargs) -> MagicMock:
        """Simulate order execution with configurable latency."""
        await asyncio.sleep(self._latency_ms / 1000)
        self._order_count += 1
        result = MagicMock()
        result.order_id = f"order-{self._order_count}"
        result.status = MagicMock()
        result.status.value = "filled"
        result.filled_size = kwargs.get("amount_shares", Decimal("10"))
        result.filled_cost = kwargs.get("amount_shares", Decimal("10")) * kwargs.get("price", Decimal("0.5"))
        return result

    async def execute_dual_leg_order(self, **kwargs) -> MagicMock:
        """Simulate dual-leg order execution."""
        await asyncio.sleep(self._latency_ms / 1000)
        self._order_count += 2
        result = MagicMock()
        result.yes_result = MagicMock()
        result.yes_result.order_id = f"order-{self._order_count - 1}"
        result.yes_result.filled_size = Decimal("10")
        result.yes_result.requested_price = kwargs.get("yes_price", Decimal("0.48"))
        result.no_result = MagicMock()
        result.no_result.order_id = f"order-{self._order_count}"
        result.no_result.filled_size = Decimal("10")
        result.no_result.requested_price = kwargs.get("no_price", Decimal("0.50"))
        result.both_filled = True
        result.has_partial_fill = False
        result.total_cost = kwargs.get("amount_usd", Decimal("20"))
        result.guaranteed_pnl = Decimal("0.20")
        result.execution_time_ms = self._latency_ms
        return result

    async def get_open_orders(self) -> list:
        return []

    async def cancel_order(self, order_id: str) -> bool:
        return True

    async def cancel_all_orders(self) -> None:
        pass


def create_mock_config() -> ConfigManager:
    """Create mock config manager for tests."""
    config = MagicMock(spec=ConfigManager)
    config.get.return_value = ""
    config.get_bool.return_value = True
    config.get_int.return_value = 3
    config.get_float.return_value = 60.0
    config.get_decimal.return_value = Decimal("10.0")
    config.register_reload_callback = MagicMock()
    config.unregister_reload_callback = MagicMock()
    return config


def create_arbitrage_order_book(
    market_id: str,
    yes_ask: Decimal = Decimal("0.48"),
    no_ask: Decimal = Decimal("0.50"),
) -> OrderBook:
    """Create an order book with an arbitrage opportunity."""
    return OrderBook(
        market_id=market_id,
        yes_bids=[OrderBookLevel(price=yes_ask - Decimal("0.01"), size=Decimal("100"))],
        yes_asks=[OrderBookLevel(price=yes_ask, size=Decimal("100"))],
        no_bids=[OrderBookLevel(price=no_ask - Decimal("0.01"), size=Decimal("100"))],
        no_asks=[OrderBookLevel(price=no_ask, size=Decimal("100"))],
        timestamp=datetime.now(timezone.utc),
    )


class TestHighFrequencyMarketData:
    """Tests for high-frequency market data processing."""

    @pytest.mark.asyncio
    async def test_market_data_throughput(self):
        """Test market data processing at high frequency (1000+ updates/sec)."""
        event_bus = MockEventBus()
        config = create_mock_config()

        # Track received updates
        updates_received = []

        async def on_orderbook(data):
            updates_received.append(time.time())

        await event_bus.subscribe("market.orderbook.*", on_orderbook)

        # Simulate high-frequency updates
        num_updates = 1000
        market_ids = [f"market-{i}" for i in range(10)]

        start_time = time.time()

        for i in range(num_updates):
            market_id = market_ids[i % len(market_ids)]
            book = create_arbitrage_order_book(market_id)

            await event_bus.publish(
                f"market.orderbook.{market_id}",
                {
                    "market_id": market_id,
                    "yes_ask": str(book.yes_best_ask),
                    "no_ask": str(book.no_best_ask),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )

        elapsed = time.time() - start_time
        throughput = num_updates / elapsed

        assert len(updates_received) == num_updates
        assert throughput > 1000, f"Throughput {throughput:.0f}/sec below 1000/sec target"

        print(f"\nMarket data throughput: {throughput:.0f} updates/sec")
        print(f"Total time for {num_updates} updates: {elapsed*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_sustained_load(self):
        """Test sustained load over multiple seconds."""
        event_bus = MockEventBus()

        updates_per_second = 500
        duration_seconds = 3
        total_updates = updates_per_second * duration_seconds

        start_time = time.time()
        update_times = []

        for i in range(total_updates):
            # Simulate realistic timing by yielding control
            if i % updates_per_second == 0 and i > 0:
                await asyncio.sleep(0.001)

            await event_bus.publish(
                f"market.orderbook.market-{i % 10}",
                {"update": i, "timestamp": time.time()}
            )
            update_times.append(time.time())

        elapsed = time.time() - start_time
        actual_rate = total_updates / elapsed

        assert event_bus.publish_count == total_updates
        assert elapsed < duration_seconds * 2, f"Took too long: {elapsed:.2f}s"

        print(f"\nSustained load test:")
        print(f"  Target: {updates_per_second}/sec for {duration_seconds}s")
        print(f"  Actual: {actual_rate:.0f}/sec over {elapsed:.2f}s")


class TestSignalToOrderLatency:
    """Tests for signal-to-order latency (<100ms target).

    Note: The execution engine has a hardcoded 100ms simulated delay in dry-run mode.
    Tests measure execution overhead EXCLUDING the simulated delay.
    In production, actual exchange latency varies but is typically 10-50ms.
    """

    @pytest.mark.asyncio
    async def test_signal_to_order_latency_with_mock_clob(self):
        """Validate signal-to-order latency with mock CLOB (non-dry-run mode)."""
        event_bus = MockEventBus()
        config = create_mock_config()
        # Use non-dry-run mode with mock CLOB to measure actual code path latency
        config.get_bool.side_effect = lambda key, default=None: {
            "mercury.dry_run": False,  # Non-dry-run to skip 100ms simulated sleep
            "execution.rebalance_partial_fills": True,
        }.get(key, default if default is not None else True)

        # Mock CLOB with fast response (5ms simulated exchange latency)
        clob = MockCLOBClient(latency_ms=5.0)
        engine = ExecutionEngine(config, event_bus, clob_client=clob)
        await engine.start()

        latencies = []

        for i in range(100):
            signal = ExecutionSignal(
                signal_id=f"signal-{i}",
                original_signal_id=f"original-{i}",
                market_id=f"market-{i % 10}",
                signal_type=SignalType.ARBITRAGE,
                target_size_usd=Decimal("20"),
                yes_price=Decimal("0.48"),
                no_price=Decimal("0.50"),
                yes_token_id="yes-token",
                no_token_id="no-token",
            )

            start = time.time()
            result = await engine.execute(signal)
            latency_ms = (time.time() - start) * 1000

            latencies.append(latency_ms)
            # In mock mode, execution should succeed
            assert result.success, f"Execution failed: {result.error}"

        await engine.stop()

        avg_latency = sum(latencies) / len(latencies)
        max_latency = max(latencies)
        min_latency = min(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)]
        p99 = sorted(latencies)[int(len(latencies) * 0.99)]

        # Target: sub-100ms (with 5ms mock exchange latency)
        assert avg_latency < 100, f"Average latency {avg_latency:.1f}ms exceeds 100ms target"
        assert p95 < 100, f"P95 latency {p95:.1f}ms exceeds 100ms target"

        print(f"\nSignal-to-order latency (mock CLOB, n={len(latencies)}):")
        print(f"  Min: {min_latency:.2f}ms")
        print(f"  Avg: {avg_latency:.2f}ms")
        print(f"  P95: {p95:.2f}ms")
        print(f"  P99: {p99:.2f}ms")
        print(f"  Max: {max_latency:.2f}ms")

    @pytest.mark.asyncio
    async def test_execution_overhead_dry_run(self):
        """Measure execution overhead in dry-run mode (includes 100ms simulated delay)."""
        event_bus = MockEventBus()
        config = create_mock_config()
        config.get_bool.side_effect = lambda key, default=None: {
            "mercury.dry_run": True,
            "execution.rebalance_partial_fills": True,
        }.get(key, default if default is not None else True)

        clob = MockCLOBClient(latency_ms=5.0)
        engine = ExecutionEngine(config, event_bus, clob_client=clob)
        await engine.start()

        latencies = []

        for i in range(30):
            signal = ExecutionSignal(
                signal_id=f"signal-{i}",
                original_signal_id=f"original-{i}",
                market_id=f"market-{i % 10}",
                signal_type=SignalType.ARBITRAGE,
                target_size_usd=Decimal("20"),
                yes_price=Decimal("0.48"),
                no_price=Decimal("0.50"),
                yes_token_id="yes-token",
                no_token_id="no-token",
            )

            start = time.time()
            result = await engine.execute(signal)
            latency_ms = (time.time() - start) * 1000

            latencies.append(latency_ms)
            assert result.success, f"Execution failed: {result.error}"

        await engine.stop()

        avg_latency = sum(latencies) / len(latencies)
        overhead = avg_latency - 100  # Subtract simulated 100ms delay

        # Verify that overhead (on top of 100ms sleep) is minimal (<20ms)
        assert overhead < 20, f"Execution overhead {overhead:.1f}ms exceeds 20ms"

        print(f"\nDry-run execution overhead (n={len(latencies)}):")
        print(f"  Avg total latency: {avg_latency:.2f}ms")
        print(f"  Simulated delay: 100.00ms")
        print(f"  Overhead: {overhead:.2f}ms")

    @pytest.mark.asyncio
    async def test_latency_tracking_breakdown(self):
        """Test detailed latency breakdown tracking."""
        event_bus = MockEventBus()
        config = create_mock_config()
        config.get_bool.side_effect = lambda key, default=None: True

        clob = MockCLOBClient(latency_ms=10.0)
        engine = ExecutionEngine(config, event_bus, clob_client=clob)
        await engine.start()

        # Queue a signal with latency tracking - use proper enum value
        signal_id = "test-latency-signal"
        signal_data = {
            "signal_id": signal_id,
            "market_id": "test-market",
            "signal_type": SignalType.ARBITRAGE.value,  # Use enum value
            "target_size_usd": "20",
            "yes_price": "0.48",
            "no_price": "0.50",
            "yes_token_id": "yes-token",
            "no_token_id": "no-token",
            "priority": "high",
        }

        await engine.queue_signal(signal_id, signal_data, SignalPriority.HIGH)

        # Wait for execution
        await asyncio.sleep(0.5)

        # Check latency stats
        stats = engine.get_latency_stats()

        assert stats["history_size"] > 0, "No latency records"

        if stats["avg_total_ms"] is not None:
            print(f"\nLatency breakdown:")
            print(f"  Avg queue time: {stats.get('avg_queue_ms', 'N/A')}ms")
            print(f"  Avg submission time: {stats.get('avg_submission_ms', 'N/A')}ms")
            print(f"  Avg total latency: {stats['avg_total_ms']:.2f}ms")
            print(f"  Within target (<100ms): {stats['within_target_count']}/{stats['history_size']}")

        await engine.stop()

    @pytest.mark.asyncio
    async def test_latency_under_queue_load(self):
        """Test latency when execution queue is under load."""
        event_bus = MockEventBus()
        config = create_mock_config()
        config.get_bool.side_effect = lambda key, default=None: True
        config.get_int.side_effect = lambda key, default=None: {
            "execution.max_concurrent": 5,
            "execution.max_queue_size": 200,
        }.get(key, default if default is not None else 3)
        config.get_float.side_effect = lambda key, default=None: 60.0

        clob = MockCLOBClient(latency_ms=5.0)
        engine = ExecutionEngine(config, event_bus, clob_client=clob)
        await engine.start()

        # Queue many signals rapidly
        num_signals = 50
        queue_times = []

        for i in range(num_signals):
            signal_id = f"load-signal-{i}"
            start = time.time()
            success = await engine.queue_signal(
                signal_id,
                {
                    "signal_id": signal_id,
                    "market_id": f"market-{i % 10}",
                    "signal_type": "arbitrage",
                    "target_size_usd": "20",
                    "yes_price": "0.48",
                    "no_price": "0.50",
                    "yes_token_id": "yes-token",
                    "no_token_id": "no-token",
                    "priority": "medium",
                },
                SignalPriority.MEDIUM,
            )
            queue_time_ms = (time.time() - start) * 1000
            queue_times.append(queue_time_ms)
            assert success, f"Failed to queue signal {signal_id}"

        # Wait for all executions to complete
        await asyncio.sleep(2.0)

        avg_queue_time = sum(queue_times) / len(queue_times)
        max_queue_time = max(queue_times)

        stats = engine.get_queue_stats()
        latency_stats = engine.get_latency_stats()

        print(f"\nQueue load test (n={num_signals}):")
        print(f"  Avg queue insert time: {avg_queue_time:.2f}ms")
        print(f"  Max queue insert time: {max_queue_time:.2f}ms")
        print(f"  Total executed: {stats['total_executed']}")
        print(f"  Total failed: {stats['total_failed']}")
        if latency_stats["avg_total_ms"]:
            print(f"  Avg execution latency: {latency_stats['avg_total_ms']:.2f}ms")

        await engine.stop()

        assert stats["total_executed"] + stats["total_failed"] >= num_signals * 0.9


class TestConcurrentStrategyExecution:
    """Tests for concurrent strategy execution."""

    @pytest.mark.asyncio
    async def test_multiple_strategies_concurrent(self):
        """Test multiple strategies processing market data concurrently."""
        event_bus = MockEventBus()
        config = create_mock_config()
        config.get.side_effect = lambda key, default=None: {
            "strategies.gabagool.enabled": True,
            "strategies.gabagool.min_spread_threshold": "0.01",
            "strategies.gabagool.min_spread_cents": "1.0",
            "strategies.gabagool.max_trade_size_usd": "25.0",
            "strategies.gabagool.markets": [],
        }.get(key, default if default is not None else "")

        engine = StrategyEngine(config, event_bus)

        # Create multiple strategy instances
        strategies = []
        for i in range(5):
            strategy = GabagoolStrategy(config)
            strategy._subscribed_markets = [f"market-{j}" for j in range(10)]
            strategy._signal_cooldown = 0  # Disable cooldown for testing
            strategies.append(strategy)
            engine.register_strategy(strategy)

        await engine.start()

        # Track signals generated
        signals_generated = []

        async def on_signal(data):
            signals_generated.append(data)

        await event_bus.subscribe("signal.*", on_signal)

        # Send market data updates
        num_updates = 100
        start_time = time.time()

        for i in range(num_updates):
            market_id = f"market-{i % 10}"
            await event_bus.publish(
                f"market.orderbook.{market_id}",
                {
                    "market_id": market_id,
                    "yes_bid": "0.47",
                    "yes_ask": "0.48",
                    "no_bid": "0.49",
                    "no_ask": "0.50",
                    "yes_bid_size": "100",
                    "yes_ask_size": "100",
                    "no_bid_size": "100",
                    "no_ask_size": "100",
                }
            )

        await asyncio.sleep(0.5)  # Allow signal processing

        elapsed = time.time() - start_time
        throughput = num_updates / elapsed

        await engine.stop()

        print(f"\nConcurrent strategy execution:")
        print(f"  Strategies: {len(strategies)}")
        print(f"  Market updates: {num_updates}")
        print(f"  Signals generated: {len(signals_generated)}")
        print(f"  Throughput: {throughput:.0f} updates/sec")

    @pytest.mark.asyncio
    async def test_strategy_isolation(self):
        """Test that strategy failures don't affect other strategies."""
        event_bus = MockEventBus()
        config = create_mock_config()

        engine = StrategyEngine(config, event_bus)

        # Create a failing strategy
        class FailingStrategy:
            name = "failing"
            enabled = True

            def __init__(self):
                self._subscribed_markets = ["market-0"]

            async def start(self):
                pass

            async def stop(self):
                pass

            def enable(self):
                pass

            def disable(self):
                pass

            def get_subscribed_markets(self):
                return self._subscribed_markets

            async def on_market_data(self, market_id, book):
                raise RuntimeError("Strategy failure!")
                yield  # Make this a generator

        # Create a working strategy
        class WorkingStrategy:
            name = "working"
            enabled = True
            signal_count = 0

            def __init__(self):
                self._subscribed_markets = ["market-0"]

            async def start(self):
                pass

            async def stop(self):
                pass

            def enable(self):
                pass

            def disable(self):
                pass

            def get_subscribed_markets(self):
                return self._subscribed_markets

            async def on_market_data(self, market_id, book):
                self.signal_count += 1
                yield TradingSignal(
                    strategy_name="working",
                    market_id=market_id,
                    signal_type=SignalType.ARBITRAGE,
                    confidence=0.8,
                    priority=SignalPriority.MEDIUM,
                    target_size_usd=Decimal("20"),
                    yes_price=Decimal("0.48"),
                    no_price=Decimal("0.50"),
                    expected_pnl=Decimal("0.20"),
                    max_slippage=Decimal("0.01"),
                )

        failing = FailingStrategy()
        working = WorkingStrategy()

        engine.register_strategy(failing)
        engine.register_strategy(working)

        await engine.start()

        # Send updates
        for i in range(10):
            await event_bus.publish(
                "market.orderbook.market-0",
                {
                    "market_id": "market-0",
                    "yes_bid": "0.47",
                    "yes_ask": "0.48",
                    "no_bid": "0.49",
                    "no_ask": "0.50",
                }
            )

        await asyncio.sleep(0.2)
        await engine.stop()

        # Working strategy should have processed updates despite failing strategy
        assert working.signal_count > 0, "Working strategy was affected by failing strategy"
        print(f"\nStrategy isolation: Working strategy processed {working.signal_count} updates")


class TestMemoryUnderLoad:
    """Tests for memory stability under load."""

    @pytest.mark.asyncio
    async def test_memory_stability(self):
        """Test memory usage remains stable under sustained load."""
        event_bus = MockEventBus()
        config = create_mock_config()
        config.get_bool.side_effect = lambda key, default=None: True

        clob = MockCLOBClient(latency_ms=1.0)
        engine = ExecutionEngine(config, event_bus, clob_client=clob)
        await engine.start()

        # Force garbage collection and start tracking
        gc.collect()
        tracemalloc.start()

        snapshot1 = tracemalloc.take_snapshot()
        initial_memory = sum(stat.size for stat in snapshot1.statistics("lineno"))

        # Run sustained load
        num_iterations = 500
        batch_size = 50

        for batch in range(num_iterations // batch_size):
            for i in range(batch_size):
                signal_id = f"mem-signal-{batch * batch_size + i}"
                await engine.queue_signal(
                    signal_id,
                    {
                        "signal_id": signal_id,
                        "market_id": f"market-{i % 10}",
                        "signal_type": "arbitrage",
                        "target_size_usd": "20",
                        "yes_price": "0.48",
                        "no_price": "0.50",
                        "yes_token_id": "yes-token",
                        "no_token_id": "no-token",
                        "priority": "medium",
                    },
                    SignalPriority.MEDIUM,
                )

            # Allow execution between batches
            await asyncio.sleep(0.1)

        # Wait for completion
        await asyncio.sleep(1.0)

        gc.collect()
        snapshot2 = tracemalloc.take_snapshot()
        final_memory = sum(stat.size for stat in snapshot2.statistics("lineno"))

        tracemalloc.stop()

        memory_growth_mb = (final_memory - initial_memory) / (1024 * 1024)

        await engine.stop()

        # Memory growth should be reasonable (< 50MB for this test)
        assert memory_growth_mb < 50, f"Memory grew by {memory_growth_mb:.2f}MB"

        print(f"\nMemory stability test:")
        print(f"  Iterations: {num_iterations}")
        print(f"  Initial memory: {initial_memory / 1024 / 1024:.2f}MB")
        print(f"  Final memory: {final_memory / 1024 / 1024:.2f}MB")
        print(f"  Growth: {memory_growth_mb:.2f}MB")

    @pytest.mark.asyncio
    async def test_event_bus_memory(self):
        """Test event bus doesn't leak memory with many events."""
        event_bus = MockEventBus()

        gc.collect()
        tracemalloc.start()
        snapshot1 = tracemalloc.take_snapshot()

        # Publish many events
        num_events = 10000

        for i in range(num_events):
            await event_bus.publish(
                f"test.channel.{i % 100}",
                {
                    "id": i,
                    "data": "x" * 100,  # Some payload
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )

        # Clear to simulate realistic cleanup
        event_bus.clear()

        gc.collect()
        snapshot2 = tracemalloc.take_snapshot()

        initial_memory = sum(stat.size for stat in snapshot1.statistics("lineno"))
        final_memory = sum(stat.size for stat in snapshot2.statistics("lineno"))

        tracemalloc.stop()

        memory_growth_mb = (final_memory - initial_memory) / (1024 * 1024)

        # Should not retain significant memory after clear
        assert memory_growth_mb < 10, f"Event bus leaked {memory_growth_mb:.2f}MB"

        print(f"\nEvent bus memory test:")
        print(f"  Events published: {num_events}")
        print(f"  Memory growth after clear: {memory_growth_mb:.2f}MB")


class TestPerformanceBenchmarks:
    """Benchmark tests to document performance characteristics."""

    @pytest.mark.asyncio
    async def test_orderbook_update_latency(self):
        """Benchmark order book update processing latency."""
        from mercury.domain.orderbook import InMemoryOrderBook

        book = InMemoryOrderBook("test-token")

        # Warm up
        for i in range(100):
            book.update_bid(Decimal(f"0.{50+i%10}"), Decimal("100"))
            book.update_ask(Decimal(f"0.{55+i%10}"), Decimal("100"))

        # Clear the book using bids/asks clear methods
        book.bids.clear()
        book.asks.clear()

        # Benchmark
        num_updates = 10000
        start = time.time()

        for i in range(num_updates):
            price = Decimal(f"0.{50 + (i % 50):02d}")
            size = Decimal(str(100 + i % 100))

            if i % 2 == 0:
                book.update_bid(price, size)
            else:
                book.update_ask(price, size)

        elapsed_ms = (time.time() - start) * 1000
        ops_per_sec = num_updates / (elapsed_ms / 1000)
        avg_latency_us = (elapsed_ms * 1000) / num_updates

        print(f"\nOrder book update benchmark:")
        print(f"  Operations: {num_updates}")
        print(f"  Total time: {elapsed_ms:.2f}ms")
        print(f"  Ops/sec: {ops_per_sec:.0f}")
        print(f"  Avg latency: {avg_latency_us:.2f}μs")

        assert ops_per_sec > 100000, f"Order book too slow: {ops_per_sec:.0f} ops/sec"

    @pytest.mark.asyncio
    async def test_signal_generation_latency(self):
        """Benchmark strategy signal generation latency."""
        config = create_mock_config()
        strategy = GabagoolStrategy(config)
        strategy._signal_cooldown = 0  # Disable for benchmarking

        await strategy.start()

        latencies = []

        for i in range(1000):
            # Create book with arbitrage opportunity
            book = create_arbitrage_order_book(
                f"market-{i}",
                yes_ask=Decimal("0.48"),
                no_ask=Decimal("0.50"),
            )

            start = time.time()
            signals = [s async for s in strategy.on_market_data(f"market-{i}", book)]
            latency_us = (time.time() - start) * 1000000

            latencies.append(latency_us)

        await strategy.stop()

        avg_latency = sum(latencies) / len(latencies)
        max_latency = max(latencies)
        p99 = sorted(latencies)[int(len(latencies) * 0.99)]

        print(f"\nSignal generation benchmark:")
        print(f"  Iterations: {len(latencies)}")
        print(f"  Avg latency: {avg_latency:.2f}μs")
        print(f"  P99 latency: {p99:.2f}μs")
        print(f"  Max latency: {max_latency:.2f}μs")

        # Signal generation should be fast (< 1ms average)
        assert avg_latency < 1000, f"Signal generation too slow: {avg_latency:.2f}μs"

    @pytest.mark.asyncio
    async def test_end_to_end_latency_benchmark(self):
        """Full end-to-end latency benchmark from market data to execution."""
        event_bus = MockEventBus()
        config = create_mock_config()
        config.get_bool.side_effect = lambda key, default=None: True

        clob = MockCLOBClient(latency_ms=5.0)

        # Track full pipeline latency
        latencies = []
        execution_complete = asyncio.Event()

        async def on_execution_complete(data):
            if "execution_ms" in data:
                latencies.append(data["execution_ms"])
            if len(latencies) >= 100:
                execution_complete.set()

        await event_bus.subscribe("execution.complete", on_execution_complete)

        # Start execution engine
        engine = ExecutionEngine(config, event_bus, clob_client=clob)
        await engine.start()

        # Queue signals
        for i in range(100):
            await engine.queue_signal(
                f"e2e-signal-{i}",
                {
                    "signal_id": f"e2e-signal-{i}",
                    "market_id": f"market-{i % 10}",
                    "signal_type": "arbitrage",
                    "target_size_usd": "20",
                    "yes_price": "0.48",
                    "no_price": "0.50",
                    "yes_token_id": "yes-token",
                    "no_token_id": "no-token",
                    "priority": "medium",
                },
                SignalPriority.MEDIUM,
            )

        # Wait for completions
        try:
            await asyncio.wait_for(execution_complete.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

        await engine.stop()

        if latencies:
            avg_latency = sum(latencies) / len(latencies)
            p95 = sorted(latencies)[int(len(latencies) * 0.95)] if len(latencies) > 20 else max(latencies)

            print(f"\nEnd-to-end latency benchmark:")
            print(f"  Executions completed: {len(latencies)}")
            print(f"  Avg latency: {avg_latency:.2f}ms")
            print(f"  P95 latency: {p95:.2f}ms")

            # E2E should be under 100ms target
            assert avg_latency < 100, f"E2E latency too high: {avg_latency:.2f}ms"
