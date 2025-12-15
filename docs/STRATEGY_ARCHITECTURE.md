# Gabagool Strategy Architecture

**Last Updated:** December 15, 2025
**Status:** PRODUCTION - All phases complete, blackout protection enabled

> **MAINTENANCE RULE:** This document MUST be updated with every code change to strategy files.
> PR checklist item: "Updated STRATEGY_ARCHITECTURE.md? [ ]"

---

## Quick Reference

| Component | File | Purpose |
|-----------|------|---------|
| Strategy Entry | `src/strategies/gabagool.py` | Main strategy orchestration |
| Order Execution | `src/client/polymarket.py` | API calls to Polymarket CLOB |
| Market Discovery | `src/monitoring/market_finder.py` | Find 15-min markets |
| Order Book Tracking | `src/monitoring/order_book.py` | Real-time price updates |
| Persistence | `src/persistence.py` | SQLite database |
| Dashboard | `src/dashboard.py` | Web UI (read-only) |
| WebSocket | `src/client/websocket.py` | Real-time market data |

---

## High-Level Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           GABAGOOL STRATEGY                                  │
│                                                                             │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐                  │
│  │ Market       │    │ Order Book   │    │ Opportunity  │                  │
│  │ Discovery    │───▶│ Tracking     │───▶│ Detection    │                  │
│  │ (15min)      │    │ (WebSocket)  │    │ (spread≥2¢)  │                  │
│  └──────────────┘    └──────────────┘    └──────┬───────┘                  │
│                                                  │                          │
│                                                  ▼                          │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                    EXECUTION DECISION                                 │  │
│  │  ┌─────────────┐   ┌─────────────┐   ┌─────────────┐                 │  │
│  │  │ Validate    │   │ Calculate   │   │ Check       │                 │  │
│  │  │ Opportunity │──▶│ Position    │──▶│ Liquidity   │                 │  │
│  │  │ (spread>0)  │   │ Sizes       │   │ Depth       │                 │  │
│  │  └─────────────┘   └─────────────┘   └──────┬──────┘                 │  │
│  └──────────────────────────────────────────────┼───────────────────────┘  │
│                                                  │                          │
│                                                  ▼                          │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                    ORDER EXECUTION                                    │  │
│  │                                                                       │  │
│  │  IF dry_run=True:                                                    │  │
│  │    → Log simulated trade                                             │  │
│  │    → Record to DB with dry_run=True                                  │  │
│  │    → Update dashboard                                                │  │
│  │                                                                       │  │
│  │  IF dry_run=False:                                                   │  │
│  │    → Calculate dynamic limit prices (market + slippage)              │  │
│  │    → Execute parallel orders (YES + NO)                              │  │
│  │    → Handle partial fills                                            │  │
│  │    → Record actual fills to DB                                       │  │
│  │    → Update dashboard                                                │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Component Details

### 1. Market Discovery (`market_finder.py`)

**Purpose:** Find active 15-minute up/down markets for BTC, ETH, SOL.

**Entry Point:** `MarketFinder.refresh()` (called every 30 seconds)

```
refresh()
    │
    ├──▶ For each asset (BTC, ETH, SOL):
    │       │
    │       ├──▶ Calculate current 15-min slot timestamp
    │       │       slot_ts = (current_ts // 900) * 900
    │       │
    │       ├──▶ Build slug: f"{asset.lower()}-updown-15m-{slot_ts}"
    │       │
    │       ├──▶ GET /markets/slug/{slug} from Gamma API
    │       │
    │       └──▶ Parse into Market15Min dataclass
    │               - condition_id
    │               - yes_token_id, no_token_id
    │               - start_time, end_time
    │               - slug (for Polymarket URL)
    │
    └──▶ Return List[Market15Min] (tradeable markets)
```

**Data Structures:**
```python
@dataclass
class Market15Min:
    condition_id: str      # Unique market identifier
    question: str          # "Bitcoin Up or Down - ..."
    asset: str             # "BTC", "ETH", "SOL"
    start_time: datetime
    end_time: datetime
    yes_token_id: str      # Token ID for YES outcome
    no_token_id: str       # Token ID for NO outcome
    slug: str              # For Polymarket URL construction
```

---

### 2. Order Book Tracking (`order_book.py`)

**Purpose:** Maintain real-time best bid/ask prices via WebSocket.

**Entry Point:** `OrderBookTracker.track_market(market)`

```
track_market(market)
    │
    ├──▶ Subscribe to WebSocket channel for market.condition_id
    │
    └──▶ Register callback: _handle_book_update()


_handle_book_update(message)
    │
    ├──▶ Parse book update (bids, asks arrays)
    │
    ├──▶ Update MarketState:
    │       - yes_best_bid, yes_best_ask
    │       - no_best_bid, no_best_ask
    │       - last_update timestamp
    │
    ├──▶ Calculate spread:
    │       spread = 1.0 - yes_best_ask - no_best_ask
    │
    ├──▶ IF spread >= min_spread_cents:
    │       │
    │       └──▶ Emit ArbitrageOpportunity to callback queue
    │
    └──▶ Emit state_change event (for dashboard updates)
```

**Data Structures:**
```python
@dataclass
class MarketState:
    market: Market15Min
    yes_best_bid: float
    yes_best_ask: float
    no_best_bid: float
    no_best_ask: float
    yes_price: float       # Alias for yes_best_ask (buy price)
    no_price: float        # Alias for no_best_ask (buy price)
    last_update: datetime
    is_stale: bool         # True if >10 seconds old

@dataclass
class ArbitrageOpportunity:
    market: Market15Min
    yes_price: float       # Best ask for YES
    no_price: float        # Best ask for NO
    spread_cents: float    # (1.0 - yes - no) * 100
    profit_percentage: float
    detected_at: datetime
```

---

### 3. Strategy Orchestration (`gabagool.py`)

**Purpose:** Coordinate all components, make trading decisions, execute trades.

**Entry Point:** `GabagoolStrategy.start()`

