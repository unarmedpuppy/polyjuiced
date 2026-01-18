# Polymarket Trading Analysis - December 13-14, 2025

**Analysis Date:** December 14, 2025
**Trading Period:** December 13, 9:30 AM ET - December 14, 7:00 AM ET (~22 hours)
**Current Balance:** $113.75

## Executive Summary

The bot executed 35 positions across BTC, ETH, and SOL 15-minute markets, spending **$363.49** total. The analysis reveals significant execution problems:

| Strategy Type | Count | % of Total |
|--------------|-------|------------|
| True Arbitrage (>80% hedged) | 5 | 14% |
| Partial Arbitrage (some hedge) | 10 | 29% |
| **Directional (one-sided)** | **20** | **57%** |

**Critical Finding:** Only 14% of positions achieved proper arbitrage execution. The remaining 86% have varying degrees of unhedged exposure, with 57% being completely one-sided.

### Financial Impact

- **Total Spent:** $363.49
- **Expected Profit from Hedged Pairs:** -$78.29 (if all markets resolve)
- **Reason for Negative:** Many "arbitrage" positions paid more than $1.00 per pair due to poor execution

---

## Position-by-Position Analysis

### SUCCESSFUL ARBITRAGE POSITIONS (5 positions)

#### Position #25: ETH 2:00AM-2:15AM ET (Dec 14)
- **Hedge Ratio:** 98.0% ✅
- **Cost:** $12.96
- **Expected Profit:** +$1.43
- UP: $7.78 → 14.69 shares ($0.53/share)
- DOWN: $5.18 → 14.39 shares ($0.36/share)
- **Analysis:** Near-perfect execution. Minimal excess (0.30 UP shares).

#### Position #29: BTC 3:15AM-3:30AM ET (Dec 14)
- **Hedge Ratio:** 94.7% ✅
- **Cost:** $7.66
- **Expected Profit:** +$0.48
- UP: $3.95 → 8.60 shares ($0.46/share)
- DOWN: $3.71 → 8.14 shares ($0.46/share)
- **Analysis:** Good execution. Small excess (0.46 UP shares).

#### Position #12: ETH 2:30PM-2:45PM ET (Dec 13)
- **Hedge Ratio:** 92.1% ✅
- **Cost:** $23.50
- **Expected Profit:** -$2.04
- UP: $12.34 → 23.29 shares ($0.53/share)
- DOWN: $11.16 → 21.46 shares ($0.52/share)
- **Analysis:** Good hedge but overpaid for shares (>$1.00/pair).

#### Position #20: SOL 12:00AM-12:15AM ET (Dec 14)
- **Hedge Ratio:** 83.0%
- **Cost:** $23.93
- **Expected Profit:** -$2.54
- UP: $10.27 → 21.39 shares ($0.48/share)
- DOWN: $13.66 → 25.77 shares ($0.53/share)
- **Analysis:** Acceptable hedge but significant overpayment.

#### Position #26: BTC 2:00AM-2:15AM ET (Dec 14)
- **Hedge Ratio:** 84.6%
- **Cost:** $15.26
- **Expected Profit:** -$1.93
- UP: $7.06 → 13.33 shares ($0.53/share)
- DOWN: $8.20 → 15.76 shares ($0.52/share)
- **Analysis:** Good hedge with minor overpayment.

---

### PARTIAL ARBITRAGE - SEVERELY UNHEDGED (10 positions)

These positions have both UP and DOWN exposure but failed to achieve proper hedging.

#### Position #17: BTC 11:00PM-11:15PM ET (Dec 13) ⚠️ CRITICAL
- **Hedge Ratio:** 30.7% ❌
- **Cost:** $44.32 (LARGEST POSITION)
- **Expected Profit:** -$22.19
- UP: $38.25 → 72.17 shares ($0.53/share)
- DOWN: $6.07 → 22.13 shares ($0.27/share)
- **Unhedged:** 50.04 UP shares
- **Analysis:** MASSIVE execution failure. Bot bought 72 UP shares but only 22 DOWN shares. This is effectively a $38 directional bet on UP.

#### Position #22: BTC 1:00AM-1:15AM ET (Dec 14) ⚠️
- **Hedge Ratio:** 32.4% ❌
- **Cost:** $17.60
- **Expected Profit:** -$7.91
- UP: $15.86 → 29.92 shares ($0.53/share)
- DOWN: $1.74 → 9.69 shares ($0.18/share)
- **Unhedged:** 20.23 UP shares
- **Analysis:** Severe imbalance. Only ~1/3 hedged.

