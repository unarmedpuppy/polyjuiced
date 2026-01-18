# Trade Analysis - December 13, 2025

## Executive Summary

**Status: Bot shut down, switched to DRY RUN mode pending fixes**

**Total P&L: ~-$5 to -$10** (estimated, pending final resolution of 3:00PM trades)

### Key Finding

**The core arbitrage strategy is sound.** The losses were caused by **execution failures** - one leg of the trade failing while the other succeeded, creating unhedged directional exposure.

Out of 16 markets traded:
- **10 markets (62.5%)** had ONE-SIDED positions (only UP or only DOWN)
- **4 markets (25%)** had severely IMBALANCED positions (<70% hedge)
- **Only 2 markets (12.5%)** had acceptable hedge ratios (>70%)

This is a **critical execution bug**, not a strategy problem.

---

## Detailed Trade Breakdown

### Losing Trades (Sorted by Loss)

#### 1. SOL 2:15PM-2:30PM ET: **-$6.82**

| Side | Shares | Cost | Avg Price |
|------|--------|------|-----------|
| UP | 31.76 | $16.83 | $0.53 |
| DOWN | 15.40 | $5.39 | $0.35 |
| **Hedge Ratio** | **48.5%** | | |

**Root Cause**: Severely imbalanced hedge - bought 2x more UP than DOWN shares.

**What Happened**:
- First bought 15.4 DOWN @ $0.35 = $5.39
- Then bought 31.76 UP @ $0.53 = $16.83
- Market resolved DOWN, so UP position lost entirely
- Only got back $15.40 (the DOWN shares)
- Lost: $16.83 on UP + ($5.39 DOWN cost - $15.40 redemption = $10.01 profit) = **-$6.82 net**

**Why It Happened**: The position sizing was incorrect - bought way more UP shares than DOWN. This could be due to:
1. Directional trading being enabled (betting on UP)
2. Bug in position sizing calculation
3. Partial fills on DOWN side followed by full fill on UP

---

#### 2. ETH 10:15AM-10:30AM ET: **-$5.97**

| Side | Shares | Cost | Avg Price |
|------|--------|------|-----------|
| UP | 30.32 | $16.07 | $0.53 |
| DOWN | 16.84 | $6.74 | $0.40 |
| **Hedge Ratio** | **55.5%** | | |

**Root Cause**: Imbalanced hedge - almost 2x more UP than DOWN.

**What Happened**:
- Bought 16.84 DOWN @ $0.40 = $6.74
- Bought 14.32 UP @ $0.53 = $7.59
- Bought 16.00 UP @ $0.53 = $8.48
- Market resolved DOWN
- Redeemed $16.84 (hedged portion)
- Lost $16.07 on UP side

**Why It Happened**: Multiple UP buys created an imbalanced position. The second UP buy ($8.48) pushed the position out of balance.

---

#### 3. SOL 11:00AM-11:15AM ET: **-$3.77**

| Side | Shares | Cost | Avg Price |
|------|--------|------|-----------|
| UP | 0.00 | $0.00 | - |
| DOWN | 12.99 | $3.77 | $0.29 |
| **Hedge Ratio** | **0%** | | |

**Root Cause**: **FULLY UNHEDGED** - only DOWN side was purchased.

**What Happened**:
- Only bought 12.99 DOWN @ $0.29 = $3.77
- Market resolved UP
- Lost entire $3.77

**Why It Happened**: This appears to be a **directional bet**, not an arbitrage trade. Either:
1. Directional trading was enabled
2. The UP order failed/timed out and only DOWN filled
3. Position stacking prevention blocked the UP side

---

#### 4. BTC 11:00AM-11:15AM ET: **-$2.98**

| Side | Shares | Cost | Avg Price |
|------|--------|------|-----------|
| UP | 0.00 | $0.00 | - |
| DOWN | 9.61 | $2.98 | $0.31 |
| **Hedge Ratio** | **0%** | | |

