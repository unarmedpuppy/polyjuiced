"""
Unit tests for StrategyRegistry.

Tests verify:
- Manual registration of strategies
- Auto-discovery of strategy classes
- Config-based enabling/filtering
- Strategy instantiation
- Name conversion utilities
"""

from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import List
from unittest.mock import MagicMock, patch
import pytest

from mercury.strategies.registry import StrategyRegistry
from mercury.strategies.base import BaseStrategy
from mercury.domain.market import OrderBook, OrderBookLevel
from mercury.domain.signal import TradingSignal, SignalType, SignalPriority


class MockStrategy:
    """A concrete strategy implementation for testing."""

    def __init__(self, name: str = "mock_strategy", config: MagicMock = None):
        self._name = name
        self._enabled = True
        self._subscribed_markets: List[str] = []
        self._started = False
        self._stopped = False
        self._config = config

    @property
    def name(self) -> str:
        return self._name

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._stopped = True

    async def on_market_data(
        self,
        market_id: str,
        book: OrderBook,
    ) -> AsyncIterator[TradingSignal]:
        if book.has_arbitrage_opportunity:
            yield TradingSignal(
                strategy_name=self._name,
                market_id=market_id,
                signal_type=SignalType.ARBITRAGE,
                confidence=0.9,
                priority=SignalPriority.HIGH,
                target_size_usd=Decimal("25.00"),
                yes_price=book.yes_best_ask or Decimal("0"),
                no_price=book.no_best_ask or Decimal("0"),
                expected_pnl=Decimal("1") - (book.combined_ask or Decimal("1")),
            )

    def get_subscribed_markets(self) -> list[str]:
        return self._subscribed_markets

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False


class AnotherStrategy(MockStrategy):
    """Another strategy for testing multiple registrations."""

    def __init__(self, config: MagicMock = None):
        super().__init__(name="another_strategy", config=config)


class GabagoolStrategy(MockStrategy):
    """Strategy with naming convention for testing name conversion."""

    def __init__(self, config: MagicMock = None):
        super().__init__(name="gabagool", config=config)


class NotAStrategy:
    """A class that doesn't implement BaseStrategy."""

    def __init__(self):
        self.value = 42


@pytest.fixture
def mock_config():
    """Create mock ConfigManager."""
    config = MagicMock()
    config.get.return_value = None
    config.get_bool.return_value = False
    return config


@pytest.fixture
def registry(mock_config):
    """Create a StrategyRegistry instance for testing."""
    return StrategyRegistry(config=mock_config)


class TestStrategyRegistration:
    """Tests for manual strategy registration."""

    def test_register_strategy_class(self, registry):
        """Verify strategy class can be registered."""
        registry.register(MockStrategy)

        assert registry.registered_count == 1
        # MockStrategy -> mock (removes Strategy suffix)
        assert "mock" in registry.registered_names

    def test_register_with_custom_name(self, registry):
        """Verify strategy can be registered with custom name."""
        registry.register(MockStrategy, name="custom_name")

        assert "custom_name" in registry.registered_names
        assert "mock" not in registry.registered_names

    def test_register_duplicate_is_idempotent(self, registry):
        """Verify registering same strategy twice doesn't duplicate."""
        registry.register(MockStrategy)
        registry.register(MockStrategy)

        assert registry.registered_count == 1

    def test_register_multiple_strategies(self, registry):
        """Verify multiple strategies can be registered."""
        registry.register(MockStrategy)
        registry.register(AnotherStrategy)

        assert registry.registered_count == 2
        # MockStrategy -> mock, AnotherStrategy -> another
        assert "mock" in registry.registered_names
        assert "another" in registry.registered_names

    def test_unregister_strategy(self, registry):
        """Verify strategy can be unregistered."""
        registry.register(MockStrategy)
        result = registry.unregister("mock")

        assert result is True
        assert registry.registered_count == 0

    def test_unregister_nonexistent_returns_false(self, registry):
        """Verify unregistering non-existent strategy returns False."""
        result = registry.unregister("nonexistent")
        assert result is False

    def test_is_registered(self, registry):
        """Verify is_registered returns correct status."""
        registry.register(MockStrategy)

        assert registry.is_registered("mock") is True
        assert registry.is_registered("nonexistent") is False

    def test_get_strategy_class(self, registry):
        """Verify get_strategy_class returns the registered class."""
        registry.register(MockStrategy)

        cls = registry.get_strategy_class("mock")
        assert cls is MockStrategy

    def test_get_strategy_class_not_found(self, registry):
        """Verify get_strategy_class returns None for unknown strategy."""
        cls = registry.get_strategy_class("nonexistent")
        assert cls is None


class TestStrategyFactory:
    """Tests for factory function registration."""

    def test_register_factory_function(self, registry):
        """Verify factory function can be registered."""

        def create_mock_strategy(**kwargs):
            return MockStrategy(name="from_factory")

        registry.register(create_mock_strategy)

        assert "create_mock_strategy" in registry.registered_names

    def test_register_factory_with_custom_name(self, registry):
        """Verify factory can be registered with custom name."""

        def my_factory(**kwargs):
            return MockStrategy(name="custom")

        registry.register(my_factory, name="custom_factory")

        assert "custom_factory" in registry.registered_names


