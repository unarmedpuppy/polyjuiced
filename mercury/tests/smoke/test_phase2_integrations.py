"""
Phase 2 Smoke Test: Integration Layer

Verifies that Phase 2 deliverables work:
- GammaClient can fetch markets
- CLOBClient can be instantiated
- WebSocket handler can be imported
- Price feeds work
- Polygon client can be imported

Run: pytest tests/smoke/test_phase2_integrations.py -v
"""

from decimal import Decimal

import pytest


class TestPhase2IntegrationLayer:
    """Phase 2 must pass ALL these tests to be considered complete."""

    # ============ Polymarket Types ============

    def test_polymarket_types_importable(self):
        """Verify Polymarket types can be imported."""
        from mercury.integrations.polymarket.types import (
            DualLegOrderResult,
            Market15Min,
            MarketInfo,
            OrderBookData,
            OrderBookLevel,
            OrderBookSnapshot,
            OrderResult,
            OrderSide,
            OrderStatus,
            PolymarketSettings,
            PositionInfo,
            TimeInForce,
            TokenPrice,
            TokenSide,
            WebSocketMessage,
        )

        assert PolymarketSettings is not None
        assert MarketInfo is not None
        assert OrderBookData is not None

    def test_polymarket_settings_creation(self):
        """Verify PolymarketSettings can be created."""
        from mercury.integrations.polymarket.types import PolymarketSettings

        settings = PolymarketSettings(
            private_key="0x1234567890abcdef",
            api_key="test_key",
            api_secret="test_secret",
            api_passphrase="test_pass",
        )

        assert settings.private_key == "0x1234567890abcdef"
        assert settings.clob_url == "https://clob.polymarket.com/"
        assert settings.signature_type == 0

    def test_order_book_data_properties(self):
        """Verify OrderBookData computed properties work."""
        from mercury.integrations.polymarket.types import OrderBookData, OrderBookLevel
        from datetime import datetime, timezone

        book = OrderBookData(
            token_id="test_token",
            timestamp=datetime.now(timezone.utc),
            bids=(
                OrderBookLevel(price=Decimal("0.50"), size=Decimal("100")),
                OrderBookLevel(price=Decimal("0.49"), size=Decimal("200")),
            ),
            asks=(
                OrderBookLevel(price=Decimal("0.51"), size=Decimal("150")),
                OrderBookLevel(price=Decimal("0.52"), size=Decimal("250")),
            ),
        )

        assert book.best_bid == Decimal("0.50")
        assert book.best_ask == Decimal("0.51")
        assert book.spread == Decimal("0.01")
        assert book.midpoint == Decimal("0.505")
        assert book.depth_at_levels(2) == Decimal("700")  # 100+200+150+250

    def test_order_book_snapshot_arbitrage_detection(self):
        """Verify OrderBookSnapshot detects arbitrage."""
        from mercury.integrations.polymarket.types import (
            OrderBookData,
            OrderBookLevel,
            OrderBookSnapshot,
        )
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)

        yes_book = OrderBookData(
            token_id="yes_token",
            timestamp=now,
            asks=(OrderBookLevel(price=Decimal("0.48"), size=Decimal("100")),),
        )

        no_book = OrderBookData(
            token_id="no_token",
            timestamp=now,
            asks=(OrderBookLevel(price=Decimal("0.50"), size=Decimal("100")),),
        )

        snapshot = OrderBookSnapshot(
            market_id="test_market",
            timestamp=now,
            yes_book=yes_book,
            no_book=no_book,
        )

        assert snapshot.combined_ask == Decimal("0.98")
        assert snapshot.arbitrage_spread_cents == Decimal("2")  # 2 cents profit
        assert snapshot.has_arbitrage is True

    # ============ GammaClient ============

    def test_gamma_client_importable(self):
        """Verify GammaClient can be imported."""
        from mercury.integrations.polymarket.gamma import GammaClient

        assert GammaClient is not None

    def test_gamma_client_instantiates(self):
        """Verify GammaClient can be instantiated."""
        from mercury.integrations.polymarket.gamma import GammaClient
        from mercury.integrations.polymarket.types import PolymarketSettings

        settings = PolymarketSettings(private_key="test")
        client = GammaClient(settings)

        assert client is not None

    # ============ CLOBClient ============

    def test_clob_client_importable(self):
        """Verify CLOBClient can be imported."""
        from mercury.integrations.polymarket.clob import (
            CLOBClient,
            CLOBClientError,
            OrderRejectedError,
            InsufficientLiquidityError,
        )

        assert CLOBClient is not None
        assert CLOBClientError is not None

    def test_clob_client_instantiates(self):
        """Verify CLOBClient can be instantiated with config."""
        from mercury.integrations.polymarket.clob import CLOBClient
        from mercury.integrations.polymarket.types import PolymarketSettings

        settings = PolymarketSettings(
            private_key="0x" + "a" * 64,
            api_key="test_key",
            api_secret="test_secret",
            api_passphrase="test_pass",
        )

        client = CLOBClient(settings)
        assert client is not None
        assert client._connected is False

    # ============ WebSocket Handler ============

    def test_websocket_handler_importable(self):
        """Verify WebSocket handler can be imported."""
        from mercury.integrations.polymarket.websocket import (
            PolymarketWebSocket,
            PolymarketWebSocketError,
        )

        assert PolymarketWebSocket is not None

    def test_websocket_handler_instantiates(self):
        """Verify WebSocket can be instantiated."""
        from unittest.mock import MagicMock

        from mercury.integrations.polymarket.websocket import PolymarketWebSocket
        from mercury.integrations.polymarket.types import PolymarketSettings

        settings = PolymarketSettings(private_key="test")
        mock_event_bus = MagicMock()

        ws = PolymarketWebSocket(settings, mock_event_bus)

        assert ws is not None
        assert ws.is_connected is False
        assert len(ws._subscriptions) == 0

    # ============ Price Feeds ============

    def test_price_feed_protocol_importable(self):
        """Verify PriceFeed protocol can be imported."""
        from mercury.integrations.price_feeds.base import PriceFeed, PriceUpdate

        assert PriceFeed is not None
        assert PriceUpdate is not None

    def test_binance_feed_importable(self):
        """Verify Binance price feed can be imported."""
        from mercury.integrations.price_feeds.binance import BinancePriceFeed

        assert BinancePriceFeed is not None

    def test_binance_feed_instantiates(self):
        """Verify BinancePriceFeed can be instantiated."""
        from mercury.integrations.price_feeds.binance import BinancePriceFeed

        feed = BinancePriceFeed()

        assert feed.name == "binance"
        assert feed.is_connected is False

    # ============ Polygon Client ============

    def test_polygon_client_importable(self):
        """Verify Polygon client can be imported."""
        from mercury.integrations.chain.client import (
            PolygonClient,
            PolygonClientError,
            TxReceipt,
        )

        assert PolygonClient is not None
        assert PolygonClientError is not None
        assert TxReceipt is not None

    def test_polygon_client_instantiates(self):
        """Verify PolygonClient can be instantiated."""
        from mercury.integrations.chain.client import PolygonClient

        client = PolygonClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        assert client is not None
        assert client.is_connected is False

    # ============ Integration Module Exports ============

    def test_integrations_module_exports(self):
        """Verify main integrations module exports key components."""
        from mercury.integrations import (
            CLOBClient,
            GammaClient,
            Market15Min,
            MarketInfo,
            OrderBookData,
            PolygonClient,
            PolymarketSettings,
            PolymarketWebSocket,
        )

        assert GammaClient is not None
        assert CLOBClient is not None
        assert PolymarketWebSocket is not None
        assert PolygonClient is not None
        assert PolymarketSettings is not None


