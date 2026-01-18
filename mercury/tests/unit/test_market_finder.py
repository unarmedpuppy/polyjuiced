"""
Unit tests for MarketFinder.

Tests cover:
- Market discovery and caching
- Time remaining calculations
- Market tradeability checks
- Asset-specific queries
- Cache management
- MarketDataService integration
"""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from mercury.integrations.polymarket.market_finder import (
    DEFAULT_ASSETS,
    DEFAULT_CACHE_TTL_SECONDS,
    DEFAULT_MIN_TIME_REMAINING_SECONDS,
    MarketFinder,
    MarketFinderCache,
    MarketFinderWithSubscription,
)
from mercury.integrations.polymarket.types import Market15Min


def make_market(
    asset: str = "BTC",
    condition_id: str = "cond-123",
    minutes_remaining: float = 10.0,
    yes_price: Decimal = Decimal("0.50"),
    no_price: Decimal = Decimal("0.50"),
) -> Market15Min:
    """Create a test Market15Min object."""
    now = datetime.now(timezone.utc)
    end_time = now + timedelta(minutes=minutes_remaining)
    start_time = end_time - timedelta(minutes=15)

    return Market15Min(
        condition_id=condition_id,
        asset=asset.upper(),
        yes_token_id=f"yes-{condition_id}",
        no_token_id=f"no-{condition_id}",
        yes_price=yes_price,
        no_price=no_price,
        start_time=start_time,
        end_time=end_time,
        slug=f"{asset.lower()}-updown-15m-{int(end_time.timestamp())}",
    )


@pytest.fixture
def mock_gamma_client():
    """Create a mock GammaClient."""
    client = MagicMock()
    client.find_15min_markets = AsyncMock(return_value=[])
    return client


@pytest.fixture
def market_finder(mock_gamma_client):
    """Create a MarketFinder instance for testing."""
    return MarketFinder(
        gamma_client=mock_gamma_client,
        cache_ttl_seconds=30.0,
        min_time_remaining_seconds=60.0,
    )


class TestMarketFinderCache:
    """Tests for MarketFinderCache."""

    def test_cache_initially_empty(self):
        """Test that cache starts empty."""
        cache = MarketFinderCache()
        assert cache.stats["size"] == 0
        assert cache.stats["hits"] == 0
        assert cache.stats["misses"] == 0

    def test_get_markets_miss_on_empty(self):
        """Test cache miss on empty cache."""
        cache = MarketFinderCache()
        markets, is_fresh = cache.get_markets_for_asset("BTC")
        assert markets == []
        assert is_fresh is False
        assert cache.stats["misses"] == 1

    def test_update_and_get_markets(self):
        """Test updating and retrieving markets."""
        cache = MarketFinderCache()
        market = make_market("BTC", "cond-1")

        cache.update_markets("BTC", [market])
        markets, is_fresh = cache.get_markets_for_asset("BTC")

        assert len(markets) == 1
        assert markets[0].condition_id == "cond-1"
        assert is_fresh is True
        assert cache.stats["hits"] == 1

    def test_cache_separates_assets(self):
        """Test that cache properly separates markets by asset."""
        cache = MarketFinderCache()
        btc_market = make_market("BTC", "btc-cond")
        eth_market = make_market("ETH", "eth-cond")

        cache.update_markets("BTC", [btc_market])
        cache.update_markets("ETH", [eth_market])

        btc_markets, _ = cache.get_markets_for_asset("BTC")
        eth_markets, _ = cache.get_markets_for_asset("ETH")

        assert len(btc_markets) == 1
        assert btc_markets[0].asset == "BTC"
        assert len(eth_markets) == 1
        assert eth_markets[0].asset == "ETH"

    def test_get_all_markets(self):
        """Test getting all cached markets."""
        cache = MarketFinderCache()
        btc_market = make_market("BTC", "btc-cond")
        eth_market = make_market("ETH", "eth-cond")

        cache.update_markets("BTC", [btc_market])
        cache.update_markets("ETH", [eth_market])

        all_markets = cache.get_all_markets()
        assert len(all_markets) == 2

    def test_cleanup_expired_markets(self):
        """Test cleanup of expired markets."""
        cache = MarketFinderCache()
        # Create an expired market
        expired_market = make_market("BTC", "expired", minutes_remaining=-10.0)
        fresh_market = make_market("BTC", "fresh", minutes_remaining=10.0)

        cache.update_markets("BTC", [expired_market, fresh_market])

        # Cleanup with very short threshold to remove expired
        removed = cache.cleanup_expired(max_age_seconds=1)

        assert removed == 1
        remaining = cache.get_all_markets()
        assert len(remaining) == 1
        assert remaining[0].condition_id == "fresh"

    def test_clear_cache(self):
        """Test clearing all cache entries."""
        cache = MarketFinderCache()
        cache.update_markets("BTC", [make_market("BTC", "btc-cond")])
        cache.update_markets("ETH", [make_market("ETH", "eth-cond")])

        cleared = cache.clear()

        assert cleared == 2
        assert cache.stats["size"] == 0

    def test_cache_hit_rate(self):
        """Test cache hit rate calculation."""
        cache = MarketFinderCache()
        cache.update_markets("BTC", [make_market("BTC")])

        # 3 hits
        cache.get_markets_for_asset("BTC")
        cache.get_markets_for_asset("BTC")
        cache.get_markets_for_asset("BTC")

        # 1 miss (ETH not cached)
        cache.get_markets_for_asset("ETH")

        assert cache.stats["hits"] == 3
        assert cache.stats["misses"] == 1
        assert cache.stats["hit_rate"] == 0.75


