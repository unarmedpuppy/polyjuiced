"""Tests for the polyjuiced to Mercury migration script."""
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

# Import migration functions
from scripts.migrate_from_polyjuiced import (
    MigrationStats,
    connect_db,
    generate_position_id,
    get_table_columns,
    migrate_circuit_breaker,
    migrate_daily_stats,
    migrate_fill_records,
    migrate_pnl_ledger,
    migrate_rebalance_trades,
    migrate_settlement_queue,
    migrate_trade_telemetry,
    migrate_trades,
    row_get,
    table_exists,
    verify_migration,
)


# Legacy schema (simplified for testing)
LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP,
    asset TEXT NOT NULL,
    market_slug TEXT,
    condition_id TEXT,
    yes_price REAL NOT NULL,
    no_price REAL NOT NULL,
    yes_cost REAL NOT NULL,
    no_cost REAL NOT NULL,
    spread REAL NOT NULL,
    expected_profit REAL NOT NULL,
    actual_profit REAL,
    status TEXT DEFAULT 'pending',
    market_end_time TEXT,
    dry_run BOOLEAN DEFAULT 0,
    yes_shares REAL,
    no_shares REAL,
    hedge_ratio REAL,
    execution_status TEXT,
    yes_order_status TEXT,
    no_order_status TEXT,
    yes_liquidity_at_price REAL,
    no_liquidity_at_price REAL,
    yes_book_depth_total REAL,
    no_book_depth_total REAL,
    strategy_id TEXT DEFAULT 'gabagool'
);

CREATE TABLE IF NOT EXISTS settlement_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    trade_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    asset TEXT NOT NULL,
    shares REAL NOT NULL,
    entry_price REAL NOT NULL,
    entry_cost REAL NOT NULL,
    market_end_time TIMESTAMP NOT NULL,
    claimed BOOLEAN DEFAULT 0,
    claimed_at TIMESTAMP,
    claim_proceeds REAL,
    claim_profit REAL,
    claim_attempts INTEGER DEFAULT 0,
    last_claim_error TEXT,
    UNIQUE(trade_id, token_id)
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    pnl REAL DEFAULT 0,
    trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    exposure REAL DEFAULT 0,
    opportunities_detected INTEGER DEFAULT 0,
    opportunities_executed INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fill_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    token_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    side TEXT NOT NULL,
    intended_size REAL NOT NULL,
    filled_size REAL NOT NULL,
    intended_price REAL NOT NULL,
    actual_avg_price REAL NOT NULL,
    time_to_fill_ms INTEGER NOT NULL,
    slippage REAL NOT NULL,
    pre_fill_depth REAL NOT NULL,
    post_fill_depth REAL,
    order_type TEXT DEFAULT 'GTC',
    order_id TEXT,
    fill_ratio REAL,
    persistence_ratio REAL
);

CREATE TABLE IF NOT EXISTS trade_telemetry (
    trade_id TEXT PRIMARY KEY,
    opportunity_detected_at TIMESTAMP,
    opportunity_spread REAL,
    opportunity_yes_price REAL,
    opportunity_no_price REAL,
    order_placed_at TIMESTAMP,
    order_filled_at TIMESTAMP,
    execution_latency_ms REAL,
    fill_latency_ms REAL,
    initial_yes_shares REAL,
    initial_no_shares REAL,
    initial_hedge_ratio REAL,
    rebalance_started_at TIMESTAMP,
    rebalance_attempts INTEGER DEFAULT 0,
    position_balanced_at TIMESTAMP,
    resolved_at TIMESTAMP,
    final_yes_shares REAL,
    final_no_shares REAL,
    final_hedge_ratio REAL,
    actual_profit REAL
);

CREATE TABLE IF NOT EXISTS rebalance_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL,
    attempted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    action TEXT NOT NULL,
    shares REAL NOT NULL,
    price REAL NOT NULL,
    status TEXT NOT NULL,
    filled_shares REAL DEFAULT 0,
    profit REAL DEFAULT 0,
    error TEXT,
    order_id TEXT
);

CREATE TABLE IF NOT EXISTS circuit_breaker_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    date TEXT NOT NULL,
    realized_pnl REAL DEFAULT 0.0,
    circuit_breaker_hit BOOLEAN DEFAULT 0,
    hit_at TIMESTAMP,
    hit_reason TEXT,
    total_trades_today INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO circuit_breaker_state (id, date) VALUES (1, date('now'));

