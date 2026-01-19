# Mercury Operations Runbook

This runbook covers operational procedures for the Mercury trading bot including startup/shutdown, health monitoring, settlement management, strategy control, and emergency procedures.

## Table of Contents

1. [Quick Reference](#quick-reference)
2. [Startup Procedures](#startup-procedures)
3. [Shutdown Procedures](#shutdown-procedures)
4. [Health Monitoring](#health-monitoring)
5. [Settlement Operations](#settlement-operations)
6. [Strategy Management](#strategy-management)
7. [Circuit Breaker & Risk Controls](#circuit-breaker--risk-controls)
8. [Common Issues & Solutions](#common-issues--solutions)
9. [Emergency Procedures](#emergency-procedures)
10. [Metrics Reference](#metrics-reference)

---

## Quick Reference

| Component | URL/Command |
|-----------|-------------|
| Start Mercury | `python -m mercury` |
| Health Check | `python -m mercury health` |
| Metrics | `http://localhost:9090/metrics` |
| Grafana | `http://localhost:3000` (admin/mercury) |
| Prometheus | `http://localhost:9091` |
| Redis | `redis://localhost:6379` |

### Key Configuration Files

| File | Purpose |
|------|---------|
| `config/default.toml` | Base configuration (always loaded) |
| `config/development.toml` | Development overrides |
| `config/production.toml` | Production overrides |
| Environment variables | `MERCURY_*` prefix (highest priority) |

---

## Startup Procedures

### Prerequisites

Before starting Mercury:

1. **Redis must be running** - Mercury uses Redis for the event bus
2. **Polymarket credentials configured** (for live trading)
3. **Polygon RPC URL accessible** (for settlement)

### Starting Local Development

```bash
# 1. Start infrastructure (Redis, Prometheus, Grafana)
cd /workspace/polyjuiced/mercury
docker compose -f docker/docker-compose.yml up -d redis prometheus grafana

# 2. Wait for Redis to be ready
docker compose -f docker/docker-compose.yml logs redis
# Should show: "Ready to accept connections"

# 3. Start Mercury (dry-run mode by default)
python -m mercury
```

### Starting with Docker

```bash
# Start all services including Mercury
cd /workspace/polyjuiced/mercury
docker compose -f docker/docker-compose.yml up -d

# View logs
docker compose -f docker/docker-compose.yml logs -f mercury
```

### Starting in Production

```bash
# 1. Verify credentials are set
echo $MERCURY_POLYMARKET_PRIVATE_KEY  # Should not be empty
echo $MERCURY_POLYMARKET_API_KEY      # Should not be empty

# 2. Start with production config
python -m mercury --config config/production.toml

# 3. To enable live trading (disable dry-run)
python -m mercury --config config/production.toml --dry-run false
```

### Verifying Startup

After starting, verify the system is healthy:

```bash
# Check health endpoint
python -m mercury health

# Expected output:
# Status: healthy
# Uptime: 15s
#   event_bus: healthy

# Check metrics are being emitted
curl -s http://localhost:9090/metrics | head -20
```

### Startup Log Events

Watch for these log events during startup:

| Event | Meaning |
|-------|---------|
| `starting_mercury` | Application initializing |
| `event_bus_connected` | Redis connection established |
| `market_data_connected` | WebSocket connected to Polymarket |
| `strategy_engine_started` | Strategies loaded and active |
| `risk_manager_started` | Risk checks active |
| `settlement_manager_started` | Settlement monitoring active |
| `mercury_started` | All systems operational |

---

## Shutdown Procedures

### Graceful Shutdown

Mercury handles SIGINT and SIGTERM for graceful shutdown:

```bash
# Option 1: Ctrl+C in terminal
# Option 2: Send SIGTERM
kill -TERM $(pgrep -f "python -m mercury")
```

### Shutdown Sequence

Mercury performs these steps on shutdown:

1. Sets shutdown flag (stops accepting new signals)
2. Cancels all background tasks
3. Disconnects from event bus (Redis)
4. Closes database connections
5. Flushes pending metrics
6. Exits cleanly

### Shutdown Log Events

| Event | Meaning |
|-------|---------|
| `shutdown_signal_received` | Shutdown initiated |
| `stopping_mercury` | Beginning shutdown sequence |
| `event_bus_disconnected` | Redis disconnected |
| `mercury_stopped` | Shutdown complete |

### Docker Shutdown

```bash
# Graceful stop (waits up to 30s)
docker compose -f docker/docker-compose.yml stop mercury

# Full shutdown (all services)
docker compose -f docker/docker-compose.yml down

# Full shutdown with volume cleanup
docker compose -f docker/docker-compose.yml down -v
```

---

## Health Monitoring

### Health Check Endpoints

```bash
# CLI health check
python -m mercury health

# HTTP health endpoint (when running)
curl -s http://localhost:9090/health | jq .
```

### Health Response Format

```json
{
  "status": "healthy",
  "message": "OK",
  "details": {
    "uptime_seconds": 3600.5,
    "dry_run": true
  },
  "checked_at": "2026-01-19T10:30:00.000000+00:00"
}
```

### Health Status Levels

| Status | Meaning | Action |
|--------|---------|--------|
| `healthy` | All systems operational | None required |
| `degraded` | Some issues but functional | Investigate |
| `unhealthy` | Critical failure | Immediate attention |

### What Can Cause Degraded Status

- Event bus disconnected
- Circuit breaker in HALT state
- No strategies enabled
- Settlement queue backlog (>50 items)

### Monitoring with Grafana

Access Grafana at `http://localhost:3000` (admin/mercury)

Available dashboards:
- **Trading Overview** - Real-time trading activity
- **Execution Quality** - Latency and fill metrics
- **Risk Status** - Circuit breaker, exposure, P&L
- **System Health** - Connections, uptime, errors

### Key Metrics to Watch

```bash
# Daily P&L
curl -s http://localhost:9090/metrics | grep mercury_daily_pnl

# Circuit breaker state (0=NORMAL, 1=WARNING, 2=CAUTION, 3=HALT)
curl -s http://localhost:9090/metrics | grep mercury_circuit_breaker

# Settlement queue depth
curl -s http://localhost:9090/metrics | grep mercury_settlement_queue_depth

# Active positions
curl -s http://localhost:9090/metrics | grep mercury_active_positions
```

---

## Settlement Operations

### How Settlement Works

1. Position opened → Queued for settlement
2. SettlementManager checks queue every 5 minutes (configurable)
3. For each pending position:
   - Check if market has resolved via Gamma API
   - Wait 10 minutes after market end (resolution wait period)
   - Attempt CTF redemption on Polygon
   - Record proceeds and profit

### Settlement Queue States

| State | Description |
|-------|-------------|
| `pending` | Waiting for market resolution |
| `claimable` | Market resolved, ready to claim |
| `claimed` | Successfully redeemed |
| `failed` | Permanently failed (max attempts reached) |

### Check Settlement Queue Status

```bash
# View settlement metrics
curl -s http://localhost:9090/metrics | grep settlement

# Key metrics:
# mercury_settlement_queue_depth - total unclaimed
# mercury_settlement_queue_size{status="pending"}
# mercury_settlement_queue_size{status="claimed"}
# mercury_settlement_queue_size{status="failed"}
```

### Database Query for Settlement Queue

```bash
# Connect to database
sqlite3 ./data/mercury.db

# View pending settlements
SELECT position_id, market_id, status, claim_attempts, last_claim_error
FROM settlement_queue
WHERE status IN ('pending', 'failed')
ORDER BY queued_at DESC
LIMIT 20;

# Count by status
SELECT status, COUNT(*)
FROM settlement_queue
GROUP BY status;
```

### Manual Settlement Operations

The SettlementManager provides methods for manual intervention:

**Retry a Failed Claim**
```python
# Via Python (in a management script)
await settlement_manager.retry_failed_claim(position_id)
```

**Force Market Resolution Check**
```python
# Bypass cache and check current resolution
market_info = await settlement_manager.force_check_market(condition_id)
```

**Clear Resolution Cache**
```python
# Clear cached resolution data
count = settlement_manager.clear_resolution_cache()
```

### Settlement Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `settlement.check_interval_seconds` | 300 | Time between queue checks (5 min) |
| `settlement.max_claim_attempts` | 5 | Max retries before permanent failure |
| `settlement.resolution_wait_seconds` | 600 | Wait time after market end (10 min) |
| `settlement.retry_initial_delay_seconds` | 60 | Initial retry delay (1 min) |
| `settlement.retry_max_delay_seconds` | 3600 | Max retry delay (1 hour) |
| `settlement.retry_exponential_base` | 2.0 | Backoff multiplier |
| `settlement.alert_after_failures` | 3 | Alert threshold |

### Settlement Retry Backoff

Failed claims use exponential backoff:
- Attempt 1: 60 seconds
- Attempt 2: 120 seconds
- Attempt 3: 240 seconds (alert emitted)
- Attempt 4: 480 seconds
- Attempt 5: 960 seconds (capped at 1 hour, then permanent failure)

Jitter (±25%) is added to prevent thundering herd.

---

## Strategy Management

### Enabled Strategies

Currently implemented:
- **Gabagool** - Arbitrage strategy (buys YES + NO when combined price < $1.00)

### Strategy Configuration

```toml
# config/default.toml
[strategies.gabagool]
enabled = true                    # Enable/disable
markets = ["BTC", "ETH", "SOL"]  # Market filters
min_spread_threshold = 0.015      # Minimum spread (1.5 cents)
max_trade_size_usd = 25.0        # Max per trade
max_per_window_usd = 50.0        # Max per time window
balance_sizing_enabled = true     # Scale by balance
balance_sizing_pct = 0.25        # Use 25% of balance
```

### Enable/Disable Strategies at Runtime

**Via Event Bus (Redis)**
```bash
# Disable Gabagool
redis-cli PUBLISH system.strategy.disable '{"strategy": "gabagool"}'

# Enable Gabagool
redis-cli PUBLISH system.strategy.enable '{"strategy": "gabagool"}'
```

**Via Configuration**
```bash
# Edit config file
vim config/production.toml
# Change: enabled = false

# Restart Mercury to apply
```

### Verify Strategy Status

```bash
# Check signals being generated
curl -s http://localhost:9090/metrics | grep mercury_signals_total

# Watch logs for signal generation
# Look for: event=signal_generated strategy=gabagool
```

---

## Circuit Breaker & Risk Controls

### Circuit Breaker Levels

| Level | Behavior | Triggers |
|-------|----------|----------|
| **NORMAL** | Full trading, 100% position sizes | Default state |
| **WARNING** | 50% position sizes | 3 consecutive failures OR -$50 daily loss |
| **CAUTION** | Close-only mode | 4 consecutive failures OR -$75 daily loss |
| **HALT** | No trading | 5 consecutive failures OR -$100 daily loss |

### Check Current Circuit Breaker State

```bash
# Via metrics
curl -s http://localhost:9090/metrics | grep mercury_circuit_breaker_level

# Value interpretation:
# 0 = NORMAL
# 1 = WARNING
# 2 = CAUTION
# 3 = HALT
```

### Daily Reset

The circuit breaker and daily P&L counters reset automatically:
- Default time: 00:00 UTC
- Configure via: `risk.daily_reset_time_utc = "00:00"`

### Risk Limits

| Limit | Default | Config Key |
|-------|---------|------------|
| Max daily loss | $100 | `risk.max_daily_loss_usd` |
| Max position size | $25 | `risk.max_position_size_usd` |
| Max unhedged exposure | $50 | `risk.max_unhedged_exposure_usd` |
| Max per-market exposure | $100 | `risk.max_per_market_exposure_usd` |
| Max daily trades | 100 | `risk.max_daily_trades` |

### Manually Reset Risk State

To manually reset the circuit breaker (requires code change or restart):

```bash
# Restart Mercury - resets in-memory state
# WARNING: Does not change database P&L records
kill -TERM $(pgrep -f "python -m mercury") && python -m mercury
```

---

## Common Issues & Solutions

### Issue: Event Bus Disconnected

**Symptoms:**
- Health check shows "event_bus_disconnected"
- No signals being processed
- Logs show connection errors

**Solution:**
```bash
# Check Redis is running
docker ps | grep redis

# Test Redis connectivity
redis-cli -u redis://localhost:6379 ping
# Should respond: PONG

# If Redis is down, restart it
docker compose -f docker/docker-compose.yml restart redis

# Wait for Mercury to reconnect (automatic)
```

### Issue: No Strategies Enabled

**Symptoms:**
- Health check shows DEGRADED
- No signals generated
- Logs show "no_strategies_enabled"

**Solution:**
```bash
# Check config
grep -A 5 "\[strategies.gabagool\]" config/production.toml

# Ensure enabled = true
# Edit if needed:
vim config/production.toml

# Restart Mercury
```

### Issue: Circuit Breaker in HALT State

**Symptoms:**
- No new trades
- Health shows DEGRADED with circuit_breaker details
- Logs show "circuit_breaker_tripped" events

**Solution:**
```bash
# Check current state and reason
curl -s http://localhost:9090/metrics | grep circuit_breaker

# Option 1: Wait for automatic daily reset
# Check next reset time in logs or calculate from daily_reset_time_utc

# Option 2: Restart Mercury (resets in-memory state)
# Note: Only do this if you've addressed the underlying issue
```

### Issue: Settlement Queue Backlog

**Symptoms:**
- `mercury_settlement_queue_depth` > 50
- Positions stuck in "pending" status

**Solution:**
```bash
# Check why claims are failing
sqlite3 ./data/mercury.db "SELECT last_claim_error, COUNT(*) FROM settlement_queue WHERE status='failed' GROUP BY last_claim_error;"

# Common errors and fixes:
# - "network" errors: Check Polygon RPC connectivity
# - "gas" errors: Ensure account has MATIC for gas
# - "not resolved": Normal - market hasn't resolved yet
# - "contract" errors: Check contract state on Polygonscan
```

### Issue: High Order Latency

**Symptoms:**
- `mercury_execution_total_time_seconds` > 5s
- Signals timing out in queue

**Solution:**
```bash
# Check where latency is occurring
curl -s http://localhost:9090/metrics | grep execution_time

# If queue_time is high: Reduce max_concurrent or max_queue_size
# If submission_time is high: Check Polymarket API status
# If fill_time is high: Check market liquidity
```

### Issue: WebSocket Disconnections

**Symptoms:**
- Stale market data
- Frequent "websocket_reconnected" logs

**Solution:**
```bash
# Check reconnect count
curl -s http://localhost:9090/metrics | grep websocket_reconnects

# If excessive reconnects:
# 1. Check network stability
# 2. Check Polymarket WebSocket status
# 3. Mercury auto-reconnects with exponential backoff
```

---

## Emergency Procedures

### Emergency Stop

**Immediately halt all trading:**

```bash
# Option 1: Kill the process
kill -TERM $(pgrep -f "python -m mercury")

# Option 2: Docker stop
docker compose -f docker/docker-compose.yml stop mercury

# Option 3: Via Redis (triggers HALT state, but process continues)
redis-cli PUBLISH risk.circuit_breaker '{"force": "HALT"}'
```

### Data Preservation

Before any emergency action, consider preserving data:

```bash
# Backup database
cp ./data/mercury.db ./data/mercury.db.backup.$(date +%Y%m%d_%H%M%S)

# Export recent logs
docker logs mercury --since 1h > mercury_logs_$(date +%Y%m%d_%H%M%S).txt
```

### Recovery After Emergency

1. **Assess the situation**
   ```bash
   # Check database state
   sqlite3 ./data/mercury.db "SELECT COUNT(*) FROM settlement_queue WHERE status='pending';"

   # Check last trades
   sqlite3 ./data/mercury.db "SELECT * FROM trades ORDER BY created_at DESC LIMIT 10;"
   ```

2. **Verify external connectivity**
   ```bash
   # Redis
   redis-cli ping

   # Polymarket API
   curl -s https://clob.polymarket.com/health

   # Polygon RPC
   curl -X POST https://polygon-rpc.com -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}'
   ```

3. **Start in dry-run mode first**
   ```bash
   python -m mercury --dry-run true --log-level DEBUG
   ```

4. **Monitor for issues**
   ```bash
   # Watch logs
   python -m mercury 2>&1 | tee recovery_$(date +%Y%m%d_%H%M%S).log
   ```

5. **Switch to live when stable**
   ```bash
   python -m mercury --dry-run false
   ```

### Rollback Procedures

If a bad deployment needs rollback:

```bash
# 1. Stop Mercury
docker compose -f docker/docker-compose.yml stop mercury

# 2. Restore previous version (via git)
git checkout <previous-tag>

# 3. Rebuild
docker compose -f docker/docker-compose.yml build mercury

# 4. Restart
docker compose -f docker/docker-compose.yml up -d mercury
```

---

## Metrics Reference

### Trading Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `mercury_trades_total` | Counter | Total trades by strategy/asset/status |
| `mercury_orders_total` | Counter | Orders by side/status |
| `mercury_signals_total` | Counter | Signals by strategy/action |
| `mercury_daily_pnl_usd` | Gauge | Current daily P&L |
| `mercury_active_positions` | Gauge | Open position count |

### Settlement Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `mercury_settlement_queue_depth` | Gauge | Positions pending settlement |
| `mercury_settlements_total` | Counter | Settlements by status/resolution |
| `mercury_settlement_failures_total` | Counter | Failures by reason |
| `mercury_settlement_latency_seconds` | Histogram | Time from resolution to claim |
| `mercury_settlement_claim_attempts` | Histogram | Attempts before success/failure |

### Latency Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `mercury_order_latency_seconds` | Histogram | Order execution latency |
| `mercury_execution_queue_time_seconds` | Histogram | Time in execution queue |
| `mercury_execution_submission_time_seconds` | Histogram | Time to submit to exchange |
| `mercury_execution_fill_time_seconds` | Histogram | Submission to fill time |
| `mercury_execution_total_time_seconds` | Histogram | Total execution latency |

### System Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `mercury_uptime_seconds` | Gauge | Process uptime |
| `mercury_circuit_breaker_level` | Gauge | 0=NORMAL, 1=WARNING, 2=CAUTION, 3=HALT |
| `mercury_websocket_connected` | Gauge | 1=connected, 0=disconnected |
| `mercury_websocket_reconnects_total` | Counter | Reconnection count |

### Prometheus Queries

```promql
# Average order latency over last 5 minutes
rate(mercury_order_latency_seconds_sum[5m]) / rate(mercury_order_latency_seconds_count[5m])

# Settlement success rate
sum(mercury_settlements_total{status="claimed"}) / sum(mercury_settlements_total)

# Trades per minute
rate(mercury_trades_total[1m])

# P&L trend (requires recording rules)
changes(mercury_daily_pnl_usd[1h])
```

---

## Appendix: Environment Variables

All configuration can be overridden via environment variables with `MERCURY_` prefix:

| Variable | Description |
|----------|-------------|
| `MERCURY_DRY_RUN` | Enable/disable dry-run mode |
| `MERCURY_LOG_LEVEL` | DEBUG, INFO, WARNING, ERROR |
| `MERCURY_REDIS_URL` | Redis connection URL |
| `MERCURY_DATABASE_PATH` | SQLite database path |
| `MERCURY_POLYMARKET_PRIVATE_KEY` | Trading wallet private key |
| `MERCURY_POLYMARKET_API_KEY` | Polymarket API key |
| `MERCURY_POLYMARKET_API_SECRET` | Polymarket API secret |
| `MERCURY_POLYMARKET_API_PASSPHRASE` | Polymarket API passphrase |
| `MERCURY_POLYGON_RPC_URL` | Polygon RPC endpoint |

---

## Document History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-01-19 | Initial runbook |
