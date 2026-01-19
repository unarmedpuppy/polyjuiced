"""Unit tests for MetricsEmitter."""
import pytest
from decimal import Decimal
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


class TestSettlementMetrics:
    """Test settlement-related metrics."""

    def test_settlement_metrics_exist(self, metrics_emitter):
        """Verify all settlement metrics are created."""
        output = metrics_emitter.get_metrics()

        assert "mercury_settlements_total" in output
        assert "mercury_settlement_proceeds_usd_total" in output
        assert "mercury_settlement_profit_usd_total" in output
        assert "mercury_settlement_failures_total" in output
        assert "mercury_settlement_queue_size" in output
        assert "mercury_settlement_claim_attempts" in output

    def test_record_settlement_claimed(self, metrics_emitter):
        """Verify successful claim is recorded."""
        metrics_emitter.record_settlement_claimed(
            resolution="YES",
            proceeds=Decimal("10.00"),
            profit=Decimal("5.50"),
            attempts=1,
        )

        output = metrics_emitter.get_metrics()

        # Check settlement counter
        assert 'mercury_settlements_total{resolution="YES",status="claimed"}' in output

        # Check proceeds counter
        assert "mercury_settlement_proceeds_usd_total 10.0" in output

        # Check profit gauge
        assert "mercury_settlement_profit_usd_total 5.5" in output

        # Check attempts histogram
        assert "mercury_settlement_claim_attempts_count 1.0" in output

    def test_record_settlement_claimed_negative_profit(self, metrics_emitter):
        """Verify losing position profit is recorded correctly."""
        metrics_emitter.record_settlement_claimed(
            resolution="NO",
            proceeds=Decimal("0.00"),
            profit=Decimal("-4.50"),
            attempts=1,
        )

        output = metrics_emitter.get_metrics()

        # Check settlement counter for losing position
        assert 'mercury_settlements_total{resolution="NO",status="claimed"}' in output

        # Check profit gauge (negative)
        assert "mercury_settlement_profit_usd_total -4.5" in output

    def test_record_settlement_claimed_cumulative_profit(self, metrics_emitter):
        """Verify profit accumulates across multiple claims."""
        # First claim: +5.50
        metrics_emitter.record_settlement_claimed(
            resolution="YES",
            proceeds=Decimal("10.00"),
            profit=Decimal("5.50"),
            attempts=1,
        )

        # Second claim: -3.00
        metrics_emitter.record_settlement_claimed(
            resolution="NO",
            proceeds=Decimal("0.00"),
            profit=Decimal("-3.00"),
            attempts=1,
        )

        output = metrics_emitter.get_metrics()

        # Net profit should be 5.50 - 3.00 = 2.50
        assert "mercury_settlement_profit_usd_total 2.5" in output

    def test_record_settlement_failed_transient(self, metrics_emitter):
        """Verify transient failure is recorded."""
        metrics_emitter.record_settlement_failed(
            reason_type="network",
            attempt_count=1,
            is_permanent=False,
        )

        output = metrics_emitter.get_metrics()

        # Check failure counter
        assert 'mercury_settlement_failures_total{reason_type="network"}' in output

        # Should NOT increment settlements_total for transient failures
        assert 'mercury_settlements_total{resolution="unknown",status="failed"} 1.0' not in output

    def test_record_settlement_failed_permanent(self, metrics_emitter):
        """Verify permanent failure is recorded."""
        metrics_emitter.record_settlement_failed(
            reason_type="contract",
            attempt_count=5,
            is_permanent=True,
        )

        output = metrics_emitter.get_metrics()

        # Check failure counter with _permanent suffix
        assert 'mercury_settlement_failures_total{reason_type="contract_permanent"}' in output

        # Should increment settlements_total for permanent failures
        assert 'mercury_settlements_total{resolution="unknown",status="failed"} 1.0' in output

        # Should record attempts histogram for permanent failures
        assert "mercury_settlement_claim_attempts_count 1.0" in output

    def test_update_settlement_queue_size(self, metrics_emitter):
        """Verify queue size gauges are updated."""
        metrics_emitter.update_settlement_queue_size(
            pending=10,
            claimed=5,
            failed=2,
        )

        output = metrics_emitter.get_metrics()

        assert 'mercury_settlement_queue_size{status="pending"} 10.0' in output
        assert 'mercury_settlement_queue_size{status="claimed"} 5.0' in output
        assert 'mercury_settlement_queue_size{status="failed"} 2.0' in output

    def test_record_settlement_claimed_multiple_attempts(self, metrics_emitter):
        """Verify multiple attempts before success is recorded."""
        metrics_emitter.record_settlement_claimed(
            resolution="YES",
            proceeds=Decimal("10.00"),
            profit=Decimal("5.50"),
            attempts=3,
        )

        output = metrics_emitter.get_metrics()

        # Check attempts histogram has a count of 1 with value 3
        assert "mercury_settlement_claim_attempts_count 1.0" in output
        assert "mercury_settlement_claim_attempts_sum 3.0" in output