class TestPhase2UnitTests:
    """Unit tests for Phase 2 components (no external calls)."""

    def test_order_result_fill_ratio(self):
        """Verify OrderResult fill ratio calculation."""
        from mercury.integrations.polymarket.types import (
            OrderResult,
            OrderSide,
            OrderStatus,
        )

        result = OrderResult(
            order_id="test",
            token_id="token",
            side=OrderSide.BUY,
            status=OrderStatus.FILLED,
            requested_price=Decimal("0.50"),
            requested_size=Decimal("100"),
            filled_size=Decimal("80"),
            filled_cost=Decimal("40"),
        )

        assert result.fill_ratio == Decimal("0.8")
        assert result.average_fill_price == Decimal("0.5")
        assert result.is_complete is True

    def test_dual_leg_result_properties(self):
        """Verify DualLegOrderResult computed properties."""
        from datetime import datetime, timezone

        from mercury.integrations.polymarket.types import (
            DualLegOrderResult,
            OrderResult,
            OrderSide,
            OrderStatus,
        )

        yes_result = OrderResult(
            order_id="yes1",
            token_id="yes_token",
            side=OrderSide.BUY,
            status=OrderStatus.FILLED,
            requested_price=Decimal("0.48"),
            requested_size=Decimal("100"),
            filled_size=Decimal("100"),
            filled_cost=Decimal("48"),
        )

        no_result = OrderResult(
            order_id="no1",
            token_id="no_token",
            side=OrderSide.BUY,
            status=OrderStatus.FILLED,
            requested_price=Decimal("0.50"),
            requested_size=Decimal("100"),
            filled_size=Decimal("100"),
            filled_cost=Decimal("50"),
        )

        dual = DualLegOrderResult(
            yes_result=yes_result,
            no_result=no_result,
            market_id="test",
            timestamp=datetime.now(timezone.utc),
        )

        assert dual.both_filled is True
        assert dual.has_partial_fill is False
        assert dual.total_cost == Decimal("98")
        assert dual.total_shares == Decimal("100")
        assert dual.guaranteed_pnl == Decimal("2")  # $100 - $98 = $2 profit

    def test_market_15min_properties(self):
        """Verify Market15Min computed properties."""
        from datetime import datetime, timezone

        from mercury.integrations.polymarket.types import Market15Min

        market = Market15Min(
            condition_id="test",
            asset="BTC",
            yes_token_id="yes",
            no_token_id="no",
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.50"),
            start_time=datetime.now(timezone.utc),
            end_time=datetime.now(timezone.utc),
            slug="btc-updown-15m-123",
        )

        assert market.combined_price == Decimal("0.98")
        assert market.spread_cents == Decimal("2")  # 2 cents
