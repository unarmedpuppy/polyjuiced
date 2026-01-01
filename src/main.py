"""Main entry point for Polymarket Gabagool Trading Bot."""

import asyncio
import logging
import signal
import sys
from datetime import datetime
from typing import Optional

import structlog

from .client.gamma import GammaClient
from .client.polymarket import PolymarketClient
from .client.websocket import PolymarketWebSocket
from .config import AppConfig
from .dashboard import DashboardServer, add_log, update_stats, init_persistence
from .dashboard import dashboard as dashboard_instance
import src.dashboard as dashboard_module
from .liquidity import LiquidityCollector
from .metrics import init_metrics
from .metrics_server import MetricsServer
from .monitoring.market_finder import MarketFinder
from .persistence import get_database, close_database, Database
from .risk import CircuitBreaker, PositionSizer
from .strategies.gabagool import GabagoolStrategy
from .strategies.near_resolution import NearResolutionStrategy
from .strategies.vol_happens import VolHappensStrategy

# Configure stdlib logging level (required for structlog filter_by_level)
logging.basicConfig(
    format="%(message)s",
    stream=sys.stdout,
    level=logging.INFO,
)

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
        self._gamma_client: Optional[GammaClient] = None
        self._ws_client: Optional[PolymarketWebSocket] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._market_finder: Optional[MarketFinder] = None
        self._circuit_breaker: Optional[CircuitBreaker] = None
        self._position_sizer: Optional[PositionSizer] = None
        self._strategy: Optional[GabagoolStrategy] = None
        self._vol_happens_strategy: Optional[VolHappensStrategy] = None
        self._near_resolution_strategy: Optional[NearResolutionStrategy] = None
        self._metrics_server: Optional[MetricsServer] = None
        self._dashboard: Optional[DashboardServer] = None
        self._db: Optional[Database] = None
        self._liquidity_collector: Optional[LiquidityCollector] = None

    async def start(self) -> None:
        """Start the bot and all components."""
        log.info(
            "Starting Gabagool Bot",
            dry_run=self.config.gabagool.dry_run,
            markets=self.config.gabagool.markets,
        )

        self._running = True

        self._db = await get_database()
        log.info("Database initialized", path=str(self._db.db_path))

        cleanup_stats = await self._db.run_data_retention_cleanup()
        if sum(cleanup_stats.values()) > 0:
            log.info("Startup cleanup completed", deleted=cleanup_stats)

        init_metrics(version="0.1.0", dry_run=self.config.gabagool.dry_run)
        init_metrics(version="0.1.0", dry_run=self.config.gabagool.dry_run)

        # Start metrics server
        self._metrics_server = MetricsServer(port=8000)
        await self._metrics_server.start()

        # Start dashboard
        self._dashboard = DashboardServer(port=8080)
        dashboard_module.dashboard = self._dashboard
        await self._dashboard.start()

        # Initialize persistence and load historical data
        await init_persistence(self._db)

        update_stats(
            dry_run=self.config.gabagool.dry_run,
            arbitrage_enabled=self.config.gabagool.enabled,
            directional_enabled=self.config.gabagool.directional_enabled,
            # Per-strategy status for multi-strategy dashboard
            gabagool_enabled=self.config.gabagool.enabled,
            gabagool_dry_run=self.config.gabagool.dry_run,
            vol_happens_enabled=self.config.vol_happens.enabled,
            vol_happens_dry_run=self.config.vol_happens.dry_run,
            near_resolution_enabled=self.config.near_resolution.enabled,
            near_resolution_dry_run=self.config.near_resolution.dry_run,
        )
        add_log("info", "Dashboard started", url="http://localhost:8080/dashboard")

        # Initialize components
        await self._init_components()

        # Register signal handlers
        self._register_signals()

        # Log startup info
        self._log_startup_info()

        # Start the strategies
        try:
            # Start Gabagool (primary strategy)
            gabagool_task = asyncio.create_task(self._strategy.start())

            # Start Vol Happens (optional secondary strategy)
            vol_happens_task = None
            if self.config.vol_happens.enabled:
                vol_happens_task = asyncio.create_task(self._vol_happens_strategy.start())
                add_log("info", "Vol Happens strategy enabled")

            # Start Near Resolution (optional secondary strategy)
            near_resolution_task = None
            if self.config.near_resolution.enabled:
                near_resolution_task = asyncio.create_task(self._near_resolution_strategy.start())
                add_log("info", "Near Resolution strategy enabled")

            # Start connection health monitor
            health_monitor_task = asyncio.create_task(self._connection_health_monitor())

            # Wait for strategies (they run until stopped)
            tasks = [gabagool_task, health_monitor_task]
            if vol_happens_task:
                tasks.append(vol_happens_task)
            if near_resolution_task:
                tasks.append(near_resolution_task)
            await asyncio.gather(*tasks)
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

        # Stop strategies
        if self._strategy:
            await self._strategy.stop()

        if self._vol_happens_strategy:
            await self._vol_happens_strategy.stop()

        if self._near_resolution_strategy:
            await self._near_resolution_strategy.stop()

        # Stop liquidity collector
        if self._liquidity_collector:
            await self._liquidity_collector.stop()

        # Cancel WebSocket task
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

        # Disconnect clients
        if self._ws_client:
            await self._ws_client.disconnect()

        if self._gamma_client:
            await self._gamma_client.close()

        if self._client:
            await self._client.disconnect()

        # Stop metrics server
        if self._metrics_server:
            await self._metrics_server.stop()

        # Stop dashboard
        if self._dashboard:
            await self._dashboard.stop()

        # Close database
        await close_database()

        log.info("Gabagool Bot stopped")

    async def _init_components(self) -> None:
        """Initialize all bot components."""
        # Create Polymarket client
        self._client = PolymarketClient(self.config.polymarket)
        connected = await self._client.connect()
        if connected:
            update_stats(clob_status="CONNECTED")
            add_log("info", "Connected to Polymarket CLOB API")
        else:
            update_stats(clob_status="ERROR")
            add_log("error", "Failed to connect to Polymarket CLOB API")

        # Verify API credentials work (try to get orders - requires auth)
        if self.config.polymarket.api_key:
            try:
                orders = self._client.get_orders()
                log.info("API credentials verified", order_count=len(orders))
                add_log("info", "API credentials verified successfully")
            except Exception as e:
                log.warning("API credentials may be invalid", error=str(e))
                add_log("warning", f"API credential check failed: {str(e)[:50]}")

        # Create WebSocket client
        self._ws_client = PolymarketWebSocket(
            ws_url=self.config.polymarket.clob_ws_url
        )

        # Connect WebSocket in background (will auto-reconnect)
        async def ws_runner():
            await self._ws_client.run()

        self._ws_task = asyncio.create_task(ws_runner())
        # Give WebSocket time to connect
        await asyncio.sleep(1)
        if self._ws_client.is_connected:
            update_stats(websocket="CONNECTED")
            add_log("info", "WebSocket connected for real-time data")
        else:
            update_stats(websocket="CONNECTING")
            add_log("warning", "WebSocket connecting (will retry automatically)")

        # Create Gamma client for market discovery (with VPN proxy if configured)
        self._gamma_client = GammaClient(
            base_url=self.config.polymarket.gamma_api_url,
            http_proxy=self.config.polymarket.http_proxy,
        )

        # Create market finder (with database for persistence)
        self._market_finder = MarketFinder(self._gamma_client, db=self._db)

        # Create risk management components
        self._circuit_breaker = CircuitBreaker(self.config.gabagool)
        self._position_sizer = PositionSizer(self.config.gabagool)

        # Create strategy
        self._strategy = GabagoolStrategy(
            client=self._client,
            ws_client=self._ws_client,
            market_finder=self._market_finder,
            config=self.config,
            db=self._db,
        )

        # Create Vol Happens strategy (runs alongside Gabagool)
        self._vol_happens_strategy = VolHappensStrategy(
            client=self._client,
            ws_client=self._ws_client,
            market_finder=self._market_finder,
            config=self.config,
            db=self._db,
        )

        # Create Near Resolution strategy (runs alongside Gabagool)
        self._near_resolution_strategy = NearResolutionStrategy(
            client=self._client,
            ws_client=self._ws_client,
            market_finder=self._market_finder,
            config=self.config,
            db=self._db,
        )

        # Create liquidity collector for fill/depth data collection
        # See docs/LIQUIDITY_SIZING.md for roadmap
        self._liquidity_collector = LiquidityCollector(
            client=self._client,
            database=self._db,
            snapshot_interval_seconds=30.0,
            max_snapshot_levels=10,
        )
        await self._liquidity_collector.start()
        self._strategy.set_liquidity_collector(self._liquidity_collector)
        add_log("info", "Liquidity data collection enabled")

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

    async def _connection_health_monitor(self) -> None:
        """Monitor connection health and trigger reconnections when needed.

        This runs as a background task and periodically checks:
        1. WebSocket connection health (is_healthy property)
        2. Polymarket CLOB client connection (via test call)
        3. Forces reconnection if connections are stale

        This handles the case where network connectivity is lost and restored,
        but the connections don't automatically recover.
        """
        health_check_interval = 30.0  # Check every 30 seconds
        clob_check_interval = 60.0  # Check CLOB less frequently
        last_clob_check = 0.0

        log.info("Connection health monitor started", interval_seconds=health_check_interval)

        while self._running:
            try:
                await asyncio.sleep(health_check_interval)

                if not self._running:
                    break

                current_time = asyncio.get_event_loop().time()

                # Check WebSocket health
                if self._ws_client:
                    if not self._ws_client.is_healthy:
                        seconds_stale = self._ws_client.seconds_since_last_message
                        log.warning(
                            "WebSocket connection unhealthy - forcing reconnect",
                            connected=self._ws_client.is_connected,
                            seconds_since_last_message=f"{seconds_stale:.0f}s",
                        )
                        update_stats(websocket="RECONNECTING")
                        add_log("warning", f"WebSocket stale ({seconds_stale:.0f}s), reconnecting...")
                        await self._ws_client.force_reconnect()
                    else:
                        # Update status if healthy
                        if self._ws_client.is_connected:
                            update_stats(websocket="CONNECTED")

                # Check CLOB client health (less frequently)
                if current_time - last_clob_check > clob_check_interval:
                    last_clob_check = current_time

                    if self._client and self._client.is_connected:
                        try:
                            # Simple health check - get balance (lightweight)
                            await asyncio.wait_for(
                                asyncio.to_thread(self._client.get_balance),
                                timeout=10.0,
                            )
                            update_stats(clob="CONNECTED")
                        except Exception as e:
                            log.warning(
                                "CLOB client health check failed - reconnecting",
                                error=str(e)[:100],
                            )
                            update_stats(clob="RECONNECTING")
                            add_log("warning", f"CLOB health check failed, reconnecting: {str(e)[:50]}")

                            # Attempt reconnection
                            try:
                                self._client._connected = False
                                success = await self._client.connect()
                                if success:
                                    log.info("CLOB client reconnected successfully")
                                    update_stats(clob="CONNECTED")
                                    add_log("info", "CLOB client reconnected")
                                else:
                                    log.error("CLOB client reconnection failed")
                                    update_stats(clob="DISCONNECTED")
                            except Exception as reconn_err:
                                log.error("CLOB reconnection error", error=str(reconn_err))

            except asyncio.CancelledError:
                log.info("Connection health monitor cancelled")
                break
            except Exception as e:
                log.error("Connection health monitor error", error=str(e))
                # Continue monitoring despite errors
                await asyncio.sleep(5.0)

        log.info("Connection health monitor stopped")


async def run_bot() -> None:
    """Run the Gabagool bot."""
    # Load configuration
    config = AppConfig.load()

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
