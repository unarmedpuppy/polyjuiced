# Active Trade Management & Position Rebalancing

**Created:** December 14, 2025
**Updated:** December 14, 2025
**Status:** IMPLEMENTED - See src/position_manager.py

---

## Core Philosophy

**The primary goal of trade execution is EQUAL SIZED positions for YES & NO, with an arbitrage spread of at least $0.02.**

This is NOT a fire-and-forget system. After initial execution, we **actively manage** positions throughout the market's lifetime, continuously seeking to balance positions.

---

## Problem Statement

When a partial fill occurs (e.g., 10 YES @ $0.48 but only 6 NO @ $0.49), we're left with an **unhedged position**. The current behavior is to hold until resolution, but this exposes us to directional risk.

**Current Risk:**
- 10 YES, 6 NO = 40% unhedged
- If YES wins: Get $10, paid ~$7.74 = +$2.26 profit
- If NO wins: Get $6, paid ~$7.74 = -$1.74 loss
- Expected value depends on actual probability - NOT arbitrage!

---

## Active Trade Management (NEW)

### Shift from Fire-and-Forget to Active Management

| Old Approach | New Approach |
|--------------|--------------|
| Detect → Execute → Record → Done | Detect → Execute → **Monitor → Rebalance → Monitor** → Resolve |
| One-shot execution | Continuous position management |
| Accept partial fills | Actively work to balance |
| No visibility into latency | Full timing telemetry |

### Position Lifecycle

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         ACTIVE TRADE MANAGEMENT                              │
│                                                                             │
│  1. OPPORTUNITY          2. EXECUTION           3. ACTIVE MANAGEMENT        │
│  ───────────────         ───────────            ──────────────────          │
│                                                                             │
│  ┌──────────────┐       ┌──────────────┐       ┌──────────────────────────┐ │
│  │ Detect arb   │       │ Place orders │       │ Monitor position         │ │
│  │ opportunity  │──────▶│ (YES + NO)   │──────▶│                          │ │
│  │              │       │              │       │ ┌────────────────────┐   │ │
│  │ Log:         │       │ Log:         │       │ │ If imbalanced:     │   │ │
│  │ • detected_at│       │ • placed_at  │       │ │ • Check prices     │   │ │
│  └──────────────┘       │ • filled_at  │       │ │ • Seek rebalancing │   │ │
│                         └──────────────┘       │ │ • Execute trades   │   │ │
│                                                │ │ • Repeat until     │   │ │
│                                                │ │   balanced         │   │ │
│                                                │ └────────────────────┘   │ │
│                                                │                          │ │
│                                                │ Continue until:          │ │
│                                                │ • Position balanced, OR  │ │
│                                                │ • Market resolves        │ │
│                                                └──────────────────────────┘ │
│                                                                             │
│  4. RESOLUTION                                                              │
│  ────────────                                                               │
│  ┌──────────────┐                                                           │
│  │ Market ends  │                                                           │
│  │ Record P&L   │                                                           │
│  │ Log timing   │                                                           │
│  └──────────────┘                                                           │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Key Principle: Always Aim for Equal Sizes

At any point during the market, if we have an imbalanced position:
- **Goal**: Get YES shares == NO shares
- **Method**: Sell excess OR buy deficit (whichever is profitable)
- **Constraint**: Stay within max position size
- **Persistence**: Keep trying until balanced or market ends

---

## Rebalancing Strategy

### Core Principle

**Instead of accepting directional risk, actively rebalance the position when profitable opportunities arise.**

### Two Rebalancing Approaches

#### 1. SELL EXCESS (Larger Position)

When the **larger position's price rises above our entry**, sell excess shares to lock in profit.

```
Example:
- Position: 10 YES @ $0.48, 6 NO @ $0.49
- Imbalance: 4 YES shares unhedged
- If YES bid rises to $0.55:
  → Sell 4 YES @ $0.55
  → Profit: 4 × ($0.55 - $0.48) = $0.28
  → Final: 6 YES, 6 NO (perfectly hedged)
  → Guaranteed profit at resolution: 6 × $1 - remaining cost
```