class TestConfigBasedEnabling:
    """Tests for config-based strategy enabling."""

    def test_is_enabled_returns_config_value(self, registry, mock_config):
        """Verify is_enabled reads from config."""
        registry.register(MockStrategy)

        # Default is disabled
        mock_config.get_bool.return_value = False
        assert registry.is_enabled("mock") is False

        # Enable in config
        mock_config.get_bool.return_value = True
        assert registry.is_enabled("mock") is True

        # Verify correct config key was used
        mock_config.get_bool.assert_called_with(
            "strategies.mock.enabled", default=False
        )

    def test_get_enabled_names(self, registry, mock_config):
        """Verify get_enabled_names returns only enabled strategies."""
        registry.register(MockStrategy)
        registry.register(AnotherStrategy)

        # Only mock is enabled
        def get_enabled(key, **kwargs):
            return "strategies.mock.enabled" == key

        mock_config.get_bool.side_effect = get_enabled

        enabled = registry.get_enabled_names()

        assert "mock" in enabled
        assert "another" not in enabled

    def test_get_disabled_names(self, registry, mock_config):
        """Verify get_disabled_names returns only disabled strategies."""
        registry.register(MockStrategy)
        registry.register(AnotherStrategy)

        # Only mock is enabled
        def get_enabled(key, **kwargs):
            return "strategies.mock.enabled" == key

        mock_config.get_bool.side_effect = get_enabled

        disabled = registry.get_disabled_names()

        assert "mock" not in disabled
        assert "another" in disabled


class TestStrategyInstantiation:
    """Tests for strategy instance creation."""

    def test_create_instance(self, registry, mock_config):
        """Verify create_instance creates strategy instance."""
        registry.register(MockStrategy)

        instance = registry.create_instance("mock", config=mock_config)

        assert instance is not None
        assert isinstance(instance, MockStrategy)

    def test_create_instance_passes_kwargs(self, registry, mock_config):
        """Verify kwargs are passed to constructor."""
        registry.register(MockStrategy)

        # Use custom_name instead of name to avoid conflict
        instance = registry.create_instance("mock", config=mock_config)

        # The instance was created with mock_config passed to it
        assert instance is not None
        assert instance._config is mock_config

    def test_create_instance_not_found(self, registry):
        """Verify create_instance returns None for unknown strategy."""
        instance = registry.create_instance("nonexistent")
        assert instance is None

    def test_create_instance_error_propagates(self, registry):
        """Verify instantiation errors are propagated."""

        class BrokenStrategy(MockStrategy):
            def __init__(self, **kwargs):
                raise RuntimeError("Intentional error")

        registry.register(BrokenStrategy)

        with pytest.raises(RuntimeError, match="Intentional error"):
            registry.create_instance("broken")

    def test_get_enabled_strategies(self, registry, mock_config):
        """Verify get_enabled_strategies returns instances of enabled strategies."""
        registry.register(MockStrategy)
        registry.register(AnotherStrategy)

        # Only mock is enabled
        def get_enabled(key, **kwargs):
            return "strategies.mock.enabled" == key

        mock_config.get_bool.side_effect = get_enabled

        strategies = registry.get_enabled_strategies(config=mock_config)

        assert len(strategies) == 1
        assert strategies[0].name == "mock_strategy"

    def test_get_enabled_strategies_continues_on_error(self, registry, mock_config):
        """Verify get_enabled_strategies skips failed strategies."""

        class BrokenStrategy(MockStrategy):
            def __init__(self, **kwargs):
                raise RuntimeError("Intentional error")

        registry.register(BrokenStrategy)
        registry.register(MockStrategy)

        # Both enabled
        mock_config.get_bool.return_value = True

        # Should get MockStrategy despite BrokenStrategy error
        strategies = registry.get_enabled_strategies(config=mock_config)

        # Only MockStrategy should succeed
        assert len(strategies) == 1
        assert strategies[0].name == "mock_strategy"


class TestNameConversion:
    """Tests for class name to strategy name conversion."""

    def test_removes_strategy_suffix(self, registry):
        """Verify 'Strategy' suffix is removed."""
        name = registry._class_name_to_strategy_name("GabagoolStrategy")
        assert name == "gabagool"

    def test_removes_strat_suffix(self, registry):
        """Verify 'Strat' suffix is removed."""
        name = registry._class_name_to_strategy_name("ArbitrageStrat")
        assert name == "arbitrage"

    def test_converts_camel_to_snake(self, registry):
        """Verify CamelCase is converted to snake_case."""
        name = registry._class_name_to_strategy_name("MyArbitrageStrategy")
        assert name == "my_arbitrage"

    def test_handles_acronyms(self, registry):
        """Verify acronyms in names are handled."""
        name = registry._class_name_to_strategy_name("BTCArbitrageStrategy")
        assert name == "b_t_c_arbitrage"

    def test_handles_single_word(self, registry):
        """Verify single word names work."""
        name = registry._class_name_to_strategy_name("Gabagool")
        assert name == "gabagool"

    def test_already_lowercase(self, registry):
        """Verify already lowercase names work."""
        name = registry._class_name_to_strategy_name("arbitrage")
        assert name == "arbitrage"


