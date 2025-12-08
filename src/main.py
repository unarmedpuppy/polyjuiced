"""Main entry point for Polymarket Gabagool Trading Bot."""

import asyncio
import signal
import sys
from datetime import datetime
from typing import Optional

import structlog

from .client.polymarket import PolymarketClient
from .client.websocket import PolymarketWebSocket
from .config import AppConfig
from .metrics import init_metrics
from .metrics_server import MetricsServer
from .monitoring.market_finder import MarketFinder
from .risk import CircuitBreaker, PositionSizer
from .strategies.gabagool import GabagoolStrategy

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

log = structlog.get_logger()


class GabagoolBot:
    """Main bot orchestrator for Gabagool arbitrage strategy."""

    def __init__(self, config: AppConfig):
        """Initialize the bot.

        Args:
            config: Application configuration
        """
        self.config = config
        self._running = False
        self._shutdown_event = asyncio.Event()

        # Components (initialized in start)
        self._client: Optional[PolymarketClient] = None
        self._ws_client: Optional[PolymarketWebSocket] = None
        self._market_finder: Optional[MarketFinder] = None
        self._circuit_breaker: Optional[CircuitBreaker] = None
        self._position_sizer: Optional[PositionSizer] = None
        self._strategy: Optional[GabagoolStrategy] = None
        self._metrics_server: Optional[MetricsServer] = None

    async def start(self) -> None:
        """Start the bot and all components."""
        log.info(
            "Starting Gabagool Bot",
            dry_run=self.config.gabagool.dry_run,
            markets=self.config.gabagool.markets,
        )

        self._running = True

        # Initialize metrics
        init_metrics(version="0.1.0", dry_run=self.config.gabagool.dry_run)

        # Start metrics server
        self._metrics_server = MetricsServer(port=8000)
        await self._metrics_server.start()

        # Initialize components
        await self._init_components()

        # Register signal handlers
        self._register_signals()

        # Log startup info
        self._log_startup_info()

        # Start the strategy
        try:
            await self._strategy.start()
        except Exception as e:
            log.error("Strategy crashed", error=str(e))
            raise
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Stop the bot gracefully."""
        if not self._running:
            return

        log.info("Stopping Gabagool Bot")
        self._running = False
        self._shutdown_event.set()

        # Stop strategy
        if self._strategy:
            await self._strategy.stop()

        # Disconnect clients
        if self._ws_client:
            await self._ws_client.disconnect()

        if self._client:
            await self._client.disconnect()

        # Stop metrics server
        if self._metrics_server:
            await self._metrics_server.stop()

        log.info("Gabagool Bot stopped")

    async def _init_components(self) -> None:
        """Initialize all bot components."""
        # Create Polymarket client
        self._client = PolymarketClient(self.config)
        await self._client.connect()

        # Create WebSocket client
        self._ws_client = PolymarketWebSocket(self.config)

        # Create market finder
        self._market_finder = MarketFinder(self.config.polymarket)

        # Create risk management components
        self._circuit_breaker = CircuitBreaker(self.config.gabagool)
        self._position_sizer = PositionSizer(self.config.gabagool)

        # Create strategy
        self._strategy = GabagoolStrategy(
            client=self._client,
            ws_client=self._ws_client,
            market_finder=self._market_finder,
            config=self.config,
        )

        log.info("All components initialized")

    def _register_signals(self) -> None:
        """Register signal handlers for graceful shutdown."""
        loop = asyncio.get_event_loop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.create_task(self._handle_signal(s)),
            )

    async def _handle_signal(self, sig: signal.Signals) -> None:
        """Handle shutdown signal.

        Args:
            sig: The signal received
        """
        log.info("Received shutdown signal", signal=sig.name)
        await self.stop()

    def _log_startup_info(self) -> None:
        """Log startup configuration info."""
        gabagool = self.config.gabagool

        log.info(
            "Gabagool Configuration",
            dry_run=gabagool.dry_run,
            min_spread=f"{gabagool.min_spread_threshold * 100:.1f} cents",
            max_trade_size=f"${gabagool.max_trade_size_usd:.2f}",
            max_daily_loss=f"${gabagool.max_daily_loss_usd:.2f}",
            max_daily_exposure=f"${gabagool.max_daily_exposure_usd:.2f}",
            markets=gabagool.markets,
        )

        if gabagool.dry_run:
            log.warning(
                "DRY RUN MODE - No real trades will be executed",
            )


async def run_bot() -> None:
    """Run the Gabagool bot."""
    # Load configuration
    config = AppConfig()

    # Create and start bot
    bot = GabagoolBot(config)

    try:
        await bot.start()
    except KeyboardInterrupt:
        log.info("Keyboard interrupt received")
    except Exception as e:
        log.error("Bot crashed", error=str(e))
        raise
    finally:
        await bot.stop()


def main() -> None:
    """Main entry point."""
    log.info(
        "Gabagool Arbitrage Bot",
        version="0.1.0",
        started_at=datetime.utcnow().isoformat(),
    )

    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        log.info("Shutdown complete")
        sys.exit(0)
    except Exception as e:
        log.error("Fatal error", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
