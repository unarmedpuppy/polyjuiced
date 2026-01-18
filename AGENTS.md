# Project Mercury - Agent Instructions

**Status**: Clean-slate rebuild in progress
**Replaces**: polyjuiced (legacy code in `legacy/` directory)

## Executive Summary

Mercury is a complete rebuild of the Polymarket trading bot. The previous codebase (polyjuiced) suffered from:
- 3,400-line god class (`gabagool.py`)
- Tight coupling between dashboard, strategies, and data feeds
- Brittle WebSocket connections without proper health monitoring
- Metrics system with global mutable state
- No ability to run strategies independently

**Mercury's Core Principles:**
1. **Event-driven architecture** - Components communicate via Redis pub/sub, never direct calls
2. **Single responsibility** - Each service does ONE thing well
3. **Strategy as plugins** - Strategies are isolated, can be enabled/disabled at runtime
4. **Observability via metrics** - Dashboard consumes metrics, never couples to trading engine
5. **Fail gracefully** - Circuit breakers, retries, graceful degradation

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                    OBSERVABILITY (Emit Only)                     │
│              Prometheus Metrics → Grafana Dashboards             │
└──────────────────────────────────────────────────────────────────┘
                                ▲ emit only (no reading)
┌──────────────────────────────────────────────────────────────────┐
│                         REDIS EVENT BUS                          │
│  market.* │ signal.* │ order.* │ position.* │ risk.* │ system.*  │
└──────────────────────────────────────────────────────────────────┘
     ▲ publish          ▲ subscribe           ▲ both
┌────┴────┐      ┌──────┴──────┐      ┌───────┴───────┐
│ Market  │      │  Strategy   │      │   Execution   │
│  Data   │─────▶│   Engine    │─────▶│    Engine     │
│ Service │      │  (Plugins)  │      │   (Orders)    │
└─────────┘      └─────────────┘      └───────────────┘
                        │                     │
                 ┌──────┴──────┐       ┌──────┴──────┐
                 │    Risk     │       │    State    │
                 │   Manager   │       │    Store    │
                 └─────────────┘       └─────────────┘
```

## Critical Design Patterns

### 1. Event Bus Communication (MANDATORY)

**NEVER** do this:
```python
# BAD - Direct coupling
class Strategy:
    def __init__(self, dashboard, metrics, persistence):
        self.dashboard = dashboard  # NO!

    def on_signal(self, signal):
        self.dashboard.update(signal)  # NO - direct call
        self.metrics.record(signal)    # NO - direct call
```

**ALWAYS** do this:
```python
# GOOD - Event-driven
class Strategy:
    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus

    async def on_signal(self, signal):
        await self.event_bus.publish(f"signal.{self.name}", signal)
        # Dashboard, metrics, persistence subscribe independently
```

### 2. Single Responsibility Services

Each service has ONE job:

| Service | Responsibility | NOT Responsible For |
|---------|---------------|---------------------|
| MarketDataService | Stream market data, maintain order books | Trading decisions, persistence |
| StrategyEngine | Load strategies, route data, collect signals | Order execution, risk checks |
| RiskManager | Validate signals against limits | Executing trades, persistence |
| ExecutionEngine | Submit orders, track lifecycle | Signal generation, risk decisions |
| StateStore | Persist data, query history | Business logic, event publishing |
| SettlementManager | Claim resolved positions | Trading, market monitoring |

### 3. Strategy Plugin Pattern

Strategies implement the `BaseStrategy` protocol:

```python
class BaseStrategy(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def enabled(self) -> bool: ...

    async def on_market_data(self, market_id: str, book: OrderBook) -> AsyncIterator[TradingSignal]:
        """Yield 0, 1, or many signals per market update."""
        ...

    def get_subscribed_markets(self) -> list[str]: ...
```

**Key constraints:**
- Strategies receive market data, emit signals - nothing else
- No direct database access (use events)
- No direct metrics calls (use events)
- No dashboard coupling (use events)
- Configuration via TOML, not hardcoded

### 4. Error Handling Pattern

```python
# Use tenacity for retries on external calls
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(TransientError)
)
async def call_external_api(self):
    ...

# Emit failures as events for circuit breaker
try:
    result = await self.execute_order(order)
