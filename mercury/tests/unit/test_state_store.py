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

    def test_migration_runner_importable(self):
        """Verify MigrationRunner class can be imported."""
        from mercury.services.state_store import MigrationRunner

        assert MigrationRunner is not None

    def test_fill_record_importable(self):
        """Verify FillRecord model can be imported."""
        from mercury.services.state_store import FillRecord

        assert FillRecord is not None

    def test_trade_telemetry_importable(self):
        """Verify TradeTelemetry model can be imported."""
        from mercury.services.state_store import TradeTelemetry

        assert TradeTelemetry is not None

    def test_rebalance_trade_importable(self):
        """Verify RebalanceTrade model can be imported."""
        from mercury.services.state_store import RebalanceTrade

        assert RebalanceTrade is not None

    def test_circuit_breaker_state_importable(self):
        """Verify CircuitBreakerState model can be imported."""
        from mercury.services.state_store import CircuitBreakerState

        assert CircuitBreakerState is not None

    def test_realized_pnl_entry_importable(self):
        """Verify RealizedPnlEntry model can be imported."""
        from mercury.services.state_store import RealizedPnlEntry

        assert RealizedPnlEntry is not None


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
        # New tables from migration
        assert "fill_records" in tables
        assert "trade_telemetry" in tables
        assert "rebalance_trades" in tables
        assert "circuit_breaker_state" in tables
        assert "realized_pnl_ledger" in tables

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
        assert health["schema_version"] == 2

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
    async def test_save_trade_with_enhanced_fields(self, tmp_path):
        """Test saving trade with enhanced fields from legacy."""
        from mercury.services.state_store import StateStore, Trade

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        trade = Trade(
            trade_id="test-trade-enhanced",
            market_id="test-market",
            strategy="gabagool",
            side="YES",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
            cost=Decimal("5.0"),
            # Enhanced fields
            condition_id="cond-123",
            asset="BTC",
            yes_price=Decimal("0.52"),
            no_price=Decimal("0.48"),
            spread=Decimal("0.04"),
            expected_profit=Decimal("0.10"),
            hedge_ratio=Decimal("1.0"),
            execution_status="FILLED",
            yes_liquidity_at_price=Decimal("100.0"),
            no_liquidity_at_price=Decimal("120.0"),
        )

        await store.save_trade(trade)

        retrieved = await store.get_trade("test-trade-enhanced")

        assert retrieved is not None
        assert retrieved.condition_id == "cond-123"
        assert retrieved.asset == "BTC"
        assert retrieved.yes_price == Decimal("0.52")
        assert retrieved.no_price == Decimal("0.48")
        assert retrieved.spread == Decimal("0.04")
        assert retrieved.expected_profit == Decimal("0.10")
        assert retrieved.hedge_ratio == Decimal("1.0")
        assert retrieved.execution_status == "FILLED"
        assert retrieved.yes_liquidity_at_price == Decimal("100.0")
        assert retrieved.no_liquidity_at_price == Decimal("120.0")

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

    @pytest.mark.asyncio
    async def test_resolve_trade(self, tmp_path):
        """Test resolving a trade with profit."""
        from mercury.services.state_store import StateStore, Trade

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        trade = Trade(
            trade_id="trade-to-resolve",
            market_id="test-market",
            strategy="gabagool",
            side="YES",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
            cost=Decimal("5.0"),
        )
        await store.save_trade(trade)

        # Resolve the trade
        await store.resolve_trade(
            trade_id="trade-to-resolve",
            actual_profit=Decimal("2.50"),
            status="resolved",
        )

        retrieved = await store.get_trade("trade-to-resolve")
        assert retrieved.status == "resolved"
        assert retrieved.actual_profit == Decimal("2.50")
        assert retrieved.resolved_at is not None

        await store.close()

    @pytest.mark.asyncio
    async def test_get_trades_by_market(self, tmp_path):
        """Test get_trades_by_market convenience method."""
        from mercury.services.state_store import StateStore, Trade

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        # Create trades for different markets
        for i in range(5):
            trade = Trade(
                trade_id=f"market-trade-{i}",
                market_id="market-A" if i < 3 else "market-B",
                strategy="gabagool",
                side="YES",
                size=Decimal("10.0"),
                price=Decimal("0.50"),
                cost=Decimal("5.0"),
            )
            await store.save_trade(trade)

        # Get trades for market-A
        market_a_trades = await store.get_trades_by_market("market-A")
        assert len(market_a_trades) == 3
        assert all(t.market_id == "market-A" for t in market_a_trades)

        # Get trades for market-B
        market_b_trades = await store.get_trades_by_market("market-B")
        assert len(market_b_trades) == 2
        assert all(t.market_id == "market-B" for t in market_b_trades)

        # Get trades for non-existent market
        empty_trades = await store.get_trades_by_market("market-C")
        assert len(empty_trades) == 0

        await store.close()

    @pytest.mark.asyncio
    async def test_get_trades_by_market_with_dry_run_filter(self, tmp_path):
        """Test get_trades_by_market with dry run filter."""
        from mercury.services.state_store import StateStore, Trade

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        # Create regular and dry run trades
        for i in range(4):
            trade = Trade(
                trade_id=f"dry-run-test-{i}",
                market_id="test-market",
                strategy="gabagool",
                side="YES",
                size=Decimal("10.0"),
                price=Decimal("0.50"),
                cost=Decimal("5.0"),
                dry_run=(i % 2 == 0),  # 0, 2 are dry runs
            )
            await store.save_trade(trade)

        # Get all trades
        all_trades = await store.get_trades_by_market("test-market")
        assert len(all_trades) == 4

        # Exclude dry runs
        real_trades = await store.get_trades_by_market("test-market", exclude_dry_runs=True)
        assert len(real_trades) == 2
        assert all(not t.dry_run for t in real_trades)

        await store.close()

    @pytest.mark.asyncio
    async def test_get_trade_with_details(self, tmp_path):
        """Test get_trade_with_details returns trade with fills and telemetry."""
        from mercury.services.state_store import StateStore, Trade, TradeTelemetry

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        # Create a trade
        trade = Trade(
            trade_id="detail-trade-1",
            market_id="test-market",
            strategy="gabagool",
            side="YES",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
            cost=Decimal("5.0"),
        )
        await store.save_trade(trade)

        # Add fills
        await store.save_fill(
            fill_id="fill-1",
            trade_id="detail-trade-1",
            order_id="order-1",
            token_id="token-123",
            side="BUY",
            size=Decimal("5.0"),
            price=Decimal("0.50"),
            fee=Decimal("0.01"),
        )
        await store.save_fill(
            fill_id="fill-2",
            trade_id="detail-trade-1",
            order_id="order-1",
            token_id="token-123",
            side="BUY",
            size=Decimal("5.0"),
            price=Decimal("0.51"),
            fee=Decimal("0.01"),
        )

        # Add telemetry
        telemetry = TradeTelemetry(
            trade_id="detail-trade-1",
            opportunity_spread=Decimal("0.05"),
            execution_latency_ms=Decimal("75.5"),
        )
        await store.save_trade_telemetry(telemetry)

        # Get trade with details
        details = await store.get_trade_with_details("detail-trade-1")

        assert details is not None
        assert details["trade"].trade_id == "detail-trade-1"
        assert len(details["fills"]) == 2
        assert details["fills"][0]["fill_id"] == "fill-1"
        assert details["fills"][1]["fill_id"] == "fill-2"
        assert details["telemetry"] is not None
        assert details["telemetry"].opportunity_spread == Decimal("0.05")
        assert details["telemetry"].execution_latency_ms == Decimal("75.5")

        await store.close()

    @pytest.mark.asyncio
    async def test_get_trade_with_details_returns_none_for_missing(self, tmp_path):
        """Test get_trade_with_details returns None for non-existent trade."""
        from mercury.services.state_store import StateStore

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        details = await store.get_trade_with_details("non-existent-trade")
        assert details is None

        await store.close()

    @pytest.mark.asyncio
    async def test_get_trade_with_details_includes_rebalance_trades(self, tmp_path):
        """Test get_trade_with_details includes rebalance trades."""
        from mercury.services.state_store import StateStore, Trade, RebalanceTrade

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        # Create a trade
        trade = Trade(
            trade_id="rebalance-detail-trade",
            market_id="test-market",
            strategy="gabagool",
            side="YES",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
            cost=Decimal("5.0"),
        )
        await store.save_trade(trade)

        # Add rebalance trades
        rebalance = RebalanceTrade(
            trade_id="rebalance-detail-trade",
            action="SELL_YES",
            shares=Decimal("2.0"),
            price=Decimal("0.55"),
            status="SUCCESS",
            filled_shares=Decimal("2.0"),
            profit=Decimal("0.10"),
        )
        await store.save_rebalance_trade(rebalance)

        # Get trade with details
        details = await store.get_trade_with_details("rebalance-detail-trade")

        assert details is not None
        assert len(details["rebalance_trades"]) == 1
        assert details["rebalance_trades"][0].action == "SELL_YES"
        assert details["rebalance_trades"][0].profit == Decimal("0.10")

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
    async def test_queue_for_settlement_with_enhanced_fields(self, tmp_path):
        """Test settlement queue with enhanced fields from legacy."""
        from mercury.services.state_store import StateStore, Position

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        position = Position(
            position_id="settle-pos-enhanced",
            market_id="test-market",
            strategy="gabagool",
            side="YES",
            size=Decimal("10.0"),
            entry_price=Decimal("0.50"),
        )

        await store.queue_for_settlement(
            position,
            condition_id="cond-123",
            token_id="token-abc",
            asset="BTC",
            market_end_time=datetime.now(timezone.utc),
        )

        claimable = await store.get_claimable_positions()
        assert any(p.position_id == "settle-pos-enhanced" for p in claimable)

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
        await store.mark_claimed("settle-claim-1", proceeds=Decimal("10.0"), profit=Decimal("5.0"))

        # Verify no longer claimable
        claimable = await store.get_claimable_positions()
        assert not any(p.position_id == "settle-claim-1" for p in claimable)

        await store.close()

    @pytest.mark.asyncio
    async def test_record_claim_attempt(self, tmp_path):
        """Test recording claim attempt."""
        from mercury.services.state_store import StateStore, Position

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        position = Position(
            position_id="settle-attempt-1",
            market_id="test-market",
            strategy="gabagool",
            side="YES",
            size=Decimal("10.0"),
            entry_price=Decimal("0.50"),
        )

        await store.queue_for_settlement(position)
        await store.record_claim_attempt("settle-attempt-1", error="Network timeout")

        # Should still be claimable with fewer than max attempts
        claimable = await store.get_claimable_positions(max_attempts=5)
        assert any(p.position_id == "settle-attempt-1" for p in claimable)

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
    async def test_update_daily_stats_with_enhanced_fields(self, tmp_path):
        """Test updating daily stats with enhanced fields from legacy."""
        from mercury.services.state_store import StateStore, DailyStats

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        today = date.today()

        stats = DailyStats(
            date=today,
            trade_count=5,
            volume_usd=Decimal("100.0"),
            realized_pnl=Decimal("10.5"),
            positions_opened=3,
            positions_closed=2,
            wins=3,
            losses=2,
            exposure=Decimal("50.0"),
            opportunities_detected=20,
            opportunities_executed=5,
        )

        await store.update_daily_stats(stats)

        retrieved = await store.get_daily_stats(today)

        assert retrieved.wins == 3
        assert retrieved.losses == 2
        assert retrieved.exposure == Decimal("50.0")
        assert retrieved.opportunities_detected == 20
        assert retrieved.opportunities_executed == 5

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


