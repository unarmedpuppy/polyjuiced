# Implementation Plan: Polymarket Bot Execution Fixes

**Created:** December 14, 2025
**Status:** Dry run mode enabled on server
**Priority:** Critical - $363 in losses from execution bugs

## Overview

This plan addresses the critical execution failures identified in [TRADE_ANALYSIS_2025-12-14.md](./TRADE_ANALYSIS_2025-12-14.md). The bot successfully detects arbitrage opportunities but fails during execution, leaving unhedged positions.

## Architecture Changes

### Current (Broken) Architecture
```
Strategy → Dashboard.add_trade() → Dashboard._db.save_trade()
              ↓
         Dashboard displays
```

**Problems:**
- Trade persistence is coupled to dashboard
- Dashboard shouldn't own trade data
- Partial fills not recorded because they're treated as "failures"

### Target Architecture
```
Strategy → Database.save_trade() → Events/Callbacks
                                        ↓
                                  Dashboard.on_trade_update()
```

**Benefits:**
- Strategy owns persistence (single source of truth)
- Dashboard is read-only consumer
- All fills recorded regardless of hedge status
- Clear separation of concerns

---

## Implementation Phases

### Phase 1: Fix Dynamic Pricing (Critical)

**Problem:** Bot uses hardcoded $0.53 limit price for both legs, causing one leg to sit on the order book unfilled.

**Files to modify:**
- `src/client/polymarket.py` - `execute_arbitrage_trade()` method

**Current code pattern:**
```python
# BROKEN: Same price for both legs
limit_price = 0.53
yes_order = create_order(price=limit_price, ...)
no_order = create_order(price=limit_price, ...)
```

**Fix:**
```python
def execute_arbitrage_trade(
    self,
    yes_token_id: str,
    no_token_id: str,
    yes_amount: float,
    no_amount: float,
    yes_price: float,  # Actual market price from opportunity
    no_price: float,   # Actual market price from opportunity
    slippage: float = 0.02,  # 2 cents default
) -> DualLegResult:
    """Execute arbitrage with dynamic pricing based on actual market prices."""

    # Calculate limit prices from actual market prices + slippage
    yes_limit = round(min(yes_price + slippage, 0.99), 2)
    no_limit = round(min(no_price + slippage, 0.99), 2)

    log.info(
        "Executing arbitrage with dynamic pricing",
        yes_market=f"${yes_price:.2f}",
        yes_limit=f"${yes_limit:.2f}",
        no_market=f"${no_price:.2f}",
        no_limit=f"${no_limit:.2f}",
    )

    # ... rest of execution
```

**Validation:**
- [ ] Prices logged match detected opportunity prices
- [ ] Both legs use appropriate limit prices
- [ ] Test with dry run showing correct prices

---

### Phase 2: Move Trade Persistence to Strategy

**Problem:** Dashboard owns trade recording, but strategy should own it.

**Files to modify:**
- `src/strategies/gabagool.py` - Add direct database calls
- `src/dashboard.py` - Remove `save_trade` calls, make read-only
- `src/persistence.py` - Add event emission capability

**Step 2.1: Add trade recording directly in strategy**

```python
# In gabagool.py

from ..persistence import get_database

class GabagoolStrategy:
    async def _record_trade(
        self,
        market: Market15Min,
        yes_price: float,
        no_price: float,
        yes_cost: float,
        no_cost: float,
        yes_shares: float,
        no_shares: float,
        expected_profit: float,
        hedge_ratio: float,
        dry_run: bool,
    ) -> str:
        """Record a trade to the database. Strategy owns persistence."""
        db = await get_database()

        trade_id = f"trade-{int(time.time() * 1000)}"

        await db.save_trade(
            trade_id=trade_id,
            asset=market.asset,
            yes_price=yes_price,
            no_price=no_price,
            yes_cost=yes_cost,
            no_cost=no_cost,
            spread=round((1.0 - yes_price - no_price) * 100, 1),
            expected_profit=expected_profit,
            market_end_time=market.end_time.strftime("%H:%M") if market.end_time else None,
            market_slug=market.slug,
            condition_id=market.condition_id,
            dry_run=dry_run,
            # New fields for Phase 2
            yes_shares=yes_shares,
            no_shares=no_shares,
            hedge_ratio=hedge_ratio,
        )

        # Notify dashboard of new trade (event-based)
        self._emit_trade_event(trade_id, market, yes_cost, no_cost, dry_run)

        return trade_id
```