```
start()
    │
    ├──▶ Initialize components:
    │       - MarketFinder
    │       - OrderBookTracker
    │       - PolymarketClient
    │       - Database connection
    │
    ├──▶ Register callbacks:
    │       - on_opportunity → _queue_opportunity()
    │       - on_state_change → _on_market_state_change()
    │
    └──▶ Start main loops:
            - Market refresh loop (every 30s)
            - Opportunity processor loop
            - Market expiry checker loop
```

#### 3.1 Opportunity Processing

```
_process_opportunity_queue()  [ASYNC LOOP]
    │
    └──▶ While running:
            │
            ├──▶ Wait for opportunity from queue
            │
            ├──▶ Validate opportunity:
            │       - spread >= min_spread?
            │       - market not stale?
            │       - not already traded this market?
            │       - within daily exposure limit?
            │
            └──▶ IF valid:
                    │
                    └──▶ _execute_arbitrage(market, opportunity)
```

#### 3.2 Arbitrage Execution

```
_execute_arbitrage(market, opportunity)
    │
    ├──▶ Calculate position sizes:
    │       cost_per_pair = yes_price + no_price
    │       num_pairs = budget / cost_per_pair
    │       yes_shares = num_pairs  # Equal shares!
    │       no_shares = num_pairs   # Equal shares!
    │       yes_amount = yes_shares * yes_price
    │       no_amount = no_shares * no_price
    │
    ├──▶ Validate expected profit:
    │       expected_profit = num_pairs - (yes_amount + no_amount)
    │       IF expected_profit <= 0:
    │           REJECT (log warning, return None)
    │
    ├──▶ [PHASE 7] Capture liquidity snapshot:
    │       yes_liquidity = get_depth_at_price(yes_token, yes_limit)
    │       no_liquidity = get_depth_at_price(no_token, no_limit)
    │
    ├──▶ IF dry_run=True:
    │       │
    │       ├──▶ Log "DRY RUN: Would execute trade"
    │       │
    │       └──▶ Record to database with dry_run=True
    │
    └──▶ IF dry_run=False:
            │
            └──▶ _client.execute_arbitrage_trade(
                    yes_token_id,
                    no_token_id,
                    yes_amount,
                    no_amount,
                    yes_price,    # ← MUST come from opportunity
                    no_price,     # ← MUST come from opportunity
                    slippage,
                )
```

---

### 4. Order Execution (`polymarket.py`)

**Purpose:** Execute orders on Polymarket CLOB API.

#### ⚠️ CRITICAL BUG IDENTIFIED (2025-12-14)

**The $0.53 Pricing Bug Root Cause:**

The function `execute_dual_leg_order_parallel()` (line 896) has a subtle but critical bug:

1. **Lines 959-960** correctly fetch prices from order book:
   ```python
   yes_price = float(yes_asks[0].get("price", 0.5))
   no_price = float(no_asks[0].get("price", 0.5))
   ```

2. **BUT Lines 1022-1024** in `place_order_sync()` re-fetches the price:
   ```python
   try:
       price = self.get_price(token_id, "buy")  # ← CALLS API AGAIN!
   except Exception:
       price = price_hint  # Falls back to hint
   ```

3. **Then applies +3¢ slippage** (line 1030):
   ```python
   limit_price_d = min(price_d + Decimal("0.03"), Decimal("0.99"))
   ```

**Why both legs get $0.53:**
- `get_price(yes_token, "buy")` returns best ask on YES book (e.g., $0.50)
- `get_price(no_token, "buy")` returns best ask on NO book (e.g., $0.50)
- Both get +3¢ slippage = both become $0.53

**The Real Problem:**
For arbitrage, we want to buy YES at YES's best ask (~$0.50) and NO at NO's best ask (~$0.50).
But if both order books have similar best asks, the limit prices end up identical.
This causes one leg to fill (aggressive enough) while the other sits on the book (not aggressive enough).

**The Fix (Phase 1):**
1. Pass actual target prices from opportunity detector through to order placement
2. Remove the `get_price()` call in `place_order_sync()` - use the provided price directly
3. **CRITICAL: NO SLIPPAGE** - Use exact prices as limit prices
   - Old approach: +3¢ slippage per leg = 6¢ total on 2¢ opportunity = LOSS
   - New approach: Exact prices, if we can't fill at opportunity price, don't trade
   - Goal is **precision execution**, not guaranteed fills
4. Use FOK (Fill-or-Kill) orders for atomicity

---

#### ACTUAL Current Code Path (BUGGY)

```
gabagool.py:_execute_arbitrage(market, opportunity)
    │
    │   ← opportunity.yes_price and opportunity.no_price are CORRECT here
    │   ← These came from real-time order book in order_book.py
    │
    ├──▶ Calculate amounts:
    │       yes_amount = budget * (yes_price / (yes_price + no_price))
    │       no_amount = budget * (no_price / (yes_price + no_price))
    │
    └──▶ Call: client.execute_dual_leg_order_parallel(
            yes_token_id,
            no_token_id,
            yes_amount_usd=yes_amount,    # Amount only, NOT price!
            no_amount_usd=no_amount,      # Amount only, NOT price!
            ...
        )
            │
            │   ← PROBLEM: Prices are NOT passed!
            │
            ├──▶ polymarket.py:execute_dual_leg_order_parallel() line 896
            │       │
            │       ├──▶ Fetch order books (lines 939-943):
            │       │       yes_book = self.get_order_book(yes_token_id)
            │       │       no_book = self.get_order_book(no_token_id)
            │       │
            │       ├──▶ Extract prices from books (lines 959-960):
            │       │       yes_price = float(yes_asks[0].get("price", 0.5))
            │       │       no_price = float(no_asks[0].get("price", 0.5))
            │       │
            │       │   ← These MIGHT be different from opportunity prices!
            │       │   ← Book could have changed since opportunity was detected
            │       │
            │       └──▶ Call place_order_sync() for each leg (line 1014):
            │               │
            │               ├──▶ ANOTHER API call (line 1022):
            │               │       price = self.get_price(token_id, "buy")
            │               │
            │               │   ← This call might return ~$0.50 for BOTH tokens
            │               │   ← Because it fetches current best ask, not opportunity price
            │               │
            │               └──▶ Add slippage (line 1030):
            │                       limit_price = price + 0.03
            │                       → Both legs get $0.53!

Result: Both YES and NO orders placed at $0.53 limit
→ YES order fills (market ask was ~$0.50)
→ NO order sits on book (market ask was ~$0.50, our $0.53 limit is on wrong side)
→ We now hold unhedged YES position = directional bet, not arbitrage!
```

