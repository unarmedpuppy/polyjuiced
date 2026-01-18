"""Unit tests for InMemoryOrderBook and MarketOrderBook.

Tests cover:
- Price level operations (add, update, remove)
- Best price lookups
- Depth queries
- Spread and midpoint calculations
- Volume weighted average price
- Arbitrage detection
- Incremental updates
- Full snapshots
"""

from decimal import Decimal
from datetime import datetime, timezone

import pytest

from mercury.domain.orderbook import (
    InMemoryOrderBook,
    MarketOrderBook,
    PriceLevel,
    SortedPriceLevels,
)


class TestPriceLevel:
    """Tests for PriceLevel dataclass."""

    def test_create_valid_price_level(self):
        """Test creating a valid price level."""
        level = PriceLevel(price=Decimal("0.50"), size=Decimal("100"))
        assert level.price == Decimal("0.50")
        assert level.size == Decimal("100")
        assert level.order_count == 1

    def test_price_level_with_order_count(self):
        """Test price level with custom order count."""
        level = PriceLevel(price=Decimal("0.45"), size=Decimal("50"), order_count=3)
        assert level.order_count == 3

    def test_price_level_invalid_price_negative(self):
        """Test that negative price raises ValueError."""
        with pytest.raises(ValueError, match="price must be between 0 and 1"):
            PriceLevel(price=Decimal("-0.1"), size=Decimal("100"))

    def test_price_level_invalid_price_above_one(self):
        """Test that price above 1 raises ValueError."""
        with pytest.raises(ValueError, match="price must be between 0 and 1"):
            PriceLevel(price=Decimal("1.5"), size=Decimal("100"))

    def test_price_level_invalid_size_negative(self):
        """Test that negative size raises ValueError."""
        with pytest.raises(ValueError, match="size must be non-negative"):
            PriceLevel(price=Decimal("0.50"), size=Decimal("-1"))

    def test_price_level_zero_price_valid(self):
        """Test that price of 0 is valid."""
        level = PriceLevel(price=Decimal("0"), size=Decimal("100"))
        assert level.price == Decimal("0")

    def test_price_level_one_price_valid(self):
        """Test that price of 1 is valid."""
        level = PriceLevel(price=Decimal("1"), size=Decimal("100"))
        assert level.price == Decimal("1")


