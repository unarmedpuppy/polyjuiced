"""Prometheus metrics for Polymarket bot."""

from prometheus_client import Counter, Gauge, Histogram, Info

# Bot info
BOT_INFO = Info("polymarket_bot", "Polymarket bot information")

# Trading metrics
TRADES_TOTAL = Counter(
    "polymarket_trades_total",
    "Total number of trades executed",
    ["market", "side", "dry_run"],
)

TRADE_AMOUNT_USD = Histogram(
    "polymarket_trade_amount_usd",
    "Trade amounts in USD",
    ["market", "side"],
    buckets=[1, 2, 5, 10, 20, 50, 100, 200, 500],
)

TRADE_PROFIT_USD = Histogram(
    "polymarket_trade_profit_usd",
    "Expected profit per trade in USD",
    ["market"],
    buckets=[0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5],
)

TRADE_ERRORS_TOTAL = Counter(
    "polymarket_trade_errors_total",
    "Total number of trade errors",
    ["market", "error_type"],
)

# P&L metrics
DAILY_PNL_USD = Gauge(
    "polymarket_daily_pnl_usd",
    "Daily profit/loss in USD",
)

DAILY_TRADES = Gauge(
    "polymarket_daily_trades",
    "Number of trades today",
)

DAILY_EXPOSURE_USD = Gauge(
    "polymarket_daily_exposure_usd",
    "Total daily exposure in USD",
)

# Market metrics
SPREAD_CENTS = Gauge(
    "polymarket_spread_cents",
    "Current spread in cents",
    ["market", "asset"],
)

YES_PRICE = Gauge(
    "polymarket_yes_price",
    "Current YES price",
    ["market", "asset"],
)

NO_PRICE = Gauge(
    "polymarket_no_price",
    "Current NO price",
    ["market", "asset"],
)

ACTIVE_MARKETS = Gauge(
    "polymarket_active_markets",
    "Number of active markets being tracked",
)

# Opportunity metrics
OPPORTUNITIES_DETECTED = Counter(
    "polymarket_opportunities_detected_total",
    "Total arbitrage opportunities detected",
    ["market"],
)

OPPORTUNITIES_EXECUTED = Counter(
    "polymarket_opportunities_executed_total",
    "Total arbitrage opportunities executed",
    ["market"],
)

OPPORTUNITIES_SKIPPED = Counter(
    "polymarket_opportunities_skipped_total",
    "Total opportunities skipped",
    ["market", "reason"],
)

# Circuit breaker metrics
CIRCUIT_BREAKER_LEVEL = Gauge(
    "polymarket_circuit_breaker_level",
    "Current circuit breaker level (0=NORMAL, 1=WARNING, 2=CAUTION, 3=HALT)",
)

CIRCUIT_BREAKER_TRIPS = Counter(
    "polymarket_circuit_breaker_trips_total",
    "Total circuit breaker trips",
    ["level"],
)

# Connection metrics
WEBSOCKET_CONNECTED = Gauge(
    "polymarket_websocket_connected",
    "WebSocket connection status (1=connected, 0=disconnected)",
)

WEBSOCKET_RECONNECTS = Counter(
    "polymarket_websocket_reconnects_total",
    "Total WebSocket reconnection attempts",
)

API_REQUESTS_TOTAL = Counter(
    "polymarket_api_requests_total",
    "Total API requests",
    ["endpoint", "method", "status"],
)

API_REQUEST_DURATION = Histogram(
    "polymarket_api_request_duration_seconds",
    "API request duration in seconds",
    ["endpoint", "method"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
)

# Position metrics
POSITION_SIZE_MULTIPLIER = Gauge(
    "polymarket_position_size_multiplier",
    "Current position size multiplier from circuit breaker",
)

UNHEDGED_EXPOSURE_USD = Gauge(
    "polymarket_unhedged_exposure_usd",
    "Current unhedged exposure in USD",
)


def init_metrics(version: str = "0.1.0", dry_run: bool = True) -> None:
    """Initialize bot info metrics.

    Args:
        version: Bot version
        dry_run: Whether running in dry-run mode
    """
    BOT_INFO.info({
        "version": version,
        "dry_run": str(dry_run).lower(),
        "strategy": "gabagool",
    })
