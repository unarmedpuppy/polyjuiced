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
    markets: List[str] = field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    min_spread_threshold: float = 0.02  # 2 cents minimum to trade

    # Position sizing
    min_trade_size_usd: float = 3.0  # Minimum trade size per side (skip smaller trades)
    max_trade_size_usd: float = 25.0  # Per order (may be overridden by balance-based sizing)
    max_per_window_usd: float = 50.0  # Per 15-min market
    max_daily_exposure_usd: float = 0.0  # 0 = unlimited (circuit breaker uses max_daily_loss instead)
    balance_sizing_enabled: bool = True  # Scale position size with available balance
    balance_sizing_pct: float = 0.25  # Use up to 25% of available capital per arb trade

    # Risk limits (circuit breaker)
    max_daily_loss_usd: float = 10.0  # Stop trading for day if losses exceed this
    max_unhedged_exposure_usd: float = 10.0  # Trigger hedge
    max_slippage_cents: float = 2.0  # Reject trade

    # Hedge ratio enforcement (Phase 2 - Dec 13, 2025 fix)
    # Arbitrage REQUIRES both legs to fill - without hedge we have directional exposure
    min_hedge_ratio: float = 0.80  # Minimum 80% hedge required for trade to be valid
    critical_hedge_ratio: float = 0.60  # Below this, halt trading (circuit breaker)
    max_position_imbalance_shares: float = 5.0  # Max unhedged shares allowed per position

    # Execution
    order_timeout_seconds: float = 10.0  # Increased - API needs time for tick-size, neg-risk, fee-rate calls
    ws_reconnect_delay_seconds: float = 1.0

    # Phase 2 Strategy (Dec 15, 2025): Gradual position building
    # Split trades into multiple tranches to reduce market impact and get better fills
    gradual_entry_enabled: bool = False  # Disabled by default - single entry
    gradual_entry_tranches: int = 3  # Number of tranches (e.g., 3 means 3 smaller orders)
    gradual_entry_delay_seconds: float = 30.0  # Delay between tranches
    gradual_entry_min_spread_cents: float = 3.0  # Only use gradual for spreads >= 3 cents

    # Phase 3: Better order execution (Dec 13, 2025)
    # Parallel execution places both orders simultaneously for true atomicity
    parallel_execution_enabled: bool = True  # Use parallel order placement
    max_liquidity_consumption_pct: float = 0.50  # Only consume 50% of displayed liquidity
    order_fill_check_interval_ms: float = 100.0  # Check fill status every 100ms
    parallel_fill_timeout_seconds: float = 5.0  # Timeout for both legs to fill in parallel mode

    # Mode
    dry_run: bool = True  # DRY RUN mode - no real trades until hedge enforcement is implemented

    # Directional trading settings
    directional_enabled: bool = False  # Disabled by default - arb only
    directional_entry_threshold: float = 0.25  # Max price to enter ($0.25)
    directional_time_threshold: float = 0.80  # Min 80% time remaining
    directional_size_ratio: float = 0.33  # 1/3 of arb trade size
    directional_target_base: float = 0.45  # Base take-profit price
    directional_stop_loss: float = 0.11  # Hard stop price
    directional_trailing_activation: float = 0.05  # Trail starts when target - 5¢
    directional_trailing_distance: float = 0.10  # 10¢ trailing stop

    # Near-resolution trading settings (high-confidence bets in final minute)
    # DISABLED: Creates one-sided positions that lose money when wrong
    near_resolution_enabled: bool = False  # Disabled - was creating unhedged positions
    near_resolution_time_threshold: float = 60.0  # Max seconds remaining (60s = 1 min)
    near_resolution_min_price: float = 0.94  # Min price to bet (94 cents)
    near_resolution_max_price: float = 0.975  # Max price to bet (97.5 cents)
    near_resolution_size_usd: float = 10.0  # Fixed trade size for near-resolution

    # Server restart blackout window (5:00-5:29 AM CST)
    # The server restarts at 5:15 AM CST daily - we blackout to avoid interrupted trades
    blackout_enabled: bool = True
    blackout_start_hour: int = 5  # 5 AM CST
    blackout_start_minute: int = 0
    blackout_end_hour: int = 5  # 5 AM CST
    blackout_end_minute: int = 29  # 5:29 AM CST
    blackout_timezone: str = "America/Chicago"  # CST/CDT

    @classmethod
    def from_env(cls) -> "GabagoolConfig":
        """Load configuration from environment variables."""
        return cls(
            enabled=os.getenv("GABAGOOL_ENABLED", "true").lower() == "true",
            markets=os.getenv("GABAGOOL_MARKETS", "BTC,ETH,SOL").split(","),
            min_spread_threshold=float(os.getenv("GABAGOOL_MIN_SPREAD", "0.02")),
            min_trade_size_usd=float(os.getenv("GABAGOOL_MIN_TRADE_SIZE", "3.0")),
            max_trade_size_usd=float(os.getenv("GABAGOOL_MAX_TRADE_SIZE", "25.0")),
            max_per_window_usd=float(os.getenv("GABAGOOL_MAX_PER_WINDOW", "50.0")),
            max_daily_exposure_usd=float(os.getenv("GABAGOOL_MAX_DAILY_EXPOSURE", "0.0")),
            balance_sizing_enabled=os.getenv("GABAGOOL_BALANCE_SIZING_ENABLED", "true").lower() == "true",
            balance_sizing_pct=float(os.getenv("GABAGOOL_BALANCE_SIZING_PCT", "0.25")),
            max_daily_loss_usd=float(os.getenv("GABAGOOL_MAX_DAILY_LOSS", "10.0")),
            max_unhedged_exposure_usd=float(os.getenv("GABAGOOL_MAX_UNHEDGED", "10.0")),
            max_slippage_cents=float(os.getenv("GABAGOOL_MAX_SLIPPAGE", "2.0")),
            # Hedge ratio enforcement
            min_hedge_ratio=float(os.getenv("GABAGOOL_MIN_HEDGE_RATIO", "0.80")),
            critical_hedge_ratio=float(os.getenv("GABAGOOL_CRITICAL_HEDGE_RATIO", "0.60")),
            max_position_imbalance_shares=float(os.getenv("GABAGOOL_MAX_POSITION_IMBALANCE", "5.0")),
            order_timeout_seconds=float(os.getenv("GABAGOOL_ORDER_TIMEOUT", "10.0")),
            ws_reconnect_delay_seconds=float(os.getenv("GABAGOOL_WS_RECONNECT_DELAY", "1.0")),
            # Phase 2: Gradual position building
            gradual_entry_enabled=os.getenv("GABAGOOL_GRADUAL_ENTRY_ENABLED", "false").lower() == "true",
            gradual_entry_tranches=int(os.getenv("GABAGOOL_GRADUAL_ENTRY_TRANCHES", "3")),
            gradual_entry_delay_seconds=float(os.getenv("GABAGOOL_GRADUAL_ENTRY_DELAY", "30.0")),
            gradual_entry_min_spread_cents=float(os.getenv("GABAGOOL_GRADUAL_ENTRY_MIN_SPREAD", "3.0")),
            # Phase 3: Better order execution
            parallel_execution_enabled=os.getenv("GABAGOOL_PARALLEL_EXECUTION", "true").lower() == "true",
            max_liquidity_consumption_pct=float(os.getenv("GABAGOOL_MAX_LIQUIDITY_CONSUMPTION", "0.50")),
            order_fill_check_interval_ms=float(os.getenv("GABAGOOL_FILL_CHECK_INTERVAL_MS", "100.0")),
            parallel_fill_timeout_seconds=float(os.getenv("GABAGOOL_PARALLEL_FILL_TIMEOUT", "5.0")),
            dry_run=os.getenv("GABAGOOL_DRY_RUN", "true").lower() == "true",  # Default TRUE until hedge enforcement complete
            # Directional trading
            directional_enabled=os.getenv("GABAGOOL_DIRECTIONAL_ENABLED", "false").lower() == "true",
            directional_entry_threshold=float(os.getenv("GABAGOOL_DIRECTIONAL_ENTRY_THRESHOLD", "0.25")),
            directional_time_threshold=float(os.getenv("GABAGOOL_DIRECTIONAL_TIME_THRESHOLD", "0.80")),
            directional_size_ratio=float(os.getenv("GABAGOOL_DIRECTIONAL_SIZE_RATIO", "0.33")),
            directional_target_base=float(os.getenv("GABAGOOL_DIRECTIONAL_TARGET_BASE", "0.45")),
            directional_stop_loss=float(os.getenv("GABAGOOL_DIRECTIONAL_STOP_LOSS", "0.11")),
            directional_trailing_activation=float(os.getenv("GABAGOOL_DIRECTIONAL_TRAILING_ACTIVATION", "0.05")),
            directional_trailing_distance=float(os.getenv("GABAGOOL_DIRECTIONAL_TRAILING_DISTANCE", "0.10")),
            # Near-resolution trading
            near_resolution_enabled=os.getenv("GABAGOOL_NEAR_RESOLUTION_ENABLED", "false").lower() == "true",  # Disabled - creates unhedged positions
            near_resolution_time_threshold=float(os.getenv("GABAGOOL_NEAR_RESOLUTION_TIME", "60.0")),
            near_resolution_min_price=float(os.getenv("GABAGOOL_NEAR_RESOLUTION_MIN_PRICE", "0.94")),
            near_resolution_max_price=float(os.getenv("GABAGOOL_NEAR_RESOLUTION_MAX_PRICE", "0.975")),
            near_resolution_size_usd=float(os.getenv("GABAGOOL_NEAR_RESOLUTION_SIZE", "10.0")),
            # Server restart blackout window
            blackout_enabled=os.getenv("GABAGOOL_BLACKOUT_ENABLED", "true").lower() == "true",
            blackout_start_hour=int(os.getenv("GABAGOOL_BLACKOUT_START_HOUR", "5")),
            blackout_start_minute=int(os.getenv("GABAGOOL_BLACKOUT_START_MINUTE", "0")),
            blackout_end_hour=int(os.getenv("GABAGOOL_BLACKOUT_END_HOUR", "5")),
            blackout_end_minute=int(os.getenv("GABAGOOL_BLACKOUT_END_MINUTE", "29")),
            blackout_timezone=os.getenv("GABAGOOL_BLACKOUT_TIMEZONE", "America/Chicago"),
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