class TestSortedPriceLevels:
    """Tests for SortedPriceLevels collection."""

    def test_ascending_order_for_asks(self):
        """Test that ascending mode sorts lowest price first (asks)."""
        levels = SortedPriceLevels(ascending=True)
        levels.update(Decimal("0.55"), Decimal("100"))
        levels.update(Decimal("0.50"), Decimal("200"))
        levels.update(Decimal("0.60"), Decimal("150"))

        assert levels.best_price == Decimal("0.50")
        depth = levels.depth(3)
        assert [l.price for l in depth] == [Decimal("0.50"), Decimal("0.55"), Decimal("0.60")]

    def test_descending_order_for_bids(self):
        """Test that descending mode sorts highest price first (bids)."""
        levels = SortedPriceLevels(ascending=False)
        levels.update(Decimal("0.45"), Decimal("100"))
        levels.update(Decimal("0.50"), Decimal("200"))
        levels.update(Decimal("0.40"), Decimal("150"))

        assert levels.best_price == Decimal("0.50")
        depth = levels.depth(3)
        assert [l.price for l in depth] == [Decimal("0.50"), Decimal("0.45"), Decimal("0.40")]

    def test_update_replaces_existing_level(self):
        """Test that update replaces existing price level."""
        levels = SortedPriceLevels(ascending=True)
        levels.update(Decimal("0.50"), Decimal("100"))
        levels.update(Decimal("0.50"), Decimal("200"))

        assert len(levels) == 1
        assert levels.best_size == Decimal("200")

    def test_update_with_zero_size_removes_level(self):
        """Test that updating with zero size removes the level."""
        levels = SortedPriceLevels(ascending=True)
        levels.update(Decimal("0.50"), Decimal("100"))
        levels.update(Decimal("0.50"), Decimal("0"))

        assert len(levels) == 0
        assert levels.best_price is None

    def test_remove_existing_level(self):
        """Test removing an existing level."""
        levels = SortedPriceLevels(ascending=True)
        levels.update(Decimal("0.50"), Decimal("100"))

        assert levels.remove(Decimal("0.50")) is True
        assert len(levels) == 0

    def test_remove_nonexistent_level(self):
        """Test removing a nonexistent level returns False."""
        levels = SortedPriceLevels(ascending=True)

        assert levels.remove(Decimal("0.50")) is False

    def test_clear_removes_all_levels(self):
        """Test that clear removes all levels."""
        levels = SortedPriceLevels(ascending=True)
        levels.update(Decimal("0.50"), Decimal("100"))
        levels.update(Decimal("0.55"), Decimal("200"))

        levels.clear()

        assert len(levels) == 0
        assert levels.best_price is None

    def test_get_existing_level(self):
        """Test getting an existing level."""
        levels = SortedPriceLevels(ascending=True)
        levels.update(Decimal("0.50"), Decimal("100"))

        level = levels.get(Decimal("0.50"))
        assert level is not None
        assert level.size == Decimal("100")

    def test_get_nonexistent_level(self):
        """Test getting a nonexistent level returns None."""
        levels = SortedPriceLevels(ascending=True)

        assert levels.get(Decimal("0.50")) is None

    def test_total_size_all_levels(self):
        """Test total size calculation for all levels."""
        levels = SortedPriceLevels(ascending=True)
        levels.update(Decimal("0.50"), Decimal("100"))
        levels.update(Decimal("0.55"), Decimal("200"))
        levels.update(Decimal("0.60"), Decimal("150"))

        assert levels.total_size() == Decimal("450")

    def test_total_size_limited_levels(self):
        """Test total size calculation for limited levels."""
        levels = SortedPriceLevels(ascending=True)
        levels.update(Decimal("0.50"), Decimal("100"))
        levels.update(Decimal("0.55"), Decimal("200"))
        levels.update(Decimal("0.60"), Decimal("150"))

        assert levels.total_size(2) == Decimal("300")

    def test_empty_levels_properties(self):
        """Test properties on empty collection."""
        levels = SortedPriceLevels(ascending=True)

        assert levels.best is None
        assert levels.best_price is None
        assert levels.best_size == Decimal("0")
        assert len(levels) == 0
        assert not bool(levels)

    def test_iteration(self):
        """Test iterating over levels."""
        levels = SortedPriceLevels(ascending=True)
        levels.update(Decimal("0.50"), Decimal("100"))
        levels.update(Decimal("0.55"), Decimal("200"))

        prices = [level.price for level in levels]
        assert prices == [Decimal("0.50"), Decimal("0.55")]


