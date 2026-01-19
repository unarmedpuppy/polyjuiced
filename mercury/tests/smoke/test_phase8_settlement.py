"""
Phase 8 Smoke Test: Settlement Manager

Verifies that Phase 8 deliverables work:
- SettlementManager processes queue
- Settlement queue logic works
- Market resolution checking via Gamma API
- Position state transitions work
- Events are published
- Health check reporting

Run: pytest tests/smoke/test_phase8_settlement.py -v
"""
import pytest
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock

from mercury.services.state_store import Position, SettlementQueueEntry
from mercury.integrations.polymarket.types import MarketInfo


@pytest.fixture
def mock_config_settlement():
    """Create mock config for settlement tests."""
    config = MagicMock()

    def get_side_effect(key, default=None):
        values = {
            "settlement.check_interval_seconds": 60,
            "settlement.resolution_wait_seconds": 0,  # No wait for testing
            "settlement.max_claim_attempts": 5,
            "mercury.dry_run": True,
            "polymarket.private_key": "0x" + "a" * 64,
            "polygon.rpc_url": "https://polygon-rpc.com",
        }
        return values.get(key, default)

    def get_int_side_effect(key, default=0):
        values = {
            "settlement.check_interval_seconds": 60,
            "settlement.resolution_wait_seconds": 0,
            "settlement.max_claim_attempts": 5,
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
def sample_position():
    """Create a sample Position for testing."""
    return Position(
        position_id="test-pos-1",
        market_id="test-market",
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
        position_id="test-pos-1",
        market_id="test-market",
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
def resolved_market_yes():
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
def resolved_market_no():
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


class TestPhase8SettlementManager:
    """Phase 8 must pass ALL these tests to be considered complete."""

    def test_settlement_manager_importable(self):
        """Verify SettlementManager can be imported."""
        from mercury.services.settlement import SettlementManager, SettlementResult
        assert SettlementManager is not None
        assert SettlementResult is not None

    @pytest.mark.asyncio
    async def test_settlement_manager_starts_stops(self, mock_config_settlement, mock_event_bus):
        """Verify SettlementManager lifecycle works."""
        from mercury.services.settlement import SettlementManager

        mock_state_store = MagicMock()
        mock_state_store.get_claimable_positions = AsyncMock(return_value=[])
        mock_state_store.get_settlement_stats = AsyncMock(return_value={})

        mock_gamma = MagicMock()
        mock_gamma.connect = AsyncMock()
        mock_gamma.close = AsyncMock()

        manager = SettlementManager(
            config=mock_config_settlement,
            event_bus=mock_event_bus,
            state_store=mock_state_store,
            gamma_client=mock_gamma,
        )

        await manager.start()
        assert manager._should_run is True

        await manager.stop()
        assert manager._should_run is False

    @pytest.mark.asyncio
    async def test_checks_for_claimable_positions(
        self,
        mock_config_settlement,
        mock_event_bus,
        sample_position,
        sample_queue_entry,
        resolved_market_yes,
    ):
        """Verify SettlementManager checks for claimable positions."""
        from mercury.services.settlement import SettlementManager

        mock_state_store = MagicMock()
        mock_state_store.get_claimable_positions = AsyncMock(return_value=[sample_position])
        mock_state_store.get_settlement_queue_entry = AsyncMock(return_value=sample_queue_entry)
        mock_state_store.mark_claimed = AsyncMock()
        mock_state_store.get_settlement_stats = AsyncMock(return_value={})

        mock_gamma = MagicMock()
        mock_gamma.connect = AsyncMock()
        mock_gamma.close = AsyncMock()
        mock_gamma.get_market_info = AsyncMock(return_value=resolved_market_yes)

        manager = SettlementManager(
            config=mock_config_settlement,
            event_bus=mock_event_bus,
            state_store=mock_state_store,
            gamma_client=mock_gamma,
        )

        result = await manager.check_settlements()

        mock_state_store.get_claimable_positions.assert_called_once()
        assert result == 1  # One position processed

    @pytest.mark.asyncio
    async def test_claims_winning_position(
        self,
        mock_config_settlement,
        mock_event_bus,
        sample_position,
        sample_queue_entry,
        resolved_market_yes,
    ):
        """Verify SettlementManager claims winning positions."""
        from mercury.services.settlement import SettlementManager

        mock_state_store = MagicMock()
        mock_state_store.get_claimable_positions = AsyncMock(return_value=[sample_position])
        mock_state_store.get_settlement_queue_entry = AsyncMock(return_value=sample_queue_entry)
        mock_state_store.mark_claimed = AsyncMock()
        mock_state_store.get_settlement_stats = AsyncMock(return_value={})

        mock_gamma = MagicMock()
        mock_gamma.connect = AsyncMock()
        mock_gamma.close = AsyncMock()
        mock_gamma.get_market_info = AsyncMock(return_value=resolved_market_yes)

        manager = SettlementManager(
            config=mock_config_settlement,
            event_bus=mock_event_bus,
            state_store=mock_state_store,
            gamma_client=mock_gamma,
        )

        result = await manager.check_settlements()

        assert result == 1
        mock_state_store.mark_claimed.assert_called_once()

        # Check proceeds calculation (YES wins, entry cost 4.50, 10 shares = $10 proceeds)
        call_args = mock_state_store.mark_claimed.call_args
        assert call_args[0][0] == "test-pos-1"  # position_id
        assert call_args[0][1] == Decimal("10")  # proceeds (shares = $10)
        assert call_args[0][2] == Decimal("5.50")  # profit ($10 - $4.50)

    @pytest.mark.asyncio
    async def test_handles_losing_position(
        self,
        mock_config_settlement,
        mock_event_bus,
        sample_position,
        sample_queue_entry,
        resolved_market_no,
    ):
        """Verify SettlementManager handles losing positions."""
        from mercury.services.settlement import SettlementManager

        mock_state_store = MagicMock()
        mock_state_store.get_claimable_positions = AsyncMock(return_value=[sample_position])
        mock_state_store.get_settlement_queue_entry = AsyncMock(return_value=sample_queue_entry)
        mock_state_store.mark_claimed = AsyncMock()
        mock_state_store.get_settlement_stats = AsyncMock(return_value={})

        mock_gamma = MagicMock()
        mock_gamma.connect = AsyncMock()
        mock_gamma.close = AsyncMock()
        mock_gamma.get_market_info = AsyncMock(return_value=resolved_market_no)

        manager = SettlementManager(
            config=mock_config_settlement,
            event_bus=mock_event_bus,
            state_store=mock_state_store,
            gamma_client=mock_gamma,
        )

        result = await manager.check_settlements()

        assert result == 1  # Still processes (losing is still a settlement)
        mock_state_store.mark_claimed.assert_called_once()

        # Check proceeds calculation (YES loses when market resolves NO)
        call_args = mock_state_store.mark_claimed.call_args
        assert call_args[0][1] == Decimal("0")  # proceeds = $0
        assert call_args[0][2] == Decimal("-4.50")  # profit = -entry_cost

    @pytest.mark.asyncio
    async def test_skips_unresolved_market(
        self,
        mock_config_settlement,
        mock_event_bus,
        sample_position,
        sample_queue_entry,
    ):
        """Verify SettlementManager skips unresolved markets."""
        from mercury.services.settlement import SettlementManager

        mock_state_store = MagicMock()
        mock_state_store.get_claimable_positions = AsyncMock(return_value=[sample_position])
        mock_state_store.get_settlement_queue_entry = AsyncMock(return_value=sample_queue_entry)
        mock_state_store.mark_claimed = AsyncMock()
        mock_state_store.get_settlement_stats = AsyncMock(return_value={})

        mock_gamma = MagicMock()
        mock_gamma.connect = AsyncMock()
        mock_gamma.close = AsyncMock()
        mock_gamma.get_market_info = AsyncMock(return_value=None)  # Not resolved

        manager = SettlementManager(
            config=mock_config_settlement,
            event_bus=mock_event_bus,
            state_store=mock_state_store,
            gamma_client=mock_gamma,
        )

        result = await manager.check_settlements()

        assert result == 0  # No claims processed
        mock_state_store.mark_claimed.assert_not_called()

    @pytest.mark.asyncio
    async def test_publishes_settlement_events(
        self,
        mock_config_settlement,
        mock_event_bus,
        sample_position,
        sample_queue_entry,
        resolved_market_yes,
    ):
        """Verify settlement events are published."""
        from mercury.services.settlement import SettlementManager

        mock_state_store = MagicMock()
        mock_state_store.get_claimable_positions = AsyncMock(return_value=[sample_position])
        mock_state_store.get_settlement_queue_entry = AsyncMock(return_value=sample_queue_entry)
        mock_state_store.mark_claimed = AsyncMock()
        mock_state_store.get_settlement_stats = AsyncMock(return_value={})

        mock_gamma = MagicMock()
        mock_gamma.connect = AsyncMock()
        mock_gamma.close = AsyncMock()
        mock_gamma.get_market_info = AsyncMock(return_value=resolved_market_yes)

        manager = SettlementManager(
            config=mock_config_settlement,
            event_bus=mock_event_bus,
            state_store=mock_state_store,
            gamma_client=mock_gamma,
        )

        await manager.check_settlements()

        # Check settlement event was published
        calls = mock_event_bus.publish.call_args_list
        channels = [call[0][0] for call in calls]

        assert "settlement.claimed" in channels

        # Verify event data
        claimed_call = next(c for c in calls if c[0][0] == "settlement.claimed")
        event_data = claimed_call[0][1]
        assert event_data["position_id"] == "test-pos-1"
        assert event_data["resolution"] == "YES"
        assert "proceeds" in event_data
        assert "profit" in event_data

    @pytest.mark.asyncio
    async def test_queues_position_on_event(
        self,
        mock_config_settlement,
        mock_event_bus,
    ):
        """Verify positions are queued when position.opened event is received."""
        from mercury.services.settlement import SettlementManager

        mock_state_store = MagicMock()
        mock_state_store.get_claimable_positions = AsyncMock(return_value=[])
        mock_state_store.queue_for_settlement = AsyncMock()
        mock_state_store.get_settlement_stats = AsyncMock(return_value={})

        mock_gamma = MagicMock()
        mock_gamma.connect = AsyncMock()
        mock_gamma.close = AsyncMock()

        manager = SettlementManager(
            config=mock_config_settlement,
            event_bus=mock_event_bus,
            state_store=mock_state_store,
            gamma_client=mock_gamma,
        )

        # Simulate position.opened event
        await manager._on_position_opened({
            "position_id": "new-pos-1",
            "market_id": "test-market",
            "side": "YES",
            "size": "10",
            "entry_price": "0.50",
        })

        mock_state_store.queue_for_settlement.assert_called_once()
        assert manager._positions_queued == 1

    @pytest.mark.asyncio
    async def test_health_check_reports_stats(
        self,
        mock_config_settlement,
        mock_event_bus,
    ):
        """Verify health check includes settlement statistics."""
        from mercury.services.settlement import SettlementManager
        from mercury.core.lifecycle import HealthStatus

        mock_state_store = MagicMock()
        mock_state_store.get_claimable_positions = AsyncMock(return_value=[])
        mock_state_store.get_settlement_stats = AsyncMock(return_value={
            "total_positions": 10,
            "unclaimed": 5,
            "total_claim_profit": Decimal("25.50"),
        })

        mock_gamma = MagicMock()
        mock_gamma.connect = AsyncMock()
        mock_gamma.close = AsyncMock()

        manager = SettlementManager(
            config=mock_config_settlement,
            event_bus=mock_event_bus,
            state_store=mock_state_store,
            gamma_client=mock_gamma,
        )

        await manager.start()

        result = await manager.health_check()

        assert result.status == HealthStatus.HEALTHY
        assert result.details["queue_total"] == 10
        assert result.details["queue_unclaimed"] == 5

        await manager.stop()

    def test_settlement_result_dataclass(self):
        """Verify SettlementResult dataclass works."""
        from mercury.services.settlement import SettlementResult

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
        assert result.resolution == "YES"

    def test_state_store_settlement_queue_entry(self):
        """Verify SettlementQueueEntry dataclass works."""
        entry = SettlementQueueEntry(
            position_id="pos-123",
            market_id="market-456",
            side="YES",
            size=Decimal("10"),
            entry_price=Decimal("0.45"),
            shares=Decimal("10"),
            entry_cost=Decimal("4.50"),
        )

        assert entry.cost_basis == Decimal("4.50")
        assert entry.is_failed is False