class TestAutoDiscovery:
    """Tests for auto-discovery of strategies."""

    def test_discover_marks_discovery_done(self, registry):
        """Verify discover sets _discovered flag."""
        assert registry._discovered is False

        with patch.object(registry, "_scan_module_for_strategies", return_value=0):
            registry.discover()

        assert registry._discovered is True

    def test_should_skip_base_module(self, registry):
        """Verify base module is skipped."""
        assert registry._should_skip_module("mercury.strategies.base") is True

    def test_should_skip_registry_module(self, registry):
        """Verify registry module is skipped."""
        assert registry._should_skip_module("mercury.strategies.registry") is True

    def test_should_skip_init_module(self, registry):
        """Verify __init__ module is skipped."""
        assert registry._should_skip_module("mercury.strategies.__init__") is True

    def test_should_not_skip_strategy_module(self, registry):
        """Verify actual strategy modules are not skipped."""
        assert registry._should_skip_module("mercury.strategies.gabagool") is False
        assert (
            registry._should_skip_module("mercury.strategies.gabagool.strategy")
            is False
        )

    def test_is_strategy_class_valid(self, registry):
        """Verify valid strategy class is detected."""
        assert registry._is_strategy_class(MockStrategy) is True

    def test_is_strategy_class_invalid(self, registry):
        """Verify non-strategy class is rejected."""
        assert registry._is_strategy_class(NotAStrategy) is False

    def test_discover_returns_count(self, registry):
        """Verify discover returns count of discovered strategies."""
        # Mock the module scanning to return 2 strategies
        with patch.object(registry, "_scan_module_for_strategies", return_value=2):
            with patch("importlib.import_module") as mock_import:
                mock_pkg = MagicMock()
                mock_pkg.__path__ = ["/fake/path"]
                mock_import.return_value = mock_pkg

                with patch("pkgutil.walk_packages", return_value=[]):
                    count = registry.discover()

        # No modules to walk, so 0 discovered
        assert count == 0


class TestRegistryOperations:
    """Tests for registry utility operations."""

    def test_clear_removes_all(self, registry):
        """Verify clear removes all registered strategies."""
        registry.register(MockStrategy)
        registry.register(AnotherStrategy)

        registry.clear()

        assert registry.registered_count == 0
        assert registry._discovered is False

    def test_len_returns_count(self, registry):
        """Verify __len__ returns registered count."""
        assert len(registry) == 0

        registry.register(MockStrategy)
        assert len(registry) == 1

        registry.register(AnotherStrategy)
        assert len(registry) == 2

    def test_contains(self, registry):
        """Verify __contains__ works."""
        registry.register(MockStrategy)

        assert "mock" in registry
        assert "nonexistent" not in registry

    def test_iter(self, registry):
        """Verify __iter__ iterates over names."""
        registry.register(MockStrategy)
        registry.register(AnotherStrategy)

        names = list(registry)

        assert "mock" in names
        assert "another" in names


class TestProtocolCompliance:
    """Tests verifying created instances comply with BaseStrategy."""

    def test_instance_is_base_strategy(self, registry, mock_config):
        """Verify created instance implements BaseStrategy protocol."""
        registry.register(MockStrategy)

        instance = registry.create_instance("mock", config=mock_config)

        # Check protocol compliance via isinstance (runtime_checkable)
        assert isinstance(instance, BaseStrategy)

    def test_enabled_strategies_all_comply(self, registry, mock_config):
        """Verify all enabled strategies implement BaseStrategy."""
        registry.register(MockStrategy)
        registry.register(AnotherStrategy)

        mock_config.get_bool.return_value = True

        strategies = registry.get_enabled_strategies(config=mock_config)

        for strategy in strategies:
            assert isinstance(strategy, BaseStrategy)


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_discover_handles_import_error(self, registry):
        """Verify discover handles import errors gracefully."""
        with patch("importlib.import_module") as mock_import:
            mock_import.side_effect = ImportError("Module not found")

            # Should not raise, returns 0
            count = registry.discover()
            assert count == 0

    def test_discover_handles_no_path(self, registry):
        """Verify discover handles packages without __path__."""
        with patch("importlib.import_module") as mock_import:
            mock_module = MagicMock(spec=[])  # No __path__ attribute
            mock_import.return_value = mock_module

            count = registry.discover()
            assert count == 0

    def test_register_invalid_raises(self, registry):
        """Verify registering non-callable without name raises."""
        with pytest.raises(ValueError, match="Cannot determine name"):
            registry.register("not a class or function")  # type: ignore