class TestFillRecords:
    """Test detailed fill records for slippage analysis."""

    @pytest.mark.asyncio
    async def test_save_fill_record(self, tmp_path):
        """Test saving a detailed fill record."""
        from mercury.services.state_store import StateStore, FillRecord

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        record = FillRecord(
            token_id="token-123",
            condition_id="cond-123",
            asset="BTC",
            side="BUY",
            intended_size=Decimal("10.0"),
            filled_size=Decimal("9.5"),
            intended_price=Decimal("0.50"),
            actual_avg_price=Decimal("0.51"),
            time_to_fill_ms=150,
            slippage=Decimal("0.02"),
            pre_fill_depth=Decimal("100.0"),
            post_fill_depth=Decimal("90.5"),
        )

        record_id = await store.save_fill_record(record)
        assert record_id > 0

        await store.close()

    @pytest.mark.asyncio
    async def test_get_fill_records(self, tmp_path):
        """Test retrieving fill records."""
        from mercury.services.state_store import StateStore, FillRecord

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        # Save some records
        for i in range(3):
            record = FillRecord(
                token_id="token-123",
                condition_id="cond-123",
                asset="BTC" if i < 2 else "ETH",
                side="BUY",
                intended_size=Decimal("10.0"),
                filled_size=Decimal("9.5"),
                intended_price=Decimal("0.50"),
                actual_avg_price=Decimal("0.51"),
                time_to_fill_ms=150 + i * 10,
                slippage=Decimal("0.02"),
                pre_fill_depth=Decimal("100.0"),
            )
            await store.save_fill_record(record)

        # Get all
        records = await store.get_fill_records()
        assert len(records) == 3

        # Filter by asset
        btc_records = await store.get_fill_records(asset="BTC")
        assert len(btc_records) == 2

        await store.close()

    @pytest.mark.asyncio
    async def test_get_slippage_stats(self, tmp_path):
        """Test slippage statistics aggregation."""
        from mercury.services.state_store import StateStore, FillRecord

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        # Save some records
        for i in range(3):
            record = FillRecord(
                token_id="token-123",
                condition_id="cond-123",
                asset="BTC",
                side="BUY",
                intended_size=Decimal("10.0"),
                filled_size=Decimal("9.5"),
                intended_price=Decimal("0.50"),
                actual_avg_price=Decimal("0.51"),
                time_to_fill_ms=100 + i * 50,
                slippage=Decimal(str(0.01 + i * 0.01)),  # 0.01, 0.02, 0.03
                pre_fill_depth=Decimal("100.0"),
            )
            await store.save_fill_record(record)

        stats = await store.get_slippage_stats(lookback_minutes=60)

        assert stats["fill_count"] == 3
        assert stats["avg_slippage"] is not None
        assert stats["total_volume"] > 0

        await store.close()


