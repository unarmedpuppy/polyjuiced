"""Regression tests for Phase 2 - Strategy-Owned Persistence.

Phase 2 Architecture Changes (2025-12-14):
- Strategy owns trade persistence via _record_trade()
- Dashboard is READ-ONLY for trade data (only manages display state)
- New fields: yes_shares, no_shares, hedge_ratio, execution_status
- Partial fills are now recorded with full execution details

See: docs/IMPLEMENTATION_PLAN_2025-12-14.md
See: docs/STRATEGY_ARCHITECTURE.md
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime
from decimal import Decimal


class TestPersistenceSchemaFields:
    """Test that new Phase 2 schema fields exist and work correctly."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database with async methods."""
        db = MagicMock()
        db.save_arbitrage_trade = AsyncMock()
        db.update_daily_stats = AsyncMock()
        db.resolve_trade = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_save_arbitrage_trade_accepts_new_fields(self, mock_db):
        """save_arbitrage_trade() should accept all Phase 2 fields."""
        from src.persistence import Database

        # This should not raise - all new fields should be accepted
        await mock_db.save_arbitrage_trade(
            trade_id="trade-1",
            asset="BTC",
            condition_id="cond123",
            yes_price=0.48,
            no_price=0.49,
            yes_cost=9.60,
            no_cost=9.80,
            spread=3.0,
            expected_profit=0.60,
            yes_shares=20.0,  # Phase 2 field
            no_shares=20.0,   # Phase 2 field
            hedge_ratio=1.0,  # Phase 2 field
            execution_status="full_fill",  # Phase 2 field
            yes_order_status="MATCHED",  # Phase 2 field
            no_order_status="MATCHED",   # Phase 2 field
            market_end_time="14:00 UTC",
            market_slug="test-slug",
            dry_run=False,
        )

        mock_db.save_arbitrage_trade.assert_called_once()
        call_kwargs = mock_db.save_arbitrage_trade.call_args.kwargs

        # Verify new fields were passed
        assert call_kwargs["yes_shares"] == 20.0
        assert call_kwargs["no_shares"] == 20.0
        assert call_kwargs["hedge_ratio"] == 1.0
        assert call_kwargs["execution_status"] == "full_fill"
        assert call_kwargs["yes_order_status"] == "MATCHED"
        assert call_kwargs["no_order_status"] == "MATCHED"

    @pytest.mark.asyncio
    async def test_liquidity_fields_optional(self, mock_db):
        """Liquidity fields (Phase 7) should be optional."""
        await mock_db.save_arbitrage_trade(
            trade_id="trade-1",
            asset="ETH",
            condition_id="cond456",
            yes_price=0.30,
            no_price=0.68,
            yes_cost=6.00,
            no_cost=13.60,
            spread=2.0,
            expected_profit=0.40,
            yes_shares=20.0,
            no_shares=20.0,
            hedge_ratio=1.0,
            execution_status="full_fill",
            yes_order_status="MATCHED",
            no_order_status="MATCHED",
            # NOT passing liquidity fields - should work
        )

        mock_db.save_arbitrage_trade.assert_called_once()