class TestInMemoryOrderBook:
    """Tests for InMemoryOrderBook."""

    @pytest.fixture
    def empty_book(self):
        """Create an empty order book."""
        return InMemoryOrderBook(token_id="test-token")

    @pytest.fixture
    def populated_book(self):
        """Create an order book with some levels."""
        book = InMemoryOrderBook(token_id="test-token")
        # Add bids (highest first)
        book.update_bid(Decimal("0.45"), Decimal("100"))
        book.update_bid(Decimal("0.44"), Decimal("200"))
        book.update_bid(Decimal("0.43"), Decimal("150"))
        # Add asks (lowest first)
        book.update_ask(Decimal("0.55"), Decimal("100"))
        book.update_ask(Decimal("0.56"), Decimal("200"))
        book.update_ask(Decimal("0.57"), Decimal("150"))
        return book

    def test_empty_book_properties(self, empty_book):
        """Test properties on empty book."""
        assert empty_book.best_bid is None
        assert empty_book.best_ask is None
        assert empty_book.midpoint is None
        assert empty_book.spread is None
        assert empty_book.is_empty

    def test_update_bid(self, empty_book):
        """Test updating a bid level."""
        empty_book.update_bid(Decimal("0.45"), Decimal("100"))

        assert empty_book.best_bid == Decimal("0.45")
        assert empty_book.best_bid_size == Decimal("100")
        assert empty_book.sequence == 1

    def test_update_ask(self, empty_book):
        """Test updating an ask level."""
        empty_book.update_ask(Decimal("0.55"), Decimal("100"))

        assert empty_book.best_ask == Decimal("0.55")
        assert empty_book.best_ask_size == Decimal("100")

    def test_best_bid_highest_price(self, populated_book):
        """Test that best bid is the highest price."""
        assert populated_book.best_bid == Decimal("0.45")

    def test_best_ask_lowest_price(self, populated_book):
        """Test that best ask is the lowest price."""
        assert populated_book.best_ask == Decimal("0.55")

    def test_midpoint_calculation(self, populated_book):
        """Test midpoint calculation."""
        # (0.45 + 0.55) / 2 = 0.50
        assert populated_book.midpoint == Decimal("0.50")

    def test_spread_calculation(self, populated_book):
        """Test spread calculation."""
        # 0.55 - 0.45 = 0.10
        assert populated_book.spread == Decimal("0.10")

    def test_spread_bps_calculation(self, populated_book):
        """Test spread in basis points."""
        # spread / midpoint * 10000 = 0.10 / 0.50 * 10000 = 2000
        assert populated_book.spread_bps == Decimal("2000")

    def test_bid_depth(self, populated_book):
        """Test getting bid depth."""
        depth = populated_book.bid_depth(2)

        assert len(depth) == 2
        assert depth[0].price == Decimal("0.45")
        assert depth[1].price == Decimal("0.44")

    def test_ask_depth(self, populated_book):
        """Test getting ask depth."""
        depth = populated_book.ask_depth(2)

        assert len(depth) == 2
        assert depth[0].price == Decimal("0.55")
        assert depth[1].price == Decimal("0.56")

    def test_total_bid_size(self, populated_book):
        """Test total bid size calculation."""
        assert populated_book.total_bid_size() == Decimal("450")
        assert populated_book.total_bid_size(2) == Decimal("300")

    def test_total_ask_size(self, populated_book):
        """Test total ask size calculation."""
        assert populated_book.total_ask_size() == Decimal("450")
        assert populated_book.total_ask_size(2) == Decimal("300")

    def test_apply_snapshot(self, empty_book):
        """Test applying a full snapshot."""
        bids = [
            (Decimal("0.48"), Decimal("100")),
            (Decimal("0.47"), Decimal("200")),
        ]
        asks = [
            (Decimal("0.52"), Decimal("100")),
            (Decimal("0.53"), Decimal("200")),
        ]

        empty_book.apply_snapshot(bids, asks)

        assert empty_book.best_bid == Decimal("0.48")
        assert empty_book.best_ask == Decimal("0.52")
        assert len(empty_book.bid_depth(10)) == 2
        assert len(empty_book.ask_depth(10)) == 2

    def test_apply_snapshot_clears_existing(self, populated_book):
        """Test that apply_snapshot clears existing levels."""
        bids = [(Decimal("0.40"), Decimal("50"))]
        asks = [(Decimal("0.60"), Decimal("50"))]

        populated_book.apply_snapshot(bids, asks)

        assert len(populated_book.bid_depth(10)) == 1
        assert len(populated_book.ask_depth(10)) == 1
        assert populated_book.best_bid == Decimal("0.40")
        assert populated_book.best_ask == Decimal("0.60")

    def test_apply_delta(self, populated_book):
        """Test applying incremental updates."""
        # Update existing bid and add new ask level
        bid_updates = [(Decimal("0.45"), Decimal("150"))]  # Update size
        ask_updates = [(Decimal("0.54"), Decimal("75"))]   # New level

        populated_book.apply_delta(bid_updates, ask_updates)

        # Existing bid updated
        assert populated_book.bids.get(Decimal("0.45")).size == Decimal("150")
        # New ask added, becomes best
        assert populated_book.best_ask == Decimal("0.54")

    def test_apply_delta_removes_level(self, populated_book):
        """Test that delta with size 0 removes level."""
        populated_book.apply_delta(
            bid_updates=[(Decimal("0.45"), Decimal("0"))],
        )

        assert populated_book.best_bid == Decimal("0.44")

    def test_volume_weighted_bid(self, populated_book):
        """Test VWAP for selling (hitting bids)."""
        # Selling 100 shares hits best bid at 0.45
        vwap = populated_book.volume_weighted_bid(Decimal("100"))
        assert vwap == Decimal("0.45")

        # Selling 200 shares hits 100@0.45 + 100@0.44 = 89 / 200 = 0.445
        vwap = populated_book.volume_weighted_bid(Decimal("200"))
        assert vwap == Decimal("0.445")

    def test_volume_weighted_bid_insufficient_liquidity(self, populated_book):
        """Test VWAP returns None when insufficient liquidity."""
        vwap = populated_book.volume_weighted_bid(Decimal("1000"))
        assert vwap is None

    def test_volume_weighted_ask(self, populated_book):
        """Test VWAP for buying (lifting asks)."""
        # Buying 100 shares lifts best ask at 0.55
        vwap = populated_book.volume_weighted_ask(Decimal("100"))
        assert vwap == Decimal("0.55")

        # Buying 200 shares lifts 100@0.55 + 100@0.56 = 111 / 200 = 0.555
        vwap = populated_book.volume_weighted_ask(Decimal("200"))
        assert vwap == Decimal("0.555")

    def test_volume_weighted_ask_insufficient_liquidity(self, populated_book):
        """Test VWAP returns None when insufficient liquidity."""
        vwap = populated_book.volume_weighted_ask(Decimal("1000"))
        assert vwap is None

    def test_is_crossed_normal(self, populated_book):
        """Test is_crossed returns False for normal book."""
        assert populated_book.is_crossed() is False

    def test_is_crossed_true(self, empty_book):
        """Test is_crossed returns True when bid >= ask."""
        empty_book.update_bid(Decimal("0.55"), Decimal("100"))
        empty_book.update_ask(Decimal("0.50"), Decimal("100"))

        assert empty_book.is_crossed() is True

    def test_to_snapshot(self, populated_book):
        """Test serializing to snapshot dictionary."""
        snapshot = populated_book.to_snapshot(levels=2)

        assert snapshot["token_id"] == "test-token"
        assert snapshot["best_bid"] == "0.45"
        assert snapshot["best_ask"] == "0.55"
        assert snapshot["spread"] == "0.10"
        assert len(snapshot["bid_depth"]) == 2
        assert len(snapshot["ask_depth"]) == 2

    def test_last_update_timestamp(self, empty_book):
        """Test that last_update is updated on changes."""
        initial_time = empty_book.last_update

        empty_book.update_bid(Decimal("0.50"), Decimal("100"))

        assert empty_book.last_update >= initial_time