#### 2. BUY DEFICIT (Smaller Position)

When the **smaller position's price drops**, buy more shares to complete the hedge.

```
Example:
- Position: 10 YES @ $0.48, 6 NO @ $0.49
- Imbalance: 4 NO shares short
- If NO ask drops to $0.42:
  → Buy 4 NO @ $0.42
  → Cost: 4 × $0.42 = $1.68
  → Final: 10 YES, 10 NO (perfectly hedged)
  → Guaranteed profit: 10 × $1 - total cost
```

---

## Decision Logic

### Step 1: Determine if Rebalancing is Needed

```python
REBALANCE_THRESHOLD = 0.80  # 80% hedge ratio

def needs_rebalancing(yes_shares: float, no_shares: float) -> bool:
    """Return True if position needs rebalancing."""
    if max(yes_shares, no_shares) == 0:
        return False

    hedge_ratio = min(yes_shares, no_shares) / max(yes_shares, no_shares)
    return hedge_ratio < REBALANCE_THRESHOLD
```

### Step 2: Identify Rebalancing Options

```python
def get_rebalancing_options(
    yes_shares: float,
    no_shares: float,
    yes_entry_price: float,
    no_entry_price: float,
    current_yes_bid: float,
    current_yes_ask: float,
    current_no_bid: float,
    current_no_ask: float,
) -> List[RebalanceOption]:
    """Get available rebalancing options."""
    options = []

    if yes_shares > no_shares:
        # Option A: Sell excess YES
        excess = yes_shares - no_shares
        sell_profit = excess * (current_yes_bid - yes_entry_price)
        if sell_profit > 0:
            options.append(RebalanceOption(
                action="SELL_YES",
                shares=excess,
                price=current_yes_bid,
                profit=sell_profit,
            ))

        # Option B: Buy more NO
        deficit = yes_shares - no_shares
        buy_cost = deficit * current_no_ask
        # Check if total position is still profitable
        total_cost = (yes_shares * yes_entry_price) + (no_shares * no_entry_price) + buy_cost
        guaranteed_return = yes_shares * 1.0
        if guaranteed_return > total_cost:
            options.append(RebalanceOption(
                action="BUY_NO",
                shares=deficit,
                price=current_no_ask,
                profit=guaranteed_return - total_cost,
            ))

    else:  # no_shares > yes_shares
        # Similar logic reversed...
        pass

    return options
```

### Step 3: Execute Best Option

```python
MIN_REBALANCE_PROFIT = 0.02  # $0.02 per share minimum

def should_execute_rebalance(option: RebalanceOption) -> bool:
    """Determine if a rebalancing option should be executed."""
    profit_per_share = option.profit / option.shares
    return profit_per_share >= MIN_REBALANCE_PROFIT
```

---

## Configuration Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `REBALANCE_THRESHOLD` | 0.80 | Minimum hedge ratio before seeking rebalancing |
| `MIN_REBALANCE_PROFIT` | $0.02 | Minimum profit per share to execute rebalance |
| `MAX_REBALANCE_WAIT` | 60s | Don't rebalance in last 60 seconds before resolution |
| `REBALANCE_CHECK_INTERVAL` | 5s | How often to check for rebalancing opportunities |
| `PREFER_SELL_OVER_BUY` | True | When both options profitable, prefer selling (capital efficient) |
| `ALLOW_PARTIAL_REBALANCE` | True | Take partial rebalancing if full balance not available |
| `MAX_REBALANCE_TRADES` | 5 | Maximum rebalancing trades per position |

---

## Timing Telemetry (NEW)

### Purpose

Track timing from opportunity detection through execution to understand:
- How fast are we executing?
- Are we missing opportunities due to latency?
- How long does rebalancing take?
- What's the average time to achieve balanced positions?

