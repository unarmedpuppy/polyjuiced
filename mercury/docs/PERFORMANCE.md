# Mercury Performance Characteristics

This document describes the performance characteristics of the Mercury trading system, including latency targets, throughput capabilities, and memory usage patterns.

## Overview

Mercury is designed for low-latency trading with a target of **sub-100ms signal-to-order execution**. The system uses an event-driven architecture to minimize coupling and maximize throughput.

## Latency Targets

### Signal-to-Order Latency

| Metric | Target | Description |
|--------|--------|-------------|
| Average | <50ms | Typical execution time |
| P95 | <100ms | 95th percentile |
| P99 | <150ms | 99th percentile |
| Max | <500ms | Maximum acceptable |

### Latency Breakdown

The execution pipeline has several components:

```
Signal Received → Queue → Dequeue → Submit → Fill
     |              |        |         |        |
     +--- ~1ms -----+        |         |        |
                             |         |        |
                +--- ~1-50ms +         |        |
                                       |        |
                           +--- ~5-20ms+        |
                                                |
                               +--- ~10-50ms ---+
```

1. **Queue Time**: Time spent in the execution queue (1-50ms depending on load)
2. **Submission Time**: Time to submit order to exchange (5-20ms)
3. **Fill Time**: Time from submission to fill confirmation (10-50ms)

### Measuring Latency

Latency is tracked via the `ExecutionLatency` class which provides:

```python
latency = ExecutionLatency(
    signal_id="sig-001",
    signal_received_at=datetime.now(),
    queue_entered_at=...,
    queue_exited_at=...,
    submission_started_at=...,
    submission_completed_at=...,
    fill_completed_at=...,
)

# Access metrics
print(f"Queue time: {latency.queue_time_ms}ms")
print(f"Submission time: {latency.submission_time_ms}ms")
print(f"Total latency: {latency.total_latency_ms}ms")
print(f"Within target: {latency.is_within_target}")  # <100ms
```

## Throughput

### Market Data Processing

| Metric | Capability |
|--------|------------|
| Order book updates | >1,000/sec per market |
| Total throughput | >10,000 events/sec |
| Markets tracked | 100+ concurrent |

### Signal Generation

| Metric | Capability |
|--------|------------|
| Strategy processing | <1ms per market update |
| Concurrent strategies | 10+ |
| Signals per second | 100+ |

### Order Execution

| Metric | Capability |
|--------|------------|
| Concurrent executions | 3-5 (configurable) |
| Queue capacity | 100 signals (configurable) |
| Throughput | 10+ orders/sec (exchange limited) |

## Memory Usage

### Baseline

| Component | Memory |
|-----------|--------|
| Core application | ~50MB |
| Per market tracked | ~1MB |
| Event history | ~10MB |

### Under Load

Memory growth should remain stable under sustained load:

| Scenario | Expected Growth |
|----------|-----------------|
| 1000 orders | <5MB |
| 10000 events | <10MB |
| 1 hour runtime | <50MB |

### Memory Management

- Event history is bounded (configurable size)
- Order book levels are pruned automatically
- Completed executions are archived periodically
- Latency history keeps last 100 records

## Configuration Tuning

### Execution Engine

```toml
[execution]
# Maximum concurrent order executions
max_concurrent = 3  # Default: 3, increase for higher throughput

# Maximum signals in queue
max_queue_size = 100  # Default: 100

# Queue timeout before signal expires
queue_timeout_seconds = 60.0  # Default: 60s
```

### Market Data

```toml
[market_data]
# Stale data threshold
stale_threshold_seconds = 10.0  # Default: 10s

# Refresh interval for staleness checks
refresh_interval_seconds = 5.0  # Default: 5s
```

## Performance Testing

### Running Performance Tests

```bash
# Run all performance tests
pytest tests/performance/ -v

# Run with detailed output
pytest tests/performance/ -v -s

# Run specific benchmark
pytest tests/performance/test_load.py::TestPerformanceBenchmarks -v
```

### Test Scenarios

1. **High-Frequency Market Data** (`test_market_data_throughput`)
   - Validates >1000 updates/sec processing

2. **Signal-to-Order Latency** (`test_signal_to_order_latency_dry_run`)
   - Validates <100ms average latency

3. **Concurrent Strategy Execution** (`test_multiple_strategies_concurrent`)
   - Validates multiple strategies don't interfere

4. **Memory Stability** (`test_memory_stability`)
   - Validates no memory leaks under load

5. **End-to-End Benchmark** (`test_end_to_end_latency_benchmark`)
   - Full pipeline latency measurement

## Production Monitoring

### Key Metrics to Watch

1. **Latency Metrics**
   - `mercury_execution_latency_ms` - Histogram of execution times
   - `mercury_queue_time_ms` - Time signals spend in queue
   - `mercury_within_target_total` - Count of executions under 100ms

2. **Throughput Metrics**
   - `mercury_signals_received_total` - Total signals received
   - `mercury_orders_executed_total` - Total orders executed
   - `mercury_events_published_total` - Total events through bus

3. **Resource Metrics**
   - `mercury_queue_size` - Current execution queue size
   - `mercury_active_executions` - Currently executing orders
   - `mercury_markets_tracked` - Number of markets being monitored

### Alerting Thresholds

| Metric | Warning | Critical |
|--------|---------|----------|
| Avg latency | >75ms | >100ms |
| P99 latency | >150ms | >300ms |
| Queue size | >75% capacity | >90% capacity |
| Memory growth/hr | >25MB | >50MB |

## Optimization Tips

### Reducing Latency

1. **Minimize queue time**: Increase `max_concurrent` if CPU allows
2. **Optimize strategies**: Profile strategy `on_market_data` methods
3. **Reduce event payload**: Only include necessary data in events
4. **Co-locate services**: Run close to exchange endpoints

### Increasing Throughput

1. **Scale execution workers**: Increase `max_concurrent`
2. **Partition markets**: Run separate instances for market groups
3. **Batch order book updates**: Process updates in batches
4. **Use efficient data structures**: `InMemoryOrderBook` provides O(log n) updates

### Reducing Memory

1. **Limit event history**: Configure retention policies
2. **Prune inactive markets**: Unsubscribe from unused markets
3. **Bound latency history**: Default 100 records, adjustable
4. **Archive completed positions**: Move to cold storage

## Benchmark Results

Typical results on reference hardware (8-core CPU, 16GB RAM):

| Test | Result |
|------|--------|
| Market data throughput | 15,000+ updates/sec |
| Signal generation | <100μs average |
| Order book updates | 500,000+ ops/sec |
| E2E latency (dry-run) | <30ms average |
| Memory growth (1000 orders) | <3MB |

*Results may vary based on hardware, network, and exchange conditions.*