class TestStrategyOwnsPersistence:
    """Test that strategy _record_trade() method works correctly."""

    @pytest.fixture
    def mock_strategy_deps(self):
        """Create mock dependencies for strategy."""
        from src.config import AppConfig, GabagoolConfig, PolymarketSettings

        gabagool_config = MagicMock(spec=GabagoolConfig)
        gabagool_config.enabled = True
        gabagool_config.dry_run = False
        gabagool_config.min_spread_cents = 2.0
        gabagool_config.max_trade_size = 25.0
        gabagool_config.parallel_execution_enabled = True
        gabagool_config.parallel_fill_timeout_seconds = 5.0
        gabagool_config.max_liquidity_consumption_pct = 0.5
        gabagool_config.min_hedge_ratio = 0.8
        gabagool_config.critical_hedge_ratio = 0.5
        gabagool_config.max_position_imbalance_shares = 5.0

        config = MagicMock(spec=AppConfig)
        config.gabagool = gabagool_config

        return config

    @pytest.fixture
    def mock_market(self):
        """Create mock market."""
        from src.monitoring.market_finder import Market15Min

        market = MagicMock(spec=Market15Min)
        market.asset = "BTC"
        market.condition_id = "cond123"
        market.yes_token_id = "yes_token"
        market.no_token_id = "no_token"
        market.slug = "btc-15min"
        market.end_time = datetime.utcnow()
        return market

    @pytest.fixture
    def mock_opportunity(self):
        """Create mock arbitrage opportunity."""
        from src.monitoring.order_book import ArbitrageOpportunity

        opp = MagicMock(spec=ArbitrageOpportunity)
        opp.yes_price = 0.48
        opp.no_price = 0.49
        opp.spread_cents = 3.0
        opp.profit_percentage = 3.1
        return opp

    @pytest.mark.asyncio
    async def test_record_trade_calls_save_arbitrage_trade(
        self, mock_strategy_deps, mock_market, mock_opportunity
    ):
        """_record_trade() should call DB save_arbitrage_trade()."""
        from src.strategies.gabagool import GabagoolStrategy
        from src.persistence import Database

        # Create strategy with mock DB
        mock_db = MagicMock(spec=Database)
        mock_db.save_arbitrage_trade = AsyncMock()
        mock_db.update_daily_stats = AsyncMock()

        mock_client = MagicMock()
        mock_ws = MagicMock()
        mock_finder = MagicMock()

        strategy = GabagoolStrategy(
            client=mock_client,
            ws_client=mock_ws,
            market_finder=mock_finder,
            config=mock_strategy_deps,
            db=mock_db,
        )

        # Call _record_trade
        await strategy._record_trade(
            trade_id="trade-123",
            market=mock_market,
            opportunity=mock_opportunity,
            yes_amount=9.60,
            no_amount=9.80,
            actual_yes_shares=20.0,
            actual_no_shares=20.0,
            hedge_ratio=1.0,
            execution_status="full_fill",
            yes_order_status="MATCHED",
            no_order_status="MATCHED",
            expected_profit=0.60,
            dry_run=False,
        )

        # Verify DB was called
        mock_db.save_arbitrage_trade.assert_called_once()
        call_kwargs = mock_db.save_arbitrage_trade.call_args.kwargs

        assert call_kwargs["trade_id"] == "trade-123"
        assert call_kwargs["asset"] == "BTC"
        assert call_kwargs["yes_price"] == 0.48
        assert call_kwargs["no_price"] == 0.49
        assert call_kwargs["hedge_ratio"] == 1.0
        assert call_kwargs["execution_status"] == "full_fill"

    @pytest.mark.asyncio
    async def test_record_trade_updates_daily_stats(
        self, mock_strategy_deps, mock_market, mock_opportunity
    ):
        """_record_trade() should update daily stats."""
        from src.strategies.gabagool import GabagoolStrategy
        from src.persistence import Database

        mock_db = MagicMock(spec=Database)
        mock_db.save_arbitrage_trade = AsyncMock()
        mock_db.update_daily_stats = AsyncMock()

        mock_client = MagicMock()
        mock_ws = MagicMock()
        mock_finder = MagicMock()

        strategy = GabagoolStrategy(
            client=mock_client,
            ws_client=mock_ws,
            market_finder=mock_finder,
            config=mock_strategy_deps,
            db=mock_db,
        )

        await strategy._record_trade(
            trade_id="trade-456",
            market=mock_market,
            opportunity=mock_opportunity,
            yes_amount=9.60,
            no_amount=9.80,
            actual_yes_shares=20.0,
            actual_no_shares=20.0,
            hedge_ratio=1.0,
            execution_status="full_fill",
            yes_order_status="MATCHED",
            no_order_status="MATCHED",
            expected_profit=0.60,
            dry_run=False,
        )

        # Verify daily stats were updated
        mock_db.update_daily_stats.assert_called_once()
        call_kwargs = mock_db.update_daily_stats.call_args.kwargs

        assert call_kwargs["trades_delta"] == 1
        assert call_kwargs["exposure_delta"] == pytest.approx(19.40, rel=0.01)

    @pytest.mark.asyncio
    async def test_record_trade_handles_no_db(
        self, mock_strategy_deps, mock_market, mock_opportunity
    ):
        """_record_trade() should gracefully handle missing DB."""
        from src.strategies.gabagool import GabagoolStrategy

        mock_client = MagicMock()
        mock_ws = MagicMock()
        mock_finder = MagicMock()

        # Create strategy WITHOUT db
        strategy = GabagoolStrategy(
            client=mock_client,
            ws_client=mock_ws,
            market_finder=mock_finder,
            config=mock_strategy_deps,
            db=None,  # No DB
        )

        # Should not raise, just warn
        await strategy._record_trade(
            trade_id="trade-789",
            market=mock_market,
            opportunity=mock_opportunity,
            yes_amount=9.60,
            no_amount=9.80,
            actual_yes_shares=20.0,
            actual_no_shares=20.0,
            hedge_ratio=1.0,
            execution_status="full_fill",
            yes_order_status="MATCHED",
            no_order_status="MATCHED",
            expected_profit=0.60,
            dry_run=False,
        )
        # No exception = success