#### Position #19: BTC 11:45PM-12:00AM ET (Dec 13) ⚠️
- **Hedge Ratio:** 46.3% ❌
- **Cost:** $22.01
- **Expected Profit:** -$7.09
- UP: $4.92 → 14.92 shares ($0.33/share)
- DOWN: $17.09 → 32.24 shares ($0.53/share)
- **Unhedged:** 17.32 DOWN shares
- **Analysis:** Opposite problem - heavy DOWN bias.

#### Position #7: BTC 10:30AM-10:45AM ET (Dec 13) ⚠️
- **Hedge Ratio:** 28.5% ❌
- **Cost:** $10.26
- **Expected Profit:** -$7.38
- UP: $0.26 → 2.88 shares ($0.09/share)
- DOWN: $10.00 → 10.10 shares ($0.99/share)
- **Unhedged:** 7.22 DOWN shares
- **Analysis:** Almost entirely one-sided. Paid $0.99/share for DOWN (near market price).

#### Position #10: SOL 2:15PM-2:30PM ET (Dec 13)
- **Hedge Ratio:** 48.5%
- **Cost:** $22.22
- **Expected Profit:** -$6.82
- UP: $16.83 → 31.76 shares ($0.53/share)
- DOWN: $5.39 → 15.40 shares ($0.35/share)
- **Unhedged:** 16.36 UP shares

#### Position #6: BTC 10:15AM-10:30AM ET (Dec 13)
- **Hedge Ratio:** 55.9%
- **Cost:** $33.67
- **Expected Profit:** -$8.65
- UP: $5.50 → 25.02 shares ($0.22/share)
- DOWN: $28.16 → 44.75 shares ($0.63/share)
- **Unhedged:** 19.73 DOWN shares
- Note: Includes a SELL transaction of UP shares ($2.13)

#### Position #5: ETH 10:15AM-10:30AM ET (Dec 13)
- **Hedge Ratio:** 55.5%
- **Cost:** $22.81
- **Expected Profit:** -$5.97
- UP: $16.07 → 30.32 shares ($0.53/share)
- DOWN: $6.74 → 16.84 shares ($0.40/share)
- **Unhedged:** 13.48 UP shares

#### Position #15: BTC 3:00PM-3:15PM ET (Dec 13)
- **Hedge Ratio:** 61.6%
- **Cost:** $22.65
- **Expected Profit:** -$4.67
- UP: $15.46 → 29.17 shares ($0.53/share)
- DOWN: $7.19 → 17.98 shares ($0.40/share)
- **Unhedged:** 11.19 UP shares

#### Position #28: BTC 3:00AM-3:15AM ET (Dec 14)
- **Hedge Ratio:** 64.9%
- **Cost:** $12.80
- **Expected Profit:** -$1.97
- UP: $3.94 → 10.83 shares ($0.36/share)
- DOWN: $8.85 → 16.70 shares ($0.53/share)
- **Unhedged:** 5.87 DOWN shares

#### Position #14: BTC 2:45PM-3:00PM ET (Dec 13)
- **Hedge Ratio:** 78.4%
- **Cost:** $19.09
- **Expected Profit:** -$1.04
- UP: $9.53 → 23.01 shares ($0.41/share)
- DOWN: $9.57 → 18.05 shares ($0.53/share)
- **Unhedged:** 4.96 UP shares
- **Analysis:** Close to acceptable but still leaves exposure.

---

### DIRECTIONAL POSITIONS (20 positions)

These are completely one-sided bets with no hedge.

#### High-Risk Directional (Entry > $0.50/share)

| Market | Side | Cost | Shares | Avg Price | Win | Lose |
|--------|------|------|--------|-----------|-----|------|
| SOL 9:45AM-10:00AM (13th) | UP | $9.90 | 10.10 | $0.98 | +$0.20 | -$9.90 |
| SOL 10:00AM-10:15AM (13th) | UP | $9.90 | 10.10 | $0.98 | +$0.20 | -$9.90 |
| BTC 9:30AM-9:45AM (13th) | UP | $5.00 | 9.43 | $0.53 | +$4.43 | -$5.00 |
| ETH 6:00AM-6:15AM (14th) | UP | $2.71 | 5.11 | $0.53 | +$2.40 | -$2.71 |

**Analysis:** These high-priced entries have poor risk/reward ratios.

#### Low-Risk Directional (Entry < $0.25/share)

