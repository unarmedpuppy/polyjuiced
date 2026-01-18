"""
Unit tests for StateStore service.

Tests the SQLite persistence layer for trades, positions, settlement queue,
and daily statistics.
"""
import pytest
from datetime import date, datetime, timezone
from decimal import Decimal


class TestStateStoreImport:
    """Test that StateStore module can be imported."""

    def test_state_store_importable(self):
        """Verify StateStore class can be imported."""
        from mercury.services.state_store import StateStore

        assert StateStore is not None

    def test_trade_model_importable(self):
        """Verify Trade model can be imported."""
        from mercury.services.state_store import Trade

        assert Trade is not None

    def test_position_model_importable(self):
        """Verify Position model can be imported."""
        from mercury.services.state_store import Position

        assert Position is not None

    def test_position_result_importable(self):
        """Verify PositionResult model can be imported."""
        from mercury.services.state_store import PositionResult

        assert PositionResult is not None

    def test_daily_stats_importable(self):
        """Verify DailyStats model can be imported."""
        from mercury.services.state_store import DailyStats

        assert DailyStats is not None

    def test_connection_pool_importable(self):
        """Verify ConnectionPool class can be imported."""
        from mercury.services.state_store import ConnectionPool

        assert ConnectionPool is not None


class TestConnectionPool:
    """Test ConnectionPool functionality."""

    @pytest.mark.asyncio
    async def test_connection_pool_connect_and_close(self, tmp_path):
        """Test basic connect and close."""
        from mercury.services.state_store import ConnectionPool

        db_path = str(tmp_path / "test_pool.db")
        pool = ConnectionPool(db_path)

        assert not pool.is_connected

        await pool.connect()
        assert pool.is_connected

        await pool.close()
        assert not pool.is_connected

    @pytest.mark.asyncio
    async def test_connection_pool_acquire(self, tmp_path):
        """Test acquiring connection from pool."""
        from mercury.services.state_store import ConnectionPool

        db_path = str(tmp_path / "test_pool.db")
        pool = ConnectionPool(db_path)

        await pool.connect()

        conn = await pool.acquire()
        assert conn is not None

        # Should be able to execute queries
        async with conn.execute("SELECT 1") as cursor:
            row = await cursor.fetchone()
            assert row[0] == 1

        await pool.close()

    @pytest.mark.asyncio
    async def test_connection_pool_acquire_raises_when_not_connected(self, tmp_path):
        """Test that acquire raises when not connected."""
        from mercury.services.state_store import ConnectionPool

        db_path = str(tmp_path / "test_pool.db")
        pool = ConnectionPool(db_path)

        with pytest.raises(RuntimeError, match="not connected"):
            await pool.acquire()

    @pytest.mark.asyncio
    async def test_connection_pool_creates_directory(self, tmp_path):
        """Test that pool creates parent directory."""
        from mercury.services.state_store import ConnectionPool

        db_path = str(tmp_path / "subdir" / "deep" / "test.db")
        pool = ConnectionPool(db_path)

        await pool.connect()

        assert (tmp_path / "subdir" / "deep").exists()

        await pool.close()


class TestStateStoreConnection:
    """Test StateStore connection functionality."""

    @pytest.mark.asyncio
    async def test_state_store_connect(self, tmp_path):
        """Test StateStore connects to database."""
        from mercury.services.state_store import StateStore

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)

        assert not store.is_connected

        await store.connect()
        assert store.is_connected

        await store.close()
        assert not store.is_connected

    @pytest.mark.asyncio
    async def test_state_store_creates_tables(self, tmp_path):
        """Test StateStore creates schema on connect."""
        from mercury.services.state_store import StateStore

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)

        await store.connect()

        tables = await store._get_tables()

        assert "trades" in tables
        assert "positions" in tables
        assert "settlement_queue" in tables
        assert "daily_stats" in tables
        assert "fills" in tables
        assert "schema_version" in tables

        await store.close()

    @pytest.mark.asyncio
    async def test_state_store_health_check_healthy(self, tmp_path):
        """Test health check returns healthy when connected."""
        from mercury.services.state_store import StateStore

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)

        await store.connect()

        health = await store.health_check()

        assert health["status"] == "healthy"
        assert "Database connected" in health["message"]

        await store.close()

    @pytest.mark.asyncio
    async def test_state_store_health_check_unhealthy(self, tmp_path):
        """Test health check returns unhealthy when not connected."""
        from mercury.services.state_store import StateStore

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)

        health = await store.health_check()

        assert health["status"] == "unhealthy"
        assert "not connected" in health["message"]


