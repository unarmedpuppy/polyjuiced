"""Tests for WebSocket client functionality.

Regression tests for:
1. price_change events being properly converted to OrderBookUpdate format
   (Issue: Dashboard not updating real-time because price_change events were ignored)
"""

import pytest
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock

from src.client.websocket import PolymarketWebSocket, OrderBookUpdate


class TestPriceChangeHandling:
    """Regression tests for price_change event handling.

    Issue: Dashboard was not showing real-time price updates because
    the WebSocket client only processed 'book' events (rare full snapshots)
    and ignored 'price_change' events (~100/sec incremental updates).

    Fix: Convert price_change events to OrderBookUpdate format and route
    through the same _on_book_update callback.
    """

    @pytest.fixture
    def ws_client(self):
        """Create a WebSocket client instance."""
        return PolymarketWebSocket()

    def test_handle_price_change_method_exists(self, ws_client):
        """Verify _handle_price_change method exists.

        Regression test: This method must exist to handle price_change events.
        """
        assert hasattr(ws_client, '_handle_price_change')
        assert callable(ws_client._handle_price_change)

    @pytest.mark.asyncio
    async def test_price_change_creates_order_book_update(self, ws_client):
        """Verify price_change events are converted to OrderBookUpdate.

        Regression test: price_change events must be processed and result
        in OrderBookUpdate objects being passed to the callback.
        """
        received_updates = []

        def capture_update(update: OrderBookUpdate):
            received_updates.append(update)

        ws_client.on_book_update(capture_update)

        # Simulate a price_change message from Polymarket
        price_change_data = {
            "event_type": "price_change",
            "market": "0x1234567890abcdef",
            "price_changes": [
                {
                    "asset_id": "12345678901234567890",
                    "best_bid": "0.45",
                    "best_ask": "0.47",
                }
            ]
        }

        await ws_client._handle_price_change(price_change_data)

        # Verify an OrderBookUpdate was created and passed to callback
        assert len(received_updates) == 1
        update = received_updates[0]
        assert isinstance(update, OrderBookUpdate)
        assert update.token_id == "12345678901234567890"
        assert update.best_bid == 0.45
        assert update.best_ask == 0.47
        assert update.midpoint == pytest.approx(0.46, rel=0.01)

    @pytest.mark.asyncio
    async def test_price_change_with_multiple_assets(self, ws_client):
        """Verify multiple price changes in one message are all processed."""
        received_updates = []

        def capture_update(update: OrderBookUpdate):
            received_updates.append(update)

        ws_client.on_book_update(capture_update)

        price_change_data = {
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": "111", "best_bid": "0.30", "best_ask": "0.32"},
                {"asset_id": "222", "best_bid": "0.60", "best_ask": "0.62"},
                {"asset_id": "333", "best_bid": "0.45", "best_ask": "0.47"},
            ]
        }

        await ws_client._handle_price_change(price_change_data)

        # All three updates should be received
        assert len(received_updates) == 3
        token_ids = [u.token_id for u in received_updates]
        assert "111" in token_ids
        assert "222" in token_ids
        assert "333" in token_ids

    @pytest.mark.asyncio
    async def test_price_change_without_callback_does_not_crash(self, ws_client):
        """Verify price_change handling doesn't crash if no callback registered."""
        # No callback registered
        price_change_data = {
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": "12345", "best_bid": "0.45", "best_ask": "0.47"},
            ]
        }

        # Should not raise any exception
        await ws_client._handle_price_change(price_change_data)

    @pytest.mark.asyncio
    async def test_handle_message_routes_price_change(self, ws_client):
        """Verify _handle_message properly routes price_change events.

        Regression test: The main message handler must recognize price_change
        event_type and call _handle_price_change.
        """
        received_updates = []

        def capture_update(update: OrderBookUpdate):
            received_updates.append(update)

        ws_client.on_book_update(capture_update)

        # Simulate the full message handling path
        message_data = {
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": "99999", "best_bid": "0.55", "best_ask": "0.57"},
            ]
        }

        await ws_client._handle_message(message_data)

        # Verify the update was processed
        assert len(received_updates) == 1
        assert received_updates[0].token_id == "99999"

    @pytest.mark.asyncio
    async def test_order_book_update_has_empty_bids_asks_for_price_change(self, ws_client):
        """Verify price_change creates OrderBookUpdate with empty bids/asks.

        price_change events only contain best_bid/best_ask, not the full
        order book. The bids/asks arrays should be empty.
        """
        received_updates = []

        def capture_update(update: OrderBookUpdate):
            received_updates.append(update)

        ws_client.on_book_update(capture_update)

        price_change_data = {
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": "12345", "best_bid": "0.45", "best_ask": "0.47"},
            ]
        }

        await ws_client._handle_price_change(price_change_data)

        update = received_updates[0]
        # price_change doesn't include full book, so bids/asks should be empty
        assert update.bids == []
        assert update.asks == []
        # But best_bid/best_ask should be set
        assert update.best_bid == 0.45
        assert update.best_ask == 0.47


class TestBookEventHandling:
    """Tests for standard book (order book snapshot) event handling."""

    @pytest.fixture
    def ws_client(self):
        """Create a WebSocket client instance."""
        return PolymarketWebSocket()

    @pytest.mark.asyncio
    async def test_book_update_creates_order_book_update(self, ws_client):
        """Verify book events create OrderBookUpdate with full depth."""
        received_updates = []

        def capture_update(update: OrderBookUpdate):
            received_updates.append(update)

        ws_client.on_book_update(capture_update)

        book_data = {
            "event_type": "book",
            "asset_id": "12345678901234567890",
            "bids": [
                {"price": "0.45", "size": "100"},
                {"price": "0.44", "size": "200"},
            ],
            "asks": [
                {"price": "0.47", "size": "150"},
                {"price": "0.48", "size": "250"},
            ],
        }

        await ws_client._handle_book_update(book_data)

        assert len(received_updates) == 1
        update = received_updates[0]
        assert update.token_id == "12345678901234567890"
        assert len(update.bids) == 2
        assert len(update.asks) == 2
        assert update.best_bid == 0.45
        assert update.best_ask == 0.47


class TestMessageTypeRouting:
    """Test that message types are correctly routed to handlers."""

    @pytest.fixture
    def ws_client(self):
        """Create a WebSocket client instance."""
        return PolymarketWebSocket()

    @pytest.mark.asyncio
    async def test_book_event_type_routed_correctly(self, ws_client):
        """Verify 'book' event_type is routed to _handle_book_update."""
        received_updates = []
        ws_client.on_book_update(lambda u: received_updates.append(('book', u)))

        await ws_client._handle_message({
            "event_type": "book",
            "asset_id": "123",
            "bids": [],
            "asks": [],
        })

        assert len(received_updates) == 1
        assert received_updates[0][0] == 'book'

    @pytest.mark.asyncio
    async def test_price_change_event_type_routed_correctly(self, ws_client):
        """Verify 'price_change' event_type is routed to _handle_price_change."""
        received_updates = []
        ws_client.on_book_update(lambda u: received_updates.append(('price', u)))

        await ws_client._handle_message({
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": "456", "best_bid": "0.50", "best_ask": "0.52"},
            ],
        })

        assert len(received_updates) == 1
        assert received_updates[0][0] == 'price'