class TestMarketFinder:
    """Tests for MarketFinder."""

    def test_initialization(self, market_finder, mock_gamma_client):
        """Test MarketFinder initialization."""
        assert market_finder._gamma == mock_gamma_client
        assert market_finder._min_time_remaining == 60.0

    def test_is_market_tradeable_with_enough_time(self, market_finder):
        """Test that market with enough time is tradeable."""
        market = make_market(minutes_remaining=5.0)  # 5 minutes = 300 seconds
        assert market_finder.is_market_tradeable(market) is True

    def test_is_market_tradeable_below_threshold(self, market_finder):
        """Test that market below threshold is not tradeable."""
        market = make_market(minutes_remaining=0.5)  # 30 seconds
        assert market_finder.is_market_tradeable(market) is False

    def test_is_market_tradeable_expired(self, market_finder):
        """Test that expired market is not tradeable."""
        market = make_market(minutes_remaining=-5.0)
        assert market_finder.is_market_tradeable(market) is False

    def test_get_time_remaining(self, market_finder):
        """Test time remaining calculation."""
        market = make_market(minutes_remaining=10.0)
        remaining = market_finder.get_time_remaining(market)
        # Should be around 600 seconds (10 minutes)
        assert 590 < remaining < 610

    def test_get_time_remaining_negative_for_expired(self, market_finder):
        """Test negative time remaining for expired market."""
        market = make_market(minutes_remaining=-5.0)
        remaining = market_finder.get_time_remaining(market)
        assert remaining < 0

    @pytest.mark.asyncio
    async def test_find_active_markets_returns_tradeable_only(
        self, market_finder, mock_gamma_client
    ):
        """Test that find_active_markets only returns tradeable markets."""
        tradeable = make_market("BTC", "tradeable", minutes_remaining=10.0)
        not_tradeable = make_market("BTC", "not-tradeable", minutes_remaining=0.5)

        mock_gamma_client.find_15min_markets.return_value = [tradeable, not_tradeable]

        markets = await market_finder.find_active_markets(assets=("BTC",))

        assert len(markets) == 1
        assert markets[0].condition_id == "tradeable"

    @pytest.mark.asyncio
    async def test_find_active_markets_multiple_assets(
        self, market_finder, mock_gamma_client
    ):
        """Test finding markets across multiple assets."""
        btc_market = make_market("BTC", "btc-1")
        eth_market = make_market("ETH", "eth-1")

        async def mock_find(asset):
            if asset == "BTC":
                return [btc_market]
            elif asset == "ETH":
                return [eth_market]
            return []

        mock_gamma_client.find_15min_markets.side_effect = mock_find

        markets = await market_finder.find_active_markets(assets=("BTC", "ETH"))

        assert len(markets) == 2
        assets = {m.asset for m in markets}
        assert assets == {"BTC", "ETH"}

    @pytest.mark.asyncio
    async def test_find_active_markets_uses_cache(
        self, market_finder, mock_gamma_client
    ):
        """Test that repeated calls use cache."""
        mock_gamma_client.find_15min_markets.return_value = [
            make_market("BTC", "btc-1")
        ]

        # First call - should hit API
        await market_finder.find_active_markets(assets=("BTC",))
        assert mock_gamma_client.find_15min_markets.call_count == 1

        # Second call - should use cache
        await market_finder.find_active_markets(assets=("BTC",))
        assert mock_gamma_client.find_15min_markets.call_count == 1

    @pytest.mark.asyncio
    async def test_find_active_markets_force_refresh(
        self, market_finder, mock_gamma_client
    ):
        """Test force refresh bypasses cache."""
        mock_gamma_client.find_15min_markets.return_value = [
            make_market("BTC", "btc-1")
        ]

        await market_finder.find_active_markets(assets=("BTC",))
        await market_finder.find_active_markets(assets=("BTC",), force_refresh=True)

        assert mock_gamma_client.find_15min_markets.call_count == 2

    @pytest.mark.asyncio
    async def test_get_next_market(self, market_finder, mock_gamma_client):
        """Test getting next market to trade."""
        earlier = make_market("BTC", "earlier", minutes_remaining=5.0)
        later = make_market("BTC", "later", minutes_remaining=10.0)

        mock_gamma_client.find_15min_markets.return_value = [later, earlier]

        next_market = await market_finder.get_next_market("BTC")

        assert next_market is not None
        assert next_market.condition_id == "earlier"

    @pytest.mark.asyncio
    async def test_get_next_market_returns_none_when_empty(
        self, market_finder, mock_gamma_client
    ):
        """Test get_next_market returns None when no markets available."""
        mock_gamma_client.find_15min_markets.return_value = []

        result = await market_finder.get_next_market("BTC")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_current_market(self, market_finder, mock_gamma_client):
        """Test getting currently active market."""
        # Market that started 5 minutes ago, ends in 10 minutes
        current = make_market("BTC", "current", minutes_remaining=10.0)

        mock_gamma_client.find_15min_markets.return_value = [current]

        result = await market_finder.get_current_market("BTC")

        # Since make_market creates a 15-minute market and we're 5 minutes before end,
        # we should be within the market window
        assert result is not None
        assert result.condition_id == "current"

    @pytest.mark.asyncio
    async def test_get_markets_by_asset(self, market_finder, mock_gamma_client):
        """Test grouping markets by asset."""
        async def mock_find(asset):
            return [make_market(asset, f"{asset.lower()}-1")]

        mock_gamma_client.find_15min_markets.side_effect = mock_find

        result = await market_finder.get_markets_by_asset()

        assert "BTC" in result
        assert "ETH" in result
        assert "SOL" in result

    def test_get_all_discovered_markets(self, market_finder):
        """Test getting all discovered markets from cache."""
        # Populate cache directly for testing
        market = make_market("BTC")
        market_finder._cache.update_markets("BTC", [market])

        markets = market_finder.get_all_discovered_markets()
        assert len(markets) == 1

    def test_get_all_discovered_markets_excludes_expired(self, market_finder):
        """Test that get_all_discovered_markets excludes expired by default."""
        fresh = make_market("BTC", "fresh", minutes_remaining=5.0)
        expired = make_market("BTC", "expired", minutes_remaining=-5.0)

        market_finder._cache.update_markets("BTC", [fresh, expired])

        markets = market_finder.get_all_discovered_markets(include_expired=False)
        assert len(markets) == 1
        assert markets[0].condition_id == "fresh"

    def test_get_all_discovered_markets_includes_expired(self, market_finder):
        """Test that get_all_discovered_markets can include expired."""
        fresh = make_market("BTC", "fresh", minutes_remaining=5.0)
        expired = make_market("BTC", "expired", minutes_remaining=-5.0)

        market_finder._cache.update_markets("BTC", [fresh, expired])

        markets = market_finder.get_all_discovered_markets(include_expired=True)
        assert len(markets) == 2

    def test_cleanup(self, market_finder):
        """Test cleanup removes old expired markets."""
        fresh = make_market("BTC", "fresh", minutes_remaining=5.0)
        expired = make_market("BTC", "expired", minutes_remaining=-10.0)

        market_finder._cache.update_markets("BTC", [fresh, expired])
        removed = market_finder.cleanup()

        assert removed == 1

    def test_invalidate_cache_all(self, market_finder):
        """Test invalidating all cache."""
        market_finder._cache.update_markets("BTC", [make_market("BTC", "btc-cond")])
        market_finder._cache.update_markets("ETH", [make_market("ETH", "eth-cond")])

        invalidated = market_finder.invalidate_cache()

        assert invalidated == 2
        assert market_finder._cache.stats["size"] == 0

    def test_invalidate_cache_specific_asset(self, market_finder):
        """Test invalidating cache for specific asset."""
        market_finder._cache.update_markets("BTC", [make_market("BTC")])

        invalidated = market_finder.invalidate_cache("BTC")

        assert invalidated == 1

    def test_cache_stats(self, market_finder):
        """Test cache statistics access."""
        stats = market_finder.cache_stats
        assert "size" in stats
        assert "hits" in stats
        assert "misses" in stats
        assert "hit_rate" in stats

    @pytest.mark.asyncio
    async def test_api_error_returns_stale_cache(
        self, market_finder, mock_gamma_client
    ):
        """Test that API errors fall back to stale cache."""
        # First successful call
        mock_gamma_client.find_15min_markets.return_value = [
            make_market("BTC", "cached")
        ]
        await market_finder.find_active_markets(assets=("BTC",))

        # Force cache to be stale
        market_finder._cache._last_refresh["BTC"] = 0

        # Second call fails
        mock_gamma_client.find_15min_markets.side_effect = Exception("API Error")

        markets = await market_finder.find_active_markets(assets=("BTC",))

        # Should return stale cache
        assert len(markets) == 1
        assert markets[0].condition_id == "cached"