---

**Entry Point:** `PolymarketClient.execute_dual_leg_order_parallel()` (FIXED - IMPLEMENTED 2025-12-14)

```
execute_dual_leg_order_parallel(yes_token, no_token, yes_amt, no_amt, yes_price, no_price, ...)
    │
    ├──▶ Validate arbitrage is still profitable:
    │       total_cost = yes_price + no_price
    │       if total_cost >= 1.0:
    │           REJECT - "Arbitrage invalidated"
    │
    ├──▶ Use EXACT prices from parameters - NO slippage!
    │       yes_limit = yes_price  # EXACT, no +0.03
    │       no_limit = no_price    # EXACT, no +0.03
    │
    │       ⚠️ CRITICAL: NO slippage added - exact prices preserve arbitrage profit
    │       ⚠️ If we can't fill at these prices, we don't take the trade
    │
    ├──▶ Calculate shares from amounts:
    │       yes_shares = round(yes_amt / yes_limit, 2)
    │       no_shares = round(no_amt / no_limit, 2)
    │
    ├──▶ Execute orders in PARALLEL with FOK:
    │       │
    │       └──▶ asyncio.gather(
    │               place_order_sync(yes_token, yes_amt, "YES", yes_price),
    │               place_order_sync(no_token, no_amt, "NO", no_price),
    │           )
    │
    │       NOTE: FOK (Fill-or-Kill) ensures atomicity:
    │       - Either fills completely at our price or not at all
    │       - No partial fills sitting on the order book
    │       - If price moved, we simply don't fill (that's OK)
    │
    ├──▶ Analyze results:
    │       │
    │       ├──▶ Both MATCHED → Success, return full result
    │       │
    │       ├──▶ One MATCHED, one didn't fill:
    │       │       │
    │       │       └──▶ Return partial result (record the fill!)
    │       │           With FOK, the unfilled leg was auto-cancelled
    │       │
    │       └──▶ Neither filled → Return failure (no cleanup needed with FOK)
    │
    └──▶ Return DualLegResult with actual fill data


place_order_sync(token_id, amount_usd, label, limit_price)
    │
    │   CRITICAL: Uses exact limit_price - NO slippage, NO re-fetching
    │
    ├──▶ Calculate shares from amount and EXACT limit price:
    │       shares = amount_usd / limit_price
    │
    ├──▶ Create OrderArgs:
    │       OrderArgs(
    │           token_id=token_id,
    │           price=limit_price,  # ← EXACT from parameter
    │           size=shares,
    │           side="BUY",
    │       )
    │
    ├──▶ Sign order:
    │       signed_order = client.create_order(order_args)
    │
    ├──▶ POST order with FOK (Fill-or-Kill):
    │       result = client.post_order(signed_order, orderType=OrderType.FOK)
    │
    │       FOK ensures: fill completely at our price, or not at all
    │
    └──▶ Return OrderResult with status (MATCHED/FAILED)
```

**Data Structures:**
```python
@dataclass
class DualLegResult:
    success: bool

    # Intended
    intended_yes_shares: float
    intended_no_shares: float

    # Actual (may differ due to partial fills)
    actual_yes_shares: float = 0.0
    actual_no_shares: float = 0.0
    actual_yes_cost: float = 0.0
    actual_no_cost: float = 0.0

    # Status
    yes_status: str = "UNKNOWN"  # MATCHED, LIVE, CANCELLED, FAILED
    no_status: str = "UNKNOWN"

    # Metrics
    hedge_ratio: float = 0.0
    error: str = None
```

---

### 5. Persistence (`persistence.py`)

**Purpose:** SQLite storage for trades, markets, logs.

**Key Tables:**

```sql
trades (
    id TEXT PRIMARY KEY,
    created_at TIMESTAMP,
    resolved_at TIMESTAMP,
    asset TEXT,
    market_slug TEXT,
    condition_id TEXT,
    yes_price REAL,
    no_price REAL,
    yes_cost REAL,
    no_cost REAL,
    yes_shares REAL,           -- [PHASE 2] New
    no_shares REAL,            -- [PHASE 2] New
    hedge_ratio REAL,          -- [PHASE 2] New
    spread REAL,
    expected_profit REAL,
    actual_profit REAL,
    status TEXT,               -- 'pending', 'win', 'loss'
    execution_status TEXT,     -- [PHASE 3] 'full', 'partial', 'failed'
    yes_liquidity_at_price REAL,  -- [PHASE 7] New
    no_liquidity_at_price REAL,   -- [PHASE 7] New
    yes_book_depth_total REAL,    -- [PHASE 7] New
    no_book_depth_total REAL,     -- [PHASE 7] New
    dry_run BOOLEAN
)

-- Position Settlement Queue (NEW 2025-12-14)
-- Tracks positions awaiting claim after market resolution
-- Survives bot restarts - positions loaded on startup
settlement_queue (
    id INTEGER PRIMARY KEY,
    created_at TIMESTAMP,
    trade_id TEXT NOT NULL,        -- Links to trades table
    condition_id TEXT NOT NULL,    -- Market identifier
    token_id TEXT NOT NULL,        -- YES or NO token
    side TEXT NOT NULL,            -- "YES" or "NO"
    asset TEXT NOT NULL,           -- BTC, ETH, SOL
    shares REAL NOT NULL,
    entry_price REAL NOT NULL,
    entry_cost REAL NOT NULL,
    market_end_time TIMESTAMP NOT NULL,
    claimed BOOLEAN DEFAULT 0,
    claimed_at TIMESTAMP,
    claim_proceeds REAL,           -- USD received when claimed
    claim_profit REAL,             -- proceeds - entry_cost
    claim_attempts INTEGER DEFAULT 0,
    last_claim_error TEXT,
    UNIQUE(trade_id, token_id)
)
```