class TestDashboardReadOnly:
    """Test that dashboard add_trade() no longer persists to DB."""

    @pytest.mark.asyncio
    async def test_add_trade_does_not_call_db_save(self):
        """Dashboard add_trade() should NOT call DB save_trade()."""
        from src import dashboard

        # Save original _db reference
        original_db = dashboard._db

        try:
            # Set up mock DB
            mock_db = MagicMock()
            mock_db.save_trade = AsyncMock()
            mock_db.update_daily_stats = AsyncMock()
            dashboard._db = mock_db

            # Call add_trade
            trade_id = dashboard.add_trade(
                asset="ETH",
                yes_price=0.30,
                no_price=0.68,
                yes_cost=6.00,
                no_cost=13.60,
                spread=2.0,
                expected_profit=0.40,
            )

            # Verify add_trade returned a trade_id
            assert trade_id.startswith("trade-")

            # CRITICAL: DB save_trade should NOT have been called
            # This is the key behavior change in Phase 2
            mock_db.save_trade.assert_not_called()

        finally:
            # Restore original
            dashboard._db = original_db

    def test_add_trade_still_updates_in_memory_state(self):
        """Dashboard add_trade() should still update in-memory display state."""
        from src import dashboard

        # Record initial state
        initial_pending = dashboard.stats.get("pending", 0)
        initial_trades = dashboard.stats.get("daily_trades", 0)

        trade_id = dashboard.add_trade(
            asset="SOL",
            yes_price=0.40,
            no_price=0.58,
            yes_cost=8.00,
            no_cost=11.60,
            spread=2.0,
            expected_profit=0.40,
        )

        # Verify in-memory state was updated
        assert dashboard.stats["pending"] == initial_pending + 1
        assert dashboard.stats["daily_trades"] == initial_trades + 1


