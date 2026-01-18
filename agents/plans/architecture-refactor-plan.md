# Polymarket Bot (Gabagool) - Architecture Refactoring Plan

**Date:** 2026-01-17
**Author:** Architectural Audit
**Status:** Ready for Review

---

## Executive Summary

This document provides a comprehensive architectural audit of the Polymarket trading bot (codebase: "polyjuiced", strategy name: "gabagool"). The bot is a sophisticated automated trading system targeting 15-minute up/down prediction markets for BTC, ETH, and SOL on Polymarket.

### Current State Assessment

**Strengths:**
- Well-documented strategy architecture (STRATEGY_ARCHITECTURE.md is excellent)
- Solid domain separation (`client/`, `strategies/`, `monitoring/`, `risk/`, `liquidity/`)
- Comprehensive Prometheus metrics for observability
- Settlement queue with persistence for bot restarts
- Event-driven architecture for dashboard updates
- Good test coverage for critical paths

**Critical Issues:**
1. **God Class Problem**: `gabagool.py` is 3,405 lines with 30+ methods mixing strategy logic, execution, persistence, and UI updates
2. **Dashboard Coupling**: Strategies directly import and call dashboard functions (tight coupling)
3. **WebSocket Fragility**: Limited reconnection logic and error handling
4. **Metrics Module Bloat**: `metrics.py` is 1,114 lines with global state
5. **Configuration Sprawl**: Multiple config classes with overlapping responsibilities

### Recommended Action

Proceed with **incremental refactoring** in priority order, starting with P0 items that directly impact reliability and maintainability. Avoid "big bang" rewrites.

---

## Table of Contents

