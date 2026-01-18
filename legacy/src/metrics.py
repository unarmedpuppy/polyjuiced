"""Prometheus metrics for Polymarket bot."""

from dataclasses import dataclass

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

# Phase 4a: Hedge ratio metrics (Dec 13, 2025)
# Tracks actual hedge achieved vs expected for arbitrage execution quality
HEDGE_RATIO = Gauge(
    "polymarket_hedge_ratio",
    "Actual hedge ratio achieved (min_shares/max_shares, 1.0=perfect hedge)",
    ["market", "asset"],
)

HEDGE_RATIO_HISTOGRAM = Histogram(
    "polymarket_hedge_ratio_distribution",
    "Distribution of hedge ratios across trades",
    ["market"],
    buckets=[0.0, 0.2, 0.4, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 1.0],
)

HEDGE_VIOLATIONS_TOTAL = Counter(
    "polymarket_hedge_violations_total",
    "Trades with hedge ratio below minimum threshold (80%)",
    ["market", "violation_type"],  # violation_type: below_min, below_critical
)

# Dual-leg execution tracking
DUAL_LEG_OUTCOMES_TOTAL = Counter(
    "polymarket_dual_leg_outcomes_total",
    "Outcomes of dual-leg order execution attempts",
    ["market", "outcome"],  # outcome: both_filled, partial_fill, both_failed, cancelled
)

DUAL_LEG_FILL_TIME_SECONDS = Histogram(
    "polymarket_dual_leg_fill_time_seconds",
    "Time taken for both legs to fill in parallel execution",
    ["market"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0],
)

# Phase 4b: Fill rate tracking metrics (Dec 13, 2025)
# Tracks order fill rates to understand execution quality and liquidity conditions
ORDER_ATTEMPTS_TOTAL = Counter(
    "polymarket_order_attempts_total",
    "Total order placement attempts",
    ["market", "side"],  # side: YES, NO
)

ORDER_FILLS_TOTAL = Counter(
    "polymarket_order_fills_total",
    "Total orders that successfully filled (MATCHED/FILLED)",
    ["market", "side"],
)

ORDER_LIVE_TOTAL = Counter(
    "polymarket_order_live_total",
    "Total orders that went LIVE (on book, not filled)",
    ["market", "side"],
)

ORDER_REJECTED_TOTAL = Counter(
    "polymarket_order_rejected_total",
    "Total orders that were rejected",
    ["market", "side", "reason"],  # reason: insufficient_liquidity, price_moved, timeout, etc.
)

FILL_RATE_GAUGE = Gauge(
    "polymarket_fill_rate",
    "Current fill rate (fills / attempts) as percentage",
    ["market", "side"],
)

