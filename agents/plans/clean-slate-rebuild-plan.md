# Mercury: Clean-Slate Polymarket Trading Bot

**Project Name:** Mercury
**Date:** 2026-01-17
**Status:** Planning - Ready for Implementation
**Replaces:** architecture-refactor-plan.md (full rebuild, not refactor)

---

## Executive Summary

Mercury is a clean-slate rebuild of the Polymarket trading bot (polyjuiced). Rather than incrementally refactoring the existing 3,400-line god class, we're building a new system from the ground up with proper architecture.

### Why "Mercury"?

- Roman god of speed, commerce, and financial gain
- Known for quick execution (sub-100ms latency goal)
- Messenger of the gods (event-driven architecture)
- Planet closest to the Sun (first to react to market movements)

### Core Requirements

| Requirement | Target |
|-------------|--------|
| Execution latency | Sub-100ms (network is bottleneck) |
| Concurrent strategies | Multiple running simultaneously |
| Initial positions | 1-10 |
| Design capacity | 50+ positions |
| Integrations | CLOB, Gamma API, price feeds, Polygon chain |
| Dashboard | Read-only via metrics (Grafana) |
| Deployment | Docker (homelab + cloud VPS) |

### Architectural Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Language | Python 3.11+ | Team familiarity, async ecosystem, SDK compatibility |
| Event bus | Redis pub/sub | Process isolation, dashboard decoupling, future scaling |
| Persistence | SQLite (local) | Proven schema from polyjuiced, simple operations |
| Metrics | Prometheus | Standard, Grafana integration, no custom dashboards |
| Config | TOML + env vars | Human-readable, 12-factor app |

---

## Table of Contents