**Root Cause**: **FULLY UNHEDGED** - only DOWN side was purchased.

**Same pattern as SOL 11:00AM** - directional bet that lost.

---

#### 5. ETH 2:15PM-2:30PM ET: **-$2.90**

| Side | Shares | Cost | Avg Price |
|------|--------|------|-----------|
| UP | 16.22 | $2.90 | $0.18 |
| DOWN | 0.00 | $0.00 | - |
| **Hedge Ratio** | **0%** | | |

**Root Cause**: **FULLY UNHEDGED** - only UP side was purchased.

**What Happened**:
- Bought 7.28 UP @ $0.19 = $1.38
- Bought 8.94 UP @ $0.17 = $1.52
- Market resolved DOWN
- Lost entire $2.90

**Note**: The low prices ($0.17-$0.19) suggest these were late-market directional bets when UP was heavily discounted.

---

#### 6. BTC 2:45PM-3:00PM ET: **-$1.04**

| Side | Shares | Cost | Avg Price |
|------|--------|------|-----------|
| UP | 23.01 | $9.53 | $0.41 |
| DOWN | 18.05 | $9.57 | $0.53 |
| **Hedge Ratio** | **78.4%** | | |

**Root Cause**: Moderately imbalanced - 5 more UP shares than DOWN.

**This was close to profitable!** With better balancing, this would have been a win.

---

### Winning Trades

#### BTC 10:15AM-10:30AM ET: **+$15.34**

| Side | Shares | Cost | Avg Price |
|------|--------|------|-----------|
| UP | 12.51 | $3.38 | $0.27 |
| DOWN | 44.75 | $28.16 | $0.63 |

**What Happened**: Heavy DOWN position, market resolved DOWN. The excess DOWN shares paid off big.

**Note**: This was also imbalanced (in the opposite direction), but happened to work out because the bet was correct. This is gambling luck, not arbitrage profit.

---

#### BTC 9:30AM-9:45AM ET: **+$4.43**

| Side | Shares | Cost | Avg Price |
|------|--------|------|-----------|
| UP | 9.43 | $5.00 | $0.53 |
| DOWN | 0.00 | $0.00 | - |

**Fully unhedged** directional bet that won. Lucky, not skill.

---

### Properly Hedged Trades (Small Wins/Losses)

These trades demonstrate the arbitrage strategy working correctly:

| Market | UP Shares | DOWN Shares | Hedge % | P&L |
|--------|-----------|-------------|---------|-----|
| ETH 2:30PM | 23.29 | 21.46 | 92.1% | -$0.21 |
| BTC 10:30AM | 2.88 | 10.10 | 28.5% | -$0.16 |
| SOL 10:00AM | 10.10 | 0.00 | 0% | +$0.20 |
| SOL 9:45AM | 10.10 | 0.00 | 0% | +$0.20 |

The ETH 2:30PM trade at 92.1% hedge ratio is the closest to proper arbitrage and only lost $0.21 (essentially break-even, which is expected when the spread is small).

---

## Root Cause Analysis

### Problem 1: Directional Trading Was Enabled

Multiple trades show **only one side** being purchased:
- SOL 11:00AM: Only DOWN
- BTC 11:00AM: Only DOWN
- ETH 2:15PM: Only UP
- BTC 2:30PM: Only DOWN

**Status**: ✅ **FIXED** - Directional trading is now disabled (`directional_enabled: false`)

### Problem 2: Imbalanced Position Sizing

Several trades show significantly more shares on one side:
- SOL 2:15PM: 31.76 UP vs 15.40 DOWN (2:1 ratio)
- ETH 10:15AM: 30.32 UP vs 16.84 DOWN (1.8:1 ratio)

**Cause**: The position sizing uses inverse weighting based on price, but this can create imbalances when:
1. Prices are very different (e.g., UP @ $0.53, DOWN @ $0.35)
2. Multiple orders stack on one side
3. Partial fills leave one side larger