PARTIAL_FILL_RATIO = Histogram(
    "polymarket_partial_fill_ratio",
    "Ratio of filled size to requested size (1.0 = fully filled)",
    ["market", "side"],
    buckets=[0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
)

SLIPPAGE_CENTS = Histogram(
    "polymarket_slippage_cents",
    "Actual slippage in cents (execution price - expected price)",
    ["market", "side"],
    buckets=[0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 10.0],
)

LIQUIDITY_AT_ORDER = Histogram(
    "polymarket_liquidity_at_order",
    "Available liquidity (in shares) when order was placed",
    ["market", "side"],
    buckets=[1, 5, 10, 20, 50, 100, 200, 500, 1000],
)

# Phase 4c: P&L tracking metrics (Dec 13, 2025)
# Tracks expected vs realized profit to understand strategy effectiveness
EXPECTED_PROFIT_USD = Histogram(
    "polymarket_expected_profit_usd",
    "Expected profit at trade entry in USD",
    ["market"],
    buckets=[0.01, 0.02, 0.05, 0.10, 0.20, 0.50, 1.00, 2.00, 5.00],
)

REALIZED_PROFIT_USD = Histogram(
    "polymarket_realized_profit_usd",
    "Realized profit at market resolution in USD",
    ["market", "outcome"],  # outcome: win, loss, break_even
    buckets=[-5.0, -2.0, -1.0, -0.50, -0.20, -0.10, 0.0, 0.10, 0.20, 0.50, 1.00, 2.00, 5.00],
)

PNL_VARIANCE_USD = Histogram(
    "polymarket_pnl_variance_usd",
    "Difference between realized and expected profit (realized - expected)",
    ["market"],
    buckets=[-5.0, -2.0, -1.0, -0.50, -0.20, -0.10, 0.0, 0.10, 0.20, 0.50, 1.00, 2.00, 5.00],
)

CUMULATIVE_EXPECTED_PNL_USD = Gauge(
    "polymarket_cumulative_expected_pnl_usd",
    "Cumulative expected P&L across all trades (session)",
    ["market"],
)

CUMULATIVE_REALIZED_PNL_USD = Gauge(
    "polymarket_cumulative_realized_pnl_usd",
    "Cumulative realized P&L across all trades (session)",
    ["market"],
)

TRADE_OUTCOME_TOTAL = Counter(
    "polymarket_trade_outcome_total",
    "Trade outcomes by result",
    ["market", "outcome"],  # outcome: win, loss, break_even, pending
)

WIN_RATE_GAUGE = Gauge(
    "polymarket_win_rate",
    "Current win rate (wins / resolved trades) as percentage",
    ["market"],
)

EXPECTED_VALUE_PER_TRADE = Gauge(
    "polymarket_expected_value_per_trade_usd",
    "Average expected value per trade in USD",
    ["market"],
)

REALIZED_VALUE_PER_TRADE = Gauge(
    "polymarket_realized_value_per_trade_usd",
    "Average realized value per trade in USD",
    ["market"],
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


def record_hedge_ratio(
    market: str,
    asset: str,
    yes_shares: float,
    no_shares: float,
    min_hedge_ratio: float = 0.80,
    critical_hedge_ratio: float = 0.60,
) -> float:
    """Record hedge ratio metrics for a trade.

    Args:
        market: Market identifier (e.g., "BTC", "ETH")
        asset: Asset identifier
        yes_shares: Number of YES shares filled
        no_shares: Number of NO shares filled
        min_hedge_ratio: Minimum acceptable hedge ratio (default 0.80)
        critical_hedge_ratio: Critical hedge ratio threshold (default 0.60)

    Returns:
        The calculated hedge ratio (0.0 to 1.0)
    """
    if yes_shares <= 0 and no_shares <= 0:
        # No shares filled - record 0 hedge ratio
        HEDGE_RATIO.labels(market=market, asset=asset).set(0.0)
        HEDGE_RATIO_HISTOGRAM.labels(market=market).observe(0.0)
        HEDGE_VIOLATIONS_TOTAL.labels(market=market, violation_type="below_critical").inc()
        return 0.0

    max_shares = max(yes_shares, no_shares)
    min_shares = min(yes_shares, no_shares)

    # Calculate hedge ratio: 1.0 = perfect hedge, 0.0 = completely unhedged
    hedge_ratio = min_shares / max_shares if max_shares > 0 else 0.0

    # Record gauge (current value)
    HEDGE_RATIO.labels(market=market, asset=asset).set(hedge_ratio)

    # Record histogram (distribution)
    HEDGE_RATIO_HISTOGRAM.labels(market=market).observe(hedge_ratio)

    # Check for violations
    if hedge_ratio < critical_hedge_ratio:
        HEDGE_VIOLATIONS_TOTAL.labels(market=market, violation_type="below_critical").inc()
    elif hedge_ratio < min_hedge_ratio:
        HEDGE_VIOLATIONS_TOTAL.labels(market=market, violation_type="below_min").inc()

    return hedge_ratio


def record_dual_leg_outcome(
    market: str,
    outcome: str,
    fill_time_seconds: float | None = None,
) -> None:
    """Record the outcome of a dual-leg order execution.

    Args:
        market: Market identifier
        outcome: One of "both_filled", "partial_fill", "both_failed", "cancelled"
        fill_time_seconds: Time taken for fills (only recorded for successful fills)
    """
    DUAL_LEG_OUTCOMES_TOTAL.labels(market=market, outcome=outcome).inc()

    if fill_time_seconds is not None and outcome == "both_filled":
        DUAL_LEG_FILL_TIME_SECONDS.labels(market=market).observe(fill_time_seconds)


# Track cumulative fill counts for rate calculation
_fill_counts: dict[tuple[str, str], dict[str, int]] = {}


def record_order_attempt(
    market: str,
    side: str,
    status: str,
    requested_size: float,
    filled_size: float,
    expected_price: float | None = None,
    execution_price: float | None = None,
    available_liquidity: float | None = None,
    rejection_reason: str | None = None,
) -> float:
    """Record order attempt and fill metrics.

    Args:
        market: Market identifier (e.g., "BTC", "ETH")
        side: Order side ("YES" or "NO")
        status: Order status ("MATCHED", "FILLED", "LIVE", "REJECTED", etc.)
        requested_size: Requested order size in shares
        filled_size: Actually filled size in shares
        expected_price: Expected execution price (0.0-1.0)
        execution_price: Actual execution price (0.0-1.0)
        available_liquidity: Liquidity available when order was placed
        rejection_reason: Reason for rejection if status is REJECTED

    Returns:
        The fill ratio (0.0 to 1.0)
    """
    key = (market, side)

    # Initialize tracking for this market/side if needed
    if key not in _fill_counts:
        _fill_counts[key] = {"attempts": 0, "fills": 0}

    # Record attempt
    ORDER_ATTEMPTS_TOTAL.labels(market=market, side=side).inc()
    _fill_counts[key]["attempts"] += 1

    # Normalize status
    status_upper = status.upper()

    # Record outcome based on status
    if status_upper in ("MATCHED", "FILLED"):
        ORDER_FILLS_TOTAL.labels(market=market, side=side).inc()
        _fill_counts[key]["fills"] += 1
    elif status_upper == "LIVE":
        ORDER_LIVE_TOTAL.labels(market=market, side=side).inc()
    elif status_upper in ("REJECTED", "CANCELLED", "FAILED"):
        reason = rejection_reason or "unknown"
        ORDER_REJECTED_TOTAL.labels(market=market, side=side, reason=reason).inc()

    # Calculate and record fill ratio
    fill_ratio = filled_size / requested_size if requested_size > 0 else 0.0
    PARTIAL_FILL_RATIO.labels(market=market, side=side).observe(fill_ratio)

    # Update fill rate gauge
    attempts = _fill_counts[key]["attempts"]
    fills = _fill_counts[key]["fills"]
    fill_rate = (fills / attempts * 100) if attempts > 0 else 0.0
    FILL_RATE_GAUGE.labels(market=market, side=side).set(fill_rate)

    # Record slippage if we have price data
    if expected_price is not None and execution_price is not None:
        # Slippage in cents (positive = paid more than expected)
        slippage = (execution_price - expected_price) * 100
        SLIPPAGE_CENTS.labels(market=market, side=side).observe(abs(slippage))

    # Record liquidity at order time
    if available_liquidity is not None:
        LIQUIDITY_AT_ORDER.labels(market=market, side=side).observe(available_liquidity)

    return fill_ratio


def get_fill_rate(market: str, side: str) -> float:
    """Get the current fill rate for a market/side.

    Args:
        market: Market identifier
        side: Order side ("YES" or "NO")

    Returns:
        Fill rate as percentage (0.0 to 100.0)
    """
    key = (market, side)
    if key not in _fill_counts:
        return 0.0

    attempts = _fill_counts[key]["attempts"]
    fills = _fill_counts[key]["fills"]
    return (fills / attempts * 100) if attempts > 0 else 0.0


def reset_fill_counts() -> None:
    """Reset fill count tracking (e.g., for daily reset)."""
    global _fill_counts
    _fill_counts = {}


# Track P&L data for rate calculations
_pnl_tracking: dict[str, dict[str, float | int]] = {}


def record_trade_entry(
    market: str,
    expected_profit_usd: float,
    yes_shares: float,
    no_shares: float,
    yes_cost_usd: float,
    no_cost_usd: float,
) -> str:
    """Record a trade entry with expected profit.

    Args:
        market: Market identifier (e.g., "BTC", "ETH")
        expected_profit_usd: Expected profit from the arbitrage
        yes_shares: Number of YES shares purchased
        no_shares: Number of NO shares purchased
        yes_cost_usd: Cost of YES shares in USD
        no_cost_usd: Cost of NO shares in USD

    Returns:
        Trade ID for later resolution tracking
    """
    import uuid

    trade_id = str(uuid.uuid4())[:8]

    # Initialize market tracking if needed
    if market not in _pnl_tracking:
        _pnl_tracking[market] = {
            "cumulative_expected": 0.0,
            "cumulative_realized": 0.0,
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "pending": 0,
        }

    # Record expected profit
    EXPECTED_PROFIT_USD.labels(market=market).observe(expected_profit_usd)

    # Update cumulative expected
    _pnl_tracking[market]["cumulative_expected"] += expected_profit_usd
    _pnl_tracking[market]["total_trades"] += 1
    _pnl_tracking[market]["pending"] += 1

    CUMULATIVE_EXPECTED_PNL_USD.labels(market=market).set(
        _pnl_tracking[market]["cumulative_expected"]
    )

    # Update expected value per trade
    total_trades = _pnl_tracking[market]["total_trades"]
    avg_expected = _pnl_tracking[market]["cumulative_expected"] / total_trades
    EXPECTED_VALUE_PER_TRADE.labels(market=market).set(avg_expected)

    # Mark as pending
    TRADE_OUTCOME_TOTAL.labels(market=market, outcome="pending").inc()

    return trade_id


def record_trade_resolution(
    market: str,
    realized_profit_usd: float,
    expected_profit_usd: float,
) -> str:
    """Record a trade resolution with realized profit.

    Args:
        market: Market identifier
        realized_profit_usd: Actual profit/loss at resolution
        expected_profit_usd: Original expected profit (for variance calculation)

    Returns:
        Outcome classification ("win", "loss", or "break_even")
    """
    # Initialize if needed (shouldn't happen but be safe)
    if market not in _pnl_tracking:
        _pnl_tracking[market] = {
            "cumulative_expected": 0.0,
            "cumulative_realized": 0.0,
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "pending": 0,
        }

    # Determine outcome
    if realized_profit_usd > 0.01:  # Small threshold for rounding
        outcome = "win"
        _pnl_tracking[market]["wins"] += 1
    elif realized_profit_usd < -0.01:
        outcome = "loss"
        _pnl_tracking[market]["losses"] += 1
    else:
        outcome = "break_even"

    # Decrement pending
    if _pnl_tracking[market]["pending"] > 0:
        _pnl_tracking[market]["pending"] -= 1

    # Record realized profit
    REALIZED_PROFIT_USD.labels(market=market, outcome=outcome).observe(realized_profit_usd)

    # Update cumulative realized
    _pnl_tracking[market]["cumulative_realized"] += realized_profit_usd
    CUMULATIVE_REALIZED_PNL_USD.labels(market=market).set(
        _pnl_tracking[market]["cumulative_realized"]
    )

    # Record variance (realized - expected)
    variance = realized_profit_usd - expected_profit_usd
    PNL_VARIANCE_USD.labels(market=market).observe(variance)

    # Update outcome counter
    TRADE_OUTCOME_TOTAL.labels(market=market, outcome=outcome).inc()

    # Update win rate
    resolved = _pnl_tracking[market]["wins"] + _pnl_tracking[market]["losses"]
    if resolved > 0:
        win_rate = (_pnl_tracking[market]["wins"] / resolved) * 100
        WIN_RATE_GAUGE.labels(market=market).set(win_rate)

    # Update realized value per trade
    total_trades = _pnl_tracking[market]["total_trades"]
    if total_trades > 0:
        avg_realized = _pnl_tracking[market]["cumulative_realized"] / total_trades
        REALIZED_VALUE_PER_TRADE.labels(market=market).set(avg_realized)

    return outcome


def get_pnl_summary(market: str) -> dict[str, float]:
    """Get P&L summary for a market.

    Args:
        market: Market identifier

    Returns:
        Dictionary with cumulative_expected, cumulative_realized, wins, losses, pending
    """
    if market not in _pnl_tracking:
        return {
            "cumulative_expected": 0.0,
            "cumulative_realized": 0.0,
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "pending": 0,
            "win_rate": 0.0,
        }

    data = _pnl_tracking[market]
    resolved = data["wins"] + data["losses"]
    win_rate = (data["wins"] / resolved * 100) if resolved > 0 else 0.0

    return {
        "cumulative_expected": data["cumulative_expected"],
        "cumulative_realized": data["cumulative_realized"],
        "total_trades": data["total_trades"],
        "wins": data["wins"],
        "losses": data["losses"],
        "pending": data["pending"],
        "win_rate": win_rate,
    }


def reset_pnl_tracking() -> None:
    """Reset P&L tracking (e.g., for daily reset)."""
    global _pnl_tracking
    _pnl_tracking = {}


# Phase 4d: Pre-trade expected hedge ratio calculation (Dec 13, 2025)
# Before placing orders, calculate what hedge ratio we expect based on liquidity
# This allows rejection of trades BEFORE any orders are placed

EXPECTED_HEDGE_RATIO = Gauge(
    "polymarket_expected_hedge_ratio",
    "Expected hedge ratio based on order book liquidity (pre-trade prediction)",
    ["market", "asset"],
)

EXPECTED_HEDGE_RATIO_HISTOGRAM = Histogram(
    "polymarket_expected_hedge_ratio_distribution",
    "Distribution of expected hedge ratios (pre-trade)",
    ["market"],
    buckets=[0.0, 0.2, 0.4, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 1.0],
)

HEDGE_RATIO_PREDICTION_ERROR = Histogram(
    "polymarket_hedge_ratio_prediction_error",
    "Error between expected and actual hedge ratio (actual - expected)",
    ["market"],
    buckets=[-0.5, -0.3, -0.2, -0.1, -0.05, 0.0, 0.05, 0.1, 0.2, 0.3, 0.5],
)

PRE_TRADE_REJECTIONS_TOTAL = Counter(
    "polymarket_pre_trade_rejections_total",
    "Trades rejected before execution due to predicted poor hedge",
    ["market", "reason"],  # reason: low_expected_hedge, insufficient_liquidity, imbalanced_books
)

LIQUIDITY_IMBALANCE_RATIO = Histogram(
    "polymarket_liquidity_imbalance_ratio",
    "Ratio of smaller side liquidity to larger side (1.0=balanced)",
    ["market"],
    buckets=[0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
)


def calculate_expected_hedge_ratio(
    yes_liquidity_shares: float,
    no_liquidity_shares: float,
    yes_shares_needed: float,
    no_shares_needed: float,
    persistence_factor: float = 0.4,
) -> tuple[float, str]:
    """Calculate expected hedge ratio based on order book liquidity.

    The expected hedge ratio predicts what hedge we'll achieve BEFORE placing orders.
    This is based on available liquidity vs shares needed.

    Key insight: If one side has less liquidity than shares needed, we expect
    partial fills which will create an imbalanced position.

    Args:
        yes_liquidity_shares: Available YES liquidity (displayed depth)
        no_liquidity_shares: Available NO liquidity (displayed depth)
        yes_shares_needed: YES shares we want to buy
        no_shares_needed: NO shares we want to buy
        persistence_factor: Fraction of displayed liquidity expected to persist (default 0.4)

    Returns:
        Tuple of (expected_hedge_ratio, reason_if_low)
        expected_hedge_ratio: 0.0 to 1.0, where 1.0 = perfectly balanced
        reason_if_low: Explanation if ratio is below acceptable threshold
    """
    # Apply persistence factor - not all displayed liquidity will fill
    yes_persistent = yes_liquidity_shares * persistence_factor
    no_persistent = no_liquidity_shares * persistence_factor

    # Handle edge cases
    if yes_shares_needed <= 0 or no_shares_needed <= 0:
        return 0.0, "zero_shares_needed"

    if yes_persistent <= 0 or no_persistent <= 0:
        return 0.0, "no_liquidity"

    # Calculate expected fills (capped at available liquidity)
    expected_yes_fill = min(yes_shares_needed, yes_persistent)
    expected_no_fill = min(no_shares_needed, no_persistent)

    # Handle case where expected fills are zero
    if expected_yes_fill <= 0 or expected_no_fill <= 0:
        return 0.0, "insufficient_liquidity"

    # Calculate expected hedge ratio: min(fills) / max(fills)
    min_fill = min(expected_yes_fill, expected_no_fill)
    max_fill = max(expected_yes_fill, expected_no_fill)

    expected_ratio = min_fill / max_fill if max_fill > 0 else 0.0

    # Determine reason if ratio is low
    reason = ""
    if expected_ratio < 0.8:
        if yes_persistent < yes_shares_needed and no_persistent < no_shares_needed:
            reason = "both_sides_insufficient"
        elif yes_persistent < yes_shares_needed:
            reason = "yes_liquidity_insufficient"
        elif no_persistent < no_shares_needed:
            reason = "no_liquidity_insufficient"
        else:
            reason = "imbalanced_order_sizes"

    return expected_ratio, reason


def record_expected_hedge_ratio(
    market: str,
    asset: str,
    yes_liquidity: float,
    no_liquidity: float,
    yes_shares_needed: float,
    no_shares_needed: float,
    min_hedge_ratio: float = 0.80,
) -> tuple[float, bool, str]:
    """Record pre-trade expected hedge ratio and check if trade should proceed.

    Args:
        market: Market identifier (condition_id)
        asset: Asset name (BTC, ETH, etc.)
        yes_liquidity: Available YES liquidity in shares
        no_liquidity: Available NO liquidity in shares
        yes_shares_needed: YES shares we want to buy
        no_shares_needed: NO shares we want to buy
        min_hedge_ratio: Minimum acceptable hedge ratio (default 0.80)

    Returns:
        Tuple of (expected_ratio, should_proceed, reason)
    """
    # Calculate expected hedge ratio
    expected_ratio, reason = calculate_expected_hedge_ratio(
        yes_liquidity_shares=yes_liquidity,
        no_liquidity_shares=no_liquidity,
        yes_shares_needed=yes_shares_needed,
        no_shares_needed=no_shares_needed,
    )

    # Record metrics
    EXPECTED_HEDGE_RATIO.labels(market=market, asset=asset).set(expected_ratio)
    EXPECTED_HEDGE_RATIO_HISTOGRAM.labels(market=market).observe(expected_ratio)

    # Record liquidity imbalance
    if yes_liquidity > 0 and no_liquidity > 0:
        imbalance = min(yes_liquidity, no_liquidity) / max(yes_liquidity, no_liquidity)
        LIQUIDITY_IMBALANCE_RATIO.labels(market=market).observe(imbalance)

    # Check if trade should proceed
    should_proceed = expected_ratio >= min_hedge_ratio

    if not should_proceed:
        PRE_TRADE_REJECTIONS_TOTAL.labels(
            market=market,
            reason=reason or "low_expected_hedge",
        ).inc()

    return expected_ratio, should_proceed, reason


def record_hedge_prediction_accuracy(
    market: str,
    expected_ratio: float,
    actual_ratio: float,
) -> float:
    """Record the accuracy of hedge ratio prediction after trade execution.

    This helps us tune the persistence_factor and improve predictions.

    Args:
        market: Market identifier
        expected_ratio: Pre-trade expected hedge ratio
        actual_ratio: Post-trade actual hedge ratio

    Returns:
        Prediction error (actual - expected)
    """
    error = actual_ratio - expected_ratio
    HEDGE_RATIO_PREDICTION_ERROR.labels(market=market).observe(error)
    return error


# Phase 4e: Post-trade rebalancing logic (Dec 13, 2025)
# When a trade results in an imbalanced position, immediately sell excess shares
# to eliminate unhedged directional exposure. Uses IOC (immediate-or-cancel) style.

REBALANCE_ATTEMPTS_TOTAL = Counter(
    "polymarket_rebalance_attempts_total",
    "Total rebalancing attempts after imbalanced trades",
    ["market", "side"],  # side: YES, NO (which side we're selling)
)

REBALANCE_OUTCOME_TOTAL = Counter(
    "polymarket_rebalance_outcome_total",
    "Outcomes of rebalancing attempts",
    ["market", "outcome"],  # outcome: full_fill, partial_fill, no_fill, skipped
)

REBALANCE_SHARES_SOLD = Histogram(
    "polymarket_rebalance_shares_sold",
    "Number of shares sold during rebalancing",
    ["market", "side"],
    buckets=[1, 2, 5, 10, 15, 20, 30, 50, 100],
)

REBALANCE_SLIPPAGE_USD = Histogram(
    "polymarket_rebalance_slippage_usd",
    "Cost of rebalancing (slippage from original purchase price)",
    ["market"],
    buckets=[0.0, 0.01, 0.02, 0.05, 0.10, 0.20, 0.50, 1.00, 2.00],
)

POST_REBALANCE_HEDGE_RATIO = Gauge(
    "polymarket_post_rebalance_hedge_ratio",
    "Hedge ratio after rebalancing attempt",
    ["market", "asset"],
)

UNHEDGED_SHARES_REMAINING = Gauge(
    "polymarket_unhedged_shares_remaining",
    "Unhedged shares remaining after rebalancing",
    ["market", "side"],
)


@dataclass
class RebalanceDecision:
    """Decision about whether and how to rebalance a position."""

    should_rebalance: bool
    side_to_sell: str  # "YES" or "NO"
    shares_to_sell: float
    current_hedge_ratio: float
    target_hedge_ratio: float
    reason: str


@dataclass
class RebalanceResult:
    """Result of a rebalancing attempt."""

    success: bool
    shares_requested: float
    shares_filled: float
    fill_price: float
    slippage_usd: float
    pre_hedge_ratio: float
    post_hedge_ratio: float
    outcome: str  # "full_fill", "partial_fill", "no_fill", "skipped"


def calculate_rebalance_needed(
    yes_shares: float,
    no_shares: float,
    min_hedge_ratio: float = 0.80,
    max_imbalance_shares: float = 5.0,
) -> RebalanceDecision:
    """Calculate if rebalancing is needed and how much.

    Determines which side has excess shares and how many to sell
    to bring the position back into balance.

    Args:
        yes_shares: Current YES shares held
        no_shares: Current NO shares held
        min_hedge_ratio: Minimum acceptable hedge ratio (default 0.80)
        max_imbalance_shares: Maximum unhedged shares allowed (default 5.0)

    Returns:
        RebalanceDecision with details about rebalancing action
    """
    # Handle edge cases
    if yes_shares <= 0 and no_shares <= 0:
        return RebalanceDecision(
            should_rebalance=False,
            side_to_sell="",
            shares_to_sell=0.0,
            current_hedge_ratio=0.0,
            target_hedge_ratio=1.0,
            reason="no_position",
        )

    if yes_shares <= 0 or no_shares <= 0:
        # Completely one-sided position
        side = "YES" if yes_shares > 0 else "NO"
        shares = yes_shares if yes_shares > 0 else no_shares
        return RebalanceDecision(
            should_rebalance=True,
            side_to_sell=side,
            shares_to_sell=shares,  # Sell everything
            current_hedge_ratio=0.0,
            target_hedge_ratio=1.0,
            reason="one_sided_position",
        )

    # Calculate current hedge ratio
    min_shares = min(yes_shares, no_shares)
    max_shares = max(yes_shares, no_shares)
    current_ratio = min_shares / max_shares if max_shares > 0 else 0.0

    # Calculate imbalance
    imbalance = abs(yes_shares - no_shares)

    # Determine if rebalancing is needed
    if current_ratio >= min_hedge_ratio and imbalance <= max_imbalance_shares:
        return RebalanceDecision(
            should_rebalance=False,
            side_to_sell="",
            shares_to_sell=0.0,
            current_hedge_ratio=current_ratio,
            target_hedge_ratio=1.0,
            reason="within_tolerance",
        )

    # Determine which side to sell (the one with more shares)
    if yes_shares > no_shares:
        side_to_sell = "YES"
        shares_to_sell = yes_shares - no_shares  # Sell excess to match NO
    else:
        side_to_sell = "NO"
        shares_to_sell = no_shares - yes_shares  # Sell excess to match YES

    return RebalanceDecision(
        should_rebalance=True,
        side_to_sell=side_to_sell,
        shares_to_sell=shares_to_sell,
        current_hedge_ratio=current_ratio,
        target_hedge_ratio=1.0,
        reason="hedge_ratio_low" if current_ratio < min_hedge_ratio else "imbalance_high",
    )


def record_rebalance_attempt(
    market: str,
    asset: str,
    decision: RebalanceDecision,
    result: RebalanceResult,
) -> None:
    """Record metrics for a rebalancing attempt.

    Args:
        market: Market identifier
        asset: Asset name (BTC, ETH, etc.)
        decision: The rebalancing decision that was made
        result: The result of executing the rebalance
    """
    # Record attempt
    REBALANCE_ATTEMPTS_TOTAL.labels(
        market=market,
        side=decision.side_to_sell,
    ).inc()

    # Record outcome
    REBALANCE_OUTCOME_TOTAL.labels(
        market=market,
        outcome=result.outcome,
    ).inc()

    # Record shares sold (if any)
    if result.shares_filled > 0:
        REBALANCE_SHARES_SOLD.labels(
            market=market,
            side=decision.side_to_sell,
        ).observe(result.shares_filled)

    # Record slippage
    if result.slippage_usd != 0:
        REBALANCE_SLIPPAGE_USD.labels(market=market).observe(abs(result.slippage_usd))

    # Record post-rebalance hedge ratio
    POST_REBALANCE_HEDGE_RATIO.labels(
        market=market,
        asset=asset,
    ).set(result.post_hedge_ratio)

    # Record remaining unhedged shares
    remaining = decision.shares_to_sell - result.shares_filled
    if remaining > 0:
        UNHEDGED_SHARES_REMAINING.labels(
            market=market,
            side=decision.side_to_sell,
        ).set(remaining)
    else:
        UNHEDGED_SHARES_REMAINING.labels(
            market=market,
            side=decision.side_to_sell,
        ).set(0)


def evaluate_rebalance_worth(
    shares_to_sell: float,
    expected_fill_price: float,
    original_purchase_price: float,
    max_slippage_pct: float = 0.10,
) -> tuple[bool, float, str]:
    """Evaluate if a rebalancing trade is worth the cost.

    Compares the expected slippage cost against the benefit of reducing risk.

    Args:
        shares_to_sell: Number of shares to sell
        expected_fill_price: Expected price we'll get (bid price)
        original_purchase_price: Price we originally paid
        max_slippage_pct: Maximum acceptable slippage as percentage (default 10%)

    Returns:
        Tuple of (is_worth_it, expected_slippage_usd, reason)
    """
    if shares_to_sell <= 0:
        return False, 0.0, "no_shares_to_sell"

    if expected_fill_price <= 0:
        return False, 0.0, "invalid_price"

    # Calculate expected slippage
    # Slippage = (original_price - sell_price) * shares
    slippage_per_share = original_purchase_price - expected_fill_price
    total_slippage = slippage_per_share * shares_to_sell

    # Calculate slippage percentage
    original_value = original_purchase_price * shares_to_sell
    slippage_pct = abs(slippage_per_share) / original_purchase_price if original_purchase_price > 0 else 0

    # Check if slippage is acceptable
    # If total_slippage is negative, we're selling at a profit - always acceptable
    if total_slippage < 0:
        return True, total_slippage, "acceptable"

    # Only reject if slippage (loss) exceeds threshold
    if slippage_pct > max_slippage_pct:
        return False, total_slippage, f"slippage_too_high_{slippage_pct:.1%}"

    # Even with positive slippage (selling at a loss), rebalancing is worth it
    # to eliminate directional risk (as long as within threshold)
    return True, total_slippage, "acceptable"


def calculate_post_rebalance_hedge_ratio(
    yes_shares_before: float,
    no_shares_before: float,
    side_sold: str,
    shares_sold: float,
) -> float:
    """Calculate hedge ratio after a rebalancing sale.

    Args:
        yes_shares_before: YES shares before rebalancing
        no_shares_before: NO shares before rebalancing
        side_sold: Which side was sold ("YES" or "NO")
        shares_sold: How many shares were sold

    Returns:
        New hedge ratio after the sale
    """
    if side_sold.upper() == "YES":
        yes_after = yes_shares_before - shares_sold
        no_after = no_shares_before
    else:
        yes_after = yes_shares_before
        no_after = no_shares_before - shares_sold

    if yes_after <= 0 or no_after <= 0:
        return 0.0

    min_shares = min(yes_after, no_after)
    max_shares = max(yes_after, no_after)

    return min_shares / max_shares if max_shares > 0 else 0.0