CREATE TABLE IF NOT EXISTS realized_pnl_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    trade_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    pnl_amount REAL NOT NULL,
    pnl_type TEXT NOT NULL,
    notes TEXT,
    UNIQUE(trade_id, pnl_type)
);
"""

# Mercury schema (from state_store.py)
MERCURY_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    side TEXT NOT NULL,
    size REAL NOT NULL,
    price REAL NOT NULL,
    cost REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    filled_size REAL DEFAULT 0,
    avg_fill_price REAL,
    fee REAL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    condition_id TEXT,
    asset TEXT,
    yes_price REAL,
    no_price REAL,
    yes_cost REAL,
    no_cost REAL,
    spread REAL,
    expected_profit REAL,
    actual_profit REAL,
    market_end_time TEXT,
    market_slug TEXT,
    dry_run BOOLEAN DEFAULT 0,
    yes_shares REAL,
    no_shares REAL,
    hedge_ratio REAL,
    execution_status TEXT,
    yes_order_status TEXT,
    no_order_status TEXT,
    yes_liquidity_at_price REAL,
    no_liquidity_at_price REAL,
    yes_book_depth_total REAL,
    no_book_depth_total REAL,
    resolved_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    side TEXT NOT NULL,
    size REAL NOT NULL,
    entry_price REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMP,
    exit_price REAL,
    realized_pnl REAL,
    unrealized_pnl REAL,
    current_price REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settlement_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT NOT NULL UNIQUE,
    market_id TEXT NOT NULL,
    condition_id TEXT,
    side TEXT NOT NULL,
    size REAL NOT NULL,
    entry_price REAL NOT NULL,
    queued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    claimed_at TIMESTAMP,
    proceeds REAL,
    status TEXT DEFAULT 'pending',
    trade_id TEXT,
    token_id TEXT,
    asset TEXT,
    shares REAL,
    entry_cost REAL,
    market_end_time TIMESTAMP,
    claimed BOOLEAN DEFAULT 0,
    claim_proceeds REAL,
    claim_profit REAL,
    claim_attempts INTEGER DEFAULT 0,
    last_claim_error TEXT
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    trade_count INTEGER DEFAULT 0,
    volume_usd REAL DEFAULT 0,
    realized_pnl REAL DEFAULT 0,
    positions_opened INTEGER DEFAULT 0,
    positions_closed INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    exposure REAL DEFAULT 0,
    opportunities_detected INTEGER DEFAULT 0,
    opportunities_executed INTEGER DEFAULT 0,
    max_drawdown REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    trade_id TEXT REFERENCES trades(trade_id),
    order_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    size REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL DEFAULT 0,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fill_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    token_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    side TEXT NOT NULL,
    intended_size REAL NOT NULL,
    filled_size REAL NOT NULL,
    intended_price REAL NOT NULL,
    actual_avg_price REAL NOT NULL,
    time_to_fill_ms INTEGER NOT NULL,
    slippage REAL NOT NULL,
    pre_fill_depth REAL NOT NULL,
    post_fill_depth REAL,
    order_type TEXT DEFAULT 'GTC',
    order_id TEXT,
    fill_ratio REAL,
    persistence_ratio REAL
);

CREATE TABLE IF NOT EXISTS trade_telemetry (
    trade_id TEXT PRIMARY KEY,
    opportunity_detected_at TIMESTAMP,
    opportunity_spread REAL,
    opportunity_yes_price REAL,
    opportunity_no_price REAL,
    order_placed_at TIMESTAMP,
    order_filled_at TIMESTAMP,
    execution_latency_ms REAL,
    fill_latency_ms REAL,
    initial_yes_shares REAL,
    initial_no_shares REAL,
    initial_hedge_ratio REAL,
    rebalance_started_at TIMESTAMP,
    rebalance_attempts INTEGER DEFAULT 0,
    position_balanced_at TIMESTAMP,
    resolved_at TIMESTAMP,
    final_yes_shares REAL,
    final_no_shares REAL,
    final_hedge_ratio REAL,
    actual_profit REAL
);

CREATE TABLE IF NOT EXISTS rebalance_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL,
    attempted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    action TEXT NOT NULL,
    shares REAL NOT NULL,
    price REAL NOT NULL,
    status TEXT NOT NULL,
    filled_shares REAL DEFAULT 0,
    profit REAL DEFAULT 0,
    error TEXT,
    order_id TEXT
);

CREATE TABLE IF NOT EXISTS circuit_breaker_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    date TEXT NOT NULL,
    realized_pnl REAL DEFAULT 0.0,
    circuit_breaker_hit BOOLEAN DEFAULT 0,
    hit_at TIMESTAMP,
    hit_reason TEXT,
    total_trades_today INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO circuit_breaker_state (id, date) VALUES (1, date('now'));

CREATE TABLE IF NOT EXISTS realized_pnl_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    trade_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    pnl_amount REAL NOT NULL,
    pnl_type TEXT NOT NULL,
    notes TEXT,
    UNIQUE(trade_id, pnl_type)
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

INSERT OR IGNORE INTO schema_version (version) VALUES (3);
"""


