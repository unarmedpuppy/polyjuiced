# Polymarket Gabagool Bot - Trading Strategy Rules

## Overview

The Gabagool bot trades on Polymarket's 15-minute binary markets (BTC/ETH/SOL Up/Down predictions). It employs three complementary strategies:

1. **Arbitrage Strategy** - Risk-free profit when market is mispriced
2. **Near-Resolution Strategy** - High-confidence bets in final minute
3. **Directional Strategy** - Speculative trades on undervalued outcomes (disabled by default)

---

## Strategy 1: Arbitrage Trading

### Concept
In a binary market, YES + NO should always equal $1.00 (since one outcome is guaranteed). When the sum is less than $1.00, buying both sides guarantees profit at resolution.

### Entry Rules
| Condition | Requirement |
|-----------|-------------|
| Spread | YES_price + NO_price < $0.98 (2¢ minimum spread) |
| Market State | Must be tradeable (not resolved/paused) |
| Time Remaining | Any (arb is risk-free) |

### Position Sizing

**CRITICAL: Arbitrage requires EQUAL SHARES on both sides.**

The formula:
```
cost_per_pair = yes_price + no_price
num_pairs = budget / cost_per_pair
yes_shares = num_pairs  (equal)
no_shares = num_pairs   (equal)
```

This ensures equal payout regardless of outcome:
- If UP wins: 21.5 shares * $1 = $21.50
- If DOWN wins: 21.5 shares * $1 = $21.50

**Limits:**
- **Max per trade**: 25% of available balance (configurable via `GABAGOOL_BALANCE_SIZING_PCT`)
- **Max per 15-min window**: $50.00 (configurable via `GABAGOOL_MAX_PER_WINDOW`)
- **Max daily exposure**: Unlimited (circuit breaker uses max daily loss instead)
- **Max daily loss**: $10.00 (circuit breaker halts trading)

### Exit Rules
- **No active exit needed** - Hold to resolution for guaranteed payout
- One side pays $1.00, other pays $0.00
- Profit = $1.00 - (YES_cost + NO_cost)

### Example
```
UP price:   $0.48 (ask)
DOWN price: $0.49 (ask)
Total cost: $0.97
Spread:     $0.03 (3¢ profit per share)
Budget:     $20.00

Calculation:
- cost_per_pair = 0.48 + 0.49 = 0.97
- num_pairs = 20 / 0.97 = 20.6 shares
- Buy 20.6 UP shares @ $0.48 = $9.89
- Buy 20.6 DOWN shares @ $0.49 = $10.09
- Total cost: $19.98

Result:
- If UP wins: 20.6 * $1 = $20.60 (profit: $0.62)
- If DOWN wins: 20.6 * $1 = $20.60 (profit: $0.62)
- Guaranteed 3.1% return
```

### Partial Fill Protection

**ISSUE**: If only one leg fills, we're left with unhedged directional exposure.

**SOLUTION**: Three-layer protection:
1. **Pre-flight liquidity check** - Verify both sides have sufficient liquidity
2. **Automatic unwind** - If YES fills but NO fails, immediately sell YES
3. **Status reporting** - Dashboard shows partial fill alerts

---

## Strategy 2: Near-Resolution Trading

### Concept
In the final minute before resolution, prices often haven't fully converged to $1.00 for the winning side. If a price is between $0.94-$0.975, it indicates high confidence in that outcome.

### Entry Rules
| Condition | Requirement |
|-----------|-------------|
| Time Remaining | ≤ 60 seconds |
| Price | $0.94 ≤ price ≤ $0.975 |
| Market State | Must be tradeable |
| No Arb Position | **Must NOT have existing arbitrage position** |

### Position Sizing
- **Fixed size**: $10.00 per trade (configurable via `GABAGOOL_NEAR_RESOLUTION_SIZE`)
- **Single-leg only** - Buy the high-confidence side

### Exit Rules
- **Hold to resolution** - These are final-minute bets
- Expected payout: $1.00 per share if correct
- Risk: $0.00 if wrong (but 94%+ probability of being correct)

### Example
```
Market: BTC 15-min Up/Down
Time:   45 seconds remaining
UP:     $0.96 (high confidence UP will win)
DOWN:   $0.03

Action: Buy UP at $0.96
Size:   $10 / 0.96 = 10.4 shares
Cost:   $10.00

Expected outcome (96% probability):
- If UP wins: 10.4 * $1 = $10.40 (profit: $0.40)
- If DOWN wins: $0.00 (loss: $10.00) - rare
```

### Strategy Isolation

**CRITICAL**: Near-resolution trades are BLOCKED on markets with existing arbitrage positions.

**WHY**: Running both strategies on the same market creates unbalanced positions:
```
BAD: Arb creates 20 UP + 20 DOWN, then Near-res adds 10 more UP
Result: 30 UP / 20 DOWN (unbalanced = directional risk!)
```

The `_arbitrage_positions` tracking dict prevents this scenario.

---

## Strategy 3: Directional Trading (DISABLED BY DEFAULT)

