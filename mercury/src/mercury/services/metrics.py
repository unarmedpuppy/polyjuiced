"""
Prometheus metrics emission for Mercury.

Provides observability through standardized metrics collection.
All metrics use the 'mercury_' prefix.
"""
from decimal import Decimal
from typing import Optional

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
)


class MetricsEmitter:
    """Prometheus metrics emission (emit only, no reading).

    Usage:
        emitter = MetricsEmitter()
        emitter.record_trade(trade)
        emitter.record_order_latency(150.0)
        metrics_output = emitter.get_metrics()
    """

    def __init__(self, registry: Optional[CollectorRegistry] = None) -> None:
        """Initialize MetricsEmitter.

        Args:
            registry: Optional custom registry (uses default if not provided)
        """
        self._registry = registry or CollectorRegistry()

        # Application info
        self._info = Info(
            "mercury",
            "Mercury trading bot information",
            registry=self._registry,
        )
        self._info.info({
            "version": "0.1.0",
            "component": "mercury",
        })

        # Uptime gauge
        self._uptime = Gauge(
            "mercury_uptime_seconds",
            "Process uptime in seconds",
            registry=self._registry,
        )

        # Trading metrics
        self._trades_total = Counter(
            "mercury_trades_total",
            "Total trades executed",
            ["strategy", "asset", "status"],
            registry=self._registry,
        )

        self._orders_total = Counter(
            "mercury_orders_total",
            "Total orders submitted",
            ["side", "status"],
            registry=self._registry,
        )

        self._signals_total = Counter(
            "mercury_signals_total",
            "Trading signals generated",
            ["strategy", "action"],
            registry=self._registry,
        )

        # Position gauges
        self._position_value = Gauge(
            "mercury_position_value_usd",
            "Current position value in USD",
            ["market_id"],
            registry=self._registry,
        )

        self._daily_pnl = Gauge(
            "mercury_daily_pnl_usd",
            "Daily realized P&L in USD",
            registry=self._registry,
        )

        self._daily_exposure = Gauge(
            "mercury_daily_exposure_usd",
            "Daily trading exposure in USD",
            registry=self._registry,
        )

        self._active_positions = Gauge(
            "mercury_active_positions",
            "Number of open positions",
            registry=self._registry,
        )

        self._circuit_breaker_level = Gauge(
            "mercury_circuit_breaker_level",
            "Circuit breaker level (0=NORMAL, 1=WARNING, 2=CRITICAL, 3=TRIGGERED)",
            registry=self._registry,
        )

        # Latency histograms
        self._order_latency = Histogram(
            "mercury_order_latency_seconds",
            "Order execution latency in seconds",
            buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
            registry=self._registry,
        )

        self._spread_cents = Histogram(
            "mercury_spread_cents",
            "Spread at trade time in cents",
            ["asset"],
            buckets=[0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.5, 10.0],
            registry=self._registry,
        )

        self._fill_ratio = Histogram(
            "mercury_fill_ratio",
            "Order fill ratio (filled / requested)",
            buckets=[0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99, 1.0],
            registry=self._registry,
        )

        # Connection metrics
        self._websocket_connected = Gauge(
            "mercury_websocket_connected",
            "WebSocket connection status (1=connected, 0=disconnected)",
            registry=self._registry,
        )

        self._websocket_reconnects = Counter(
            "mercury_websocket_reconnects_total",
            "WebSocket reconnection count",
            registry=self._registry,
        )

        self._api_requests = Counter(
            "mercury_api_requests_total",
            "API requests made",
            ["endpoint", "status"],
            registry=self._registry,
        )

        # Event bus metrics
        self._event_bus_messages = Counter(
            "mercury_event_bus_messages_total",
            "Event bus messages processed",
            ["channel"],
            registry=self._registry,
        )

        # Settlement metrics
        self._settlements_total = Counter(
            "mercury_settlements_total",
            "Total settlement claims processed",
            ["status", "resolution"],
            registry=self._registry,
        )

        self._settlement_proceeds = Counter(
            "mercury_settlement_proceeds_usd_total",
            "Total settlement proceeds in USD",
            registry=self._registry,
        )

        self._settlement_profit = Gauge(
            "mercury_settlement_profit_usd_total",
            "Total settlement profit/loss in USD (can be negative)",
            registry=self._registry,
        )

        self._settlement_failures = Counter(
            "mercury_settlement_failures_total",
            "Total settlement claim failures",
            ["reason_type"],
            registry=self._registry,
        )

        self._settlement_queue_size = Gauge(
            "mercury_settlement_queue_size",
            "Current size of settlement queue",
            ["status"],
            registry=self._registry,
        )

        # Simple queue depth gauge (total unclaimed items)
        self._settlement_queue_depth = Gauge(
            "mercury_settlement_queue_depth",
            "Total number of positions pending settlement",
            registry=self._registry,
        )

        self._settlement_claim_attempts = Histogram(
            "mercury_settlement_claim_attempts",
            "Number of attempts before claim success/permanent failure",
            buckets=[1, 2, 3, 4, 5],
            registry=self._registry,
        )

        # Settlement latency histogram - time from market resolution to claim
        # Buckets span from minutes to days (in seconds)
        # 1min, 5min, 10min, 30min, 1hr, 2hr, 6hr, 12hr, 24hr, 48hr, 7days
        settlement_latency_buckets = [
            60, 300, 600, 1800, 3600, 7200, 21600, 43200, 86400, 172800, 604800
        ]
        self._settlement_latency = Histogram(
            "mercury_settlement_latency_seconds",
            "Time from market resolution to successful claim in seconds",
            buckets=settlement_latency_buckets,
            registry=self._registry,
        )

        # Execution latency breakdown histograms (target: sub-100ms total)
        # Buckets optimized for low-latency trading: 1ms to 500ms
        latency_buckets = [0.001, 0.005, 0.010, 0.025, 0.050, 0.075, 0.100, 0.150, 0.250, 0.500]

        self._execution_queue_time = Histogram(
            "mercury_execution_queue_time_seconds",
            "Time spent in execution queue (signal received to execution start)",
            buckets=latency_buckets,
            registry=self._registry,
        )

        self._execution_submission_time = Histogram(
            "mercury_execution_submission_time_seconds",
            "Time to submit order to exchange (execution start to exchange ack)",
            buckets=latency_buckets,
            registry=self._registry,
        )

        self._execution_fill_time = Histogram(
            "mercury_execution_fill_time_seconds",
            "Time from submission to fill completion",
            buckets=latency_buckets,
            registry=self._registry,
        )

        self._execution_total_time = Histogram(
            "mercury_execution_total_time_seconds",
            "Total execution latency (signal received to order confirmed)",
            buckets=latency_buckets,
            registry=self._registry,
        )

        # Counters for latency target tracking
        self._execution_within_target = Counter(
            "mercury_execution_within_target_total",
            "Executions completing within 100ms target",
            registry=self._registry,
        )

        self._execution_exceeded_target = Counter(
            "mercury_execution_exceeded_target_total",
            "Executions exceeding 100ms target",
            registry=self._registry,
        )

    def record_trade(
        self,
        strategy: str,
        asset: str,
        status: str = "executed",
    ) -> None:
        """Record a trade.

        Args:
            strategy: Strategy name
            asset: Asset/market traded
            status: Trade status (executed, failed, etc.)
        """
        self._trades_total.labels(
            strategy=strategy,
            asset=asset,
            status=status,
        ).inc()

    def record_order(self, side: str, status: str = "submitted") -> None:
        """Record an order.

        Args:
            side: Order side (BUY, SELL)
            status: Order status
        """
        self._orders_total.labels(side=side, status=status).inc()

    def record_signal(self, strategy: str, action: str) -> None:
        """Record a trading signal.

        Args:
            strategy: Strategy name
            action: Signal action type
        """
        self._signals_total.labels(strategy=strategy, action=action).inc()

    def record_order_latency(self, latency_ms: float) -> None:
        """Record order execution latency.

        Args:
            latency_ms: Latency in milliseconds
        """
        self._order_latency.observe(latency_ms / 1000.0)

    def record_spread(self, asset: str, spread_cents: float) -> None:
        """Record trade spread.

        Args:
            asset: Asset name
            spread_cents: Spread in cents
        """
        self._spread_cents.labels(asset=asset).observe(spread_cents)

    def record_fill_ratio(self, ratio: float) -> None:
        """Record order fill ratio.

        Args:
            ratio: Fill ratio (0.0 to 1.0)
        """
        self._fill_ratio.observe(ratio)

    def update_position_value(self, market_id: str, value: Decimal) -> None:
        """Update position value gauge.

        Args:
            market_id: Market identifier
            value: Position value in USD
        """
        self._position_value.labels(market_id=market_id).set(float(value))

    def update_daily_pnl(self, pnl: Decimal) -> None:
        """Update daily P&L gauge.

        Args:
            pnl: Daily P&L in USD
        """
        self._daily_pnl.set(float(pnl))

    def update_daily_exposure(self, exposure: Decimal) -> None:
        """Update daily exposure gauge.

        Args:
            exposure: Daily exposure in USD
        """
        self._daily_exposure.set(float(exposure))

    def update_active_positions(self, count: int) -> None:
        """Update active positions count.

        Args:
            count: Number of open positions
        """
        self._active_positions.set(count)

    def update_circuit_breaker(self, level: int) -> None:
        """Update circuit breaker level.

        Args:
            level: Circuit breaker level (0=NORMAL, 1=WARNING, 2=CRITICAL, 3=TRIGGERED)
        """
        self._circuit_breaker_level.set(level)

    def update_uptime(self, seconds: float) -> None:
        """Update uptime gauge.

        Args:
            seconds: Uptime in seconds
        """
        self._uptime.set(seconds)

    def update_websocket_status(self, connected: bool) -> None:
        """Update WebSocket connection status.

        Args:
            connected: Whether connected
        """
        self._websocket_connected.set(1 if connected else 0)

    def record_websocket_reconnect(self) -> None:
        """Record a WebSocket reconnection."""
        self._websocket_reconnects.inc()

    def record_api_request(self, endpoint: str, status: str) -> None:
        """Record an API request.

        Args:
            endpoint: API endpoint called
            status: Response status (success, error, etc.)
        """
        self._api_requests.labels(endpoint=endpoint, status=status).inc()

    def record_event_bus_message(self, channel: str) -> None:
        """Record an event bus message.

        Args:
            channel: Event channel
        """
        self._event_bus_messages.labels(channel=channel).inc()

    def record_execution_queue_time(self, queue_time_ms: float) -> None:
        """Record time spent in execution queue.

        Args:
            queue_time_ms: Queue time in milliseconds
        """
        self._execution_queue_time.observe(queue_time_ms / 1000.0)

    def record_execution_submission_time(self, submission_time_ms: float) -> None:
        """Record time to submit order to exchange.

        Args:
            submission_time_ms: Submission time in milliseconds
        """
        self._execution_submission_time.observe(submission_time_ms / 1000.0)

    def record_execution_fill_time(self, fill_time_ms: float) -> None:
        """Record time from submission to fill.

        Args:
            fill_time_ms: Fill time in milliseconds
        """
        self._execution_fill_time.observe(fill_time_ms / 1000.0)

    def record_execution_total_time(self, total_time_ms: float) -> None:
        """Record total execution latency.

        Args:
            total_time_ms: Total latency in milliseconds
        """
        self._execution_total_time.observe(total_time_ms / 1000.0)

        # Track target compliance
        if total_time_ms < 100.0:
            self._execution_within_target.inc()
        else:
            self._execution_exceeded_target.inc()

    def record_execution_latency_breakdown(
        self,
        queue_time_ms: Optional[float],
        submission_time_ms: Optional[float],
        fill_time_ms: Optional[float],
        total_time_ms: Optional[float],
    ) -> None:
        """Record complete execution latency breakdown.

        Convenience method to record all latency components at once.

        Args:
            queue_time_ms: Time in queue (ms), or None if not available
            submission_time_ms: Submission time (ms), or None if not available
            fill_time_ms: Fill time (ms), or None if not available
            total_time_ms: Total latency (ms), or None if not available
        """
        if queue_time_ms is not None:
            self.record_execution_queue_time(queue_time_ms)
        if submission_time_ms is not None:
            self.record_execution_submission_time(submission_time_ms)
        if fill_time_ms is not None:
            self.record_execution_fill_time(fill_time_ms)
        if total_time_ms is not None:
            self.record_execution_total_time(total_time_ms)

    def record_settlement_claimed(
        self,
        resolution: str,
        proceeds: Decimal,
        profit: Decimal,
        attempts: int = 1,
    ) -> None:
        """Record a successful settlement claim.

        Args:
            resolution: Market resolution ("YES" or "NO")
            proceeds: Settlement proceeds in USD
            profit: Settlement profit/loss in USD (can be negative)
            attempts: Number of attempts before success
        """
        self._settlements_total.labels(status="claimed", resolution=resolution).inc()
        self._settlement_proceeds.inc(float(proceeds))
        # For profit, we need to track cumulative - use a counter for positive, gauge for net
        # Since profit can be negative, we'll use the gauge to track running total
        current = self._settlement_profit._value.get()
        self._settlement_profit.set(current + float(profit))
        self._settlement_claim_attempts.observe(attempts)

    def record_settlement_failed(
        self,
        reason_type: str,
        attempt_count: int,
        is_permanent: bool = False,
    ) -> None:
        """Record a settlement claim failure.

        Args:
            reason_type: Type of failure (e.g., "network", "contract", "not_resolved")
            attempt_count: Current attempt number
            is_permanent: Whether this is a permanent failure (max attempts reached)
        """
        failure_type = f"{reason_type}_permanent" if is_permanent else reason_type
        self._settlement_failures.labels(reason_type=failure_type).inc()

        if is_permanent:
            self._settlements_total.labels(status="failed", resolution="unknown").inc()
            self._settlement_claim_attempts.observe(attempt_count)

    def update_settlement_queue_size(
        self,
        pending: int,
        claimed: int,
        failed: int,
    ) -> None:
        """Update settlement queue size gauges.

        Args:
            pending: Number of pending settlements
            claimed: Number of claimed settlements
            failed: Number of permanently failed settlements
        """
        self._settlement_queue_size.labels(status="pending").set(pending)
        self._settlement_queue_size.labels(status="claimed").set(claimed)
        self._settlement_queue_size.labels(status="failed").set(failed)

    def update_settlement_queue_depth(self, depth: int) -> None:
        """Update settlement queue depth gauge.

        This is a simple gauge tracking total unclaimed positions
        pending settlement.

        Args:
            depth: Total number of positions waiting to be claimed.
        """
        self._settlement_queue_depth.set(depth)

    def record_settlement_latency(self, latency_seconds: float) -> None:
        """Record settlement latency.

        Measures the time from market resolution (market_end_time) to when
        the position was successfully claimed. This helps track how quickly
        settlements are being processed.

        Args:
            latency_seconds: Time in seconds from market resolution to claim.
        """
        self._settlement_latency.observe(latency_seconds)

    def get_metrics(self) -> str:
        """Get Prometheus metrics output.

        Returns:
            Metrics in Prometheus text format
        """
        return generate_latest(self._registry).decode("utf-8")

    @property
    def registry(self) -> CollectorRegistry:
        """Get the metrics registry."""
        return self._registry
