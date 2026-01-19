# Mercury - Polymarket Trading Bot

**Status**: Production-ready
**Replaces**: polyjuiced (legacy code in `legacy/` directory - reference only)

## Quick Start

```bash
# Development (dry-run mode)
cd mercury
pip install -e .[dev]
python -m mercury --dry-run

# Run tests
pytest tests/ -v

# Check health
python -m mercury health

# Production (Docker)
cd mercury/docker
docker compose -f docker-compose.prod.yml up -d
```

## Overview

Mercury is an event-driven Polymarket trading bot built on clean architecture principles. It replaces the legacy polyjuiced codebase which suffered from tight coupling and a 3,400-line god class.

**Core Principles:**
1. **Event-driven architecture** - Components communicate via Redis pub/sub
2. **Single responsibility** - Each service does ONE thing well
3. **Strategy as plugins** - Strategies are isolated, discovered automatically
4. **Observability** - Prometheus metrics, structured logging, health endpoints
5. **Fail gracefully** - Circuit breakers, retries, graceful shutdown

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                    OBSERVABILITY (Emit Only)                     │
│              Prometheus Metrics → Grafana Dashboards             │
└──────────────────────────────────────────────────────────────────┘
                                ▲ emit only
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

### Service Responsibilities

| Service | Responsibility |
|---------|----------------|
| **MarketDataService** | Stream market data, maintain order books |
| **StrategyEngine** | Load strategies, route data, collect signals |
| **RiskManager** | Validate signals, enforce limits, circuit breaker |
| **ExecutionEngine** | Submit orders, track lifecycle |
| **StateStore** | Persist positions, trades, daily stats |
| **SettlementManager** | Claim resolved positions on-chain |
| **MetricsEmitter** | Emit Prometheus metrics |
| **HealthService** | Health check endpoint at `:9090/health` |

## Project Structure

```
polyjuiced/
├── AGENTS.md                 # This file (agent instructions)
├── legacy/                   # OLD polyjuiced code (reference only)
│   ├── src/                  # Do NOT modify
│   └── docs/
│
├── mercury/                  # Current implementation
│   ├── pyproject.toml        # Project metadata and dependencies
│   ├── README.md             # Quick reference
│   │
│   ├── config/
│   │   ├── default.toml      # Base configuration
│   │   └── production.toml   # Production overrides
│   │
│   ├── docker/
│   │   ├── Dockerfile        # Multi-stage build
│   │   ├── docker-compose.yml         # Local development
│   │   └── docker-compose.prod.yml    # Production deployment
│   │
│   ├── src/mercury/
│   │   ├── __init__.py
│   │   ├── __main__.py       # CLI entry point
│   │   ├── app.py            # Application lifecycle
│   │   │
│   │   ├── core/             # Framework (no business logic)
│   │   │   ├── config.py     # TOML config + env vars
│   │   │   ├── events.py     # EventBus (Redis pub/sub)
│   │   │   ├── logging.py    # Structured logging (structlog)
│   │   │   ├── lifecycle.py  # Start/stop protocols
│   │   │   ├── retry.py      # Tenacity retry utilities
│   │   │   └── shutdown.py   # Graceful shutdown manager
│   │   │
│   │   ├── domain/           # Pure models (no I/O)
│   │   │   ├── market.py     # Market metadata
│   │   │   ├── orderbook.py  # OrderBook (sorted containers)
│   │   │   ├── order.py      # Order, Position, Fill
│   │   │   ├── signal.py     # TradingSignal
│   │   │   ├── risk.py       # RiskLimits, CircuitBreakerState
│   │   │   └── events.py     # Domain event types
│   │   │
│   │   ├── services/         # Business logic services
│   │   │   ├── market_data.py
│   │   │   ├── strategy_engine.py
│   │   │   ├── risk_manager.py
│   │   │   ├── execution.py
│   │   │   ├── state_store.py
│   │   │   ├── settlement.py
│   │   │   ├── metrics.py
│   │   │   ├── health.py
│   │   │   └── migrations/   # Database migrations
│   │   │
│   │   ├── strategies/       # Strategy plugins
│   │   │   ├── base.py       # BaseStrategy protocol
│   │   │   ├── registry.py   # Auto-discovery
│   │   │   └── gabagool/     # Arbitrage strategy
│   │   │       ├── __init__.py
│   │   │       ├── config.py
│   │   │       └── strategy.py
│   │   │
│   │   ├── integrations/     # External service adapters
│   │   │   ├── polymarket/
│   │   │   │   ├── clob.py        # CLOB API client
│   │   │   │   ├── gamma.py       # Gamma API client
│   │   │   │   ├── websocket.py   # Real-time market data
│   │   │   │   ├── market_finder.py
│   │   │   │   └── types.py
│   │   │   ├── price_feeds/
│   │   │   │   ├── base.py
│   │   │   │   └── binance.py
│   │   │   └── chain/
│   │   │       ├── client.py      # Polygon RPC client
│   │   │       └── ctf.py         # CTF contract interactions
│   │   │
│   │   ├── validation/       # Parallel validation framework
│   │   │   └── parallel_validator.py
│   │   │
│   │   └── cli/              # CLI commands
│   │
│   ├── scripts/
│   │   ├── migrate_from_polyjuiced.py  # Legacy migration
│   │   └── parallel_validation.py      # Validation runner
│   │
│   └── tests/
│       ├── conftest.py       # Shared fixtures
│       ├── unit/             # Unit tests
│       ├── integration/      # Integration tests
│       ├── smoke/            # Phase smoke tests
│       └── performance/      # Load tests
│
└── agents/
    └── plans/
        └── clean-slate-rebuild-plan.md  # Original architecture doc
```