except OrderRejected as e:
    await self.event_bus.publish("order.rejected", {"order_id": order.id, "reason": str(e)})
    raise
```

## Project Structure

```
polyjuiced/
├── AGENTS.md                 # This file
├── legacy/                   # OLD polyjuiced code (reference only)
│   ├── src/                  # Do NOT modify - reference for porting
│   ├── tests/
│   └── docs/
│
├── mercury/                  # NEW clean-slate implementation
│   ├── pyproject.toml
│   ├── config/
│   │   ├── default.toml
│   │   ├── production.toml
│   │   └── development.toml
│   │
│   └── src/mercury/
│       ├── __init__.py
│       ├── __main__.py       # Entry: python -m mercury
│       ├── app.py            # Application wiring
│       │
│       ├── core/             # Framework (no business logic)
│       │   ├── config.py     # ConfigManager
│       │   ├── events.py     # EventBus (Redis pub/sub)
│       │   ├── logging.py    # Structured logging
│       │   └── lifecycle.py  # Start/stop protocols
│       │
│       ├── domain/           # Pure models (no I/O)
│       │   ├── market.py     # Market, OrderBook
│       │   ├── order.py      # Order, Position, Fill
│       │   ├── signal.py     # TradingSignal
│       │   └── risk.py       # RiskLimits, CircuitBreakerState
│       │
│       ├── services/         # Business logic (single responsibility)
│       │   ├── market_data.py
│       │   ├── strategy_engine.py
│       │   ├── risk_manager.py
│       │   ├── execution.py
│       │   ├── state_store.py
│       │   ├── settlement.py
│       │   └── metrics.py
│       │
│       ├── strategies/       # Strategy plugins
│       │   ├── base.py       # BaseStrategy protocol
│       │   ├── registry.py   # Discovery/loading
│       │   └── gabagool/     # Ported strategy
│       │
│       └── integrations/     # External adapters
│           ├── polymarket/   # CLOB, Gamma, WebSocket
│           ├── price_feeds/  # Binance, etc.
│           └── chain/        # Polygon/Web3
│
├── agents/
│   └── plans/
│       └── clean-slate-rebuild-plan.md  # Full architecture doc
│
└── docker/
    ├── Dockerfile
    └── docker-compose.yml
```

## Task Execution Guidelines

### Beads Workflow

All Mercury tasks are tracked as beads. The beads database lives in `../home-server/.beads/` (centralized for all projects), but implementation happens here in `polyjuiced/`.

```bash
# BEADS COMMANDS - run from home-server (sibling directory)
cd ../home-server

bd list --label mercury              # All Mercury tasks
bd ready --label mercury             # Unblocked tasks
bd show home-server-rspl             # Master epic details
bd update <id> --status in_progress  # Claim a task
bd close <id> --reason "Implemented" # Complete a task

# IMPLEMENTATION - run from polyjuiced (this repo)
cd ../polyjuiced
# ... write code, run tests, commit
```

### Phase Dependencies

Phases are sequential - do NOT skip ahead:

```
Phase 1 (Core Infrastructure)
    ↓ blocks
Phase 2 (Integration Layer)
    ↓ blocks
Phase 3 (Market Data Service)
    ↓ blocks
Phase 4 (State Store)
    ↓ blocks
Phase 5 (Execution Engine)
    ↓ blocks
Phase 6 (Strategy Engine)
    ↓ blocks
Phase 7 (Risk Manager)
    ↓ blocks
Phase 8 (Settlement Manager)
    ↓ blocks