| Market | Side | Cost | Shares | Avg Price | Win | Lose |
|--------|------|------|--------|-----------|-----|------|
| BTC 10:00AM-10:15AM (13th) | DOWN | $0.31 | 3.85 | $0.08 | +$3.54 | -$0.31 |
| BTC 11:30PM-11:45PM (13th) | UP | $0.91 | 11.40 | $0.08 | +$10.49 | -$0.91 |
| BTC 6:30AM-6:45AM (14th) | UP | $0.39 | 3.85 | $0.10 | +$3.46 | -$0.39 |
| ETH 3:00PM-3:15PM (13th) | UP | $0.42 | 5.29 | $0.08 | +$4.87 | -$0.42 |
| BTC 12:15AM-12:30AM (14th) | UP | $0.33 | 3.65 | $0.09 | +$3.32 | -$0.33 |
| BTC 6:45AM-7:00AM (14th) | DOWN | $1.05 | 12.49 | $0.08 | +$11.44 | -$1.05 |

**Analysis:** These follow the directional strategy rules (entry < $0.25) and have good risk/reward.

#### Medium-Risk Directional

| Market | Side | Cost | Shares | Avg Price | Win | Lose |
|--------|------|------|--------|-----------|-----|------|
| SOL 11:00AM-11:15AM (13th) | DOWN | $3.77 | 12.99 | $0.29 | +$9.22 | -$3.77 |
| BTC 11:00AM-11:15AM (13th) | DOWN | $2.98 | 9.61 | $0.31 | +$6.63 | -$2.98 |
| BTC 1:30AM-1:45AM (14th) | DOWN | $2.61 | 13.34 | $0.20 | +$10.73 | -$2.61 |
| BTC 1:15AM-1:30AM (14th) | UP | $2.09 | 8.71 | $0.24 | +$6.62 | -$2.09 |
| ETH 2:15PM-2:30PM (13th) | UP | $2.90 | 16.22 | $0.18 | +$13.32 | -$2.90 |
| SOL 2:30AM-2:45AM (14th) | UP | $0.70 | 3.05 | $0.23 | +$2.35 | -$0.70 |
| BTC 2:30PM-2:45PM (13th) | DOWN | $0.62 | 4.80 | $0.13 | +$4.18 | -$0.62 |
| ETH 6:45AM-7:00AM (14th) | UP | $2.05 | 8.52 | $0.24 | +$6.47 | -$2.05 |
| BTC 4:00AM-4:15AM (14th) | UP | $2.28 | 5.01 | $0.45 | +$2.73 | -$2.28 |
| BTC 4:15AM-4:30AM (14th) | UP | $1.84 | 4.12 | $0.45 | +$2.28 | -$1.84 |

---

## Root Cause Analysis

### CONFIRMED: Live Log Analysis (Dec 14, 2025 ~13:25-13:30 UTC)

From the bot logs, I can see exactly what's happening:

```
13:25:19 Arbitrage opportunity detected - BTC spread 2.0¢
13:25:19 Executing PARALLEL dual-leg arbitrage order
13:25:19 Placing YES order (parallel) - price=0.53, shares=13.95
13:25:19 Placing NO order (parallel) - price=0.53, shares=33.21
13:25:22 Parallel order results: no_filled=False, no_status=LIVE, yes_filled=True, yes_status=MATCHED
13:25:22 WARNING: One or both orders went LIVE - cancelling both for atomicity
13:25:22 ERROR: PARTIAL FILL: YES filled but NO didn't - attempting unwind
13:25:22 POST https://clob.polymarket.com/order HTTP/2 400 Bad Request  ← UNWIND FAILED!
```

**This pattern repeats for every trade:**
- Orders are placed in parallel (correct)
- One leg gets MATCHED (immediate fill)
- Other leg goes LIVE (sitting on order book, not filled)
- Bot attempts to cancel/unwind but **FAILS with 400 Bad Request**
- Result: Unhedged position

### Why Arbitrage Execution Failed

#### 1. LIVE vs MATCHED Disparity
The Polymarket CLOB is showing different behavior for each leg:
- **MATCHED** = Order was immediately filled against existing liquidity
- **LIVE** = Order is now sitting on the order book (no one to trade against)

This happens when:
- One side of the market has depth, the other doesn't
- The "cheap" side (e.g., $0.29) has takers, the "expensive" side ($0.69) doesn't
- Market makers are providing liquidity asymmetrically

#### 2. Unwind Mechanism Failing
When partial fills are detected, the bot tries to unwind but:
```
HTTP/2 400 Bad Request  ← Cannot cancel already-filled orders
```
You can't "cancel" a MATCHED order - it's already executed. The bot's atomicity mechanism is broken.

