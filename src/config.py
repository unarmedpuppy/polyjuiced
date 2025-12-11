"""Configuration management for Polymarket Trading Bot."""

import os
from dataclasses import dataclass, field
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings


class PolymarketSettings(BaseSettings):
    """Polymarket API and wallet configuration."""

    # Wallet Configuration
    private_key: str = Field(default="", description="Polygon wallet private key")
    proxy_wallet: str = Field(default="", description="Polymarket proxy wallet address")
    signature_type: int = Field(default=1, description="0=EOA, 1=Magic, 2=Browser")

    # API Credentials (generated from private key)
    api_key: Optional[str] = Field(default=None, description="Polymarket API key")
    api_secret: Optional[str] = Field(default=None, description="Polymarket API secret")
    api_passphrase: Optional[str] = Field(default=None, description="Polymarket API passphrase")

    # Network Configuration
    polygon_rpc_url: str = Field(
        default="https://polygon-rpc.com",
        description="Polygon RPC URL"
    )
    clob_http_url: str = Field(
        default="https://clob.polymarket.com/",
        description="CLOB HTTP API URL"
    )
    clob_ws_url: str = Field(
        default="wss://ws-live-data.polymarket.com",
        description="CLOB WebSocket URL for live data streaming"
    )
    gamma_api_url: str = Field(
        default="https://gamma-api.polymarket.com",
        description="Gamma API URL for market metadata"
    )

    # HTTP Proxy (for routing through VPN)
    http_proxy: Optional[str] = Field(
        default=None,
        description="HTTP proxy URL (e.g., http://gluetun:8888)"
    )

    # Logging
    log_level: str = Field(default="INFO", description="Log level")
    log_json: bool = Field(default=False, description="Enable JSON structured logging")

    model_config = {"env_prefix": "POLYMARKET_"}


@dataclass
class GabagoolConfig:
    """Gabagool arbitrage strategy configuration."""

    # Strategy settings
    enabled: bool = True
    markets: List[str] = field(default_factory=lambda: ["BTC", "ETH"])
    min_spread_threshold: float = 0.02  # 2 cents minimum to trade

    # Position sizing (for $100 capital)
    max_trade_size_usd: float = 5.0  # Per order
    max_per_window_usd: float = 10.0  # Per 15-min market
    max_daily_exposure_usd: float = 90.0  # Keep $10 reserve

    # Risk limits
    max_daily_loss_usd: float = 5.0  # Stop trading for day
    max_unhedged_exposure_usd: float = 10.0  # Trigger hedge
    max_slippage_cents: float = 2.0  # Reject trade

    # Execution
    order_timeout_seconds: float = 0.5
    ws_reconnect_delay_seconds: float = 1.0

    # Mode
    dry_run: bool = False  # LIVE mode enabled

    # Directional trading settings
    directional_enabled: bool = False  # Disabled by default - arb only
    directional_entry_threshold: float = 0.25  # Max price to enter ($0.25)
    directional_time_threshold: float = 0.80  # Min 80% time remaining
    directional_size_ratio: float = 0.33  # 1/3 of arb trade size
    directional_target_base: float = 0.45  # Base take-profit price
    directional_stop_loss: float = 0.11  # Hard stop price
    directional_trailing_activation: float = 0.05  # Trail starts when target - 5¢
    directional_trailing_distance: float = 0.10  # 10¢ trailing stop

    @classmethod
    def from_env(cls) -> "GabagoolConfig":
        """Load configuration from environment variables."""
        return cls(
            enabled=os.getenv("GABAGOOL_ENABLED", "true").lower() == "true",
            markets=os.getenv("GABAGOOL_MARKETS", "BTC,ETH").split(","),
            min_spread_threshold=float(os.getenv("GABAGOOL_MIN_SPREAD", "0.02")),
            max_trade_size_usd=float(os.getenv("GABAGOOL_MAX_TRADE_SIZE", "5.0")),
            max_per_window_usd=float(os.getenv("GABAGOOL_MAX_PER_WINDOW", "10.0")),
            max_daily_exposure_usd=float(os.getenv("GABAGOOL_MAX_DAILY_EXPOSURE", "90.0")),
            max_daily_loss_usd=float(os.getenv("GABAGOOL_MAX_DAILY_LOSS", "5.0")),
            max_unhedged_exposure_usd=float(os.getenv("GABAGOOL_MAX_UNHEDGED", "10.0")),
            max_slippage_cents=float(os.getenv("GABAGOOL_MAX_SLIPPAGE", "2.0")),
            order_timeout_seconds=float(os.getenv("GABAGOOL_ORDER_TIMEOUT", "0.5")),
            ws_reconnect_delay_seconds=float(os.getenv("GABAGOOL_WS_RECONNECT_DELAY", "1.0")),
            dry_run=os.getenv("GABAGOOL_DRY_RUN", "false").lower() == "true",
            # Directional trading
            directional_enabled=os.getenv("GABAGOOL_DIRECTIONAL_ENABLED", "false").lower() == "true",
            directional_entry_threshold=float(os.getenv("GABAGOOL_DIRECTIONAL_ENTRY_THRESHOLD", "0.25")),
            directional_time_threshold=float(os.getenv("GABAGOOL_DIRECTIONAL_TIME_THRESHOLD", "0.80")),
            directional_size_ratio=float(os.getenv("GABAGOOL_DIRECTIONAL_SIZE_RATIO", "0.33")),
            directional_target_base=float(os.getenv("GABAGOOL_DIRECTIONAL_TARGET_BASE", "0.45")),
            directional_stop_loss=float(os.getenv("GABAGOOL_DIRECTIONAL_STOP_LOSS", "0.11")),
            directional_trailing_activation=float(os.getenv("GABAGOOL_DIRECTIONAL_TRAILING_ACTIVATION", "0.05")),
            directional_trailing_distance=float(os.getenv("GABAGOOL_DIRECTIONAL_TRAILING_DISTANCE", "0.10")),
        )


@dataclass
class CopyTradingConfig:
    """Copy trading strategy configuration."""

    enabled: bool = False
    target_wallet: str = ""
    poll_interval_seconds: float = 4.0
    size_multiplier: float = 1.0  # 1.0 = same size as target
    max_position_size_usd: float = 100.0
    min_position_size_usd: float = 5.0

    @classmethod
    def from_env(cls) -> "CopyTradingConfig":
        """Load configuration from environment variables."""
        return cls(
            enabled=os.getenv("COPY_TRADING_ENABLED", "false").lower() == "true",
            target_wallet=os.getenv("TARGET_WALLET_ADDRESS", ""),
            poll_interval_seconds=float(os.getenv("POLL_INTERVAL_SECONDS", "4.0")),
            size_multiplier=float(os.getenv("COPY_SIZE_MULTIPLIER", "1.0")),
            max_position_size_usd=float(os.getenv("COPY_MAX_POSITION_SIZE", "100.0")),
            min_position_size_usd=float(os.getenv("COPY_MIN_POSITION_SIZE", "5.0")),
        )


@dataclass
class AppConfig:
    """Main application configuration."""

    polymarket: PolymarketSettings
    gabagool: GabagoolConfig
    copy_trading: CopyTradingConfig

    @classmethod
    def load(cls) -> "AppConfig":
        """Load all configuration from environment."""
        from dotenv import load_dotenv
        load_dotenv()

        return cls(
            polymarket=PolymarketSettings(),
            gabagool=GabagoolConfig.from_env(),
            copy_trading=CopyTradingConfig.from_env(),
        )


def load_config() -> AppConfig:
    """Convenience function to load configuration."""
    return AppConfig.load()