### Timestamps to Capture

```python
@dataclass
class TradeTelemetry:
    """Timing telemetry for trade execution analysis."""
    trade_id: str

    # Opportunity Phase
    opportunity_detected_at: datetime      # When spread opportunity first seen
    opportunity_spread: float              # Spread at detection time
    opportunity_yes_price: float           # YES price at detection
    opportunity_no_price: float            # NO price at detection

    # Execution Phase
    order_placed_at: datetime = None       # When orders submitted to exchange
    order_filled_at: datetime = None       # When fill confirmation received
    execution_latency_ms: float = None     # placed_at - detected_at
    fill_latency_ms: float = None          # filled_at - placed_at

    # Position State at Fill
    initial_yes_shares: float = 0.0
    initial_no_shares: float = 0.0
    initial_hedge_ratio: float = 0.0

    # Rebalancing Phase (if needed)
    rebalance_started_at: datetime = None  # When we started seeking rebalance
    rebalance_attempts: int = 0            # Number of rebalancing attempts
    rebalance_trades: List[Dict] = None    # Details of each rebalance trade
    position_balanced_at: datetime = None  # When position became balanced

    # Resolution
    resolved_at: datetime = None           # Market resolution time
    final_yes_shares: float = 0.0
    final_no_shares: float = 0.0
    final_hedge_ratio: float = 0.0
    actual_profit: float = 0.0

    @property
    def total_execution_time_ms(self) -> float:
        """Time from detection to fill."""
        if self.order_filled_at and self.opportunity_detected_at:
            return (self.order_filled_at - self.opportunity_detected_at).total_seconds() * 1000
        return None

    @property
    def time_to_balance_ms(self) -> float:
        """Time from initial fill to balanced position."""
        if self.position_balanced_at and self.order_filled_at:
            return (self.position_balanced_at - self.order_filled_at).total_seconds() * 1000
        return None
```

### Telemetry Events

```python
class TelemetryEvents:
    """Event types for timing telemetry."""
    OPPORTUNITY_DETECTED = "telemetry.opportunity_detected"
    ORDER_PLACED = "telemetry.order_placed"
    ORDER_FILLED = "telemetry.order_filled"
    REBALANCE_STARTED = "telemetry.rebalance_started"
    REBALANCE_ATTEMPTED = "telemetry.rebalance_attempted"
    REBALANCE_SUCCEEDED = "telemetry.rebalance_succeeded"
    REBALANCE_FAILED = "telemetry.rebalance_failed"
    POSITION_BALANCED = "telemetry.position_balanced"
    TRADE_RESOLVED = "telemetry.trade_resolved"
```

### Database Schema for Telemetry

```sql
-- New table for detailed timing telemetry
CREATE TABLE trade_telemetry (
    trade_id TEXT PRIMARY KEY,

    -- Opportunity timing
    opportunity_detected_at TIMESTAMP,
    opportunity_spread REAL,
    opportunity_yes_price REAL,
    opportunity_no_price REAL,

    -- Execution timing
    order_placed_at TIMESTAMP,
    order_filled_at TIMESTAMP,
    execution_latency_ms REAL,
    fill_latency_ms REAL,

    -- Initial position
    initial_yes_shares REAL,
    initial_no_shares REAL,
    initial_hedge_ratio REAL,

    -- Rebalancing
    rebalance_started_at TIMESTAMP,
    rebalance_attempts INTEGER DEFAULT 0,
    position_balanced_at TIMESTAMP,

    -- Resolution
    resolved_at TIMESTAMP,
    final_yes_shares REAL,
    final_no_shares REAL,
    final_hedge_ratio REAL,
    actual_profit REAL,

    FOREIGN KEY (trade_id) REFERENCES trades(id)
);

-- Table for individual rebalancing trades
CREATE TABLE rebalance_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT,
    attempted_at TIMESTAMP,
    action TEXT,              -- SELL_YES, BUY_NO, etc.
    shares REAL,
    price REAL,
    status TEXT,              -- SUCCESS, FAILED, PARTIAL
    filled_shares REAL,
    profit REAL,

    FOREIGN KEY (trade_id) REFERENCES trades(id)
);
```