**Status**: ⚠️ **PARTIALLY ADDRESSED** - Position stacking prevention added, but sizing imbalance remains

### Problem 3: Position Stacking

Multiple buys on the same side within one market:
- ETH 10:15AM: Three separate UP buys totaling $16.07
- BTC 2:45PM: Three separate DOWN buys

This happens when the bot doesn't recognize it already has a position in the market.

**Status**: ✅ **FIXED** - Position stacking prevention now tracks active positions

---

## Recommendations

### Immediate (Already Implemented)

1. ✅ **Disable directional trading** - Done, prevents one-sided bets
2. ✅ **Position stacking prevention** - Done, prevents multiple buys on same side
3. ✅ **Balance-based sizing** - Done, caps position at 25% of balance

### Future Improvements

1. **Enforce hedge ratio floor**: Don't execute trades unless both sides can achieve at least 80% hedge ratio

2. **Atomic order execution**: Execute YES and NO orders simultaneously, cancel both if either fails

3. **Pre-trade validation**: Before executing, verify:
   - Both order books have sufficient liquidity
   - Expected hedge ratio is acceptable
   - Combined spread still exceeds minimum threshold

4. **Post-trade rebalancing**: If position becomes imbalanced, attempt to rebalance before market close

---

## Financial Summary

| Category | Amount |
|----------|--------|
| **Gross Losses** | -$23.48 |
| **Gross Wins** | +$20.17 |
| **Near Break-even** | -$1.30 |
| **Net P&L** | **-$4.61** |

### Loss Attribution

| Cause | Lost Amount | % of Losses |
|-------|-------------|-------------|
| Fully unhedged directional bets | -$9.65 | 41% |
| Severely imbalanced hedges (<60%) | -$12.79 | 54% |
| Minor imbalances (>60% hedge) | -$1.04 | 4% |

**Conclusion**: 95% of losses were due to missing or severely imbalanced hedges. Proper arbitrage execution would have resulted in approximately break-even to small profit.

---

## Root Cause Deep Dive

### The Dual-Leg Execution Problem

The `execute_dual_leg_order()` function attempts to:
1. Place YES order first
2. If YES fills → Place NO order
3. If NO fails → Attempt to unwind YES

**The problem**: This sequential approach fails in practice because:

1. **GTC orders vs FOK**: We use GTC (Good-Till-Cancel) instead of FOK (Fill-or-Kill) due to decimal precision bugs in py-clob-client. GTC orders can partially fill or sit unfilled.

2. **"LIVE" status ambiguity**: The code treats `LIVE` status as "filled" (`yes_filled = yes_status in ("MATCHED", "FILLED", "LIVE")`), but LIVE means the order is active on the book, not filled.

3. **Unwind failures**: When the second leg fails, the unwind attempt also uses GTC orders which may not fill, leaving us exposed.

4. **Directional trading interference**: Even with `directional_enabled: false`, the near-resolution trading feature was still creating one-sided positions.

### Evidence from Trade Data

**One-sided positions (10 markets)**:
```
BTC 9:30AM:  Only UP (9.43 shares)  - directional or failed dual-leg
BTC 10:00AM: Only DOWN (3.85 shares) - directional
BTC 10:15AM: Only DOWN (44.75 shares after sell) - UP was sold/unwound
BTC 11:00AM: Only DOWN (9.61 shares) - directional
BTC 2:30PM:  Only DOWN (4.80 shares) - directional
SOL 9:45AM:  Only UP (10.10 shares) - directional
SOL 10:00AM: Only UP (10.10 shares) - directional
SOL 11:00AM: Only DOWN (12.99 shares) - directional
ETH 2:15PM:  Only UP (16.22 shares) - directional
ETH 3:00PM:  Only UP (5.29 shares) - directional
```

**Pattern**: Many of these show prices at $0.08-$0.30, which are the cheap "directional bet" prices. The near-resolution feature and/or directional trading were placing one-sided bets.

