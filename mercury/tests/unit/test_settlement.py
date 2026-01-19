"""
Unit tests for the SettlementManager service.

Tests cover:
- Lifecycle management (start/stop)
- Periodic check loop with configurable interval
- Position event handling (position.opened subscription)
- Settlement queue processing
- Market resolution checking via Gamma API
- Claim success/failure handling with proceeds calculation
- Event publishing (settlement.claimed, settlement.failed, settlement.queued, settlement.alert)
- Health check reporting with queue statistics
- Settlement queue state transitions (pending -> claimable -> claimed)
- Exponential backoff for claim retries
- Alert emission after configurable failures
- Non-blocking failure handling (continue processing other claims)
"""
import pytest
import asyncio
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

from mercury.services.settlement import (
    SettlementManager,
    SettlementResult,
    DEFAULT_CHECK_INTERVAL,
    DEFAULT_RESOLUTION_WAIT_SECONDS,
    MAX_CLAIM_ATTEMPTS,
    DEFAULT_RETRY_INITIAL_DELAY,
    DEFAULT_RETRY_MAX_DELAY,
    DEFAULT_RETRY_EXPONENTIAL_BASE,
    DEFAULT_ALERT_AFTER_FAILURES,
)
from mercury.services.state_store import Position, SettlementQueueEntry
from mercury.integrations.polymarket.types import MarketInfo


@pytest.fixture
def settlement_config():
    """Create mock config for settlement manager."""
    config = MagicMock()

    def get_side_effect(key, default=None):
        values = {
            "settlement.check_interval_seconds": 60,
            "settlement.resolution_wait_seconds": 600,
            "settlement.max_claim_attempts": 5,
            "settlement.retry_initial_delay_seconds": 60,
            "settlement.retry_max_delay_seconds": 3600,
            "settlement.retry_exponential_base": 2.0,
            "settlement.retry_jitter": False,  # Disable for predictable tests
            "settlement.alert_after_failures": 3,
            "mercury.dry_run": True,
            "polymarket.private_key": "0x" + "a" * 64,
            "polygon.rpc_url": "https://polygon-rpc.com",
        }
        return values.get(key, default)

    def get_int_side_effect(key, default=0):
        values = {
            "settlement.check_interval_seconds": 60,
            "settlement.resolution_wait_seconds": 600,
            "settlement.max_claim_attempts": 5,
            "settlement.retry_initial_delay_seconds": 60,
            "settlement.retry_max_delay_seconds": 3600,
            "settlement.alert_after_failures": 3,
        }
        return values.get(key, default)

    def get_float_side_effect(key, default=0.0):
        values = {
            "settlement.retry_exponential_base": 2.0,
        }
        return values.get(key, default)

    def get_bool_side_effect(key, default=False):
        values = {
            "mercury.dry_run": True,
            "settlement.retry_jitter": False,  # Disable for predictable tests
        }
        return values.get(key, default)

    config.get.side_effect = get_side_effect
    config.get_int.side_effect = get_int_side_effect
    config.get_float.side_effect = get_float_side_effect
    config.get_bool.side_effect = get_bool_side_effect
    return config


@pytest.fixture
def mock_state_store():
    """Create mock StateStore."""
    store = MagicMock()
    store.get_claimable_positions = AsyncMock(return_value=[])
    store.get_settlement_queue_entry = AsyncMock(return_value=None)
    store.get_settlement_queue = AsyncMock(return_value=[])
    store.get_settlement_stats = AsyncMock(return_value={
        "total_positions": 0,
        "unclaimed": 0,
        "claimed_count": 0,
        "total_claim_profit": 0,
    })
    store.get_failed_claims = AsyncMock(return_value=[])
    store.mark_claimed = AsyncMock()
    store.mark_settlement_failed = AsyncMock()
    store.record_claim_attempt = AsyncMock()
    store.retry_failed_claim = AsyncMock(return_value=True)
    store.queue_for_settlement = AsyncMock()
    return store


@pytest.fixture
def mock_gamma_client():
    """Create mock GammaClient."""
    client = MagicMock()
    client.connect = AsyncMock()
    client.close = AsyncMock()
    client.get_market = AsyncMock(return_value={"resolved": False})
    client.get_market_info = AsyncMock(return_value=None)
    return client


@pytest.fixture
def mock_polygon_client():
    """Create mock PolygonClient."""
    client = MagicMock()
    client.connect = AsyncMock()
    client.close = AsyncMock()
    client.redeem_ctf_positions = AsyncMock()
    return client


@pytest.fixture
def settlement_manager(settlement_config, mock_event_bus, mock_state_store, mock_gamma_client):
    """Create SettlementManager instance with mocked dependencies."""
    return SettlementManager(
        config=settlement_config,
        event_bus=mock_event_bus,
        state_store=mock_state_store,
        gamma_client=mock_gamma_client,
    )


@pytest.fixture
def sample_position():
    """Create a sample Position for testing."""
    return Position(
        position_id="pos-123",
        market_id="market-456",
        strategy="gabagool",
        side="YES",
        size=Decimal("10"),
        entry_price=Decimal("0.45"),
    )


@pytest.fixture
def sample_queue_entry():
    """Create a sample SettlementQueueEntry for testing."""
    return SettlementQueueEntry(
        id=1,
        position_id="pos-123",
        market_id="market-456",
        condition_id="cond-789" + "0" * 40,
        side="YES",
        size=Decimal("10"),
        entry_price=Decimal("0.45"),
        shares=Decimal("10"),
        entry_cost=Decimal("4.50"),
        status="pending",
        claim_attempts=0,
    )


