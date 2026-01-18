"""
Unit tests for Gabagool arbitrage strategy.

Tests verify:
- Arbitrage detection logic
- Spread calculation
- Entry criteria validation
- Position size calculation
- Signal generation
- Cooldown behavior
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from mercury.core.config import ConfigManager
from mercury.domain.market import OrderBook, OrderBookLevel
from mercury.domain.signal import SignalPriority, SignalType
from mercury.strategies.gabagool import GabagoolConfig, GabagoolStrategy
from mercury.strategies.gabagool.strategy import ArbitrageOpportunity, ValidationResult


class TestGabagoolConfig:
    """Tests for GabagoolConfig dataclass."""

    def test_default_values(self):
        """Verify default configuration values."""
        config = GabagoolConfig()

        assert config.enabled is True
        assert config.markets == ["BTC", "ETH", "SOL"]
        assert config.min_spread_threshold == Decimal("0.015")
        assert config.max_trade_size_usd == Decimal("25.0")
        assert config.min_time_remaining_seconds == 60
        assert config.balance_sizing_enabled is True
        assert config.min_hedge_ratio == Decimal("0.8")

    def test_min_spread_cents_property(self):
        """Verify min_spread_cents converts correctly."""
        config = GabagoolConfig(min_spread_threshold=Decimal("0.02"))
        assert config.min_spread_cents == Decimal("2.0")

    def test_from_config_manager(self):
        """Verify config loads from ConfigManager."""
        mock_config = MagicMock(spec=ConfigManager)
        mock_config.get_bool.return_value = True
        mock_config.get_list.return_value = ["BTC", "ETH"]
        mock_config.get_decimal.return_value = Decimal("0.02")
        mock_config.get_int.return_value = 90

        config = GabagoolConfig.from_config_manager(mock_config)

        assert config.enabled is True
        assert config.markets == ["BTC", "ETH"]
        assert config.min_spread_threshold == Decimal("0.02")


class TestArbitrageOpportunity:
    """Tests for ArbitrageOpportunity class."""

    def test_is_valid_fresh_opportunity(self):
        """Verify fresh opportunity is valid."""
        opp = ArbitrageOpportunity(
            market_id="test",
            yes_price=Decimal("0.45"),
            no_price=Decimal("0.50"),
            combined_price=Decimal("0.95"),
            spread=Decimal("0.05"),
            spread_cents=Decimal("5.0"),
            profit_percentage=Decimal("5.26"),
            detected_at=datetime.now(timezone.utc),
        )
        assert opp.is_valid is True

    def test_is_valid_stale_opportunity(self):
        """Verify stale opportunity is invalid."""
        old_time = datetime.now(timezone.utc) - timedelta(seconds=60)
        opp = ArbitrageOpportunity(
            market_id="test",
            yes_price=Decimal("0.45"),
            no_price=Decimal("0.50"),
            combined_price=Decimal("0.95"),
            spread=Decimal("0.05"),
            spread_cents=Decimal("5.0"),
            profit_percentage=Decimal("5.26"),
            detected_at=old_time,
        )
        assert opp.is_valid is False

    def test_age_seconds(self):
        """Verify age_seconds calculation."""
        past_time = datetime.now(timezone.utc) - timedelta(seconds=10)
        opp = ArbitrageOpportunity(
            market_id="test",
            yes_price=Decimal("0.45"),
            no_price=Decimal("0.50"),
            combined_price=Decimal("0.95"),
            spread=Decimal("0.05"),
            spread_cents=Decimal("5.0"),
            profit_percentage=Decimal("5.26"),
            detected_at=past_time,
        )
        # Allow 1 second tolerance for test execution time
        assert 9 <= opp.age_seconds <= 12


class TestValidationResult:
    """Tests for ValidationResult class."""

    def test_valid_result(self):
        """Verify valid result creation."""
        result = ValidationResult(is_valid=True)
        assert result.is_valid is True
        assert result.reason == ""

    def test_invalid_result_with_reason(self):
        """Verify invalid result with reason."""
        result = ValidationResult(is_valid=False, reason="Spread too small")
        assert result.is_valid is False
        assert result.reason == "Spread too small"


@pytest.fixture
def mock_config_manager():
    """Create a mock ConfigManager for testing."""
    config = MagicMock(spec=ConfigManager)
    config.get_bool.return_value = True
    config.get_list.return_value = ["BTC", "ETH", "SOL"]
    config.get_decimal.side_effect = lambda key, default=None: {
        "strategies.gabagool.min_spread_threshold": Decimal("0.015"),
        "strategies.gabagool.max_trade_size_usd": Decimal("25.0"),
        "strategies.gabagool.max_per_window_usd": Decimal("50.0"),
        "strategies.gabagool.balance_sizing_pct": Decimal("0.25"),
        "strategies.gabagool.gradual_entry_min_spread_cents": Decimal("3.0"),
        "strategies.gabagool.min_hedge_ratio": Decimal("0.8"),
        "strategies.gabagool.critical_hedge_ratio": Decimal("0.5"),
    }.get(key, default)
    config.get_int.side_effect = lambda key, default=None: {
        "strategies.gabagool.min_time_remaining_seconds": 60,
        "strategies.gabagool.gradual_entry_tranches": 3,
    }.get(key, default)
    return config


@pytest.fixture
def gabagool_strategy(mock_config_manager):
    """Create a GabagoolStrategy instance for testing."""
    return GabagoolStrategy(config=mock_config_manager)


@pytest.fixture
def order_book_with_arbitrage() -> OrderBook:
    """Create an order book with arbitrage opportunity (combined ask < 1)."""
    # YES ask = 0.45, NO ask = 0.50 => combined = 0.95, spread = 5 cents
    return OrderBook(
        market_id="test_market_123",
        yes_asks=[OrderBookLevel(price=Decimal("0.45"), size=Decimal("100"))],
        yes_bids=[OrderBookLevel(price=Decimal("0.44"), size=Decimal("100"))],
        no_asks=[OrderBookLevel(price=Decimal("0.50"), size=Decimal("100"))],
        no_bids=[OrderBookLevel(price=Decimal("0.49"), size=Decimal("100"))],
        timestamp=datetime.utcnow(),
    )


@pytest.fixture
def order_book_small_spread() -> OrderBook:
    """Create an order book with small spread (below threshold)."""
    # YES ask = 0.49, NO ask = 0.50 => combined = 0.99, spread = 1 cent
    return OrderBook(
        market_id="test_market_456",
        yes_asks=[OrderBookLevel(price=Decimal("0.49"), size=Decimal("100"))],
        yes_bids=[OrderBookLevel(price=Decimal("0.48"), size=Decimal("100"))],
        no_asks=[OrderBookLevel(price=Decimal("0.50"), size=Decimal("100"))],
        no_bids=[OrderBookLevel(price=Decimal("0.49"), size=Decimal("100"))],
        timestamp=datetime.utcnow(),
    )


@pytest.fixture
def order_book_no_arbitrage() -> OrderBook:
    """Create an order book without arbitrage opportunity (combined ask >= 1)."""
    # YES ask = 0.55, NO ask = 0.50 => combined = 1.05, no arbitrage
    return OrderBook(
        market_id="test_market_789",
        yes_asks=[OrderBookLevel(price=Decimal("0.55"), size=Decimal("100"))],
        yes_bids=[OrderBookLevel(price=Decimal("0.54"), size=Decimal("100"))],
        no_asks=[OrderBookLevel(price=Decimal("0.50"), size=Decimal("100"))],
        no_bids=[OrderBookLevel(price=Decimal("0.49"), size=Decimal("100"))],
        timestamp=datetime.utcnow(),
    )


@pytest.fixture
def order_book_empty() -> OrderBook:
    """Create an empty order book (no asks)."""
    return OrderBook(
        market_id="test_market_empty",
        yes_asks=[],
        yes_bids=[],
        no_asks=[],
        no_bids=[],
        timestamp=datetime.utcnow(),
    )


class TestGabagoolStrategy:
    """Tests for GabagoolStrategy class."""

    def test_strategy_name(self, gabagool_strategy):
        """Verify strategy name is 'gabagool'."""
        assert gabagool_strategy.name == "gabagool"

    def test_enabled_by_default(self, gabagool_strategy):
        """Verify strategy is enabled by default."""
        assert gabagool_strategy.enabled is True

    def test_enable_disable(self, gabagool_strategy):
        """Verify enable/disable functionality."""
        gabagool_strategy.disable()
        assert gabagool_strategy.enabled is False

        gabagool_strategy.enable()
        assert gabagool_strategy.enabled is True

    @pytest.mark.asyncio
    async def test_start_stop_lifecycle(self, gabagool_strategy):
        """Verify start/stop lifecycle."""
        await gabagool_strategy.start()
        assert gabagool_strategy._running is True

        await gabagool_strategy.stop()
        assert gabagool_strategy._running is False


class TestArbitrageDetection:
    """Tests for arbitrage detection logic."""

    def test_detect_valid_arbitrage(self, gabagool_strategy, order_book_with_arbitrage):
        """Verify arbitrage is detected when combined ask < 1."""
        opportunity = gabagool_strategy._detect_arbitrage(order_book_with_arbitrage)

        assert opportunity is not None
        assert opportunity.yes_price == Decimal("0.45")
        assert opportunity.no_price == Decimal("0.50")
        assert opportunity.combined_price == Decimal("0.95")
        assert opportunity.spread == Decimal("0.05")
        assert opportunity.spread_cents == Decimal("5.0")

    def test_no_arbitrage_when_combined_gte_one(self, gabagool_strategy, order_book_no_arbitrage):
        """Verify no arbitrage when combined ask >= 1."""
        opportunity = gabagool_strategy._detect_arbitrage(order_book_no_arbitrage)
        assert opportunity is None

    def test_no_arbitrage_with_empty_book(self, gabagool_strategy, order_book_empty):
        """Verify no arbitrage with empty order book."""
        opportunity = gabagool_strategy._detect_arbitrage(order_book_empty)
        assert opportunity is None

    def test_profit_percentage_calculation(self, gabagool_strategy, order_book_with_arbitrage):
        """Verify profit percentage is calculated correctly."""
        opportunity = gabagool_strategy._detect_arbitrage(order_book_with_arbitrage)

        # Profit % = (spread / combined) * 100 = (0.05 / 0.95) * 100 ≈ 5.26%
        assert opportunity.profit_percentage == pytest.approx(Decimal("5.263157894736842"), rel=Decimal("0.01"))


class TestOpportunityValidation:
    """Tests for opportunity validation logic."""

    def test_validates_good_opportunity(self, gabagool_strategy, order_book_with_arbitrage):
        """Verify good opportunity passes validation."""
        opportunity = gabagool_strategy._detect_arbitrage(order_book_with_arbitrage)
        result = gabagool_strategy._validate_opportunity(opportunity, "test_market")

        assert result.is_valid is True

    def test_rejects_small_spread(self, gabagool_strategy, order_book_small_spread):
        """Verify opportunity with small spread is rejected."""
        opportunity = gabagool_strategy._detect_arbitrage(order_book_small_spread)
        result = gabagool_strategy._validate_opportunity(opportunity, "test_market")

        assert result.is_valid is False
        assert "threshold" in result.reason.lower()

    def test_rejects_invalid_prices(self, gabagool_strategy):
        """Verify opportunity with invalid prices is rejected."""
        opportunity = ArbitrageOpportunity(
            market_id="test",
            yes_price=Decimal("0"),  # Invalid
            no_price=Decimal("0.50"),
            combined_price=Decimal("0.50"),
            spread=Decimal("0.50"),
            spread_cents=Decimal("50.0"),
            profit_percentage=Decimal("100"),
            detected_at=datetime.now(timezone.utc),
        )
        result = gabagool_strategy._validate_opportunity(opportunity, "test_market")

        assert result.is_valid is False
        assert "invalid" in result.reason.lower() or "zero" in result.reason.lower()


class TestPositionSizing:
    """Tests for position size calculation."""

    def test_equal_shares_calculation(self, gabagool_strategy):
        """Verify position sizes result in equal shares."""
        yes_amount, no_amount = gabagool_strategy.calculate_position_sizes(
            budget=Decimal("25.0"),
            yes_price=Decimal("0.40"),
            no_price=Decimal("0.55"),
        )

        # For equal shares: yes_amount/yes_price == no_amount/no_price
        yes_shares = yes_amount / Decimal("0.40")
        no_shares = no_amount / Decimal("0.55")

        assert yes_shares == pytest.approx(no_shares, rel=Decimal("0.001"))

    def test_budget_distribution(self, gabagool_strategy):
        """Verify budget is fully distributed."""
        yes_amount, no_amount = gabagool_strategy.calculate_position_sizes(
            budget=Decimal("25.0"),
            yes_price=Decimal("0.45"),
            no_price=Decimal("0.50"),
        )

        # Total should be close to budget
        total = yes_amount + no_amount
        assert total <= Decimal("25.0")

    def test_zero_cost_returns_zero(self, gabagool_strategy):
        """Verify zero cost pair returns zero amounts."""
        yes_amount, no_amount = gabagool_strategy.calculate_position_sizes(
            budget=Decimal("25.0"),
            yes_price=Decimal("0.0"),
            no_price=Decimal("0.50"),
        )

        assert yes_amount == Decimal("0")
        assert no_amount == Decimal("0")

    def test_no_arbitrage_returns_zero(self, gabagool_strategy):
        """Verify no arbitrage condition returns zero amounts."""
        yes_amount, no_amount = gabagool_strategy.calculate_position_sizes(
            budget=Decimal("25.0"),
            yes_price=Decimal("0.55"),
            no_price=Decimal("0.50"),
        )

        assert yes_amount == Decimal("0")
        assert no_amount == Decimal("0")


class TestExpectedProfitCalculation:
    """Tests for expected profit calculation."""

    def test_profit_calculation(self, gabagool_strategy):
        """Verify expected profit calculation."""
        profit = gabagool_strategy.calculate_expected_profit(
            yes_amount=Decimal("10.0"),
            no_amount=Decimal("12.5"),
            yes_price=Decimal("0.40"),
            no_price=Decimal("0.50"),
        )

        # yes_shares = 10/0.40 = 25
        # no_shares = 12.5/0.50 = 25
        # min_shares = 25
        # payout = 25 * 1 = 25
        # profit = 25 - (10 + 12.5) = 2.5
        assert profit == Decimal("2.5")

    def test_unequal_shares_profit(self, gabagool_strategy):
        """Verify profit when shares are unequal (uses minimum)."""
        profit = gabagool_strategy.calculate_expected_profit(
            yes_amount=Decimal("10.0"),
            no_amount=Decimal("15.0"),
            yes_price=Decimal("0.40"),
            no_price=Decimal("0.50"),
        )

        # yes_shares = 10/0.40 = 25
        # no_shares = 15/0.50 = 30
        # min_shares = 25
        # payout = 25 * 1 = 25
        # profit = 25 - (10 + 15) = 0
        assert profit == Decimal("0")


class TestSignalGeneration:
    """Tests for signal generation."""

    @pytest.mark.asyncio
    async def test_generates_signal_on_opportunity(
        self,
        gabagool_strategy,
        order_book_with_arbitrage,
    ):
        """Verify signal is generated for valid arbitrage opportunity."""
        await gabagool_strategy.start()

        signals = []
        async for signal in gabagool_strategy.on_market_data(
            "test_market_123", order_book_with_arbitrage
        ):
            signals.append(signal)

        assert len(signals) == 1
        signal = signals[0]

        assert signal.strategy_name == "gabagool"
        assert signal.market_id == "test_market_123"
        assert signal.signal_type == SignalType.ARBITRAGE
        assert signal.yes_price == Decimal("0.45")
        assert signal.no_price == Decimal("0.50")

    @pytest.mark.asyncio
    async def test_no_signal_for_small_spread(
        self,
        gabagool_strategy,
        order_book_small_spread,
    ):
        """Verify no signal is generated for small spread."""
        await gabagool_strategy.start()

        signals = []
        async for signal in gabagool_strategy.on_market_data(
            "test_market_456", order_book_small_spread
        ):
            signals.append(signal)

        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_no_signal_for_no_arbitrage(
        self,
        gabagool_strategy,
        order_book_no_arbitrage,
    ):
        """Verify no signal when no arbitrage opportunity."""
        await gabagool_strategy.start()

        signals = []
        async for signal in gabagool_strategy.on_market_data(
            "test_market_789", order_book_no_arbitrage
        ):
            signals.append(signal)

        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_no_signal_when_disabled(
        self,
        gabagool_strategy,
        order_book_with_arbitrage,
    ):
        """Verify no signal when strategy is disabled."""
        await gabagool_strategy.start()
        gabagool_strategy.disable()

        signals = []
        async for signal in gabagool_strategy.on_market_data(
            "test_market_123", order_book_with_arbitrage
        ):
            signals.append(signal)

        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_signal_generated_without_start(
        self,
        gabagool_strategy,
        order_book_with_arbitrage,
    ):
        """Verify signals are generated without calling start() (for direct testing).

        The strategy should work for signal generation even without start() being
        called. This allows for flexible testing and is consistent with the
        BaseStrategy protocol which doesn't mandate start() for signal generation.
        """
        # Don't call start() - strategy should still work for direct testing

        signals = []
        async for signal in gabagool_strategy.on_market_data(
            "test_market_123", order_book_with_arbitrage
        ):
            signals.append(signal)

        # Signal should be generated even without start()
        assert len(signals) == 1


class TestCooldownBehavior:
    """Tests for signal cooldown behavior."""

    @pytest.mark.asyncio
    async def test_cooldown_prevents_duplicate_signals(
        self,
        gabagool_strategy,
        order_book_with_arbitrage,
    ):
        """Verify cooldown prevents immediate duplicate signals."""
        await gabagool_strategy.start()

        # First signal should be generated
        signals1 = []
        async for signal in gabagool_strategy.on_market_data(
            "test_market_123", order_book_with_arbitrage
        ):
            signals1.append(signal)
        assert len(signals1) == 1

        # Immediate second call should be blocked by cooldown
        signals2 = []
        async for signal in gabagool_strategy.on_market_data(
            "test_market_123", order_book_with_arbitrage
        ):
            signals2.append(signal)
        assert len(signals2) == 0

    @pytest.mark.asyncio
    async def test_different_markets_not_affected_by_cooldown(
        self,
        gabagool_strategy,
        order_book_with_arbitrage,
    ):
        """Verify different markets have independent cooldowns."""
        await gabagool_strategy.start()

        # First market signal
        signals1 = []
        async for signal in gabagool_strategy.on_market_data(
            "market_1", order_book_with_arbitrage
        ):
            signals1.append(signal)
        assert len(signals1) == 1

        # Different market should not be blocked
        book2 = OrderBook(
            market_id="market_2",
            yes_asks=[OrderBookLevel(price=Decimal("0.45"), size=Decimal("100"))],
            yes_bids=[OrderBookLevel(price=Decimal("0.44"), size=Decimal("100"))],
            no_asks=[OrderBookLevel(price=Decimal("0.50"), size=Decimal("100"))],
            no_bids=[OrderBookLevel(price=Decimal("0.49"), size=Decimal("100"))],
            timestamp=datetime.utcnow(),
        )

        signals2 = []
        async for signal in gabagool_strategy.on_market_data("market_2", book2):
            signals2.append(signal)
        assert len(signals2) == 1


class TestSignalProperties:
    """Tests for signal properties and metadata."""

    @pytest.mark.asyncio
    async def test_signal_has_expected_pnl(
        self,
        gabagool_strategy,
        order_book_with_arbitrage,
    ):
        """Verify signal has positive expected PnL."""
        await gabagool_strategy.start()

        signals = []
        async for signal in gabagool_strategy.on_market_data(
            "test_market_123", order_book_with_arbitrage
        ):
            signals.append(signal)

        signal = signals[0]
        assert signal.expected_pnl > 0

    @pytest.mark.asyncio
    async def test_signal_has_metadata(
        self,
        gabagool_strategy,
        order_book_with_arbitrage,
    ):
        """Verify signal has expected metadata."""
        await gabagool_strategy.start()

        signals = []
        async for signal in gabagool_strategy.on_market_data(
            "test_market_123", order_book_with_arbitrage
        ):
            signals.append(signal)

        signal = signals[0]
        assert "spread_cents" in signal.metadata
        assert "profit_percentage" in signal.metadata
        assert "yes_amount" in signal.metadata
        assert "no_amount" in signal.metadata

    @pytest.mark.asyncio
    async def test_signal_priority_high_for_large_spread(
        self,
        gabagool_strategy,
    ):
        """Verify signal priority is HIGH for large spread."""
        await gabagool_strategy.start()

        # Create order book with large spread (5 cents)
        book = OrderBook(
            market_id="test_market",
            yes_asks=[OrderBookLevel(price=Decimal("0.45"), size=Decimal("100"))],
            yes_bids=[OrderBookLevel(price=Decimal("0.44"), size=Decimal("100"))],
            no_asks=[OrderBookLevel(price=Decimal("0.50"), size=Decimal("100"))],
            no_bids=[OrderBookLevel(price=Decimal("0.49"), size=Decimal("100"))],
            timestamp=datetime.utcnow(),
        )

        signals = []
        async for signal in gabagool_strategy.on_market_data("test_market", book):
            signals.append(signal)

        signal = signals[0]
        assert signal.priority in (SignalPriority.HIGH, SignalPriority.CRITICAL)

    @pytest.mark.asyncio
    async def test_signal_has_expiration(
        self,
        gabagool_strategy,
        order_book_with_arbitrage,
    ):
        """Verify signal has expiration time set."""
        await gabagool_strategy.start()

        signals = []
        async for signal in gabagool_strategy.on_market_data(
            "test_market_123", order_book_with_arbitrage
        ):
            signals.append(signal)

        signal = signals[0]
        assert signal.expires_at is not None
        # Should expire in approximately 30 seconds
        time_to_expiry = (signal.expires_at - datetime.now(timezone.utc)).total_seconds()
        assert 25 <= time_to_expiry <= 35


class TestMarketSubscription:
    """Tests for market subscription management."""

    def test_subscribe_market(self, gabagool_strategy):
        """Verify market subscription adds to list."""
        gabagool_strategy.subscribe_market("market_1")
        gabagool_strategy.subscribe_market("market_2")

        markets = gabagool_strategy.get_subscribed_markets()
        assert "market_1" in markets
        assert "market_2" in markets

    def test_subscribe_market_idempotent(self, gabagool_strategy):
        """Verify duplicate subscription is ignored."""
        gabagool_strategy.subscribe_market("market_1")
        gabagool_strategy.subscribe_market("market_1")

        markets = gabagool_strategy.get_subscribed_markets()
        assert markets.count("market_1") == 1

    def test_unsubscribe_market(self, gabagool_strategy):
        """Verify market unsubscription removes from list."""
        gabagool_strategy.subscribe_market("market_1")
        gabagool_strategy.subscribe_market("market_2")

        gabagool_strategy.unsubscribe_market("market_1")

        markets = gabagool_strategy.get_subscribed_markets()
        assert "market_1" not in markets
        assert "market_2" in markets


class TestConfidenceCalculation:
    """Tests for confidence calculation."""

    def test_confidence_scales_with_spread(self, gabagool_strategy):
        """Verify confidence increases with spread size."""
        # Low spread (at threshold)
        low_conf = gabagool_strategy._calculate_confidence(Decimal("1.5"))

        # High spread
        high_conf = gabagool_strategy._calculate_confidence(Decimal("5.0"))

        assert high_conf > low_conf

    def test_confidence_bounded(self, gabagool_strategy):
        """Verify confidence stays within bounds."""
        min_conf = gabagool_strategy._calculate_confidence(Decimal("0.5"))
        max_conf = gabagool_strategy._calculate_confidence(Decimal("10.0"))

        assert 0.0 <= min_conf <= 1.0
        assert 0.0 <= max_conf <= 1.0
        assert max_conf <= 0.95  # Max is capped at 0.95


class TestPriorityDetermination:
    """Tests for signal priority determination."""

    def test_critical_priority_for_large_spread(self, gabagool_strategy):
        """Verify CRITICAL priority for large spread (>= 4 cents)."""
        priority = gabagool_strategy._determine_priority(Decimal("4.5"))
        assert priority == SignalPriority.CRITICAL

    def test_high_priority_for_medium_spread(self, gabagool_strategy):
        """Verify HIGH priority for medium spread (>= 3 cents)."""
        priority = gabagool_strategy._determine_priority(Decimal("3.5"))
        assert priority == SignalPriority.HIGH

    def test_medium_priority_for_small_spread(self, gabagool_strategy):
        """Verify MEDIUM priority for small spread (>= 2 cents)."""
        priority = gabagool_strategy._determine_priority(Decimal("2.5"))
        assert priority == SignalPriority.MEDIUM

    def test_low_priority_for_tiny_spread(self, gabagool_strategy):
        """Verify LOW priority for tiny spread (< 2 cents)."""
        priority = gabagool_strategy._determine_priority(Decimal("1.5"))
        assert priority == SignalPriority.LOW