## How to Run

### Local Development

```bash
cd mercury

# Install in development mode
pip install -e .[dev]

# Run with dry-run (no real trades)
python -m mercury --dry-run

# Run with custom config
python -m mercury --config config/development.toml

# Run with debug logging
python -m mercury --log-level DEBUG

# Check health
python -m mercury health

# Show version
python -m mercury version
```

### Docker Development

```bash
cd mercury/docker

# Start all services (Mercury, Redis, Prometheus, Grafana)
docker compose up -d

# View logs
docker compose logs -f mercury

# Stop
docker compose down
```

### Production Deployment

```bash
cd mercury/docker

# Create external network (once)
docker network create my-network

# Set up credentials in .env
cat > .env << 'EOF'
MERCURY_POLYMARKET_PRIVATE_KEY=your_key_here
MERCURY_POLYMARKET_API_KEY=your_key_here
MERCURY_POLYMARKET_API_SECRET=your_secret_here
MERCURY_POLYMARKET_API_PASSPHRASE=your_passphrase_here
MERCURY_POLYGON_PRIVATE_KEY=your_polygon_key  # Optional
GRAFANA_ADMIN_PASSWORD=secure_password
EOF

# Deploy
docker compose -f docker-compose.prod.yml up -d
```

**Production URLs:**
- `https://mercury.server.unarmedpuppy.com/health` - Health check
- `https://mercury-grafana.server.unarmedpuppy.com` - Dashboards
- `https://mercury-prometheus.server.unarmedpuppy.com` - Metrics

## Configuration

Configuration uses TOML files with environment variable overrides.

### Config Priority (highest to lowest)

1. Command-line arguments (`--dry-run`, `--log-level`)
2. Environment variables (`MERCURY_*`)
3. Custom config file (`--config path/to/config.toml`)
4. Production config (`config/production.toml`)
5. Default config (`config/default.toml`)

### Key Configuration Sections

```toml
[mercury]
log_level = "INFO"
log_json = false
dry_run = true  # ALWAYS default to dry run

[redis]
url = "redis://localhost:6379"

[database]
path = "./data/mercury.db"

[polymarket]
clob_url = "https://clob.polymarket.com/"
gamma_url = "https://gamma-api.polymarket.com"
ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
# Credentials from env: MERCURY_POLYMARKET_*

[metrics]
enabled = true
port = 9090

[risk]
max_daily_loss_usd = 100.0
max_position_size_usd = 25.0
max_daily_trades = 100
circuit_breaker_cooldown_minutes = 5

[strategies.gabagool]
enabled = true
markets = ["BTC", "ETH", "SOL"]
min_spread_threshold = 0.015
max_trade_size_usd = 25.0
```