class TestMarketOrderBook:
    """Tests for MarketOrderBook (combined YES + NO)."""

    @pytest.fixture
    def market_book(self):
        """Create a market order book with data."""
        book = MarketOrderBook.create(
            market_id="test-market",
            yes_token_id="yes-token",
            no_token_id="no-token",
        )
        # YES side: best ask 0.52
        book.yes_book.update_bid(Decimal("0.48"), Decimal("100"))
        book.yes_book.update_ask(Decimal("0.52"), Decimal("100"))
        # NO side: best ask 0.47
        book.no_book.update_bid(Decimal("0.45"), Decimal("100"))
        book.no_book.update_ask(Decimal("0.47"), Decimal("100"))
        return book

    def test_create_factory_method(self):
        """Test the create factory method."""
        book = MarketOrderBook.create(
            market_id="test",
            yes_token_id="yes",
            no_token_id="no",
        )

        assert book.market_id == "test"
        assert book.yes_book.token_id == "yes"
        assert book.no_book.token_id == "no"

    def test_yes_best_prices(self, market_book):
        """Test YES side best prices."""
        assert market_book.yes_best_bid == Decimal("0.48")
        assert market_book.yes_best_ask == Decimal("0.52")

    def test_no_best_prices(self, market_book):
        """Test NO side best prices."""
        assert market_book.no_best_bid == Decimal("0.45")
        assert market_book.no_best_ask == Decimal("0.47")

    def test_combined_ask(self, market_book):
        """Test combined ask calculation."""
        # YES ask (0.52) + NO ask (0.47) = 0.99
        assert market_book.combined_ask == Decimal("0.99")

    def test_combined_bid(self, market_book):
        """Test combined bid calculation."""
        # YES bid (0.48) + NO bid (0.45) = 0.93
        assert market_book.combined_bid == Decimal("0.93")

    def test_arbitrage_spread(self, market_book):
        """Test arbitrage spread calculation."""
        # 1.0 - 0.99 = 0.01
        assert market_book.arbitrage_spread == Decimal("0.01")

    def test_arbitrage_spread_cents(self, market_book):
        """Test arbitrage spread in cents."""
        # 0.01 * 100 = 1 cent
        assert market_book.arbitrage_spread_cents == Decimal("1")

    def test_has_arbitrage_true(self, market_book):
        """Test has_arbitrage returns True when spread > 0."""
        assert market_book.has_arbitrage is True

    def test_has_arbitrage_false(self):
        """Test has_arbitrage returns False when spread <= 0."""
        book = MarketOrderBook.create(
            market_id="test",
            yes_token_id="yes",
            no_token_id="no",
        )
        # Combined ask = 0.60 + 0.50 = 1.10 (no arb)
        book.yes_book.update_ask(Decimal("0.60"), Decimal("100"))
        book.no_book.update_ask(Decimal("0.50"), Decimal("100"))

        assert book.has_arbitrage is False

    def test_combined_ask_none_when_missing_side(self):
        """Test combined_ask returns None when one side missing."""
        book = MarketOrderBook.create(
            market_id="test",
            yes_token_id="yes",
            no_token_id="no",
        )
        book.yes_book.update_ask(Decimal("0.52"), Decimal("100"))
        # NO side has no asks

        assert book.combined_ask is None
        assert book.has_arbitrage is False

    def test_to_snapshot(self, market_book):
        """Test serializing to snapshot dictionary."""
        snapshot = market_book.to_snapshot(levels=2)

        assert snapshot["market_id"] == "test-market"
        assert snapshot["yes_best_ask"] == "0.52"
        assert snapshot["no_best_ask"] == "0.47"
        assert snapshot["combined_ask"] == "0.99"
        assert snapshot["has_arbitrage"] is True
        assert "yes_book" in snapshot
        assert "no_book" in snapshot


