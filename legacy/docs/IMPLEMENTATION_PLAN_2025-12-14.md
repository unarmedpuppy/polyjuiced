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

**Problem:** Bot uses the same limit price ($0.53) for both legs due to redundant price fetching. This causes one leg to fill while the other sits on the book.

**Root Cause (see [STRATEGY_ARCHITECTURE.md](./STRATEGY_ARCHITECTURE.md#-critical-bug-identified-2025-12-14)):**

The `execute_dual_leg_order_parallel()` function at `polymarket.py:896` has a subtle bug:

1. **Strategy** (gabagool.py) calls execution with amounts but **NOT prices**
2. **Execution** (polymarket.py) fetches order book prices (correct so far)
3. **BUT** the inner `place_order_sync()` function calls `get_price()` **AGAIN** (line 1022)
4. This returns the best ask for each token (~$0.50 for both)
5. Both get +3¢ slippage → both become $0.53

**Files to modify:**
- `src/client/polymarket.py` - `execute_dual_leg_order_parallel()` and `place_order_sync()`
- `src/strategies/gabagool.py` - Pass prices through to execution

**Current code pattern (polymarket.py:1014-1030):**
```python
def place_order_sync(token_id, amount_usd, label, price_hint):
    # PROBLEM: Re-fetches price instead of using price_hint!
    try:
        price = self.get_price(token_id, "buy")  # ← REDUNDANT API CALL
    except Exception:
        price = price_hint  # Only used on error

    # Both legs get same price ~$0.50 → both get $0.53 limit
    limit_price_d = min(price_d + Decimal("0.03"), Decimal("0.99"))
```

#### ⚠️ CRITICAL: Slippage Strategy

**The old approach was fundamentally broken:**
- Adding 3¢ slippage to each leg = 6¢ total slippage
- On a 2¢ arbitrage opportunity, this turns profit into loss!
- Example: YES=$0.49, NO=$0.49 (spread=2¢)
  - Old: Buy YES@$0.52 + NO@$0.52 = $1.04 total → **GUARANTEED LOSS**
  - Correct: Buy YES@$0.49 + NO@$0.49 = $0.98 total → **2¢ profit per share**

**New slippage philosophy:**
1. **NO slippage** - Use exact opportunity prices as limit prices
2. If we can't fill at the detected price, **don't take the trade**
3. The goal is **precision execution**, not guaranteed fills
4. A missed opportunity is better than a losing trade

**Why this works:**
- We're buying at the ask price (what sellers are offering)
- If our limit = ask, we should fill immediately if liquidity exists
- If liquidity disappears before our order arrives, we simply don't fill
- FOK (Fill-or-Kill) order type ensures atomicity

---

**Fix: Pass prices through from strategy with ZERO slippage**

Modify `execute_dual_leg_order_parallel()` signature:

```python
# polymarket.py
async def execute_dual_leg_order_parallel(
    self,
    yes_token_id: str,
    no_token_id: str,
    yes_amount_usd: float,
    no_amount_usd: float,
    yes_price: float,  # Exact price from opportunity detection
    no_price: float,   # Exact price from opportunity detection
    timeout_seconds: float = 5.0,
    max_liquidity_consumption_pct: float = 0.50,
    condition_id: str = "",
    asset: str = "",
) -> Dict[str, Any]:
    """Execute YES and NO orders in PARALLEL for true atomic execution.

    IMPORTANT: Prices are exact limit prices. NO slippage is added.
    If we can't fill at these prices, we don't take the trade.
    The goal is precision, not guaranteed fills.
    """

    # Use prices EXACTLY as provided - no slippage!
    yes_limit = yes_price
    no_limit = no_price

    # Validate the arbitrage still makes sense
    total_cost = yes_limit + no_limit
    if total_cost >= 1.0:
        log.warning(
            "Arbitrage no longer valid - total cost >= $1.00",
            yes_limit=yes_limit,
            no_limit=no_limit,
            total=total_cost,
        )
        return {"success": False, "error": "Arbitrage invalidated - prices sum to >= $1.00"}

    log.info(
        "Executing arbitrage with EXACT pricing (no slippage)",
        yes_limit=f"${yes_limit:.2f}",
        no_limit=f"${no_limit:.2f}",
        total_cost=f"${total_cost:.2f}",
        expected_profit_per_share=f"${1.0 - total_cost:.2f}",
    )

    # ... rest of execution with FOK orders
```

Update gabagool.py to pass exact prices:

```python
# gabagool.py around line 980
api_result = await self.client.execute_dual_leg_order_parallel(
    yes_token_id=market.yes_token_id,
    no_token_id=market.no_token_id,
    yes_amount_usd=yes_amount,
    no_amount_usd=no_amount,
    yes_price=opportunity.yes_price,  # EXACT price, no slippage
    no_price=opportunity.no_price,    # EXACT price, no slippage
    timeout_seconds=self.gabagool_config.parallel_fill_timeout_seconds,
    max_liquidity_consumption_pct=self.gabagool_config.max_liquidity_consumption_pct,
    condition_id=market.condition_id,
    asset=market.asset,
)
```

Update `place_order_sync()` to use price directly:

```python
# polymarket.py - place_order_sync inner function
def place_order_sync(token_id: str, amount_usd: float, label: str, limit_price: float) -> Dict[str, Any]:
    """Place order at EXACT limit price. No slippage, no re-fetching."""
    from decimal import Decimal, ROUND_DOWN

    # Use the limit price EXACTLY as provided
    price_d = Decimal(str(limit_price))
    amount_d = Decimal(str(amount_usd))

    # DO NOT add slippage - price is already the exact limit we want
    # DO NOT call get_price() - we already have the price

    shares_d = (amount_d / price_d).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

    log.info(
        f"Placing {label} order at EXACT limit",
        limit_price=f"{float(price_d):.2f}",
        shares=f"{float(shares_d):.2f}",
    )

    # ... rest of order creation
```

**Validation:**
- [ ] Prices logged match detected opportunity prices EXACTLY (no slippage added)
- [ ] Total cost (yes_price + no_price) is validated < $1.00 before execution
- [ ] Expected profit per share is logged and matches spread
- [ ] Test with dry run showing correct prices
- [ ] No magic numbers (0.53, 0.03, etc.) in pricing code path
- [ ] FOK orders used to ensure atomicity (no partial fills sitting on book)

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

## Phase 7: Record Liquidity Depth with Every Trade

**Problem:** We have no visibility into available liquidity at trade time.

**Files to modify:**
- `src/persistence.py` - Add liquidity fields to trades table
- `src/strategies/gabagool.py` - Capture and record liquidity snapshot
- `src/monitoring/order_book.py` - Add method to get depth at price

**Schema additions:**
```sql
-- Add to trades table
yes_liquidity_at_price REAL,  -- Shares available at our limit price (YES side)
no_liquidity_at_price REAL,   -- Shares available at our limit price (NO side)
yes_book_depth_total REAL,    -- Total YES order book depth
no_book_depth_total REAL,     -- Total NO order book depth
```

**Implementation:**
```python
# In gabagool.py - before executing trade

async def _capture_liquidity_snapshot(
    self,
    market: Market15Min,
    yes_limit_price: float,
    no_limit_price: float,
) -> dict:
    """Capture liquidity available at our intended prices."""
    yes_at_price = self._tracker.get_depth_at_price(
        market.yes_token_id, "BUY", yes_limit_price
    )
    no_at_price = self._tracker.get_depth_at_price(
        market.no_token_id, "BUY", no_limit_price
    )
    yes_total = self._tracker.get_total_depth(market.yes_token_id, "BUY")
    no_total = self._tracker.get_total_depth(market.no_token_id, "BUY")

    return {
        "yes_liquidity_at_price": yes_at_price,
        "no_liquidity_at_price": no_at_price,
        "yes_book_depth_total": yes_total,
        "no_book_depth_total": no_total,
    }
```

**Validation:**
- [ ] Every trade record includes liquidity snapshot
- [ ] Can analyze correlation between liquidity and fill success
- [ ] Dashboard shows liquidity warnings

---

## Phase 8: Comprehensive Regression Test Suite

**Problem:** Hardcoded $0.53 price made it to production - testing is inadequate.

**New test files:**
- `tests/test_pricing_logic.py` - Ensure prices are never hardcoded
- `tests/test_execution_flow.py` - Full execution path testing
- `tests/test_order_parameters.py` - Validate all order parameters
- `tests/test_invariants.py` - Assert business logic invariants

### 8.1 Pricing Logic Tests

```python
# tests/test_pricing_logic.py

import pytest
from src.client.polymarket import PolymarketClient

class TestPricingLogic:
    """Ensure prices are NEVER hardcoded and always derived from market data."""

    def test_limit_price_derived_from_market_price(self):
        """Limit price must be based on actual market price + slippage."""
        market_price = 0.35
        slippage = 0.02

        limit_price = calculate_limit_price(market_price, slippage, "BUY")

        # Must be market price + slippage, NOT a hardcoded value
        assert limit_price == pytest.approx(0.37, abs=0.001)
        assert limit_price != 0.53  # Explicitly check not hardcoded

    def test_yes_and_no_prices_differ(self):
        """YES and NO legs must use different prices based on their markets."""
        yes_market = 0.30
        no_market = 0.68

        yes_limit = calculate_limit_price(yes_market, 0.02, "BUY")
        no_limit = calculate_limit_price(no_market, 0.02, "BUY")

        assert yes_limit != no_limit
        assert yes_limit == pytest.approx(0.32, abs=0.001)
        assert no_limit == pytest.approx(0.70, abs=0.001)

    def test_no_magic_numbers_in_pricing(self):
        """Scan codebase for hardcoded price values."""
        import re
        from pathlib import Path

        # Files to scan
        src_files = Path("src").rglob("*.py")

        magic_price_pattern = re.compile(
            r'price\s*=\s*(0\.\d{2})\b',  # price = 0.XX
            re.IGNORECASE
        )

        violations = []
        for filepath in src_files:
            content = filepath.read_text()
            matches = magic_price_pattern.findall(content)
            for match in matches:
                # Allow 0.01, 0.02 (slippage), 0.99 (max), 0.00 (min)
                if match not in ('0.01', '0.02', '0.99', '0.00'):
                    violations.append(f"{filepath}: hardcoded price {match}")

        assert not violations, f"Found hardcoded prices: {violations}"

    def test_price_bounds(self):
        """Prices must be within valid Polymarket range."""
        for market_price in [0.01, 0.25, 0.50, 0.75, 0.99]:
            limit = calculate_limit_price(market_price, 0.02, "BUY")
            assert 0.01 <= limit <= 0.99, f"Invalid limit price: {limit}"
```

### 8.2 Execution Flow Tests

```python
# tests/test_execution_flow.py

import pytest
from unittest.mock import AsyncMock, MagicMock
from src.strategies.gabagool import GabagoolStrategy

class TestExecutionFlow:
    """Test the complete execution path from opportunity to trade record."""

    @pytest.fixture
    def strategy(self):
        """Create strategy with mocked dependencies."""
        strategy = GabagoolStrategy(config=mock_config)
        strategy._client = AsyncMock()
        strategy._db = AsyncMock()
        return strategy

    async def test_opportunity_to_execution_uses_correct_prices(self, strategy):
        """Verify prices flow correctly from opportunity to order."""
        opportunity = MockOpportunity(
            yes_price=0.30,
            no_price=0.68,
            spread_cents=2.0,
        )

        await strategy._execute_arbitrage(mock_market, opportunity)

        # Verify the client was called with prices derived from opportunity
        call_args = strategy._client.execute_arbitrage_trade.call_args
        assert call_args.kwargs['yes_price'] == 0.30
        assert call_args.kwargs['no_price'] == 0.68

    async def test_partial_fill_is_recorded(self, strategy):
        """Partial fills must be recorded, not silently dropped."""
        strategy._client.execute_arbitrage_trade.return_value = DualLegResult(
            success=False,
            actual_yes_shares=10.0,
            actual_no_shares=0.0,  # NO leg failed
            hedge_ratio=0.0,
        )

        await strategy._execute_arbitrage(mock_market, mock_opportunity)

        # Must still record the partial fill
        strategy._db.save_trade.assert_called_once()
        call_args = strategy._db.save_trade.call_args
        assert call_args.kwargs['yes_shares'] == 10.0
        assert call_args.kwargs['no_shares'] == 0.0
        assert call_args.kwargs['hedge_ratio'] == 0.0

    async def test_liquidity_snapshot_captured(self, strategy):
        """Every trade must capture liquidity at execution time."""
        await strategy._execute_arbitrage(mock_market, mock_opportunity)

        call_args = strategy._db.save_trade.call_args
        assert 'yes_liquidity_at_price' in call_args.kwargs
        assert 'no_liquidity_at_price' in call_args.kwargs
```

### 8.3 Business Invariants Tests

```python
# tests/test_invariants.py

import pytest

class TestBusinessInvariants:
    """Test invariants that must ALWAYS hold true."""

    def test_arbitrage_requires_spread(self):
        """Cannot execute arbitrage without positive spread."""
        with pytest.raises(ValueError, match="spread"):
            validate_arbitrage_opportunity(yes_price=0.50, no_price=0.52)

    def test_total_cost_less_than_one_dollar(self):
        """Arbitrage only works if YES + NO < $1.00."""
        # Valid
        assert is_valid_arbitrage(yes=0.30, no=0.68)  # 0.98 < 1.00

        # Invalid
        assert not is_valid_arbitrage(yes=0.50, no=0.52)  # 1.02 > 1.00

    def test_shares_calculated_for_equal_pairs(self):
        """Must buy equal shares of YES and NO for true arbitrage."""
        yes_shares, no_shares = calculate_arbitrage_shares(
            budget=10.0,
            yes_price=0.30,
            no_price=0.68,
        )
        assert yes_shares == pytest.approx(no_shares, rel=0.01)

    def test_expected_profit_positive(self):
        """Expected profit must be positive for valid arbitrage."""
        profit = calculate_expected_profit(
            yes_price=0.30,
            no_price=0.68,
            shares=10.0,
        )
        assert profit > 0

    def test_dry_run_never_calls_exchange(self, mocker):
        """Dry run must NEVER make real API calls."""
        mock_post = mocker.patch('httpx.AsyncClient.post')

        strategy = GabagoolStrategy(dry_run=True)
        await strategy._execute_arbitrage(...)

        # post() should never be called in dry run
        mock_post.assert_not_called()
```

---

## Phase 9: Strategy Code Path Documentation

**Problem:** No visual documentation of how the strategy executes.

**Create:** `docs/STRATEGY_ARCHITECTURE.md` - Living document updated with every code change.

**Contents:**
1. High-level flow diagram
2. Function-by-function documentation
3. Data flow diagrams
4. State machine diagrams
5. Error handling paths

See [STRATEGY_ARCHITECTURE.md](./STRATEGY_ARCHITECTURE.md) for full documentation.

**Maintenance rule:** Every PR that touches strategy code MUST update this document.

---

## Phase 10: Code Audit Checklist

**Problem:** How did $0.53 get into production?

### Immediate Audit Actions

1. **Grep for magic numbers:**
   ```bash
   grep -rn "0\.[0-9][0-9]" src/ --include="*.py" | grep -v "0.01\|0.02\|0.99\|0.00"
   ```

2. **Review all hardcoded values:**
   ```bash
   grep -rn "price.*=" src/ --include="*.py"
   grep -rn "amount.*=" src/ --include="*.py"
   grep -rn "shares.*=" src/ --include="*.py"
   ```

3. **Check for TODO/FIXME/HACK comments:**
   ```bash
   grep -rn "TODO\|FIXME\|HACK\|XXX" src/ --include="*.py"
   ```

4. **Review recent commits for suspicious patterns:**
   ```bash
   git log --oneline -20 --all -- src/client/polymarket.py
   git log -p --since="2025-12-01" -- src/client/polymarket.py
   ```

### Pre-Commit Hooks

Add `.pre-commit-config.yaml`:
```yaml
repos:
  - repo: local
    hooks:
      - id: no-magic-prices
        name: Check for hardcoded prices
        entry: python scripts/check_magic_numbers.py
        language: python
        files: \.py$

      - id: test-pricing
        name: Run pricing tests
        entry: pytest tests/test_pricing_logic.py -v
        language: python
        pass_filenames: false
```

### CI Pipeline Additions

```yaml
# Add to CI workflow
- name: Run invariant tests
  run: pytest tests/test_invariants.py -v

- name: Check for magic numbers
  run: python scripts/check_magic_numbers.py

- name: Verify strategy documentation is current
  run: python scripts/check_docs_current.py
```

---

## End-to-End Trade Scenario Tests

These scenarios should be executed against the strategy to validate complete execution paths.

### Scenario 1: Perfect Arbitrage Fill
```
Setup:
- BTC market with YES=$0.48, NO=$0.49 (spread=3¢)
- Both legs have 100+ shares liquidity
- Budget: $20

Expected:
- Both FOK orders fill at exact prices (no slippage)
- Trade recorded with execution_status='full_fill'
- hedge_ratio=1.0
- yes_shares ≈ no_shares ≈ 20.6
- Expected profit: $0.62
```

### Scenario 2: FOK Rejection (Liquidity Disappeared)
```
Setup:
- ETH market with YES=$0.30, NO=$0.68 (spread=2¢)
- Only 5 shares liquidity on YES side
- Budget: $20 (would need ~10 shares each side)

Expected:
- FOK orders rejected (insufficient liquidity)
- Trade NOT recorded (no fill = no trade)
- No partial exposure
```

### Scenario 3: One-Leg Partial Fill (if FOK fails atomically)
```
Setup:
- SOL market with YES=$0.40, NO=$0.58 (spread=2¢)
- YES fills 20 shares, NO fills 0 shares
- (Note: With FOK this shouldn't happen, but test for safety)

Expected:
- Trade recorded with execution_status='one_leg_only'
- hedge_ratio=0
- yes_shares=20, no_shares=0
- Dashboard shows PARTIAL FILL alert
```

### Scenario 4: Dry Run Recording
```
Setup:
- GABAGOOL_DRY_RUN=true
- BTC market with valid arbitrage opportunity

Expected:
- Trade recorded with dry_run=True
- execution_status='full_fill' (simulated)
- yes_order_status='SIMULATED', no_order_status='SIMULATED'
- No real API calls made
```

### Scenario 5: Hedge Ratio Below Minimum
```
Setup:
- min_hedge_ratio=0.80 (config)
- BTC market executes with YES=20, NO=10 (hedge_ratio=0.5)

Expected:
- Trade rejected due to poor hedge
- Error logged: "Hedge ratio 50% below minimum 80%"
- If critical_hedge_ratio breached, consider circuit breaker
```

### Scenario 6: Price Invalidation Before Execution
```
Setup:
- Opportunity detected at YES=$0.48, NO=$0.49 (spread=3¢)
- Prices change before execution to YES=$0.52, NO=$0.50 (total=$1.02)

Expected:
- Pre-validation catches total >= $1.00
- Trade rejected: "Arbitrage invalidated - prices sum to >= $1.00"
- No orders placed
```

### Test Execution
```bash
# Run all Phase 2 tests
pytest tests/test_phase2_persistence.py -v

# Run end-to-end scenarios (when implemented)
pytest tests/test_e2e_scenarios.py -v

# Run all regression tests
pytest tests/test_phase1_regressions.py tests/test_phase2_persistence.py -v
```

---

## Files Changed Summary

| File | Changes |
|------|---------|
| `src/client/polymarket.py` | Dynamic pricing, fix unwind logic, return actual fills |
| `src/strategies/gabagool.py` | Own persistence via _record_trade(), emit events, liquidity check |
| `src/persistence.py` | Add new fields (shares, hedge_ratio, execution_status, liquidity), migration |
| `src/dashboard.py` | Remove persistence, dashboard is READ-ONLY |
| `src/events.py` | New file - event emitter (Phase 6) |
| `docs/STRATEGY_ARCHITECTURE.md` | Living architecture documentation |
| `tests/test_phase1_regressions.py` | Phase 1 regression tests |
| `tests/test_phase2_persistence.py` | Phase 2 persistence/recording tests |
| `tests/test_e2e_scenarios.py` | End-to-end trade scenario tests |
| `scripts/check_magic_numbers.py` | Audit script |
| `.pre-commit-config.yaml` | Pre-commit hooks |
