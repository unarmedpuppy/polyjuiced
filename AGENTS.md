# Polymarket Bot - Agent Instructions

Automated arbitrage trading bot for Polymarket prediction markets.

## Overview

Targets 15-minute up/down markets (BTC, ETH, SOL) with multiple strategies:
- **Gabagool**: Statistical arbitrage on binary outcomes
- **Vol Happens**: Mean reversion strategy
- **Near Resolution**: Late-stage resolution betting

**Entry Point**: `src/main.py` runs `GabagoolBot` orchestrating market discovery, WebSocket data, strategy execution, and dashboard.

## Tech Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.11 |
| Polymarket SDK | py-clob-client |
| Async | httpx, aiohttp, websockets |
| Blockchain | web3 (Polygon) |
| Config | pydantic-settings |
| Database | SQLite + SQLAlchemy (async) |
| Logging | structlog |
| Metrics | Prometheus |
| Testing | pytest-asyncio |

## Project Structure

```
polymarket-bot/
├── src/
│   ├── main.py              # Entry point, GabagoolBot class
│   ├── config.py            # Environment config validation
│   ├── client/
│   │   ├── polymarket.py    # CLOB API client, order execution
│   │   └── websocket.py     # Real-time market data
│   ├── strategies/
│   │   ├── base.py          # Strategy base class
│   │   ├── gabagool.py      # Main arbitrage strategy
│   │   ├── vol_happens.py   # Mean reversion
│   │   └── near_resolution.py
│   ├── risk/
│   │   ├── circuit_breaker.py  # Multi-level safety controls
│   │   └── position_sizing.py  # Kelly criterion sizing
│   ├── monitoring/
│   │   ├── market_finder.py    # Discovers active markets
│   │   └── order_book.py       # Best bid/ask tracking
│   ├── dashboard.py         # Web UI (read-only)
│   ├── persistence.py       # Trade & settlement DB
│   ├── events.py            # Event system
│   └── metrics.py           # Prometheus metrics
├── scripts/                 # Utility scripts (diagnostics, reconciliation, testing)
├── tests/                   # Test suite (pytest-asyncio)
├── docs/
│   ├── STRATEGY_ARCHITECTURE.md  # Core strategy implementation
│   ├── REBALANCING_STRATEGY.md   # Position rebalancing logic
│   ├── LIQUIDITY_SIZING.md       # Liquidity-aware sizing
│   ├── strategy-rules.md         # Trading rules reference
│   └── *.md                      # Post-mortems, trade analysis, implementation plans
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Quick Commands

```bash
# Local development
cp .env.template .env       # Configure credentials
pip install -r requirements.txt
python -m src.main

# Docker (local)
docker compose up -d --build

# Run tests
pytest tests/ -v

# Run specific strategy tests
pytest tests/strategies/ -v
```

## Configuration

Copy `.env.template` to `.env`. Key variables:

### Required
```bash
# Polymarket API (from polymarket.com settings)
POLYMARKET_API_KEY=
POLYMARKET_API_SECRET=
POLYMARKET_PASSPHRASE=

# Wallet (for order signing)
PRIVATE_KEY=           # Polygon wallet private key
FUNDER_ADDRESS=        # Your wallet address
```

### Strategy Config
```bash
# Gabagool strategy
GABAGOOL_MIN_SPREAD=0.04      # Minimum profit spread
GABAGOOL_MAX_POSITION=50      # Max USDC per trade
GABAGOOL_DAILY_LIMIT=500      # Max daily exposure

# Risk limits
CIRCUIT_BREAKER_MAX_CONSECUTIVE_FAILURES=5
CIRCUIT_BREAKER_MAX_DAILY_LOSS=100
```

### Safety
```bash
DRY_RUN=true                  # ALWAYS start with dry run
BLACKOUT_START=05:00          # Server restart window (CST)
BLACKOUT_END=05:29
```

## Safety Mechanisms

**CRITICAL**: This bot trades real money. Understand these safeguards:

### Circuit Breaker (4 levels)
| Level | Trigger | Action |
|-------|---------|--------|
| NORMAL | Default | Full trading |
| WARNING | 3 consecutive failures | Reduced sizing |
| CAUTION | 5 failures OR -$50 daily | Minimal trading |
| HALT | 10 failures OR -$100 daily | No new trades |

### Order Safety
- **FOK Orders**: Fill-or-Kill for atomic execution
- **Zero Slippage**: Orders execute at exact price or fail
- **Liquidity Checks**: Pre-trade depth validation

### Position Limits
- Min/max trade sizes per strategy
- Daily exposure caps
- Per-window position limits
- Blackout window protection (5:00-5:29 AM CST)

## Deployment

### Production (via home-server)
```bash
# Deployed at: home-server/apps/polymarket-bot/
# Uses Harbor registry image

# On server:
cd ~/server/apps/polymarket-bot
docker compose up -d
```

### Monitoring
- **Dashboard**: http://localhost:8080 (when running)
- **Metrics**: Prometheus endpoint at /metrics
- **Logs**: `docker logs polymarket-bot -f`

## Testing

```bash
# Full test suite
pytest tests/ -v

# Strategy-specific
pytest tests/strategies/test_gabagool.py -v

# With coverage
pytest tests/ --cov=src --cov-report=html
```

## Definition of Done

A task is complete when:
- [ ] All tests pass (`pytest tests/ -v`)
- [ ] No new type errors (existing codebase uses runtime validation via Pydantic)
- [ ] Changes tested with `DRY_RUN=true` before any live testing
- [ ] If touching risk/order logic: manual review of edge cases
- [ ] If adding new config: `.env.template` updated

For strategy changes specifically:
- [ ] Backtested or paper-traded before live deployment
- [ ] Circuit breaker behavior verified
- [ ] Position sizing limits respected

## Boundaries

### Always Do
- Start with `DRY_RUN=true` when testing changes
- Check circuit breaker status before deploying
- Review settlement queue after restarts
- Test strategy changes on paper first

### Ask First
- Changing risk parameters
- Modifying order execution logic
- Adding new strategies
- Changing position sizing calculations

### Never Do
- Deploy without dry run testing first
- Disable circuit breaker in production
- Commit private keys or API credentials
- Modify live config without understanding impact

## Documentation

See `docs/STRATEGY_ARCHITECTURE.md` for:
- Detailed strategy implementation
- Market structure analysis
- Edge calculation methodology
- Settlement reconciliation

## See Also

- [Root AGENTS.md](../AGENTS.md) - Cross-project conventions
- [home-server deployment](../home-server/apps/polymarket-bot/)
