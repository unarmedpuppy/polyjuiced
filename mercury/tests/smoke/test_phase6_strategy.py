"""
Phase 6 Smoke Test: Strategy Engine + Gabagool Port

Verifies that Phase 6 deliverables work:
- BaseStrategy protocol is defined
- StrategyEngine loads and routes to strategies
- StrategyRegistry discovers strategies
- Gabagool strategy generates signals
- Runtime enable/disable works
- Signals are published to EventBus

Run: pytest tests/smoke/test_phase6_strategy.py -v
"""
import pytest
from decimal import Decimal
from unittest.mock import MagicMock, AsyncMock


class TestPhase6StrategyEngine:
    """Phase 6 must pass ALL these tests to be considered complete."""

    def test_base_strategy_importable(self):
        """Verify BaseStrategy protocol can be imported."""
        from mercury.strategies.base import BaseStrategy
        assert BaseStrategy is not None

    def test_strategy_engine_importable(self):
        """Verify StrategyEngine can be imported."""
        from mercury.services.strategy_engine import StrategyEngine
        assert StrategyEngine is not None

    def test_strategy_registry_importable(self):
        """Verify StrategyRegistry can be imported."""
        from mercury.strategies.registry import StrategyRegistry
        assert StrategyRegistry is not None

    def test_gabagool_strategy_importable(self):
        """Verify Gabagool strategy can be imported."""
        from mercury.strategies.gabagool import GabagoolStrategy
        assert GabagoolStrategy is not None

    @pytest.mark.asyncio
    async def test_strategy_engine_starts_stops(self, mock_config, mock_event_bus):
        """Verify StrategyEngine lifecycle works."""
        from mercury.services.strategy_engine import StrategyEngine

        engine = StrategyEngine(config=mock_config, event_bus=mock_event_bus)

        await engine.start()
        assert engine.is_running

        await engine.stop()
        assert not engine.is_running

    def test_strategy_registry_discovers_strategies(self, mock_config):
        """Verify StrategyRegistry discovers available strategies."""
        from mercury.strategies.registry import StrategyRegistry

        registry = StrategyRegistry(config=mock_config)
        registry.discover()

        strategies = registry.available_strategies
        assert "gabagool" in strategies

    def test_strategy_registry_returns_enabled(self, mock_config):
        """Verify StrategyRegistry returns only enabled strategies."""
        from mercury.strategies.registry import StrategyRegistry

        mock_config.get.side_effect = lambda k, d=None: {
            "strategies.gabagool.enabled": True,
        }.get(k, d)

        registry = StrategyRegistry(config=mock_config)
        registry.discover()

        enabled = registry.get_enabled_strategies()
        assert len(enabled) >= 1
        assert any(s.name == "gabagool" for s in enabled)

    def test_gabagool_implements_protocol(self):
        """Verify Gabagool implements BaseStrategy protocol."""
        from mercury.strategies.gabagool import GabagoolStrategy
        from mercury.strategies.base import BaseStrategy

        # Check it has required attributes/methods
        strategy = GabagoolStrategy.__new__(GabagoolStrategy)

        assert hasattr(strategy, 'name')
        assert hasattr(strategy, 'enabled')
        assert hasattr(strategy, 'on_market_data')
        assert hasattr(strategy, 'get_subscribed_markets')
        assert callable(getattr(strategy, 'start', None))
        assert callable(getattr(strategy, 'stop', None))

    @pytest.mark.asyncio
    async def test_gabagool_generates_signals(self, mock_config):
        """Verify Gabagool generates signals from arbitrage opportunity."""
        from mercury.strategies.gabagool import GabagoolStrategy
        from mercury.domain.market import OrderBook, OrderBookLevel

        mock_config.get.side_effect = lambda k, d=None: {
            "strategies.gabagool.min_spread_threshold": 0.01,
            "strategies.gabagool.max_trade_size_usd": Decimal("25.0"),
            "strategies.gabagool.markets": ["BTC"],
        }.get(k, d)

        strategy = GabagoolStrategy(config=mock_config)

        # Create order book with arbitrage opportunity
        # YES at 0.48 + NO at 0.50 = 0.98 < 1.00 (2% spread)
        book = OrderBook(
            market_id="test-market",
            yes_bids=[],
            yes_asks=[OrderBookLevel(price=Decimal("0.48"), size=Decimal("100"))],
            no_bids=[],
            no_asks=[OrderBookLevel(price=Decimal("0.50"), size=Decimal("100"))],
        )

        signals = []
        async for signal in strategy.on_market_data("test-market", book):
            signals.append(signal)

        assert len(signals) >= 1
        assert signals[0].signal_type.value in ["ARBITRAGE", "BUY_BOTH"]

    @pytest.mark.asyncio
    async def test_gabagool_no_signal_when_no_opportunity(self, mock_config):
        """Verify Gabagool doesn't signal when no opportunity."""
        from mercury.strategies.gabagool import GabagoolStrategy
        from mercury.domain.market import OrderBook, OrderBookLevel

        mock_config.get.side_effect = lambda k, d=None: {
            "strategies.gabagool.min_spread_threshold": 0.01,
            "strategies.gabagool.max_trade_size_usd": Decimal("25.0"),
        }.get(k, d)

        strategy = GabagoolStrategy(config=mock_config)

        # Create order book WITHOUT arbitrage (YES + NO > 1.00)
        book = OrderBook(
            market_id="test-market",
            yes_bids=[],
            yes_asks=[OrderBookLevel(price=Decimal("0.52"), size=Decimal("100"))],
            no_bids=[],
            no_asks=[OrderBookLevel(price=Decimal("0.52"), size=Decimal("100"))],
        )

        signals = []
        async for signal in strategy.on_market_data("test-market", book):
            signals.append(signal)

        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_runtime_enable_disable(self, mock_config, mock_event_bus):
        """Verify strategies can be enabled/disabled at runtime."""
        from mercury.services.strategy_engine import StrategyEngine

        mock_config.get.side_effect = lambda k, d=None: {
            "strategies.gabagool.enabled": True,
        }.get(k, d)

        engine = StrategyEngine(config=mock_config, event_bus=mock_event_bus)
        await engine.start()

        assert engine.is_strategy_enabled("gabagool")

        engine.disable_strategy("gabagool")
        assert not engine.is_strategy_enabled("gabagool")

        engine.enable_strategy("gabagool")
        assert engine.is_strategy_enabled("gabagool")

        await engine.stop()

    @pytest.mark.asyncio
    async def test_signals_published_to_event_bus(self, mock_config, mock_event_bus):
        """Verify signals are published to EventBus."""
        from mercury.services.strategy_engine import StrategyEngine
        from mercury.domain.market import OrderBook, OrderBookLevel

        mock_config.get.side_effect = lambda k, d=None: {
            "strategies.gabagool.enabled": True,
            "strategies.gabagool.min_spread_threshold": 0.01,
            "strategies.gabagool.max_trade_size_usd": Decimal("25.0"),
            "strategies.gabagool.markets": ["BTC"],
        }.get(k, d)

        engine = StrategyEngine(config=mock_config, event_bus=mock_event_bus)
        await engine.start()

        # Simulate market data with opportunity
        book = OrderBook(
            market_id="test-market",
            yes_bids=[],
            yes_asks=[OrderBookLevel(price=Decimal("0.48"), size=Decimal("100"))],
            no_bids=[],
            no_asks=[OrderBookLevel(price=Decimal("0.50"), size=Decimal("100"))],
        )

        await engine._on_market_data("test-market", book)

        # Check signal was published
        calls = mock_event_bus.publish.call_args_list
        channels = [call[0][0] for call in calls]

        assert any("signal.gabagool" in c for c in channels)

        await engine.stop()

    def test_gabagool_config_importable(self):
        """Verify Gabagool config can be imported."""
        from mercury.strategies.gabagool.config import GabagoolConfig
        assert GabagoolConfig is not None

    def test_gabagool_config_loads_from_toml(self, mock_config):
        """Verify Gabagool config loads from ConfigManager."""
        from mercury.strategies.gabagool.config import GabagoolConfig

        mock_config.get.side_effect = lambda k, d=None: {
            "strategies.gabagool.enabled": True,
            "strategies.gabagool.min_spread_threshold": 0.015,
            "strategies.gabagool.max_trade_size_usd": 25.0,
            "strategies.gabagool.markets": ["BTC", "ETH"],
        }.get(k, d)

        config = GabagoolConfig.from_config(mock_config)

        assert config.enabled is True
        assert config.min_spread_threshold == 0.015
        assert config.max_trade_size_usd == 25.0
        assert "BTC" in config.markets