class TestTradeOperations:
    """Test trade CRUD operations."""

    @pytest.mark.asyncio
    async def test_save_and_get_trade(self, tmp_path):
        """Test saving and retrieving a trade."""
        from mercury.services.state_store import StateStore, Trade

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        trade = Trade(
            trade_id="test-trade-1",
            market_id="test-market",
            strategy="gabagool",
            side="YES",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
            cost=Decimal("5.0"),
            timestamp=datetime.now(timezone.utc),
        )

        await store.save_trade(trade)

        retrieved = await store.get_trade("test-trade-1")

        assert retrieved is not None
        assert retrieved.trade_id == "test-trade-1"
        assert retrieved.market_id == "test-market"
        assert retrieved.strategy == "gabagool"
        assert retrieved.side == "YES"
        assert retrieved.size == Decimal("10.0")
        assert retrieved.price == Decimal("0.50")
        assert retrieved.cost == Decimal("5.0")

        await store.close()

    @pytest.mark.asyncio
    async def test_get_trades_with_filters(self, tmp_path):
        """Test getting trades with filters."""
        from mercury.services.state_store import StateStore, Trade

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        # Create multiple trades
        trades = [
            Trade(
                trade_id=f"trade-{i}",
                market_id="market-1" if i < 3 else "market-2",
                strategy="strat-a" if i % 2 == 0 else "strat-b",
                side="YES",
                size=Decimal("10.0"),
                price=Decimal("0.50"),
                cost=Decimal("5.0"),
            )
            for i in range(5)
        ]

        for trade in trades:
            await store.save_trade(trade)

        # Get all trades
        all_trades = await store.get_trades()
        assert len(all_trades) == 5

        # Filter by market
        market_trades = await store.get_trades(market_id="market-1")
        assert len(market_trades) == 3

        # Filter by strategy
        strat_trades = await store.get_trades(strategy="strat-a")
        assert len(strat_trades) == 3

        # Combined filter
        combined = await store.get_trades(market_id="market-1", strategy="strat-a")
        assert len(combined) == 2

        await store.close()

    @pytest.mark.asyncio
    async def test_get_trade_returns_none_for_missing(self, tmp_path):
        """Test get_trade returns None for non-existent trade."""
        from mercury.services.state_store import StateStore

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        result = await store.get_trade("non-existent-trade")

        assert result is None

        await store.close()