#### 3. Price Mismatch
The bot is pricing both legs at $0.53:
```
Placing YES order: price=0.53, shares=13.95
Placing NO order: price=0.53, shares=33.21
```

But the detected opportunity had YES at $0.29 and NO at $0.69. The bot is using **aggressive limit pricing** ($0.53 = midpoint?) which:
- Works for the cheap side (YES) - gets filled at better price
- Fails for the expensive side (NO) - $0.53 is below the $0.69 ask, so order sits on book

#### 4. Database Not Recording Trades
The bot's SQLite database shows 0 trades despite $363 in actual execution. This means:
- The `add_trade()` function is never being called after successful execution
- Likely because the bot's execution flow treats partial fills as "failures"
- P&L calculations in the dashboard are wrong
- No ability to verify or audit trades

#### 5. Multiple Fills Creating Position Imbalance
The transaction history shows multiple small fills accumulating:
- BTC 11:00PM-11:15PM: 3 separate UP fills totaling $38.25 + 2 DOWN fills totaling $6.07
- This suggests the bot keeps trying to complete the hedge but fails repeatedly

---

## Recommendations

### Immediate Actions

1. **STOP LIVE TRADING** until execution issues are fixed
2. **Enable DRY_RUN=true** for testing
3. **Investigate why trades aren't recorded** in the database

### Code Fixes Required

#### 1. Fix Aggressive Limit Pricing Strategy

The current approach uses $0.53 for both legs regardless of actual market prices. This causes LIVE orders that never fill.

```python
# CURRENT (BROKEN):
limit_price = 0.53  # Same for both legs

# FIX: Use actual market prices with minimal slippage
yes_limit_price = round(min(yes_price + 0.01, 0.99), 2)  # Slightly above current ask
no_limit_price = round(min(no_price + 0.01, 0.99), 2)   # Slightly above current ask
```

#### 2. Fix Unwind Logic

The bot tries to "unwind" MATCHED orders which is impossible:

```python
# CURRENT (BROKEN):
if yes_matched and not no_matched:
    # Try to sell YES to unwind - but what if SELL fails too?

# FIX: If one leg fills and other doesn't:
# 1. Cancel the LIVE order (this works)
# 2. Accept the partial fill as a directional position
# 3. Either hold to resolution OR try to hedge later at market
```

#### 3. Record Partial Fills as Trades

Currently partial fills are treated as "failures" and not recorded:

```python
# FIX: Always record what actually executed
if yes_result.status == "MATCHED":
    record_fill(yes_token, yes_shares_filled)
if no_result.status == "MATCHED":
    record_fill(no_token, no_shares_filled)

# Calculate actual hedge ratio
hedge_ratio = min(yes_filled, no_filled) / max(yes_filled, no_filled)
add_trade(..., actual_hedge_ratio=hedge_ratio)
```

#### 4. Pre-Check Liquidity Before Trading

Before placing orders, verify both sides have sufficient depth:

```python
yes_depth = get_order_book_depth(yes_token, "BUY")
no_depth = get_order_book_depth(no_token, "BUY")

min_required_depth = trade_size * 1.5  # 50% buffer
if yes_depth < min_required_depth or no_depth < min_required_depth:
    log.warning("Insufficient liquidity for atomic execution")
    return None  # Skip this opportunity
```

#### 5. Consider FOK (Fill-or-Kill) Orders

Instead of GTC orders that can sit on the book:

```python
# FOK = Either fill immediately and completely, or cancel entirely
order_args = OrderArgs(
    token_id=token_id,
    price=limit_price,
    size=shares,
    side=side,
)
signed_order = client.create_order(order_args)
result = client.post_order(signed_order, orderType=OrderType.FOK)

# If FOK fails, both legs fail atomically - no partial exposure
```

**Note:** FOK may reduce fill rates but ensures atomicity.

---

## Summary Table

| Metric | Value |
|--------|-------|
| Total Positions | 35 |
| Total Spent | $363.49 |
| Properly Hedged (>80%) | 5 (14%) |
| Partially Hedged | 10 (29%) |
| One-Sided | 20 (57%) |
| Expected P&L from Hedged | -$78.29 |
| Worst Position | BTC 11PM-11:15PM (-$22.19 expected) |
| Best Position | ETH 2AM-2:15AM (+$1.43 expected) |

**Bottom Line:** The arbitrage strategy is fundamentally sound but execution is severely broken. 86% of positions failed to achieve proper hedging, and the database isn't recording any trades. Trading should be halted until these issues are resolved.