### Concept
When a side is significantly underpriced early in a market's life, take a speculative position betting on mean reversion or favorable resolution.

### Entry Rules
| Condition | Requirement |
|-----------|-------------|
| Time Remaining | > 80% of market duration |
| Price | Either UP or DOWN < $0.25 |
| Position | Buy the cheaper side only (one-sided) |

**Rationale**: Early markets often have mispriced extremes. A $0.25 price implies 25% probability - if true probability is higher, we profit.

### Position Sizing
- **Size**: 1/3 of arbitrage trade size
- **Example**: If arb max is $5.00, directional max is ~$1.67
- **Separate exposure tracking** from arbitrage positions

### Exit Rules (Priority Order)

1. **Take Profit Target**
   - Base target: Sell when price ≥ $0.45
   - Scaled targets based on entry:
     | Entry Price | Target Price | Gain |
     |-------------|--------------|------|
     | $0.20 | $0.40 | 100% |
     | $0.25 | $0.45 | 80% |
     | $0.30 | $0.50 | 67% |

2. **Trailing Stop** (once profitable)
   - Activates when price reaches target - 5¢
   - Trails at 10¢ below highest price seen
   - Locks in gains while allowing further upside

3. **Stop Loss**
   - Hard stop at $0.11 (~55-65% loss depending on entry)
   - Prevents total wipeout

4. **Time-Based Exit**
   - When < 20% time remaining:
     - If profitable: **Hold to resolution** (let it ride)
     - If unprofitable: Cut position (avoid resolution risk)

5. **Hold to Resolution**
   - If near expiry AND in profit → hold for full payout
   - Binary outcome: $1.00 if correct, $0.00 if wrong
   - Only hold if conviction is reasonable based on price action

### Example Trade
```
Market: ETH 15-min Up/Down
Time:   13 minutes remaining (87% of 15 min)
UP:     $0.22 (ask)
DOWN:   $0.76 (ask)

Entry:  Buy UP at $0.22 (cheaper side, < $0.25 threshold)
Size:   $1.67 (1/3 of $5 arb size)
Target: $0.40 (scaled for $0.22 entry = 82% gain)
Stop:   $0.11 (50% loss)

Scenario A: Price rises to $0.45 → Sell for $0.38 profit (82%)
Scenario B: Price drops to $0.11 → Sell for $0.18 loss (50%)
Scenario C: 3 min left, price at $0.35 → Hold to resolution
```

---

## Strategy Priority

When multiple strategies signal simultaneously:

1. **Arbitrage takes priority** - It's risk-free
2. **Near-resolution runs in final minute** - But NEVER on arb markets
3. **Directional can run alongside** - Uses separate position sizing (if enabled)

**Key Rule**: Near-resolution is BLOCKED on markets with existing arbitrage positions.

### Combined Scenario
```
Market: ETH Up/Down
Time:   10 minutes remaining

UP:   $0.47
DOWN: $0.48
Total: $0.95 → 5¢ spread

10:00 remaining: Execute ARBITRAGE (5¢ spread qualifies)
- Buy 21 UP shares + 21 DOWN shares

0:45 remaining: Near-resolution check
- UP now at $0.96 (qualifies for near-res)
- BUT market already has arb position
- SKIP near-resolution (prevents stacking)

Resolution: Market resolves, one side wins
- Settlement: Sell winning side at $0.99
```

---

## Auto-Settlement (Claiming Winnings)

### The Problem
The py-clob-client library doesn't have a native redeem/claim function. After a market resolves, winning positions are worth $1.00 but there's no direct API to claim them.

See: https://github.com/Polymarket/py-clob-client/issues/117

### The Workaround
Sell winning positions at $0.99 to realize profits. After market resolution, prices for the winning side reach ~$0.99 within 10-15 minutes.

### Settlement Process
1. **Track positions** - All trades are recorded in `_tracked_positions`
2. **Wait 10 minutes** - Allow prices to converge after market close
3. **Sell at $0.99** - Execute GTC sell order for winning positions
4. **Clean up** - Cancel stale orders for ended markets

### Settlement Timing
| Event | Action |
|-------|--------|
| Market closes | Position tracked |
| +10 minutes | First settlement attempt |
| Every 60 seconds | Retry if not claimed |
| Success | Position marked claimed |

### Example Settlement
```
Position: 21.5 UP shares (market resolved UP)
Entry cost: $10.75 (21.5 * $0.50)
Sell at: $0.99 per share
Proceeds: $21.29 (21.5 * $0.99)
Profit: $10.54 (proceeds - cost)

Note: $0.22 lost to 1% spread ($21.50 - $21.29)
This is the cost of the workaround.
```

---

## Risk Management

### Daily Limits
| Limit | Value | Purpose |
|-------|-------|---------|
| Max Daily Loss | $10.00 | Circuit breaker halts trading |
| Max Daily Exposure | Unlimited | No limit (circuit breaker handles risk) |
| Max Unhedged Exposure | $10.00 | Trigger hedge alert |

