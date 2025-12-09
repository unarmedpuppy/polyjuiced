# Polymarket Gabagool Bot - Trading Strategy Rules

## Overview

The Gabagool bot trades on Polymarket's 15-minute binary markets (BTC/ETH Up/Down predictions). It employs two complementary strategies:

1. **Arbitrage Strategy** - Risk-free profit when market is mispriced
2. **Directional Strategy** - Speculative trades on undervalued outcomes

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
- **Max per trade**: $5.00 per side (configurable via `GABAGOOL_MAX_TRADE_SIZE`)
- **Max per 15-min window**: $10.00 (configurable via `GABAGOOL_MAX_PER_WINDOW`)
- **Max daily exposure**: $90.00 (keeps $10 reserve)

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
Trade:      Buy $5 UP + $5 DOWN
Result:     Guaranteed $0.31 profit (~3.1% return)
```

---

## Strategy 2: Directional Trading

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

When both strategies signal simultaneously:

1. **Arbitrage takes priority** - It's risk-free
2. **Directional can run alongside** - Uses separate position sizing
3. **No conflict** - Arb is two-sided, directional is one-sided

### Combined Scenario
```
UP:   $0.23
DOWN: $0.72
Total: $0.95 → 5¢ spread (ARB: YES)
UP price $0.23 < $0.25 (DIRECTIONAL: YES on UP)

Action: Execute both
- Arb: Buy $5 UP + $5 DOWN
- Directional: Buy $1.67 UP (additional)
```

---

## Risk Management

### Daily Limits
| Limit | Value | Purpose |
|-------|-------|---------|
| Max Daily Loss | $5.00 | Stop trading for day |
| Max Daily Exposure | $90.00 | Keep $10 reserve |
| Max Unhedged Exposure | $10.00 | Trigger hedge alert |

### Slippage Protection
- Max slippage: 2¢ from quoted price
- Order timeout: 500ms
- Reject trade if slippage exceeds limit

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
GABAGOOL_DRY_RUN=false  # Set to false for LIVE trading

# Arbitrage Settings
GABAGOOL_MIN_SPREAD=0.02        # 2¢ minimum spread
GABAGOOL_MAX_TRADE_SIZE=5.0     # $ per side
GABAGOOL_MAX_PER_WINDOW=10.0    # $ per 15-min market

# Directional Settings
GABAGOOL_DIRECTIONAL_ENABLED=true
GABAGOOL_DIRECTIONAL_ENTRY_THRESHOLD=0.25   # Max price to enter
GABAGOOL_DIRECTIONAL_TIME_THRESHOLD=0.80    # Min time remaining %
GABAGOOL_DIRECTIONAL_SIZE_RATIO=0.33        # 1/3 of arb size
GABAGOOL_DIRECTIONAL_TARGET_BASE=0.45       # Base take-profit
GABAGOOL_DIRECTIONAL_STOP_LOSS=0.11         # Hard stop

# Risk Limits
GABAGOOL_MAX_DAILY_EXPOSURE=90.0
GABAGOOL_MAX_DAILY_LOSS=5.0
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