class TestIncrementalUpdates:
    """Tests for incremental update scenarios."""

    def test_multiple_bid_updates(self):
        """Test multiple incremental bid updates."""
        book = InMemoryOrderBook(token_id="test")

        # Add initial bids
        book.update_bid(Decimal("0.50"), Decimal("100"))
        book.update_bid(Decimal("0.49"), Decimal("100"))

        # Update best bid
        book.update_bid(Decimal("0.50"), Decimal("200"))
        assert book.best_bid_size == Decimal("200")

        # Add new best bid
        book.update_bid(Decimal("0.51"), Decimal("50"))
        assert book.best_bid == Decimal("0.51")

        # Remove old best
        book.update_bid(Decimal("0.51"), Decimal("0"))
        assert book.best_bid == Decimal("0.50")

    def test_price_level_replacement(self):
        """Test that price levels are correctly replaced."""
        book = InMemoryOrderBook(token_id="test")

        book.update_ask(Decimal("0.55"), Decimal("100"), order_count=2)
        book.update_ask(Decimal("0.55"), Decimal("150"), order_count=3)

        level = book.asks.get(Decimal("0.55"))
        assert level.size == Decimal("150")
        assert level.order_count == 3

    def test_sequence_increments(self):
        """Test that sequence number increments on each update."""
        book = InMemoryOrderBook(token_id="test")

        assert book.sequence == 0

        book.update_bid(Decimal("0.50"), Decimal("100"))
        assert book.sequence == 1

        book.update_ask(Decimal("0.55"), Decimal("100"))
        assert book.sequence == 2

        book.apply_snapshot([], [])
        assert book.sequence == 3

        book.apply_delta(bid_updates=[(Decimal("0.45"), Decimal("50"))])
        assert book.sequence == 4
