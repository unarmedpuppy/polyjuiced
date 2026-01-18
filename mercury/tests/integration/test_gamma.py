"""
Unit and integration tests for GammaClient.

Run: pytest tests/integration/test_gamma.py -v
"""

import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from mercury.integrations.polymarket.gamma import (
    DEFAULT_CACHE_TTL,
    CacheEntry,
    GammaClient,
    GammaClientError,
    MarketCache,
)
from mercury.integrations.polymarket.types import MarketInfo, PolymarketSettings


# ============ MarketCache Tests ============


class TestMarketCache:
    """Unit tests for the MarketCache class."""

    @pytest.fixture
    def sample_market_info(self) -> MarketInfo:
        """Create a sample MarketInfo for testing."""
        return MarketInfo(
            condition_id="test_condition_123",
            question_id="test_question_123",
            question="Will BTC hit 100k?",
            slug="btc-100k",
            yes_token_id="yes_token_123",
            no_token_id="no_token_123",
            yes_price=Decimal("0.55"),
            no_price=Decimal("0.45"),
            active=True,
            closed=False,
            resolved=False,
            volume=Decimal("1000000"),
            liquidity=Decimal("50000"),
        )

    def test_cache_set_and_get(self, sample_market_info):
        """Test basic set and get operations."""
        cache = MarketCache(ttl_seconds=60.0)
        condition_id = "test_123"

        # Initially empty
        assert cache.get(condition_id) is None
        assert cache.size == 0

        # Set and retrieve
        cache.set(condition_id, sample_market_info)
        assert cache.size == 1

        result = cache.get(condition_id)
        assert result is not None
        assert result.condition_id == sample_market_info.condition_id
        assert result.yes_price == Decimal("0.55")

    def test_cache_expiration(self, sample_market_info):
        """Test that entries expire after TTL."""
        cache = MarketCache(ttl_seconds=0.1)  # 100ms TTL
        condition_id = "test_expiring"

        cache.set(condition_id, sample_market_info)
        assert cache.get(condition_id) is not None

        # Wait for expiration
        time.sleep(0.15)
        assert cache.get(condition_id) is None

    def test_cache_invalidate(self, sample_market_info):
        """Test manual cache invalidation."""
        cache = MarketCache()
        condition_id = "test_invalidate"

        cache.set(condition_id, sample_market_info)
        assert cache.size == 1

        # Invalidate existing entry
        result = cache.invalidate(condition_id)
        assert result is True
        assert cache.size == 0
        assert cache.get(condition_id) is None

        # Invalidate non-existent entry
        result = cache.invalidate("nonexistent")
        assert result is False

    def test_cache_clear(self, sample_market_info):
        """Test clearing all cache entries."""
        cache = MarketCache()

        # Add multiple entries
        for i in range(5):
            market = MarketInfo(
                condition_id=f"cond_{i}",
                question_id=f"q_{i}",
                question=f"Question {i}",
                slug=f"slug-{i}",
                yes_token_id=f"yes_{i}",
                no_token_id=f"no_{i}",
                yes_price=Decimal("0.5"),
                no_price=Decimal("0.5"),
                active=True,
                closed=False,
                resolved=False,
            )
            cache.set(f"cond_{i}", market)

        assert cache.size == 5

        count = cache.clear()
        assert count == 5
        assert cache.size == 0

    def test_cache_cleanup_expired(self, sample_market_info):
        """Test cleanup of expired entries."""
        cache = MarketCache(ttl_seconds=0.1)

        # Add entries
        cache.set("entry1", sample_market_info)
        cache.set("entry2", sample_market_info)

        # Wait for expiration
        time.sleep(0.15)

        # Add fresh entry
        cache.set("entry3", sample_market_info)

        # Cleanup should remove 2 expired entries
        removed = cache.cleanup_expired()
        assert removed == 2
        assert cache.size == 1
        assert cache.get("entry3") is not None

    def test_cache_hit_rate(self, sample_market_info):
        """Test cache hit rate calculation."""
        cache = MarketCache()
        condition_id = "test_hit_rate"

        # Initial hit rate should be 0
        assert cache.hit_rate == 0.0

        # Miss
        cache.get(condition_id)
        assert cache.hit_rate == 0.0

        # Set and hit
        cache.set(condition_id, sample_market_info)
        cache.get(condition_id)

        # 1 hit, 1 miss = 50%
        assert cache.hit_rate == 0.5

        # Another hit
        cache.get(condition_id)
        # 2 hits, 1 miss = 66.67%
        assert abs(cache.hit_rate - 0.6666666) < 0.001

    def test_cache_stats(self, sample_market_info):
        """Test cache statistics."""
        cache = MarketCache(ttl_seconds=30.0)

        cache.set("test1", sample_market_info)
        cache.get("test1")  # hit
        cache.get("test2")  # miss

        stats = cache.stats
        assert stats["size"] == 1
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["hit_rate"] == 0.5
        assert stats["ttl_seconds"] == 30.0


