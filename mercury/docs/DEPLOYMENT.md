# Mercury Deployment Guide

This document provides a comprehensive checklist for deploying Mercury to the homelab production environment.

## Prerequisites

### Server Requirements
- [ ] Docker Engine 24.0+ installed
- [ ] Docker Compose v2+ installed
- [ ] External network `my-network` exists (`docker network create my-network`)
- [ ] Traefik reverse proxy configured and running
- [ ] Harbor registry access configured

### DNS Configuration
- [ ] `mercury.server.unarmedpuppy.com` DNS record exists
- [ ] `mercury-prometheus.server.unarmedpuppy.com` DNS record exists
- [ ] `mercury-grafana.server.unarmedpuppy.com` DNS record exists
- [ ] DNS records added to Cloudflare DDNS config (if dynamic)

### Credentials
- [ ] Polymarket API credentials obtained from CLOB
- [ ] `.env` file created from `.env.example`
- [ ] `MERCURY_POLYMARKET_PRIVATE_KEY` set
- [ ] `MERCURY_POLYMARKET_API_KEY` set
- [ ] `MERCURY_POLYMARKET_API_SECRET` set
- [ ] `MERCURY_POLYMARKET_API_PASSPHRASE` set
- [ ] `GRAFANA_ADMIN_PASSWORD` changed from default
- [ ] `MERCURY_BASIC_AUTH` hash generated for Traefik auth

## Deployment Steps

### 1. Initial Setup (First-time only)

```bash
# Create deployment directory
mkdir -p /opt/mercury
cd /opt/mercury

# Clone or copy deployment files
# Option A: From git repository
git clone https://gitea.server.unarmedpuppy.com/homelab/polyjuiced.git
cd polyjuiced/mercury/docker

# Option B: Copy files manually
cp docker-compose.prod.yml /opt/mercury/
cp prometheus-prod.yml /opt/mercury/
cp prometheus-alerts.yml /opt/mercury/
cp -r grafana/ /opt/mercury/
cp -r ../config/ /opt/mercury/
cp .env.example /opt/mercury/.env

# Create .env from example and fill in credentials
vim /opt/mercury/.env

# Create external network if not exists
docker network create my-network 2>/dev/null || true

# Create volume directories for proper permissions
mkdir -p /opt/mercury/data
chown 1000:1000 /opt/mercury/data
```

### 2. Image Build/Pull

```bash
# Option A: Pull pre-built image (recommended for tags)
docker pull harbor.server.unarmedpuppy.com/library/mercury:latest

# Option B: Build locally (for development/testing)
cd /path/to/polyjuiced/mercury
docker compose -f docker/docker-compose.prod.yml build --no-cache
```

### 3. Deploy Services

```bash
cd /opt/mercury

# Start all services
docker compose -f docker-compose.prod.yml up -d

# Verify containers are running
docker compose -f docker-compose.prod.yml ps

# Check logs for errors
docker compose -f docker-compose.prod.yml logs -f --tail=100
```

### 4. Verify Deployment

```bash
# Check Mercury health
curl -sf http://localhost:9090/health | jq .

# Check Prometheus is scraping
curl -sf http://localhost:9091/-/healthy

# Check Grafana is up
curl -sf http://localhost:3000/api/health | jq .

# Verify via Traefik (external)
curl -sf https://mercury.server.unarmedpuppy.com/health | jq .
```

## Post-Deployment Verification

### Health Checks
- [ ] Mercury health endpoint returns `{"status": "healthy"}`
- [ ] Redis health check passes
- [ ] Prometheus scraping Mercury metrics
- [ ] Grafana accessible and showing dashboards

### Metrics Verification
- [ ] `mercury_up` metric is 1
- [ ] `mercury_circuit_breaker_state` shows NORMAL
- [ ] `mercury_websocket_connected` shows 1
- [ ] No scrape errors in Prometheus targets

### Dashboard Verification
- [ ] Trading Overview dashboard loads
- [ ] Execution Quality dashboard loads
- [ ] Risk Status dashboard loads
- [ ] System Health dashboard loads
- [ ] All panels show data (not "No data")

