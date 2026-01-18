"""Mercury Trading Bot - Entry Point

Usage:
    python -m mercury [--config PATH] [--dry-run] [--log-level LEVEL]

Commands:
    run     - Start the trading bot (default)
    health  - Check health status
    version - Show version

Examples:
    python -m mercury
    python -m mercury --config config/production.toml
    python -m mercury --dry-run --log-level DEBUG
    python -m mercury health
"""

import argparse
import asyncio
import sys
from pathlib import Path

from mercury import __version__


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="mercury",
        description="Polymarket trading bot with modular event-driven architecture",
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"Mercury {__version__}",
    )

    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to configuration file (TOML)",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Run in dry-run mode (no real trades)",
    )

    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
        help="Log level",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Run command (default)
    subparsers.add_parser("run", help="Start the trading bot")

    # Health command
    subparsers.add_parser("health", help="Check health status")

    # Version command
    subparsers.add_parser("version", help="Show version")

    return parser.parse_args()


def find_config_file(specified: Path | None) -> Path | None:
    """Find configuration file."""
    if specified and specified.exists():
        return specified

    # Search paths
    search_paths = [
        Path("config/default.toml"),
        Path("config/development.toml"),
        Path("config/production.toml"),
        Path("mercury.toml"),
        Path("/etc/mercury/mercury.toml"),
    ]

    for path in search_paths:
        if path.exists():
            return path

    return None


async def run_bot(args: argparse.Namespace) -> int:
    """Run the trading bot."""
    from mercury.app import MercuryApp
    from mercury.core.config import ConfigManager
    from mercury.core.logging import setup_logging

    # Find config
    config_path = find_config_file(args.config)

    # Set up logging first
    log_level = args.log_level or "INFO"
    setup_logging(level=log_level)

    import structlog
    log = structlog.get_logger()

    log.info(
        "starting_mercury",
        version=__version__,
        config=str(config_path) if config_path else "defaults",
        dry_run=args.dry_run,
    )

    # Load config
    config = ConfigManager(config_path) if config_path else ConfigManager()

    # Override with command-line args
    if args.dry_run is not None:
        config._config["mercury"] = config._config.get("mercury", {})
        config._config["mercury"]["dry_run"] = args.dry_run

    # Create and run app
    app = MercuryApp(config)

    try:
        await app.run()
        return 0
    except KeyboardInterrupt:
        log.info("shutdown_requested")
        await app.shutdown()
        return 0
    except Exception as e:
        log.error("fatal_error", error=str(e))
        return 1


async def check_health() -> int:
    """Check health status."""
    import httpx

    port = 9090
    url = f"http://localhost:{port}/health"

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=5.0)

        if response.status_code == 200:
            data = response.json()
            print(f"Status: {data.get('status', 'unknown')}")
            print(f"Uptime: {data.get('uptime_seconds', 0):.0f}s")

            components = data.get("components", {})
            for name, info in components.items():
                status = info.get("status", "unknown")
                print(f"  {name}: {status}")

            return 0 if data.get("status") == "healthy" else 1
        else:
            print(f"Health check failed: HTTP {response.status_code}")
            return 1

    except httpx.ConnectError:
        print("Cannot connect to Mercury (is it running?)")
        return 1
    except Exception as e:
        print(f"Health check error: {e}")
        return 1


def main() -> int:
    """Main entry point."""
    args = parse_args()

    if args.command == "version":
        print(f"Mercury {__version__}")
        return 0

    if args.command == "health":
        return asyncio.run(check_health())

    # Default: run the bot
    return asyncio.run(run_bot(args))


if __name__ == "__main__":
    sys.exit(main())