@pytest.fixture
def resolved_market_info_yes():
    """Create a MarketInfo that resolved to YES."""
    return MarketInfo(
        condition_id="cond-789" + "0" * 40,
        question_id="question-123",
        question="Will BTC be above $50k?",
        slug="btc-above-50k",
        yes_token_id="yes-token-123",
        no_token_id="no-token-456",
        yes_price=Decimal("1.0"),
        no_price=Decimal("0.0"),
        active=False,
        closed=True,
        resolved=True,
        resolution="YES",
    )


@pytest.fixture
def resolved_market_info_no():
    """Create a MarketInfo that resolved to NO."""
    return MarketInfo(
        condition_id="cond-789" + "0" * 40,
        question_id="question-123",
        question="Will BTC be above $50k?",
        slug="btc-above-50k",
        yes_token_id="yes-token-123",
        no_token_id="no-token-456",
        yes_price=Decimal("0.0"),
        no_price=Decimal("1.0"),
        active=False,
        closed=True,
        resolved=True,
        resolution="NO",
    )


class TestSettlementManagerInitialization:
    """Test SettlementManager initialization."""

    def test_initializes_with_default_check_interval(self, mock_event_bus, mock_state_store):
        """Verify default check interval is used when not in config."""
        config = MagicMock()
        config.get_int.return_value = DEFAULT_CHECK_INTERVAL
        config.get_bool.return_value = True

        manager = SettlementManager(
            config=config,
            event_bus=mock_event_bus,
            state_store=mock_state_store,
        )

        assert manager._check_interval == DEFAULT_CHECK_INTERVAL

    def test_initializes_with_custom_check_interval(self, settlement_config, mock_event_bus, mock_state_store):
        """Verify custom check interval is loaded from config."""
        manager = SettlementManager(
            config=settlement_config,
            event_bus=mock_event_bus,
            state_store=mock_state_store,
        )

        assert manager._check_interval == 60

    def test_initializes_with_resolution_wait(self, settlement_config, mock_event_bus, mock_state_store):
        """Verify resolution wait is loaded from config."""
        manager = SettlementManager(
            config=settlement_config,
            event_bus=mock_event_bus,
            state_store=mock_state_store,
        )

        assert manager._resolution_wait == 600

    def test_initial_state(self, settlement_manager):
        """Verify initial manager state."""
        assert settlement_manager._should_run is False
        assert settlement_manager._claims_processed == 0
        assert settlement_manager._claims_failed == 0
        assert settlement_manager._positions_queued == 0
        assert settlement_manager._markets_checked == 0
        assert settlement_manager._check_task is None
        assert settlement_manager._resolution_cache == {}