---

### 6. Dashboard (`dashboard.py`)

**Purpose:** Web UI for monitoring. READ-ONLY consumer of trade data.

**Architecture:**
```
Strategy ──(events)──▶ Dashboard ──(SSE)──▶ Browser

Dashboard does NOT:
  - Write to database
  - Make trading decisions
  - Own trade state

Dashboard DOES:
  - Subscribe to trade events
  - Format data for display
  - Broadcast to connected browsers via SSE
```

**Active Markets Display:**
- Only shows markets within 15-minute trading window (≤900 seconds remaining)
- Markets with >15 minutes remaining are filtered out
- Container height limited to ~4 rows (180px max-height)
- Client-side countdown timer for smooth updates

**Status Banners (Priority Order):**
1. **BLACKOUT** (purple) - Server restart blackout window active
2. **CIRCUIT_BREAKER** (red) - Daily loss limit hit
3. **DRY_RUN** (yellow) - Paper trading mode

---

### 7. Server Restart Blackout Protection

**Purpose:** Prevent trades from being interrupted by daily server restart at 5:15 AM CST.

**Window:** 5:00 AM - 5:29 AM CST (configurable)

**Architecture:**
```
_blackout_checker_loop() [BACKGROUND TASK - every 60s]
    │
    ├──▶ _check_blackout_window()
    │       │
    │       ├──▶ Get current time in configured timezone
    │       │
    │       └──▶ Return True if within start/end window
    │
    └──▶ Set _in_blackout flag (atomic read by trade path)


_is_trading_disabled()
    │
    ├──▶ Check _in_blackout flag (read-only, no performance impact)
    │
    ├──▶ Check circuit_breaker_hit flag
    │
    └──▶ Return True if any condition met
```

**Key Design Decisions:**
- Background task updates flag every 60 seconds (not on every trade)
- Trade execution path only reads the flag (no time calculations)
- Uses `zoneinfo` for proper timezone handling (CST/CDT aware)
- Blackout has highest priority over other trading modes

**Trading Mode Priority:**
```
BLACKOUT > CIRCUIT_BREAKER > DRY_RUN > LIVE
```

**Relevant Files:**
- `src/config.py` - Blackout configuration (GabagoolConfig)
- `src/strategies/gabagool.py`:
  - `_check_blackout_window()` - Time check logic
  - `_blackout_checker_loop()` - Background task
  - `_is_trading_disabled()` - Includes blackout check
  - `_get_trading_mode()` - Returns "BLACKOUT" when in window
- `tests/test_blackout.py` - Regression tests

---

## Error Handling Paths

### Scenario: One Leg Fills, Other Doesn't

```
execute_arbitrage_trade()
    │
    ├──▶ Place YES order → MATCHED (filled)
    ├──▶ Place NO order → LIVE (on book, not filled)
    │
    ├──▶ Detect mismatch:
    │       yes_status == "MATCHED" and no_status == "LIVE"
    │
    ├──▶ Cancel the LIVE order:
    │       await cancel_order(no_order_id)
    │
    ├──▶ DO NOT try to unwind MATCHED order (impossible!)
    │
    └──▶ Return partial result:
            DualLegResult(
                success=False,
                actual_yes_shares=yes_filled,
                actual_no_shares=0,
                hedge_ratio=0,
                error="Partial fill: YES matched, NO cancelled"
            )
```

### Scenario: WebSocket Disconnection

```
_on_ws_disconnect()
    │
    ├──▶ Mark all market states as STALE
    │
    ├──▶ Stop processing opportunities
    │
    ├──▶ Attempt reconnection with backoff
    │
    └──▶ On reconnect: resubscribe to all markets
```

### Scenario: API Rate Limit

```
_place_order() raises RateLimitError
    │
    ├──▶ Log warning with retry-after header
    │
    ├──▶ Wait for retry-after duration
    │
    └──▶ Retry order (max 3 attempts)
```

---

## Configuration Reference

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| dry_run | GABAGOOL_DRY_RUN | false | Skip actual order execution |
| min_spread | GABAGOOL_MIN_SPREAD | 0.02 | Minimum spread in dollars |
| max_trade_size | GABAGOOL_MAX_TRADE_SIZE | 25.0 | Max USD per trade |
| max_daily_exposure | GABAGOOL_MAX_DAILY_EXPOSURE | 90.0 | Max daily USD exposure |
| max_slippage | GABAGOOL_MAX_SLIPPAGE | 0.02 | Price slippage buffer |
| markets | GABAGOOL_MARKETS | BTC,ETH,SOL | Assets to trade |

### Blackout Window Configuration

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| blackout_enabled | GABAGOOL_BLACKOUT_ENABLED | true | Enable server restart blackout |
| blackout_start_hour | GABAGOOL_BLACKOUT_START_HOUR | 5 | Start hour (24h format) |
| blackout_start_minute | GABAGOOL_BLACKOUT_START_MINUTE | 0 | Start minute |
| blackout_end_hour | GABAGOOL_BLACKOUT_END_HOUR | 5 | End hour (24h format) |
| blackout_end_minute | GABAGOOL_BLACKOUT_END_MINUTE | 29 | End minute |
| blackout_timezone | GABAGOOL_BLACKOUT_TIMEZONE | America/Chicago | Timezone for window |

---

## Audit Checkpoints

When reviewing code changes, verify:

1. **Prices are NEVER hardcoded**
   - All prices must flow from market data → opportunity → execution
   - Search for: `price = 0.` patterns

2. **All fills are recorded**
   - Partial fills MUST be saved to database
   - Check: Does code path skip recording on partial fill?

3. **Dry run is honored**
   - `if dry_run:` must skip ALL API calls
   - Check: Are there API calls outside the dry_run check?

4. **Liquidity is captured**
   - Every trade record should include liquidity snapshot
   - Check: Is liquidity captured BEFORE execution?