class TestTradeTelemetry:
    """Test trade telemetry operations."""

    @pytest.mark.asyncio
    async def test_save_and_get_trade_telemetry(self, tmp_path):
        """Test saving and retrieving trade telemetry."""
        from mercury.services.state_store import StateStore, TradeTelemetry

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        telemetry = TradeTelemetry(
            trade_id="trade-tel-1",
            opportunity_detected_at=datetime.now(timezone.utc),
            opportunity_spread=Decimal("0.05"),
            opportunity_yes_price=Decimal("0.52"),
            opportunity_no_price=Decimal("0.47"),
            execution_latency_ms=Decimal("50.5"),
            initial_hedge_ratio=Decimal("1.0"),
            rebalance_attempts=2,
        )

        await store.save_trade_telemetry(telemetry)

        retrieved = await store.get_trade_telemetry("trade-tel-1")

        assert retrieved is not None
        assert retrieved.trade_id == "trade-tel-1"
        assert retrieved.opportunity_spread == Decimal("0.05")
        assert retrieved.opportunity_yes_price == Decimal("0.52")
        assert retrieved.execution_latency_ms == Decimal("50.5")
        assert retrieved.rebalance_attempts == 2

        await store.close()


class TestRebalanceTrades:
    """Test rebalance trade operations."""

    @pytest.mark.asyncio
    async def test_save_and_get_rebalance_trades(self, tmp_path):
        """Test saving and retrieving rebalance trades."""
        from mercury.services.state_store import StateStore, RebalanceTrade

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        rebalance = RebalanceTrade(
            trade_id="parent-trade-1",
            action="SELL_YES",
            shares=Decimal("5.0"),
            price=Decimal("0.60"),
            status="SUCCESS",
            filled_shares=Decimal("5.0"),
            profit=Decimal("0.50"),
        )

        record_id = await store.save_rebalance_trade(rebalance)
        assert record_id > 0

        # Save another
        rebalance2 = RebalanceTrade(
            trade_id="parent-trade-1",
            action="BUY_NO",
            shares=Decimal("3.0"),
            price=Decimal("0.45"),
            status="PARTIAL",
            filled_shares=Decimal("2.5"),
            error="Insufficient liquidity",
        )
        await store.save_rebalance_trade(rebalance2)

        # Retrieve
        trades = await store.get_rebalance_trades("parent-trade-1")
        assert len(trades) == 2
        assert trades[0].action == "SELL_YES"
        assert trades[1].action == "BUY_NO"
        assert trades[1].error == "Insufficient liquidity"

        await store.close()