### Environment Variables

All config values can be overridden via environment:

```bash
# Pattern: MERCURY_SECTION_KEY
MERCURY_DRY_RUN=true
MERCURY_LOG_LEVEL=DEBUG
MERCURY_REDIS_URL=redis://localhost:6379
MERCURY_RISK_MAX_DAILY_LOSS_USD=50.0

# Credentials (always via env, never in config files)
MERCURY_POLYMARKET_PRIVATE_KEY=0x...
MERCURY_POLYMARKET_API_KEY=...
MERCURY_POLYMARKET_API_SECRET=...
MERCURY_POLYMARKET_API_PASSPHRASE=...
MERCURY_POLYGON_PRIVATE_KEY=0x...
```

## Adding New Strategies

### 1. Create Strategy Directory

```bash
mkdir -p mercury/src/mercury/strategies/my_strategy
touch mercury/src/mercury/strategies/my_strategy/__init__.py
```

### 2. Implement Strategy Class

Create `mercury/src/mercury/strategies/my_strategy/strategy.py`:

```python
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import AsyncIterator

import structlog

from mercury.core.config import ConfigManager
from mercury.domain.market import OrderBook
from mercury.domain.signal import SignalPriority, SignalType, TradingSignal

log = structlog.get_logger()


class MyStrategy:
    """My custom trading strategy.

    Implements the BaseStrategy protocol for automatic discovery.
    """

    def __init__(self, config: ConfigManager) -> None:
        self._config = config
        self._enabled = config.get_bool("strategies.my_strategy.enabled", False)
        self._log = log.bind(strategy="my_strategy")
        self._subscribed_markets: list[str] = []

    @property
    def name(self) -> str:
        """Unique strategy identifier."""
        return "my_strategy"

    @property
    def enabled(self) -> bool:
        """Whether strategy is enabled."""
        return self._enabled

    async def start(self) -> None:
        """Initialize strategy resources."""
        self._log.info("strategy_started")

    async def stop(self) -> None:
        """Cleanup strategy resources."""
        self._log.info("strategy_stopped")

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    def get_subscribed_markets(self) -> list[str]:
        """Markets this strategy wants data for."""
        return self._subscribed_markets

    async def on_market_data(
        self,
        market_id: str,
        book: OrderBook,
    ) -> AsyncIterator[TradingSignal]:
        """Process market data and yield signals.

        This is an async generator - yield 0, 1, or many signals.
        """
        if not self._enabled:
            return

        # Your signal detection logic here
        opportunity = self._detect_opportunity(book)
        if opportunity:
            yield TradingSignal(
                strategy_name=self.name,
                market_id=market_id,
                signal_type=SignalType.DIRECTIONAL,
                confidence=0.75,
                priority=SignalPriority.MEDIUM,
                target_size_usd=Decimal("10.0"),
                yes_price=book.yes_best_ask,
                no_price=book.no_best_ask,
                expected_pnl=Decimal("0.50"),
                max_slippage=Decimal("0.01"),
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
            )

    def _detect_opportunity(self, book: OrderBook) -> bool:
        """Your custom detection logic."""
        # Implement your strategy logic
        return False
```

### 3. Export in `__init__.py`

```python
# mercury/src/mercury/strategies/my_strategy/__init__.py
from .strategy import MyStrategy

__all__ = ["MyStrategy"]
```

### 4. Add Configuration

Add to `config/default.toml`:

```toml
[strategies.my_strategy]
enabled = false  # Disable by default
# Your strategy-specific config
my_param = 0.05
```

### 5. Add Tests

Create `mercury/tests/unit/test_my_strategy.py`:

```python
import pytest
from decimal import Decimal
from mercury.strategies.my_strategy import MyStrategy
from mercury.domain.market import OrderBook


@pytest.fixture
def strategy(mock_config):
    return MyStrategy(mock_config)


async def test_strategy_name(strategy):
    assert strategy.name == "my_strategy"


async def test_disabled_yields_nothing(strategy):
    strategy._enabled = False
    book = OrderBook(market_id="test", yes_token_id="1", no_token_id="2")

    signals = [s async for s in strategy.on_market_data("test", book)]

    assert len(signals) == 0
```

### 6. Enable in Config