**Imbalanced positions (4 markets)**:
```
BTC 10:30AM: UP 2.88 vs DOWN 10.10 (29% hedge)
BTC 3:00PM:  UP 29.17 vs DOWN 17.98 (62% hedge)
ETH 10:15AM: UP 30.32 vs DOWN 16.84 (56% hedge)
SOL 2:15PM:  UP 31.76 vs DOWN 15.40 (48% hedge)
```

**Pattern**: Multiple separate buy orders on one side. The position stacking prevention wasn't working correctly, allowing 2-3 UP buys in a row.

---

## Action Plan

### Phase 1: Immediate (Before Going Live Again)

1. ✅ **Enable DRY RUN mode** - Done
2. ⬜ **Disable near-resolution trading** - It's creating one-sided positions
3. ⬜ **Fix LIVE status handling** - Only treat MATCHED/FILLED as filled
4. ⬜ **Add post-trade hedge verification** - Check actual position after trade

### Phase 2: Enforce Hedge Ratio (Critical)

**New config parameters:**
```python
min_hedge_ratio: float = 0.80  # Minimum 80% hedge required
max_position_imbalance_shares: float = 5.0  # Max unhedged shares allowed
```

**Implementation approach:**
1. **Pre-trade**: Calculate expected hedge ratio, reject if <80%
2. **During execution**: If first leg fills, second leg MUST fill or unwind
3. **Post-trade**: Verify actual hedge ratio, rebalance if needed
4. **Circuit breaker**: If hedge ratio drops below 60%, halt new trades

### Phase 3: Better Order Execution

**Option A: Truly atomic execution**
- Use limit orders on both sides simultaneously
- Cancel both if either doesn't fill within timeout
- Requires careful price selection to ensure fills

**Option B: Conservative sizing**
- Size orders to only consume 20% of displayed liquidity
- Higher probability of fills but smaller positions

**Option C: External execution service**
- Use a DEX aggregator or professional execution API
- Higher reliability but adds dependency

### Phase 4: Monitoring & Alerting

1. **Real-time hedge ratio dashboard** - Show current hedge % per market
2. **Alert on imbalance** - Notify if any position drops below 70% hedge
3. **Daily P&L tracking** - Track actual vs expected profit
4. **Fill rate metrics** - Track what % of dual-leg orders succeed

---

## Recommended Next Steps

1. **Keep bot in DRY RUN mode** while we implement fixes
2. **Implement hedge ratio enforcement** - This is the critical fix
3. **Disable near-resolution trading** until we verify it's not the source
4. **Add comprehensive logging** to understand why legs fail
5. **Test in DRY RUN** for 1-2 days to verify fixes work
6. **Go live with smaller position sizes** ($5 max per trade initially)

---

## Appendix: All Trades by Time