### Alert Verification
- [ ] Test circuit breaker alert (manually trip and verify)
- [ ] Verify alert rules in Prometheus UI
- [ ] Check alertmanager connectivity (if configured)

## 24-Hour Monitoring Checklist

### Hour 0-1 (Initial)
- [ ] All services started successfully
- [ ] No crash loops or restarts
- [ ] WebSocket connection established
- [ ] First market data received

### Hour 1-4
- [ ] Memory usage stable (< 500MB)
- [ ] CPU usage reasonable (< 50% avg)
- [ ] No reconnection issues
- [ ] Market data flowing continuously

### Hour 4-12
- [ ] First trading signals generated (if markets active)
- [ ] Risk limits respected
- [ ] No unhandled exceptions in logs
- [ ] Database writes successful

### Hour 12-24
- [ ] Daily reset occurred at configured time
- [ ] No memory leaks (memory not growing)
- [ ] Log rotation working
- [ ] No disk space issues

## Troubleshooting

### Mercury won't start
```bash
# Check logs for errors
docker logs mercury --tail=100

# Verify environment variables
docker exec mercury env | grep MERCURY

# Check config file exists
docker exec mercury cat /app/config/production.toml
```

### No metrics in Prometheus
```bash
# Check if Mercury metrics endpoint is responding
curl -sf http://localhost:9090/metrics | head -20

# Check Prometheus targets
curl -sf http://localhost:9091/api/v1/targets | jq .

# Check Prometheus scrape config
docker exec mercury-prometheus cat /etc/prometheus/prometheus.yml
```

### Grafana shows "No data"
1. Check datasource connectivity: Settings > Data Sources > Prometheus > Test
2. Verify time range is correct
3. Check Prometheus has data: `up{job="mercury"}`
4. Check for metric name typos in queries

### WebSocket disconnections
```bash
# Check Mercury logs for WS errors
docker logs mercury 2>&1 | grep -i websocket

# Verify network connectivity
docker exec mercury ping -c 3 ws-subscriptions-clob.polymarket.com

# Check for rate limiting
grep -i "rate" /opt/mercury/logs/mercury.log
```

### High memory usage
```bash
# Check container stats
docker stats mercury

# Force garbage collection (Python)
docker exec mercury python -c "import gc; gc.collect()"

# Consider restarting if > 800MB sustained
docker compose -f docker-compose.prod.yml restart mercury
```

## Rollback Procedure

```bash
# Stop current deployment
cd /opt/mercury
docker compose -f docker-compose.prod.yml down

# Revert to previous version
MERCURY_VERSION=v1.0.0 docker compose -f docker-compose.prod.yml pull
MERCURY_VERSION=v1.0.0 docker compose -f docker-compose.prod.yml up -d

# Verify rollback
curl -sf http://localhost:9090/health | jq .
```

## Upgrade Procedure

```bash
cd /opt/mercury

# Pull new version
MERCURY_VERSION=v1.1.0 docker compose -f docker-compose.prod.yml pull

# Stop and restart with new version
docker compose -f docker-compose.prod.yml down
MERCURY_VERSION=v1.1.0 docker compose -f docker-compose.prod.yml up -d

# Verify upgrade
docker compose -f docker-compose.prod.yml logs -f mercury

# Check health
curl -sf http://localhost:9090/health | jq .
```

## Maintenance Tasks

### Log rotation
Logs are automatically rotated by Docker's json-file driver (50MB max, 5 files).

### Database backup
```bash
# Backup SQLite database
docker exec mercury cp /app/data/mercury.db /app/data/mercury.db.backup
docker cp mercury:/app/data/mercury.db.backup ./mercury-backup-$(date +%Y%m%d).db
```

### Prometheus data management
```bash
# Check TSDB size
docker exec mercury-prometheus du -sh /prometheus

# Prometheus retention is set to 30 days / 5GB max
```

## Contact & Escalation

- **Documentation**: `./AGENTS.md`
- **Issue Tracker**: gitea.server.unarmedpuppy.com/homelab/polyjuiced/issues
- **Logs**: `/opt/mercury/` or `docker logs mercury`
