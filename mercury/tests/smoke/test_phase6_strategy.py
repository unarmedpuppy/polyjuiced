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


def make_gabagool_config_mock():
    """Create a properly configured mock for Gabagool strategy."""
    mock_config = MagicMock()

    # Configure get_bool to return appropriate booleans
    def mock_get_bool(key, default=False):
        values = {
            "strategies.gabagool.enabled": True,
            "strategies.gabagool.balance_sizing_enabled": True,
            "strategies.gabagool.gradual_entry_enabled": False,
        }
        return values.get(key, default)

    # Configure get_list to return appropriate lists
    def mock_get_list(key, default=None):
        values = {
            "strategies.gabagool.markets": ["BTC", "ETH", "SOL"],
        }
        return values.get(key, default if default is not None else [])

    # Configure get_decimal to return appropriate decimals
    def mock_get_decimal(key, default=None):
        values = {
            "strategies.gabagool.min_spread_threshold": Decimal("0.015"),
            "strategies.gabagool.max_trade_size_usd": Decimal("25.0"),
            "strategies.gabagool.max_per_window_usd": Decimal("50.0"),
            "strategies.gabagool.balance_sizing_pct": Decimal("0.25"),
            "strategies.gabagool.gradual_entry_min_spread_cents": Decimal("3.0"),
            "strategies.gabagool.min_hedge_ratio": Decimal("0.8"),
            "strategies.gabagool.critical_hedge_ratio": Decimal("0.5"),
        }
        return values.get(key, default if default is not None else Decimal("0"))

    # Configure get_int to return appropriate integers
    def mock_get_int(key, default=0):
        values = {
            "strategies.gabagool.min_time_remaining_seconds": 60,
            "strategies.gabagool.gradual_entry_tranches": 3,
        }
        return values.get(key, default)

    mock_config.get_bool = MagicMock(side_effect=mock_get_bool)
    mock_config.get_list = MagicMock(side_effect=mock_get_list)
    mock_config.get_decimal = MagicMock(side_effect=mock_get_decimal)
    mock_config.get_int = MagicMock(side_effect=mock_get_int)
    mock_config.get = MagicMock(return_value=None)
    mock_config.register_reload_callback = MagicMock()
    mock_config.unregister_reload_callback = MagicMock()

    return mock_config


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

    def test_strategy_registry_returns_enabled(self):
        """Verify StrategyRegistry returns only enabled strategies."""
        from mercury.strategies.registry import StrategyRegistry

        mock_config = make_gabagool_config_mock()
        mock_config.get_bool = MagicMock(side_effect=lambda key, default=False: {
            "strategies.gabagool.enabled": True,
            "strategies.gabagool.balance_sizing_enabled": True,
            "strategies.gabagool.gradual_entry_enabled": False,
        }.get(key, default))

        registry = StrategyRegistry(config=mock_config)
        registry.discover()

        enabled = registry.get_enabled_strategies(config=mock_config)
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
    async def test_gabagool_generates_signals(self):
        """Verify Gabagool generates signals from arbitrage opportunity."""
        from mercury.strategies.gabagool import GabagoolStrategy
        from mercury.domain.market import OrderBook, OrderBookLevel

        mock_config = make_gabagool_config_mock()

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
    async def test_gabagool_no_signal_when_no_opportunity(self):
        """Verify Gabagool doesn't signal when no opportunity."""
        from mercury.strategies.gabagool import GabagoolStrategy
        from mercury.domain.market import OrderBook, OrderBookLevel

        mock_config = make_gabagool_config_mock()

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
    async def test_runtime_enable_disable(self):
        """Verify strategies can be enabled/disabled at runtime."""
        from mercury.services.strategy_engine import StrategyEngine
        from mercury.strategies.gabagool import GabagoolStrategy

        mock_config = make_gabagool_config_mock()
        mock_event_bus = MagicMock()
        mock_event_bus.publish = AsyncMock()
        mock_event_bus.subscribe = AsyncMock()
        mock_event_bus.unsubscribe = AsyncMock()

        engine = StrategyEngine(config=mock_config, event_bus=mock_event_bus)

        # Register a Gabagool strategy
        strategy = GabagoolStrategy(config=mock_config)
        engine.register_strategy(strategy)

        await engine.start()

        assert engine.is_strategy_enabled("gabagool")

        await engine.disable_strategy("gabagool")
        assert not engine.is_strategy_enabled("gabagool")

        await engine.enable_strategy("gabagool")
        assert engine.is_strategy_enabled("gabagool")

        await engine.stop()

    @pytest.mark.asyncio
    async def test_signals_published_to_event_bus(self):
        """Verify signals are published to EventBus."""
        from mercury.services.strategy_engine import StrategyEngine
        from mercury.strategies.gabagool import GabagoolStrategy

        mock_config = make_gabagool_config_mock()
        mock_event_bus = MagicMock()
        mock_event_bus.publish = AsyncMock()
        mock_event_bus.subscribe = AsyncMock()
        mock_event_bus.unsubscribe = AsyncMock()

        engine = StrategyEngine(config=mock_config, event_bus=mock_event_bus)

        # Register a Gabagool strategy and subscribe it to a market
        strategy = GabagoolStrategy(config=mock_config)
        strategy.subscribe_market("test-market")
        engine.register_strategy(strategy)

        await engine.start()

        # Reset mock to clear subscription calls
        mock_event_bus.publish.reset_mock()

        # Simulate market data with opportunity (as a dict, which is what _on_market_data expects)
        await engine._on_market_data({
            "market_id": "test-market",
            "yes_bid": "0.47",
            "yes_ask": "0.48",
            "no_bid": "0.49",
            "no_ask": "0.50",
        })

        # Check signal was published
        calls = mock_event_bus.publish.call_args_list
        channels = [call[0][0] for call in calls]

        assert any("signal.gabagool" in c for c in channels)

        await engine.stop()

    def test_gabagool_config_importable(self):
        """Verify Gabagool config can be imported."""
        from mercury.strategies.gabagool.config import GabagoolConfig
        assert GabagoolConfig is not None

    def test_gabagool_config_loads_from_toml(self):
        """Verify Gabagool config loads from ConfigManager."""
        from mercury.strategies.gabagool.config import GabagoolConfig

        mock_config = make_gabagool_config_mock()

        config = GabagoolConfig.from_config(mock_config)

        assert config.enabled is True
        assert config.min_spread_threshold == Decimal("0.015")
        assert config.max_trade_size_usd == Decimal("25.0")
        assert "BTC" in config.markets