Phase 9 (Polish & Production)
```

### Task Execution Protocol

1. **Before starting a task:**
   ```bash
   bd show <task-id>                    # Read full description
   bd dep tree <task-id>                # Check dependencies are done
   bd update <task-id> --status in_progress
   ```

2. **While working:**
   - Follow the design patterns in this document
   - Reference `legacy/` for logic to port, NOT patterns to copy
   - Write tests alongside implementation
   - Commit frequently with clear messages

3. **After completing:**
   ```bash
   # Run tests
   cd mercury && pytest tests/ -v

   # Close the bead
   bd close <task-id> --reason "Implemented with tests"

   # Commit
   git add . && git commit -m "mercury: <task description>"
   ```

### What to Port vs Rewrite

| Port (extract logic) | Rewrite (new implementation) |
|---------------------|------------------------------|
| `legacy/src/client/gamma.py` | Main orchestration |
| `legacy/src/client/websocket.py` message parsing | Strategy framework |
| `legacy/src/monitoring/market_finder.py` filters | Dashboard (eliminated) |
| `legacy/src/risk/circuit_breaker.py` state machine | Metrics system |
| `legacy/src/persistence.py` schema | Configuration |
| Gabagool signal detection logic | All event publishing |

**When porting:**
- Extract the LOGIC, not the structure
- Remove all dashboard/metrics coupling
- Add proper type hints
- Add async where appropriate
- Add tenacity retries for external calls

## Coding Standards

### Type Hints (Required)

```python
from decimal import Decimal
from datetime import datetime

async def execute_order(
    self,
    order: OrderRequest,
    timeout: float = 30.0
) -> OrderResult:
    ...
```

### Async Patterns

```python
# Use async for all I/O
async def fetch_order_book(self, market_id: str) -> OrderBook:
    ...

# Use asyncio.gather for parallel operations
results = await asyncio.gather(
    self.fetch_yes_book(market_id),
    self.fetch_no_book(market_id),
)
```

### Logging

```python
import structlog
log = structlog.get_logger()

# Always use structured logging with context
log.info("order_executed",
    order_id=order.id,
    market_id=order.market_id,
    size=str(order.size),
    latency_ms=latency
)
```

### Configuration Access

```python
# Via ConfigManager, never hardcoded
spread_threshold = self.config.get("strategies.gabagool.min_spread_threshold")

# With defaults
max_size = self.config.get("strategies.gabagool.max_trade_size_usd", Decimal("25.0"))
```

## Testing & Verification Requirements

### Task-Level Verification (MANDATORY)

Every task MUST include tests that verify completion. This enables automated verification loops.

**Convention:** For a task that creates `services/foo.py`, there must be a corresponding `tests/unit/test_foo.py` that:
1. Imports the module successfully
2. Tests the core functionality described in the bead

```python
# tests/unit/test_risk_manager.py
import pytest
from mercury.services.risk_manager import RiskManager

@pytest.fixture
def risk_manager(mock_config, mock_event_bus):
    return RiskManager(mock_config, mock_event_bus)

async def test_rejects_when_daily_loss_exceeded(risk_manager):
    risk_manager._daily_pnl = Decimal("-100.0")  # At limit

    signal = TradingSignal(target_size_usd=Decimal("10.0"), ...)
    allowed, reason = await risk_manager.check_pre_trade(signal)

    assert not allowed
    assert "daily loss" in reason.lower()
```

### Verification Command

After completing ANY task, run:
```bash
cd ../polyjuiced/mercury
pytest tests/ -v --tb=short
```

**A task is NOT complete until tests pass.**

### Phase-Level Smoke Tests

Each phase has a smoke test that verifies the phase deliverables work together:

| Phase | Smoke Test | What It Verifies |
|-------|------------|------------------|
| 1 | `pytest tests/smoke/test_phase1_core.py` | Config loads, EventBus connects to Redis, metrics endpoint responds |
| 2 | `pytest tests/smoke/test_phase2_integrations.py` | Can connect to Polymarket, fetch markets, subscribe to WebSocket |
| 3 | `pytest tests/smoke/test_phase3_market_data.py` | MarketDataService streams data, publishes to EventBus |
| 4 | `pytest tests/smoke/test_phase4_persistence.py` | StateStore CRUD operations work, schema is correct |
| 5 | `pytest tests/smoke/test_phase5_execution.py` | ExecutionEngine can submit orders (dry-run), emit events |
| 6 | `pytest tests/smoke/test_phase6_strategy.py` | Gabagool strategy loads, generates signals from mock data |
| 7 | `pytest tests/smoke/test_phase7_risk.py` | RiskManager validates signals, circuit breaker works |
| 8 | `pytest tests/smoke/test_phase8_settlement.py` | SettlementManager processes queue, emits events |
| 9 | `pytest tests/smoke/test_phase9_e2e.py` | Full trading flow works end-to-end |

### Automated Verification Loop Protocol

For Ralph Wiggum or similar automated loops:

```bash
# 1. Before starting a task
cd ../home-server && bd update <id> --status in_progress