### Position Sizing
| Setting | Value | Purpose |
|---------|-------|---------|
| Balance Sizing | 25% of available | Scale with account size |
| Max Trade Size | $25.00 (cap) | Upper bound for balance sizing |
| Max Per Window | $50.00 | Limit per 15-min market |

### Slippage Protection
- **UPDATED 2025-12-14**: Zero slippage policy
- Use EXACT opportunity prices as limit prices
- If we can't fill at the detected price, don't take the trade
- Goal is **precision execution**, not guaranteed fills
- A missed opportunity is better than a losing trade
- FOK (Fill-or-Kill) orders ensure atomicity

### Position Tracking
- Track arbitrage and directional positions separately
- Monitor P&L per strategy
- Log all decisions for analysis

---

## Configuration Reference

### Environment Variables
```bash
# Strategy Enable/Disable
GABAGOOL_ENABLED=true
GABAGOOL_DRY_RUN=false           # LIVE mode
GABAGOOL_MARKETS=BTC,ETH,SOL     # Markets to monitor

# Arbitrage Settings
GABAGOOL_MIN_SPREAD=0.02         # 2¢ minimum spread
GABAGOOL_BALANCE_SIZING_ENABLED=true  # Scale with available balance
GABAGOOL_BALANCE_SIZING_PCT=0.25      # Use 25% of balance per trade
GABAGOOL_MAX_TRADE_SIZE=25.0     # $ per trade (cap for balance sizing)
GABAGOOL_MAX_PER_WINDOW=50.0     # $ per 15-min market
GABAGOOL_ORDER_TIMEOUT=10.0      # Seconds for order execution

# Near-Resolution Settings (enabled by default)
GABAGOOL_NEAR_RESOLUTION_ENABLED=true
GABAGOOL_NEAR_RESOLUTION_TIME=60.0        # Max seconds remaining
GABAGOOL_NEAR_RESOLUTION_MIN_PRICE=0.94   # Minimum price (94¢)
GABAGOOL_NEAR_RESOLUTION_MAX_PRICE=0.975  # Maximum price (97.5¢)
GABAGOOL_NEAR_RESOLUTION_SIZE=10.0        # Fixed $ per trade

# Directional Settings (disabled by default)
GABAGOOL_DIRECTIONAL_ENABLED=false
GABAGOOL_DIRECTIONAL_ENTRY_THRESHOLD=0.25   # Max price to enter
GABAGOOL_DIRECTIONAL_TIME_THRESHOLD=0.80    # Min time remaining %
GABAGOOL_DIRECTIONAL_SIZE_RATIO=0.33        # 1/3 of arb size
GABAGOOL_DIRECTIONAL_TARGET_BASE=0.45       # Base take-profit
GABAGOOL_DIRECTIONAL_STOP_LOSS=0.11         # Hard stop

# Risk Limits
GABAGOOL_MAX_DAILY_EXPOSURE=0.0  # 0 = unlimited
GABAGOOL_MAX_DAILY_LOSS=10.0     # Circuit breaker: halt at $10 loss
GABAGOOL_MAX_SLIPPAGE=0.02
```

---

## Dashboard Display

### Arbitrage Decisions
```
[ARB: YES] Spread 3.2¢ >= 2.0¢ threshold
[ARB: NO]  Spread 1.5¢ < 2.0¢ threshold
```

### Directional Decisions
```
[DIR: YES] UP $0.22 < $0.25, 87% time remaining
[DIR: NO]  Prices $0.45/$0.55 above threshold
[DIR: NO]  Only 15% time remaining
```

### Position Status
```
[POSITION] Directional UP @ $0.22, current $0.38, +72%
[EXIT] Trailing stop triggered at $0.35
[HOLD] Holding to resolution, 2 min remaining, +45%
```

---

## Change Log

| Date | Change |
|------|--------|
| 2024-12-09 | Initial strategy documentation |
| 2024-12-09 | Added directional strategy with optimizations |
| 2024-12-09 | Added scaled targets, trailing stops, hold-to-resolution |
| 2024-12-13 | Added near-resolution strategy (final minute high-confidence bets) |
| 2024-12-13 | Added SOL to monitored markets |
| 2024-12-13 | Fixed partial fill protection with automatic unwind |
| 2024-12-13 | Added strategy isolation (arb blocks near-res on same market) |
| 2024-12-13 | Added auto-settlement via $0.99 sell workaround |
| 2024-12-13 | Fixed decimal precision (use Decimal + ROUND_DOWN) |
| 2024-12-13 | Switched from FOK to GTC orders (py-clob-client bug) |
| 2024-12-13 | Updated position limits to $25/trade, $200/day |
| 2024-12-13 | Added liquidity data collection (Phase 1 of sizing roadmap) |
| 2024-12-13 | Removed daily exposure limit, set max daily loss to $10 (circuit breaker) |
| 2024-12-13 | Arb position sizes now scale with available balance (25% of capital) |
| 2024-12-13 | Disabled directional trading |
| 2025-12-14 | **CRITICAL**: Changed slippage policy to ZERO slippage |
| 2025-12-14 | Switched from GTC to FOK orders for atomicity |
| 2025-12-14 | Prices now flow from opportunity detection to execution |
