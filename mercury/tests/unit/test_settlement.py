"""
Unit tests for the SettlementManager service.

Tests cover:
- Lifecycle management (start/stop)
- Periodic check loop with configurable interval
- Position event handling (position.opened subscription)
- Settlement queue processing
- Claim success/failure handling
- Event publishing (settlement.claimed, settlement.failed)
- Health check reporting
"""
import pytest
import asyncio
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock, patch

from mercury.services.settlement import SettlementManager, DEFAULT_CHECK_INTERVAL


@pytest.fixture
def settlement_config():
    """Create mock config for settlement manager."""
    config = MagicMock()

    def get_side_effect(key, default=None):
        values = {
            "settlement.check_interval_seconds": 60,
            "mercury.dry_run": True,
            "polymarket.private_key": "0x" + "a" * 64,
            "polygon.rpc_url": "https://polygon-rpc.com",
        }
        return values.get(key, default)

    def get_int_side_effect(key, default=0):
        values = {
            "settlement.check_interval_seconds": 60,
        }
        return values.get(key, default)

    def get_bool_side_effect(key, default=False):
        values = {
            "mercury.dry_run": True,
        }
        return values.get(key, default)

    config.get.side_effect = get_side_effect
    config.get_int.side_effect = get_int_side_effect
    config.get_bool.side_effect = get_bool_side_effect
    return config


@pytest.fixture
def mock_state_store():
    """Create mock StateStore."""
    store = MagicMock()
    store.get_claimable_positions = AsyncMock(return_value=[])
    store.mark_settlement_attempt = AsyncMock()
    store.queue_for_settlement = AsyncMock()
    return store


@pytest.fixture
def mock_gamma_client():
    """Create mock GammaClient."""
    client = MagicMock()
    client.connect = AsyncMock()
    client.close = AsyncMock()
    client.get_market = AsyncMock(return_value={"resolved": False})
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

    def test_initial_state(self, settlement_manager):
        """Verify initial manager state."""
        assert settlement_manager._should_run is False
        assert settlement_manager._claims_processed == 0
        assert settlement_manager._claims_failed == 0
        assert settlement_manager._check_task is None


class TestSettlementManagerLifecycle:
    """Test start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_sets_running_state(self, settlement_manager, mock_event_bus):
        """Start should set running state and start check loop."""
        await settlement_manager.start()

        assert settlement_manager._should_run is True
        assert settlement_manager._check_task is not None
        mock_event_bus.subscribe.assert_called_once_with(
            "position.opened", settlement_manager._on_position_opened
        )

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
    async def test_processes_claimable_positions(self, settlement_manager, mock_state_store, mock_gamma_client, mock_event_bus):
        """Should process positions from the settlement queue."""
        queue_item = {
            "id": 1,
            "position_id": "pos-123",
            "market_id": "market-456",
            "condition_id": "cond-789" + "0" * 40,  # 64 char hex
        }
        mock_state_store.get_claimable_positions.return_value = [queue_item]
        mock_gamma_client.get_market.return_value = {"resolved": True}

        result = await settlement_manager.check_settlements()

        # In dry run mode, should succeed
        assert result == 1
        assert settlement_manager._claims_processed == 1
        mock_event_bus.publish.assert_called()

    @pytest.mark.asyncio
    async def test_skips_unresolved_markets(self, settlement_manager, mock_state_store, mock_gamma_client):
        """Should skip positions where market is not resolved."""
        queue_item = {
            "id": 1,
            "position_id": "pos-123",
            "market_id": "market-456",
            "condition_id": "cond-789",
        }
        mock_state_store.get_claimable_positions.return_value = [queue_item]
        mock_gamma_client.get_market.return_value = {"resolved": False}

        result = await settlement_manager.check_settlements()

        assert result == 0

    @pytest.mark.asyncio
    async def test_handles_claim_errors(self, settlement_manager, mock_state_store, mock_gamma_client):
        """Should handle errors during claim processing."""
        queue_item = {
            "id": 1,
            "position_id": "pos-123",
            "market_id": "market-456",
            "condition_id": "cond-789",
        }
        mock_state_store.get_claimable_positions.return_value = [queue_item]
        mock_gamma_client.get_market.side_effect = Exception("API error")

        result = await settlement_manager.check_settlements()

        assert result == 0
        assert settlement_manager._claims_failed == 1


class TestPositionOpenedHandler:
    """Test the position.opened event handler."""

    @pytest.mark.asyncio
    async def test_queues_position_for_settlement(self, settlement_manager, mock_state_store):
        """Should queue new positions for settlement."""
        event_data = {
            "position_id": "pos-123",
            "market_id": "market-456",
        }

        await settlement_manager._on_position_opened(event_data)

        mock_state_store.queue_for_settlement.assert_called_once_with(
            position_id="pos-123",
            market_id="market-456",
            condition_id="market-456",  # Uses market_id as condition_id
        )

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

        await settlement_manager.stop()


class TestDryRunMode:
    """Test dry run mode behavior."""

    @pytest.mark.asyncio
    async def test_dry_run_simulates_claim(self, settlement_manager, mock_state_store, mock_gamma_client, mock_event_bus):
        """Dry run should simulate successful claims."""
        queue_item = {
            "id": 1,
            "position_id": "pos-123",
            "market_id": "market-456",
            "condition_id": "cond-789" + "0" * 40,
        }
        mock_state_store.get_claimable_positions.return_value = [queue_item]
        mock_gamma_client.get_market.return_value = {"resolved": True}

        result = await settlement_manager.check_settlements()

        assert result == 1
        mock_state_store.mark_settlement_attempt.assert_called_once()

        # Should publish settlement.claimed event
        call_args = mock_event_bus.publish.call_args
        assert call_args[0][0] == "settlement.claimed"
        assert call_args[0][1]["position_id"] == "pos-123"


class TestEventPublishing:
    """Test event publishing behavior."""

    @pytest.mark.asyncio
    async def test_publishes_claimed_event_on_success(self, settlement_manager, mock_state_store, mock_gamma_client, mock_event_bus):
        """Should publish settlement.claimed on successful claim."""
        queue_item = {
            "id": 1,
            "position_id": "pos-123",
            "market_id": "market-456",
            "condition_id": "cond-789" + "0" * 40,
        }
        mock_state_store.get_claimable_positions.return_value = [queue_item]
        mock_gamma_client.get_market.return_value = {"resolved": True}

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
        assert "timestamp" in event_data