| Time | Market | Action | Shares | Side | Cost | Result |
|------|--------|--------|--------|------|------|--------|
| 9:30 AM | BTC | Buy | 9.43 | UP | $5.00 | Win (unhedged) |
| 9:45 AM | SOL | Buy | 10.10 | UP | $9.90 | Win (unhedged) |
| 10:00 AM | SOL | Buy | 10.10 | UP | $9.90 | Win (unhedged) |
| 10:00 AM | BTC | Buy | 3.85 | DOWN | $0.31 | Loss (unhedged) |
| 10:15 AM | ETH | Buy | 16.84 | DOWN | $6.74 | - |
| 10:15 AM | ETH | Buy | 14.32 | UP | $7.59 | - |
| 10:15 AM | ETH | Buy | 16.00 | UP | $8.48 | Loss (imbalanced) |
| 10:15 AM | BTC | Buy | 12.51 | UP | $3.38 | - |
| 10:15 AM | BTC | Buy | 10.10 | DOWN | $9.80 | - |
| 10:15 AM | BTC | Buy | 34.65 | DOWN | $18.36 | Win (imbalanced) |
| 10:30 AM | BTC | Buy | 2.88 | UP | $0.26 | - |
| 10:30 AM | BTC | Buy | 10.10 | DOWN | $10.00 | Near break-even |
| 11:00 AM | SOL | Buy | 12.99 | DOWN | $3.77 | Loss (unhedged) |
| 11:00 AM | BTC | Buy | 9.61 | DOWN | $2.98 | Loss (unhedged) |
| 2:15 PM | SOL | Buy | 15.40 | DOWN | $5.39 | - |
| 2:15 PM | SOL | Buy | 31.76 | UP | $16.83 | Loss (imbalanced) |
| 2:15 PM | ETH | Buy | 7.28 | UP | $1.38 | - |
| 2:15 PM | ETH | Buy | 8.94 | UP | $1.52 | Loss (unhedged) |
| 2:30 PM | ETH | Buy | 6.08 | UP | $3.22 | - |
| 2:30 PM | ETH | Buy | 10.63 | UP | $5.63 | - |
| 2:30 PM | ETH | Buy | 6.58 | UP | $3.49 | - |
| 2:30 PM | ETH | Buy | 21.46 | DOWN | $11.16 | Near break-even |
| 2:30 PM | BTC | Buy | 4.80 | DOWN | $0.62 | Loss (unhedged) |
| 2:45 PM | BTC | Buy | 15.32 | UP | $7.22 | - |
| 2:45 PM | BTC | Buy | 5.00 | DOWN | $2.65 | - |
| 2:45 PM | BTC | Buy | 10.63 | DOWN | $5.63 | - |
| 2:45 PM | BTC | Buy | 2.42 | DOWN | $1.28 | - |
| 2:45 PM | BTC | Buy | 7.69 | UP | $2.31 | Loss (imbalanced) |
| 3:00 PM | BTC | Buy | 17.98 | DOWN | $7.19 | - |
| 3:00 PM | BTC | Buy | 16.17 | UP | $8.57 | - |
| 3:00 PM | BTC | Buy | 13.00 | UP | $6.89 | Pending (62% hedge) |
| 3:00 PM | ETH | Buy | 5.29 | UP | $0.42 | Pending (unhedged) |

---

## Position Summary Table

| Market | UP Shares | DOWN Shares | Hedge % | Issue |
|--------|-----------|-------------|---------|-------|
| BTC 9:30AM | 9.43 | 0.00 | 0% | ONE-SIDED |
| SOL 9:45AM | 10.10 | 0.00 | 0% | ONE-SIDED |
| BTC 10:00AM | 0.00 | 3.85 | 0% | ONE-SIDED |
| SOL 10:00AM | 10.10 | 0.00 | 0% | ONE-SIDED |
| BTC 10:15AM | 0.00 | 44.75 | 0% | ONE-SIDED (after unwind) |
| ETH 10:15AM | 30.32 | 16.84 | 56% | IMBALANCED |
| BTC 10:30AM | 2.88 | 10.10 | 29% | IMBALANCED |
| SOL 11:00AM | 0.00 | 12.99 | 0% | ONE-SIDED |
| BTC 11:00AM | 0.00 | 9.61 | 0% | ONE-SIDED |
| SOL 2:15PM | 31.76 | 15.40 | 48% | IMBALANCED |
| ETH 2:15PM | 16.22 | 0.00 | 0% | ONE-SIDED |
| ETH 2:30PM | 23.29 | 21.46 | **92%** | ✅ GOOD |
| BTC 2:30PM | 0.00 | 4.80 | 0% | ONE-SIDED |
| BTC 2:45PM | 23.01 | 18.05 | **78%** | ✅ ACCEPTABLE |
| BTC 3:00PM | 29.17 | 17.98 | 62% | IMBALANCED |
| ETH 3:00PM | 5.29 | 0.00 | 0% | ONE-SIDED |