5. **Dashboard is read-only**
   - Dashboard should NEVER call `save_trade()` directly
   - Check: Is dashboard importing persistence functions?

---

## Strategy Rules Summary (from strategy-rules.md)

### Arbitrage Strategy Rules

| Rule | Description |
|------|-------------|
| **Entry Condition** | YES_price + NO_price < $0.98 (2¢ minimum spread) |
| **Position Sizing** | EQUAL SHARES on both sides: `num_pairs = budget / (yes_price + no_price)` |
| **Max Per Trade** | 25% of available balance (cap: $25) |
| **Max Per Window** | $50 per 15-minute market |
| **Exit** | Hold to resolution - guaranteed payout |
| **Slippage** | 0¢ (use exact opportunity prices) |
| **Order Type** | FOK (Fill-or-Kill) for atomicity |

### Near-Resolution Strategy Rules (DISABLED in current config)

| Rule | Description |
|------|-------------|
| **Entry Condition** | ≤60 seconds remaining, price $0.94-$0.975 |
| **Position** | Single-leg only (buy high-confidence side) |
| **Size** | Fixed $10 per trade |
| **Isolation** | BLOCKED on markets with existing arb positions |

### Directional Strategy Rules (DISABLED)

| Rule | Description |
|------|-------------|
| **Entry** | Price < $0.25, time > 80% remaining |
| **Size** | 1/3 of arb trade size |
| **Exit** | Take profit at $0.45, stop loss at $0.11 |

### Position Rebalancing Rules (NEW - 2025-12-14)

| Rule | Description |
|------|-------------|
| **Trigger** | Hedge ratio < 80% after partial fill |
| **Sell Excess** | If larger position's price rises above entry, sell excess to lock in profit |
| **Buy Deficit** | If smaller position's price drops below entry, buy more to complete hedge |
| **Min Profit** | Require ≥$0.02/share profit to execute rebalance |
| **Time Limit** | Don't rebalance in last 60 seconds before resolution |
| **Preference** | Prefer selling (capital efficient) over buying when both profitable |
| **Fallback** | If no opportunity, hold to resolution |

**Rebalancing Decision Flow:**
```
Partial Fill Detected (hedge_ratio < 80%)
    │
    ├──▶ Add to RebalancingMonitor
    │
    └──▶ Every 5 seconds, check:
            │
            ├──▶ Can sell excess at profit?
            │       → Sell to lock in gain, balance position
            │
            ├──▶ Can buy deficit cheaply?
            │       → Buy to complete hedge, lock in arb profit
            │
            └──▶ Neither profitable?
                    → Hold to resolution

See [REBALANCING_STRATEGY.md](./REBALANCING_STRATEGY.md) for full details.
```

---

## Discrepancies: Strategy Rules vs Implementation

### ✅ IMPLEMENTED CORRECTLY

| Rule | Status | Notes |
|------|--------|-------|
| 2¢ minimum spread | ✅ | `GABAGOOL_MIN_SPREAD=0.02` |
| Equal shares calculation | ✅ | `gabagool.py` calculates equal pairs |
| Prices flow from opportunity | ✅ | Fixed 2025-12-14, prices passed through |
| FOK orders for atomicity | ✅ | Fixed 2025-12-14, using `OrderType.FOK` |
| Zero slippage | ✅ | Fixed 2025-12-14, exact prices used |
| Pre-validation total < $1.00 | ✅ | Added in `execute_dual_leg_order_parallel()` |

### ⚠️ DISCREPANCIES / NOT YET IMPLEMENTED

| Rule | Expected | Actual | Impact |
|------|----------|--------|--------|
| **Partial fill recording** | Record all fills | Partial fills treated as failures, not recorded | Can't audit actual positions |
| **Dashboard read-only** | Strategy owns persistence | Dashboard still owns `save_trade()` | Wrong architecture |
| **Liquidity snapshot** | Capture before every trade | Captured but not saved to DB | No liquidity analysis |
| **Slippage config** | `GABAGOOL_MAX_SLIPPAGE=0.02` | Code uses 0 slippage (correct!) | Config is misleading |
| ~~**Automatic unwind**~~ | ~~Sell YES if NO fails~~ | ✅ Fixed: No unwind attempts, positions held | N/A (positions resolve naturally) |
| **Max per window** | $50 per 15-min market | Not enforced in code | Could over-trade |

### 🔴 CRITICAL GAPS

1. ~~**Trade Persistence** (Phase 2 not implemented)~~ ✅ FIXED
   - Rule: Strategy should record trades directly to DB
   - ~~Actual: Dashboard calls `save_trade()`, strategy doesn't~~
   - **Now**: Strategy calls `_record_trade()` with full execution details

2. ~~**Partial Fill Handling** (Phase 3 not implemented)~~ ✅ FIXED
   - Rule: Record partial fills with hedge_ratio
   - ~~Actual: Partial fills treated as "failures", not recorded~~
   - **Now**: All fills recorded with execution_status, hedge_ratio, order statuses

3. ~~**Unwind Logic** (Phase 4 not implemented)~~ ✅ FIXED
   - Rule: Cancel LIVE orders, don't try to unwind MATCHED
   - ~~Actual: Bot tries to unwind MATCHED orders → 400 error~~
   - **Now**: Only LIVE orders cancelled. MATCHED positions held until resolution.

4. **Event System** (Phase 6 partially implemented)
   - Rule: Strategy emits events, dashboard subscribes
   - Actual: Dashboard add_trade() no longer writes to DB, but events not implemented
   - Impact: Dashboard still called directly from strategy

---

## Implementation Status

### Phase 1: Fix Dynamic Pricing ✅ COMPLETE
- [x] Pass prices from gabagool.py to polymarket.py
- [x] Remove `get_price()` call in `place_order_sync()`
- [x] Remove 3¢ slippage addition
- [x] Use exact opportunity prices
- [x] Add pre-validation (total < $1.00)
- [x] Switch to FOK orders

