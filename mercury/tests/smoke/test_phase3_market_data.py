"""
Phase 3 Smoke Test: Market Data Service

Verifies that Phase 3 deliverables work:
- MarketDataService streams data
- Order book state management works
- Staleness detection works
- MarketFinder discovers markets
- Events are published to EventBus

Run: pytest tests/smoke/test_phase3_market_data.py -v
"""
import pytest
from decimal import Decimal


class TestPhase3MarketDataService:
    """Phase 3 must pass ALL these tests to be considered complete."""

    def test_market_data_service_importable(self):
        """Verify MarketDataService can be imported."""
        from mercury.services.market_data import MarketDataService
        assert MarketDataService is not None

    @pytest.mark.asyncio
    async def test_market_data_service_starts_stops(self, mock_config, mock_event_bus):
        """Verify MarketDataService lifecycle works."""
        from mercury.services.market_data import MarketDataService

        service = MarketDataService(config=mock_config, event_bus=mock_event_bus)
        await service.start()
        assert service.is_running
        await service.stop()
        assert not service.is_running

    @pytest.mark.asyncio
    async def test_market_data_service_subscribes_to_market(self, mock_config, mock_event_bus):
        """Verify can subscribe to market data."""
        from mercury.services.market_data import MarketDataService

        service = MarketDataService(config=mock_config, event_bus=mock_event_bus)
        await service.start()

        await service.subscribe_market("test-market-id")
        assert "test-market-id" in service.subscribed_markets

        await service.stop()

    def test_order_book_state_management(self):
        """Verify order book state can be maintained."""
        from mercury.services.market_data import MarketDataService
        from mercury.domain.market import OrderBook, OrderBookLevel

        service = MarketDataService.__new__(MarketDataService)
        service._order_books = {}

        # Simulate order book update
        book = OrderBook(
            market_id="test",
            yes_bids=[OrderBookLevel(price=Decimal("0.45"), size=Decimal("100"))],
            yes_asks=[OrderBookLevel(price=Decimal("0.50"), size=Decimal("100"))],
            no_bids=[OrderBookLevel(price=Decimal("0.48"), size=Decimal("100"))],
            no_asks=[OrderBookLevel(price=Decimal("0.52"), size=Decimal("100"))],
        )
        service._order_books["test"] = book

        retrieved = service.get_order_book("test")
        assert retrieved is not None
        assert retrieved.yes_bids[0].price == Decimal("0.45")

    def test_get_best_prices(self):
        """Verify best prices can be retrieved."""
        from mercury.services.market_data import MarketDataService
        from mercury.domain.market import OrderBook, OrderBookLevel

        service = MarketDataService.__new__(MarketDataService)
        service._order_books = {}

        book = OrderBook(
            market_id="test",
            yes_bids=[OrderBookLevel(price=Decimal("0.45"), size=Decimal("100"))],
            yes_asks=[OrderBookLevel(price=Decimal("0.50"), size=Decimal("100"))],
            no_bids=[OrderBookLevel(price=Decimal("0.48"), size=Decimal("100"))],
            no_asks=[OrderBookLevel(price=Decimal("0.52"), size=Decimal("100"))],
        )
        service._order_books["test"] = book

        best_bid, best_ask = service.get_best_prices("test")
        assert best_bid == Decimal("0.45")
        assert best_ask == Decimal("0.50")

    @pytest.mark.asyncio
    async def test_staleness_detection(self, mock_config, mock_event_bus):
        """Verify stale market detection works."""
        from mercury.services.market_data import MarketDataService
        import asyncio

        mock_config.get.return_value = 0.1  # 100ms staleness threshold for test

        service = MarketDataService(config=mock_config, event_bus=mock_event_bus)
        service._last_update = {"test": 0}  # Very old timestamp

        await service._check_staleness()

        # Should have published stale event
        mock_event_bus.publish.assert_called()
        call_args = mock_event_bus.publish.call_args
        assert "stale" in call_args[0][0]

    def test_market_finder_importable(self):
        """Verify MarketFinder can be imported."""
        from mercury.integrations.polymarket.market_finder import MarketFinder
        assert MarketFinder is not None

    @pytest.mark.asyncio
    async def test_market_finder_finds_markets(self):
        """Verify MarketFinder can find 15-min markets."""
        from mercury.integrations.polymarket.market_finder import MarketFinder

        finder = MarketFinder()
        markets = await finder.find_active_markets(assets=["BTC", "ETH"])

        assert isinstance(markets, list)

    @pytest.mark.asyncio
    async def test_publishes_orderbook_events(self, mock_config, mock_event_bus):
        """Verify order book updates are published to EventBus."""
        from mercury.services.market_data import MarketDataService
        from mercury.domain.market import OrderBook, OrderBookLevel

        service = MarketDataService(config=mock_config, event_bus=mock_event_bus)

        book = OrderBook(
            market_id="test",
            yes_bids=[OrderBookLevel(price=Decimal("0.45"), size=Decimal("100"))],
            yes_asks=[OrderBookLevel(price=Decimal("0.50"), size=Decimal("100"))],
            no_bids=[],
            no_asks=[],
        )

        await service._publish_order_book(book)

        mock_event_bus.publish.assert_called()
        channel = mock_event_bus.publish.call_args[0][0]
        assert channel == "market.orderbook.test"