1. [Project Structure](#1-project-structure)
2. [Core Components](#2-core-components)
3. [Event Schema](#3-event-schema)
4. [Strategy Plugin System](#4-strategy-plugin-system)
5. [Integration Layer](#5-integration-layer)
6. [Metrics Strategy](#6-metrics-strategy)
7. [Configuration Management](#7-configuration-management)
8. [What to Port from Polyjuiced](#8-what-to-port-from-polyjuiced)
9. [Implementation Phases](#9-implementation-phases)
10. [Testing Strategy](#10-testing-strategy)
11. [Deployment](#11-deployment)

---

## 1. Project Structure

```
mercury/
├── pyproject.toml              # Modern Python packaging (uv/poetry)
├── config/
│   ├── default.toml            # Default configuration
│   ├── production.toml         # Production overrides
│   └── development.toml        # Dev/testing overrides
│
├── src/
│   └── mercury/
│       ├── __init__.py
│       ├── __main__.py         # Entry point: python -m mercury
│       ├── app.py              # Application lifecycle, component wiring
│       │
│       ├── core/               # Framework-level abstractions
│       │   ├── __init__.py
│       │   ├── config.py       # ConfigManager - TOML + env loading
│       │   ├── events.py       # EventBus - Redis pub/sub wrapper
│       │   ├── logging.py      # Structured logging setup
│       │   └── lifecycle.py    # Component start/stop protocols
│       │
│       ├── domain/             # Domain models (no external deps)
│       │   ├── __init__.py
│       │   ├── market.py       # Market, Token, OrderBook models
│       │   ├── order.py        # Order, Fill, Position models
│       │   ├── signal.py       # TradingSignal, SignalType
│       │   └── risk.py         # RiskLimits, CircuitBreakerState
│       │
│       ├── services/           # Core services (single responsibility)
│       │   ├── __init__.py
│       │   ├── market_data.py      # MarketDataService
│       │   ├── strategy_engine.py  # StrategyEngine
│       │   ├── risk_manager.py     # RiskManager
│       │   ├── execution.py        # ExecutionEngine
│       │   ├── state_store.py      # StateStore (persistence)
│       │   ├── settlement.py       # SettlementManager
│       │   └── metrics.py          # MetricsEmitter
│       │
│       ├── strategies/         # Strategy plugins
│       │   ├── __init__.py
│       │   ├── base.py         # BaseStrategy protocol
│       │   ├── gabagool/       # Ported gabagool strategy
│       │   │   ├── __init__.py
│       │   │   ├── strategy.py
│       │   │   └── config.py
│       │   └── registry.py     # Strategy discovery/loading
│       │
│       ├── integrations/       # External system adapters
│       │   ├── __init__.py
│       │   ├── polymarket/     # Polymarket CLOB + Gamma
│       │   │   ├── __init__.py
│       │   │   ├── clob.py     # CLOB client (port from polyjuiced)
│       │   │   ├── gamma.py    # Gamma client (port from polyjuiced)
│       │   │   ├── websocket.py # WebSocket handler
│       │   │   └── types.py    # Polymarket-specific types
│       │   ├── price_feeds/    # External price sources
│       │   │   ├── __init__.py
│       │   │   ├── base.py     # PriceFeed protocol
│       │   │   └── binance.py  # Binance price adapter
│       │   └── chain/          # Polygon chain interactions
│       │       ├── __init__.py
│       │       ├── client.py   # Web3 client
│       │       └── ctf.py      # CTF redemption logic
│       │
│       └── cli/                # CLI commands
│           ├── __init__.py
│           ├── run.py          # Main run command
│           └── tools.py        # Utility commands
│
├── tests/
│   ├── conftest.py             # Shared fixtures
│   ├── unit/                   # Unit tests (no external deps)
│   ├── integration/            # Integration tests (Redis, etc.)
│   └── e2e/                    # End-to-end scenarios
│
├── scripts/
│   ├── migrate_from_polyjuiced.py  # Data migration helper
│   └── health_check.py             # Deployment health check
│
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yml      # Local development
│   └── docker-compose.prod.yml # Production deployment
│
└── docs/
    ├── ARCHITECTURE.md         # This document (simplified)
    └── RUNBOOK.md              # Operations guide
```

### Package Responsibilities

| Package | Responsibility |
|---------|----------------|
| `core/` | Framework infrastructure - config, events, logging. No business logic. |
| `domain/` | Pure domain models. No I/O, no external dependencies. |
| `services/` | Business logic services. Each has single responsibility. |
| `strategies/` | Pluggable trading strategies. Implement BaseStrategy protocol. |
| `integrations/` | External system adapters. Isolate third-party APIs. |
| `cli/` | Command-line interface. Thin layer over services. |

---

## 2. Core Components

### 2.1 ConfigManager

**Single Responsibility:** Load, validate, and provide configuration.

```python
class ConfigManager:
    """Centralized configuration with TOML + env var support."""

    def __init__(self, config_path: Path, env_prefix: str = "MERCURY_"):
        ...

    def get(self, key: str, default: Any = None) -> Any:
        """Get config value with dot notation: 'strategies.gabagool.enabled'"""

    def get_section(self, section: str) -> dict:
        """Get entire config section."""

    def reload(self) -> None:
        """Hot-reload configuration (for runtime updates)."""
```

**Subscribes to:** None (passive)
**Publishes:** `system.config.reloaded` (on hot reload)
**Dependencies:** None

### 2.2 EventBus (Redis Pub/Sub)

**Single Responsibility:** Decouple components via message passing.

```python
class EventBus:
    """Redis-backed event bus for component communication."""

    async def publish(self, channel: str, event: Event) -> None:
        """Publish event to channel."""

    async def subscribe(self, pattern: str, handler: Callable) -> None:
        """Subscribe to channel pattern with callback."""

    async def unsubscribe(self, pattern: str) -> None:
        """Unsubscribe from channel pattern."""
```

**Subscribes to:** All channels (routing)
**Publishes:** Routes all events
**Dependencies:** Redis connection

### 2.3 MarketDataService

**Single Responsibility:** Manage market data streams and order book state.

```python
class MarketDataService:
    """Manages WebSocket connections and order book state."""

    async def start(self) -> None:
        """Connect to data sources, start streaming."""

    async def stop(self) -> None:
        """Disconnect and cleanup."""

    async def subscribe_market(self, market_id: str) -> None:
        """Subscribe to market data stream."""

    def get_order_book(self, market_id: str) -> Optional[OrderBook]:
        """Get current order book snapshot."""

    def get_best_prices(self, market_id: str) -> tuple[Decimal, Decimal]:
        """Get (best_bid, best_ask) for market."""
```

**Subscribes to:**
- WebSocket price updates (external)
- `system.market.subscribe` (internal command)

**Publishes:**
- `market.orderbook.{market_id}` - Order book snapshots
- `market.trade.{market_id}` - Trade executions
- `market.stale.{market_id}` - Data staleness alerts

**Dependencies:** WebSocket client, EventBus

### 2.4 StrategyEngine

**Single Responsibility:** Load strategies, route market data, collect signals.

```python
class StrategyEngine:
    """Orchestrates strategy execution."""

    async def start(self) -> None:
        """Load enabled strategies, start processing."""

    async def stop(self) -> None:
        """Stop all strategies gracefully."""

    def register_strategy(self, strategy: BaseStrategy) -> None:
        """Register a strategy instance."""

    def enable_strategy(self, name: str) -> None:
        """Enable strategy at runtime."""

    def disable_strategy(self, name: str) -> None:
        """Disable strategy at runtime."""
```

**Subscribes to:**
- `market.orderbook.*` - Market data for strategies
- `system.strategy.enable` - Runtime enable/disable

**Publishes:**
- `signal.{strategy_name}` - Trading signals from strategies

**Dependencies:** ConfigManager, EventBus, strategy plugins

### 2.5 RiskManager

**Single Responsibility:** Validate trades against risk limits, manage circuit breakers.

```python
class RiskManager:
    """Pre-trade validation and risk controls."""

    def check_pre_trade(self, signal: TradingSignal) -> tuple[bool, str]:
        """Validate signal against risk limits. Returns (allowed, reason)."""

    def record_fill(self, fill: Fill) -> None:
        """Update risk state after fill."""

    def record_pnl(self, pnl: Decimal) -> None:
        """Record realized P&L."""

    @property
    def circuit_breaker_state(self) -> CircuitBreakerState:
        """Current circuit breaker level."""

    def reset_daily(self) -> None:
        """Reset daily counters."""
```

**Subscribes to:**
- `signal.*` - Signals to validate
- `order.filled` - Fill events for exposure tracking
- `position.closed` - P&L events

**Publishes:**
- `risk.approved.{signal_id}` - Approved signals (pass to execution)
- `risk.rejected.{signal_id}` - Rejected signals (with reason)
- `risk.circuit_breaker` - Circuit breaker state changes

**Dependencies:** ConfigManager, EventBus, StateStore (for position data)

### 2.6 ExecutionEngine

**Single Responsibility:** Execute orders, handle retries, track order lifecycle.

```python
class ExecutionEngine:
    """Order execution and lifecycle management."""

    async def execute(self, order_request: OrderRequest) -> OrderResult:
        """Execute single order."""

    async def execute_dual_leg(
        self,
        yes_order: OrderRequest,
        no_order: OrderRequest
    ) -> DualLegResult:
        """Execute paired arbitrage order atomically."""

    async def cancel(self, order_id: str) -> bool:
        """Cancel open order."""

    def get_open_orders(self) -> list[Order]:
        """Get all open orders."""
```

**Subscribes to:**
- `risk.approved.*` - Approved signals to execute

**Publishes:**
- `order.submitted` - Order sent to exchange
- `order.filled` - Order filled (full or partial)
- `order.rejected` - Order rejected by exchange
- `order.cancelled` - Order cancelled

**Dependencies:** CLOB client, EventBus, ConfigManager

### 2.7 StateStore

**Single Responsibility:** Persist and query trading state (positions, trades, history).

```python
class StateStore:
    """SQLite persistence layer."""

    async def connect(self) -> None:
        """Connect to database, run migrations."""

    async def close(self) -> None:
        """Close connection."""

    # Trades
    async def save_trade(self, trade: Trade) -> None:
    async def get_trade(self, trade_id: str) -> Optional[Trade]:
    async def get_trades(self, since: datetime) -> list[Trade]:

    # Positions
    async def save_position(self, position: Position) -> None:
    async def get_open_positions(self) -> list[Position]:
    async def close_position(self, position_id: str, result: PositionResult) -> None:

    # Settlement queue
    async def queue_for_settlement(self, position: Position) -> None:
    async def get_claimable_positions(self) -> list[Position]:
    async def mark_claimed(self, position_id: str, proceeds: Decimal) -> None:

    # Stats
    async def get_daily_stats(self, date: date) -> DailyStats:
```

**Subscribes to:**
- `order.filled` - Record fills
- `position.opened` - Record new positions
- `position.closed` - Record closed positions

**Publishes:** None (passive persistence)

**Dependencies:** SQLite connection

### 2.8 SettlementManager

**Single Responsibility:** Monitor resolved markets, claim winning positions.

```python
class SettlementManager:
    """Handles position settlement after market resolution."""

    async def start(self) -> None:
        """Start settlement monitoring loop."""

    async def stop(self) -> None:
        """Stop monitoring."""

    async def check_settlements(self) -> None:
        """Check for claimable positions and attempt claims."""

    async def claim_position(self, position: Position) -> ClaimResult:
        """Attempt to claim a resolved position."""
```

**Subscribes to:**
- `position.opened` - Track new positions for settlement

**Publishes:**
- `settlement.claimed` - Position successfully claimed
- `settlement.failed` - Claim attempt failed

**Dependencies:** StateStore, CLOB client, CTF client, EventBus

### 2.9 MetricsEmitter

**Single Responsibility:** Emit Prometheus metrics for all components.

```python
class MetricsEmitter:
    """Prometheus metrics emission (emit only, no reading)."""

    def __init__(self, registry: CollectorRegistry):
        # Define all metrics with proper naming
        self.trades_total = Counter("mercury_trades_total", "Total trades", ["strategy", "status"])
        self.position_value = Gauge("mercury_position_value_usd", "Position value", ["market_id"])
        self.order_latency = Histogram("mercury_order_latency_seconds", "Order latency")
        # ... more metrics

    def record_trade(self, trade: Trade) -> None:
        """Record trade metrics."""

    def record_order_latency(self, latency_ms: float) -> None:
        """Record order execution latency."""

    def update_position_value(self, market_id: str, value: Decimal) -> None:
        """Update position value gauge."""
```

**Subscribes to:** All relevant events for metric recording
**Publishes:** None (exposes /metrics endpoint)
**Dependencies:** EventBus, Prometheus registry

---

## 3. Event Schema

### Channel Naming Convention

```
{domain}.{entity}.{action|market_id}
```

### Core Channels

| Channel Pattern | Payload | Publisher | Subscribers |
|----------------|---------|-----------|-------------|
| `market.orderbook.{market_id}` | OrderBookSnapshot | MarketDataService | StrategyEngine |
| `market.trade.{market_id}` | TradeEvent | MarketDataService | StrategyEngine, MetricsEmitter |
| `market.stale.{market_id}` | StaleAlert | MarketDataService | StrategyEngine, RiskManager |
| `signal.{strategy_name}` | TradingSignal | StrategyEngine | RiskManager |
| `risk.approved.{signal_id}` | ApprovedSignal | RiskManager | ExecutionEngine |
| `risk.rejected.{signal_id}` | RejectedSignal | RiskManager | MetricsEmitter |
| `risk.circuit_breaker` | CircuitBreakerEvent | RiskManager | All services |
| `order.submitted` | OrderSubmitted | ExecutionEngine | MetricsEmitter |
| `order.filled` | OrderFilled | ExecutionEngine | StateStore, RiskManager, MetricsEmitter |
| `order.rejected` | OrderRejected | ExecutionEngine | RiskManager, MetricsEmitter |
| `position.opened` | PositionOpened | ExecutionEngine | StateStore, SettlementManager |
| `position.closed` | PositionClosed | SettlementManager | StateStore, RiskManager |
| `settlement.claimed` | SettlementClaimed | SettlementManager | StateStore, MetricsEmitter |
| `system.health` | HealthCheck | All services | Monitoring |
| `system.shutdown` | Shutdown | CLI | All services |

### Event Payload Examples

```python
@dataclass
class OrderBookSnapshot:
    market_id: str
    timestamp: datetime
    yes_best_bid: Decimal
    yes_best_ask: Decimal
    no_best_bid: Decimal
    no_best_ask: Decimal
    yes_depth: list[tuple[Decimal, Decimal]]  # [(price, size), ...]
    no_depth: list[tuple[Decimal, Decimal]]

@dataclass
class TradingSignal:
    signal_id: str
    strategy_name: str
    market_id: str
    signal_type: SignalType  # BUY_YES, BUY_NO, ARBITRAGE, etc.
    confidence: float
    target_size_usd: Decimal
    yes_price: Decimal
    no_price: Decimal
    metadata: dict  # Strategy-specific data

@dataclass
class OrderFilled:
    order_id: str
    market_id: str
    token_id: str
    side: str  # "YES" or "NO"
    requested_size: Decimal
    filled_size: Decimal
    price: Decimal
    cost: Decimal
    timestamp: datetime
```

---

## 4. Strategy Plugin System

### Base Strategy Protocol

```python
from typing import Protocol, AsyncIterator
from mercury.domain.signal import TradingSignal
from mercury.domain.market import OrderBook

class BaseStrategy(Protocol):
    """Protocol that all strategies must implement."""

    @property
    def name(self) -> str:
        """Unique strategy identifier."""
        ...

    @property
    def enabled(self) -> bool:
        """Whether strategy is currently enabled."""
        ...

    async def start(self) -> None:
        """Initialize strategy resources."""
        ...

    async def stop(self) -> None:
        """Cleanup strategy resources."""
        ...

    async def on_market_data(self, market_id: str, book: OrderBook) -> AsyncIterator[TradingSignal]:
        """Process market data update, yield any signals.

        This is an async generator - strategies can yield 0, 1, or multiple signals
        per market data update.
        """
        ...

    def get_subscribed_markets(self) -> list[str]:
        """Return list of market IDs this strategy wants data for."""
        ...
```

### Strategy Configuration

Each strategy has its own config section in TOML:

```toml
[strategies.gabagool]
enabled = true
markets = ["BTC", "ETH", "SOL"]
min_spread_threshold = 0.015
max_trade_size_usd = 25.0
max_per_window_usd = 50.0
gradual_entry_enabled = false
gradual_entry_tranches = 3
```

### Strategy Discovery

```python
class StrategyRegistry:
    """Discovers and loads strategy plugins."""

    def __init__(self, config: ConfigManager):
        self._strategies: dict[str, type[BaseStrategy]] = {}

    def discover(self) -> None:
        """Scan strategies/ package for implementations."""
        # Uses importlib to find all BaseStrategy implementations

    def get_enabled_strategies(self) -> list[BaseStrategy]:
        """Instantiate and return enabled strategies."""

    def register(self, strategy_cls: type[BaseStrategy]) -> None:
        """Manually register a strategy class."""
```

### Runtime Enable/Disable

Strategies can be enabled/disabled at runtime via Redis events:

```python
# Enable strategy
await event_bus.publish("system.strategy.enable", {"strategy": "gabagool"})

# Disable strategy
await event_bus.publish("system.strategy.disable", {"strategy": "gabagool"})
```

---

## 5. Integration Layer

### 5.1 Polymarket CLOB Client (Port from Polyjuiced)

**Source:** `polyjuiced/src/client/polymarket.py`
**Target:** `mercury/src/mercury/integrations/polymarket/clob.py`

```python
class CLOBClient:
    """Polymarket CLOB API client."""

    def __init__(self, settings: PolymarketSettings):
        self._client: ClobClient  # py-clob-client
        self._executor: ThreadPoolExecutor  # For sync SDK calls

    async def connect(self) -> None:
        """Establish connection, validate credentials."""

    async def get_order_book(self, token_id: str) -> OrderBook:
        """Get current order book."""

    async def place_order(self, order: OrderRequest) -> OrderResult:
        """Place order with FOK/GTC."""

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel open order."""

    async def get_positions(self) -> list[Position]:
        """Get current positions."""

    async def get_balance(self) -> Decimal:
        """Get USDC balance."""
```

**Port Notes:**
- Keep thread pool executor pattern (SDK is synchronous)
- Clean up 200-line `execute_dual_leg_order_parallel` method
- Remove dashboard/metrics coupling
- Standardize error handling

### 5.2 Gamma API Client (Port from Polyjuiced)

**Source:** `polyjuiced/src/client/gamma.py`
**Target:** `mercury/src/mercury/integrations/polymarket/gamma.py`

```python
class GammaClient:
    """Polymarket Gamma API for market discovery."""

    async def get_markets(self, limit: int = 100) -> list[MarketInfo]:
        """Get list of markets."""

    async def search_markets(self, query: str) -> list[MarketInfo]:
        """Search markets by query."""

    async def find_15min_markets(self, asset: str) -> list[Market15Min]:
        """Find active 15-minute up/down markets."""
```

**Port Notes:**
- This is already clean - minimal changes needed
- Add retry logic with tenacity
- Add caching for market metadata

### 5.3 WebSocket Handler (Port with Rewrite)

**Source:** `polyjuiced/src/client/websocket.py`
**Target:** `mercury/src/mercury/integrations/polymarket/websocket.py`

```python
class PolymarketWebSocket:
    """WebSocket client with improved reliability."""

    def __init__(self, event_bus: EventBus, url: str):
        self._ws: WebSocketClientProtocol
        self._subscriptions: set[str]
        self._last_message_time: float

    async def connect(self) -> None:
        """Connect with auto-reconnect."""

    async def subscribe(self, market_ids: list[str]) -> None:
        """Subscribe to market data."""

    async def run(self) -> None:
        """Main message loop with heartbeat monitoring."""
```

**Port Notes:**
- Add proper heartbeat/ping-pong
- Track subscription state for reconnect
- Publish events via EventBus instead of callbacks
- Add connection health metrics

### 5.4 Price Feed Adapters (New)

```python
class PriceFeed(Protocol):
    """Protocol for external price feeds."""

    @property
    def name(self) -> str:
        """Feed identifier."""

    async def get_price(self, symbol: str) -> Decimal:
        """Get current price."""

    async def subscribe(self, symbol: str, callback: Callable) -> None:
        """Subscribe to price updates."""


class BinancePriceFeed:
    """Binance spot price feed via WebSocket."""

    async def connect(self) -> None:
    async def get_price(self, symbol: str) -> Decimal:
    async def subscribe(self, symbol: str, callback: Callable) -> None:
```

### 5.5 Chain Interaction Layer (New)

```python
class PolygonClient:
    """Polygon chain interactions via Web3."""

    def __init__(self, rpc_url: str, private_key: str):
        self._w3: Web3
        self._account: Account

    async def redeem_ctf_positions(
        self,
        condition_id: str,
        index_sets: list[int]
    ) -> TxReceipt:
        """Redeem positions via CTF contract."""

    async def get_token_balance(self, token_address: str) -> Decimal:
        """Get ERC20 token balance."""
```

---

## 6. Metrics Strategy

### Naming Convention

All metrics use the `mercury_` prefix:

```
mercury_{component}_{metric}_{unit}
```

### Component Metrics

#### Trading Metrics
```python
# Counters
mercury_trades_total{strategy, asset, status}          # Total trades by strategy
mercury_orders_total{side, status}                      # Total orders
mercury_signals_total{strategy, action}                 # Signals generated

# Gauges
mercury_position_value_usd{market_id}                   # Current position value
mercury_daily_pnl_usd                                   # Daily P&L
mercury_daily_exposure_usd                              # Daily exposure
mercury_active_positions                                # Number of open positions
mercury_circuit_breaker_level                           # 0=NORMAL, 1=WARNING, etc.

# Histograms
mercury_order_latency_seconds{quantile}                 # Order execution time
mercury_spread_cents{asset}                             # Spread at trade time
mercury_fill_ratio                                      # Requested vs filled
```

#### Connection Metrics
```python
mercury_websocket_connected                             # 1=connected, 0=disconnected
mercury_websocket_reconnects_total                      # Reconnection count
mercury_websocket_latency_seconds                       # Message latency
mercury_api_requests_total{endpoint, status}            # API call counts
```

#### System Metrics
```python
mercury_uptime_seconds                                  # Process uptime
mercury_event_bus_messages_total{channel}               # Event throughput
mercury_queue_depth{queue}                              # Queue depths
```

### Dashboard Strategy

**NO custom dashboard** - Use Grafana with Prometheus datasource.

Pre-built dashboards:
1. **Trading Overview** - P&L, trades, positions
2. **Execution Quality** - Latency, fill rates, slippage
3. **Risk Status** - Circuit breaker, exposure, limits
4. **System Health** - Connections, queues, errors

---

## 7. Configuration Management

### Configuration Hierarchy

```
1. Default values (in code)
2. default.toml
3. {environment}.toml (development/production)
4. Environment variables (MERCURY_*)
5. CLI arguments (highest priority)
```

### TOML Configuration Structure

```toml
# default.toml

[mercury]
log_level = "INFO"
log_json = false
dry_run = true  # ALWAYS default to dry run

[redis]
url = "redis://localhost:6379"
db = 0

[database]
path = "./data/mercury.db"

[polymarket]
clob_url = "https://clob.polymarket.com/"
gamma_url = "https://gamma-api.polymarket.com"
ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
# Credentials from env vars: MERCURY_POLYMARKET_API_KEY, etc.

[polygon]
rpc_url = "https://polygon-rpc.com"
# Private key from env: MERCURY_POLYGON_PRIVATE_KEY

[metrics]
enabled = true
port = 9090

[risk]
max_daily_loss_usd = 100.0
max_unhedged_exposure_usd = 50.0
max_position_size_usd = 25.0
circuit_breaker_cooldown_minutes = 5

[strategies.gabagool]
enabled = true
markets = ["BTC", "ETH", "SOL"]
min_spread_threshold = 0.015
max_trade_size_usd = 25.0
max_per_window_usd = 50.0
balance_sizing_enabled = true
balance_sizing_pct = 0.25
gradual_entry_enabled = false
gradual_entry_tranches = 3
```

### Environment Variable Overrides

```bash
# Core
MERCURY_DRY_RUN=false
MERCURY_LOG_LEVEL=DEBUG

# Polymarket credentials (secrets)
MERCURY_POLYMARKET_API_KEY=xxx
MERCURY_POLYMARKET_API_SECRET=xxx
MERCURY_POLYMARKET_API_PASSPHRASE=xxx

# Polygon wallet (secret)
MERCURY_POLYGON_PRIVATE_KEY=xxx
MERCURY_POLYGON_FUNDER_ADDRESS=0x...

# Redis
MERCURY_REDIS_URL=redis://redis:6379

# Strategy overrides
MERCURY_STRATEGIES_GABAGOOL_ENABLED=true
MERCURY_STRATEGIES_GABAGOOL_MAX_TRADE_SIZE_USD=10.0
```

### Secrets Management

- **Development:** `.env` file (gitignored)
- **Production:** Docker secrets or environment variables
- **Never commit:** Private keys, API credentials

---

## 8. What to Port from Polyjuiced

### Definitely Port (With Cleanup)

| Source File | Target | Changes Needed |
|-------------|--------|----------------|
| `client/gamma.py` | `integrations/polymarket/gamma.py` | Add tenacity retries, type hints |
| `client/websocket.py` | `integrations/polymarket/websocket.py` | Add heartbeat, EventBus integration |
| `monitoring/market_finder.py` | `integrations/polymarket/market_finder.py` | Clean up, add caching |
| `monitoring/order_book.py` | `services/market_data.py` | Merge into MarketDataService |
| `risk/circuit_breaker.py` | `services/risk_manager.py` | Integrate into RiskManager |
| `persistence.py` (schema) | `services/state_store.py` | Port schema, rewrite access layer |

### Port Logic, Rewrite Structure

| Source | Target | What to Keep |
|--------|--------|--------------|
| `strategies/gabagool.py` | `strategies/gabagool/` | Core trading logic, opportunity detection |
| `client/polymarket.py` | `integrations/polymarket/clob.py` | SDK wrapper, order execution |

### Definitely NOT Port

| Source | Reason |
|--------|--------|
| `dashboard.py` | Use Grafana instead |
| `metrics.py` (most of it) | Rebuild with clean Prometheus approach |
| `events.py` | Replace with Redis pub/sub |
| `main.py` | Complete rewrite |
| `position_manager.py` | Logic absorbed into other services |

### Database Schema to Migrate

```sql
-- Port these tables from polyjuiced
trades                  -- Trade history
settlement_queue        -- Pending settlements
daily_stats            -- Daily performance stats
fill_records           -- For slippage analysis
trade_telemetry        -- Timing data

-- Don't port
markets                -- Will re-discover
logs                   -- Use structured logging
liquidity_snapshots    -- Rebuild if needed
```

---

## 9. Implementation Phases

### Phase 1: Core Infrastructure (Week 1)

**Goal:** Redis event bus, config system, metrics skeleton, project structure.

**Tasks:**
1. Set up project with pyproject.toml, uv/poetry
2. Implement ConfigManager with TOML loading
3. Implement EventBus with Redis pub/sub
4. Set up structured logging
5. Create MetricsEmitter skeleton
6. Create Dockerfile and docker-compose.yml
7. Add pytest infrastructure

**Deliverable:** Running skeleton that loads config, connects to Redis, exposes /metrics.

**Tests:**
- Config loading from TOML + env vars
- EventBus publish/subscribe
- Docker build and startup

### Phase 2: Integration Layer (Week 2)

**Goal:** Port CLOB, Gamma, and WebSocket clients.

**Tasks:**
1. Port GammaClient from polyjuiced
2. Port CLOBClient from polyjuiced (cleanup)
3. Rewrite WebSocket handler with EventBus
4. Add Binance price feed adapter
5. Integration tests with mocked APIs

**Deliverable:** Can connect to Polymarket, fetch markets, subscribe to data.

**Tests:**
- Gamma market discovery
- CLOB order book fetch
- WebSocket connection/reconnection
- Price feed subscription

### Phase 3: Market Data Service (Week 3)

**Goal:** Real-time market data streaming and state management.

**Tasks:**
1. Implement MarketDataService
2. Order book state management
3. Market staleness detection
4. Market discovery integration (MarketFinder)
5. EventBus integration for data publishing

**Deliverable:** Continuous market data streaming, order book snapshots published.

**Tests:**
- Order book update processing
- Staleness detection
- Multi-market subscriptions

### Phase 4: State Store and Persistence (Week 3-4)

**Goal:** SQLite persistence with migrated schema.

**Tasks:**
1. Implement StateStore with async SQLite
2. Port database schema from polyjuiced
3. Implement trade/position CRUD
4. Implement settlement queue
5. Write migration script for existing data

**Deliverable:** Full persistence layer with all trading operations.

**Tests:**
- Trade recording and retrieval
- Position lifecycle
- Settlement queue operations
- Data migration from polyjuiced

### Phase 5: Execution Engine (Week 4)

**Goal:** Order execution with proper lifecycle management.

**Tasks:**
1. Implement ExecutionEngine
2. Single order execution
3. Dual-leg arbitrage execution
4. Order cancellation
5. Retry logic with backoff
6. EventBus integration

**Deliverable:** Can execute orders, handle partial fills, emit events.

**Tests:**
- Order placement (mocked)
- Partial fill handling
- Dual-leg execution scenarios
- Order cancellation

### Phase 6: Strategy Engine + Gabagool Port (Week 5)

**Goal:** Strategy plugin system with working gabagool.

**Tasks:**
1. Implement StrategyEngine
2. Implement BaseStrategy protocol
3. Port gabagool strategy logic
4. Strategy configuration
5. Runtime enable/disable
6. Signal generation flow

**Deliverable:** Gabagool strategy running, generating signals.

**Tests:**
- Strategy registration and discovery
- Market data routing
- Signal generation
- Enable/disable at runtime

### Phase 7: Risk Manager (Week 5-6)

**Goal:** Pre-trade validation and circuit breakers.

**Tasks:**
1. Implement RiskManager
2. Port circuit breaker logic
3. Position limit enforcement
4. Daily loss limits
5. Signal validation flow
6. EventBus integration

**Deliverable:** Signals validated, rejected if risk limits exceeded.

**Tests:**
- Pre-trade validation
- Circuit breaker states
- Daily limit enforcement
- Exposure tracking

### Phase 8: Settlement Manager (Week 6)

**Goal:** Automatic position settlement.

**Tasks:**
1. Implement SettlementManager
2. Port settlement queue logic
3. CTF redemption integration
4. Claim retry logic
5. Settlement metrics

**Deliverable:** Positions automatically settled after market resolution.

**Tests:**
- Settlement queue processing
- Claim execution
- Retry on failure
- Persistence across restarts

### Phase 9: Polish and Additional Strategies (Week 7+)

**Goal:** Production readiness.

**Tasks:**
1. Grafana dashboard setup
2. Runbook documentation
3. Health check endpoint
4. Graceful shutdown
5. Port additional strategies if needed
6. Load testing
7. Production deployment

**Deliverable:** Production-ready system with monitoring.

---

## 10. Testing Strategy

### Unit Tests (`tests/unit/`)

Test individual components in isolation:

```python
# Example: test_risk_manager.py
async def test_pre_trade_rejects_when_exposure_limit_reached():
    risk = RiskManager(mock_config, mock_state_store)
    risk._daily_exposure = Decimal("100.0")  # At limit

    signal = TradingSignal(target_size_usd=Decimal("10.0"), ...)
    allowed, reason = risk.check_pre_trade(signal)

    assert not allowed
    assert "exposure limit" in reason.lower()
```

**Coverage targets:**
- ConfigManager: 95%
- RiskManager: 95%
- Domain models: 90%
- Strategy logic: 90%

### Integration Tests (`tests/integration/`)

Test component interactions with real Redis:

```python
# Example: test_event_flow.py
async def test_signal_flows_through_risk_to_execution():
    # Start all services
    market_data = MarketDataService(...)
    strategy_engine = StrategyEngine(...)
    risk_manager = RiskManager(...)
    execution = ExecutionEngine(...)

    # Inject market data
    await event_bus.publish("market.orderbook.test", mock_orderbook)

    # Wait for signal to flow through
    executed_orders = await wait_for_events("order.submitted", timeout=5)

    assert len(executed_orders) == 2  # Dual-leg
```

### End-to-End Tests (`tests/e2e/`)

Test full trading scenarios with mocked exchange:

```python
# Example: test_arbitrage_e2e.py
async def test_full_arbitrage_trade_lifecycle():
    """
    1. Market data shows arbitrage opportunity
    2. Signal generated by strategy
    3. Risk approves
    4. Execution places dual-leg order
    5. Orders fill
    6. Position recorded
    7. Market resolves
    8. Settlement claims proceeds
    """
    ...
```

### Mock Market Data

```python
class MockMarketData:
    """Generate realistic market data for testing."""

    def arbitrage_opportunity(
        self,
        spread_cents: float = 2.0,
        yes_price: Decimal = Decimal("0.49"),
    ) -> OrderBookSnapshot:
        """Generate order book with arbitrage opportunity."""
        no_price = Decimal("1.0") - yes_price - Decimal(str(spread_cents / 100))
        return OrderBookSnapshot(
            yes_best_ask=yes_price,
            no_best_ask=no_price,
            ...
        )
```

### Backtesting (Future)

Backtesting is a "nice to have" - design the system to support it later:

1. **Event replay:** Record all market events to file/database
2. **Deterministic execution:** Mock ExecutionEngine for backtest
3. **Time simulation:** Virtual clock for fast-forward

```python
# Future: BacktestRunner
class BacktestRunner:
    async def run(self, start: datetime, end: datetime) -> BacktestResult:
        """Replay historical events through strategies."""
```

---

## 11. Deployment

### Docker Images

```dockerfile
# Dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install uv && uv sync --frozen

COPY src/ ./src/
COPY config/ ./config/

ENV PYTHONPATH=/app/src
CMD ["python", "-m", "mercury"]
```

### Docker Compose (Development)

```yaml
# docker-compose.yml
version: "3.8"

services:
  mercury:
    build: .
    environment:
      - MERCURY_DRY_RUN=true
      - MERCURY_REDIS_URL=redis://redis:6379
    depends_on:
      - redis
    volumes:
      - ./data:/app/data
    ports:
      - "9090:9090"  # Metrics

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"

  prometheus:
    image: prom/prometheus
    volumes:
      - ./docker/prometheus.yml:/etc/prometheus/prometheus.yml
    ports:
      - "9091:9090"

  grafana:
    image: grafana/grafana
    ports:
      - "3000:3000"
    volumes:
      - ./docker/grafana/dashboards:/etc/grafana/provisioning/dashboards
```

### Production Deployment

**Homelab (Docker):**
- Deploy via docker-compose.prod.yml
- Mount persistent volume for SQLite
- Connect to homelab Prometheus/Grafana

**Cloud VPS:**
- Same Docker image
- Use managed Redis (Upstash/Redis Cloud) or co-located container
- Persistent volume for database

### Health Check

```python
# scripts/health_check.py
async def check_health() -> dict:
    return {
        "status": "healthy",
        "redis_connected": await redis.ping(),
        "websocket_connected": market_data.is_connected,
        "circuit_breaker": risk.circuit_breaker_state.name,
        "uptime_seconds": time.time() - start_time,
    }
```

---

## Appendix A: Migration Checklist

### Before Starting Mercury

- [ ] Export polyjuiced SQLite database
- [ ] Document current production config
- [ ] Capture Prometheus metrics for baseline
- [ ] Create backup of polyjuiced codebase

### Migration Steps

1. [ ] Run Mercury in dry-run alongside polyjuiced
2. [ ] Compare signal generation between systems
3. [ ] Validate order execution matches
4. [ ] Migrate database (run migration script)
5. [ ] Switch traffic to Mercury
6. [ ] Monitor for 24 hours
7. [ ] Decommission polyjuiced

---

## Appendix B: Decision Log

| Date | Decision | Rationale | Alternatives Considered |
|------|----------|-----------|------------------------|
| 2026-01-17 | Redis pub/sub for events | Process isolation, dashboard decoupling, proven at scale | In-process asyncio queues (no isolation), RabbitMQ (overkill) |
| 2026-01-17 | Python over Rust/Go | Team familiarity, py-clob-client SDK, async ecosystem | Rust (learning curve), Go (SDK rewrite needed) |
| 2026-01-17 | TOML for config | Human-readable, supports comments, native Python 3.11 | YAML (ambiguous types), JSON (no comments) |
| 2026-01-17 | Grafana over custom dashboard | Standard tooling, less code to maintain | Custom dashboard (coupling, maintenance burden) |
| 2026-01-17 | SQLite over PostgreSQL | Proven schema from polyjuiced, simple deployment | PostgreSQL (overkill for single-node) |
| 2026-01-17 | Clean-slate over refactor | 3400-line god class too tangled, faster to rebuild | Incremental refactor (death by 1000 cuts) |

---

## Appendix C: Success Criteria

### Phase 1 Complete When:
- [ ] Can load config from TOML + env vars
- [ ] Redis pub/sub working
- [ ] Prometheus metrics exposed
- [ ] Docker container builds and runs

### Phase 6 Complete When:
- [ ] Gabagool strategy generates signals
- [ ] Orders execute on testnet/paper
- [ ] Signals flow through risk manager
- [ ] All events published correctly

### Production Ready When:
- [ ] 24-hour dry-run with no errors
- [ ] Metrics in Grafana
- [ ] All Phase 1-8 tests passing
- [ ] Runbook documented
- [ ] Health check working

---

*End of Clean-Slate Rebuild Plan*