### Phase 2: Move Trade Persistence to Strategy ✅ COMPLETE (2025-12-14)
- [x] Add `_record_trade()` method to GabagoolStrategy
- [x] Add yes_shares, no_shares, hedge_ratio to DB schema
- [x] Add execution_status, yes_order_status, no_order_status fields
- [x] Add schema migration for existing databases
- [x] Remove `save_trade()` DB calls from dashboard
- [x] Dashboard is now READ-ONLY for trade data
- [x] Record partial fills with proper hedge_ratio
- [x] Create regression tests (test_phase2_persistence.py)

### Phase 3: Record All Fills ✅ COMPLETE (merged into Phase 2)
- [x] Partial fills now recorded via _record_trade()
- [x] execution_status tracks: 'full_fill', 'partial_fill', 'one_leg_only', 'failed'
- [x] hedge_ratio calculated and stored for all fills
- [x] Order statuses (MATCHED/LIVE/FAILED) recorded per leg

### Phase 4: Fix Unwind Logic ✅ COMPLETE (2025-12-14)
- [x] Cancel LIVE orders only (not MATCHED)
- [x] Return partial result for strategy to record
- [x] No more 400 errors from unwind attempts
- [x] Removed sell-back logic that was creating new trades
- [x] Positions held until market resolution (better than guaranteed loss)
- [x] Created regression tests (test_phase4_unwind.py)

### Phase 5: Pre-Trade Liquidity Check ✅ COMPLETE (2025-12-14)
- [x] Liquidity check exists in `execute_dual_leg_order_parallel()`
- [x] Configurable buffer via `max_liquidity_consumption_pct` (default 50% = 200% buffer)
- [x] Liquidity fields added to DB schema (yes_liquidity_at_price, etc.)
- [x] Liquidity data captured before execution (`pre_fill_yes_depth`, `pre_fill_no_depth`)
- [x] Liquidity data returned with all API results (success, rejection, partial fill)
- [x] Strategy passes liquidity data to `_record_trade()` and database
- [x] Created regression tests (test_phase5_liquidity.py)

### Phase 6: Dashboard Read-Only Mode ✅ COMPLETE (2025-12-14)
- [x] Dashboard add_trade() no longer writes to DB
- [x] Strategy owns persistence via _record_trade()
- [x] Event emitter implemented (`src/events.py`)
- [x] Strategy emits TRADE_CREATED events after recording trades
- [x] Dashboard subscribes to events via `_on_trade_event()` handler
- [x] Dashboard resolve_trade() DB calls removed (truly read-only)
- [x] Created regression tests (test_phase6_events.py)

### Phase 7: Record Liquidity Depth ✅ COMPLETE (2025-12-14)
- [x] Liquidity fields added to trades table schema
- [x] Capture snapshot before execution (`pre_fill_yes_depth`, `pre_fill_no_depth`)
- [x] Save with trade record (via `yes_book_depth_total`, `no_book_depth_total` in DB)
- Note: Combined with Phase 5 implementation

### Phase 8-10: Testing & Audit ✅ COMPLETE (2025-12-14)
- [x] test_phase2_persistence.py created
- [x] End-to-end test scenarios documented
- [x] test_phase8_pricing_logic.py - Magic number detection in pricing
- [x] test_phase8_execution_flow.py - E2E execution path tests
- [x] test_phase8_invariants.py - Business logic invariant tests
- [x] scripts/audit_magic_numbers.py - Codebase audit script
- [ ] Pre-commit hooks (optional, manual audit available)

### Phase 11: Position Rebalancing ⚠️ DESIGNED (2025-12-14)
- [x] REBALANCING_STRATEGY.md - Complete design document
- [x] test_e2e_scenarios.py - Comprehensive E2E test scenarios
- [x] test_rebalancing.py - Rebalancing logic tests
- [ ] Implement `UnbalancedPosition` dataclass
- [ ] Implement `RebalancingMonitor` class
- [ ] Add rebalancing configuration to GabagoolConfig
- [ ] Integrate with GabagoolStrategy
- [ ] Add database schema for rebalancing tracking
- [ ] Add rebalancing events

**Key Rebalancing Rules:**
- Trigger: hedge_ratio < 80% after partial fill
- Sell excess if price rises above entry
- Buy deficit if price drops below entry
- Minimum $0.02/share profit to execute
- No rebalancing in last 60 seconds before resolution

### Phase 12: Position Settlement Persistence ✅ COMPLETE (2025-12-14)
- [x] Add `settlement_queue` table to database schema
- [x] Implement `add_to_settlement_queue()` - save position on trade execution
- [x] Implement `get_unclaimed_positions()` - query all unclaimed positions
- [x] Implement `get_claimable_positions()` - query positions ready to claim (market ended + wait)
- [x] Implement `mark_position_claimed()` - update on successful claim
- [x] Implement `record_claim_attempt()` - track failed attempts
- [x] Implement `get_settlement_stats()` - settlement queue statistics
- [x] Modify `_track_position()` to save to database (async)
- [x] Add `_load_unclaimed_positions()` - restore positions on startup
- [x] Update `_check_settlement()` to query database for claimable positions
- [x] Extract `_attempt_claim_position()` helper for cleaner claim logic

**Settlement Flow:**
```
Trade Executes → _track_position() → Saves to settlement_queue
                                    ↓
Bot Restarts → start() → _load_unclaimed_positions() → Loads from DB
                                    ↓
Every 60s → _check_settlement() → Queries DB for claimable positions
                                    ↓
Market Ended + 10min Wait → _attempt_claim_position() → Sell at $0.99
                                    ↓
Success → mark_position_claimed() → Updates DB with proceeds/profit
Failure → record_claim_attempt() → Tracks error, will retry next cycle
```