@pytest.fixture
def legacy_db():
    """Create a temporary legacy database with test data."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(LEGACY_SCHEMA)
    conn.commit()

    yield db_path, conn

    conn.close()
    db_path.unlink()


@pytest.fixture
def mercury_db():
    """Create a temporary Mercury database with schema."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(MERCURY_SCHEMA)
    conn.commit()

    yield db_path, conn

    conn.close()
    db_path.unlink()


class TestHelperFunctions:
    """Tests for helper functions."""

    def test_generate_position_id(self):
        """Test position ID generation is deterministic."""
        pos_id1 = generate_position_id("trade-123", "token-abc")
        pos_id2 = generate_position_id("trade-123", "token-abc")
        pos_id3 = generate_position_id("trade-123", "token-xyz")

        assert pos_id1 == pos_id2  # Same inputs = same output
        assert pos_id1 != pos_id3  # Different token = different ID
        assert len(pos_id1) == 16  # Truncated to 16 chars

    def test_table_exists(self, legacy_db):
        """Test table existence check."""
        _, conn = legacy_db

        assert table_exists(conn, "trades")
        assert table_exists(conn, "settlement_queue")
        assert not table_exists(conn, "nonexistent_table")

    def test_get_table_columns(self, legacy_db):
        """Test getting table columns."""
        _, conn = legacy_db

        columns = get_table_columns(conn, "trades")
        assert "id" in columns
        assert "yes_price" in columns
        assert "strategy_id" in columns


class TestMigrationStats:
    """Tests for MigrationStats dataclass."""

    def test_default_values(self):
        """Test default stats values."""
        stats = MigrationStats()

        assert stats.trades_migrated == 0
        assert stats.errors == []

    def test_print_summary(self, capsys):
        """Test summary printing."""
        stats = MigrationStats()
        stats.trades_migrated = 5
        stats.settlement_queue_migrated = 3

        stats.print_summary()
        captured = capsys.readouterr()

        assert "Trades: 5 migrated" in captured.out
        assert "Settlement queue: 3 migrated" in captured.out