class TestMarketFinderWithSubscription:
    """Tests for MarketFinderWithSubscription."""

    @pytest.fixture
    def mock_market_data_service(self):
        """Create a mock MarketDataService."""
        service = MagicMock()
        service.subscribe_market = AsyncMock()
        service.unsubscribe_market = AsyncMock()
        return service

    @pytest.fixture
    def finder_with_subscription(self, mock_gamma_client, mock_market_data_service):
        """Create a MarketFinderWithSubscription instance."""
        return MarketFinderWithSubscription(
            gamma_client=mock_gamma_client,
            market_data_service=mock_market_data_service,
            auto_subscribe=True,
        )

    @pytest.mark.asyncio
    async def test_auto_subscribes_to_new_markets(
        self, finder_with_subscription, mock_gamma_client, mock_market_data_service
    ):
        """Test that new markets are automatically subscribed."""
        market = make_market("BTC", "new-market")
        mock_gamma_client.find_15min_markets.return_value = [market]

        await finder_with_subscription.find_active_markets(assets=("BTC",))

        mock_market_data_service.subscribe_market.assert_called_once_with(
            market_id="new-market",
            yes_token_id="yes-new-market",
            no_token_id="no-new-market",
        )

    @pytest.mark.asyncio
    async def test_does_not_resubscribe_existing_markets(
        self, finder_with_subscription, mock_gamma_client, mock_market_data_service
    ):
        """Test that already-subscribed markets are not re-subscribed."""
        market = make_market("BTC", "existing")
        mock_gamma_client.find_15min_markets.return_value = [market]

        await finder_with_subscription.find_active_markets(assets=("BTC",))
        await finder_with_subscription.find_active_markets(
            assets=("BTC",), force_refresh=True
        )

        assert mock_market_data_service.subscribe_market.call_count == 1

    @pytest.mark.asyncio
    async def test_subscribed_count(
        self, finder_with_subscription, mock_gamma_client, mock_market_data_service
    ):
        """Test subscribed count tracking."""
        mock_gamma_client.find_15min_markets.return_value = [
            make_market("BTC", "m1"),
            make_market("BTC", "m2"),
        ]

        await finder_with_subscription.find_active_markets(assets=("BTC",))

        assert finder_with_subscription.subscribed_count == 2

    @pytest.mark.asyncio
    async def test_cleanup_subscriptions(
        self, finder_with_subscription, mock_gamma_client, mock_market_data_service
    ):
        """Test cleanup unsubscribes expired markets."""
        # First, subscribe to a market
        mock_gamma_client.find_15min_markets.return_value = [
            make_market("BTC", "will-expire", minutes_remaining=5.0)
        ]
        await finder_with_subscription.find_active_markets(assets=("BTC",))

        # Now mark the cache as having only expired markets
        finder_with_subscription._cache.update_markets(
            "BTC",
            [make_market("BTC", "will-expire", minutes_remaining=-5.0)]
        )

        removed = await finder_with_subscription.cleanup_subscriptions()

        assert removed == 1
        mock_market_data_service.unsubscribe_market.assert_called_once()

    @pytest.mark.asyncio
    async def test_auto_subscribe_disabled(
        self, mock_gamma_client, mock_market_data_service
    ):
        """Test that auto-subscribe can be disabled."""
        finder = MarketFinderWithSubscription(
            gamma_client=mock_gamma_client,
            market_data_service=mock_market_data_service,
            auto_subscribe=False,
        )

        mock_gamma_client.find_15min_markets.return_value = [
            make_market("BTC", "new-market")
        ]

        await finder.find_active_markets(assets=("BTC",))

        mock_market_data_service.subscribe_market.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_subscription_error(
        self, finder_with_subscription, mock_gamma_client, mock_market_data_service
    ):
        """Test that subscription errors are handled gracefully."""
        mock_market_data_service.subscribe_market.side_effect = Exception("Error")
        mock_gamma_client.find_15min_markets.return_value = [
            make_market("BTC", "error-market")
        ]

        # Should not raise
        markets = await finder_with_subscription.find_active_markets(assets=("BTC",))

        # Markets should still be returned
        assert len(markets) == 1
        # But not marked as subscribed
        assert finder_with_subscription.subscribed_count == 0


