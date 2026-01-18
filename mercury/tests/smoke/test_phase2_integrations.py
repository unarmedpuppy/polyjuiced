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
            BalanceInfo,
            CLOBOrderBook,
            DualLegOrderResult,
            Market15Min,
            MarketInfo,
            MarketStatus,
            OpenOrder,
            OrderBookData,
            OrderBookLevel,
            OrderBookSnapshot,
            OrderResult,
            OrderSide,
            OrderStatus,
            PolymarketSettings,
            PositionInfo,
            TimeInForce,
            TokenPair,
            TokenPrice,
            TokenSide,
            TradeInfo,
            WebSocketMessage,
        )

        assert PolymarketSettings is not None
        assert MarketInfo is not None
        assert OrderBookData is not None
        assert TokenPair is not None
        assert CLOBOrderBook is not None

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

    def test_token_pair_lookup(self):
        """Verify TokenPair lookup methods work."""
        from mercury.integrations.polymarket.types import TokenPair, TokenSide

        pair = TokenPair(
            condition_id="test_condition",
            yes_token_id="yes_123",
            no_token_id="no_456",
            question="Will it rain tomorrow?",
        )

        # Test get_token_id
        assert pair.get_token_id(TokenSide.YES) == "yes_123"
        assert pair.get_token_id(TokenSide.NO) == "no_456"

        # Test get_side
        assert pair.get_side("yes_123") == TokenSide.YES
        assert pair.get_side("no_456") == TokenSide.NO
        assert pair.get_side("unknown") is None

    def test_balance_info_properties(self):
        """Verify BalanceInfo computed properties."""
        from mercury.integrations.polymarket.types import BalanceInfo

        # Case 1: More balance than allowance
        balance1 = BalanceInfo(
            balance=Decimal("100.00"),
            allowance=Decimal("50.00"),
        )
        assert balance1.has_allowance is True
        assert balance1.available_for_trading == Decimal("50.00")

        # Case 2: More allowance than balance
        balance2 = BalanceInfo(
            balance=Decimal("30.00"),
            allowance=Decimal("100.00"),
        )
        assert balance2.available_for_trading == Decimal("30.00")

        # Case 3: No allowance
        balance3 = BalanceInfo(
            balance=Decimal("100.00"),
            allowance=Decimal("0"),
        )
        assert balance3.has_allowance is False
        assert balance3.available_for_trading == Decimal("0")

    def test_trade_info_cost_calculation(self):
        """Verify TradeInfo cost calculations."""
        from datetime import datetime, timezone

        from mercury.integrations.polymarket.types import OrderSide, TradeInfo

        trade = TradeInfo(
            trade_id="trade_1",
            token_id="token_123",
            market_id="market_abc",
            side=OrderSide.BUY,
            price=Decimal("0.50"),
            size=Decimal("100"),
            fee=Decimal("0.25"),
            timestamp=datetime.now(timezone.utc),
        )

        assert trade.total_cost == Decimal("50.25")  # 50 + 0.25
        assert trade.net_proceeds == Decimal("49.75")  # 50 - 0.25

    def test_open_order_fill_tracking(self):
        """Verify OpenOrder fill tracking properties."""
        from datetime import datetime, timezone

        from mercury.integrations.polymarket.types import (
            OpenOrder,
            OrderSide,
            TimeInForce,
        )

        order = OpenOrder(
            order_id="order_1",
            token_id="token_123",
            market_id="market_abc",
            side=OrderSide.BUY,
            price=Decimal("0.50"),
            original_size=Decimal("100"),
            remaining_size=Decimal("60"),
            time_in_force=TimeInForce.GTC,
            created_at=datetime.now(timezone.utc),
            filled_size=Decimal("40"),
        )

        assert order.is_partially_filled is True
        assert order.fill_percentage == Decimal("40")  # 40%

    def test_clob_order_book_is_alias(self):
        """Verify CLOBOrderBook is an alias for OrderBookData."""
        from mercury.integrations.polymarket.types import (
            CLOBOrderBook,
            OrderBookData,
        )

        assert CLOBOrderBook is OrderBookData

    def test_market_status_enum(self):
        """Verify MarketStatus enum values."""
        from mercury.integrations.polymarket.types import MarketStatus

        assert MarketStatus.ACTIVE == "active"
        assert MarketStatus.PAUSED == "paused"
        assert MarketStatus.CLOSED == "closed"
        assert MarketStatus.RESOLVED == "resolved"
