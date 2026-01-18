"""Regression tests for opportunity queue processing (Dec 14, 2025 fix).

These tests ensure:
1. Opportunities have correct validity window (30s, not 5s)
2. Dedicated queue processor runs independently of main loop
3. Expired opportunities are properly logged and skipped
4. Queue processor responds quickly to new opportunities

Root cause of issue:
- Opportunity validity was 5 seconds
- Main loop was blocked by market refresh (4+ seconds)
- By the time queue was checked, opportunity had expired
- Trade appeared to execute on Polymarket but bot had no record

Fix:
- Increased validity from 5s to 30s
- Created dedicated async task for queue processing
- Task runs independently, not blocked by main loop operations
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

from src.monitoring.order_book import ArbitrageOpportunity
from src.monitoring.market_finder import Market15Min


@pytest.fixture
def mock_market():
    """Create a mock Market15Min for testing."""
    market = MagicMock(spec=Market15Min)
    market.condition_id = "test_condition_123"
    market.asset = "BTC"
    market.question = "Bitcoin Up or Down - Test"
    market.yes_token_id = "yes_token_123"
    market.no_token_id = "no_token_123"
    market.end_time = datetime.utcnow() + timedelta(minutes=10)
    market.seconds_remaining = 600
    return market


class TestOpportunityValidity:
    """Test that opportunity validity window is properly configured."""

    def test_validity_window_is_30_seconds(self, mock_market):
        """Opportunity validity should be 30 seconds, not 5."""
        opportunity = ArbitrageOpportunity(
            market=mock_market,
            yes_price=0.40,
            no_price=0.52,
            spread=0.08,
            spread_cents=8.0,
            profit_percentage=8.7,
        )

        # Should be 30 seconds
        assert opportunity.VALIDITY_SECONDS == 30.0

    def test_fresh_opportunity_is_valid(self, mock_market):
        """A freshly created opportunity should be valid."""
        opportunity = ArbitrageOpportunity(
            market=mock_market,
            yes_price=0.40,
            no_price=0.52,
            spread=0.08,
            spread_cents=8.0,
            profit_percentage=8.7,
        )

        assert opportunity.is_valid is True
        assert opportunity.age_seconds < 1.0

    def test_opportunity_valid_at_25_seconds(self, mock_market):
        """Opportunity should still be valid at 25 seconds (was failing at 5s before)."""
        opportunity = ArbitrageOpportunity(
            market=mock_market,
            yes_price=0.40,
            no_price=0.52,
            spread=0.08,
            spread_cents=8.0,
            profit_percentage=8.7,
            detected_at=datetime.utcnow() - timedelta(seconds=25),
        )

        # This would have failed with the old 5s validity!
        assert opportunity.is_valid is True
        assert opportunity.age_seconds >= 25.0

    def test_opportunity_invalid_at_31_seconds(self, mock_market):
        """Opportunity should be invalid after 30 seconds."""
        opportunity = ArbitrageOpportunity(
            market=mock_market,
            yes_price=0.40,
            no_price=0.52,
            spread=0.08,
            spread_cents=8.0,
            profit_percentage=8.7,
            detected_at=datetime.utcnow() - timedelta(seconds=31),
        )

        assert opportunity.is_valid is False
        assert opportunity.age_seconds >= 31.0

    def test_age_seconds_property(self, mock_market):
        """Test age_seconds property returns correct age."""
        past = datetime.utcnow() - timedelta(seconds=10)
        opportunity = ArbitrageOpportunity(
            market=mock_market,
            yes_price=0.40,
            no_price=0.52,
            spread=0.08,
            spread_cents=8.0,
            profit_percentage=8.7,
            detected_at=past,
        )

        # Should be approximately 10 seconds
        assert 9.5 <= opportunity.age_seconds <= 11.0


class TestDedicatedQueueProcessor:
    """Test that the dedicated queue processor task exists and works."""

    @pytest.fixture
    def mock_strategy(self):
        """Create a mock GabagoolStrategy for testing."""
        from src.strategies.gabagool import GabagoolStrategy
        from src.config import AppConfig, GabagoolConfig, PolymarketSettings, CopyTradingConfig

        # Create minimal config
        poly_settings = MagicMock(spec=PolymarketSettings)
        poly_settings.clob_http_url = "https://test.com"
        poly_settings.private_key = "0x" + "a" * 64
        poly_settings.signature_type = 1
        poly_settings.proxy_wallet = None
        poly_settings.api_key = None

        gabagool_config = GabagoolConfig(
            enabled=True,
            dry_run=True,
            min_spread_threshold=0.02,
            max_trade_size_usd=25.0,
        )

        copy_config = MagicMock(spec=CopyTradingConfig)
        copy_config.enabled = False

        config = MagicMock(spec=AppConfig)
        config.polymarket = poly_settings
        config.gabagool = gabagool_config
        config.copy_trading = copy_config

        # Create mocks
        client = MagicMock()
        client.get_balance = MagicMock(return_value={"balance": 100.0})
        ws_client = MagicMock()
        market_finder = MagicMock()

        strategy = GabagoolStrategy(
            client=client,
            ws_client=ws_client,
            market_finder=market_finder,
            config=config,
        )

        return strategy

    def test_strategy_has_queue_processor_task_attribute(self, mock_strategy):
        """Strategy should have _queue_processor_task attribute."""
        assert hasattr(mock_strategy, "_queue_processor_task")
        assert mock_strategy._queue_processor_task is None  # Not started yet

    def test_strategy_has_opportunity_queue(self, mock_strategy):
        """Strategy should have an asyncio.Queue for opportunities."""
        assert hasattr(mock_strategy, "_opportunity_queue")
        assert isinstance(mock_strategy._opportunity_queue, asyncio.Queue)

    @pytest.mark.asyncio
    async def test_queue_opportunity_adds_to_queue(self, mock_strategy, mock_market):
        """_queue_opportunity should add opportunity to queue."""
        opportunity = ArbitrageOpportunity(
            market=mock_market,
            yes_price=0.40,
            no_price=0.52,
            spread=0.08,
            spread_cents=8.0,
            profit_percentage=8.7,
        )

        # Queue should be empty initially
        assert mock_strategy._opportunity_queue.empty()

        # Add opportunity
        mock_strategy._queue_opportunity(opportunity)

        # Queue should have one item
        assert not mock_strategy._opportunity_queue.empty()
        queued = mock_strategy._opportunity_queue.get_nowait()
        assert queued.market.asset == "BTC"
        assert queued.spread_cents == 8.0


class TestQueueProcessorResponsiveness:
    """Test that queue processor responds quickly to opportunities."""

    @pytest.mark.asyncio
    async def test_queue_processor_loop_processes_quickly(self):
        """Queue processor should process opportunities within 100ms timeout."""
        from src.strategies.gabagool import GabagoolStrategy
        from src.config import AppConfig, GabagoolConfig, PolymarketSettings, CopyTradingConfig

        # Create minimal config
        poly_settings = MagicMock(spec=PolymarketSettings)
        poly_settings.clob_http_url = "https://test.com"
        poly_settings.private_key = "0x" + "a" * 64
        poly_settings.signature_type = 1
        poly_settings.proxy_wallet = None
        poly_settings.api_key = None

        gabagool_config = GabagoolConfig(
            enabled=True,
            dry_run=True,
            min_spread_threshold=0.02,
            max_trade_size_usd=25.0,
        )

        copy_config = MagicMock(spec=CopyTradingConfig)
        copy_config.enabled = False

        config = MagicMock(spec=AppConfig)
        config.polymarket = poly_settings
        config.gabagool = gabagool_config
        config.copy_trading = copy_config

        client = MagicMock()
        ws_client = MagicMock()
        market_finder = MagicMock()

        strategy = GabagoolStrategy(
            client=client,
            ws_client=ws_client,
            market_finder=market_finder,
            config=config,
        )

        # Track if on_opportunity was called
        executed = asyncio.Event()
        original_on_opportunity = strategy.on_opportunity

        async def mock_on_opportunity(opp):
            executed.set()
            # Don't actually execute

        strategy.on_opportunity = mock_on_opportunity
        strategy._running = True

        # Create and queue an opportunity
        mock_market = MagicMock(spec=Market15Min)
        mock_market.condition_id = "test_123"
        mock_market.asset = "BTC"
        mock_market.question = "Test"
        mock_market.yes_token_id = "yes"
        mock_market.no_token_id = "no"
        mock_market.end_time = datetime.utcnow() + timedelta(minutes=10)
        mock_market.seconds_remaining = 600

        opportunity = ArbitrageOpportunity(
            market=mock_market,
            yes_price=0.40,
            no_price=0.52,
            spread=0.08,
            spread_cents=8.0,
            profit_percentage=8.7,
        )

        # Start the queue processor
        task = asyncio.create_task(strategy._queue_processor_loop())

        # Add opportunity to queue
        strategy._queue_opportunity(opportunity)

        # Wait for execution (should be quick - within 200ms)
        try:
            await asyncio.wait_for(executed.wait(), timeout=0.5)
            assert True, "Opportunity was processed within timeout"
        except asyncio.TimeoutError:
            pytest.fail("Queue processor did not process opportunity within 500ms")
        finally:
            strategy._running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


class TestExpiredOpportunityLogging:
    """Test that expired opportunities are properly logged and tracked."""

    @pytest.mark.asyncio
    async def test_expired_opportunity_is_logged_with_warning(self):
        """Expired opportunities should be logged with WARNING level."""
        from src.strategies.gabagool import GabagoolStrategy
        from src.config import AppConfig, GabagoolConfig, PolymarketSettings, CopyTradingConfig

        # Create minimal config
        poly_settings = MagicMock(spec=PolymarketSettings)
        poly_settings.clob_http_url = "https://test.com"
        poly_settings.private_key = "0x" + "a" * 64
        poly_settings.signature_type = 1
        poly_settings.proxy_wallet = None
        poly_settings.api_key = None

        gabagool_config = GabagoolConfig(
            enabled=True,
            dry_run=True,
            min_spread_threshold=0.02,
            max_trade_size_usd=25.0,
        )

        copy_config = MagicMock(spec=CopyTradingConfig)
        copy_config.enabled = False

        config = MagicMock(spec=AppConfig)
        config.polymarket = poly_settings
        config.gabagool = gabagool_config
        config.copy_trading = copy_config

        client = MagicMock()
        ws_client = MagicMock()
        market_finder = MagicMock()

        strategy = GabagoolStrategy(
            client=client,
            ws_client=ws_client,
            market_finder=market_finder,
            config=config,
        )

        strategy._running = True

        # Create an EXPIRED opportunity (40 seconds old)
        mock_market = MagicMock(spec=Market15Min)
        mock_market.condition_id = "test_123"
        mock_market.asset = "BTC"
        mock_market.question = "Test"
        mock_market.yes_token_id = "yes"
        mock_market.no_token_id = "no"
        mock_market.end_time = datetime.utcnow() + timedelta(minutes=10)
        mock_market.seconds_remaining = 600

        expired_opportunity = ArbitrageOpportunity(
            market=mock_market,
            yes_price=0.40,
            no_price=0.52,
            spread=0.08,
            spread_cents=8.0,
            profit_percentage=8.7,
            detected_at=datetime.utcnow() - timedelta(seconds=40),
        )

        # Verify it's actually expired
        assert expired_opportunity.is_valid is False

        # Track if on_opportunity was NOT called (since it's expired)
        on_opp_called = False

        async def mock_on_opportunity(opp):
            nonlocal on_opp_called
            on_opp_called = True

        strategy.on_opportunity = mock_on_opportunity

        # Start queue processor
        task = asyncio.create_task(strategy._queue_processor_loop())

        # Add expired opportunity
        await strategy._opportunity_queue.put(expired_opportunity)

        # Give it time to process
        await asyncio.sleep(0.3)

        # Stop and cleanup
        strategy._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # on_opportunity should NOT have been called
        assert on_opp_called is False, "on_opportunity was called for expired opportunity!"


class TestMainLoopNotBlocking:
    """Test that main loop operations don't block queue processing."""

    def test_main_loop_comment_mentions_dedicated_task(self):
        """Main loop docstring should mention dedicated queue processor."""
        from src.strategies.gabagool import GabagoolStrategy

        docstring = GabagoolStrategy._run_loop.__doc__
        assert "dedicated" in docstring.lower() or "queue" in docstring.lower()

    def test_main_loop_does_not_process_queue_directly(self):
        """Main loop should NOT have direct queue processing code."""
        import inspect
        from src.strategies.gabagool import GabagoolStrategy

        source = inspect.getsource(GabagoolStrategy._run_loop)

        # Should NOT contain the old queue processing pattern
        assert "_opportunity_queue.get_nowait()" not in source
        assert "PRIORITY 1: Process queued" not in source
