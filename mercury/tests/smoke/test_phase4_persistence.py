"""
Phase 4 Smoke Test: State Store and Persistence

Verifies that Phase 4 deliverables work:
- StateStore connects to SQLite
- Trade CRUD operations work
- Position lifecycle works
- Settlement queue works
- Daily stats aggregation works

Run: pytest tests/smoke/test_phase4_persistence.py -v
"""
import pytest
from decimal import Decimal
from datetime import datetime, date


class TestPhase4Persistence:
    """Phase 4 must pass ALL these tests to be considered complete."""

    def test_state_store_importable(self):
        """Verify StateStore can be imported."""
        from mercury.services.state_store import StateStore
        assert StateStore is not None

    @pytest.mark.asyncio
    async def test_state_store_connects(self, tmp_path):
        """Verify StateStore can connect to SQLite."""
        from mercury.services.state_store import StateStore

        db_path = tmp_path / "test.db"
        store = StateStore(db_path=str(db_path))

        await store.connect()
        assert store.is_connected

        await store.close()

    @pytest.mark.asyncio
    async def test_state_store_runs_migrations(self, tmp_path):
        """Verify StateStore creates tables on connect."""
        from mercury.services.state_store import StateStore

        db_path = tmp_path / "test.db"
        store = StateStore(db_path=str(db_path))

        await store.connect()

        # Check tables exist
        tables = await store._get_tables()
        assert "trades" in tables
        assert "positions" in tables
        assert "settlement_queue" in tables
        assert "daily_stats" in tables

        await store.close()

    @pytest.mark.asyncio
    async def test_trade_crud(self, tmp_path):
        """Verify trade CRUD operations work."""
        from mercury.services.state_store import StateStore, Trade

        db_path = tmp_path / "test.db"
        store = StateStore(db_path=str(db_path))
        await store.connect()

        # Create
        trade = Trade(
            trade_id="test-trade-1",
            market_id="test-market",
            strategy="gabagool",
            side="YES",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
            cost=Decimal("5.0"),
            timestamp=datetime.utcnow(),
        )
        await store.save_trade(trade)

        # Read
        retrieved = await store.get_trade("test-trade-1")
        assert retrieved is not None
        assert retrieved.trade_id == "test-trade-1"
        assert retrieved.size == Decimal("10.0")

        # List
        trades = await store.get_trades(since=datetime(2020, 1, 1))
        assert len(trades) >= 1

        await store.close()

    @pytest.mark.asyncio
    async def test_position_lifecycle(self, tmp_path):
        """Verify position lifecycle operations work."""
        from mercury.services.state_store import StateStore, Position, PositionResult

        db_path = tmp_path / "test.db"
        store = StateStore(db_path=str(db_path))
        await store.connect()

        # Create position
        position = Position(
            position_id="test-pos-1",
            market_id="test-market",
            strategy="gabagool",
            side="YES",
            size=Decimal("10.0"),
            entry_price=Decimal("0.50"),
            opened_at=datetime.utcnow(),
        )
        await store.save_position(position)

        # Get open positions
        open_positions = await store.get_open_positions()
        assert len(open_positions) >= 1
        assert open_positions[0].position_id == "test-pos-1"

        # Close position
        result = PositionResult(
            exit_price=Decimal("0.60"),
            realized_pnl=Decimal("1.0"),
            closed_at=datetime.utcnow(),
        )
        await store.close_position("test-pos-1", result)

        # Verify closed
        open_positions = await store.get_open_positions()
        assert len([p for p in open_positions if p.position_id == "test-pos-1"]) == 0

        await store.close()

    @pytest.mark.asyncio
    async def test_settlement_queue(self, tmp_path):
        """Verify settlement queue operations work."""
        from mercury.services.state_store import StateStore, Position

        db_path = tmp_path / "test.db"
        store = StateStore(db_path=str(db_path))
        await store.connect()

        # Queue for settlement
        position = Position(
            position_id="test-settle-1",
            market_id="test-market",
            strategy="gabagool",
            side="YES",
            size=Decimal("10.0"),
            entry_price=Decimal("0.50"),
            opened_at=datetime.utcnow(),
        )
        await store.queue_for_settlement(position)

        # Get claimable
        claimable = await store.get_claimable_positions()
        assert len(claimable) >= 1

        # Mark claimed
        await store.mark_claimed("test-settle-1", proceeds=Decimal("10.0"))

        # Verify no longer claimable
        claimable = await store.get_claimable_positions()
        assert len([p for p in claimable if p.position_id == "test-settle-1"]) == 0

        await store.close()

    @pytest.mark.asyncio
    async def test_daily_stats(self, tmp_path):
        """Verify daily statistics aggregation works."""
        from mercury.services.state_store import StateStore

        db_path = tmp_path / "test.db"
        store = StateStore(db_path=str(db_path))
        await store.connect()

        # Get stats (should create if not exists)
        today = date.today()
        stats = await store.get_daily_stats(today)

        assert stats is not None
        assert stats.date == today
        assert stats.trade_count >= 0

        # Update stats
        stats.trade_count += 1
        stats.volume_usd += Decimal("100.0")
        await store.update_daily_stats(stats)

        # Verify update
        stats = await store.get_daily_stats(today)
        assert stats.trade_count >= 1

        await store.close()