### Analysis Queries

```sql
-- Average execution latency
SELECT
    AVG(execution_latency_ms) as avg_latency_ms,
    MIN(execution_latency_ms) as min_latency_ms,
    MAX(execution_latency_ms) as max_latency_ms,
    COUNT(*) as total_trades
FROM trade_telemetry
WHERE order_placed_at IS NOT NULL;

-- Trades that required rebalancing
SELECT
    COUNT(*) as total_needing_rebalance,
    SUM(CASE WHEN position_balanced_at IS NOT NULL THEN 1 ELSE 0 END) as successfully_balanced,
    AVG(rebalance_attempts) as avg_attempts
FROM trade_telemetry
WHERE rebalance_started_at IS NOT NULL;

-- Time to balance distribution
SELECT
    CASE
        WHEN (julianday(position_balanced_at) - julianday(order_filled_at)) * 86400 < 60 THEN '< 1 min'
        WHEN (julianday(position_balanced_at) - julianday(order_filled_at)) * 86400 < 300 THEN '1-5 min'
        ELSE '> 5 min'
    END as time_bucket,
    COUNT(*) as count
FROM trade_telemetry
WHERE position_balanced_at IS NOT NULL
GROUP BY time_bucket;
```

---

## Implementation Plan

### Overview: ActivePositionManager

The core component is the `ActivePositionManager` class that:
1. Tracks ALL open positions (not just imbalanced ones)
2. Monitors real-time prices via WebSocket
3. Actively seeks rebalancing opportunities
4. Records telemetry for all actions
5. Handles position lifecycle from execution to resolution

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         ActivePositionManager                                │
│                                                                             │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │                         Active Positions                                │ │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                  │ │
│  │  │ Position 1   │  │ Position 2   │  │ Position 3   │                  │ │
│  │  │ BTC 10Y/6N   │  │ ETH 8Y/8N    │  │ SOL 5Y/0N    │                  │ │
│  │  │ hedge: 60%   │  │ hedge: 100%  │  │ hedge: 0%    │                  │ │
│  │  │ REBALANCING  │  │ BALANCED     │  │ REBALANCING  │                  │ │
│  │  └──────────────┘  └──────────────┘  └──────────────┘                  │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │                         WebSocket Price Feed                            │ │
│  │  On price update:                                                       │ │
│  │    → Check if any position can rebalance profitably                    │ │
│  │    → Execute immediately if opportunity exists                         │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │                         Telemetry Recorder                              │ │
│  │  • Log all timestamps                                                   │ │
│  │  • Track rebalancing attempts                                          │ │
│  │  • Compute latency metrics                                             │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

### Phase 1: Position Tracking

Track ALL open positions (balanced and imbalanced):