**Step 2.2: Update database schema for new fields**

```python
# In persistence.py - add to CREATE TABLE trades

yes_shares REAL,
no_shares REAL,
hedge_ratio REAL,
execution_status TEXT DEFAULT 'unknown',  # 'full', 'partial', 'failed'
```

**Step 2.3: Make dashboard read-only**

```python
# In dashboard.py - remove save_trade calls

def add_trade(...) -> str:
    """Add trade to dashboard display (called by strategy via event)."""
    # NO database calls here - just update in-memory display
    trade = {
        "id": trade_id,
        "asset": asset,
        # ... display fields only
    }
    trades.append(trade)

    # Broadcast to SSE clients
    if dashboard:
        asyncio.create_task(dashboard.broadcast({"trades": [trade]}))

    return trade_id
```

**Validation:**
- [ ] Trades appear in database after strategy execution
- [ ] Dashboard displays trades (read from strategy events)
- [ ] No `save_trade` calls in dashboard.py

---

### Phase 3: Record All Fills (Partial and Full)

**Problem:** Partial fills treated as failures and not recorded.

**Files to modify:**
- `src/client/polymarket.py` - Return actual fill data
- `src/strategies/gabagool.py` - Record regardless of hedge status

**Step 3.1: Update DualLegResult to include actual fills**

```python
@dataclass
class DualLegResult:
    """Result of dual-leg arbitrage execution."""
    success: bool

    # What we intended
    intended_yes_shares: float
    intended_no_shares: float

    # What actually filled
    actual_yes_shares: float = 0.0
    actual_no_shares: float = 0.0
    actual_yes_cost: float = 0.0
    actual_no_cost: float = 0.0

    # Fill status
    yes_status: str = "UNKNOWN"  # MATCHED, LIVE, CANCELLED, FAILED
    no_status: str = "UNKNOWN"

    # Calculated
    hedge_ratio: float = 0.0

    error: str = None
```

**Step 3.2: Always record what actually executed**

```python
# In gabagool.py - _execute_arbitrage()

async def _execute_arbitrage(self, market, opportunity) -> Optional[TradeResult]:
    # ... execute trade ...
    result = await self._client.execute_arbitrage_trade(...)

    # ALWAYS record what actually filled, even if partial
    if result.actual_yes_shares > 0 or result.actual_no_shares > 0:
        trade_id = await self._record_trade(
            market=market,
            yes_price=opportunity.yes_price,
            no_price=opportunity.no_price,
            yes_cost=result.actual_yes_cost,
            no_cost=result.actual_no_cost,
            yes_shares=result.actual_yes_shares,
            no_shares=result.actual_no_shares,
            expected_profit=self._calculate_profit(result),
            hedge_ratio=result.hedge_ratio,
            dry_run=self.gabagool_config.dry_run,
        )

        if result.hedge_ratio < 0.8:
            log.warning(
                "Partial fill recorded",
                trade_id=trade_id,
                hedge_ratio=f"{result.hedge_ratio:.1%}",
                yes_filled=result.actual_yes_shares,
                no_filled=result.actual_no_shares,
            )

    return TradeResult(...)
```

**Validation:**
- [ ] Partial fills appear in database with correct hedge_ratio
- [ ] Dashboard shows partial fills with warning indicator
- [ ] Logs show fill details for debugging

---

### Phase 4: Fix Unwind Logic

**Problem:** Bot tries to "unwind" MATCHED orders (impossible) and fails with 400 error.

**Files to modify:**
- `src/client/polymarket.py` - `execute_arbitrage_trade()` unwind section

**Current broken logic:**
```python
if yes_matched and not no_matched:
    # Try to SELL yes_shares to unwind - but this can fail too!
    await self._unwind_position(yes_token, yes_shares)  # 400 error
```