class TestMarket15MinProperties:
    """Tests for Market15Min type properties."""

    def test_combined_price(self):
        """Test combined_price calculation."""
        market = make_market(yes_price=Decimal("0.48"), no_price=Decimal("0.48"))
        assert market.combined_price == Decimal("0.96")

    def test_spread_cents(self):
        """Test spread_cents calculation."""
        market = make_market(yes_price=Decimal("0.48"), no_price=Decimal("0.48"))
        # Spread = (1 - 0.96) * 100 = 4 cents
        assert market.spread_cents == Decimal("4")

    def test_spread_cents_no_arbitrage(self):
        """Test spread_cents when no arbitrage (combined > 1)."""
        market = make_market(yes_price=Decimal("0.55"), no_price=Decimal("0.55"))
        # Spread = (1 - 1.10) * 100 = -10 cents (negative = no arb)
        assert market.spread_cents == Decimal("-10")


class TestDefaultConstants:
    """Tests for default configuration constants."""

    def test_default_assets(self):
        """Test default assets include BTC, ETH, SOL."""
        assert "BTC" in DEFAULT_ASSETS
        assert "ETH" in DEFAULT_ASSETS
        assert "SOL" in DEFAULT_ASSETS

    def test_default_cache_ttl(self):
        """Test default cache TTL is reasonable."""
        assert DEFAULT_CACHE_TTL_SECONDS == 30.0

    def test_default_min_time_remaining(self):
        """Test default minimum time remaining is 60 seconds."""
        assert DEFAULT_MIN_TIME_REMAINING_SECONDS == 60