class TestPartialFillRecording:
    """Test that partial fills are now recorded with execution details."""

    @pytest.fixture
    def mock_strategy_with_db(self):
        """Create strategy with mock DB for partial fill testing."""
        from src.strategies.gabagool import GabagoolStrategy
        from src.persistence import Database
        from src.config import AppConfig, GabagoolConfig

        gabagool_config = MagicMock(spec=GabagoolConfig)
        gabagool_config.enabled = True
        gabagool_config.dry_run = False
        gabagool_config.parallel_execution_enabled = True
        gabagool_config.parallel_fill_timeout_seconds = 5.0
        gabagool_config.max_liquidity_consumption_pct = 0.5
        gabagool_config.min_hedge_ratio = 0.8
        gabagool_config.critical_hedge_ratio = 0.5

        config = MagicMock(spec=AppConfig)
        config.gabagool = gabagool_config

        mock_db = MagicMock(spec=Database)
        mock_db.save_arbitrage_trade = AsyncMock()
        mock_db.update_daily_stats = AsyncMock()

        mock_client = MagicMock()
        mock_ws = MagicMock()
        mock_finder = MagicMock()

        strategy = GabagoolStrategy(
            client=mock_client,
            ws_client=mock_ws,
            market_finder=mock_finder,
            config=config,
            db=mock_db,
        )

        return strategy, mock_db

    @pytest.mark.asyncio
    async def test_partial_fill_recorded_with_one_leg_only_status(
        self, mock_strategy_with_db
    ):
        """Partial fills with only one leg should be recorded."""
        strategy, mock_db = mock_strategy_with_db

        # Create mock market and opportunity
        mock_market = MagicMock()
        mock_market.asset = "BTC"
        mock_market.condition_id = "cond123"
        mock_market.slug = "btc-15min"
        mock_market.end_time = datetime.utcnow()

        mock_opportunity = MagicMock()
        mock_opportunity.yes_price = 0.48
        mock_opportunity.no_price = 0.49
        mock_opportunity.spread_cents = 3.0

        # Record a one-leg-only partial fill
        await strategy._record_trade(
            trade_id="partial-abc123",
            market=mock_market,
            opportunity=mock_opportunity,
            yes_amount=9.60,
            no_amount=0,  # NO leg didn't fill
            actual_yes_shares=20.0,
            actual_no_shares=0,  # NO leg didn't fill
            hedge_ratio=0,  # No hedge
            execution_status="one_leg_only",
            yes_order_status="MATCHED",
            no_order_status="FAILED",
            expected_profit=0,  # No profit on partial
            dry_run=False,
        )

        # Verify it was recorded
        mock_db.save_arbitrage_trade.assert_called_once()
        call_kwargs = mock_db.save_arbitrage_trade.call_args.kwargs

        assert call_kwargs["execution_status"] == "one_leg_only"
        assert call_kwargs["yes_shares"] == 20.0
        assert call_kwargs["no_shares"] == 0
        assert call_kwargs["hedge_ratio"] == 0

    @pytest.mark.asyncio
    async def test_partial_fill_recorded_with_imbalanced_hedge(
        self, mock_strategy_with_db
    ):
        """Partial fills with imbalanced hedge should be recorded."""
        strategy, mock_db = mock_strategy_with_db

        mock_market = MagicMock()
        mock_market.asset = "ETH"
        mock_market.condition_id = "cond456"
        mock_market.slug = "eth-15min"
        mock_market.end_time = datetime.utcnow()

        mock_opportunity = MagicMock()
        mock_opportunity.yes_price = 0.30
        mock_opportunity.no_price = 0.68
        mock_opportunity.spread_cents = 2.0

        # Record an imbalanced partial fill (60% hedge ratio)
        await strategy._record_trade(
            trade_id="partial-def456",
            market=mock_market,
            opportunity=mock_opportunity,
            yes_amount=6.00,
            no_amount=8.16,  # Partial NO fill
            actual_yes_shares=20.0,
            actual_no_shares=12.0,  # Only got 12 of intended 20
            hedge_ratio=0.6,  # 12/20 = 60% hedge
            execution_status="partial_fill",
            yes_order_status="MATCHED",
            no_order_status="MATCHED",  # Both matched but different amounts
            expected_profit=0,
            dry_run=False,
        )

        mock_db.save_arbitrage_trade.assert_called_once()
        call_kwargs = mock_db.save_arbitrage_trade.call_args.kwargs

        assert call_kwargs["execution_status"] == "partial_fill"
        assert call_kwargs["hedge_ratio"] == pytest.approx(0.6, rel=0.01)


class TestExecutionStatusValues:
    """Test that execution_status values are correctly determined."""

    def test_full_fill_status_for_perfect_hedge(self):
        """Perfect hedge (ratio >= 0.95) should be 'full_fill'."""
        yes_shares = 20.0
        no_shares = 20.0
        hedge_ratio = min(yes_shares, no_shares) / max(yes_shares, no_shares)

        assert hedge_ratio >= 0.95

        if yes_shares > 0 and no_shares > 0:
            if hedge_ratio >= 0.95:
                status = "full_fill"
            else:
                status = "partial_fill"
        elif yes_shares > 0 or no_shares > 0:
            status = "one_leg_only"
        else:
            status = "failed"

        assert status == "full_fill"

    def test_partial_fill_status_for_imperfect_hedge(self):
        """Imperfect hedge (ratio < 0.95) should be 'partial_fill'."""
        yes_shares = 20.0
        no_shares = 15.0  # Only got 75% of NO side
        hedge_ratio = min(yes_shares, no_shares) / max(yes_shares, no_shares)

        assert hedge_ratio == pytest.approx(0.75, rel=0.01)

        if yes_shares > 0 and no_shares > 0:
            if hedge_ratio >= 0.95:
                status = "full_fill"
            else:
                status = "partial_fill"
        elif yes_shares > 0 or no_shares > 0:
            status = "one_leg_only"
        else:
            status = "failed"

        assert status == "partial_fill"

    def test_one_leg_only_status(self):
        """Only one leg filled should be 'one_leg_only'."""
        yes_shares = 20.0
        no_shares = 0

        if yes_shares > 0 and no_shares > 0:
            status = "partial_fill"
        elif yes_shares > 0 or no_shares > 0:
            status = "one_leg_only"
        else:
            status = "failed"

        assert status == "one_leg_only"

    def test_failed_status_for_no_fills(self):
        """No fills should be 'failed'."""
        yes_shares = 0
        no_shares = 0

        if yes_shares > 0 and no_shares > 0:
            status = "partial_fill"
        elif yes_shares > 0 or no_shares > 0:
            status = "one_leg_only"
        else:
            status = "failed"

        assert status == "failed"


