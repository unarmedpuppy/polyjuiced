# Mercury

An event-driven Polymarket trading bot built on clean architecture principles. Mercury replaces the legacy polyjuiced codebase, featuring modular services that communicate via Redis pub/sub, plugin-based strategies, and comprehensive observability.

## Features

- **Event-driven architecture** - Services communicate via Redis pub/sub, enabling loose coupling and scalability
- **Plugin-based strategies** - Strategies are auto-discovered and isolated; add new ones without modifying core code
- **Risk management** - Circuit breakers, position limits, and daily loss caps
- **Observability** - Prometheus metrics, Grafana dashboards, structured logging, and health endpoints
- **Graceful operations** - Proper shutdown handling, retry logic with exponential backoff

## Architecture

```
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

| Service | Responsibility |
|---------|----------------|
| **MarketDataService** | Stream market data, maintain order books |
| **StrategyEngine** | Load strategies, route data, collect signals |
| **RiskManager** | Validate signals, enforce limits, circuit breaker |
| **ExecutionEngine** | Submit orders, track lifecycle |
| **StateStore** | Persist positions, trades, daily stats |
| **SettlementManager** | Claim resolved positions on-chain |

## Quick Start

### Local Development

```bash
# Install in development mode
pip install -e .[dev]

# Run with dry-run (no real trades)
python -m mercury --dry-run

# Run with debug logging
python -m mercury --log-level DEBUG

# Check health
python -m mercury health

# Run tests
pytest tests/ -v
```

### Docker Development

```bash
cd docker

# Start all services (Mercury, Redis, Prometheus, Grafana)
docker compose up -d

# View logs
docker compose logs -f mercury
```

### Production Deployment

```bash
cd docker

# Set up credentials in .env
cat > .env << 'EOF'
MERCURY_POLYMARKET_PRIVATE_KEY=your_key_here
MERCURY_POLYMARKET_API_KEY=your_key_here
MERCURY_POLYMARKET_API_SECRET=your_secret_here
MERCURY_POLYMARKET_API_PASSPHRASE=your_passphrase_here
EOF

# Deploy
docker compose -f docker-compose.prod.yml up -d
```

## Configuration

Configuration uses TOML files with environment variable overrides.

**Priority (highest to lowest):**
1. Command-line arguments (`--dry-run`, `--log-level`)
2. Environment variables (`MERCURY_*`)
3. Custom config file (`--config path/to/config.toml`)
4. `config/production.toml` → `config/default.toml`

### Key Settings

```toml
[mercury]
dry_run = true          # Safety: always default to dry run

[risk]
max_daily_loss_usd = 100.0
max_position_size_usd = 25.0
max_daily_trades = 100

[strategies.gabagool]
enabled = true
markets = ["BTC", "ETH", "SOL"]
min_spread_threshold = 0.015
```

### Environment Variables

```bash
# Pattern: MERCURY_SECTION_KEY
MERCURY_DRY_RUN=true
MERCURY_LOG_LEVEL=DEBUG
MERCURY_RISK_MAX_DAILY_LOSS_USD=50.0

# Credentials (never in config files)
MERCURY_POLYMARKET_PRIVATE_KEY=0x...
MERCURY_POLYMARKET_API_KEY=...
```

## Project Structure

```
mercury/
├── config/               # TOML configuration files
├── docker/               # Docker Compose files
├── src/mercury/
│   ├── core/             # Framework (config, events, logging)
│   ├── domain/           # Pure models (no I/O)
│   ├── services/         # Business logic services
│   ├── strategies/       # Strategy plugins
│   └── integrations/     # External service adapters
└── tests/
    ├── unit/             # Unit tests
    ├── integration/      # Integration tests
    └── smoke/            # End-to-end tests
```

## Adding Strategies

Create a new strategy in `src/mercury/strategies/`:

```python
class MyStrategy:
    @property
    def name(self) -> str:
        return "my_strategy"

    async def on_market_data(
        self,
        market_id: str,
        book: OrderBook,
    ) -> AsyncIterator[TradingSignal]:
        # Your signal detection logic
        yield TradingSignal(...)
```

Enable in config:

```toml
[strategies.my_strategy]
enabled = true
```

## Monitoring

- **Health endpoint**: `GET :9090/health`
- **Prometheus metrics**: `GET :9090/metrics`
- **Grafana dashboards**: Trading overview, risk, market data, system

## Deployment Next Steps

### Pre-Deployment Checklist

- [ ] **Credentials configured** - Set up `.env` file with Polymarket API keys and private key
- [ ] **Risk limits tuned** - Review `config/production.toml` risk parameters for your tolerance
- [ ] **Strategy enabled** - Enable at least one strategy in config (gabagool is available)
- [ ] **Dry-run validation** - Run locally with `--dry-run` to verify signal generation

### Initial Production Deployment

1. **Create external network** (one-time):
   ```bash
   docker network create my-network
   ```

2. **Deploy stack**:
   ```bash
   cd docker
   docker compose -f docker-compose.prod.yml up -d
   ```

3. **Verify health**:
   ```bash
   curl https://mercury.server.unarmedpuppy.com/health
   ```

### Enabling Live Trading

Once dry-run validation looks good:

1. Set `MERCURY_DRY_RUN=false` in `.env`
2. Start with conservative risk limits (low `max_daily_loss_usd`, small `max_position_size_usd`)
3. Monitor via Grafana dashboards at `https://mercury-grafana.server.unarmedpuppy.com`
4. Watch logs: `docker compose -f docker-compose.prod.yml logs -f mercury`

### CI/CD Pipeline

Tag a release to trigger automated build and deploy:

```bash
git tag v1.0.0
git push origin v1.0.0
```

Watchtower handles rolling updates automatically.

## Documentation

See [`../AGENTS.md`](../AGENTS.md) for complete documentation including:
- Detailed architecture explanation
- Full configuration reference
- Strategy development guide
- Deployment procedures
- Coding standards