**Fix:**
```python
async def execute_arbitrage_trade(...) -> DualLegResult:
    # Execute both legs in parallel
    yes_result, no_result = await asyncio.gather(
        self._place_order(yes_token, yes_limit, yes_shares, "BUY"),
        self._place_order(no_token, no_limit, no_shares, "BUY"),
    )

    # Determine what actually filled
    yes_filled = yes_result.status == "MATCHED"
    no_filled = no_result.status == "MATCHED"

    # Handle partial fill scenarios
    if yes_filled and no_filled:
        # Perfect - both legs filled
        return DualLegResult(
            success=True,
            actual_yes_shares=yes_result.filled_shares,
            actual_no_shares=no_result.filled_shares,
            hedge_ratio=1.0,
            ...
        )

    elif yes_filled and not no_filled:
        # YES filled, NO is LIVE on book
        # Cancel the LIVE NO order (don't try to unwind MATCHED YES)
        if no_result.status == "LIVE":
            await self._cancel_order(no_result.order_id)

        # Return partial result - strategy will record this
        return DualLegResult(
            success=False,  # Not a successful arb
            actual_yes_shares=yes_result.filled_shares,
            actual_no_shares=0.0,
            actual_yes_cost=yes_result.cost,
            actual_no_cost=0.0,
            yes_status="MATCHED",
            no_status="CANCELLED",
            hedge_ratio=0.0,
            error="Partial fill: YES matched, NO cancelled",
        )

    elif no_filled and not yes_filled:
        # NO filled, YES is LIVE - same logic, reversed
        if yes_result.status == "LIVE":
            await self._cancel_order(yes_result.order_id)

        return DualLegResult(
            success=False,
            actual_yes_shares=0.0,
            actual_no_shares=no_result.filled_shares,
            actual_yes_cost=0.0,
            actual_no_cost=no_result.cost,
            yes_status="CANCELLED",
            no_status="MATCHED",
            hedge_ratio=0.0,
            error="Partial fill: NO matched, YES cancelled",
        )

    else:
        # Neither filled - both LIVE or both failed
        # Cancel both LIVE orders
        await self._cancel_all_live_orders([yes_result, no_result])

        return DualLegResult(
            success=False,
            actual_yes_shares=0.0,
            actual_no_shares=0.0,
            hedge_ratio=0.0,
            error="No fills - both orders cancelled",
        )
```

**Validation:**
- [ ] No more 400 errors in logs from unwind attempts
- [ ] LIVE orders are properly cancelled
- [ ] Partial fills correctly recorded

---

### Phase 5: Pre-Trade Liquidity Check

**Problem:** Bot attempts trades when one side has no liquidity.

**Files to modify:**
- `src/strategies/gabagool.py` - Add liquidity check before execution
- `src/monitoring/order_book.py` - Add depth query method

**Implementation:**
```python
# In gabagool.py

async def _execute_arbitrage(self, market, opportunity) -> Optional[TradeResult]:
    # Check liquidity BEFORE attempting trade
    yes_depth = self._tracker.get_depth(market.yes_token_id, "BUY")
    no_depth = self._tracker.get_depth(market.no_token_id, "BUY")

    required_yes = yes_amount / opportunity.yes_price
    required_no = no_amount / opportunity.no_price

    # Require 150% of needed shares available
    if yes_depth < required_yes * 1.5:
        log.info(
            "Skipping: insufficient YES liquidity",
            required=required_yes,
            available=yes_depth,
        )
        return None

    if no_depth < required_no * 1.5:
        log.info(
            "Skipping: insufficient NO liquidity",
            required=required_no,
            available=no_depth,
        )
        return None

    # Proceed with execution
    ...
```

**Validation:**
- [ ] Trades with insufficient liquidity are skipped
- [ ] Logs show liquidity check results
- [ ] Fill rate improves

---

### Phase 6: Dashboard Read-Only Mode

**Problem:** Dashboard has persistence logic mixed with display logic.

**Files to modify:**
- `src/dashboard.py` - Remove all database writes
- `src/strategies/gabagool.py` - Add event emission for dashboard

**Step 6.1: Create trade event system**

