"""
Phase 8 Smoke Test: Settlement Manager

Verifies that Phase 8 deliverables work:
- SettlementManager processes queue
- Settlement queue logic works
- CTF redemption works
- Retry logic works
- Events are published
- Metrics are emitted

Run: pytest tests/smoke/test_phase8_settlement.py -v
"""
import pytest
from decimal import Decimal
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock


class TestPhase8SettlementManager:
    """Phase 8 must pass ALL these tests to be considered complete."""

    def test_settlement_manager_importable(self):
        """Verify SettlementManager can be imported."""
        from mercury.services.settlement import SettlementManager
        assert SettlementManager is not None

    @pytest.mark.asyncio
    async def test_settlement_manager_starts_stops(self, mock_config, mock_event_bus):
        """Verify SettlementManager lifecycle works."""
        from mercury.services.settlement import SettlementManager

        mock_state_store = MagicMock()
        mock_state_store.get_claimable_positions = AsyncMock(return_value=[])

        manager = SettlementManager(
            config=mock_config,
            event_bus=mock_event_bus,
            state_store=mock_state_store,
        )

        await manager.start()
        assert manager.is_running

        await manager.stop()
        assert not manager.is_running

    @pytest.mark.asyncio
    async def test_checks_for_claimable_positions(self, mock_config, mock_event_bus):
        """Verify SettlementManager checks for claimable positions."""
        from mercury.services.settlement import SettlementManager
        from mercury.domain.order import Position

        position = Position(
            position_id="test-pos-1",
            market_id="test-market",
            strategy="gabagool",
            side="YES",
            size=Decimal("10.0"),
            entry_price=Decimal("0.50"),
            opened_at=datetime.utcnow(),
        )

        mock_state_store = MagicMock()
        mock_state_store.get_claimable_positions = AsyncMock(return_value=[position])
        mock_state_store.mark_claimed = AsyncMock()

        mock_clob = MagicMock()
        mock_clob.check_market_resolved = AsyncMock(return_value=True)
        mock_clob.get_winning_outcome = AsyncMock(return_value="YES")

        mock_ctf = MagicMock()
        mock_ctf.redeem = AsyncMock(return_value=Decimal("10.0"))

        manager = SettlementManager(
            config=mock_config,
            event_bus=mock_event_bus,
            state_store=mock_state_store,
            clob_client=mock_clob,
            ctf_client=mock_ctf,
        )

        await manager.check_settlements()

        mock_state_store.get_claimable_positions.assert_called_once()

    @pytest.mark.asyncio
    async def test_claims_winning_position(self, mock_config, mock_event_bus):
        """Verify SettlementManager claims winning positions."""
        from mercury.services.settlement import SettlementManager
        from mercury.domain.order import Position

        position = Position(
            position_id="test-pos-1",
            market_id="test-market",
            strategy="gabagool",
            side="YES",
            size=Decimal("10.0"),
            entry_price=Decimal("0.50"),
            opened_at=datetime.utcnow(),
        )

        mock_state_store = MagicMock()
        mock_state_store.get_claimable_positions = AsyncMock(return_value=[position])
        mock_state_store.mark_claimed = AsyncMock()

        mock_clob = MagicMock()
        mock_clob.check_market_resolved = AsyncMock(return_value=True)
        mock_clob.get_winning_outcome = AsyncMock(return_value="YES")

        mock_ctf = MagicMock()
        mock_ctf.redeem = AsyncMock(return_value=Decimal("10.0"))

        manager = SettlementManager(
            config=mock_config,
            event_bus=mock_event_bus,
            state_store=mock_state_store,
            clob_client=mock_clob,
            ctf_client=mock_ctf,
        )

        result = await manager.claim_position(position)

        assert result.success is True
        assert result.proceeds == Decimal("10.0")
        mock_ctf.redeem.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_losing_position(self, mock_config, mock_event_bus):
        """Verify SettlementManager handles losing positions."""
        from mercury.services.settlement import SettlementManager
        from mercury.domain.order import Position

        position = Position(
            position_id="test-pos-1",
            market_id="test-market",
            strategy="gabagool",
            side="YES",
            size=Decimal("10.0"),
            entry_price=Decimal("0.50"),
            opened_at=datetime.utcnow(),
        )

        mock_state_store = MagicMock()
        mock_state_store.mark_claimed = AsyncMock()

        mock_clob = MagicMock()
        mock_clob.check_market_resolved = AsyncMock(return_value=True)
        mock_clob.get_winning_outcome = AsyncMock(return_value="NO")  # We had YES

        mock_ctf = MagicMock()

        manager = SettlementManager(
            config=mock_config,
            event_bus=mock_event_bus,
            state_store=mock_state_store,
            clob_client=mock_clob,
            ctf_client=mock_ctf,
        )

        result = await manager.claim_position(position)

        # Should still succeed but with 0 proceeds
        assert result.success is True
        assert result.proceeds == Decimal("0")
        mock_ctf.redeem.assert_not_called()  # No redemption for losing side

    @pytest.mark.asyncio
    async def test_retry_on_claim_failure(self, mock_config, mock_event_bus):
        """Verify SettlementManager retries on claim failure."""
        from mercury.services.settlement import SettlementManager
        from mercury.domain.order import Position

        position = Position(
            position_id="test-pos-1",
            market_id="test-market",
            strategy="gabagool",
            side="YES",
            size=Decimal("10.0"),
            entry_price=Decimal("0.50"),
            opened_at=datetime.utcnow(),
        )

        call_count = 0

        async def flaky_redeem(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("Transient error")
            return Decimal("10.0")

        mock_state_store = MagicMock()
        mock_state_store.mark_claimed = AsyncMock()

        mock_clob = MagicMock()
        mock_clob.check_market_resolved = AsyncMock(return_value=True)
        mock_clob.get_winning_outcome = AsyncMock(return_value="YES")

        mock_ctf = MagicMock()
        mock_ctf.redeem = AsyncMock(side_effect=flaky_redeem)

        manager = SettlementManager(
            config=mock_config,
            event_bus=mock_event_bus,
            state_store=mock_state_store,
            clob_client=mock_clob,
            ctf_client=mock_ctf,
        )

        result = await manager.claim_position(position)

        assert result.success is True
        assert call_count == 3  # Two failures, then success

    @pytest.mark.asyncio
    async def test_publishes_settlement_events(self, mock_config, mock_event_bus):
        """Verify settlement events are published."""
        from mercury.services.settlement import SettlementManager
        from mercury.domain.order import Position

        position = Position(
            position_id="test-pos-1",
            market_id="test-market",
            strategy="gabagool",
            side="YES",
            size=Decimal("10.0"),
            entry_price=Decimal("0.50"),
            opened_at=datetime.utcnow(),
        )

        mock_state_store = MagicMock()
        mock_state_store.mark_claimed = AsyncMock()

        mock_clob = MagicMock()
        mock_clob.check_market_resolved = AsyncMock(return_value=True)
        mock_clob.get_winning_outcome = AsyncMock(return_value="YES")

        mock_ctf = MagicMock()
        mock_ctf.redeem = AsyncMock(return_value=Decimal("10.0"))

        manager = SettlementManager(
            config=mock_config,
            event_bus=mock_event_bus,
            state_store=mock_state_store,
            clob_client=mock_clob,
            ctf_client=mock_ctf,
        )

        await manager.claim_position(position)

        # Check settlement event was published
        calls = mock_event_bus.publish.call_args_list
        channels = [call[0][0] for call in calls]

        assert any("settlement.claimed" in c for c in channels)

    @pytest.mark.asyncio
    async def test_publishes_failed_event_on_max_retries(self, mock_config, mock_event_bus):
        """Verify failed event published after max retries."""
        from mercury.services.settlement import SettlementManager
        from mercury.domain.order import Position

        position = Position(
            position_id="test-pos-1",
            market_id="test-market",
            strategy="gabagool",
            side="YES",
            size=Decimal("10.0"),
            entry_price=Decimal("0.50"),
            opened_at=datetime.utcnow(),
        )

        mock_state_store = MagicMock()
        mock_state_store.record_claim_failure = AsyncMock()

        mock_clob = MagicMock()
        mock_clob.check_market_resolved = AsyncMock(return_value=True)
        mock_clob.get_winning_outcome = AsyncMock(return_value="YES")

        mock_ctf = MagicMock()
        mock_ctf.redeem = AsyncMock(side_effect=Exception("Permanent error"))

        mock_config.get.side_effect = lambda k, d=None: {
            "settlement.max_retries": 3,
        }.get(k, d)

        manager = SettlementManager(
            config=mock_config,
            event_bus=mock_event_bus,
            state_store=mock_state_store,
            clob_client=mock_clob,
            ctf_client=mock_ctf,
        )

        result = await manager.claim_position(position)

        assert result.success is False

        # Check failed event was published
        calls = mock_event_bus.publish.call_args_list
        channels = [call[0][0] for call in calls]

        assert any("settlement.failed" in c for c in channels)

    def test_ctf_redemption_importable(self):
        """Verify CTF redemption module is importable."""
        from mercury.integrations.chain.ctf import CTFRedemption
        assert CTFRedemption is not None