**Note:** Settlement requires `dry_run=False`. Claim workaround sells at $0.99 (py-clob-client has no native redeem API per GitHub issue #117).

### Phase 13: Server Restart Blackout Protection ✅ COMPLETE (2025-12-15)
- [x] Add blackout window configuration to GabagoolConfig
- [x] Add `_in_blackout` flag to GabagoolStrategy
- [x] Implement `_check_blackout_window()` - time check logic with zoneinfo
- [x] Implement `_blackout_checker_loop()` - background task (every 60s)
- [x] Update `_is_trading_disabled()` to include blackout check
- [x] Update `_get_trading_mode()` to return "BLACKOUT" when in window
- [x] Add purple "BLACKOUT" banner to dashboard
- [x] Start/stop blackout checker in strategy start()/stop()
- [x] Create regression tests (test_blackout.py)

**Design Decision:** Background task updates flag every 60 seconds rather than calculating time on every trade execution. This optimizes performance by keeping the hot trade path free of time calculations.

### Phase 14: Dashboard Active Markets Filter ✅ COMPLETE (2025-12-15)
- [x] Filter active markets to only show those within 15-minute window
- [x] Markets with >900 seconds remaining are hidden
- [x] Shrink container max-height from 430px to 180px (~4 rows)
- [x] Handle case where all markets are filtered out

### Phase 15: Trade Reconciliation & Observability ✅ COMPLETE (2025-12-15)
- [x] Create `scripts/reconcile_trades.py` - standalone reconciliation script
- [x] Fetches trades from Polymarket Data API (`https://data-api.polymarket.com/trades`)
- [x] Compares with local DB (trades + settlement_queue tables)
- [x] Identifies untracked positions (on Polymarket but not in local DB)
- [x] `--fix` flag adds untracked positions to settlement queue
- [x] `--json` flag for programmatic output
- [x] `--days N` parameter to control lookback window

**Reconciliation Script Usage:**
```bash
# Show discrepancies (read-only)
python scripts/reconcile_trades.py

# Fix by adding untracked positions to settlement queue
python scripts/reconcile_trades.py --fix

# Output as JSON
python scripts/reconcile_trades.py --json

# Check last 14 days
python scripts/reconcile_trades.py --days 14
```

### Phase 16: Dashboard Observability Widgets ✅ COMPLETE (2025-12-15)
- [x] Add Historical Positions panel showing settlement queue data
- [x] Add `/dashboard/positions` endpoint returning settlement history
- [x] Add `get_settlement_history()` method to persistence.py
- [x] Add Reconciliation Status panel with:
  - RECON status indicator in header (green/yellow/red)
  - Untracked positions count and value
  - Polymarket trades vs local tracked count
  - Status message with color-coded warnings
  - REFRESH button for manual reconciliation checks
- [x] Add `/dashboard/reconciliation` endpoint that:
  - Fetches trades from Polymarket Data API (async httpx)
  - Compares with local trades and settlement queue
  - Returns discrepancy summary as JSON

**Dashboard Endpoints:**
| Endpoint | Purpose |
|----------|---------|
| `/dashboard/positions` | Historical positions from settlement queue |
| `/dashboard/reconciliation` | Real-time reconciliation status |
| `/dashboard/state` | Full dashboard state (markets, stats, trades) |
| `/dashboard/pnl-history` | P&L chart data |

### Phase 17: Partial Fill Detection Fix ✅ COMPLETE (2025-12-15)
- [x] Fixed `place_order_sync()` to catch exceptions and return error dict
- [x] Previously: exceptions would bubble up, `asyncio.gather` would raise, outer handler returned `partial_fill=False`
- [x] Now: exceptions caught, returns `{"status": "EXCEPTION", "error": ..., "size_matched": 0}`
- [x] Allows parallel execution to detect when one leg fills and other fails
- [x] Partial fills now properly recorded to database and settlement queue

### Phase 18: Configurable Position Sizing ✅ COMPLETE (2025-12-15)
- [x] Added `min_trade_size_usd` config parameter (default $3.00)
- [x] Configurable via `GABAGOOL_MIN_TRADE_SIZE` environment variable
- [x] Replaced hardcoded `min_trade = 1.0` in `_adjust_for_liquidity()`
- [x] Added minimum budget enforcement in `_process_opportunity()`
- [x] Skip trades when budget < min_trade_size_usd * 2 (need funds for both legs)
- [x] Matches gabagool22's successful pattern: $3-8 per trade
- [x] Regression tests in `tests/test_phase1_position_sizing.py`

**Production Config (already set):**
```env
GABAGOOL_MIN_TRADE_SIZE=3.0   # New: minimum per leg
GABAGOOL_MAX_TRADE_SIZE=5.0   # Already in production
GABAGOOL_MAX_SLIPPAGE=0.0     # Already in production (zero slippage)
```

### Phase 19: Gradual Position Building ✅ COMPLETE (2025-12-15)
- [x] Added `gradual_entry_enabled` config (default: false)
- [x] Added `gradual_entry_tranches` config (default: 3)
- [x] Added `gradual_entry_delay_seconds` config (default: 30.0)
- [x] Added `gradual_entry_min_spread_cents` config (default: 3.0)
- [x] Implemented `_execute_gradual_entry()` method in GabagoolStrategy
- [x] Split trades into multiple tranches with configurable delays
- [x] Fallback to single entry when tranche size < min_trade_size
- [x] Safety checks: market tradeable, trading enabled before each tranche
- [x] Aggregated result tracking across all tranches
- [x] Regression tests in `tests/test_phase2_gradual_entry.py`

**How Gradual Entry Works:**
```
SINGLE ENTRY (default):
1. Opportunity detected: $5 spread on BTC market
2. Execute full position: $5 YES + $5 NO in one dual-leg order
3. Done

GRADUAL ENTRY (when enabled for spreads >= 3¢):
1. Opportunity detected: $5 spread on BTC market
2. Split into 3 tranches: $1.67 per tranche
3. Tranche 1: Execute $1.67 YES + $1.67 NO
4. Wait 30 seconds
5. Tranche 2: Execute $1.67 YES + $1.67 NO (if still tradeable)
6. Wait 30 seconds
7. Tranche 3: Execute $1.67 YES + $1.67 NO (if still tradeable)
8. Return aggregated result
```

**Config (disabled by default):**
```env
GABAGOOL_GRADUAL_ENTRY_ENABLED=false
GABAGOOL_GRADUAL_ENTRY_TRANCHES=3
GABAGOOL_GRADUAL_ENTRY_DELAY=30.0
GABAGOOL_GRADUAL_ENTRY_MIN_SPREAD=3.0
```

**Root Cause of Untracked Positions:**
```
BEFORE FIX:
1. Bot sends YES order → FILLS ✅
2. Bot sends NO order → EXCEPTION raised by py-clob-client
3. asyncio.gather raises → outer try/except catches
4. Returns success=False, partial_fill=False → NO database record
5. BUT: YES order already executed on-chain!

AFTER FIX:
1. Bot sends YES order → FILLS ✅
2. Bot sends NO order → Exception caught in place_order_sync()
3. Returns {"status": "EXCEPTION", ...} instead of raising
4. Parallel execution completes, detects partial fill
5. Records partial fill to database AND settlement queue
```

---

## Change Log

| Date | Author | Changes |
|------|--------|---------|
| 2025-12-14 | Claude | Initial document, audit of execution bugs |
| 2025-12-14 | Claude | **Phase 1 COMPLETE**: Prices flow from opportunity to execution with ZERO slippage. Changed from GTC to FOK orders for atomicity. |
| 2025-12-14 | Claude | Added strategy rules summary, discrepancies section, implementation status checklist |
| 2025-12-14 | Claude | **Phase 2 COMPLETE**: Strategy owns persistence via `_record_trade()`. Dashboard is READ-ONLY. Schema migration adds new fields. Partial fills now recorded with hedge_ratio and execution_status. |
| 2025-12-14 | Claude | **Phase 3 COMPLETE**: Merged into Phase 2. All fills recorded with proper status tracking. |
| 2025-12-14 | Claude | **Phase 4 COMPLETE**: Removed unwind logic that was creating new trades. LIVE orders cancelled, MATCHED positions held. No more 400 errors. |
| 2025-12-14 | Claude | **Phase 5 COMPLETE**: Pre-trade liquidity check with configurable buffer (default 200%). Liquidity data captured before execution and saved with trade records. Also completes Phase 7 (record liquidity depth). |
| 2025-12-14 | Claude | **Phase 6 COMPLETE**: Dashboard read-only mode with event emitter. Created `src/events.py` with TradeEventEmitter. Strategy emits events, dashboard subscribes. Removed all DB writes from dashboard. |
| 2025-12-14 | Claude | **Phase 8-10 COMPLETE**: Comprehensive regression test suite. Created test_phase8_pricing_logic.py (magic number detection), test_phase8_execution_flow.py (E2E tests), test_phase8_invariants.py (business invariants). Added scripts/audit_magic_numbers.py for codebase auditing. |
| 2025-12-14 | Claude | **Phase 11 DESIGNED**: Position rebalancing strategy for partial fills. Created REBALANCING_STRATEGY.md, test_e2e_scenarios.py (comprehensive E2E tests), test_rebalancing.py (rebalancing logic tests). Two strategies: sell excess at profit OR buy deficit cheaply. 80% hedge threshold, $0.02/share min profit. |
| 2025-12-14 | Claude | **Phase 12 COMPLETE**: Position persistence for auto-settlement. Added `settlement_queue` table. Positions survive bot restarts. `_track_position()` saves to DB, `_load_unclaimed_positions()` restores on startup, `_check_settlement()` queries DB for claimable positions. |
| 2025-12-15 | Claude | **Server Restart Blackout Protection**: Added 5:00-5:29 AM CST blackout window to prevent trades during daily server restart at 5:15 AM. Background task updates `_in_blackout` flag every 60 seconds. Trading mode priority: BLACKOUT > CIRCUIT_BREAKER > DRY_RUN > LIVE. Purple "BLACKOUT" banner on dashboard. |
| 2025-12-15 | Claude | **Dashboard Active Markets Filter**: Markets display now filtered to only show those within 15-minute window (≤900 seconds remaining). Shrunk container to 180px (fits ~4 rows). Improves focus on actionable markets. |
| 2025-12-15 | Claude | **Phase 15 COMPLETE**: Trade reconciliation script (`scripts/reconcile_trades.py`). Fetches trades from Polymarket Data API, compares with local DB, identifies untracked positions. `--fix` flag adds to settlement queue. |
| 2025-12-15 | Claude | **Phase 16 COMPLETE**: Dashboard observability widgets. Added Historical Positions panel, Reconciliation Status panel with RECON indicator, `/dashboard/positions` and `/dashboard/reconciliation` endpoints. |
| 2025-12-15 | Claude | **Phase 17 COMPLETE**: Partial fill detection fix. `place_order_sync()` now catches exceptions and returns error dict instead of raising. Allows detection of partial fills when one leg fills and other throws exception. |
| 2025-12-15 | Claude | **Phase 18 COMPLETE**: Configurable position sizing. Added `min_trade_size_usd` config (default $3.0), `GABAGOOL_MIN_TRADE_SIZE` env var, minimum budget enforcement. Matches gabagool22's $3-8 per trade pattern. |
| 2025-12-15 | Claude | **Phase 19 COMPLETE**: Gradual position building. Split trades into tranches with delays. `gradual_entry_enabled`, `gradual_entry_tranches` (default 3), `gradual_entry_delay_seconds` (default 30s), `gradual_entry_min_spread_cents` (default 3¢). Disabled by default. |

---

## Related Documents

- [strategy-rules.md](./strategy-rules.md) - Authoritative strategy rules (SHOULD behavior)
- [REBALANCING_STRATEGY.md](./REBALANCING_STRATEGY.md) - Position rebalancing for partial fills
- [TRADE_ANALYSIS_2025-12-14.md](./TRADE_ANALYSIS_2025-12-14.md) - Analysis of execution failures
- [IMPLEMENTATION_PLAN_2025-12-14.md](./IMPLEMENTATION_PLAN_2025-12-14.md) - Fix plan with code samples
- [POST_MORTEM_2025-12-13.md](./POST_MORTEM_2025-12-13.md) - Previous incident analysis
