"""Market finder for discovering 15-minute up/down markets.

This module provides market discovery functionality for finding active
15-minute up/down prediction markets on Polymarket. It focuses on
BTC, ETH, and SOL markets with time-based windows.

Features:
- Discovers active 15-minute markets for specified assets
- Caches results to reduce API calls
- Provides filtering by tradeability and time remaining
- Integrates with MarketDataService for automatic subscription
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, Protocol

import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from mercury.integrations.polymarket.types import Market15Min

log = structlog.get_logger()

# Default configuration
DEFAULT_CACHE_TTL_SECONDS = 30.0  # How often to refresh market list
DEFAULT_STALE_MARKET_THRESHOLD_SECONDS = 300  # Remove markets expired > 5 min ago
DEFAULT_MIN_TIME_REMAINING_SECONDS = 60  # Minimum time to consider market tradeable
DEFAULT_ASSETS = ("BTC", "ETH", "SOL")


class GammaClientProtocol(Protocol):
    """Protocol for GammaClient to allow testing without full implementation."""

    async def find_15min_markets(self, asset: str) -> list[Market15Min]:
        """Find 15-minute markets for an asset."""
        ...


@dataclass
class MarketFinderCache:
    """Cache for discovered markets with TTL management.

    Stores discovered Market15Min objects and tracks cache freshness.
    """

    ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS
    _markets: dict[str, Market15Min] = field(default_factory=dict)
    _last_refresh: dict[str, float] = field(default_factory=dict)  # Per-asset
    _hits: int = 0
    _misses: int = 0

    def get_markets_for_asset(self, asset: str) -> tuple[list[Market15Min], bool]:
        """Get cached markets for an asset.

        Args:
            asset: Asset symbol (e.g., "BTC", "ETH", "SOL").

        Returns:
            Tuple of (markets_list, is_fresh). is_fresh is False if cache expired.
        """
        now = time.monotonic()
        asset_upper = asset.upper()
        last_refresh = self._last_refresh.get(asset_upper, 0)

        # Filter markets for this asset
        markets = [m for m in self._markets.values() if m.asset == asset_upper]

        if now - last_refresh > self.ttl_seconds:
            self._misses += 1
            return (markets, False)

        self._hits += 1
        return (markets, True)

    def update_markets(self, asset: str, markets: list[Market15Min]) -> None:
        """Update cached markets for an asset.

        Args:
            asset: Asset symbol.
            markets: List of markets to cache.
        """
        asset_upper = asset.upper()
        now = time.monotonic()

        # Remove old markets for this asset
        self._markets = {
            cid: m for cid, m in self._markets.items()
            if m.asset != asset_upper
        }

        # Add new markets
        for market in markets:
            self._markets[market.condition_id] = market

        self._last_refresh[asset_upper] = now

    def get_all_markets(self) -> list[Market15Min]:
        """Get all cached markets."""
        return list(self._markets.values())

    def cleanup_expired(self, max_age_seconds: float = DEFAULT_STALE_MARKET_THRESHOLD_SECONDS) -> int:
        """Remove markets that have been expired for too long.

        Args:
            max_age_seconds: Maximum age after end_time to keep in cache.

        Returns:
            Number of markets removed.
        """
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=max_age_seconds)

        expired_ids = [
            cid for cid, m in self._markets.items()
            if m.end_time < cutoff
        ]

        for cid in expired_ids:
            del self._markets[cid]

        return len(expired_ids)

    def clear(self) -> int:
        """Clear all cached markets.

        Returns:
            Number of markets cleared.
        """
        count = len(self._markets)
        self._markets.clear()
        self._last_refresh.clear()
        return count

    @property
    def stats(self) -> dict:
        """Get cache statistics."""
        total = self._hits + self._misses
        return {
            "size": len(self._markets),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": self._hits / total if total > 0 else 0.0,
            "ttl_seconds": self.ttl_seconds,
            "assets_tracked": list(self._last_refresh.keys()),
        }


class MarketFinder:
    """Discovers and tracks active 15-minute up/down markets.

    This class wraps the GammaClient's market discovery functionality with
    additional caching, filtering, and convenience methods. It's designed to
    be used by strategies that trade 15-minute markets.

    Features:
    - Caches discovered markets to reduce API calls
    - Filters markets by tradeability (minimum time remaining)
    - Supports multiple assets (BTC, ETH, SOL)
    - Provides methods to find next/current market for a given asset
    - Can optionally auto-subscribe to MarketDataService
    """

    def __init__(
        self,
        gamma_client: GammaClientProtocol,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        min_time_remaining_seconds: float = DEFAULT_MIN_TIME_REMAINING_SECONDS,
    ):
        """Initialize the market finder.

        Args:
            gamma_client: GammaClient for API access.
            cache_ttl_seconds: How often to refresh market list from API.
            min_time_remaining_seconds: Minimum seconds remaining to consider market tradeable.
        """
        self._gamma = gamma_client
        self._cache = MarketFinderCache(ttl_seconds=cache_ttl_seconds)
        self._min_time_remaining = min_time_remaining_seconds
        self._log = log.bind(component="market_finder")
        self._discovery_lock = asyncio.Lock()

    @property
    def cache_stats(self) -> dict:
        """Get cache statistics for observability."""
        return self._cache.stats

    def is_market_tradeable(self, market: Market15Min) -> bool:
        """Check if a market has enough time remaining to trade.

        Args:
            market: The market to check.

        Returns:
            True if market has more than min_time_remaining_seconds left.
        """
        now = datetime.now(timezone.utc)
        remaining = (market.end_time - now).total_seconds()
        return remaining > self._min_time_remaining

    def get_time_remaining(self, market: Market15Min) -> float:
        """Get seconds remaining until market ends.

        Args:
            market: The market to check.

        Returns:
            Seconds remaining (can be negative if expired).
        """
        now = datetime.now(timezone.utc)
        return (market.end_time - now).total_seconds()

    async def find_active_markets(
        self,
        assets: tuple[str, ...] = DEFAULT_ASSETS,
        force_refresh: bool = False,
    ) -> list[Market15Min]:
        """Find all active 15-minute markets for given assets.

        Args:
            assets: Tuple of asset symbols to search for.
            force_refresh: If True, bypass cache and fetch fresh data.

        Returns:
            List of active (tradeable) Market15Min objects.
        """
        all_markets = []

        for asset in assets:
            markets = await self._find_markets_for_asset(
                asset, force_refresh=force_refresh
            )
            all_markets.extend(markets)

        # Filter to only tradeable markets
        tradeable = [m for m in all_markets if self.is_market_tradeable(m)]

        self._log.debug(
            "find_active_markets",
            total_found=len(all_markets),
            tradeable=len(tradeable),
            assets=assets,
        )

        return tradeable

    async def _find_markets_for_asset(
        self,
        asset: str,
        force_refresh: bool = False,
    ) -> list[Market15Min]:
        """Find markets for a specific asset, using cache when possible.

        Args:
            asset: Asset symbol (BTC, ETH, SOL).
            force_refresh: If True, bypass cache.

        Returns:
            List of Market15Min objects for the asset.
        """
        # Check cache first (unless force refresh)
        if not force_refresh:
            cached_markets, is_fresh = self._cache.get_markets_for_asset(asset)
            if is_fresh:
                return cached_markets

        # Use lock to prevent concurrent API calls for same asset
        async with self._discovery_lock:
            # Check cache again in case another coroutine just refreshed
            if not force_refresh:
                cached_markets, is_fresh = self._cache.get_markets_for_asset(asset)
                if is_fresh:
                    return cached_markets

            # Fetch from API
            try:
                markets = await self._gamma.find_15min_markets(asset)
                self._cache.update_markets(asset, markets)

                self._log.info(
                    "refreshed_markets",
                    asset=asset,
                    count=len(markets),
                )

                return markets

            except Exception as e:
                self._log.error(
                    "market_discovery_failed",
                    asset=asset,
                    error=str(e),
                )
                # Return stale cache on error
                cached_markets, _ = self._cache.get_markets_for_asset(asset)
                return cached_markets

    async def get_next_market(
        self,
        asset: str = "BTC",
    ) -> Optional[Market15Min]:
        """Get the next upcoming market to trade for an asset.

        Returns the tradeable market with the earliest end time.

        Args:
            asset: Asset symbol.

        Returns:
            Next tradeable market, or None if none available.
        """
        markets = await self.find_active_markets(assets=(asset,))

        if not markets:
            return None

        # Get market with earliest end time
        return min(markets, key=lambda m: m.end_time)

    async def get_current_market(
        self,
        asset: str = "BTC",
    ) -> Optional[Market15Min]:
        """Get the currently active market for an asset.

        A market is "current" if we're between its start and end time
        and it's still tradeable.

        Args:
            asset: Asset symbol.

        Returns:
            Currently active market, or None if none active.
        """
        now = datetime.now(timezone.utc)
        markets = await self.find_active_markets(assets=(asset,))

        for market in markets:
            if market.start_time <= now <= market.end_time:
                return market

        return None

    async def get_markets_by_asset(
        self,
        force_refresh: bool = False,
    ) -> dict[str, list[Market15Min]]:
        """Get all markets grouped by asset.

        Args:
            force_refresh: If True, bypass cache.

        Returns:
            Dictionary mapping asset -> list of tradeable markets.
        """
        result: dict[str, list[Market15Min]] = {}

        for asset in DEFAULT_ASSETS:
            markets = await self._find_markets_for_asset(
                asset, force_refresh=force_refresh
            )
            tradeable = [m for m in markets if self.is_market_tradeable(m)]
            if tradeable:
                result[asset] = tradeable

        return result

    def get_all_discovered_markets(
        self,
        include_expired: bool = False,
    ) -> list[Market15Min]:
        """Get all markets currently in cache.

        Useful for dashboard display to show recent market history.

        Args:
            include_expired: If True, include recently expired markets.

        Returns:
            List of cached markets.
        """
        markets = self._cache.get_all_markets()

        if not include_expired:
            now = datetime.now(timezone.utc)
            markets = [m for m in markets if m.end_time > now]

        return markets

    def cleanup(self) -> int:
        """Clean up expired markets from cache.

        Returns:
            Number of markets removed.
        """
        return self._cache.cleanup_expired()

    def invalidate_cache(self, asset: Optional[str] = None) -> int:
        """Invalidate cached data.

        Args:
            asset: If specified, only invalidate for this asset.
                   If None, clear all cache.

        Returns:
            Number of entries invalidated.
        """
        if asset is None:
            return self._cache.clear()

        # For specific asset, we mark it as stale by removing the refresh time
        asset_upper = asset.upper()
        if asset_upper in self._cache._last_refresh:
            del self._cache._last_refresh[asset_upper]
            return 1
        return 0


class MarketFinderWithSubscription(MarketFinder):
    """MarketFinder that automatically subscribes discovered markets to MarketDataService.

    This extension integrates market discovery with the MarketDataService,
    automatically subscribing to WebSocket feeds for discovered markets.
    """

    def __init__(
        self,
        gamma_client: GammaClientProtocol,
        market_data_service: Optional["MarketDataServiceProtocol"] = None,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        min_time_remaining_seconds: float = DEFAULT_MIN_TIME_REMAINING_SECONDS,
        auto_subscribe: bool = True,
    ):
        """Initialize the market finder with MarketDataService integration.

        Args:
            gamma_client: GammaClient for API access.
            market_data_service: MarketDataService for WebSocket subscriptions.
            cache_ttl_seconds: How often to refresh market list.
            min_time_remaining_seconds: Minimum time remaining for tradeable markets.
            auto_subscribe: If True, automatically subscribe to discovered markets.
        """
        super().__init__(
            gamma_client=gamma_client,
            cache_ttl_seconds=cache_ttl_seconds,
            min_time_remaining_seconds=min_time_remaining_seconds,
        )
        self._market_data_service = market_data_service
        self._auto_subscribe = auto_subscribe
        self._subscribed_markets: set[str] = set()

    async def find_active_markets(
        self,
        assets: tuple[str, ...] = DEFAULT_ASSETS,
        force_refresh: bool = False,
    ) -> list[Market15Min]:
        """Find active markets and optionally subscribe to them.

        Args:
            assets: Tuple of asset symbols to search for.
            force_refresh: If True, bypass cache.

        Returns:
            List of active (tradeable) Market15Min objects.
        """
        markets = await super().find_active_markets(
            assets=assets, force_refresh=force_refresh
        )

        # Auto-subscribe to new markets
        if self._auto_subscribe and self._market_data_service is not None:
            await self._subscribe_to_new_markets(markets)

        return markets

    async def _subscribe_to_new_markets(self, markets: list[Market15Min]) -> None:
        """Subscribe to any new markets not yet subscribed.

        Args:
            markets: List of markets to check for subscription.
        """
        for market in markets:
            if market.condition_id not in self._subscribed_markets:
                try:
                    await self._market_data_service.subscribe_market(
                        market_id=market.condition_id,
                        yes_token_id=market.yes_token_id,
                        no_token_id=market.no_token_id,
                    )
                    self._subscribed_markets.add(market.condition_id)
                    self._log.info(
                        "auto_subscribed_market",
                        condition_id=market.condition_id[:16] + "...",
                        asset=market.asset,
                    )
                except Exception as e:
                    self._log.warning(
                        "failed_to_subscribe_market",
                        condition_id=market.condition_id[:16] + "...",
                        error=str(e),
                    )

    async def cleanup_subscriptions(self) -> int:
        """Unsubscribe from markets that are no longer tradeable.

        Returns:
            Number of markets unsubscribed.
        """
        if self._market_data_service is None:
            return 0

        now = datetime.now(timezone.utc)
        all_markets = self._cache.get_all_markets()
        market_ids = {m.condition_id for m in all_markets}

        # Find subscribed markets that are either expired or no longer in cache
        to_unsubscribe = []
        for cid in list(self._subscribed_markets):
            # Check if market is in cache
            market = next((m for m in all_markets if m.condition_id == cid), None)
            if market is None or market.end_time < now:
                to_unsubscribe.append(cid)

        # Unsubscribe
        for cid in to_unsubscribe:
            try:
                await self._market_data_service.unsubscribe_market(cid)
                self._subscribed_markets.discard(cid)
            except Exception as e:
                self._log.warning(
                    "failed_to_unsubscribe_market",
                    condition_id=cid[:16] + "...",
                    error=str(e),
                )

        return len(to_unsubscribe)

    @property
    def subscribed_count(self) -> int:
        """Number of markets currently subscribed."""
        return len(self._subscribed_markets)


class MarketDataServiceProtocol(Protocol):
    """Protocol for MarketDataService for type checking."""

    async def subscribe_market(
        self,
        market_id: str,
        yes_token_id: Optional[str] = None,
        no_token_id: Optional[str] = None,
    ) -> None:
        """Subscribe to a market's data feed."""
        ...

    async def unsubscribe_market(self, market_id: str) -> None:
        """Unsubscribe from a market's data feed."""
        ...