```python
# In src/events.py (new file)

from typing import Callable, Dict, Any, List
import asyncio

class TradeEventEmitter:
    """Simple event emitter for trade updates."""

    def __init__(self):
        self._listeners: List[Callable] = []

    def subscribe(self, callback: Callable) -> None:
        """Subscribe to trade events."""
        self._listeners.append(callback)

    def unsubscribe(self, callback: Callable) -> None:
        """Unsubscribe from trade events."""
        self._listeners.remove(callback)

    async def emit(self, event_type: str, data: Dict[str, Any]) -> None:
        """Emit event to all listeners."""
        for listener in self._listeners:
            try:
                if asyncio.iscoroutinefunction(listener):
                    await listener(event_type, data)
                else:
                    listener(event_type, data)
            except Exception as e:
                log.error("Event listener error", error=str(e))

# Global emitter
trade_events = TradeEventEmitter()
```

**Step 6.2: Strategy emits events**

```python
# In gabagool.py

from ..events import trade_events

async def _record_trade(self, ...) -> str:
    # Save to database
    await db.save_trade(...)

    # Emit event for dashboard
    await trade_events.emit("trade_created", {
        "trade_id": trade_id,
        "asset": market.asset,
        "yes_cost": yes_cost,
        "no_cost": no_cost,
        "hedge_ratio": hedge_ratio,
        "dry_run": dry_run,
        # ... other display fields
    })

    return trade_id
```

**Step 6.3: Dashboard subscribes to events**

```python
# In dashboard.py

from .events import trade_events

async def init_dashboard():
    """Initialize dashboard and subscribe to trade events."""
    trade_events.subscribe(on_trade_event)

async def on_trade_event(event_type: str, data: dict) -> None:
    """Handle trade events from strategy."""
    if event_type == "trade_created":
        # Update in-memory state for display
        trade = {
            "id": data["trade_id"],
            "asset": data["asset"],
            # ... map to display format
        }
        trades.append(trade)

        # Broadcast to SSE clients
        await broadcast({"trades": [trade]})
```

**Validation:**
- [ ] Dashboard has no direct database calls
- [ ] Trades appear in dashboard via events
- [ ] Dashboard can restart without losing strategy state

---

## Testing Plan

### Unit Tests
- [ ] `test_dynamic_pricing.py` - Verify limit prices match market + slippage
- [ ] `test_partial_fills.py` - Verify partial fills are recorded correctly
- [ ] `test_unwind_logic.py` - Verify LIVE orders are cancelled, not unwound
- [ ] `test_trade_events.py` - Verify event emission and subscription

### Integration Tests (Dry Run)
- [ ] Run bot in dry run mode for 24 hours
- [ ] Verify all trades are recorded in database
- [ ] Verify dashboard shows dry run trades
- [ ] Verify no actual orders are placed

### Live Testing (Small Stakes)
- [ ] Set max_trade_size to $1
- [ ] Run for 2-4 hours
- [ ] Verify Polymarket transaction history matches database
- [ ] Verify hedge ratios are acceptable (>80%)

---

## Rollout Plan

1. **Implement Phase 1-2** (dynamic pricing + persistence architecture)
2. **Test in dry run** for 24-48 hours
3. **Implement Phase 3-4** (partial fills + unwind logic)
4. **Test in dry run** for 24 hours
5. **Implement Phase 5-6** (liquidity check + dashboard read-only)
6. **Final dry run test** for 24 hours
7. **Go live with $1 max trade size**
8. **Monitor for 48 hours**
9. **Gradually increase trade size**

---

## Success Criteria

- [ ] Hedge ratio > 80% on 90%+ of trades
- [ ] All trades recorded in database (dry run and live)
- [ ] Dashboard displays trades without owning persistence
- [ ] No 400 errors from unwind attempts
- [ ] Liquidity-gated trades reduce partial fills

---

## Files Changed Summary

| File | Changes |
|------|---------|
| `src/client/polymarket.py` | Dynamic pricing, fix unwind logic, return actual fills |
| `src/strategies/gabagool.py` | Own persistence, emit events, liquidity check |
| `src/persistence.py` | Add new fields (shares, hedge_ratio, status) |
| `src/dashboard.py` | Remove persistence, subscribe to events |
| `src/events.py` | New file - event emitter |
| `tests/test_dynamic_pricing.py` | New test file |
| `tests/test_partial_fills.py` | New test file |
| `tests/test_unwind_logic.py` | New test file |