# ============ GammaClient Tests ============


class TestGammaClient:
    """Unit tests for the GammaClient class."""

    @pytest.fixture
    def settings(self) -> PolymarketSettings:
        """Create test settings."""
        return PolymarketSettings(
            private_key="0x" + "a" * 64,
            gamma_url="https://gamma-api.polymarket.com",
        )

    @pytest.fixture
    def client(self, settings) -> GammaClient:
        """Create a GammaClient instance."""
        return GammaClient(settings, cache_ttl=60.0)

    def test_client_initialization(self, settings):
        """Test client initialization with various settings."""
        client = GammaClient(settings)
        assert client._base_url == "https://gamma-api.polymarket.com"
        assert client._timeout == 30.0
        assert client._client is None

        # With custom timeout and cache TTL
        client = GammaClient(settings, timeout=60.0, cache_ttl=120.0)
        assert client._timeout == 60.0
        assert client._cache.ttl_seconds == 120.0

    def test_client_not_connected_error(self, client):
        """Test that methods raise error when not connected."""
        with pytest.raises(GammaClientError, match="Client not connected"):
            client._ensure_connected()

    @pytest.mark.asyncio
    async def test_client_context_manager(self, settings):
        """Test async context manager."""
        async with GammaClient(settings) as client:
            assert client._client is not None

        # After exiting, client should be closed
        assert client._client is None

    @pytest.mark.asyncio
    async def test_client_connect_disconnect(self, client):
        """Test connect and close methods."""
        assert client._client is None

        await client.connect()
        assert client._client is not None

        # Double connect should be safe
        await client.connect()
        assert client._client is not None

        await client.close()
        assert client._client is None

        # Double close should be safe
        await client.close()
        assert client._client is None

    def test_parse_market_info(self, client):
        """Test parsing market data to MarketInfo."""
        data = {
            "conditionId": "0x123abc",
            "questionId": "q_123",
            "question": "Will it happen?",
            "slug": "will-it-happen",
            "clobTokenIds": json.dumps(["token_yes", "token_no"]),
            "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(["0.65", "0.35"]),
            "active": True,
            "closed": False,
            "resolved": False,
            "volume": "50000",
            "liquidity": "10000",
            "endDate": "2025-12-31T23:59:59Z",
            "eventSlug": "2025-predictions",
            "eventTitle": "2025 Predictions",
        }

        market = client.parse_market_info(data)

        assert market.condition_id == "0x123abc"
        assert market.question_id == "q_123"
        assert market.question == "Will it happen?"
        assert market.yes_token_id == "token_yes"
        assert market.no_token_id == "token_no"
        assert market.yes_price == Decimal("0.65")
        assert market.no_price == Decimal("0.35")
        assert market.active is True
        assert market.volume == Decimal("50000")
        assert market.end_date is not None

    def test_parse_market_info_up_down_outcomes(self, client):
        """Test parsing market with Up/Down outcomes (15min markets)."""
        data = {
            "conditionId": "0x456def",
            "questionId": "q_456",
            "question": "Will BTC go up?",
            "slug": "btc-updown-15m-123456",
            "clobTokenIds": json.dumps(["up_token", "down_token"]),
            "outcomes": json.dumps(["Up", "Down"]),
            "outcomePrices": json.dumps(["0.48", "0.52"]),
            "active": True,
            "closed": False,
            "resolved": False,
        }

        market = client.parse_market_info(data)

        assert market.yes_token_id == "up_token"  # "Up" maps to YES
        assert market.no_token_id == "down_token"  # "Down" maps to NO
        assert market.yes_price == Decimal("0.48")
        assert market.no_price == Decimal("0.52")

    def test_cache_stats_property(self, client):
        """Test cache_stats property."""
        stats = client.cache_stats
        assert "size" in stats
        assert "hits" in stats
        assert "misses" in stats
        assert "hit_rate" in stats
        assert "ttl_seconds" in stats

    def test_invalidate_cache(self, client, settings):
        """Test cache invalidation methods."""
        # Create a market and manually add to cache
        market_info = MarketInfo(
            condition_id="test_cond",
            question_id="q_test",
            question="Test?",
            slug="test-slug",
            yes_token_id="yes",
            no_token_id="no",
            yes_price=Decimal("0.5"),
            no_price=Decimal("0.5"),
            active=True,
            closed=False,
            resolved=False,
        )

        client._cache.set("test_cond", market_info)
        assert client._cache.size == 1

        # Invalidate specific entry
        count = client.invalidate_cache("test_cond")
        assert count == 1
        assert client._cache.size == 0

        # Invalidate non-existent
        count = client.invalidate_cache("nonexistent")
        assert count == 0

        # Add multiple and clear all
        client._cache.set("cond1", market_info)
        client._cache.set("cond2", market_info)
        assert client._cache.size == 2

        count = client.invalidate_cache()
        assert count == 2
        assert client._cache.size == 0