1. [Current Architecture](#1-current-architecture)
2. [Component Breakdown](#2-component-breakdown)
3. [Architectural Issues](#3-architectural-issues)
4. [Code Quality Issues](#4-code-quality-issues)
5. [Refactoring Plan](#5-refactoring-plan)
6. [Proposed New Architecture](#6-proposed-new-architecture)
7. [Migration Strategy](#7-migration-strategy)

---

## 1. Current Architecture

### High-Level Data Flow

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              MAIN ENTRY (main.py)                                │
│  - Initializes all components                                                    │
│  - Starts dashboard, metrics server, and strategy                               │
│  - Handles graceful shutdown                                                     │
└───────────────────────────────────────┬─────────────────────────────────────────┘
                                        │
                    ┌───────────────────┼───────────────────┐
                    │                   │                   │
                    ▼                   ▼                   ▼
            ┌───────────────┐   ┌───────────────┐   ┌───────────────┐
            │   Dashboard   │   │ MetricsServer │   │   Strategy    │
            │  (dashboard)  │   │   (aiohttp)   │   │  (gabagool)   │
            └───────────────┘   └───────────────┘   └───────┬───────┘
                    ▲                                       │
                    │ Events                                │
                    │                                       │
┌───────────────────┴───────────────────────────────────────┼─────────────────────┐
│                         STRATEGY LAYER                    │                      │
│                                                           │                      │
│  ┌─────────────────┐    ┌─────────────────┐    ┌─────────▼─────────┐           │
│  │  MarketFinder   │───▶│ OrderBookTracker│───▶│  GabagoolStrategy │           │
│  │ (market_finder) │    │  (order_book)   │    │    (gabagool)     │           │
│  └─────────────────┘    └─────────────────┘    └─────────┬─────────┘           │
│                                                           │                      │
└───────────────────────────────────────────────────────────┼─────────────────────┘
                                                            │
┌───────────────────────────────────────────────────────────┼─────────────────────┐
│                         CLIENT LAYER                      │                      │
│                                                           │                      │
│  ┌─────────────────┐    ┌─────────────────┐    ┌─────────▼─────────┐           │
│  │   GammaClient   │    │PolymarketWebSocket│   │PolymarketClient  │           │
│  │    (gamma)      │    │   (websocket)   │    │  (polymarket)     │           │
│  └─────────────────┘    └─────────────────┘    └───────────────────┘           │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌──────────────────────────────────────────────────────────────────────────────────┐
│                         PERSISTENCE LAYER                                         │
│                                                                                   │
│  ┌─────────────────────────────────────────────────────────────────────────────┐ │
│  │                         Database (persistence.py)                           │ │
│  │  Tables: trades, markets, logs, daily_stats, fill_records,                 │ │
│  │          liquidity_snapshots, trade_telemetry, rebalance_trades,           │ │
│  │          settlement_queue, circuit_breaker_state, realized_pnl_ledger      │ │
│  └─────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                   │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### File Size Analysis

| File | Lines | Purpose | Complexity |
|------|-------|---------|------------|
| `gabagool.py` | 3,405 | Main strategy (GOD CLASS) | High |
| `dashboard.py` | 2,821 | Web UI (~2000 lines HTML/CSS) | Medium |
| `persistence.py` | 2,252 | SQLite database operations | Medium |
| `position_manager.py` | 1,100+ | Position tracking and rebalancing | Medium |
| `metrics.py` | 1,114 | Prometheus metrics definitions | Low |
| `config.py` | 580+ | Configuration management | Low |
| `polymarket.py` | 800+ | CLOB API client | Medium |
| `websocket.py` | 400+ | WebSocket client | Medium |

---

## 2. Component Breakdown

### 2.1 Strategy Layer

#### GabagoolStrategy (`strategies/gabagool.py`)

**Current Responsibilities (TOO MANY):**
1. Market opportunity detection
2. Position sizing calculations
3. Trade execution orchestration
4. Parallel order placement
5. Partial fill handling
6. Settlement queue management
7. Blackout window checking
8. Dashboard state updates
9. Metrics updates
10. Database persistence
11. Liquidity analysis
12. Gradual entry logic
13. Circuit breaker integration

**Key Methods:**
- `start()` / `stop()` - Lifecycle management
- `_process_opportunity_queue()` - Opportunity handling loop
- `_execute_trade()` - Trade execution
- `_execute_gradual_entry()` - Multi-tranche entry
- `_track_position()` - Settlement queue
- `_record_trade()` - Database persistence
- `_check_settlement()` - Claim winnings
- `_adjust_for_liquidity()` - Pre-trade liquidity check

#### Other Strategies

- `vol_happens.py` - Mean reversion strategy (disabled)
- `near_resolution.py` - Late-stage resolution betting (disabled)
- `base.py` - Base strategy class (minimal)

### 2.2 Client Layer

#### PolymarketClient (`client/polymarket.py`)

**Responsibilities:**
- Order placement (FOK and GTC)
- Order cancellation
- Balance queries
- Order book fetching
- Position queries

**Good Patterns:**
- Thread pool for sync SDK calls in async context
- Retry logic with configurable attempts

**Issues:**
- `execute_dual_leg_order_parallel()` is 200+ lines
- Mixed sync/async patterns (SDK is synchronous)

#### PolymarketWebSocket (`client/websocket.py`)

**Responsibilities:**
- Real-time order book streaming
- Subscription management
- Reconnection logic

**Issues:**
- Basic reconnection (exponential backoff exists)
- No heartbeat mechanism
- No connection health monitoring

#### GammaClient (`client/gamma.py`)

**Responsibilities:**
- Market discovery via Gamma API
- 15-minute market slot calculation

### 2.3 Monitoring Layer

#### OrderBookTracker (`monitoring/order_book.py`)

**Responsibilities:**
- Maintain real-time best bid/ask
- Detect arbitrage opportunities
- Emit callbacks to strategy

**Good Patterns:**
- Clean separation from strategy
- Event-based opportunity notification

#### MarketFinder (`monitoring/market_finder.py`)

**Responsibilities:**
- Discover active 15-minute markets
- Cache market data
- Parse market slugs

### 2.4 Risk Layer

#### CircuitBreaker (`risk/circuit_breaker.py`)

**Responsibilities:**
- Track consecutive failures
- Monitor daily loss
- Adjust position sizing multiplier

**Good Patterns:**
- Multi-level states (NORMAL, WARNING, CAUTION, HALT)
- Clean API for pre/post trade checks

#### PositionSizer (`risk/position_sizing.py`)

**Responsibilities:**
- Calculate optimal position sizes
- Kelly criterion implementation
- Spread-scaled sizing

### 2.5 Dashboard

#### Dashboard (`dashboard.py`)

**Architecture:**
- Single-page HTML with embedded CSS/JS
- Server-Sent Events (SSE) for real-time updates
- In-memory state with deque-based history

**Responsibilities:**
- Serve web UI
- Stream state updates
- Display trades, stats, markets

**Coupling Issues:**
- Strategies directly call `add_log()`, `add_trade()`, `add_decision()`, `update_stats()`
- Dashboard functions are imported globally in strategy files

### 2.6 Metrics

#### Metrics (`metrics.py`)

**Pattern:** Global Prometheus metrics with helper functions

**Issues:**
- 1,114 lines of metric definitions
- Global mutable state (`_fill_counts`, `_pnl_tracking`)
- Helper functions mixed with metric definitions

### 2.7 Persistence

#### Database (`persistence.py`)

**Pattern:** Async SQLite with aiosqlite

**Tables:**
- `trades` - Trade records
- `markets` - Discovered markets
- `logs` - Persistent logs
- `daily_stats` - Daily performance
- `fill_records` - Order fills for slippage analysis
- `liquidity_snapshots` - Order book snapshots
- `trade_telemetry` - Timing telemetry
- `rebalance_trades` - Rebalancing history
- `settlement_queue` - Pending claims
- `circuit_breaker_state` - Persisted risk state
- `realized_pnl_ledger` - P&L audit trail

**Good Patterns:**
- Schema migrations
- Comprehensive indexes
- Async operations with lock

---

## 3. Architectural Issues

### 3.1 Dashboard/Data Feed Coupling

**Current State:**
```python
# gabagool.py - direct dashboard calls
from ..dashboard import add_log, add_trade, add_decision, update_stats

# In strategy methods:
add_log("info", "Trade executed...")
add_decision(asset=..., action="TRADE", ...)
update_stats(daily_pnl=..., opportunities_executed=...)
```

**Problems:**
1. Strategy cannot run without dashboard module being importable
2. Dashboard functions are called synchronously in trade path
3. No abstraction layer for UI updates
4. Testing strategy requires mocking dashboard

**Can the bot run without dashboard?**
- **No** - Strategy imports dashboard functions directly
- Dashboard module must be available even if not started

**Can the dashboard work independently?**
- **Partially** - It can serve the UI but has no data without strategy

### 3.2 Strategy Isolation

**Current State:**
- All strategies share `PolymarketClient`, `Database`, `WebSocket`
- Strategies are enabled/disabled via config flags
- No runtime strategy hot-swapping

**Problems:**
1. `gabagool.py` handles its own persistence (breaks SRP)
2. Each strategy duplicates dashboard call patterns
3. No common execution interface for strategies

**How to enable/disable strategies:**
```python
# config.py
class GabagoolConfig:
    enabled: bool = True

class VolHappensConfig:
    enabled: bool = True

# main.py checks config and instantiates accordingly
```

### 3.3 WebSocket Connection Management

**Current State (`websocket.py`):**
```python
async def _connect(self):
    while self._running:
        try:
            async with websockets.connect(self._url) as ws:
                self._ws = ws
                WEBSOCKET_CONNECTED.set(1)
                await self._handle_messages()
        except Exception as e:
            WEBSOCKET_CONNECTED.set(0)
            WEBSOCKET_RECONNECTS.inc()
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, 60)
```

**Problems:**
1. No heartbeat/ping-pong to detect stale connections
2. Reconnection resets all subscriptions (must resubscribe)
3. No connection health monitoring beyond binary connected/disconnected
4. Market data staleness detection is in `OrderBookTracker` (10s timeout)

**Improvements Needed:**
- Implement heartbeat mechanism
- Track subscription state for proper restore on reconnect
- Add connection latency monitoring
- Implement circuit breaker for repeated connection failures

### 3.4 Trade Execution Brittleness

**Current State:**
- FOK (Fill-or-Kill) orders for atomicity
- Parallel dual-leg execution
- Exception handling in `place_order_sync()`

**Existing Error Handling:**
```python
# place_order_sync catches exceptions and returns error dict
try:
    result = client.post_order(signed_order, orderType=OrderType.FOK)
except Exception as e:
    return {"status": "EXCEPTION", "error": str(e), "size_matched": 0}
```

**Retry Logic:**
- No automatic retries on transient failures
- Failed trades logged but not retried
- Partial fills recorded to settlement queue

**State Management:**
- Trade telemetry tracks timing
- Settlement queue survives restarts
- Circuit breaker tracks consecutive failures

### 3.5 Metrics Reliability

**Current State:**
- Prometheus metrics with global counters/gauges/histograms
- Helper functions maintain session state

**Issues:**
1. Global mutable state (`_fill_counts`, `_pnl_tracking`)
2. State lost on restart (metrics reset)
3. No metric aggregation across restarts
4. Session-scoped metrics vs. persistent metrics confusion

---

## 4. Code Quality Issues

### 4.1 God Class: `GabagoolStrategy`

**Size:** 3,405 lines, 30+ methods

**Violates:**
- Single Responsibility Principle
- Open/Closed Principle (can't extend without modifying)

**Should Be Split Into:**
1. `OpportunityProcessor` - Queue management, validation
2. `TradeExecutor` - Order placement, fill handling
3. `SettlementManager` - Position tracking, claims
4. `StrategyCoordinator` - Orchestrates above components

### 4.2 Code Smells

#### Long Methods
- `_execute_trade()` - 150+ lines
- `_execute_gradual_entry()` - 100+ lines
- `execute_dual_leg_order_parallel()` in polymarket.py - 200+ lines

#### Hardcoded Values
```python
# websocket.py
await asyncio.sleep(5)  # Magic number for subscription batching

# gabagool.py
if opportunity.market.seconds_remaining < 60:  # Magic number

# order_book.py
VALIDITY_SECONDS: float = 30.0  # Should be configurable
```

#### Missing Abstractions
- No `TradeExecutionService` interface
- No `NotificationService` for dashboard/alerts
- No `PositionRepository` abstraction

### 4.3 Circular Dependencies

**Current Import Pattern:**
```
gabagool.py → dashboard.py → (no deps back)
gabagool.py → events.py → (no deps back)
gabagool.py → persistence.py → (no deps back)
```

**Potential Issue:**
- `gabagool.py` imports from too many modules (10+ imports)
- Module coupling is high but not circular

### 4.4 Poor Error Handling Areas

1. **WebSocket Subscription Failures**
   - Subscriptions can fail silently
   - No verification that subscription succeeded

2. **Database Connection Loss**
   - `aiosqlite.connect()` can fail
   - No reconnection logic for database

3. **API Rate Limiting**
   - Basic retry with backoff exists
   - No queuing for rate-limited requests

### 4.5 Missing Abstractions

1. **Notification Service**
   ```python
   # Instead of:
   add_log("info", "Trade executed...")
   add_decision(...)
   update_stats(...)

   # Should be:
   await notification_service.notify(TradeExecutedEvent(...))
   ```

2. **Trade Repository**
   ```python
   # Instead of:
   await self._db.save_arbitrage_trade(...)

   # Should be:
   await trade_repository.save(trade)
   ```

3. **Execution Engine**
   ```python
   # Instead of inline execution logic:
   # Should be:
   result = await execution_engine.execute(order_request)
   ```

---

## 5. Refactoring Plan

### P0: Critical (Do First - Reliability Impact)

#### P0.1: Decouple Dashboard from Strategy

**Current State:**
```python
# gabagool.py
from ..dashboard import add_log, add_trade, add_decision, update_stats
```

**Target State:**
```python
# gabagool.py
from ..events import EventBus

# Usage:
await event_bus.emit(LogEvent(level="info", message="Trade executed"))
await event_bus.emit(TradeEvent(trade_id=..., result=...))
await event_bus.emit(StatsUpdateEvent(daily_pnl=...))
```

**Changes Required:**
1. Create `events/bus.py` with `EventBus` class
2. Create `events/types.py` with event dataclasses
3. Dashboard subscribes to event bus
4. Strategy emits events instead of calling dashboard functions
5. Remove direct dashboard imports from strategies

**Effort:** 2-3 days
**Risk:** Low (already have event system foundation)
**Benefit:** Bot can run headless, easier testing

#### P0.2: WebSocket Heartbeat & Health Monitoring

**Changes Required:**
1. Implement ping/pong heartbeat (every 30s)
2. Add connection health gauge (latency, time since last message)
3. Implement subscription state tracking
4. Auto-resubscribe on reconnect
5. Add circuit breaker for connection failures

**Effort:** 2-3 days
**Risk:** Medium (WebSocket is critical path)
**Benefit:** More reliable market data, faster failure detection

#### P0.3: Extract Settlement Manager

**Current State:**
Settlement logic mixed into `GabagoolStrategy`:
- `_track_position()`
- `_load_unclaimed_positions()`
- `_check_settlement()`
- `_attempt_claim_position()`

**Target State:**
```python
class SettlementManager:
    async def track_position(self, position: TrackedPosition)
    async def get_claimable(self) -> List[TrackedPosition]
    async def attempt_claim(self, position: TrackedPosition) -> ClaimResult
    async def load_pending(self) -> List[TrackedPosition]
```

**Effort:** 2 days
**Risk:** Low (well-isolated logic)
**Benefit:** Cleaner strategy code, reusable settlement logic

### P1: Important (Do Second - Maintainability)

#### P1.1: Split GabagoolStrategy

**Target Structure:**
```
strategies/
├── gabagool/
│   ├── __init__.py
│   ├── strategy.py          # GabagoolStrategy (coordinator)
│   ├── opportunity.py       # OpportunityProcessor
│   ├── executor.py          # TradeExecutor
│   ├── sizing.py            # PositionSizer (move from risk/)
│   └── config.py            # GabagoolConfig (move from config.py)
```

**Effort:** 3-4 days
**Risk:** Medium (many internal changes)
**Benefit:** Easier to understand, test, and modify

#### P1.2: Create Trade Repository Abstraction

**Target:**
```python
class TradeRepository(Protocol):
    async def save(self, trade: Trade) -> None
    async def get_by_id(self, trade_id: str) -> Optional[Trade]
    async def get_pending(self) -> List[Trade]
    async def update_status(self, trade_id: str, status: TradeStatus) -> None
```

**Benefits:**
- Decouple strategy from persistence implementation
- Enable different storage backends (SQLite, PostgreSQL, etc.)
- Easier mocking in tests

**Effort:** 2 days
**Risk:** Low

#### P1.3: Externalize Dashboard HTML/CSS

**Current State:**
`dashboard.py` has ~2000 lines of embedded HTML/CSS as Python strings.

**Target State:**
```
templates/
├── dashboard.html
├── styles.css
└── scripts.js
```

**Changes:**
1. Extract HTML to Jinja2 template
2. Move CSS to separate file
3. Move JavaScript to separate file
4. Use `aiohttp-jinja2` for templating

**Effort:** 1-2 days
**Risk:** Low
**Benefit:** Web developers can modify UI without touching Python

#### P1.4: Metrics Module Refactoring

**Current State:**
- 1,114 lines of metric definitions and helper functions
- Global mutable state

**Target Structure:**
```
metrics/
├── __init__.py
├── trading.py      # Trade-related metrics
├── execution.py    # Order execution metrics
├── risk.py         # Circuit breaker, position sizing
├── connection.py   # WebSocket, API metrics
└── helpers.py      # Recording functions
```

**Effort:** 1-2 days
**Risk:** Low

### P2: Nice to Have (Do Later - Polish)

#### P2.1: Strategy Base Class Enhancement

**Current State:**
`base.py` is minimal with just logging.

**Enhancements:**
- Add lifecycle hooks (on_opportunity, on_trade, on_fill)
- Add common validation methods
- Add metrics integration
- Add event emission helpers

#### P2.2: Configuration Consolidation

**Current State:**
Multiple config classes in `config.py`:
- `AppConfig`
- `PolymarketSettings`
- `GabagoolConfig`
- `VolHappensConfig`
- `NearResolutionConfig`

**Target:**
- Hierarchical config with clear inheritance
- Validation with Pydantic v2
- Environment-specific overrides

#### P2.3: Execution Engine Abstraction

**Target:**
```python
class ExecutionEngine:
    async def execute_single(self, order: OrderRequest) -> OrderResult
    async def execute_dual_leg(self, yes_order: OrderRequest, no_order: OrderRequest) -> DualLegResult
    async def cancel_order(self, order_id: str) -> CancelResult
```

**Benefits:**
- Centralized execution logic
- Easier to add execution policies (retries, timeouts)
- Simpler mocking for tests

#### P2.4: Comprehensive Error Handling

**Add:**
- Custom exception hierarchy
- Error context propagation
- Structured error logging
- Error aggregation for monitoring

---

## 6. Proposed New Architecture

### Target Module Structure

```
src/
├── main.py                    # Entry point (unchanged)
├── config/
│   ├── __init__.py
│   ├── base.py                # BaseConfig
│   ├── app.py                 # AppConfig
│   ├── strategies/
│   │   ├── gabagool.py        # GabagoolConfig
│   │   └── vol_happens.py     # VolHappensConfig
│   └── validation.py          # Config validators
│
├── events/
│   ├── __init__.py
│   ├── bus.py                 # EventBus
│   ├── types.py               # Event dataclasses
│   └── handlers.py            # Built-in handlers
│
├── client/
│   ├── __init__.py
│   ├── polymarket.py          # PolymarketClient (slimmed down)
│   ├── websocket.py           # PolymarketWebSocket (enhanced)
│   ├── gamma.py               # GammaClient (unchanged)
│   └── execution/
│       ├── engine.py          # ExecutionEngine
│       ├── orders.py          # OrderRequest, OrderResult
│       └── retry.py           # RetryPolicy
│
├── strategies/
│   ├── __init__.py
│   ├── base.py                # Enhanced BaseStrategy
│   ├── gabagool/
│   │   ├── __init__.py
│   │   ├── strategy.py        # GabagoolStrategy (coordinator)
│   │   ├── opportunity.py     # OpportunityProcessor
│   │   ├── executor.py        # TradeExecutor
│   │   └── sizing.py          # PositionSizer
│   ├── vol_happens/
│   │   └── ...
│   └── near_resolution/
│       └── ...
│
├── monitoring/
│   ├── __init__.py
│   ├── market_finder.py       # (unchanged)
│   └── order_book.py          # (unchanged)
│
├── risk/
│   ├── __init__.py
│   ├── circuit_breaker.py     # (unchanged)
│   └── position_sizing.py     # (move to strategy if specific)
│
├── settlement/
│   ├── __init__.py
│   ├── manager.py             # SettlementManager
│   ├── position.py            # TrackedPosition
│   └── claim.py               # ClaimExecutor
│
├── persistence/
│   ├── __init__.py
│   ├── database.py            # Database connection
│   ├── repositories/
│   │   ├── trades.py          # TradeRepository
│   │   ├── markets.py         # MarketRepository
│   │   └── settlement.py      # SettlementRepository
│   └── migrations/
│       └── ...
│
├── metrics/
│   ├── __init__.py
│   ├── trading.py
│   ├── execution.py
│   ├── risk.py
│   └── connection.py
│
├── dashboard/
│   ├── __init__.py
│   ├── server.py              # DashboardServer
│   ├── handlers.py            # Event handlers
│   └── templates/
│       ├── dashboard.html
│       ├── styles.css
│       └── scripts.js
│
└── liquidity/
    ├── __init__.py
    ├── collector.py           # (unchanged)
    └── models.py              # (unchanged)
```

### Key Architectural Principles

1. **Event-Driven Communication**
   - Strategies emit events, never call UI directly
   - Dashboard subscribes to event bus
   - Loose coupling between components

2. **Repository Pattern for Persistence**
   - Domain objects don't know about database
   - Repositories handle all data access
   - Easy to swap storage backends

3. **Execution Engine Abstraction**
   - Single point for all order execution
   - Centralized retry and error handling
   - Consistent logging and metrics

4. **Hierarchical Configuration**
   - Base config with common settings
   - Strategy-specific configs inherit base
   - Clear validation and defaults

5. **Strategy as Coordinator**
   - Strategy class orchestrates components
   - Delegates to specialized classes
   - Remains testable and focused

---

## 7. Migration Strategy

### Phase 1: Foundation (Week 1)

**Goal:** Establish event system and decouple dashboard

1. Create `events/` module with `EventBus` and event types
2. Dashboard subscribes to event bus
3. Add event emission to `GabagoolStrategy`
4. Keep existing dashboard functions as fallback
5. Verify both paths work (events + direct calls)
6. Remove direct dashboard calls once verified

**Validation:**
- Run bot with dashboard disabled
- Verify events are emitted and logged
- Ensure no runtime errors

### Phase 2: WebSocket Hardening (Week 1-2)

**Goal:** Improve connection reliability

1. Add heartbeat mechanism
2. Implement subscription state tracking
3. Add connection health metrics
4. Test reconnection scenarios
5. Add circuit breaker for connection failures

**Validation:**
- Simulate network disconnection
- Verify reconnection with subscriptions restored
- Check metrics report health accurately

### Phase 3: Settlement Extraction (Week 2)

**Goal:** Extract settlement logic to dedicated module

1. Create `settlement/` module
2. Move settlement logic from `GabagoolStrategy`
3. Update strategy to use `SettlementManager`
4. Add tests for settlement scenarios

**Validation:**
- Verify positions are tracked correctly
- Test claim execution
- Ensure restart recovery works

### Phase 4: Strategy Decomposition (Week 2-3)

**Goal:** Split `GabagoolStrategy` into components

1. Create `strategies/gabagool/` directory
2. Extract `OpportunityProcessor`
3. Extract `TradeExecutor`
4. Update strategy to use components
5. Maintain backward compatibility

**Validation:**
- Run full trading cycle
- Verify all edge cases work
- Performance testing

### Phase 5: Repository Pattern (Week 3)

**Goal:** Abstract persistence layer

1. Create repository interfaces
2. Implement `TradeRepository`
3. Implement `SettlementRepository`
4. Update consumers to use repositories
5. Add integration tests

**Validation:**
- Database operations work correctly
- Transactions are proper
- Error handling is robust

### Phase 6: Dashboard Extraction (Week 3-4)

**Goal:** Externalize dashboard assets

1. Extract HTML to template file
2. Extract CSS to separate file
3. Extract JavaScript to separate file
4. Implement template rendering
5. Test all dashboard functionality

**Validation:**
- Dashboard renders correctly
- Real-time updates work
- Mobile responsiveness maintained

### Phase 7: Metrics Reorganization (Week 4)

**Goal:** Organize metrics into focused modules

1. Create `metrics/` directory structure
2. Split metrics by domain
3. Update imports across codebase
4. Verify all metrics still work

**Validation:**
- Prometheus scrape works
- All metrics are populated
- Grafana dashboards work

### Phase 8: Execution Engine (Week 4+)

**Goal:** Centralize order execution

1. Create `ExecutionEngine` class
2. Implement execution policies
3. Migrate `PolymarketClient` to use engine
4. Add comprehensive error handling

**Validation:**
- All order types work
- Retries function correctly
- Metrics capture execution

---

## Appendix A: Risk Assessment

| Change | Risk Level | Mitigation |
|--------|------------|------------|
| Event system | Low | Keep fallback calls during transition |
| WebSocket changes | Medium | Comprehensive testing, feature flag |
| Settlement extraction | Low | Well-isolated code, easy rollback |
| Strategy decomposition | Medium | Incremental changes, maintain interfaces |
| Repository pattern | Low | Add new layer, don't modify existing |
| Dashboard extraction | Low | Static content, easy to test |
| Metrics reorganization | Low | Only import changes |
| Execution engine | Medium | Critical path, extensive testing needed |

## Appendix B: Testing Strategy

### Unit Tests
- Event bus functionality
- Repository operations
- Settlement manager logic
- Opportunity processor
- Trade executor

### Integration Tests
- WebSocket connection/reconnection
- Database operations
- Full trade cycle
- Settlement claiming

### End-to-End Tests
- Complete trading flow (dry run)
- Dashboard functionality
- Metrics collection
- Error scenarios

## Appendix C: Rollback Plan

Each phase should be independently reversible:

1. **Event System:** Remove event calls, restore direct dashboard calls
2. **WebSocket:** Revert to previous connection handling
3. **Settlement:** Move code back to strategy
4. **Strategy Split:** Inline component code back to single file
5. **Repository:** Direct database calls from existing code
6. **Dashboard:** Inline templates back to Python strings
7. **Metrics:** Combine files back to single module
8. **Execution Engine:** Inline execution logic

---

## Appendix D: Success Metrics

### Reliability
- WebSocket uptime > 99.9%
- Zero unhandled exceptions in trade path
- Settlement success rate > 99%

### Maintainability
- No file > 500 lines (except templates)
- Test coverage > 80%
- Clear module boundaries

### Performance
- Trade execution latency < 100ms
- Dashboard response time < 200ms
- Memory usage stable over 24h

---

*End of Architecture Refactoring Plan*
