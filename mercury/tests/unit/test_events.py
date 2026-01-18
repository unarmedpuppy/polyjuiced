"""
Unit tests for domain event payload dataclasses.

Tests cover:
- OrderBookSnapshotEvent creation and serialization
- TradeEvent creation and validation
- StaleAlert creation with timestamp handling
- FreshAlert creation
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from mercury.domain.events import (
    FreshAlert,
    OrderBookSnapshotEvent,
    StaleAlert,
    TradeEvent,
)


class TestOrderBookSnapshotEvent:
    """Tests for OrderBookSnapshotEvent dataclass."""

    def test_create_with_all_fields(self):
        """Test creating event with all fields populated."""
        event = OrderBookSnapshotEvent(
            market_id="test-market-id",
            timestamp="2024-01-15T10:30:00+00:00",
            yes_best_bid="0.45",
            yes_best_ask="0.55",
            no_best_bid="0.48",
            no_best_ask="0.52",
            combined_ask="1.07",
            arbitrage_spread_cents="-7.00",
            yes_bid_size="100",
            yes_ask_size="150",
            no_bid_size="200",
            no_ask_size="250",
            sequence=42,
        )

        assert event.market_id == "test-market-id"
        assert event.yes_best_bid == "0.45"
        assert event.yes_best_ask == "0.55"
        assert event.no_best_bid == "0.48"
        assert event.no_best_ask == "0.52"
        assert event.combined_ask == "1.07"
        assert event.arbitrage_spread_cents == "-7.00"
        assert event.yes_bid_size == "100"
        assert event.yes_ask_size == "150"
        assert event.no_bid_size == "200"
        assert event.no_ask_size == "250"
        assert event.sequence == 42

    def test_create_with_minimal_fields(self):
        """Test creating event with only required fields."""
        event = OrderBookSnapshotEvent(
            market_id="test-market",
            timestamp="2024-01-15T10:30:00+00:00",
        )

        assert event.market_id == "test-market"
        assert event.timestamp == "2024-01-15T10:30:00+00:00"
        assert event.yes_best_bid is None
        assert event.yes_best_ask is None
        assert event.no_best_bid is None
        assert event.no_best_ask is None
        assert event.combined_ask is None
        assert event.arbitrage_spread_cents is None
        assert event.sequence == 0

    def test_from_market_book_with_all_prices(self):
        """Test creating event from market book data."""
        event = OrderBookSnapshotEvent.from_market_book(
            market_id="test-market",
            yes_best_bid=Decimal("0.45"),
            yes_best_ask=Decimal("0.55"),
            no_best_bid=Decimal("0.48"),
            no_best_ask=Decimal("0.45"),  # Combined = 1.00
            yes_bid_size=Decimal("100"),
            yes_ask_size=Decimal("150"),
            no_bid_size=Decimal("200"),
            no_ask_size=Decimal("250"),
            sequence=10,
        )

        assert event.market_id == "test-market"
        assert event.yes_best_bid == "0.45"
        assert event.yes_best_ask == "0.55"
        assert event.no_best_bid == "0.48"
        assert event.no_best_ask == "0.45"
        assert event.combined_ask == "1.00"
        assert event.arbitrage_spread_cents == "0.00"  # No arbitrage at 1.00
        assert event.yes_bid_size == "100"
        assert event.yes_ask_size == "150"
        assert event.no_bid_size == "200"
        assert event.no_ask_size == "250"
        assert event.sequence == 10
        assert event.timestamp  # Should have a timestamp

    def test_from_market_book_with_arbitrage_opportunity(self):
        """Test creating event when arbitrage opportunity exists."""
        event = OrderBookSnapshotEvent.from_market_book(
            market_id="arb-market",
            yes_best_bid=Decimal("0.45"),
            yes_best_ask=Decimal("0.48"),
            no_best_bid=Decimal("0.50"),
            no_best_ask=Decimal("0.48"),  # Combined = 0.96
        )

        assert event.combined_ask == "0.96"
        assert event.arbitrage_spread_cents == "4.00"  # 4 cents profit

    def test_from_market_book_with_missing_asks(self):
        """Test creating event when asks are missing."""
        event = OrderBookSnapshotEvent.from_market_book(
            market_id="partial-market",
            yes_best_bid=Decimal("0.45"),
            yes_best_ask=None,
            no_best_bid=Decimal("0.48"),
            no_best_ask=Decimal("0.52"),
        )

        assert event.yes_best_ask is None
        assert event.combined_ask is None
        assert event.arbitrage_spread_cents is None

    def test_from_market_book_with_custom_timestamp(self):
        """Test creating event with custom timestamp."""
        custom_time = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = OrderBookSnapshotEvent.from_market_book(
            market_id="test",
            yes_best_bid=Decimal("0.5"),
            yes_best_ask=Decimal("0.5"),
            no_best_bid=Decimal("0.5"),
            no_best_ask=Decimal("0.5"),
            timestamp=custom_time,
        )

        assert event.timestamp == "2024-06-15T12:00:00+00:00"

    def test_frozen_dataclass(self):
        """Test that event is immutable (frozen)."""
        event = OrderBookSnapshotEvent(
            market_id="test",
            timestamp="2024-01-15T10:30:00+00:00",
        )

        with pytest.raises(AttributeError):
            event.market_id = "modified"


class TestTradeEvent:
    """Tests for TradeEvent dataclass."""

    def test_create_buy_trade(self):
        """Test creating a buy trade event."""
        event = TradeEvent.create(
            market_id="test-market",
            token_id="yes-token-123",
            side="buy",
            price=Decimal("0.55"),
            size=Decimal("100"),
            trade_id="trade-001",
        )

        assert event.market_id == "test-market"
        assert event.token_id == "yes-token-123"
        assert event.side == "buy"
        assert event.price == "0.55"
        assert event.size == "100"
        assert event.trade_id == "trade-001"
        assert event.timestamp  # Should have a timestamp

    def test_create_sell_trade(self):
        """Test creating a sell trade event."""
        event = TradeEvent.create(
            market_id="test-market",
            token_id="no-token-456",
            side="sell",
            price=Decimal("0.48"),
            size=Decimal("50"),
        )

        assert event.side == "sell"
        assert event.price == "0.48"
        assert event.size == "50"
        assert event.trade_id is None

    def test_create_with_order_ids(self):
        """Test creating trade with maker/taker order IDs."""
        event = TradeEvent.create(
            market_id="test-market",
            token_id="token",
            side="buy",
            price=Decimal("0.5"),
            size=Decimal("10"),
            maker_order_id="maker-123",
            taker_order_id="taker-456",
        )

        assert event.maker_order_id == "maker-123"
        assert event.taker_order_id == "taker-456"

    def test_create_with_invalid_side_raises(self):
        """Test that invalid side raises ValueError."""
        with pytest.raises(ValueError, match="side must be 'buy' or 'sell'"):
            TradeEvent.create(
                market_id="test",
                token_id="token",
                side="invalid",
                price=Decimal("0.5"),
                size=Decimal("10"),
            )

    def test_create_with_custom_timestamp(self):
        """Test creating trade with custom timestamp."""
        custom_time = datetime(2024, 3, 20, 14, 30, 0, tzinfo=timezone.utc)
        event = TradeEvent.create(
            market_id="test",
            token_id="token",
            side="buy",
            price=Decimal("0.5"),
            size=Decimal("10"),
            timestamp=custom_time,
        )

        assert event.timestamp == "2024-03-20T14:30:00+00:00"

    def test_frozen_dataclass(self):
        """Test that trade event is immutable (frozen)."""
        event = TradeEvent(
            market_id="test",
            timestamp="2024-01-15T10:30:00+00:00",
            token_id="token",
            side="buy",
            price="0.5",
            size="10",
        )

        with pytest.raises(AttributeError):
            event.price = "0.6"


class TestStaleAlert:
    """Tests for StaleAlert dataclass."""

    def test_create_stale_alert(self):
        """Test creating a stale alert."""
        alert = StaleAlert.create(
            market_id="stale-market",
            age_seconds=15.5,
            threshold_seconds=10.0,
        )

        assert alert.market_id == "stale-market"
        assert alert.age_seconds == 15.5
        assert alert.threshold_seconds == 10.0
        assert alert.last_update_timestamp is None
        assert alert.timestamp  # Should have a timestamp

    def test_create_with_last_update_time(self):
        """Test creating alert with last update time."""
        import time
        last_update = time.time() - 20.0  # 20 seconds ago

        alert = StaleAlert.create(
            market_id="test",
            age_seconds=20.0,
            threshold_seconds=10.0,
            last_update_time=last_update,
        )

        assert alert.last_update_timestamp is not None
        # Should be a valid ISO timestamp
        assert "T" in alert.last_update_timestamp
        assert "+" in alert.last_update_timestamp or "Z" in alert.last_update_timestamp

    def test_create_with_zero_last_update_time(self):
        """Test creating alert when last_update_time is 0."""
        alert = StaleAlert.create(
            market_id="never-updated",
            age_seconds=-1,  # Infinite age
            threshold_seconds=10.0,
            last_update_time=0,
        )

        assert alert.last_update_timestamp is None

    def test_create_with_custom_timestamp(self):
        """Test creating alert with custom timestamp."""
        custom_time = datetime(2024, 5, 10, 8, 0, 0, tzinfo=timezone.utc)
        alert = StaleAlert.create(
            market_id="test",
            age_seconds=30.0,
            threshold_seconds=10.0,
            timestamp=custom_time,
        )

        assert alert.timestamp == "2024-05-10T08:00:00+00:00"

    def test_frozen_dataclass(self):
        """Test that stale alert is immutable (frozen)."""
        alert = StaleAlert(
            market_id="test",
            timestamp="2024-01-15T10:30:00+00:00",
            age_seconds=15.0,
            threshold_seconds=10.0,
        )

        with pytest.raises(AttributeError):
            alert.age_seconds = 25.0


class TestFreshAlert:
    """Tests for FreshAlert dataclass."""

    def test_create_fresh_alert(self):
        """Test creating a fresh alert."""
        alert = FreshAlert.create(
            market_id="recovered-market",
            age_seconds=2.5,
        )

        assert alert.market_id == "recovered-market"
        assert alert.age_seconds == 2.5
        assert alert.stale_duration_seconds is None
        assert alert.timestamp  # Should have a timestamp

    def test_create_with_stale_duration(self):
        """Test creating alert with stale duration."""
        alert = FreshAlert.create(
            market_id="test",
            age_seconds=1.0,
            stale_duration_seconds=45.0,
        )

        assert alert.stale_duration_seconds == 45.0

    def test_create_with_custom_timestamp(self):
        """Test creating alert with custom timestamp."""
        custom_time = datetime(2024, 7, 1, 16, 45, 0, tzinfo=timezone.utc)
        alert = FreshAlert.create(
            market_id="test",
            age_seconds=0.5,
            timestamp=custom_time,
        )

        assert alert.timestamp == "2024-07-01T16:45:00+00:00"

    def test_frozen_dataclass(self):
        """Test that fresh alert is immutable (frozen)."""
        alert = FreshAlert(
            market_id="test",
            timestamp="2024-01-15T10:30:00+00:00",
            age_seconds=1.0,
        )

        with pytest.raises(AttributeError):
            alert.market_id = "modified"


class TestEventSerialization:
    """Tests for event serialization compatibility with EventBus."""

    def test_orderbook_snapshot_as_dict(self):
        """Test that OrderBookSnapshotEvent can be converted to dict."""
        from dataclasses import asdict

        event = OrderBookSnapshotEvent.from_market_book(
            market_id="test",
            yes_best_bid=Decimal("0.45"),
            yes_best_ask=Decimal("0.55"),
            no_best_bid=Decimal("0.48"),
            no_best_ask=Decimal("0.52"),
        )

        event_dict = asdict(event)

        assert isinstance(event_dict, dict)
        assert event_dict["market_id"] == "test"
        assert event_dict["yes_best_bid"] == "0.45"
        assert event_dict["yes_best_ask"] == "0.55"

    def test_trade_event_as_dict(self):
        """Test that TradeEvent can be converted to dict."""
        from dataclasses import asdict

        event = TradeEvent.create(
            market_id="test",
            token_id="token",
            side="buy",
            price=Decimal("0.5"),
            size=Decimal("100"),
        )

        event_dict = asdict(event)

        assert isinstance(event_dict, dict)
        assert event_dict["market_id"] == "test"
        assert event_dict["side"] == "buy"
        assert event_dict["price"] == "0.5"

    def test_stale_alert_as_dict(self):
        """Test that StaleAlert can be converted to dict."""
        from dataclasses import asdict

        alert = StaleAlert.create(
            market_id="test",
            age_seconds=15.0,
            threshold_seconds=10.0,
        )

        alert_dict = asdict(alert)

        assert isinstance(alert_dict, dict)
        assert alert_dict["market_id"] == "test"
        assert alert_dict["age_seconds"] == 15.0
        assert alert_dict["threshold_seconds"] == 10.0

    def test_fresh_alert_as_dict(self):
        """Test that FreshAlert can be converted to dict."""
        from dataclasses import asdict

        alert = FreshAlert.create(
            market_id="test",
            age_seconds=1.0,
            stale_duration_seconds=30.0,
        )

        alert_dict = asdict(alert)

        assert isinstance(alert_dict, dict)
        assert alert_dict["market_id"] == "test"
        assert alert_dict["age_seconds"] == 1.0
        assert alert_dict["stale_duration_seconds"] == 30.0
