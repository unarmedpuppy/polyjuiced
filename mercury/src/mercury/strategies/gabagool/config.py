"""Configuration for Gabagool strategy.

Configuration is loaded from ConfigManager at initialization.
All parameters have sensible defaults.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from mercury.core.config import ConfigManager


@dataclass(frozen=True)
class GabagoolConfig:
    """Configuration parameters for the Gabagool arbitrage strategy.

    All monetary values are in USD. Spread thresholds are in decimal (0.015 = 1.5%).

    Attributes:
        enabled: Whether the strategy is enabled.
        markets: List of asset symbols to trade (e.g., ["BTC", "ETH"]).
        min_spread_threshold: Minimum arbitrage spread to trigger entry (decimal).
        max_trade_size_usd: Maximum USD per trade.
        max_per_window_usd: Maximum USD exposure per time window.
        min_time_remaining_seconds: Minimum seconds before market close to enter.
        balance_sizing_enabled: Whether to size trades based on balance.
        balance_sizing_pct: Percentage of balance to use per trade.
        gradual_entry_enabled: Whether to split entries into tranches.
        gradual_entry_tranches: Number of tranches for gradual entry.
        gradual_entry_min_spread_cents: Minimum spread (cents) for gradual entry.
        min_hedge_ratio: Minimum hedge ratio for partial fills.
        critical_hedge_ratio: Hedge ratio below which to reject trades.
    """

    enabled: bool = True
    markets: list[str] = field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    min_spread_threshold: Decimal = Decimal("0.015")  # 1.5 cents
    max_trade_size_usd: Decimal = Decimal("25.0")
    max_per_window_usd: Decimal = Decimal("50.0")
    min_time_remaining_seconds: int = 60
    balance_sizing_enabled: bool = True
    balance_sizing_pct: Decimal = Decimal("0.25")  # 25% of balance
    gradual_entry_enabled: bool = False
    gradual_entry_tranches: int = 3
    gradual_entry_min_spread_cents: Decimal = Decimal("3.0")
    min_hedge_ratio: Decimal = Decimal("0.8")  # 80%
    critical_hedge_ratio: Decimal = Decimal("0.5")  # 50%

    @classmethod
    def from_config(
        cls,
        config: ConfigManager,
        prefix: str = "strategies.gabagool",
    ) -> "GabagoolConfig":
        """Create GabagoolConfig from ConfigManager.

        Alias for from_config_manager for backwards compatibility.
        """
        return cls.from_config_manager(config, prefix)

    @classmethod
    def from_config_manager(
        cls,
        config: ConfigManager,
        prefix: str = "strategies.gabagool",
    ) -> "GabagoolConfig":
        """Create GabagoolConfig from ConfigManager.

        Args:
            config: ConfigManager instance.
            prefix: Configuration key prefix.

        Returns:
            GabagoolConfig instance.
        """
        return cls(
            enabled=config.get_bool(f"{prefix}.enabled", default=True),
            markets=config.get_list(f"{prefix}.markets", default=["BTC", "ETH", "SOL"]),
            min_spread_threshold=config.get_decimal(
                f"{prefix}.min_spread_threshold", default=Decimal("0.015")
            ),
            max_trade_size_usd=config.get_decimal(
                f"{prefix}.max_trade_size_usd", default=Decimal("25.0")
            ),
            max_per_window_usd=config.get_decimal(
                f"{prefix}.max_per_window_usd", default=Decimal("50.0")
            ),
            min_time_remaining_seconds=config.get_int(
                f"{prefix}.min_time_remaining_seconds", default=60
            ),
            balance_sizing_enabled=config.get_bool(
                f"{prefix}.balance_sizing_enabled", default=True
            ),
            balance_sizing_pct=config.get_decimal(
                f"{prefix}.balance_sizing_pct", default=Decimal("0.25")
            ),
            gradual_entry_enabled=config.get_bool(
                f"{prefix}.gradual_entry_enabled", default=False
            ),
            gradual_entry_tranches=config.get_int(
                f"{prefix}.gradual_entry_tranches", default=3
            ),
            gradual_entry_min_spread_cents=config.get_decimal(
                f"{prefix}.gradual_entry_min_spread_cents", default=Decimal("3.0")
            ),
            min_hedge_ratio=config.get_decimal(
                f"{prefix}.min_hedge_ratio", default=Decimal("0.8")
            ),
            critical_hedge_ratio=config.get_decimal(
                f"{prefix}.critical_hedge_ratio", default=Decimal("0.5")
            ),
        )

    @property
    def min_spread_cents(self) -> Decimal:
        """Get minimum spread threshold in cents."""
        return self.min_spread_threshold * Decimal("100")