class TestTradesMigration:
    """Tests for trades migration."""

    def test_migrate_empty_trades(self, legacy_db, mercury_db):
        """Test migration with no trades."""
        _, legacy_conn = legacy_db
        _, mercury_conn = mercury_db

        stats = MigrationStats()
        migrate_trades(legacy_conn, mercury_conn, stats)

        assert stats.trades_migrated == 0
        assert stats.trades_skipped == 0

    def test_migrate_single_trade(self, legacy_db, mercury_db):
        """Test migrating a single trade."""
        _, legacy_conn = legacy_db
        _, mercury_conn = mercury_db

        # Insert a test trade
        legacy_conn.execute(
            """
            INSERT INTO trades (
                id, asset, yes_price, no_price, yes_cost, no_cost,
                spread, expected_profit, status, condition_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("trade-001", "BTC", 0.55, 0.45, 10.0, 8.0, 0.10, 2.0, "win", "cond-123"),
        )
        legacy_conn.commit()

        stats = MigrationStats()
        migrate_trades(legacy_conn, mercury_conn, stats)

        assert stats.trades_migrated == 1
        assert stats.trades_skipped == 0

        # Verify trade in Mercury
        cursor = mercury_conn.execute("SELECT * FROM trades WHERE trade_id = 'trade-001'")
        trade = cursor.fetchone()

        assert trade is not None
        assert trade["market_id"] == "cond-123"
        assert trade["strategy"] == "gabagool"
        assert trade["status"] == "closed"  # "win" maps to "closed"

    def test_skip_existing_trades(self, legacy_db, mercury_db):
        """Test skipping already migrated trades."""
        _, legacy_conn = legacy_db
        _, mercury_conn = mercury_db

        # Insert same trade in both databases
        legacy_conn.execute(
            """
            INSERT INTO trades (
                id, asset, yes_price, no_price, yes_cost, no_cost,
                spread, expected_profit, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("trade-001", "BTC", 0.55, 0.45, 10.0, 8.0, 0.10, 2.0, "win"),
        )
        legacy_conn.commit()

        mercury_conn.execute(
            """
            INSERT INTO trades (
                trade_id, market_id, strategy, side, size, price, cost, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("trade-001", "market-1", "gabagool", "BUY", 100.0, 0.55, 18.0, "closed"),
        )
        mercury_conn.commit()

        stats = MigrationStats()
        migrate_trades(legacy_conn, mercury_conn, stats)

        assert stats.trades_migrated == 0
        assert stats.trades_skipped == 1


class TestSettlementQueueMigration:
    """Tests for settlement queue migration."""

    def test_migrate_settlement_entry(self, legacy_db, mercury_db):
        """Test migrating settlement queue entries."""
        _, legacy_conn = legacy_db
        _, mercury_conn = mercury_db

        # Insert a settlement entry
        legacy_conn.execute(
            """
            INSERT INTO settlement_queue (
                trade_id, condition_id, token_id, side, asset,
                shares, entry_price, entry_cost, market_end_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("trade-001", "cond-123", "token-abc", "YES", "BTC", 100.0, 0.55, 55.0, "2025-01-01"),
        )
        legacy_conn.commit()

        stats = MigrationStats()
        migrate_settlement_queue(legacy_conn, mercury_conn, stats)

        assert stats.settlement_queue_migrated == 1
        assert stats.positions_created == 1

        # Verify position was created
        cursor = mercury_conn.execute("SELECT COUNT(*) FROM positions")
        assert cursor.fetchone()[0] == 1


class TestDailyStatsMigration:
    """Tests for daily stats migration."""

    def test_migrate_daily_stats(self, legacy_db, mercury_db):
        """Test migrating daily stats."""
        _, legacy_conn = legacy_db
        _, mercury_conn = mercury_db

        # Insert daily stats
        legacy_conn.execute(
            """
            INSERT INTO daily_stats (date, pnl, trades, wins, losses)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("2025-01-15", 25.50, 5, 3, 2),
        )
        legacy_conn.commit()

        stats = MigrationStats()
        migrate_daily_stats(legacy_conn, mercury_conn, stats)

        assert stats.daily_stats_migrated == 1

        # Verify in Mercury
        cursor = mercury_conn.execute("SELECT * FROM daily_stats WHERE date = '2025-01-15'")
        day = cursor.fetchone()

        assert day is not None
        assert day["realized_pnl"] == 25.50
        assert day["trade_count"] == 5
        assert day["wins"] == 3


class TestPnlLedgerMigration:
    """Tests for P&L ledger migration."""

    def test_migrate_pnl_entries(self, legacy_db, mercury_db):
        """Test migrating P&L ledger entries."""
        _, legacy_conn = legacy_db
        _, mercury_conn = mercury_db

        # Insert P&L entry
        legacy_conn.execute(
            """
            INSERT INTO realized_pnl_ledger (trade_id, trade_date, pnl_amount, pnl_type, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("trade-001", "2025-01-15", 12.50, "resolution", "Market resolved YES"),
        )
        legacy_conn.commit()

        stats = MigrationStats()
        migrate_pnl_ledger(legacy_conn, mercury_conn, stats)

        assert stats.pnl_ledger_migrated == 1

        # Verify in Mercury
        cursor = mercury_conn.execute("SELECT * FROM realized_pnl_ledger WHERE trade_id = 'trade-001'")
        entry = cursor.fetchone()

        assert entry is not None
        assert entry["pnl_amount"] == 12.50
        assert entry["pnl_type"] == "resolution"


class TestCircuitBreakerMigration:
    """Tests for circuit breaker migration."""

    def test_migrate_circuit_breaker(self, legacy_db, mercury_db):
        """Test migrating circuit breaker state."""
        _, legacy_conn = legacy_db
        _, mercury_conn = mercury_db

        # Update circuit breaker state
        legacy_conn.execute(
            """
            UPDATE circuit_breaker_state
            SET realized_pnl = -50.0, total_trades_today = 10
            WHERE id = 1
            """,
        )
        legacy_conn.commit()

        stats = MigrationStats()
        migrate_circuit_breaker(legacy_conn, mercury_conn, stats)

        assert stats.circuit_breaker_migrated is True

        # Verify in Mercury
        cursor = mercury_conn.execute("SELECT * FROM circuit_breaker_state WHERE id = 1")
        state = cursor.fetchone()

        assert state is not None
        assert state["realized_pnl"] == -50.0
        assert state["total_trades_today"] == 10


class TestDryRun:
    """Tests for dry run mode."""

    def test_dry_run_no_writes(self, legacy_db, mercury_db):
        """Test that dry run doesn't write to database."""
        _, legacy_conn = legacy_db
        _, mercury_conn = mercury_db

        # Insert a test trade
        legacy_conn.execute(
            """
            INSERT INTO trades (
                id, asset, yes_price, no_price, yes_cost, no_cost,
                spread, expected_profit, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("trade-001", "BTC", 0.55, 0.45, 10.0, 8.0, 0.10, 2.0, "win"),
        )
        legacy_conn.commit()

        stats = MigrationStats()
        migrate_trades(legacy_conn, mercury_conn, stats, dry_run=True)

        assert stats.trades_migrated == 1  # Counted but not written

        # Verify nothing was written
        cursor = mercury_conn.execute("SELECT COUNT(*) FROM trades")
        assert cursor.fetchone()[0] == 0


class TestVerification:
    """Tests for migration verification."""

    def test_verify_empty_databases(self, legacy_db, mercury_db):
        """Test verification with empty databases."""
        _, legacy_conn = legacy_db
        _, mercury_conn = mercury_db

        # Should not raise any exceptions
        result = verify_migration(legacy_conn, mercury_conn)
        # Verification returns bool indicating all checks passed
        assert isinstance(result, bool)


class TestFullMigration:
    """Integration tests for full migration flow."""

    def test_full_migration_flow(self, legacy_db, mercury_db):
        """Test complete migration with all table types."""
        _, legacy_conn = legacy_db
        _, mercury_conn = mercury_db

        # Populate legacy database
        legacy_conn.execute(
            """
            INSERT INTO trades (
                id, asset, yes_price, no_price, yes_cost, no_cost,
                spread, expected_profit, actual_profit, status, condition_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("trade-001", "BTC", 0.55, 0.45, 10.0, 8.0, 0.10, 2.0, 1.5, "win", "cond-123"),
        )

        legacy_conn.execute(
            """
            INSERT INTO settlement_queue (
                trade_id, condition_id, token_id, side, asset,
                shares, entry_price, entry_cost, market_end_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("trade-001", "cond-123", "token-yes", "YES", "BTC", 100.0, 0.55, 55.0, "2025-01-01"),
        )

        legacy_conn.execute(
            "INSERT INTO daily_stats (date, pnl, trades) VALUES (?, ?, ?)",
            ("2025-01-15", 1.5, 1),
        )

        legacy_conn.execute(
            """
            INSERT INTO realized_pnl_ledger (trade_id, trade_date, pnl_amount, pnl_type)
            VALUES (?, ?, ?, ?)
            """,
            ("trade-001", "2025-01-15", 1.5, "resolution"),
        )

        legacy_conn.commit()

        # Run all migrations
        stats = MigrationStats()
        migrate_trades(legacy_conn, mercury_conn, stats)
        migrate_settlement_queue(legacy_conn, mercury_conn, stats)
        migrate_daily_stats(legacy_conn, mercury_conn, stats)
        migrate_pnl_ledger(legacy_conn, mercury_conn, stats)
        migrate_circuit_breaker(legacy_conn, mercury_conn, stats)

        # Verify counts
        assert stats.trades_migrated == 1
        assert stats.settlement_queue_migrated == 1
        assert stats.positions_created == 1
        assert stats.daily_stats_migrated == 1
        assert stats.pnl_ledger_migrated == 1
        assert stats.circuit_breaker_migrated is True
        assert len(stats.errors) == 0

        # Run verification
        verify_migration(legacy_conn, mercury_conn)
