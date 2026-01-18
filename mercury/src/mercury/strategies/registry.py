"""Strategy Registry - auto-discovery and management of trading strategies.

This module provides:
- Auto-discovery of strategy classes in the strategies/ package
- Manual registration of strategies
- Config-based filtering of enabled strategies
"""

import importlib
import importlib.util
import inspect
import pkgutil
from pathlib import Path
from typing import Any, Callable, Type, TypeVar

import structlog

from mercury.core.config import ConfigManager
from mercury.strategies.base import BaseStrategy

log = structlog.get_logger()

# Type for strategy class (not instance)
StrategyClass = Type[Any]  # Classes that implement BaseStrategy protocol
StrategyFactory = Callable[..., Any]  # Factory functions that return strategy instances

T = TypeVar("T")


class StrategyRegistry:
    """Registry for strategy discovery, registration, and instantiation.

    The registry supports two modes of strategy registration:

    1. Auto-discovery: Scans the strategies/ package for classes that implement
       the BaseStrategy protocol.

    2. Manual registration: Explicitly register strategy classes or factories.

    Configuration controls which strategies are enabled:
    - strategies.<name>.enabled = true/false in TOML config
    - Environment variable: MERCURY_STRATEGIES_<NAME>_ENABLED

    Usage:
        registry = StrategyRegistry(config)

        # Auto-discover strategies
        registry.discover()

        # Or manually register
        registry.register(MyStrategy)

        # Get enabled strategy instances
        strategies = registry.get_enabled_strategies(event_bus=event_bus)
    """

    def __init__(self, config: ConfigManager) -> None:
        """Initialize the strategy registry.

        Args:
            config: ConfigManager for checking enabled status.
        """
        self._config = config
        self._log = log.bind(component="strategy_registry")
        self._registry: dict[str, StrategyClass | StrategyFactory] = {}
        self._discovered = False

    @property
    def registered_count(self) -> int:
        """Number of registered strategies."""
        return len(self._registry)

    @property
    def registered_names(self) -> list[str]:
        """Names of all registered strategies."""
        return list(self._registry.keys())

    @property
    def available_strategies(self) -> list[str]:
        """Alias for registered_names for backwards compatibility."""
        return self.registered_names

    def register(
        self,
        strategy: StrategyClass | StrategyFactory,
        name: str | None = None,
    ) -> None:
        """Register a strategy class or factory.

        Args:
            strategy: Strategy class or factory function.
            name: Optional name override. If not provided, uses:
                  - Class name in snake_case for classes
                  - Function name for factories
        """
        # Determine the name
        if name is None:
            if inspect.isclass(strategy):
                name = self._class_name_to_strategy_name(strategy.__name__)
            elif callable(strategy):
                name = strategy.__name__
            else:
                raise ValueError(
                    f"Cannot determine name for {strategy}. "
                    "Provide a name explicitly."
                )

        if name in self._registry:
            self._log.warning("strategy_already_registered", name=name)
            return

        self._registry[name] = strategy
        self._log.info("strategy_registered", name=name)

    def unregister(self, name: str) -> bool:
        """Unregister a strategy by name.

        Args:
            name: Strategy name to unregister.

        Returns:
            True if unregistered, False if not found.
        """
        if name not in self._registry:
            return False

        del self._registry[name]
        self._log.info("strategy_unregistered", name=name)
        return True

    def is_registered(self, name: str) -> bool:
        """Check if a strategy is registered.

        Args:
            name: Strategy name.

        Returns:
            True if registered.
        """
        return name in self._registry

    def is_enabled(self, name: str) -> bool:
        """Check if a strategy is enabled in configuration.

        Args:
            name: Strategy name.

        Returns:
            True if enabled in config.
        """
        return self._config.get_bool(f"strategies.{name}.enabled", default=False)

    def get_enabled_names(self) -> list[str]:
        """Get names of all registered and enabled strategies.

        Returns:
            List of enabled strategy names.
        """
        return [name for name in self._registry if self.is_enabled(name)]

    def get_disabled_names(self) -> list[str]:
        """Get names of all registered but disabled strategies.

        Returns:
            List of disabled strategy names.
        """
        return [name for name in self._registry if not self.is_enabled(name)]

    def get_strategy_class(self, name: str) -> StrategyClass | StrategyFactory | None:
        """Get a strategy class or factory by name.

        Args:
            name: Strategy name.

        Returns:
            Strategy class/factory or None if not found.
        """
        return self._registry.get(name)

    def create_instance(self, name: str, **kwargs: Any) -> Any | None:
        """Create an instance of a strategy.

        Args:
            name: Strategy name.
            **kwargs: Arguments to pass to the constructor/factory.

        Returns:
            Strategy instance or None if not found.
        """
        strategy_cls = self._registry.get(name)
        if strategy_cls is None:
            self._log.warning("strategy_not_found", name=name)
            return None

        try:
            instance = strategy_cls(**kwargs)
            self._log.info("strategy_instantiated", name=name)
            return instance
        except Exception as e:
            self._log.error(
                "strategy_instantiation_failed",
                name=name,
                error=str(e),
            )
            raise

    def get_enabled_strategies(self, **kwargs: Any) -> list[Any]:
        """Get instances of all enabled strategies.

        Args:
            **kwargs: Arguments to pass to strategy constructors.

        Returns:
            List of strategy instances.
        """
        strategies = []

        for name in self.get_enabled_names():
            try:
                instance = self.create_instance(name, **kwargs)
                if instance is not None:
                    strategies.append(instance)
            except Exception as e:
                self._log.error(
                    "failed_to_create_strategy",
                    name=name,
                    error=str(e),
                )
                # Continue with other strategies

        self._log.info(
            "enabled_strategies_loaded",
            count=len(strategies),
            names=[s.name if hasattr(s, "name") else str(type(s)) for s in strategies],
        )

        return strategies

    def discover(self, package_path: str | None = None) -> int:
        """Auto-discover strategy classes in the strategies package.

        Scans for classes that implement the BaseStrategy protocol.
        Excludes the base module and registry itself.

        Args:
            package_path: Optional path to strategies package.
                         Defaults to mercury.strategies.

        Returns:
            Number of strategies discovered.
        """
        if package_path is None:
            # Default to mercury.strategies package
            package_path = "mercury.strategies"

        discovered_count = 0

        try:
            package = importlib.import_module(package_path)
        except ImportError as e:
            self._log.error("failed_to_import_package", package=package_path, error=str(e))
            return 0

        # Get the package directory
        if not hasattr(package, "__path__"):
            self._log.warning("package_has_no_path", package=package_path)
            return 0

        package_dirs = package.__path__

        # Iterate through all modules in the package
        for module_info in pkgutil.walk_packages(package_dirs, prefix=f"{package_path}."):
            module_name = module_info.name

            # Skip known non-strategy modules
            if self._should_skip_module(module_name):
                continue

            try:
                module = importlib.import_module(module_name)
                discovered_count += self._scan_module_for_strategies(module)
            except ImportError as e:
                self._log.warning(
                    "failed_to_import_module",
                    module=module_name,
                    error=str(e),
                )
            except Exception as e:
                self._log.warning(
                    "error_scanning_module",
                    module=module_name,
                    error=str(e),
                )

        self._discovered = True
        self._log.info(
            "strategy_discovery_complete",
            discovered=discovered_count,
            total_registered=self.registered_count,
        )

        return discovered_count

    def _should_skip_module(self, module_name: str) -> bool:
        """Check if a module should be skipped during discovery.

        Args:
            module_name: Full module name.

        Returns:
            True if should skip.
        """
        # Skip known non-strategy modules
        skip_suffixes = [
            ".base",
            ".registry",
            ".__init__",
        ]

        for suffix in skip_suffixes:
            if module_name.endswith(suffix):
                return True

        return False

    def _scan_module_for_strategies(self, module: Any) -> int:
        """Scan a module for strategy classes.

        Args:
            module: Module to scan.

        Returns:
            Number of strategies found.
        """
        found = 0

        for name, obj in inspect.getmembers(module, inspect.isclass):
            # Skip imported classes (only look at classes defined in this module)
            if obj.__module__ != module.__name__:
                continue

            # Skip the BaseStrategy protocol itself
            if obj is BaseStrategy:
                continue

            # Check if it implements BaseStrategy
            if self._is_strategy_class(obj):
                strategy_name = self._class_name_to_strategy_name(name)
                self.register(obj, name=strategy_name)
                found += 1

        return found

    def _is_strategy_class(self, cls: type) -> bool:
        """Check if a class implements the BaseStrategy protocol.

        Args:
            cls: Class to check.

        Returns:
            True if implements BaseStrategy.
        """
        # Check for required properties and methods
        required_attrs = ["name", "enabled", "start", "stop", "on_market_data", "get_subscribed_markets"]

        for attr in required_attrs:
            if not hasattr(cls, attr):
                return False

        # Additional check: verify it's a class (not protocol)
        # and has concrete implementations (not abstract)
        if inspect.isabstract(cls):
            return False

        return True

    def _class_name_to_strategy_name(self, class_name: str) -> str:
        """Convert a class name to a strategy name.

        Examples:
            GabagoolStrategy -> gabagool
            MyArbitrageStrategy -> my_arbitrage

        Args:
            class_name: Class name (e.g., "GabagoolStrategy").

        Returns:
            Strategy name in snake_case without "Strategy" suffix.
        """
        # Remove common suffixes
        for suffix in ["Strategy", "Strat"]:
            if class_name.endswith(suffix):
                class_name = class_name[: -len(suffix)]
                break

        # Convert CamelCase to snake_case
        result = []
        for i, char in enumerate(class_name):
            if char.isupper() and i > 0:
                result.append("_")
            result.append(char.lower())

        return "".join(result)

    def clear(self) -> None:
        """Clear all registered strategies."""
        self._registry.clear()
        self._discovered = False
        self._log.info("registry_cleared")

    def __len__(self) -> int:
        """Return number of registered strategies."""
        return len(self._registry)

    def __contains__(self, name: str) -> bool:
        """Check if strategy is registered."""
        return name in self._registry

    def __iter__(self):
        """Iterate over registered strategy names."""
        return iter(self._registry)