class TestCircuitBreaker:
    """Test circuit breaker operations."""

    @pytest.mark.asyncio
    async def test_get_circuit_breaker_state(self, tmp_path):
        """Test getting circuit breaker state."""
        from mercury.services.state_store import StateStore

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        state = await store.get_circuit_breaker_state()

        assert state is not None
        assert state.realized_pnl == Decimal("0")
        assert state.circuit_breaker_hit is False
        assert state.total_trades_today == 0

        await store.close()

    @pytest.mark.asyncio
    async def test_record_realized_pnl(self, tmp_path):
        """Test recording realized P&L."""
        from mercury.services.state_store import StateStore

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        # Record profit
        state = await store.record_realized_pnl(
            trade_id="trade-pnl-1",
            pnl_amount=Decimal("10.0"),
            pnl_type="resolution",
            max_daily_loss=Decimal("100.0"),
        )

        assert state.realized_pnl == Decimal("10.0")
        assert state.total_trades_today == 1
        assert state.circuit_breaker_hit is False

        await store.close()

    @pytest.mark.asyncio
    async def test_circuit_breaker_triggers(self, tmp_path):
        """Test circuit breaker triggers on loss limit."""
        from mercury.services.state_store import StateStore

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        # Record large loss
        state = await store.record_realized_pnl(
            trade_id="trade-loss-1",
            pnl_amount=Decimal("-150.0"),
            pnl_type="resolution",
            max_daily_loss=Decimal("100.0"),
        )

        assert state.realized_pnl == Decimal("-150.0")
        assert state.circuit_breaker_hit is True
        assert state.hit_reason is not None
        assert "Daily loss limit exceeded" in state.hit_reason

        await store.close()

    @pytest.mark.asyncio
    async def test_reset_circuit_breaker(self, tmp_path):
        """Test manually resetting circuit breaker."""
        from mercury.services.state_store import StateStore

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        # Trigger circuit breaker
        await store.record_realized_pnl(
            trade_id="trade-loss-reset",
            pnl_amount=Decimal("-150.0"),
            pnl_type="resolution",
            max_daily_loss=Decimal("100.0"),
        )

        # Reset
        await store.reset_circuit_breaker(reason="Testing reset")

        state = await store.get_circuit_breaker_state()
        assert state.circuit_breaker_hit is False
        assert state.realized_pnl == Decimal("-150.0")  # P&L preserved

        await store.close()