```python
@dataclass
class ActivePosition:
    """Tracks an active position throughout its lifecycle."""
    trade_id: str
    market: Market15Min

    # Position details (mutable as rebalancing occurs)
    yes_shares: float
    no_shares: float
    yes_avg_price: float   # Weighted average entry price
    no_avg_price: float

    # Telemetry
    telemetry: TradeTelemetry

    # Tracking
    created_at: datetime
    resolution_time: datetime
    status: str = "ACTIVE"  # ACTIVE, BALANCED, RESOLVED

    # Rebalancing history
    rebalance_trades: List[Dict] = field(default_factory=list)

    @property
    def hedge_ratio(self) -> float:
        if max(self.yes_shares, self.no_shares) == 0:
            return 0.0
        return min(self.yes_shares, self.no_shares) / max(self.yes_shares, self.no_shares)

    @property
    def is_balanced(self) -> bool:
        return self.hedge_ratio >= 0.80

    @property
    def needs_rebalancing(self) -> bool:
        return not self.is_balanced

    @property
    def excess_side(self) -> str:
        return "YES" if self.yes_shares > self.no_shares else "NO"

    @property
    def deficit_side(self) -> str:
        return "NO" if self.yes_shares > self.no_shares else "YES"

    @property
    def excess_shares(self) -> float:
        return abs(self.yes_shares - self.no_shares)

    @property
    def total_cost(self) -> float:
        return (self.yes_shares * self.yes_avg_price) + (self.no_shares * self.no_avg_price)

    @property
    def max_additional_cost(self) -> float:
        """How much more can we spend within position limits."""
        # TODO: Get from config
        max_position_size = 25.0
        return max(0, max_position_size - self.total_cost)

    def update_after_rebalance(
        self,
        action: str,
        shares: float,
        price: float,
        filled_shares: float,
    ) -> None:
        """Update position after a rebalancing trade."""
        if action == "SELL_YES":
            self.yes_shares -= filled_shares
        elif action == "SELL_NO":
            self.no_shares -= filled_shares
        elif action == "BUY_YES":
            # Update weighted average price
            total_yes_cost = (self.yes_shares * self.yes_avg_price) + (filled_shares * price)
            self.yes_shares += filled_shares
            self.yes_avg_price = total_yes_cost / self.yes_shares
        elif action == "BUY_NO":
            total_no_cost = (self.no_shares * self.no_avg_price) + (filled_shares * price)
            self.no_shares += filled_shares
            self.no_avg_price = total_no_cost / self.no_shares

        # Record the trade
        self.rebalance_trades.append({
            "action": action,
            "shares": shares,
            "filled_shares": filled_shares,
            "price": price,
            "timestamp": datetime.utcnow().isoformat(),
        })

        # Update telemetry
        self.telemetry.rebalance_attempts += 1
        if self.is_balanced:
            self.telemetry.position_balanced_at = datetime.utcnow()
            self.status = "BALANCED"
```

### Phase 2: ActivePositionManager

The core class that manages all active positions and handles rebalancing:

```python
class ActivePositionManager:
    """Manages all active positions and handles rebalancing.

    Key Features:
    - Tracks ALL open positions (not just imbalanced)
    - Receives real-time price updates via WebSocket callback
    - Immediately evaluates rebalancing opportunities on price change
    - Records full telemetry for analysis
    """

    def __init__(self, strategy: GabagoolStrategy, config: RebalancingConfig):
        self.strategy = strategy
        self.config = config
        self.positions: Dict[str, ActivePosition] = {}
        self._telemetry_recorder = TelemetryRecorder()

    # =========================================================================
    # Position Lifecycle
    # =========================================================================

    async def add_position(self, position: ActivePosition) -> None:
        """Add a new position to track."""
        self.positions[position.trade_id] = position

        log.info(
            "Position added to active management",
            trade_id=position.trade_id,
            yes_shares=position.yes_shares,
            no_shares=position.no_shares,
            hedge_ratio=position.hedge_ratio,
            status="BALANCED" if position.is_balanced else "NEEDS_REBALANCING",
        )

        # If imbalanced, start seeking rebalancing immediately
        if position.needs_rebalancing:
            position.telemetry.rebalance_started_at = datetime.utcnow()
            await self._emit_event(TelemetryEvents.REBALANCE_STARTED, position)

    async def remove_position(self, trade_id: str, reason: str = "resolved") -> None:
        """Remove a position (usually after resolution)."""
        if trade_id in self.positions:
            position = self.positions.pop(trade_id)
            position.status = "RESOLVED"
            position.telemetry.resolved_at = datetime.utcnow()

            # Save final telemetry
            await self._telemetry_recorder.save(position.telemetry)
            await self._emit_event(TelemetryEvents.TRADE_RESOLVED, position)

    # =========================================================================
    # Real-Time Price Updates (WebSocket Callback)
    # =========================================================================

    async def on_price_update(self, condition_id: str, market_state: MarketState) -> None:
        """Called on every WebSocket price update.

        This is the key to ACTIVE management - we check for rebalancing
        opportunities immediately when prices change, not on a timer.
        """
        # Find positions for this market
        relevant_positions = [
            p for p in self.positions.values()
            if p.market.condition_id == condition_id and p.needs_rebalancing
        ]

        for position in relevant_positions:
            await self._evaluate_rebalancing(position, market_state)

    async def _evaluate_rebalancing(
        self,
        position: ActivePosition,
        market_state: MarketState,
    ) -> None:
        """Evaluate and potentially execute rebalancing for a position."""
        # Skip if too close to resolution
        time_remaining = (position.resolution_time - datetime.utcnow()).total_seconds()
        if time_remaining < self.config.max_rebalance_wait:
            return

        # Skip if max rebalance attempts reached
        if position.telemetry.rebalance_attempts >= self.config.max_rebalance_trades:
            return

        # Get rebalancing options
        options = self._get_rebalancing_options(position, market_state)

        # Select best option
        best_option = self._select_best_option(options)

        if best_option and self._should_execute(best_option, position):
            await self._execute_rebalance(position, best_option)

    # =========================================================================
    # Rebalancing Logic
    # =========================================================================

    def _get_rebalancing_options(
        self,
        position: ActivePosition,
        market_state: MarketState,
    ) -> List[RebalanceOption]:
        """Get available rebalancing options."""
        options = []

        if position.yes_shares > position.no_shares:
            excess = position.yes_shares - position.no_shares

            # Option A: Sell excess YES
            sell_profit = excess * (market_state.yes_best_bid - position.yes_avg_price)
            if sell_profit > 0:
                options.append(RebalanceOption(
                    action="SELL_YES",
                    shares=excess,
                    price=market_state.yes_best_bid,
                    profit=sell_profit,
                ))

            # Option B: Buy more NO (within budget)
            max_buy_cost = position.max_additional_cost
            max_shares = max_buy_cost / market_state.no_best_ask if market_state.no_best_ask > 0 else 0
            shares_to_buy = min(excess, max_shares)

            if shares_to_buy > 0:
                buy_cost = shares_to_buy * market_state.no_best_ask
                new_total_cost = position.total_cost + buy_cost
                new_guaranteed_return = (position.yes_shares) * 1.0  # Will have more hedged shares
                buy_profit = new_guaranteed_return - new_total_cost

                if buy_profit > 0:
                    options.append(RebalanceOption(
                        action="BUY_NO",
                        shares=shares_to_buy,
                        price=market_state.no_best_ask,
                        profit=buy_profit,
                    ))

        else:  # no_shares > yes_shares
            # Mirror logic for excess NO
            excess = position.no_shares - position.yes_shares

            sell_profit = excess * (market_state.no_best_bid - position.no_avg_price)
            if sell_profit > 0:
                options.append(RebalanceOption(
                    action="SELL_NO",
                    shares=excess,
                    price=market_state.no_best_bid,
                    profit=sell_profit,
                ))

            # Buy YES option...
            max_buy_cost = position.max_additional_cost
            max_shares = max_buy_cost / market_state.yes_best_ask if market_state.yes_best_ask > 0 else 0
            shares_to_buy = min(excess, max_shares)

            if shares_to_buy > 0:
                buy_cost = shares_to_buy * market_state.yes_best_ask
                new_total_cost = position.total_cost + buy_cost
                new_guaranteed_return = (position.no_shares) * 1.0
                buy_profit = new_guaranteed_return - new_total_cost

                if buy_profit > 0:
                    options.append(RebalanceOption(
                        action="BUY_YES",
                        shares=shares_to_buy,
                        price=market_state.yes_best_ask,
                        profit=buy_profit,
                    ))

        return options

    def _select_best_option(self, options: List[RebalanceOption]) -> Optional[RebalanceOption]:
        """Select the best rebalancing option."""
        if not options:
            return None

        # Filter by minimum profit
        viable = [
            o for o in options
            if o.profit / o.shares >= self.config.min_profit_per_share
        ]

        if not viable:
            return None

        # Prefer selling if configured (capital efficient)
        if self.config.prefer_sell_over_buy:
            sell_options = [o for o in viable if o.action.startswith("SELL")]
            if sell_options:
                return max(sell_options, key=lambda o: o.profit)

        return max(viable, key=lambda o: o.profit)

    def _should_execute(self, option: RebalanceOption, position: ActivePosition) -> bool:
        """Final check before executing."""
        # Check minimum profit threshold
        if option.profit / option.shares < self.config.min_profit_per_share:
            return False

        # Check if partial rebalancing is allowed
        if not self.config.allow_partial_rebalance:
            if option.shares < position.excess_shares:
                return False

        return True

    async def _execute_rebalance(
        self,
        position: UnbalancedPosition,
        option: RebalanceOption,
    ) -> None:
        """Execute a rebalancing trade."""
        log.info(
            "Executing rebalance",
            trade_id=position.trade_id,
            action=option.action,
            shares=option.shares,
            price=option.price,
            expected_profit=option.profit,
        )

        if option.action == "SELL_YES":
            result = await self.strategy._client.place_order(
                token_id=position.market.yes_token_id,
                side="SELL",
                size=option.shares,
                price=option.price,
            )
            if result.status == "MATCHED":
                position.yes_shares -= option.shares

        elif option.action == "SELL_NO":
            # Similar...
            pass

        elif option.action == "BUY_YES":
            result = await self.strategy._client.place_order(
                token_id=position.market.yes_token_id,
                side="BUY",
                size=option.shares,
                price=option.price,
            )
            if result.status == "MATCHED":
                position.yes_shares += option.shares

        elif option.action == "BUY_NO":
            # Similar...
            pass

        # Check if now balanced
        if not position.needs_rebalancing:
            del self.positions[position.trade_id]
            log.info("Position now balanced", trade_id=position.trade_id)
```

