# Mercury

Polymarket trading bot with modular event-driven architecture.

## Quick Start

```bash
# Development
pip install -e .[dev]
python -m mercury --dry-run

# Production
docker compose -f docker/docker-compose.prod.yml up -d
```

See `../AGENTS.md` for full documentation.