class TestRealizedPnlLedger:
    """Test realized P&L ledger operations."""

    @pytest.mark.asyncio
    async def test_get_total_realized_pnl(self, tmp_path):
        """Test getting total realized P&L."""
        from mercury.services.state_store import StateStore

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        # Record some P&L
        await store.record_realized_pnl("trade-1", Decimal("10.0"), "resolution", Decimal("100.0"))
        await store.record_realized_pnl("trade-2", Decimal("-5.0"), "settlement", Decimal("100.0"))
        await store.record_realized_pnl("trade-3", Decimal("3.0"), "rebalance", Decimal("100.0"))

        total = await store.get_total_realized_pnl()
        assert total == Decimal("8.0")

        await store.close()

    @pytest.mark.asyncio
    async def test_get_pnl_by_type(self, tmp_path):
        """Test getting P&L breakdown by type."""
        from mercury.services.state_store import StateStore

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        # Record P&L of different types
        await store.record_realized_pnl("trade-a", Decimal("10.0"), "resolution", Decimal("100.0"))
        await store.record_realized_pnl("trade-b", Decimal("-5.0"), "settlement", Decimal("100.0"))
        await store.record_realized_pnl("trade-c", Decimal("3.0"), "rebalance", Decimal("100.0"))

        by_type = await store.get_pnl_by_type()

        assert by_type["resolution"] == Decimal("10.0")
        assert by_type["settlement"] == Decimal("-5.0")
        assert by_type["rebalance"] == Decimal("3.0")

        await store.close()

    @pytest.mark.asyncio
    async def test_get_daily_pnl_breakdown(self, tmp_path):
        """Test getting daily P&L breakdown."""
        from mercury.services.state_store import StateStore

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        # Record some P&L
        await store.record_realized_pnl("trade-day-1", Decimal("10.0"), "resolution", Decimal("100.0"), notes="First trade")
        await store.record_realized_pnl("trade-day-2", Decimal("5.0"), "settlement", Decimal("100.0"))

        entries = await store.get_daily_pnl_breakdown()

        assert len(entries) == 2
        assert entries[0].trade_id == "trade-day-1"
        assert entries[0].pnl_amount == Decimal("10.0")
        assert entries[0].notes == "First trade"

        await store.close()


class TestDatabaseStatistics:
    """Test database statistics."""

    @pytest.mark.asyncio
    async def test_get_database_stats(self, tmp_path):
        """Test getting database statistics."""
        from mercury.services.state_store import StateStore, Trade

        db_path = str(tmp_path / "test.db")
        store = StateStore(db_path=db_path)
        await store.connect()

        # Add some data
        trade = Trade(
            trade_id="stats-trade-1",
            market_id="test-market",
            strategy="gabagool",
            side="YES",
            size=Decimal("10.0"),
            price=Decimal("0.50"),
            cost=Decimal("5.0"),
        )
        await store.save_trade(trade)

        stats = await store.get_database_stats()

        assert stats["trades"] == 1
        assert stats["positions"] == 0
        assert "db_size_mb" in stats

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
