"""Strategy Engine - orchestrates strategy execution.

This service:
- Loads and manages trading strategies
- Routes market data to strategies
- Collects and publishes trading signals
- Supports runtime enable/disable via events and config hot-reload
"""

import asyncio
from datetime import datetime
from typing import Any, Dict, Optional, Set

import structlog

from mercury.core.config import ConfigManager
from mercury.core.events import EventBus
from mercury.core.lifecycle import BaseComponent, HealthCheckResult, HealthStatus
from mercury.domain.market import OrderBook
from mercury.domain.signal import TradingSignal
from mercury.strategies.base import BaseStrategy

log = structlog.get_logger()


class StrategyEngine(BaseComponent):
    """Orchestrates trading strategy execution.

    This service:
    1. Loads enabled strategies from configuration
    2. Subscribes to market data for each strategy's markets
    3. Routes market data to appropriate strategies
    4. Collects signals and publishes to EventBus
    5. Syncs strategy enabled/disabled state on config hot-reload

    Event channels subscribed:
    - market.orderbook.* - Market data for strategies
    - system.strategy.enable - Enable a strategy at runtime
    - system.strategy.disable - Disable a strategy at runtime

    Event channels published:
    - signal.{strategy_name} - Trading signals from strategies
    - system.strategy.enabled - Published when a strategy is enabled
    - system.strategy.disabled - Published when a strategy is disabled
    """

    def __init__(
        self,
        config: ConfigManager,
        event_bus: EventBus,
    ):
        """Initialize the strategy engine.

        Args:
            config: Configuration manager.
            event_bus: EventBus for events.
        """
        super().__init__()
        self._config = config
        self._event_bus = event_bus
        self._log = log.bind(component="strategy_engine")

        self._strategies: Dict[str, BaseStrategy] = {}
        self._market_to_strategies: Dict[str, Set[str]] = {}
        self._should_run = False
        self._config_reload_registered = False

    @property
    def strategy_count(self) -> int:
        """Number of registered strategies."""
        return len(self._strategies)

    @property
    def enabled_strategies(self) -> list[str]:
        """Names of currently enabled strategies."""
        return [name for name, s in self._strategies.items() if s.enabled]

    async def start(self) -> None:
        """Start the strategy engine."""
        self._should_run = True
        self._log.info("starting_strategy_engine")

        # Start all registered strategies
        for name, strategy in self._strategies.items():
            try:
                await strategy.start()
                self._log.info("strategy_started", name=name)
            except Exception as e:
                self._log.error("strategy_start_failed", name=name, error=str(e))

        # Subscribe to events
        await self._event_bus.subscribe("market.orderbook.*", self._on_market_data)
        await self._event_bus.subscribe("system.strategy.enable", self._on_enable_strategy)
        await self._event_bus.subscribe("system.strategy.disable", self._on_disable_strategy)

        # Register config reload callback to sync strategy states
        if not self._config_reload_registered:
            self._config.register_reload_callback(self._on_config_reload)
            self._config_reload_registered = True
            self._log.debug("config_reload_callback_registered")

        self._log.info(
            "strategy_engine_started",
            strategies=len(self._strategies),
            enabled=len(self.enabled_strategies),
        )

        # Set running state
        self._running = True
        self._started_at = datetime.utcnow()

    async def stop(self) -> None:
        """Stop the strategy engine."""
        self._should_run = False

        # Unregister config reload callback
        if self._config_reload_registered:
            self._config.unregister_reload_callback(self._on_config_reload)
            self._config_reload_registered = False
            self._log.debug("config_reload_callback_unregistered")

        # Stop all strategies
        for name, strategy in self._strategies.items():
            try:
                await strategy.stop()
                self._log.info("strategy_stopped", name=name)
            except Exception as e:
                self._log.warning("strategy_stop_error", name=name, error=str(e))

        self._log.info("strategy_engine_stopped")
        self._running = False

    async def health_check(self) -> HealthCheckResult:
        """Check strategy engine health."""
        enabled = len(self.enabled_strategies)
        total = len(self._strategies)

        if total == 0:
            return HealthCheckResult(
                status=HealthStatus.DEGRADED,
                message="No strategies registered",
            )

        if enabled == 0:
            return HealthCheckResult(
                status=HealthStatus.DEGRADED,
                message="No strategies enabled",
                details={"total": total, "enabled": 0},
            )

        return HealthCheckResult(
            status=HealthStatus.HEALTHY,
            message=f"{enabled}/{total} strategies enabled",
            details={
                "total": total,
                "enabled": enabled,
                "strategies": self.enabled_strategies,
            },
        )

    def register_strategy(self, strategy: BaseStrategy) -> None:
        """Register a strategy instance.

        Args:
            strategy: Strategy to register.
        """
        name = strategy.name
        if name in self._strategies:
            self._log.warning("strategy_already_registered", name=name)
            return

        self._strategies[name] = strategy

        # Map markets to strategy
        for market_id in strategy.get_subscribed_markets():
            if market_id not in self._market_to_strategies:
                self._market_to_strategies[market_id] = set()
            self._market_to_strategies[market_id].add(name)

        self._log.info(
            "strategy_registered",
            name=name,
            markets=len(strategy.get_subscribed_markets()),
        )

    def unregister_strategy(self, name: str) -> None:
        """Unregister a strategy.

        Args:
            name: Strategy name to unregister.
        """
        if name not in self._strategies:
            return

        strategy = self._strategies.pop(name)

        # Remove from market mappings
        for market_id in strategy.get_subscribed_markets():
            if market_id in self._market_to_strategies:
                self._market_to_strategies[market_id].discard(name)

        self._log.info("strategy_unregistered", name=name)

    def get_strategy(self, name: str) -> Optional[BaseStrategy]:
        """Get a strategy by name."""
        return self._strategies.get(name)

    def is_strategy_enabled(self, name: str) -> bool:
        """Check if a strategy is enabled.

        Args:
            name: Strategy name.

        Returns:
            True if strategy exists and is enabled.
        """
        strategy = self._strategies.get(name)
        return strategy is not None and strategy.enabled

    async def enable_strategy(self, name: str, publish_event: bool = True) -> bool:
        """Enable a strategy at runtime.

        Args:
            name: Strategy name.
            publish_event: Whether to publish state change event.

        Returns:
            True if enabled, False if strategy not found or already enabled.
        """
        strategy = self._strategies.get(name)
        if strategy is None:
            self._log.warning("strategy_not_found", name=name)
            return False

        if strategy.enabled:
            # Already enabled, no state change
            return True

        strategy.enable()
        self._log.info("strategy_enabled", name=name)

        if publish_event:
            await self._publish_state_change(name, enabled=True)

        return True

    async def disable_strategy(self, name: str, publish_event: bool = True) -> bool:
        """Disable a strategy at runtime.

        Args:
            name: Strategy name.
            publish_event: Whether to publish state change event.

        Returns:
            True if disabled, False if strategy not found or already disabled.
        """
        strategy = self._strategies.get(name)
        if strategy is None:
            return False

        if not strategy.enabled:
            # Already disabled, no state change
            return True

        strategy.disable()
        self._log.info("strategy_disabled", name=name)

        if publish_event:
            await self._publish_state_change(name, enabled=False)

        return True

    async def _publish_state_change(self, name: str, enabled: bool) -> None:
        """Publish strategy state change event.

        Args:
            name: Strategy name.
            enabled: New enabled state.
        """
        channel = "system.strategy.enabled" if enabled else "system.strategy.disabled"
        await self._event_bus.publish(
            channel,
            {
                "strategy": name,
                "enabled": enabled,
                "timestamp": datetime.utcnow().isoformat(),
            }
        )

    async def _on_market_data(self, data: dict) -> None:
        """Handle market data update."""
        market_id = data.get("market_id")
        if not market_id:
            return

        # Find strategies interested in this market
        strategy_names = self._market_to_strategies.get(market_id, set())
        if not strategy_names:
            return

        # Build OrderBook from data
        from decimal import Decimal
        from mercury.domain.market import OrderBookLevel

        # Parse price data into OrderBookLevel lists
        yes_bids = []
        yes_asks = []
        no_bids = []
        no_asks = []

        if data.get("yes_bid"):
            yes_bids.append(OrderBookLevel(
                price=Decimal(data["yes_bid"]),
                size=Decimal(data.get("yes_bid_size", "100"))
            ))
        if data.get("yes_ask"):
            yes_asks.append(OrderBookLevel(
                price=Decimal(data["yes_ask"]),
                size=Decimal(data.get("yes_ask_size", "100"))
            ))
        if data.get("no_bid"):
            no_bids.append(OrderBookLevel(
                price=Decimal(data["no_bid"]),
                size=Decimal(data.get("no_bid_size", "100"))
            ))
        if data.get("no_ask"):
            no_asks.append(OrderBookLevel(
                price=Decimal(data["no_ask"]),
                size=Decimal(data.get("no_ask_size", "100"))
            ))

        # Create OrderBook with level lists
        book = OrderBook(
            market_id=market_id,
            yes_bids=yes_bids,
            yes_asks=yes_asks,
            no_bids=no_bids,
            no_asks=no_asks,
            timestamp=datetime.utcnow(),
        )

        # Route to each strategy
        for name in strategy_names:
            strategy = self._strategies.get(name)
            if strategy is None or not strategy.enabled:
                continue

            try:
                async for signal in strategy.on_market_data(market_id, book):
                    await self._publish_signal(name, signal)
            except Exception as e:
                self._log.error(
                    "strategy_error",
                    strategy=name,
                    market_id=market_id,
                    error=str(e),
                )

    async def _publish_signal(self, strategy_name: str, signal: TradingSignal) -> None:
        """Publish a trading signal to EventBus.

        Publishes to signal.{strategy_name} channel with full TradingSignal data.

        Signal payload includes:
        - signal_id: Unique signal identifier
        - strategy_name: Name of the strategy that generated this signal
        - market_id: The market condition ID
        - signal_type: Type of signal (BUY_YES, BUY_NO, ARBITRAGE, etc.)
        - confidence: Signal confidence score (0.0 to 1.0)
        - priority: Signal priority level (low, medium, high, critical)
        - target_size_usd: Target trade size in USD
        - yes_price: Current YES price
        - no_price: Current NO price
        - expected_pnl: Expected profit/loss
        - max_slippage: Maximum acceptable slippage
        - metadata: Additional strategy-specific data
        - created_at: When the signal was generated
        - expires_at: When the signal expires (optional)

        Args:
            strategy_name: Name of the strategy generating the signal.
            signal: The TradingSignal to publish.
        """
        self._log.info(
            "signal_generated",
            strategy=strategy_name,
            signal_id=signal.signal_id,
            signal_type=signal.signal_type.value,
            target_size=str(signal.target_size_usd),
            confidence=signal.confidence,
            priority=signal.priority.value,
        )

        # Build the full signal payload with all fields
        payload = {
            "signal_id": signal.signal_id,
            "strategy_name": strategy_name,
            "market_id": signal.market_id,
            "signal_type": signal.signal_type.value,
            "confidence": signal.confidence,
            "priority": signal.priority.value,
            "target_size_usd": str(signal.target_size_usd),
            "yes_price": str(signal.yes_price),
            "no_price": str(signal.no_price),
            "expected_pnl": str(signal.expected_pnl),
            "max_slippage": str(signal.max_slippage),
            "metadata": signal.metadata,
            "created_at": signal.created_at.isoformat(),
        }

        # Add optional expires_at if set
        if signal.expires_at is not None:
            payload["expires_at"] = signal.expires_at.isoformat()

        await self._event_bus.publish(f"signal.{strategy_name}", payload)

    async def _on_enable_strategy(self, data: dict) -> None:
        """Handle enable strategy request from event bus."""
        name = data.get("strategy")
        if name:
            # Don't publish event since this came from event bus
            # (prevents infinite loop if someone listens to state change events)
            await self.enable_strategy(name, publish_event=True)

    async def _on_disable_strategy(self, data: dict) -> None:
        """Handle disable strategy request from event bus."""
        name = data.get("strategy")
        if name:
            # Don't publish event since this came from event bus
            await self.disable_strategy(name, publish_event=True)

    def _on_config_reload(
        self,
        old_config: dict[str, Any],
        new_config: dict[str, Any],
    ) -> None:
        """Handle configuration hot-reload.

        Syncs strategy enabled/disabled states based on new config.
        This is a synchronous callback, so we schedule the async work.

        Args:
            old_config: Previous configuration data.
            new_config: New configuration data after reload.
        """
        self._log.info("config_reload_detected")

        # Schedule the async sync operation
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're in an async context, schedule as a task
                asyncio.create_task(self._sync_strategy_states_from_config())
            else:
                # Run directly (shouldn't happen in normal operation)
                loop.run_until_complete(self._sync_strategy_states_from_config())
        except RuntimeError:
            # No event loop running, log and skip
            self._log.warning("config_reload_no_event_loop")

    async def _sync_strategy_states_from_config(self) -> None:
        """Sync all strategy enabled/disabled states from configuration.

        Reads the current configuration for each registered strategy
        and enables/disables them accordingly.
        """
        enabled_count = 0
        disabled_count = 0

        for name in list(self._strategies.keys()):
            # Check if strategy should be enabled according to config
            config_enabled = self._config.get_bool(
                f"strategies.{name}.enabled",
                default=True,  # Default to enabled if not specified
            )

            strategy = self._strategies.get(name)
            if strategy is None:
                continue

            if config_enabled and not strategy.enabled:
                await self.enable_strategy(name)
                enabled_count += 1
            elif not config_enabled and strategy.enabled:
                await self.disable_strategy(name)
                disabled_count += 1

        if enabled_count > 0 or disabled_count > 0:
            self._log.info(
                "strategies_synced_from_config",
                enabled=enabled_count,
                disabled=disabled_count,
            )

    async def sync_strategy_states(self) -> dict[str, bool]:
        """Manually sync strategy states from configuration.

        This can be called to force a sync of strategy states
        with the current configuration values.

        Returns:
            Dict mapping strategy names to their new enabled state.
        """
        await self._sync_strategy_states_from_config()
        return {name: s.enabled for name, s in self._strategies.items()}