To use the strategy, enable it in your config:

```toml
[strategies.my_strategy]
enabled = true
```

The strategy will be auto-discovered on startup.

## Testing

```bash
cd mercury

# Run all tests
pytest tests/ -v

# Run unit tests only
pytest tests/unit/ -v

# Run with coverage
pytest tests/ --cov=mercury --cov-report=html

# Run specific test file
pytest tests/unit/test_risk_manager.py -v

# Run smoke tests
pytest tests/smoke/ -v
```

### Test Categories

| Directory | Purpose |
|-----------|---------|
| `tests/unit/` | Unit tests (mocked dependencies) |
| `tests/integration/` | Integration tests (real Redis, etc.) |
| `tests/smoke/` | Phase smoke tests (end-to-end per phase) |
| `tests/performance/` | Load and performance tests |

## Deployment

### Tag-Based Deployment

Mercury uses Gitea Actions for CI/CD. Tag a release to trigger build and deploy:

```bash
git tag v1.0.0
git push origin v1.0.0
```

This will:
1. Build Docker image
2. Push to Harbor registry
3. Deploy via Watchtower (auto-updates)

### Manual Deployment

```bash
cd mercury/docker

# Build image
docker build -t harbor.server.unarmedpuppy.com/library/mercury:latest -f Dockerfile ..

# Push to registry
docker push harbor.server.unarmedpuppy.com/library/mercury:latest

# Deploy
docker compose -f docker-compose.prod.yml up -d
```

## Monitoring

### Health Check

```bash
curl http://localhost:9090/health
```

Returns:
```json
{
  "status": "healthy",
  "uptime_seconds": 3600,
  "components": {
    "redis": {"status": "connected"},
    "market_data": {"status": "healthy"}
  }
}
```

### Metrics

Prometheus metrics available at `:9090/metrics`:

- `mercury_uptime_seconds` - Application uptime
- `mercury_signals_generated_total` - Trading signals by strategy
- `mercury_orders_executed_total` - Orders by status
- `mercury_daily_pnl_usd` - Daily P&L
- `mercury_circuit_breaker_state` - Circuit breaker status

### Grafana Dashboards

Pre-configured dashboards at `https://mercury-grafana.server.unarmedpuppy.com`:

- **Trading Overview** - P&L, trades, signals
- **Market Data** - Order book health, latency
- **Risk** - Position exposure, circuit breaker
- **System** - CPU, memory, Redis

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

### Structured Logging

```python
import structlog
log = structlog.get_logger()

log.info("order_executed",
    order_id=order.id,
    market_id=order.market_id,
    size=str(order.size),
    latency_ms=latency,
)
```

### Event Bus Communication

```python
# Always publish events, never call services directly
await self.event_bus.publish(f"signal.{self.name}", signal.to_dict())
```

## Task Tracking

All Mercury tasks are tracked centrally in the beads database:

```bash
# From home-server directory
cd ../home-server

bd list --label mercury              # All Mercury tasks
bd ready --label mercury             # Unblocked tasks
bd show <task-id>                    # Task details
```

## Legacy Code Reference

The `legacy/` directory contains the original polyjuiced implementation. Use it as reference for:

- **Port**: Signal detection logic, API client implementations, market filtering
- **Don't port**: Architecture patterns, dashboard coupling, global state

When porting logic:
1. Extract the LOGIC, not the structure
2. Remove dashboard/metrics coupling
3. Add type hints and async
4. Add tenacity retries for external calls

## Boundaries

### Always Do
- Communicate via EventBus
- Use async for I/O operations
- Add retries for external calls
- Emit metrics for observability
- Write tests alongside code
- Use structured logging

### Ask First
- Changing event schemas
- Adding new dependencies
- Modifying core/ components
- Cross-service refactoring

### Never Do
- Direct coupling between services
- Synchronous blocking calls in hot path
- Hardcoded configuration values
- Skip writing tests
- Pull Docker images directly from Docker Hub (use Harbor)

## Reference Documents

- [Clean-Slate Rebuild Plan](agents/plans/clean-slate-rebuild-plan.md)
- [Beads Task Tracking](../home-server/AGENTS.md)
- [Homelab Deployment Patterns](../home-server/agents/reference/)
