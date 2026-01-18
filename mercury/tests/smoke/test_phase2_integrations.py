"""
Phase 2 Smoke Test: Integration Layer

Verifies that Phase 2 deliverables work:
- GammaClient can fetch markets
- CLOBClient can be instantiated
- WebSocket handler connects
- Price feeds work
- Polygon client connects

Run: pytest tests/smoke/test_phase2_integrations.py -v
"""
import pytest


class TestPhase2IntegrationLayer:
    """Phase 2 must pass ALL these tests to be considered complete."""

    def test_gamma_client_importable(self):
        """Verify GammaClient can be imported."""
        from mercury.integrations.polymarket.gamma import GammaClient
        assert GammaClient is not None

    @pytest.mark.asyncio
    async def test_gamma_client_fetches_markets(self):
        """Verify GammaClient can fetch markets from API."""
        from mercury.integrations.polymarket.gamma import GammaClient

        client = GammaClient()
        markets = await client.get_markets(limit=10)

        assert isinstance(markets, list)
        assert len(markets) > 0

    @pytest.mark.asyncio
    async def test_gamma_client_finds_15min_markets(self):
        """Verify GammaClient can find 15-minute markets."""
        from mercury.integrations.polymarket.gamma import GammaClient

        client = GammaClient()
        markets = await client.find_15min_markets("BTC")

        assert isinstance(markets, list)
        # May be empty if no active markets, but should not error

    def test_clob_client_importable(self):
        """Verify CLOBClient can be imported."""
        from mercury.integrations.polymarket.clob import CLOBClient
        assert CLOBClient is not None

    def test_clob_client_instantiates(self, mock_config):
        """Verify CLOBClient can be instantiated with config."""
        from mercury.integrations.polymarket.clob import CLOBClient

        mock_config.get.side_effect = lambda k, d=None: {
            "polymarket.api_key": "test",
            "polymarket.api_secret": "test",
            "polymarket.passphrase": "test",
        }.get(k, d)

        client = CLOBClient(mock_config)
        assert client is not None

    def test_websocket_handler_importable(self):
        """Verify WebSocket handler can be imported."""
        from mercury.integrations.polymarket.websocket import PolymarketWebSocket
        assert PolymarketWebSocket is not None

    @pytest.mark.asyncio
    async def test_websocket_connects(self, mock_event_bus):
        """Verify WebSocket can connect to Polymarket."""
        from mercury.integrations.polymarket.websocket import PolymarketWebSocket

        ws = PolymarketWebSocket(event_bus=mock_event_bus)
        await ws.connect()

        assert ws.is_connected
        await ws.disconnect()

    def test_polymarket_types_importable(self):
        """Verify Polymarket types can be imported."""
        from mercury.integrations.polymarket.types import (
            PolymarketSettings,
            MarketInfo,
            TokenPair,
        )
        assert PolymarketSettings is not None
        assert MarketInfo is not None

    def test_price_feed_protocol_importable(self):
        """Verify PriceFeed protocol can be imported."""
        from mercury.integrations.price_feeds.base import PriceFeed
        assert PriceFeed is not None

    def test_binance_feed_importable(self):
        """Verify Binance price feed can be imported."""
        from mercury.integrations.price_feeds.binance import BinancePriceFeed
        assert BinancePriceFeed is not None

    @pytest.mark.asyncio
    async def test_binance_feed_gets_price(self):
        """Verify Binance feed can fetch BTC price."""
        from mercury.integrations.price_feeds.binance import BinancePriceFeed

        feed = BinancePriceFeed()
        await feed.connect()

        price = await feed.get_price("BTCUSDT")
        assert price > 0

        await feed.disconnect()

    def test_polygon_client_importable(self):
        """Verify Polygon client can be imported."""
        from mercury.integrations.chain.client import PolygonClient
        assert PolygonClient is not None

    def test_ctf_module_importable(self):
        """Verify CTF redemption module can be imported."""
        from mercury.integrations.chain.ctf import CTFRedemption
        assert CTFRedemption is not None