class TestPositionOperations:
    """Test position CRUD operations."""

    @pytest.mark.asyncio
    async def test_save_and_get_position(self, tmp_path):
        """Test saving and retrieving a position."""
        from mercury.services.state_store import StateStore, Position

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        position = Position(
            position_id="test-pos-1",
            market_id="test-market",
            strategy="gabagool",
            side="YES",
            size=Decimal("10.0"),
            entry_price=Decimal("0.50"),
        )

        await store.save_position(position)

        retrieved = await store.get_position("test-pos-1")

        assert retrieved is not None
        assert retrieved.position_id == "test-pos-1"
        assert retrieved.market_id == "test-market"
        assert retrieved.strategy == "gabagool"
        assert retrieved.side == "YES"
        assert retrieved.size == Decimal("10.0")
        assert retrieved.entry_price == Decimal("0.50")
        assert retrieved.status == "open"

        await store.close()

    @pytest.mark.asyncio
    async def test_get_open_positions(self, tmp_path):
        """Test getting open positions."""
        from mercury.services.state_store import StateStore, Position

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        # Create open and closed positions
        open_pos = Position(
            position_id="open-pos",
            market_id="test-market",
            strategy="gabagool",
            side="YES",
            size=Decimal("10.0"),
            entry_price=Decimal("0.50"),
            status="open",
        )

        closed_pos = Position(
            position_id="closed-pos",
            market_id="test-market",
            strategy="gabagool",
            side="NO",
            size=Decimal("5.0"),
            entry_price=Decimal("0.45"),
            status="closed",
        )

        await store.save_position(open_pos)
        await store.save_position(closed_pos)

        open_positions = await store.get_open_positions()

        assert len(open_positions) == 1
        assert open_positions[0].position_id == "open-pos"

        await store.close()

    @pytest.mark.asyncio
    async def test_close_position(self, tmp_path):
        """Test closing a position."""
        from mercury.services.state_store import StateStore, Position, PositionResult

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        position = Position(
            position_id="test-pos-close",
            market_id="test-market",
            strategy="gabagool",
            side="YES",
            size=Decimal("10.0"),
            entry_price=Decimal("0.50"),
        )

        await store.save_position(position)

        # Close the position
        result = PositionResult(
            exit_price=Decimal("0.60"),
            realized_pnl=Decimal("1.0"),
            closed_at=datetime.now(timezone.utc),
        )

        await store.close_position("test-pos-close", result)

        # Verify it's closed
        open_positions = await store.get_open_positions()
        assert len([p for p in open_positions if p.position_id == "test-pos-close"]) == 0

        # Verify exit data was saved
        closed_pos = await store.get_position("test-pos-close")
        assert closed_pos.status == "closed"
        assert closed_pos.exit_price == Decimal("0.60")
        assert closed_pos.realized_pnl == Decimal("1.0")

        await store.close()


class TestSettlementQueue:
    """Test settlement queue operations."""

    @pytest.mark.asyncio
    async def test_queue_for_settlement(self, tmp_path):
        """Test adding position to settlement queue."""
        from mercury.services.state_store import StateStore, Position

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        position = Position(
            position_id="settle-pos-1",
            market_id="test-market",
            strategy="gabagool",
            side="YES",
            size=Decimal("10.0"),
            entry_price=Decimal("0.50"),
        )

        await store.queue_for_settlement(position, condition_id="cond-123")

        claimable = await store.get_claimable_positions()

        assert len(claimable) >= 1
        assert any(p.position_id == "settle-pos-1" for p in claimable)

        await store.close()

    @pytest.mark.asyncio
    async def test_mark_claimed(self, tmp_path):
        """Test marking position as claimed."""
        from mercury.services.state_store import StateStore, Position

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        position = Position(
            position_id="settle-claim-1",
            market_id="test-market",
            strategy="gabagool",
            side="YES",
            size=Decimal("10.0"),
            entry_price=Decimal("0.50"),
        )

        await store.queue_for_settlement(position)

        # Verify it's claimable
        claimable = await store.get_claimable_positions()
        assert any(p.position_id == "settle-claim-1" for p in claimable)

        # Mark as claimed
        await store.mark_claimed("settle-claim-1", proceeds=Decimal("10.0"))

        # Verify no longer claimable
        claimable = await store.get_claimable_positions()
        assert not any(p.position_id == "settle-claim-1" for p in claimable)

        await store.close()