class TestGammaClientWithMockedHTTP:
    """Tests that mock HTTP responses."""

    @pytest.fixture
    def settings(self) -> PolymarketSettings:
        return PolymarketSettings(private_key="test")

    @pytest.fixture
    def mock_response(self):
        """Create a mock HTTP response."""
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        return response

    @pytest.mark.asyncio
    async def test_get_market_info_caching(self, settings):
        """Test that get_market_info uses cache."""
        client = GammaClient(settings, cache_ttl=60.0)
        await client.connect()

        market_data = {
            "conditionId": "cached_market",
            "questionId": "q_cached",
            "question": "Cached question?",
            "slug": "cached-question",
            "clobTokenIds": json.dumps(["yes", "no"]),
            "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(["0.6", "0.4"]),
            "active": True,
            "closed": False,
            "resolved": False,
        }

        # Mock the HTTP client
        with patch.object(client._client, "get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = market_data
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            # First call should hit API
            result1 = await client.get_market_info("cached_market")
            assert result1 is not None
            assert result1.condition_id == "cached_market"
            assert mock_get.call_count == 1

            # Second call should use cache
            result2 = await client.get_market_info("cached_market")
            assert result2 is not None
            assert result2.condition_id == "cached_market"
            assert mock_get.call_count == 1  # No additional API call

            # Bypass cache
            result3 = await client.get_market_info("cached_market", use_cache=False)
            assert result3 is not None
            assert mock_get.call_count == 2  # API called again

        await client.close()

    @pytest.mark.asyncio
    async def test_get_market_not_found(self, settings):
        """Test handling of 404 responses."""
        client = GammaClient(settings)
        await client.connect()

        with patch.object(client._client, "get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_get.return_value = mock_response

            result = await client.get_market("nonexistent")
            assert result is None

        await client.close()

    @pytest.mark.asyncio
    async def test_get_markets_list(self, settings):
        """Test fetching markets list."""
        client = GammaClient(settings)
        await client.connect()

        markets_data = [
            {"conditionId": "market1", "question": "Q1"},
            {"conditionId": "market2", "question": "Q2"},
        ]

        with patch.object(client._client, "get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = markets_data
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            result = await client.get_markets(limit=10)
            assert len(result) == 2
            assert result[0]["conditionId"] == "market1"

        await client.close()

    @pytest.mark.asyncio
    async def test_search_markets(self, settings):
        """Test market search."""
        client = GammaClient(settings)
        await client.connect()

        search_results = [
            {"conditionId": "btc_market", "question": "BTC prediction"},
        ]

        with patch.object(client._client, "get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = search_results
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            result = await client.search_markets("BTC")
            assert len(result) == 1
            assert "BTC" in result[0]["question"]

        await client.close()


class TestGammaClientCacheIntegration:
    """Integration tests for cache behavior."""

    @pytest.fixture
    def settings(self) -> PolymarketSettings:
        return PolymarketSettings(private_key="test")

    @pytest.mark.asyncio
    async def test_cache_expiry_triggers_refetch(self, settings):
        """Test that expired cache entries trigger API refetch."""
        client = GammaClient(settings, cache_ttl=0.1)  # 100ms TTL
        await client.connect()

        market_data = {
            "conditionId": "expiring_market",
            "questionId": "q_exp",
            "question": "Expiring?",
            "slug": "expiring",
            "clobTokenIds": json.dumps(["yes", "no"]),
            "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(["0.5", "0.5"]),
            "active": True,
            "closed": False,
            "resolved": False,
        }

        with patch.object(client._client, "get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = market_data
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            # First fetch
            await client.get_market_info("expiring_market")
            assert mock_get.call_count == 1

            # Second fetch (from cache)
            await client.get_market_info("expiring_market")
            assert mock_get.call_count == 1

            # Wait for expiry
            time.sleep(0.15)

            # Third fetch (cache expired, refetch)
            await client.get_market_info("expiring_market")
            assert mock_get.call_count == 2

        await client.close()
