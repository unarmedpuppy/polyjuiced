"""Unit tests for MetricsEmitter."""
import pytest
from prometheus_client import CollectorRegistry, REGISTRY

from mercury.services.metrics import MetricsEmitter


@pytest.fixture
def metrics_emitter():
    """Create a MetricsEmitter with isolated registry."""
    registry = CollectorRegistry()
    return MetricsEmitter(registry=registry)


class TestMetricsEmitter:
    """Test MetricsEmitter functionality."""

    def test_init_creates_metrics(self, metrics_emitter):
        """Verify all expected metrics are created."""
        output = metrics_emitter.get_metrics()

        assert "mercury_uptime_seconds" in output
        assert "mercury_trades_total" in output
        assert "mercury_orders_total" in output
        assert "mercury_position_value_usd" in output

    def test_record_trade(self, metrics_emitter):
        """Verify trade recording."""
        metrics_emitter.record_trade(strategy="gabagool", asset="test-market")

        output = metrics_emitter.get_metrics()
        assert 'mercury_trades_total{asset="test-market",status="executed",strategy="gabagool"}' in output

    def test_record_order(self, metrics_emitter):
        """Verify order recording."""
        metrics_emitter.record_order(side="BUY", status="filled")

        output = metrics_emitter.get_metrics()
        assert 'mercury_orders_total{side="BUY",status="filled"}' in output

    def test_record_order_latency(self, metrics_emitter):
        """Verify order latency recording."""
        metrics_emitter.record_order_latency(50.0)

        output = metrics_emitter.get_metrics()
        assert "mercury_order_latency_seconds" in output


class TestExecutionLatencyMetrics:
    """Test execution latency specific metrics."""

    def test_record_execution_queue_time(self, metrics_emitter):
        """Verify queue time is recorded."""
        metrics_emitter.record_execution_queue_time(25.0)

        output = metrics_emitter.get_metrics()
        assert "mercury_execution_queue_time_seconds" in output

    def test_record_execution_submission_time(self, metrics_emitter):
        """Verify submission time is recorded."""
        metrics_emitter.record_execution_submission_time(15.0)

        output = metrics_emitter.get_metrics()
        assert "mercury_execution_submission_time_seconds" in output

    def test_record_execution_fill_time(self, metrics_emitter):
        """Verify fill time is recorded."""
        metrics_emitter.record_execution_fill_time(10.0)

        output = metrics_emitter.get_metrics()
        assert "mercury_execution_fill_time_seconds" in output

    def test_record_execution_total_time_within_target(self, metrics_emitter):
        """Verify total time recording and within-target counter."""
        metrics_emitter.record_execution_total_time(50.0)

        output = metrics_emitter.get_metrics()
        assert "mercury_execution_total_time_seconds" in output
        assert "mercury_execution_within_target_total" in output
        # Should increment within_target counter (50ms < 100ms)
        assert 'mercury_execution_within_target_total 1.0' in output

    def test_record_execution_total_time_exceeded_target(self, metrics_emitter):
        """Verify exceeded-target counter when > 100ms."""
        metrics_emitter.record_execution_total_time(150.0)

        output = metrics_emitter.get_metrics()
        assert "mercury_execution_exceeded_target_total" in output
        # Should increment exceeded_target counter (150ms > 100ms)
        assert 'mercury_execution_exceeded_target_total 1.0' in output

    def test_record_execution_latency_breakdown(self, metrics_emitter):
        """Verify convenience method records all components."""
        metrics_emitter.record_execution_latency_breakdown(
            queue_time_ms=10.0,
            submission_time_ms=20.0,
            fill_time_ms=5.0,
            total_time_ms=35.0,
        )

        output = metrics_emitter.get_metrics()
        # All metrics should be present
        assert "mercury_execution_queue_time_seconds" in output
        assert "mercury_execution_submission_time_seconds" in output
        assert "mercury_execution_fill_time_seconds" in output
        assert "mercury_execution_total_time_seconds" in output
        # Within target (35ms < 100ms)
        assert 'mercury_execution_within_target_total 1.0' in output

    def test_record_execution_latency_breakdown_with_none_values(self, metrics_emitter):
        """Verify None values are skipped gracefully."""
        metrics_emitter.record_execution_latency_breakdown(
            queue_time_ms=None,
            submission_time_ms=20.0,
            fill_time_ms=None,
            total_time_ms=None,
        )

        # Should not raise, and submission time should be recorded
        output = metrics_emitter.get_metrics()
        assert "mercury_execution_submission_time_seconds" in output

    def test_latency_buckets_optimized_for_low_latency(self, metrics_emitter):
        """Verify histogram buckets are appropriate for sub-100ms target."""
        # Record latencies at different bucket boundaries
        metrics_emitter.record_execution_total_time(1.0)    # 1ms
        metrics_emitter.record_execution_total_time(5.0)    # 5ms
        metrics_emitter.record_execution_total_time(10.0)   # 10ms
        metrics_emitter.record_execution_total_time(25.0)   # 25ms
        metrics_emitter.record_execution_total_time(50.0)   # 50ms
        metrics_emitter.record_execution_total_time(100.0)  # 100ms (at target)

        output = metrics_emitter.get_metrics()
        # Verify histogram buckets exist
        assert 'mercury_execution_total_time_seconds_bucket{le="0.001"}' in output
        assert 'mercury_execution_total_time_seconds_bucket{le="0.01"}' in output
        assert 'mercury_execution_total_time_seconds_bucket{le="0.1"}' in output