### Phase 3: Integration with Strategy

Modify `GabagoolStrategy` to use the rebalancing monitor:

```python
class GabagoolStrategy:
    def __init__(self, ...):
        # ... existing init ...
        self._rebalancing_monitor = RebalancingMonitor(self)

    async def _record_trade(self, ...):
        # ... existing recording ...

        # If partial fill, add to rebalancing monitor
        if hedge_ratio < 0.80:
            position = UnbalancedPosition(
                trade_id=trade_id,
                market=market,
                yes_shares=actual_yes_shares,
                no_shares=actual_no_shares,
                yes_entry_price=yes_price,
                no_entry_price=no_price,
                created_at=datetime.utcnow(),
                resolution_time=market.end_time,
            )
            await self._rebalancing_monitor.add_position(position)

    async def start(self):
        # ... existing start ...

        # Start rebalancing monitor
        asyncio.create_task(self._rebalancing_loop())

    async def _rebalancing_loop(self):
        """Background loop to check for rebalancing opportunities."""
        while self._running:
            await self._rebalancing_monitor.check_rebalancing_opportunities()
            await asyncio.sleep(5)  # Check every 5 seconds
```

---

## Database Schema Updates

Add rebalancing tracking to the trades table:

```sql
-- Add to trades table
rebalance_status TEXT DEFAULT 'not_needed',  -- 'not_needed', 'pending', 'completed', 'failed'
rebalance_action TEXT,                        -- 'SELL_YES', 'BUY_NO', etc.
rebalance_shares REAL,
rebalance_price REAL,
rebalance_profit REAL,
rebalanced_at TIMESTAMP,
final_yes_shares REAL,
final_no_shares REAL,
final_hedge_ratio REAL,
```

---

## Events

Add rebalancing events to the event system:

```python
class EventTypes:
    # ... existing ...
    REBALANCE_OPPORTUNITY = "rebalance_opportunity"
    REBALANCE_EXECUTED = "rebalance_executed"
    POSITION_BALANCED = "position_balanced"
```

---

## Risk Considerations

### 1. Rebalancing Can Fail

If the rebalancing order doesn't fill:
- Continue monitoring for next opportunity
- Log the failed attempt
- Don't give up on the position

### 2. Market Might Not Provide Opportunity

Sometimes prices won't move favorably before resolution:
- Accept that not all positions can be rebalanced
- Hold to resolution as fallback
- Track statistics on rebalancing success rate

### 3. Transaction Costs

Rebalancing incurs fees:
- Ensure minimum profit threshold covers fees
- Consider Polymarket fee structure (typically 0-2%)
- Factor fees into `MIN_REBALANCE_PROFIT`

### 4. Slippage on Rebalancing Orders

Market might move while executing:
- Use limit orders with reasonable slippage
- Monitor fill rates
- Adjust strategy based on observed slippage

---

## Success Metrics

Track these metrics to evaluate rebalancing effectiveness:

| Metric | Description |
|--------|-------------|
| Rebalancing Rate | % of partial fills that get rebalanced |
| Avg Time to Rebalance | How quickly opportunities appear |
| Rebalancing Profit | Additional profit from rebalancing |
| Avoided Losses | Losses prevented by rebalancing |
| Fill Rate | % of rebalancing orders that fill |

---

## Example Walkthrough

### Scenario: Partial Fill with Profitable Rebalancing

1. **Initial Trade (t=0)**
   - Opportunity: YES @ $0.48, NO @ $0.49 (3¢ spread)
   - Budget: $10
   - Target: 10.31 shares each

2. **Execution Result**
   - YES: 10 shares @ $0.48 (MATCHED) - Cost: $4.80
   - NO: 6 shares @ $0.49 (Partial) - Cost: $2.94
   - Total cost: $7.74
   - Hedge ratio: 60% (below 80% threshold)

3. **Position Added to Monitor**
   - Tracking: 4 YES shares excess
   - Watching for: YES bid > $0.48 OR NO ask < $0.49

4. **Market Movement (t=2 min)**
   - YES bid: $0.52 (+$0.04)
   - NO ask: $0.46 (-$0.03)

5. **Rebalancing Options Evaluated**
   - Option A: Sell 4 YES @ $0.52 → Profit: $0.16
   - Option B: Buy 4 NO @ $0.46 → Cost: $1.84, Final profit: $0.42

6. **Decision: Buy NO** (higher final profit)
   - Execute: Buy 4 NO @ $0.46
   - New position: 10 YES, 10 NO
   - Total cost: $7.74 + $1.84 = $9.58
   - Guaranteed return: $10.00
   - Final profit: $0.42

7. **Position Balanced**
   - Remove from monitor
   - Log success
   - Emit `POSITION_BALANCED` event

---

## Implementation Status

1. [x] Implement `ActivePosition` dataclass - **src/position_manager.py**
2. [x] Implement `TradeTelemetry` dataclass - **src/position_manager.py**
3. [x] Implement `ActivePositionManager` class - **src/position_manager.py**
4. [x] Add configuration parameters (`RebalancingConfig`) - **src/position_manager.py**
5. [x] Integrate with GabagoolStrategy - **src/strategies/gabagool.py**
6. [x] Add database schema for telemetry tracking - **src/persistence.py**
7. [x] Create comprehensive tests - **tests/test_position_manager.py**
8. [ ] Deploy and monitor

---

## Related Documents

- [STRATEGY_ARCHITECTURE.md](./STRATEGY_ARCHITECTURE.md) - Overall strategy architecture
- [strategy-rules.md](./strategy-rules.md) - Trading rules
- [IMPLEMENTATION_PLAN_2025-12-14.md](./IMPLEMENTATION_PLAN_2025-12-14.md) - Phase implementation plan