class TestDailyStats:
    """Test daily statistics operations."""

    @pytest.mark.asyncio
    async def test_get_daily_stats_creates_empty(self, tmp_path):
        """Test get_daily_stats returns empty stats for new date."""
        from mercury.services.state_store import StateStore

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        today = date.today()
        stats = await store.get_daily_stats(today)

        assert stats is not None
        assert stats.date == today
        assert stats.trade_count == 0
        assert stats.volume_usd == Decimal("0")

        await store.close()

    @pytest.mark.asyncio
    async def test_update_daily_stats(self, tmp_path):
        """Test updating daily stats."""
        from mercury.services.state_store import StateStore, DailyStats

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        today = date.today()

        # Create and save stats
        stats = DailyStats(
            date=today,
            trade_count=5,
            volume_usd=Decimal("100.0"),
            realized_pnl=Decimal("10.5"),
            positions_opened=3,
            positions_closed=2,
        )

        await store.update_daily_stats(stats)

        # Retrieve and verify
        retrieved = await store.get_daily_stats(today)

        assert retrieved.trade_count == 5
        assert retrieved.volume_usd == Decimal("100.0")
        assert retrieved.realized_pnl == Decimal("10.5")
        assert retrieved.positions_opened == 3
        assert retrieved.positions_closed == 2

        await store.close()

    @pytest.mark.asyncio
    async def test_increment_daily_stats(self, tmp_path):
        """Test incrementing daily stats."""
        from mercury.services.state_store import StateStore

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        today = date.today()

        # Increment multiple times
        await store.increment_daily_stats(
            for_date=today, trades=1, volume=Decimal("50.0"), positions_opened=1
        )
        await store.increment_daily_stats(
            for_date=today, trades=2, volume=Decimal("75.0"), positions_closed=1
        )

        # Retrieve and verify
        stats = await store.get_daily_stats(today)

        assert stats.trade_count == 3
        assert stats.volume_usd == Decimal("125.0")
        assert stats.positions_opened == 1
        assert stats.positions_closed == 1

        await store.close()


class TestFillOperations:
    """Test fill recording operations."""

    @pytest.mark.asyncio
    async def test_save_fill(self, tmp_path):
        """Test saving a fill record."""
        from mercury.services.state_store import StateStore

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        await store.save_fill(
            fill_id="fill-1",
            trade_id="trade-1",
            order_id="order-1",
            token_id="token-123",
            side="BUY",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
            fee=Decimal("0.01"),
        )

        # Verify fill was saved by checking table count
        conn = await store._pool.acquire()
        async with conn.execute("SELECT COUNT(*) FROM fills") as cursor:
            row = await cursor.fetchone()
            assert row[0] == 1

        await store.close()


class TestConcurrency:
    """Test concurrent access patterns."""

    @pytest.mark.asyncio
    async def test_concurrent_writes(self, tmp_path):
        """Test concurrent write operations are serialized."""
        import asyncio
        from mercury.services.state_store import StateStore, Trade

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        async def write_trade(i: int):
            trade = Trade(
                trade_id=f"concurrent-trade-{i}",
                market_id="test-market",
                strategy="gabagool",
                side="YES",
                size=Decimal("10.0"),
                price=Decimal("0.50"),
                cost=Decimal("5.0"),
            )
            await store.save_trade(trade)

        # Launch concurrent writes
        await asyncio.gather(*[write_trade(i) for i in range(10)])

        # Verify all trades were saved
        trades = await store.get_trades(limit=20)
        assert len(trades) == 10

        await store.close()

    @pytest.mark.asyncio
    async def test_concurrent_reads(self, tmp_path):
        """Test concurrent read operations."""
        import asyncio
        from mercury.services.state_store import StateStore, Trade

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        # Create test data
        trade = Trade(
            trade_id="read-test-trade",
            market_id="test-market",
            strategy="gabagool",
            side="YES",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
            cost=Decimal("5.0"),
        )
        await store.save_trade(trade)

        async def read_trade():
            return await store.get_trade("read-test-trade")

        # Launch concurrent reads
        results = await asyncio.gather(*[read_trade() for _ in range(10)])

        # Verify all reads succeeded
        assert all(r is not None for r in results)
        assert all(r.trade_id == "read-test-trade" for r in results)

        await store.close()