# 2. After implementation, verify task
cd ../polyjuiced/mercury
pytest tests/unit/test_<component>.py -v
# Must exit 0

# 3. Verify phase smoke test still passes
pytest tests/smoke/test_phase<N>_*.py -v
# Must exit 0

# 4. Only then close the bead
cd ../home-server && bd close <id> --reason "Verified: tests pass"
```

### Test File Naming Convention

| Component | Test File |
|-----------|-----------|
| `core/config.py` | `tests/unit/test_config.py` |
| `core/events.py` | `tests/unit/test_events.py` |
| `services/market_data.py` | `tests/unit/test_market_data.py` |
| `services/execution.py` | `tests/unit/test_execution.py` |
| `strategies/gabagool/strategy.py` | `tests/unit/test_gabagool.py` |
| `integrations/polymarket/clob.py` | `tests/integration/test_clob.py` |

## Definition of Done

A Mercury task is complete when:

- [ ] Implementation follows event-driven patterns (no direct coupling)
- [ ] Single responsibility maintained (service does ONE thing)
- [ ] Type hints on all public interfaces
- [ ] Unit tests for core logic
- [ ] Integration test if touching external services
- [ ] Structured logging added
- [ ] Metrics emitted (where applicable)
- [ ] No hardcoded configuration
- [ ] Code reviewed against this AGENTS.md
- [ ] Bead closed with reason

## Boundaries

### Always Do
- Communicate via EventBus
- Use async for I/O operations
- Add retries for external calls
- Emit metrics for observability
- Write tests alongside code
- Follow the phase order

### Ask First
- Changing the event schema
- Adding new dependencies
- Modifying core/ components
- Deviating from the plan

### Never Do
- Direct coupling between services
- Synchronous blocking calls in hot path
- Hardcoded configuration values
- Skipping tests
- Porting polyjuiced patterns (only logic)
- Working on blocked tasks

## Coordination for Parallel Work

**Current recommendation: Sequential execution within phases**

If multiple agents must work in parallel:

1. **Claim tasks explicitly** - Update bead status before starting
2. **File ownership** - Each task owns specific files (see task descriptions)
3. **Commit frequently** - Small commits reduce merge conflicts
4. **Pull before push** - Always pull latest before committing

### File Ownership by Phase

| Phase | Primary Files |
|-------|---------------|
| 1 | `core/*`, `domain/*`, `pyproject.toml` |
| 2 | `integrations/*` |
| 3 | `services/market_data.py` |
| 4 | `services/state_store.py` |
| 5 | `services/execution.py` |
| 6 | `services/strategy_engine.py`, `strategies/*` |
| 7 | `services/risk_manager.py` |
| 8 | `services/settlement.py` |
| 9 | `docker/*`, `docs/*`, tests |

## Reference Documents

- [Clean-Slate Rebuild Plan](agents/plans/clean-slate-rebuild-plan.md) - Full architecture
- [Legacy Code](legacy/) - Reference for porting logic
- [Beads Reference](../beads-viewer/agents/reference/beads.md) - Task tracking

## Quick Reference

```bash
# ============================================
# BEADS COMMANDS (run from home-server)
# Beads source of truth: ../home-server/.beads/
# ============================================
cd ../home-server

bd ready --label mercury              # Find unblocked task
bd show <id>                          # View task details
bd update <id> --status in_progress   # Claim task
bd close <id> --reason "Done"         # Complete task

# ============================================
# IMPLEMENTATION WORK (run from polyjuiced)
# Code lives here: ./mercury/
# ============================================
cd ../polyjuiced

# During work
cd mercury
pytest tests/ -v                   # Run tests
python -m mercury health           # Check if it runs

# Commit work (from polyjuiced root)
cd ..  # back to polyjuiced root
git add . && git commit -m "mercury: <description>"
git push
```

**Remember:** Beads tracks tasks across ALL projects from `../home-server`. The code for Mercury lives here in `polyjuiced/mercury/`.
