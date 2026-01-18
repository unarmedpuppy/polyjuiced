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