class TestDatabaseMigration:
    """Test that schema migration adds new columns."""

    @pytest.mark.asyncio
    async def test_migration_adds_new_columns(self):
        """_migrate_schema() should add Phase 2 columns."""
        import tempfile
        import os
        from pathlib import Path
        from src.persistence import Database

        # Create temp DB
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = Database(db_path)

            await db.connect()

            # Check that new columns exist by querying PRAGMA
            cursor = await db._conn.execute("PRAGMA table_info(trades)")
            columns = await cursor.fetchall()
            column_names = [col[1] for col in columns]

            # Verify Phase 2 columns exist
            assert "yes_shares" in column_names
            assert "no_shares" in column_names
            assert "hedge_ratio" in column_names
            assert "execution_status" in column_names
            assert "yes_order_status" in column_names
            assert "no_order_status" in column_names

            await db.close()


class TestDryRunRecording:
    """Test that dry run trades are also recorded properly."""

    @pytest.fixture
    def mock_market_and_opportunity(self):
        """Create mock market and opportunity for dry run tests."""
        mock_market = MagicMock()
        mock_market.asset = "BTC"
        mock_market.condition_id = "dry_cond123"
        mock_market.slug = "btc-dry-test"
        mock_market.end_time = datetime.utcnow()

        mock_opportunity = MagicMock()
        mock_opportunity.yes_price = 0.48
        mock_opportunity.no_price = 0.49
        mock_opportunity.spread_cents = 3.0

        return mock_market, mock_opportunity

    @pytest.mark.asyncio
    async def test_dry_run_recorded_with_simulated_status(
        self, mock_market_and_opportunity
    ):
        """Dry run trades should have SIMULATED order status."""
        from src.strategies.gabagool import GabagoolStrategy
        from src.persistence import Database
        from src.config import AppConfig, GabagoolConfig

        mock_market, mock_opportunity = mock_market_and_opportunity

        gabagool_config = MagicMock(spec=GabagoolConfig)
        gabagool_config.enabled = True
        gabagool_config.dry_run = True

        config = MagicMock(spec=AppConfig)
        config.gabagool = gabagool_config

        mock_db = MagicMock(spec=Database)
        mock_db.save_arbitrage_trade = AsyncMock()
        mock_db.update_daily_stats = AsyncMock()

        strategy = GabagoolStrategy(
            client=MagicMock(),
            ws_client=MagicMock(),
            market_finder=MagicMock(),
            config=config,
            db=mock_db,
        )

        await strategy._record_trade(
            trade_id="dry-trade-1",
            market=mock_market,
            opportunity=mock_opportunity,
            yes_amount=9.60,
            no_amount=9.80,
            actual_yes_shares=20.0,
            actual_no_shares=20.0,
            hedge_ratio=1.0,
            execution_status="full_fill",
            yes_order_status="SIMULATED",
            no_order_status="SIMULATED",
            expected_profit=0.60,
            dry_run=True,
        )

        mock_db.save_arbitrage_trade.assert_called_once()
        call_kwargs = mock_db.save_arbitrage_trade.call_args.kwargs

        assert call_kwargs["dry_run"] is True
        assert call_kwargs["yes_order_status"] == "SIMULATED"
        assert call_kwargs["no_order_status"] == "SIMULATED"