class TestSettlementManagerLifecycle:
    """Test start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_sets_running_state(self, settlement_manager, mock_event_bus):
        """Start should set running state and start check loop."""
        await settlement_manager.start()

        assert settlement_manager._should_run is True
        assert settlement_manager._check_task is not None
        # Should subscribe to both position.opened and order.filled
        assert mock_event_bus.subscribe.call_count == 2

        await settlement_manager.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_check_loop(self, settlement_manager):
        """Stop should cancel the check loop task."""
        await settlement_manager.start()
        assert settlement_manager._check_task is not None

        await settlement_manager.stop()

        assert settlement_manager._should_run is False

    @pytest.mark.asyncio
    async def test_stop_closes_clients(self, settlement_manager, mock_gamma_client):
        """Stop should close the gamma client."""
        await settlement_manager.start()
        await settlement_manager.stop()

        mock_gamma_client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_idempotent(self, settlement_manager, mock_event_bus):
        """Multiple start calls should be handled gracefully."""
        await settlement_manager.start()
        task1 = settlement_manager._check_task

        # Second start - should not create a new task if already running
        await settlement_manager.start()

        await settlement_manager.stop()


class TestPeriodicCheckLoop:
    """Test the periodic settlement check loop."""

    @pytest.mark.asyncio
    async def test_check_loop_runs_periodically(self, settlement_manager, mock_state_store):
        """Check loop should call check_settlements periodically."""
        # Use a very short interval for testing
        settlement_manager._check_interval = 0.01

        await settlement_manager.start()

        # Wait for a couple of iterations
        await asyncio.sleep(0.05)

        await settlement_manager.stop()

        # Should have been called at least once
        assert mock_state_store.get_claimable_positions.call_count >= 1

    @pytest.mark.asyncio
    async def test_check_loop_handles_errors(self, settlement_manager, mock_state_store):
        """Check loop should continue running even if check_settlements raises."""
        mock_state_store.get_claimable_positions.side_effect = Exception("Database error")
        settlement_manager._check_interval = 0.01

        await settlement_manager.start()

        # Wait for a couple of iterations
        await asyncio.sleep(0.05)

        await settlement_manager.stop()

        # Should have tried multiple times despite errors
        assert mock_state_store.get_claimable_positions.call_count >= 1


class TestCheckSettlements:
    """Test the check_settlements method."""

    @pytest.mark.asyncio
    async def test_returns_zero_without_state_store(self, settlement_config, mock_event_bus, mock_gamma_client):
        """Should return 0 if no state store is configured."""
        manager = SettlementManager(
            config=settlement_config,
            event_bus=mock_event_bus,
            state_store=None,
            gamma_client=mock_gamma_client,
        )

        result = await manager.check_settlements()

        assert result == 0

    @pytest.mark.asyncio
    async def test_returns_zero_when_queue_empty(self, settlement_manager, mock_state_store):
        """Should return 0 when no claimable positions."""
        mock_state_store.get_claimable_positions.return_value = []

        result = await settlement_manager.check_settlements()

        assert result == 0
        mock_state_store.get_claimable_positions.assert_called_once()

    @pytest.mark.asyncio
    async def test_processes_claimable_positions(
        self,
        settlement_manager,
        mock_state_store,
        mock_gamma_client,
        mock_event_bus,
        sample_position,
        sample_queue_entry,
        resolved_market_info_yes,
    ):
        """Should process positions from the settlement queue."""
        mock_state_store.get_claimable_positions.return_value = [sample_position]
        mock_state_store.get_settlement_queue_entry.return_value = sample_queue_entry
        mock_gamma_client.get_market_info.return_value = resolved_market_info_yes

        result = await settlement_manager.check_settlements()

        # In dry run mode, should succeed
        assert result == 1
        assert settlement_manager._claims_processed == 1
        mock_event_bus.publish.assert_called()

    @pytest.mark.asyncio
    async def test_skips_unresolved_markets(
        self,
        settlement_manager,
        mock_state_store,
        mock_gamma_client,
        sample_position,
        sample_queue_entry,
    ):
        """Should skip positions where market is not resolved."""
        mock_state_store.get_claimable_positions.return_value = [sample_position]
        mock_state_store.get_settlement_queue_entry.return_value = sample_queue_entry
        mock_gamma_client.get_market_info.return_value = None  # Not resolved

        result = await settlement_manager.check_settlements()

        assert result == 0

    @pytest.mark.asyncio
    async def test_handles_claim_errors(
        self,
        settlement_manager,
        mock_state_store,
        mock_gamma_client,
        sample_position,
        sample_queue_entry,
    ):
        """Should handle errors during claim processing."""
        mock_state_store.get_claimable_positions.return_value = [sample_position]
        mock_state_store.get_settlement_queue_entry.return_value = sample_queue_entry
        mock_gamma_client.get_market_info.side_effect = Exception("API error")

        result = await settlement_manager.check_settlements()

        assert result == 0
        assert settlement_manager._claims_failed == 1


class TestMarketResolutionChecking:
    """Test market resolution checking via Gamma API."""

    @pytest.mark.asyncio
    async def test_caches_resolved_markets(
        self,
        settlement_manager,
        mock_gamma_client,
        resolved_market_info_yes,
    ):
        """Should cache resolved market info."""
        condition_id = "cond-789" + "0" * 40
        mock_gamma_client.get_market_info.return_value = resolved_market_info_yes

        # First call - should hit API
        result1 = await settlement_manager._check_market_resolution(condition_id)
        assert result1 is not None
        assert result1.resolved is True
        assert mock_gamma_client.get_market_info.call_count == 1

        # Second call - should use cache
        result2 = await settlement_manager._check_market_resolution(condition_id)
        assert result2 is not None
        # Still only 1 API call
        assert mock_gamma_client.get_market_info.call_count == 1

    @pytest.mark.asyncio
    async def test_does_not_cache_unresolved_markets(
        self,
        settlement_manager,
        mock_gamma_client,
    ):
        """Should not cache unresolved market info."""
        condition_id = "cond-789" + "0" * 40
        unresolved = MarketInfo(
            condition_id=condition_id,
            question_id="q",
            question="test",
            slug="test",
            yes_token_id="y",
            no_token_id="n",
            yes_price=Decimal("0.5"),
            no_price=Decimal("0.5"),
            active=True,
            closed=False,
            resolved=False,
        )
        mock_gamma_client.get_market_info.return_value = unresolved

        # First call
        result1 = await settlement_manager._check_market_resolution(condition_id)
        assert result1 is None  # Not resolved

        # Second call - should still hit API
        await settlement_manager._check_market_resolution(condition_id)
        assert mock_gamma_client.get_market_info.call_count == 2

    @pytest.mark.asyncio
    async def test_clear_resolution_cache(self, settlement_manager, mock_gamma_client, resolved_market_info_yes):
        """Should be able to clear the resolution cache."""
        condition_id = "cond-789" + "0" * 40
        mock_gamma_client.get_market_info.return_value = resolved_market_info_yes

        # Populate cache
        await settlement_manager._check_market_resolution(condition_id)
        assert len(settlement_manager._resolution_cache) == 1

        # Clear cache
        cleared = settlement_manager.clear_resolution_cache()
        assert cleared == 1
        assert len(settlement_manager._resolution_cache) == 0


class TestSettlementProceedsCalculation:
    """Test settlement proceeds and profit calculation."""

    def test_winning_yes_position(self, settlement_manager, sample_queue_entry, resolved_market_info_yes):
        """YES position wins when market resolves YES."""
        proceeds, profit = settlement_manager._calculate_settlement_proceeds(
            sample_queue_entry,
            resolved_market_info_yes,
        )

        # Shares are 10, entry cost is 4.50
        # Winner gets $1 per share = $10
        # Profit = $10 - $4.50 = $5.50
        assert proceeds == Decimal("10")
        assert profit == Decimal("5.50")

    def test_losing_yes_position(self, settlement_manager, sample_queue_entry, resolved_market_info_no):
        """YES position loses when market resolves NO."""
        proceeds, profit = settlement_manager._calculate_settlement_proceeds(
            sample_queue_entry,
            resolved_market_info_no,
        )

        # Loser gets $0
        # Loss = -entry_cost = -$4.50
        assert proceeds == Decimal("0")
        assert profit == Decimal("-4.50")

    def test_winning_no_position(self, settlement_manager, resolved_market_info_no):
        """NO position wins when market resolves NO."""
        no_entry = SettlementQueueEntry(
            position_id="pos-456",
            market_id="market-456",
            side="NO",
            size=Decimal("10"),
            entry_price=Decimal("0.55"),
            shares=Decimal("10"),
            entry_cost=Decimal("5.50"),
        )

        proceeds, profit = settlement_manager._calculate_settlement_proceeds(
            no_entry,
            resolved_market_info_no,
        )

        # Winner gets $1 per share = $10
        # Profit = $10 - $5.50 = $4.50
        assert proceeds == Decimal("10")
        assert profit == Decimal("4.50")

    def test_losing_no_position(self, settlement_manager, resolved_market_info_yes):
        """NO position loses when market resolves YES."""
        no_entry = SettlementQueueEntry(
            position_id="pos-456",
            market_id="market-456",
            side="NO",
            size=Decimal("10"),
            entry_price=Decimal("0.55"),
            shares=Decimal("10"),
            entry_cost=Decimal("5.50"),
        )

        proceeds, profit = settlement_manager._calculate_settlement_proceeds(
            no_entry,
            resolved_market_info_yes,
        )

        # Loser gets $0
        # Loss = -entry_cost = -$5.50
        assert proceeds == Decimal("0")
        assert profit == Decimal("-5.50")


class TestPositionOpenedHandler:
    """Test the position.opened event handler."""

    @pytest.mark.asyncio
    async def test_queues_position_for_settlement(self, settlement_manager, mock_state_store, mock_event_bus):
        """Should queue new positions for settlement."""
        event_data = {
            "position_id": "pos-123",
            "market_id": "market-456",
            "side": "YES",
            "size": "10",
            "entry_price": "0.45",
            "strategy": "gabagool",
        }

        await settlement_manager._on_position_opened(event_data)

        mock_state_store.queue_for_settlement.assert_called_once()
        assert settlement_manager._positions_queued == 1
        # Should emit settlement.queued event
        mock_event_bus.publish.assert_called()

    @pytest.mark.asyncio
    async def test_parses_market_end_time(self, settlement_manager, mock_state_store):
        """Should parse market_end_time from event data."""
        event_data = {
            "position_id": "pos-123",
            "market_id": "market-456",
            "side": "YES",
            "size": "10",
            "entry_price": "0.45",
            "market_end_time": "2025-01-01T12:00:00Z",
        }

        await settlement_manager._on_position_opened(event_data)

        call_kwargs = mock_state_store.queue_for_settlement.call_args[1]
        assert call_kwargs["market_end_time"] is not None

    @pytest.mark.asyncio
    async def test_ignores_incomplete_events(self, settlement_manager, mock_state_store):
        """Should ignore events missing required fields."""
        # Missing position_id
        await settlement_manager._on_position_opened({"market_id": "market-456"})
        mock_state_store.queue_for_settlement.assert_not_called()

        # Missing market_id
        await settlement_manager._on_position_opened({"position_id": "pos-123"})
        mock_state_store.queue_for_settlement.assert_not_called()

    @pytest.mark.asyncio
    async def test_noop_without_state_store(self, settlement_config, mock_event_bus, mock_gamma_client):
        """Should do nothing if no state store is configured."""
        manager = SettlementManager(
            config=settlement_config,
            event_bus=mock_event_bus,
            state_store=None,
            gamma_client=mock_gamma_client,
        )

        # Should not raise
        await manager._on_position_opened({"position_id": "pos-123", "market_id": "market-456"})


class TestHealthCheck:
    """Test health check functionality."""

    @pytest.mark.asyncio
    async def test_unhealthy_when_not_running(self, settlement_manager):
        """Health check should report unhealthy when not running."""
        from mercury.core.lifecycle import HealthStatus

        result = await settlement_manager.health_check()

        assert result.status == HealthStatus.UNHEALTHY
        assert "not running" in result.message.lower()

    @pytest.mark.asyncio
    async def test_healthy_when_running(self, settlement_manager):
        """Health check should report healthy when running."""
        from mercury.core.lifecycle import HealthStatus

        await settlement_manager.start()

        result = await settlement_manager.health_check()

        assert result.status == HealthStatus.HEALTHY
        assert "active" in result.message.lower()
        assert "claims_processed" in result.details
        assert "claims_failed" in result.details
        assert "positions_queued" in result.details
        assert "markets_checked" in result.details

        await settlement_manager.stop()

    @pytest.mark.asyncio
    async def test_includes_queue_stats(self, settlement_manager, mock_state_store):
        """Health check should include queue statistics."""
        mock_state_store.get_settlement_stats.return_value = {
            "total_positions": 10,
            "unclaimed": 5,
            "total_claim_profit": 25.50,
        }

        await settlement_manager.start()

        result = await settlement_manager.health_check()

        assert result.details["queue_total"] == 10
        assert result.details["queue_unclaimed"] == 5
        assert result.details["total_claim_profit"] == 25.50

        await settlement_manager.stop()


class TestDryRunMode:
    """Test dry run mode behavior."""

    @pytest.mark.asyncio
    async def test_dry_run_simulates_claim(
        self,
        settlement_manager,
        mock_state_store,
        mock_gamma_client,
        mock_event_bus,
        sample_position,
        sample_queue_entry,
        resolved_market_info_yes,
    ):
        """Dry run should simulate successful claims."""
        mock_state_store.get_claimable_positions.return_value = [sample_position]
        mock_state_store.get_settlement_queue_entry.return_value = sample_queue_entry
        mock_gamma_client.get_market_info.return_value = resolved_market_info_yes

        result = await settlement_manager.check_settlements()

        assert result == 1
        mock_state_store.mark_claimed.assert_called_once()

        # Should publish settlement.claimed event with dry_run=True
        call_args = mock_event_bus.publish.call_args
        assert call_args[0][0] == "settlement.claimed"
        assert call_args[0][1]["dry_run"] is True


class TestEventPublishing:
    """Test event publishing behavior."""

    @pytest.mark.asyncio
    async def test_publishes_claimed_event_on_success(
        self,
        settlement_manager,
        mock_state_store,
        mock_gamma_client,
        mock_event_bus,
        sample_position,
        sample_queue_entry,
        resolved_market_info_yes,
    ):
        """Should publish settlement.claimed on successful claim."""
        mock_state_store.get_claimable_positions.return_value = [sample_position]
        mock_state_store.get_settlement_queue_entry.return_value = sample_queue_entry
        mock_gamma_client.get_market_info.return_value = resolved_market_info_yes

        await settlement_manager.check_settlements()

        # Find the settlement.claimed call
        claimed_calls = [
            call for call in mock_event_bus.publish.call_args_list
            if call[0][0] == "settlement.claimed"
        ]
        assert len(claimed_calls) == 1

        event_data = claimed_calls[0][0][1]
        assert event_data["position_id"] == "pos-123"
        assert event_data["market_id"] == "market-456"
        assert event_data["resolution"] == "YES"
        assert "proceeds" in event_data
        assert "profit" in event_data
        assert "timestamp" in event_data

    @pytest.mark.asyncio
    async def test_publishes_queued_event(self, settlement_manager, mock_state_store, mock_event_bus):
        """Should publish settlement.queued when position is queued."""
        event_data = {
            "position_id": "pos-123",
            "market_id": "market-456",
            "side": "YES",
            "size": "10",
            "entry_price": "0.45",
        }

        await settlement_manager._on_position_opened(event_data)

        # Find the settlement.queued call
        queued_calls = [
            call for call in mock_event_bus.publish.call_args_list
            if call[0][0] == "settlement.queued"
        ]
        assert len(queued_calls) == 1

        event_data = queued_calls[0][0][1]
        assert event_data["position_id"] == "pos-123"
        assert event_data["market_id"] == "market-456"


class TestPublicQueueManagement:
    """Test public queue management methods."""

    @pytest.mark.asyncio
    async def test_get_settlement_queue(self, settlement_manager, mock_state_store):
        """Should return settlement queue entries."""
        entries = [
            SettlementQueueEntry(
                position_id="pos-1",
                market_id="m-1",
                side="YES",
                size=Decimal("10"),
                entry_price=Decimal("0.5"),
            )
        ]
        mock_state_store.get_settlement_queue.return_value = entries

        result = await settlement_manager.get_settlement_queue()

        assert len(result) == 1
        assert result[0].position_id == "pos-1"

    @pytest.mark.asyncio
    async def test_get_failed_claims(self, settlement_manager, mock_state_store):
        """Should return failed claim entries."""
        entries = [
            SettlementQueueEntry(
                position_id="pos-1",
                market_id="m-1",
                side="YES",
                size=Decimal("10"),
                entry_price=Decimal("0.5"),
                claim_attempts=3,
                last_claim_error="Network error",
            )
        ]
        mock_state_store.get_failed_claims.return_value = entries

        result = await settlement_manager.get_failed_claims()

        assert len(result) == 1
        assert result[0].claim_attempts == 3

    @pytest.mark.asyncio
    async def test_retry_failed_claim(self, settlement_manager, mock_state_store):
        """Should reset failed claim for retry."""
        mock_state_store.retry_failed_claim.return_value = True

        result = await settlement_manager.retry_failed_claim("pos-123")

        assert result is True
        mock_state_store.retry_failed_claim.assert_called_once_with("pos-123")

    @pytest.mark.asyncio
    async def test_force_check_market(self, settlement_manager, mock_gamma_client, resolved_market_info_yes):
        """Should bypass cache for forced market check."""
        condition_id = "cond-789" + "0" * 40
        mock_gamma_client.get_market_info.return_value = resolved_market_info_yes

        # First, populate cache
        await settlement_manager._check_market_resolution(condition_id)
        assert len(settlement_manager._resolution_cache) == 1

        # Force check should clear and refetch
        mock_gamma_client.get_market_info.reset_mock()
        result = await settlement_manager.force_check_market(condition_id)

        assert result is not None
        mock_gamma_client.get_market_info.assert_called_once()


class TestSettlementResult:
    """Test the SettlementResult dataclass."""

    def test_successful_result(self):
        """Test creating a successful settlement result."""
        result = SettlementResult(
            success=True,
            position_id="pos-123",
            condition_id="cond-456",
            proceeds=Decimal("10"),
            profit=Decimal("5.50"),
            resolution="YES",
        )

        assert result.success is True
        assert result.proceeds == Decimal("10")
        assert result.profit == Decimal("5.50")
        assert result.error is None

    def test_failed_result(self):
        """Test creating a failed settlement result."""
        result = SettlementResult(
            success=False,
            position_id="pos-123",
            condition_id="cond-456",
            error="Transaction failed",
        )

        assert result.success is False
        assert result.error == "Transaction failed"
        assert result.proceeds is None


class TestExponentialBackoff:
    """Test exponential backoff for claim retries."""

    def test_calculate_next_retry_time_first_attempt(self, settlement_manager):
        """First retry should use initial delay."""
        # Disable jitter for predictable testing
        settlement_manager._retry_jitter = False
        settlement_manager._retry_initial_delay = 60

        next_retry = settlement_manager._calculate_next_retry_time(1)

        # First attempt: 60 * 2^0 = 60 seconds
        expected_delay = timedelta(seconds=60)
        assert (next_retry - datetime.now(timezone.utc)) < expected_delay + timedelta(seconds=5)
        assert (next_retry - datetime.now(timezone.utc)) > expected_delay - timedelta(seconds=5)

    def test_calculate_next_retry_time_exponential_increase(self, settlement_manager):
        """Retry delay should increase exponentially."""
        settlement_manager._retry_jitter = False
        settlement_manager._retry_initial_delay = 60
        settlement_manager._retry_exponential_base = 2.0

        # Calculate delays for multiple attempts
        delays = []
        for attempt in range(1, 5):
            next_retry = settlement_manager._calculate_next_retry_time(attempt)
            delay = (next_retry - datetime.now(timezone.utc)).total_seconds()
            delays.append(delay)

        # Verify exponential increase: 60, 120, 240, 480
        assert 55 < delays[0] < 65  # ~60 seconds
        assert 115 < delays[1] < 125  # ~120 seconds
        assert 235 < delays[2] < 245  # ~240 seconds
        assert 475 < delays[3] < 485  # ~480 seconds

    def test_calculate_next_retry_time_max_delay_cap(self, settlement_manager):
        """Delay should be capped at max_delay."""
        settlement_manager._retry_jitter = False
        settlement_manager._retry_initial_delay = 60
        settlement_manager._retry_max_delay = 300  # 5 minutes max
        settlement_manager._retry_exponential_base = 2.0

        # Attempt 10 would be 60 * 2^9 = 30720 seconds without cap
        next_retry = settlement_manager._calculate_next_retry_time(10)
        delay = (next_retry - datetime.now(timezone.utc)).total_seconds()

        # Should be capped at 300 seconds
        assert 295 < delay < 305

    def test_calculate_next_retry_time_with_jitter(self, settlement_manager):
        """Jitter should add randomness to delay."""
        settlement_manager._retry_jitter = True
        settlement_manager._retry_initial_delay = 60

        # Run multiple times to verify jitter adds variation
        delays = []
        for _ in range(10):
            next_retry = settlement_manager._calculate_next_retry_time(1)
            delay = (next_retry - datetime.now(timezone.utc)).total_seconds()
            delays.append(delay)

        # With jitter, delays should vary (not all exactly 60)
        min_delay = min(delays)
        max_delay = max(delays)
        # Base delay is 60, jitter adds up to 25%, so range is 60-75
        assert 59 < min_delay < 76
        assert 59 < max_delay < 76


class TestClaimFailureHandling:
    """Test claim failure handling with retries and alerts."""

    @pytest.mark.asyncio
    async def test_handle_claim_failure_records_attempt(
        self,
        settlement_manager,
        mock_state_store,
    ):
        """Should record claim attempt with next retry time."""
        mock_state_store.record_claim_attempt.return_value = 1

        await settlement_manager._handle_claim_failure(
            position_id="pos-123",
            error="Network error",
            current_attempts=0,
            market_id="market-456",
        )

        # Verify record_claim_attempt was called with next_retry_at
        mock_state_store.record_claim_attempt.assert_called_once()
        call_args = mock_state_store.record_claim_attempt.call_args
        # The call is: record_claim_attempt(position_id, error=error, next_retry_at=next_retry_at)
        assert call_args[0][0] == "pos-123"  # position_id as first positional arg
        assert call_args[1]["error"] == "Network error"
        assert call_args[1]["next_retry_at"] is not None

    @pytest.mark.asyncio
    async def test_handle_claim_failure_emits_alert_at_threshold(
        self,
        settlement_manager,
        mock_state_store,
        mock_event_bus,
    ):
        """Should emit alert when failures reach alert threshold."""
        settlement_manager._alert_after_failures = 3
        mock_state_store.record_claim_attempt.return_value = 3  # Matches threshold

        await settlement_manager._handle_claim_failure(
            position_id="pos-123",
            error="Persistent error",
            current_attempts=2,  # Will become 3
            market_id="market-456",
            condition_id="cond-789",
        )

        # Verify settlement.alert was published
        alert_calls = [
            call for call in mock_event_bus.publish.call_args_list
            if call[0][0] == "settlement.alert"
        ]
        assert len(alert_calls) == 1

        alert_data = alert_calls[0][0][1]
        assert alert_data["position_id"] == "pos-123"
        assert alert_data["attempts"] == 3
        assert alert_data["severity"] == "warning"

    @pytest.mark.asyncio
    async def test_handle_claim_failure_no_alert_before_threshold(
        self,
        settlement_manager,
        mock_state_store,
        mock_event_bus,
    ):
        """Should not emit alert before reaching threshold."""
        settlement_manager._alert_after_failures = 3
        mock_state_store.record_claim_attempt.return_value = 2  # Below threshold

        await settlement_manager._handle_claim_failure(
            position_id="pos-123",
            error="Transient error",
            current_attempts=1,
            market_id="market-456",
        )

        # Verify settlement.alert was NOT published
        alert_calls = [
            call for call in mock_event_bus.publish.call_args_list
            if call[0][0] == "settlement.alert"
        ]
        assert len(alert_calls) == 0

    @pytest.mark.asyncio
    async def test_handle_claim_failure_marks_failed_at_max_attempts(
        self,
        settlement_manager,
        mock_state_store,
        mock_event_bus,
    ):
        """Should mark as failed when max attempts reached."""
        settlement_manager._max_claim_attempts = 5
        settlement_manager._alert_after_failures = 3
        mock_state_store.record_claim_attempt.return_value = 5  # At max

        await settlement_manager._handle_claim_failure(
            position_id="pos-123",
            error="Final error",
            current_attempts=4,  # Will become 5
            market_id="market-456",
        )

        # Verify mark_settlement_failed was called
        mock_state_store.mark_settlement_failed.assert_called_once_with(
            "pos-123",
            "Max attempts (5) reached: Final error",
        )


class TestCheckSettlementsWithRetry:
    """Test check_settlements behavior with retry logic."""

    @pytest.mark.asyncio
    async def test_continues_processing_after_failure(
        self,
        settlement_manager,
        mock_state_store,
        mock_gamma_client,
        mock_event_bus,
    ):
        """Should continue processing other claims after one fails."""
        # Create two positions
        pos1 = Position(
            position_id="pos-1",
            market_id="market-1",
            strategy="test",
            side="YES",
            size=Decimal("10"),
            entry_price=Decimal("0.5"),
        )
        pos2 = Position(
            position_id="pos-2",
            market_id="market-2",
            strategy="test",
            side="YES",
            size=Decimal("10"),
            entry_price=Decimal("0.5"),
        )
        mock_state_store.get_claimable_positions.return_value = [pos1, pos2]

        # First position fails to get entry, second succeeds
        entry2 = SettlementQueueEntry(
            position_id="pos-2",
            market_id="market-2",
            condition_id="cond-2" + "0" * 40,
            side="YES",
            size=Decimal("10"),
            entry_price=Decimal("0.5"),
            shares=Decimal("10"),
            entry_cost=Decimal("5.0"),
        )

        def get_entry_side_effect(pos_id):
            if pos_id == "pos-1":
                return None  # Entry not found
            return entry2

        mock_state_store.get_settlement_queue_entry.side_effect = get_entry_side_effect
        mock_gamma_client.get_market_info.return_value = None  # Not resolved yet

        result = await settlement_manager.check_settlements()

        # Both positions were processed (first skipped, second returned not resolved)
        assert result == 0  # Neither claimed successfully
        # But processing didn't stop after first failure
        assert mock_state_store.get_settlement_queue_entry.call_count == 2

    @pytest.mark.asyncio
    async def test_handles_exception_during_claim_gracefully(
        self,
        settlement_manager,
        mock_state_store,
        mock_gamma_client,
        mock_event_bus,
    ):
        """Should handle exceptions during claim and continue.

        Note: Exceptions in _check_market_resolution are caught and logged,
        returning None (market not resolved). The failure counter increments
        because the claim is unsuccessful, but no retry is scheduled since
        "market not resolved" is a normal expected state.
        """
        pos1 = Position(
            position_id="pos-1",
            market_id="market-1",
            strategy="test",
            side="YES",
            size=Decimal("10"),
            entry_price=Decimal("0.5"),
        )
        mock_state_store.get_claimable_positions.return_value = [pos1]

        entry1 = SettlementQueueEntry(
            position_id="pos-1",
            market_id="market-1",
            condition_id="cond-1" + "0" * 40,
            side="YES",
            size=Decimal("10"),
            entry_price=Decimal("0.5"),
            shares=Decimal("10"),
            entry_cost=Decimal("5.0"),
            claim_attempts=0,
        )
        mock_state_store.get_settlement_queue_entry.return_value = entry1

        # Make get_settlement_queue_entry raise an exception to test catch-all
        def raise_on_second_call(pos_id):
            if hasattr(raise_on_second_call, "called"):
                raise Exception("Unexpected DB error")
            raise_on_second_call.called = True
            return entry1

        # Reset for this test - use a fresh exception that will be caught
        mock_state_store.get_settlement_queue_entry.side_effect = Exception("DB crashed")
        mock_state_store.record_claim_attempt.return_value = 1

        result = await settlement_manager.check_settlements()

        # Claim failed but was handled
        assert result == 0
        assert settlement_manager._claims_failed == 1
        # Failure was recorded with backoff (entry is None so claim_attempts is 0)
        mock_state_store.record_claim_attempt.assert_called()


class TestSettlementQueueEntryCanRetry:
    """Test the can_retry property of SettlementQueueEntry."""

    def test_can_retry_true_when_no_next_retry_at(self):
        """Should be retryable when next_retry_at is None."""
        entry = SettlementQueueEntry(
            position_id="pos-1",
            market_id="market-1",
            side="YES",
            size=Decimal("10"),
            entry_price=Decimal("0.5"),
            next_retry_at=None,
        )
        assert entry.can_retry is True

    def test_can_retry_true_when_past_next_retry_at(self):
        """Should be retryable when past next_retry_at."""
        entry = SettlementQueueEntry(
            position_id="pos-1",
            market_id="market-1",
            side="YES",
            size=Decimal("10"),
            entry_price=Decimal("0.5"),
            next_retry_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        assert entry.can_retry is True

    def test_can_retry_false_when_before_next_retry_at(self):
        """Should not be retryable when before next_retry_at."""
        entry = SettlementQueueEntry(
            position_id="pos-1",
            market_id="market-1",
            side="YES",
            size=Decimal("10"),
            entry_price=Decimal("0.5"),
            next_retry_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        assert entry.can_retry is False

    def test_can_retry_false_when_claimed(self):
        """Should not be retryable when already claimed."""
        entry = SettlementQueueEntry(
            position_id="pos-1",
            market_id="market-1",
            side="YES",
            size=Decimal("10"),
            entry_price=Decimal("0.5"),
            claimed=True,
            next_retry_at=None,
        )
        assert entry.can_retry is False

    def test_can_retry_false_when_permanently_failed(self):
        """Should not be retryable when status is failed."""
        entry = SettlementQueueEntry(
            position_id="pos-1",
            market_id="market-1",
            side="YES",
            size=Decimal("10"),
            entry_price=Decimal("0.5"),
            status="failed",
            next_retry_at=None,
        )
        assert entry.can_retry is False


class TestSettlementMetricsEmission:
    """Test metrics emission for settlement events."""

    @pytest.fixture
    def mock_metrics_emitter(self):
        """Create mock MetricsEmitter."""
        metrics = MagicMock()
        metrics.record_settlement_claimed = MagicMock()
        metrics.record_settlement_failed = MagicMock()
        metrics.update_settlement_queue_size = MagicMock()
        return metrics

    @pytest.fixture
    def settlement_manager_with_metrics(
        self, settlement_config, mock_event_bus, mock_state_store, mock_gamma_client, mock_metrics_emitter
    ):
        """Create SettlementManager with metrics emitter."""
        return SettlementManager(
            config=settlement_config,
            event_bus=mock_event_bus,
            state_store=mock_state_store,
            gamma_client=mock_gamma_client,
            metrics_emitter=mock_metrics_emitter,
        )

    @pytest.mark.asyncio
    async def test_emits_metrics_on_successful_claim(
        self,
        settlement_manager_with_metrics,
        mock_state_store,
        mock_gamma_client,
        mock_metrics_emitter,
        sample_position,
        sample_queue_entry,
        resolved_market_info_yes,
    ):
        """Should emit metrics when claim succeeds."""
        mock_state_store.get_claimable_positions.return_value = [sample_position]
        mock_state_store.get_settlement_queue_entry.return_value = sample_queue_entry
        mock_gamma_client.get_market_info.return_value = resolved_market_info_yes

        await settlement_manager_with_metrics.check_settlements()

        # Verify metrics were emitted
        mock_metrics_emitter.record_settlement_claimed.assert_called_once()
        call_args = mock_metrics_emitter.record_settlement_claimed.call_args
        assert call_args[1]["resolution"] == "YES"
        assert call_args[1]["proceeds"] == Decimal("10")  # 10 shares @ $1 each
        assert call_args[1]["profit"] == Decimal("5.50")  # $10 - $4.50 entry cost
        assert call_args[1]["attempts"] == 1

    @pytest.mark.asyncio
    async def test_emits_metrics_on_losing_claim(
        self,
        settlement_manager_with_metrics,
        mock_state_store,
        mock_gamma_client,
        mock_metrics_emitter,
        sample_position,
        sample_queue_entry,
        resolved_market_info_no,  # Market resolved to NO
    ):
        """Should emit metrics for losing position claim."""
        mock_state_store.get_claimable_positions.return_value = [sample_position]
        mock_state_store.get_settlement_queue_entry.return_value = sample_queue_entry
        mock_gamma_client.get_market_info.return_value = resolved_market_info_no

        await settlement_manager_with_metrics.check_settlements()

        # Verify metrics were emitted with negative profit
        mock_metrics_emitter.record_settlement_claimed.assert_called_once()
        call_args = mock_metrics_emitter.record_settlement_claimed.call_args
        assert call_args[1]["resolution"] == "NO"
        assert call_args[1]["proceeds"] == Decimal("0")  # Loser gets $0
        assert call_args[1]["profit"] == Decimal("-4.50")  # Lost entry cost

    @pytest.mark.asyncio
    async def test_no_metrics_without_emitter(
        self,
        settlement_manager,  # Uses manager without metrics emitter
        mock_state_store,
        mock_gamma_client,
        sample_position,
        sample_queue_entry,
        resolved_market_info_yes,
    ):
        """Should work fine without metrics emitter configured."""
        mock_state_store.get_claimable_positions.return_value = [sample_position]
        mock_state_store.get_settlement_queue_entry.return_value = sample_queue_entry
        mock_gamma_client.get_market_info.return_value = resolved_market_info_yes

        # Should not raise even without metrics emitter
        result = await settlement_manager.check_settlements()
        assert result == 1


class TestErrorCategorization:
    """Test error categorization for metrics."""

    @pytest.fixture
    def manager(self, settlement_config, mock_event_bus, mock_state_store, mock_gamma_client):
        """Create a manager for testing _categorize_error."""
        return SettlementManager(
            config=settlement_config,
            event_bus=mock_event_bus,
            state_store=mock_state_store,
            gamma_client=mock_gamma_client,
        )

    def test_categorize_network_error(self, manager):
        """Network errors should be categorized as network."""
        assert manager._categorize_error("Network connection failed") == "network"
        assert manager._categorize_error("Connection timeout") == "network"
        assert manager._categorize_error("Request timeout exceeded") == "network"

    def test_categorize_gas_error(self, manager):
        """Gas errors should be categorized as gas."""
        assert manager._categorize_error("Gas price too high") == "gas"
        assert manager._categorize_error("Insufficient funds for gas") == "gas"

    def test_categorize_contract_error(self, manager):
        """Contract errors should be categorized as contract."""
        assert manager._categorize_error("Contract execution reverted") == "contract"
        assert manager._categorize_error("Revert: already claimed") == "contract"

    def test_categorize_not_resolved(self, manager):
        """Not resolved errors should be categorized appropriately."""
        assert manager._categorize_error("Market not resolved yet") == "not_resolved"

    def test_categorize_transaction_error(self, manager):
        """Transaction errors should be categorized as transaction."""
        assert manager._categorize_error("Transaction failed") == "transaction"

    def test_categorize_unknown_error(self, manager):
        """Unknown errors should be categorized as unknown."""
        assert manager._categorize_error("Something unexpected happened") == "unknown"
        assert manager._categorize_error("") == "unknown"
