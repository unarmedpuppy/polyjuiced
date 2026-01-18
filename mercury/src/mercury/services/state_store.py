"""State Store Service - Async SQLite persistence layer.

This service:
- Persists trades, positions, and settlement queue
- Provides query interface for historical data
- Tracks daily statistics
- Uses connection pooling for concurrent access
- Supports schema migrations

Ported schema from legacy/src/persistence.py includes:
- trades: Enhanced with execution tracking, liquidity context, hedge ratio
- settlement_queue: Enhanced with token_id, asset, claim tracking
- daily_stats: Enhanced with opportunities tracking
- fill_records: Detailed fill tracking for slippage analysis
- trade_telemetry: Execution timing data
- rebalance_trades: Individual rebalancing trades
- circuit_breaker_state: Daily loss tracking
- realized_pnl_ledger: Audit trail for realized P&L
"""

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import aiosqlite
import structlog

from mercury.core.config import ConfigManager
from mercury.core.events import EventBus

log = structlog.get_logger()

# Schema version - increment when base schema changes
# Migrations handle upgrades from previous versions
SCHEMA_VERSION = 2

# Base schema (version 1) - minimal schema for new databases
# Migrations add additional tables and columns
SCHEMA_SQL = """
-- Trades table (base structure)
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
    -- Enhanced fields from legacy
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

-- Positions table
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
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Settlement queue (enhanced from legacy)
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
    -- Enhanced fields from legacy
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

-- Daily statistics (enhanced from legacy)
CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    trade_count INTEGER DEFAULT 0,
    volume_usd REAL DEFAULT 0,
    realized_pnl REAL DEFAULT 0,
    positions_opened INTEGER DEFAULT 0,
    positions_closed INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- Enhanced fields from legacy
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    exposure REAL DEFAULT 0,
    opportunities_detected INTEGER DEFAULT 0,
    opportunities_executed INTEGER DEFAULT 0
);

-- Fills for slippage analysis (basic)
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

-- Fill records (detailed slippage analysis from legacy)
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

-- Trade telemetry (timing data from legacy)
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

-- Rebalance trades (individual rebalancing trades from legacy)
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

-- Circuit breaker state (daily loss tracking from legacy)
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

-- Realized PnL ledger (audit trail from legacy)
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

-- Base Indexes
CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_id);
CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_condition ON trades(condition_id);
CREATE INDEX IF NOT EXISTS idx_trades_asset ON trades(asset);
CREATE INDEX IF NOT EXISTS idx_trades_execution_status ON trades(execution_status);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id);
CREATE INDEX IF NOT EXISTS idx_settlement_status ON settlement_queue(status);
CREATE INDEX IF NOT EXISTS idx_settlement_unclaimed ON settlement_queue(claimed, market_end_time);
CREATE INDEX IF NOT EXISTS idx_settlement_trade ON settlement_queue(trade_id);
CREATE INDEX IF NOT EXISTS idx_fills_trade ON fills(trade_id);
CREATE INDEX IF NOT EXISTS idx_daily_stats_date ON daily_stats(date);
CREATE INDEX IF NOT EXISTS idx_fill_records_timestamp ON fill_records(timestamp);
CREATE INDEX IF NOT EXISTS idx_fill_records_token ON fill_records(token_id);
CREATE INDEX IF NOT EXISTS idx_fill_records_asset ON fill_records(asset);
CREATE INDEX IF NOT EXISTS idx_fill_records_condition ON fill_records(condition_id);
CREATE INDEX IF NOT EXISTS idx_telemetry_detected ON trade_telemetry(opportunity_detected_at);
CREATE INDEX IF NOT EXISTS idx_telemetry_resolved ON trade_telemetry(resolved_at);
CREATE INDEX IF NOT EXISTS idx_rebalance_trade_id ON rebalance_trades(trade_id);
CREATE INDEX IF NOT EXISTS idx_rebalance_status ON rebalance_trades(status);
CREATE INDEX IF NOT EXISTS idx_rebalance_attempted ON rebalance_trades(attempted_at);
CREATE INDEX IF NOT EXISTS idx_pnl_ledger_date ON realized_pnl_ledger(trade_date);
CREATE INDEX IF NOT EXISTS idx_pnl_ledger_trade ON realized_pnl_ledger(trade_id);
CREATE INDEX IF NOT EXISTS idx_pnl_ledger_type ON realized_pnl_ledger(pnl_type);

-- Schema version
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
INSERT OR IGNORE INTO schema_version (version) VALUES (2);

-- Initialize circuit breaker singleton
INSERT OR IGNORE INTO circuit_breaker_state (id, date)
VALUES (1, date('now'));
"""


@dataclass
class Trade:
    """A recorded trade."""

    trade_id: str
    market_id: str
    strategy: str
    side: str
    size: Decimal
    price: Decimal
    cost: Decimal
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "open"
    filled_size: Decimal = Decimal("0")
    avg_fill_price: Optional[Decimal] = None
    fee: Decimal = Decimal("0")
    # Enhanced fields from legacy
    condition_id: Optional[str] = None
    asset: Optional[str] = None
    yes_price: Optional[Decimal] = None
    no_price: Optional[Decimal] = None
    yes_cost: Optional[Decimal] = None
    no_cost: Optional[Decimal] = None
    spread: Optional[Decimal] = None
    expected_profit: Optional[Decimal] = None
    actual_profit: Optional[Decimal] = None
    market_end_time: Optional[str] = None
    market_slug: Optional[str] = None
    dry_run: bool = False
    yes_shares: Optional[Decimal] = None
    no_shares: Optional[Decimal] = None
    hedge_ratio: Optional[Decimal] = None
    execution_status: Optional[str] = None
    yes_order_status: Optional[str] = None
    no_order_status: Optional[str] = None
    yes_liquidity_at_price: Optional[Decimal] = None
    no_liquidity_at_price: Optional[Decimal] = None
    yes_book_depth_total: Optional[Decimal] = None
    no_book_depth_total: Optional[Decimal] = None
    resolved_at: Optional[datetime] = None


@dataclass
class Position:
    """A position in a market."""

    position_id: str
    market_id: str
    strategy: str
    side: str
    size: Decimal
    entry_price: Decimal
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "open"
    closed_at: Optional[datetime] = None
    exit_price: Optional[Decimal] = None
    realized_pnl: Optional[Decimal] = None


@dataclass
class PositionResult:
    """Result of closing a position."""

    exit_price: Decimal
    realized_pnl: Decimal
    closed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class DailyStats:
    """Daily trading statistics."""

    date: date
    trade_count: int = 0
    volume_usd: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    positions_opened: int = 0
    positions_closed: int = 0
    # Enhanced fields from legacy
    wins: int = 0
    losses: int = 0
    exposure: Decimal = Decimal("0")
    opportunities_detected: int = 0
    opportunities_executed: int = 0


@dataclass
class FillRecord:
    """Detailed fill record for slippage analysis (from legacy)."""

    token_id: str
    condition_id: str
    asset: str
    side: str
    intended_size: Decimal
    filled_size: Decimal
    intended_price: Decimal
    actual_avg_price: Decimal
    time_to_fill_ms: int
    slippage: Decimal
    pre_fill_depth: Decimal
    id: Optional[int] = None
    timestamp: Optional[datetime] = None
    post_fill_depth: Optional[Decimal] = None
    order_type: str = "GTC"
    order_id: Optional[str] = None
    fill_ratio: Optional[Decimal] = None
    persistence_ratio: Optional[Decimal] = None


@dataclass
class TradeTelemetry:
    """Execution timing telemetry for a trade (from legacy)."""

    trade_id: str
    opportunity_detected_at: Optional[datetime] = None
    opportunity_spread: Optional[Decimal] = None
    opportunity_yes_price: Optional[Decimal] = None
    opportunity_no_price: Optional[Decimal] = None
    order_placed_at: Optional[datetime] = None
    order_filled_at: Optional[datetime] = None
    execution_latency_ms: Optional[Decimal] = None
    fill_latency_ms: Optional[Decimal] = None
    initial_yes_shares: Optional[Decimal] = None
    initial_no_shares: Optional[Decimal] = None
    initial_hedge_ratio: Optional[Decimal] = None
    rebalance_started_at: Optional[datetime] = None
    rebalance_attempts: int = 0
    position_balanced_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    final_yes_shares: Optional[Decimal] = None
    final_no_shares: Optional[Decimal] = None
    final_hedge_ratio: Optional[Decimal] = None
    actual_profit: Optional[Decimal] = None


@dataclass
class RebalanceTrade:
    """Individual rebalancing trade record (from legacy)."""

    trade_id: str
    action: str  # SELL_YES, BUY_NO, SELL_NO, BUY_YES
    shares: Decimal
    price: Decimal
    status: str  # SUCCESS, FAILED, PARTIAL
    id: Optional[int] = None
    attempted_at: Optional[datetime] = None
    filled_shares: Decimal = Decimal("0")
    profit: Decimal = Decimal("0")
    error: Optional[str] = None
    order_id: Optional[str] = None


@dataclass
class CircuitBreakerState:
    """Circuit breaker state (from legacy)."""

    date: str
    realized_pnl: Decimal = Decimal("0")
    circuit_breaker_hit: bool = False
    hit_at: Optional[datetime] = None
    hit_reason: Optional[str] = None
    total_trades_today: int = 0


@dataclass
class RealizedPnlEntry:
    """Realized PnL ledger entry (from legacy)."""

    trade_id: str
    trade_date: str
    pnl_amount: Decimal
    pnl_type: str  # 'resolution', 'settlement', 'rebalance', 'historical_import'
    id: Optional[int] = None
    created_at: Optional[datetime] = None
    notes: Optional[str] = None


class ConnectionPool:
    """Simple connection pool for concurrent SQLite access.

    aiosqlite connections are not thread-safe by default. This pool
    manages a single connection with a lock to serialize access,
    which is the recommended approach for SQLite.

    For higher concurrency, consider using WAL mode (already enabled)
    which allows concurrent reads while writes are serialized.
    """

    def __init__(self, db_path: str, pool_size: int = 1):
        """Initialize connection pool.

        Args:
            db_path: Path to SQLite database file.
            pool_size: Number of connections (currently always 1 for SQLite).
        """
        self._db_path = db_path
        self._pool_size = pool_size
        self._connection: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()
        self._connected = False

    async def connect(self) -> None:
        """Connect to the database."""
        async with self._lock:
            if self._connected:
                return

            # Ensure directory exists
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

            self._connection = await aiosqlite.connect(self._db_path)
            self._connection.row_factory = aiosqlite.Row

            # Enable WAL mode for better concurrent read performance
            await self._connection.execute("PRAGMA journal_mode=WAL")
            await self._connection.execute("PRAGMA synchronous=NORMAL")
            await self._connection.execute("PRAGMA busy_timeout=5000")

            self._connected = True

    async def close(self) -> None:
        """Close all connections."""
        async with self._lock:
            if self._connection:
                await self._connection.close()
                self._connection = None
            self._connected = False

    @property
    def is_connected(self) -> bool:
        """Check if pool is connected."""
        return self._connected

    async def acquire(self) -> aiosqlite.Connection:
        """Acquire a connection from the pool.

        Returns:
            Database connection.

        Raises:
            RuntimeError: If pool is not connected.
        """
        if not self._connected or not self._connection:
            raise RuntimeError("Connection pool not connected")
        return self._connection

    @property
    def lock(self) -> asyncio.Lock:
        """Get the connection lock for write operations."""
        return self._lock


class MigrationRunner:
    """Handles database schema migrations.

    Migrations are defined in mercury.services.migrations module.
    Each migration has a VERSION number and UP_SQL to apply.
    """

    def __init__(self, pool: ConnectionPool):
        """Initialize migration runner.

        Args:
            pool: Database connection pool.
        """
        self._pool = pool
        self._log = log.bind(component="migration_runner")

    async def get_current_version(self) -> int:
        """Get current schema version from database.

        Returns:
            Current schema version (0 if no version table).
        """
        conn = await self._pool.acquire()
        try:
            async with conn.execute("SELECT version FROM schema_version LIMIT 1") as cursor:
                row = await cursor.fetchone()
                return row["version"] if row else 0
        except aiosqlite.OperationalError:
            # Table doesn't exist yet
            return 0

    async def run_migrations(self) -> list[int]:
        """Run pending migrations.

        Returns:
            List of migration versions that were applied.
        """
        current = await self.get_current_version()
        self._log.info("checking_migrations", current_version=current)

        # Import migrations dynamically
        try:
            from mercury.services.migrations import v001_port_legacy_schema
            migrations = [v001_port_legacy_schema]
        except ImportError:
            migrations = []

        applied = []
        for migration in migrations:
            if migration.VERSION > current:
                self._log.info(
                    "applying_migration",
                    version=migration.VERSION,
                    description=migration.DESCRIPTION,
                )

                conn = await self._pool.acquire()
                async with self._pool.lock:
                    try:
                        # Run migration SQL statements one at a time
                        # (ALTER TABLE doesn't work well in executescript)
                        for statement in migration.UP_SQL.split(";"):
                            statement = statement.strip()
                            if statement and not statement.startswith("--"):
                                try:
                                    await conn.execute(statement)
                                except aiosqlite.OperationalError as e:
                                    # Column/table already exists - skip
                                    if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
                                        self._log.debug("migration_skip_exists", statement=statement[:50])
                                    else:
                                        raise

                        await conn.commit()
                        applied.append(migration.VERSION)
                        self._log.info("migration_applied", version=migration.VERSION)
                    except Exception as e:
                        self._log.error("migration_failed", version=migration.VERSION, error=str(e))
                        raise

        return applied


class StateStore:
    """SQLite-based persistence for trading state.

    Stores:
    - Trade history (with execution tracking, liquidity context)
    - Open and closed positions
    - Settlement queue (with claim tracking)
    - Daily statistics (with opportunities tracking)
    - Fill records for slippage analysis
    - Trade telemetry for execution timing
    - Rebalance trades
    - Circuit breaker state
    - Realized PnL ledger

    Uses connection pooling for concurrent access with proper locking
    for write operations. Supports schema migrations.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        config: Optional[ConfigManager] = None,
        event_bus: Optional[EventBus] = None,
    ):
        """Initialize the state store.

        Args:
            db_path: Direct path to database file (takes precedence).
            config: Configuration manager for default settings.
            event_bus: Optional EventBus for event subscription.
        """
        self._config = config
        self._event_bus = event_bus
        self._log = log.bind(component="state_store")

        # Determine database path
        if db_path:
            self._db_path = db_path
        elif config:
            self._db_path = config.get("database.path", "./data/mercury.db")
        else:
            self._db_path = "./data/mercury.db"

        self._pool = ConnectionPool(self._db_path)
        self._migration_runner = MigrationRunner(self._pool)
        self._start_time: Optional[float] = None

    @property
    def is_connected(self) -> bool:
        """Check if database is connected."""
        return self._pool.is_connected

    async def connect(self) -> None:
        """Connect to database and run migrations."""
        import time

        self._start_time = time.time()
        self._log.info("connecting_state_store", db_path=str(self._db_path))

        await self._pool.connect()

        # Run base schema
        conn = await self._pool.acquire()
        async with self._pool.lock:
            await conn.executescript(SCHEMA_SQL)
            await conn.commit()

        # Run migrations
        applied = await self._migration_runner.run_migrations()
        if applied:
            self._log.info("migrations_applied", versions=applied)

        # Subscribe to events
        if self._event_bus:
            await self._event_bus.subscribe("order.filled", self._on_order_filled)
            await self._event_bus.subscribe("position.opened", self._on_position_opened)
            await self._event_bus.subscribe("position.closed", self._on_position_closed)

        self._log.info("state_store_connected")

    async def close(self) -> None:
        """Close database connection."""
        await self._pool.close()
        self._log.info("state_store_closed")

    async def _get_tables(self) -> list[str]:
        """Get list of tables in the database.

        Returns:
            List of table names.
        """
        conn = await self._pool.acquire()
        tables = []
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ) as cursor:
            async for row in cursor:
                tables.append(row["name"])
        return tables

    async def get_schema_version(self) -> int:
        """Get current schema version.

        Returns:
            Schema version number.
        """
        return await self._migration_runner.get_current_version()

    # ============ Trade Operations ============

    async def save_trade(self, trade: Trade) -> None:
        """Save a trade record."""
        conn = await self._pool.acquire()
        async with self._pool.lock:
            await conn.execute(
                """
                INSERT OR REPLACE INTO trades
                (trade_id, market_id, strategy, side, size, price, cost,
                 status, timestamp, filled_size, avg_fill_price, fee, updated_at,
                 condition_id, asset, yes_price, no_price, yes_cost, no_cost,
                 spread, expected_profit, actual_profit, market_end_time, market_slug,
                 dry_run, yes_shares, no_shares, hedge_ratio, execution_status,
                 yes_order_status, no_order_status, yes_liquidity_at_price,
                 no_liquidity_at_price, yes_book_depth_total, no_book_depth_total,
                 resolved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.trade_id,
                    trade.market_id,
                    trade.strategy,
                    trade.side,
                    float(trade.size),
                    float(trade.price),
                    float(trade.cost),
                    trade.status,
                    trade.timestamp,
                    float(trade.filled_size),
                    float(trade.avg_fill_price) if trade.avg_fill_price else None,
                    float(trade.fee),
                    datetime.now(timezone.utc),
                    trade.condition_id,
                    trade.asset,
                    float(trade.yes_price) if trade.yes_price else None,
                    float(trade.no_price) if trade.no_price else None,
                    float(trade.yes_cost) if trade.yes_cost else None,
                    float(trade.no_cost) if trade.no_cost else None,
                    float(trade.spread) if trade.spread else None,
                    float(trade.expected_profit) if trade.expected_profit else None,
                    float(trade.actual_profit) if trade.actual_profit else None,
                    trade.market_end_time,
                    trade.market_slug,
                    trade.dry_run,
                    float(trade.yes_shares) if trade.yes_shares else None,
                    float(trade.no_shares) if trade.no_shares else None,
                    float(trade.hedge_ratio) if trade.hedge_ratio else None,
                    trade.execution_status,
                    trade.yes_order_status,
                    trade.no_order_status,
                    float(trade.yes_liquidity_at_price) if trade.yes_liquidity_at_price else None,
                    float(trade.no_liquidity_at_price) if trade.no_liquidity_at_price else None,
                    float(trade.yes_book_depth_total) if trade.yes_book_depth_total else None,
                    float(trade.no_book_depth_total) if trade.no_book_depth_total else None,
                    trade.resolved_at,
                ),
            )
            await conn.commit()

        self._log.debug(
            "trade_saved",
            trade_id=trade.trade_id,
            market_id=trade.market_id,
        )

    async def get_trade(self, trade_id: str) -> Optional[Trade]:
        """Get a trade by ID."""
        conn = await self._pool.acquire()
        async with conn.execute(
            "SELECT * FROM trades WHERE trade_id = ?", (trade_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return None

        return self._row_to_trade(row)

    async def get_trades(
        self,
        since: Optional[datetime] = None,
        limit: int = 100,
        market_id: Optional[str] = None,
        strategy: Optional[str] = None,
        status: Optional[str] = None,
        exclude_dry_runs: bool = False,
    ) -> list[Trade]:
        """Get trades with optional filters.

        Args:
            since: Only return trades after this time.
            limit: Maximum number of trades to return.
            market_id: Filter by market ID.
            strategy: Filter by strategy name.
            status: Filter by status.
            exclude_dry_runs: Exclude dry run trades.

        Returns:
            List of trades ordered by timestamp descending.
        """
        conn = await self._pool.acquire()

        query = "SELECT * FROM trades WHERE 1=1"
        params: list[Any] = []

        if since:
            query += " AND timestamp >= ?"
            params.append(since)

        if market_id:
            query += " AND market_id = ?"
            params.append(market_id)

        if strategy:
            query += " AND strategy = ?"
            params.append(strategy)

        if status:
            query += " AND status = ?"
            params.append(status)

        if exclude_dry_runs:
            query += " AND dry_run = 0"

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        trades = []
        async with conn.execute(query, params) as cursor:
            async for row in cursor:
                trades.append(self._row_to_trade(row))

        return trades

    async def get_pending_trades(self) -> list[Trade]:
        """Get all pending trades."""
        return await self.get_trades(status="pending", limit=1000)

    async def resolve_trade(
        self,
        trade_id: str,
        actual_profit: Decimal,
        status: str = "resolved",
    ) -> None:
        """Update trade with resolution result.

        Args:
            trade_id: Trade to resolve.
            actual_profit: Actual profit/loss.
            status: New status (default 'resolved').
        """
        conn = await self._pool.acquire()
        async with self._pool.lock:
            await conn.execute(
                """
                UPDATE trades
                SET status = ?,
                    actual_profit = ?,
                    resolved_at = ?
                WHERE trade_id = ?
                """,
                (status, float(actual_profit), datetime.now(timezone.utc), trade_id),
            )
            await conn.commit()

        self._log.info(
            "trade_resolved",
            trade_id=trade_id,
            actual_profit=str(actual_profit),
            status=status,
        )

    def _row_to_trade(self, row: aiosqlite.Row) -> Trade:
        """Convert a database row to Trade."""
        # Helper to safely get value from row (sqlite3.Row doesn't have .get())
        def get_val(key: str, default=None):
            try:
                return row[key] if row[key] is not None else default
            except (KeyError, IndexError):
                return default

        return Trade(
            trade_id=row["trade_id"],
            market_id=row["market_id"],
            strategy=row["strategy"],
            side=row["side"],
            size=Decimal(str(row["size"])),
            price=Decimal(str(row["price"])),
            cost=Decimal(str(row["cost"])),
            status=row["status"],
            timestamp=row["timestamp"] if isinstance(row["timestamp"], datetime) else datetime.fromisoformat(row["timestamp"]) if row["timestamp"] else datetime.now(timezone.utc),
            filled_size=Decimal(str(row["filled_size"])) if row["filled_size"] else Decimal("0"),
            avg_fill_price=Decimal(str(row["avg_fill_price"])) if row["avg_fill_price"] else None,
            fee=Decimal(str(row["fee"])) if row["fee"] else Decimal("0"),
            condition_id=get_val("condition_id"),
            asset=get_val("asset"),
            yes_price=Decimal(str(get_val("yes_price"))) if get_val("yes_price") else None,
            no_price=Decimal(str(get_val("no_price"))) if get_val("no_price") else None,
            yes_cost=Decimal(str(get_val("yes_cost"))) if get_val("yes_cost") else None,
            no_cost=Decimal(str(get_val("no_cost"))) if get_val("no_cost") else None,
            spread=Decimal(str(get_val("spread"))) if get_val("spread") else None,
            expected_profit=Decimal(str(get_val("expected_profit"))) if get_val("expected_profit") else None,
            actual_profit=Decimal(str(get_val("actual_profit"))) if get_val("actual_profit") else None,
            market_end_time=get_val("market_end_time"),
            market_slug=get_val("market_slug"),
            dry_run=bool(get_val("dry_run", False)),
            yes_shares=Decimal(str(get_val("yes_shares"))) if get_val("yes_shares") else None,
            no_shares=Decimal(str(get_val("no_shares"))) if get_val("no_shares") else None,
            hedge_ratio=Decimal(str(get_val("hedge_ratio"))) if get_val("hedge_ratio") else None,
            execution_status=get_val("execution_status"),
            yes_order_status=get_val("yes_order_status"),
            no_order_status=get_val("no_order_status"),
            yes_liquidity_at_price=Decimal(str(get_val("yes_liquidity_at_price"))) if get_val("yes_liquidity_at_price") else None,
            no_liquidity_at_price=Decimal(str(get_val("no_liquidity_at_price"))) if get_val("no_liquidity_at_price") else None,
            yes_book_depth_total=Decimal(str(get_val("yes_book_depth_total"))) if get_val("yes_book_depth_total") else None,
            no_book_depth_total=Decimal(str(get_val("no_book_depth_total"))) if get_val("no_book_depth_total") else None,
            resolved_at=row["resolved_at"] if isinstance(get_val("resolved_at"), datetime) else datetime.fromisoformat(row["resolved_at"]) if get_val("resolved_at") else None,
        )

    # ============ Position Operations ============

    async def save_position(self, position: Position) -> None:
        """Save a position."""
        conn = await self._pool.acquire()
        async with self._pool.lock:
            await conn.execute(
                """
                INSERT OR REPLACE INTO positions
                (position_id, market_id, strategy, side, size, entry_price,
                 status, opened_at, closed_at, exit_price, realized_pnl, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position.position_id,
                    position.market_id,
                    position.strategy,
                    position.side,
                    float(position.size),
                    float(position.entry_price),
                    position.status,
                    position.opened_at,
                    position.closed_at,
                    float(position.exit_price) if position.exit_price else None,
                    float(position.realized_pnl) if position.realized_pnl else None,
                    datetime.now(timezone.utc),
                ),
            )
            await conn.commit()

        self._log.debug(
            "position_saved",
            position_id=position.position_id,
            status=position.status,
        )

    async def get_position(self, position_id: str) -> Optional[Position]:
        """Get a position by ID."""
        conn = await self._pool.acquire()
        async with conn.execute(
            "SELECT * FROM positions WHERE position_id = ?", (position_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return None

        return self._row_to_position(row)

    async def get_open_positions(
        self,
        market_id: Optional[str] = None,
        strategy: Optional[str] = None,
    ) -> list[Position]:
        """Get all open positions.

        Args:
            market_id: Filter by market ID.
            strategy: Filter by strategy name.

        Returns:
            List of open positions.
        """
        conn = await self._pool.acquire()

        query = "SELECT * FROM positions WHERE status = 'open'"
        params: list[Any] = []

        if market_id:
            query += " AND market_id = ?"
            params.append(market_id)

        if strategy:
            query += " AND strategy = ?"
            params.append(strategy)

        query += " ORDER BY opened_at DESC"

        positions = []
        async with conn.execute(query, params) as cursor:
            async for row in cursor:
                positions.append(self._row_to_position(row))

        return positions

    async def close_position(
        self,
        position_id: str,
        result: PositionResult,
    ) -> None:
        """Close a position with result.

        Args:
            position_id: Position to close.
            result: Result of closing (exit price, realized PnL).
        """
        conn = await self._pool.acquire()
        async with self._pool.lock:
            await conn.execute(
                """
                UPDATE positions
                SET status = 'closed',
                    closed_at = ?,
                    exit_price = ?,
                    realized_pnl = ?,
                    updated_at = ?
                WHERE position_id = ?
                """,
                (
                    result.closed_at,
                    float(result.exit_price),
                    float(result.realized_pnl),
                    datetime.now(timezone.utc),
                    position_id,
                ),
            )
            await conn.commit()

        self._log.info(
            "position_closed",
            position_id=position_id,
            exit_price=str(result.exit_price),
            realized_pnl=str(result.realized_pnl),
        )

    def _row_to_position(self, row: aiosqlite.Row) -> Position:
        """Convert a database row to Position."""
        return Position(
            position_id=row["position_id"],
            market_id=row["market_id"],
            strategy=row["strategy"],
            side=row["side"],
            size=Decimal(str(row["size"])),
            entry_price=Decimal(str(row["entry_price"])),
            status=row["status"],
            opened_at=row["opened_at"] if isinstance(row["opened_at"], datetime) else datetime.fromisoformat(row["opened_at"]) if row["opened_at"] else datetime.now(timezone.utc),
            closed_at=row["closed_at"] if isinstance(row["closed_at"], datetime) else datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None,
            exit_price=Decimal(str(row["exit_price"])) if row["exit_price"] else None,
            realized_pnl=Decimal(str(row["realized_pnl"])) if row["realized_pnl"] else None,
        )

    # ============ Settlement Queue ============

    async def queue_for_settlement(
        self,
        position: Position,
        condition_id: Optional[str] = None,
        token_id: Optional[str] = None,
        asset: Optional[str] = None,
        market_end_time: Optional[datetime] = None,
    ) -> None:
        """Add a position to the settlement queue.

        Args:
            position: Position to queue for settlement.
            condition_id: Market condition ID for settlement.
            token_id: Token ID for the position.
            asset: Asset symbol.
            market_end_time: When the market resolves.
        """
        conn = await self._pool.acquire()
        entry_cost = float(position.size * position.entry_price)
        async with self._pool.lock:
            await conn.execute(
                """
                INSERT OR REPLACE INTO settlement_queue
                (position_id, market_id, condition_id, side, size, entry_price, status,
                 token_id, asset, shares, entry_cost, market_end_time)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                """,
                (
                    position.position_id,
                    position.market_id,
                    condition_id,
                    position.side,
                    float(position.size),
                    float(position.entry_price),
                    token_id,
                    asset,
                    float(position.size),
                    entry_cost,
                    market_end_time,
                ),
            )
            await conn.commit()

        self._log.debug(
            "position_queued_for_settlement",
            position_id=position.position_id,
        )

    async def get_claimable_positions(
        self,
        max_attempts: int = 5,
        min_time_since_end_seconds: int = 0,
    ) -> list[Position]:
        """Get positions pending settlement.

        Args:
            max_attempts: Maximum claim attempts to include.
            min_time_since_end_seconds: Minimum seconds after market end.

        Returns:
            List of positions that can be claimed.
        """
        conn = await self._pool.acquire()

        query = """
            SELECT sq.*, p.strategy, p.opened_at
            FROM settlement_queue sq
            LEFT JOIN positions p ON sq.position_id = p.position_id
            WHERE sq.status = 'pending'
              AND (sq.claimed IS NULL OR sq.claimed = 0)
              AND (sq.claim_attempts IS NULL OR sq.claim_attempts < ?)
        """
        params: list[Any] = [max_attempts]

        if min_time_since_end_seconds > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=min_time_since_end_seconds)).isoformat()
            query += " AND (sq.market_end_time IS NULL OR sq.market_end_time <= ?)"
            params.append(cutoff)

        query += " ORDER BY sq.queued_at"

        positions = []
        async with conn.execute(query, params) as cursor:
            async for row in cursor:
                positions.append(
                    Position(
                        position_id=row["position_id"],
                        market_id=row["market_id"],
                        strategy=row["strategy"] or "unknown",
                        side=row["side"],
                        size=Decimal(str(row["size"])),
                        entry_price=Decimal(str(row["entry_price"])),
                        opened_at=row["opened_at"] if row["opened_at"] else datetime.now(timezone.utc),
                        status="pending_settlement",
                    )
                )

        return positions

    async def mark_claimed(
        self,
        position_id: str,
        proceeds: Decimal,
        profit: Optional[Decimal] = None,
    ) -> None:
        """Mark a position as successfully claimed.

        Args:
            position_id: Position that was claimed.
            proceeds: Settlement proceeds received.
            profit: Calculated profit (optional).
        """
        conn = await self._pool.acquire()
        async with self._pool.lock:
            await conn.execute(
                """
                UPDATE settlement_queue
                SET status = 'claimed',
                    claimed = 1,
                    claimed_at = ?,
                    proceeds = ?,
                    claim_proceeds = ?,
                    claim_profit = ?
                WHERE position_id = ?
                """,
                (
                    datetime.now(timezone.utc),
                    float(proceeds),
                    float(proceeds),
                    float(profit) if profit else None,
                    position_id,
                ),
            )
            await conn.commit()

        self._log.info(
            "position_claimed",
            position_id=position_id,
            proceeds=str(proceeds),
        )

    async def record_claim_attempt(
        self,
        position_id: str,
        error: Optional[str] = None,
    ) -> None:
        """Record a claim attempt.

        Args:
            position_id: Position ID.
            error: Error message if failed.
        """
        conn = await self._pool.acquire()
        async with self._pool.lock:
            await conn.execute(
                """
                UPDATE settlement_queue
                SET claim_attempts = COALESCE(claim_attempts, 0) + 1,
                    last_claim_error = ?
                WHERE position_id = ?
                """,
                (error, position_id),
            )
            await conn.commit()

    async def get_settlement_stats(self) -> dict[str, Any]:
        """Get settlement queue statistics.

        Returns:
            Dict with queue statistics.
        """
        conn = await self._pool.acquire()
        async with conn.execute(
            """
            SELECT
                COUNT(*) as total_positions,
                SUM(CASE WHEN claimed = 0 OR claimed IS NULL THEN 1 ELSE 0 END) as unclaimed,
                SUM(CASE WHEN claimed = 1 THEN 1 ELSE 0 END) as claimed_count,
                SUM(CASE WHEN claimed = 0 OR claimed IS NULL THEN COALESCE(shares, size) * entry_price ELSE 0 END) as unclaimed_value,
                SUM(CASE WHEN claimed = 1 THEN COALESCE(claim_profit, 0) ELSE 0 END) as total_claim_profit
            FROM settlement_queue
            """
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else {}

    # ============ Daily Stats ============

    async def get_daily_stats(self, for_date: Optional[date] = None) -> DailyStats:
        """Get statistics for a date.

        Args:
            for_date: Date to get stats for (defaults to today).

        Returns:
            DailyStats for the requested date.
        """
        conn = await self._pool.acquire()
        target_date = for_date or date.today()

        async with conn.execute(
            "SELECT * FROM daily_stats WHERE date = ?", (target_date.isoformat(),)
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            # Create empty stats for the date
            return DailyStats(date=target_date)

        # Helper to safely get value from row (sqlite3.Row doesn't have .get())
        def get_val(key: str, default=None):
            try:
                return row[key] if row[key] is not None else default
            except (KeyError, IndexError):
                return default

        return DailyStats(
            date=target_date,
            trade_count=row["trade_count"],
            volume_usd=Decimal(str(row["volume_usd"])),
            realized_pnl=Decimal(str(row["realized_pnl"])),
            positions_opened=row["positions_opened"],
            positions_closed=row["positions_closed"],
            wins=get_val("wins", 0) or 0,
            losses=get_val("losses", 0) or 0,
            exposure=Decimal(str(get_val("exposure", 0) or 0)),
            opportunities_detected=get_val("opportunities_detected", 0) or 0,
            opportunities_executed=get_val("opportunities_executed", 0) or 0,
        )

    async def update_daily_stats(self, stats: DailyStats) -> None:
        """Update daily statistics.

        Args:
            stats: Updated stats to save.
        """
        conn = await self._pool.acquire()
        async with self._pool.lock:
            await conn.execute(
                """
                INSERT INTO daily_stats
                (date, trade_count, volume_usd, realized_pnl, positions_opened, positions_closed,
                 wins, losses, exposure, opportunities_detected, opportunities_executed, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    trade_count = excluded.trade_count,
                    volume_usd = excluded.volume_usd,
                    realized_pnl = excluded.realized_pnl,
                    positions_opened = excluded.positions_opened,
                    positions_closed = excluded.positions_closed,
                    wins = excluded.wins,
                    losses = excluded.losses,
                    exposure = excluded.exposure,
                    opportunities_detected = excluded.opportunities_detected,
                    opportunities_executed = excluded.opportunities_executed,
                    updated_at = excluded.updated_at
                """,
                (
                    stats.date.isoformat(),
                    stats.trade_count,
                    float(stats.volume_usd),
                    float(stats.realized_pnl),
                    stats.positions_opened,
                    stats.positions_closed,
                    stats.wins,
                    stats.losses,
                    float(stats.exposure),
                    stats.opportunities_detected,
                    stats.opportunities_executed,
                    datetime.now(timezone.utc),
                ),
            )
            await conn.commit()

        self._log.debug(
            "daily_stats_updated",
            date=stats.date.isoformat(),
            trade_count=stats.trade_count,
        )

    async def increment_daily_stats(
        self,
        for_date: Optional[date] = None,
        trades: int = 0,
        volume: Decimal = Decimal("0"),
        realized_pnl: Decimal = Decimal("0"),
        positions_opened: int = 0,
        positions_closed: int = 0,
        wins: int = 0,
        losses: int = 0,
        exposure: Decimal = Decimal("0"),
        opportunities_detected: int = 0,
        opportunities_executed: int = 0,
    ) -> None:
        """Increment daily statistics.

        Args:
            for_date: Date to update (defaults to today).
            trades: Number of trades to add.
            volume: Volume to add.
            realized_pnl: Realized PnL to add.
            positions_opened: Positions opened to add.
            positions_closed: Positions closed to add.
            wins: Winning trades to add.
            losses: Losing trades to add.
            exposure: Exposure to add.
            opportunities_detected: Opportunities detected to add.
            opportunities_executed: Opportunities executed to add.
        """
        target_date = (for_date or date.today()).isoformat()
        conn = await self._pool.acquire()

        async with self._pool.lock:
            await conn.execute(
                """
                INSERT INTO daily_stats
                (date, trade_count, volume_usd, realized_pnl, positions_opened, positions_closed,
                 wins, losses, exposure, opportunities_detected, opportunities_executed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    trade_count = trade_count + excluded.trade_count,
                    volume_usd = volume_usd + excluded.volume_usd,
                    realized_pnl = realized_pnl + excluded.realized_pnl,
                    positions_opened = positions_opened + excluded.positions_opened,
                    positions_closed = positions_closed + excluded.positions_closed,
                    wins = COALESCE(wins, 0) + excluded.wins,
                    losses = COALESCE(losses, 0) + excluded.losses,
                    exposure = COALESCE(exposure, 0) + excluded.exposure,
                    opportunities_detected = COALESCE(opportunities_detected, 0) + excluded.opportunities_detected,
                    opportunities_executed = COALESCE(opportunities_executed, 0) + excluded.opportunities_executed,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    target_date, trades, float(volume), float(realized_pnl),
                    positions_opened, positions_closed, wins, losses, float(exposure),
                    opportunities_detected, opportunities_executed,
                ),
            )
            await conn.commit()

    # ============ Fill Operations ============

    async def save_fill(
        self,
        fill_id: str,
        trade_id: str,
        order_id: str,
        token_id: str,
        side: str,
        size: Decimal,
        price: Decimal,
        fee: Decimal = Decimal("0"),
    ) -> None:
        """Save a fill record for slippage analysis.

        Args:
            fill_id: Unique fill identifier.
            trade_id: Parent trade ID.
            order_id: Exchange order ID.
            token_id: Token that was filled.
            side: BUY or SELL.
            size: Fill size.
            price: Fill price.
            fee: Fee paid.
        """
        conn = await self._pool.acquire()
        async with self._pool.lock:
            await conn.execute(
                """
                INSERT OR REPLACE INTO fills
                (fill_id, trade_id, order_id, token_id, side, size, price, fee)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (fill_id, trade_id, order_id, token_id, side, float(size), float(price), float(fee)),
            )
            await conn.commit()

    # ============ Fill Records (Detailed) ============

    async def save_fill_record(self, record: FillRecord) -> int:
        """Save a detailed fill record for slippage analysis.

        Args:
            record: FillRecord to save.

        Returns:
            ID of the inserted record.
        """
        # Calculate ratios if not provided
        fill_ratio = record.fill_ratio
        if fill_ratio is None and record.intended_size > 0:
            fill_ratio = record.filled_size / record.intended_size

        persistence_ratio = record.persistence_ratio
        if persistence_ratio is None and record.pre_fill_depth > 0:
            persistence_ratio = record.filled_size / record.pre_fill_depth

        conn = await self._pool.acquire()
        async with self._pool.lock:
            cursor = await conn.execute(
                """
                INSERT INTO fill_records
                (token_id, condition_id, asset, side, intended_size, filled_size,
                 intended_price, actual_avg_price, time_to_fill_ms, slippage,
                 pre_fill_depth, post_fill_depth, order_type, order_id,
                 fill_ratio, persistence_ratio)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.token_id, record.condition_id, record.asset, record.side,
                    float(record.intended_size), float(record.filled_size),
                    float(record.intended_price), float(record.actual_avg_price),
                    record.time_to_fill_ms, float(record.slippage),
                    float(record.pre_fill_depth),
                    float(record.post_fill_depth) if record.post_fill_depth else None,
                    record.order_type, record.order_id,
                    float(fill_ratio) if fill_ratio else None,
                    float(persistence_ratio) if persistence_ratio else None,
                ),
            )
            await conn.commit()
            return cursor.lastrowid or 0

    async def get_fill_records(
        self,
        token_id: Optional[str] = None,
        asset: Optional[str] = None,
        limit: int = 100,
        lookback_minutes: Optional[int] = None,
    ) -> list[FillRecord]:
        """Get fill records with optional filters.

        Args:
            token_id: Filter by token.
            asset: Filter by asset.
            limit: Maximum records to return.
            lookback_minutes: Only get fills from last N minutes.

        Returns:
            List of FillRecord objects.
        """
        conn = await self._pool.acquire()

        query = "SELECT * FROM fill_records WHERE 1=1"
        params: list[Any] = []

        if token_id:
            query += " AND token_id = ?"
            params.append(token_id)

        if asset:
            query += " AND asset = ?"
            params.append(asset)

        if lookback_minutes:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)).isoformat()
            query += " AND timestamp >= ?"
            params.append(cutoff)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        records = []
        async with conn.execute(query, params) as cursor:
            async for row in cursor:
                records.append(
                    FillRecord(
                        id=row["id"],
                        timestamp=row["timestamp"],
                        token_id=row["token_id"],
                        condition_id=row["condition_id"],
                        asset=row["asset"],
                        side=row["side"],
                        intended_size=Decimal(str(row["intended_size"])),
                        filled_size=Decimal(str(row["filled_size"])),
                        intended_price=Decimal(str(row["intended_price"])),
                        actual_avg_price=Decimal(str(row["actual_avg_price"])),
                        time_to_fill_ms=row["time_to_fill_ms"],
                        slippage=Decimal(str(row["slippage"])),
                        pre_fill_depth=Decimal(str(row["pre_fill_depth"])),
                        post_fill_depth=Decimal(str(row["post_fill_depth"])) if row["post_fill_depth"] else None,
                        order_type=row["order_type"],
                        order_id=row["order_id"],
                        fill_ratio=Decimal(str(row["fill_ratio"])) if row["fill_ratio"] else None,
                        persistence_ratio=Decimal(str(row["persistence_ratio"])) if row["persistence_ratio"] else None,
                    )
                )

        return records

    async def get_slippage_stats(
        self,
        token_id: Optional[str] = None,
        asset: Optional[str] = None,
        lookback_minutes: int = 60,
    ) -> dict[str, Any]:
        """Get aggregated slippage statistics.

        Args:
            token_id: Filter by token.
            asset: Filter by asset.
            lookback_minutes: Analysis window.

        Returns:
            Dict with slippage statistics.
        """
        conn = await self._pool.acquire()

        query = """
            SELECT
                COUNT(*) as fill_count,
                AVG(slippage) as avg_slippage,
                MAX(slippage) as max_slippage,
                MIN(slippage) as min_slippage,
                AVG(fill_ratio) as avg_fill_ratio,
                AVG(persistence_ratio) as avg_persistence_ratio,
                AVG(time_to_fill_ms) as avg_time_to_fill_ms,
                SUM(filled_size) as total_volume
            FROM fill_records
            WHERE timestamp >= ?
        """
        # Use SQLite-compatible timestamp format (YYYY-MM-DD HH:MM:SS)
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        params: list[Any] = [cutoff]

        if token_id:
            query += " AND token_id = ?"
            params.append(token_id)

        if asset:
            query += " AND asset = ?"
            params.append(asset)

        async with conn.execute(query, params) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else {}

    # ============ Trade Telemetry ============

    async def save_trade_telemetry(self, telemetry: TradeTelemetry) -> None:
        """Save trade telemetry data.

        Args:
            telemetry: TradeTelemetry to save.
        """
        conn = await self._pool.acquire()
        async with self._pool.lock:
            await conn.execute(
                """
                INSERT OR REPLACE INTO trade_telemetry
                (trade_id, opportunity_detected_at, opportunity_spread,
                 opportunity_yes_price, opportunity_no_price,
                 order_placed_at, order_filled_at, execution_latency_ms, fill_latency_ms,
                 initial_yes_shares, initial_no_shares, initial_hedge_ratio,
                 rebalance_started_at, rebalance_attempts, position_balanced_at,
                 resolved_at, final_yes_shares, final_no_shares, final_hedge_ratio, actual_profit)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    telemetry.trade_id,
                    telemetry.opportunity_detected_at,
                    float(telemetry.opportunity_spread) if telemetry.opportunity_spread else None,
                    float(telemetry.opportunity_yes_price) if telemetry.opportunity_yes_price else None,
                    float(telemetry.opportunity_no_price) if telemetry.opportunity_no_price else None,
                    telemetry.order_placed_at,
                    telemetry.order_filled_at,
                    float(telemetry.execution_latency_ms) if telemetry.execution_latency_ms else None,
                    float(telemetry.fill_latency_ms) if telemetry.fill_latency_ms else None,
                    float(telemetry.initial_yes_shares) if telemetry.initial_yes_shares else None,
                    float(telemetry.initial_no_shares) if telemetry.initial_no_shares else None,
                    float(telemetry.initial_hedge_ratio) if telemetry.initial_hedge_ratio else None,
                    telemetry.rebalance_started_at,
                    telemetry.rebalance_attempts,
                    telemetry.position_balanced_at,
                    telemetry.resolved_at,
                    float(telemetry.final_yes_shares) if telemetry.final_yes_shares else None,
                    float(telemetry.final_no_shares) if telemetry.final_no_shares else None,
                    float(telemetry.final_hedge_ratio) if telemetry.final_hedge_ratio else None,
                    float(telemetry.actual_profit) if telemetry.actual_profit else None,
                ),
            )
            await conn.commit()

    async def get_trade_telemetry(self, trade_id: str) -> Optional[TradeTelemetry]:
        """Get telemetry for a specific trade.

        Args:
            trade_id: Trade ID.

        Returns:
            TradeTelemetry or None.
        """
        conn = await self._pool.acquire()
        async with conn.execute(
            "SELECT * FROM trade_telemetry WHERE trade_id = ?", (trade_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return None

        return TradeTelemetry(
            trade_id=row["trade_id"],
            opportunity_detected_at=row["opportunity_detected_at"],
            opportunity_spread=Decimal(str(row["opportunity_spread"])) if row["opportunity_spread"] else None,
            opportunity_yes_price=Decimal(str(row["opportunity_yes_price"])) if row["opportunity_yes_price"] else None,
            opportunity_no_price=Decimal(str(row["opportunity_no_price"])) if row["opportunity_no_price"] else None,
            order_placed_at=row["order_placed_at"],
            order_filled_at=row["order_filled_at"],
            execution_latency_ms=Decimal(str(row["execution_latency_ms"])) if row["execution_latency_ms"] else None,
            fill_latency_ms=Decimal(str(row["fill_latency_ms"])) if row["fill_latency_ms"] else None,
            initial_yes_shares=Decimal(str(row["initial_yes_shares"])) if row["initial_yes_shares"] else None,
            initial_no_shares=Decimal(str(row["initial_no_shares"])) if row["initial_no_shares"] else None,
            initial_hedge_ratio=Decimal(str(row["initial_hedge_ratio"])) if row["initial_hedge_ratio"] else None,
            rebalance_started_at=row["rebalance_started_at"],
            rebalance_attempts=row["rebalance_attempts"] or 0,
            position_balanced_at=row["position_balanced_at"],
            resolved_at=row["resolved_at"],
            final_yes_shares=Decimal(str(row["final_yes_shares"])) if row["final_yes_shares"] else None,
            final_no_shares=Decimal(str(row["final_no_shares"])) if row["final_no_shares"] else None,
            final_hedge_ratio=Decimal(str(row["final_hedge_ratio"])) if row["final_hedge_ratio"] else None,
            actual_profit=Decimal(str(row["actual_profit"])) if row["actual_profit"] else None,
        )

    # ============ Rebalance Trades ============

    async def save_rebalance_trade(self, rebalance: RebalanceTrade) -> int:
        """Save a rebalancing trade record.

        Args:
            rebalance: RebalanceTrade to save.

        Returns:
            ID of the inserted record.
        """
        conn = await self._pool.acquire()
        async with self._pool.lock:
            cursor = await conn.execute(
                """
                INSERT INTO rebalance_trades
                (trade_id, attempted_at, action, shares, price, status,
                 filled_shares, profit, error, order_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rebalance.trade_id,
                    rebalance.attempted_at or datetime.now(timezone.utc),
                    rebalance.action,
                    float(rebalance.shares),
                    float(rebalance.price),
                    rebalance.status,
                    float(rebalance.filled_shares),
                    float(rebalance.profit),
                    rebalance.error,
                    rebalance.order_id,
                ),
            )
            await conn.commit()
            return cursor.lastrowid or 0

    async def get_rebalance_trades(self, trade_id: str) -> list[RebalanceTrade]:
        """Get all rebalancing trades for a position.

        Args:
            trade_id: Parent trade ID.

        Returns:
            List of RebalanceTrade records.
        """
        conn = await self._pool.acquire()

        trades = []
        async with conn.execute(
            "SELECT * FROM rebalance_trades WHERE trade_id = ? ORDER BY attempted_at ASC",
            (trade_id,),
        ) as cursor:
            async for row in cursor:
                trades.append(
                    RebalanceTrade(
                        id=row["id"],
                        trade_id=row["trade_id"],
                        attempted_at=row["attempted_at"],
                        action=row["action"],
                        shares=Decimal(str(row["shares"])),
                        price=Decimal(str(row["price"])),
                        status=row["status"],
                        filled_shares=Decimal(str(row["filled_shares"])) if row["filled_shares"] else Decimal("0"),
                        profit=Decimal(str(row["profit"])) if row["profit"] else Decimal("0"),
                        error=row["error"],
                        order_id=row["order_id"],
                    )
                )

        return trades

    # ============ Circuit Breaker ============

    async def get_circuit_breaker_state(self) -> CircuitBreakerState:
        """Get current circuit breaker state.

        Handles daily reset automatically.

        Returns:
            CircuitBreakerState.
        """
        conn = await self._pool.acquire()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        async with conn.execute(
            "SELECT * FROM circuit_breaker_state WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            # Initialize if not exists
            async with self._pool.lock:
                await conn.execute(
                    "INSERT INTO circuit_breaker_state (id, date) VALUES (1, ?)",
                    (today,),
                )
                await conn.commit()
            return CircuitBreakerState(date=today)

        state = CircuitBreakerState(
            date=row["date"],
            realized_pnl=Decimal(str(row["realized_pnl"] or 0)),
            circuit_breaker_hit=bool(row["circuit_breaker_hit"]),
            hit_at=row["hit_at"],
            hit_reason=row["hit_reason"],
            total_trades_today=row["total_trades_today"] or 0,
        )

        # Check if we need to reset for new day
        if state.date != today:
            self._log.info(
                "new_trading_day",
                previous_date=state.date,
                previous_pnl=str(state.realized_pnl),
                new_date=today,
            )
            async with self._pool.lock:
                await conn.execute(
                    """
                    UPDATE circuit_breaker_state
                    SET date = ?,
                        realized_pnl = 0.0,
                        circuit_breaker_hit = 0,
                        hit_at = NULL,
                        hit_reason = NULL,
                        total_trades_today = 0,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                    """,
                    (today,),
                )
                await conn.commit()
            return CircuitBreakerState(date=today)

        return state

    async def record_realized_pnl(
        self,
        trade_id: str,
        pnl_amount: Decimal,
        pnl_type: str,
        max_daily_loss: Decimal,
        notes: Optional[str] = None,
    ) -> CircuitBreakerState:
        """Record realized P&L and check circuit breaker.

        Args:
            trade_id: Associated trade ID.
            pnl_amount: Actual profit/loss amount.
            pnl_type: Type of P&L ('resolution', 'settlement', 'rebalance').
            max_daily_loss: Maximum daily loss threshold (positive number).
            notes: Optional notes.

        Returns:
            Updated circuit breaker state.
        """
        conn = await self._pool.acquire()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        async with self._pool.lock:
            # Insert into ledger
            try:
                await conn.execute(
                    """
                    INSERT INTO realized_pnl_ledger (trade_id, trade_date, pnl_amount, pnl_type, notes)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (trade_id, today, float(pnl_amount), pnl_type, notes),
                )
            except aiosqlite.IntegrityError:
                # Duplicate entry - already recorded
                self._log.debug("pnl_already_recorded", trade_id=trade_id, pnl_type=pnl_type)
                return await self.get_circuit_breaker_state()

            # Get current state
            state = await self.get_circuit_breaker_state()
            new_pnl = state.realized_pnl + pnl_amount
            new_trade_count = state.total_trades_today + 1

            # Check circuit breaker
            circuit_breaker_hit = state.circuit_breaker_hit
            hit_at = state.hit_at
            hit_reason = state.hit_reason

            if not circuit_breaker_hit and new_pnl <= -max_daily_loss:
                circuit_breaker_hit = True
                hit_at = datetime.now(timezone.utc)
                hit_reason = f"Daily loss limit exceeded: ${abs(new_pnl):.2f} >= ${max_daily_loss:.2f}"
                self._log.warning(
                    "circuit_breaker_triggered",
                    realized_pnl=str(new_pnl),
                    max_daily_loss=str(max_daily_loss),
                )

            # Update state
            await conn.execute(
                """
                UPDATE circuit_breaker_state
                SET realized_pnl = ?,
                    circuit_breaker_hit = ?,
                    hit_at = ?,
                    hit_reason = ?,
                    total_trades_today = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
                """,
                (float(new_pnl), circuit_breaker_hit, hit_at, hit_reason, new_trade_count),
            )
            await conn.commit()

        self._log.info(
            "realized_pnl_recorded",
            trade_id=trade_id[:8] + "..." if len(trade_id) > 8 else trade_id,
            pnl=str(pnl_amount),
            pnl_type=pnl_type,
            daily_total=str(new_pnl),
        )

        return CircuitBreakerState(
            date=today,
            realized_pnl=new_pnl,
            circuit_breaker_hit=circuit_breaker_hit,
            hit_at=hit_at,
            hit_reason=hit_reason,
            total_trades_today=new_trade_count,
        )

    async def reset_circuit_breaker(self, reason: str = "Manual reset") -> None:
        """Manually reset circuit breaker (keeps PnL, clears hit flag).

        Args:
            reason: Reason for manual reset.
        """
        conn = await self._pool.acquire()
        async with self._pool.lock:
            await conn.execute(
                """
                UPDATE circuit_breaker_state
                SET circuit_breaker_hit = 0,
                    hit_at = NULL,
                    hit_reason = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
                """
            )
            await conn.commit()

        self._log.info("circuit_breaker_reset", reason=reason)

    # ============ Realized PnL Ledger ============

    async def get_total_realized_pnl(self) -> Decimal:
        """Get total realized P&L from the ledger.

        Returns:
            Total realized P&L.
        """
        conn = await self._pool.acquire()
        async with conn.execute(
            "SELECT COALESCE(SUM(pnl_amount), 0) as total FROM realized_pnl_ledger"
        ) as cursor:
            row = await cursor.fetchone()
            return Decimal(str(row["total"])) if row else Decimal("0")

    async def get_today_realized_pnl(self) -> Decimal:
        """Get today's realized P&L from the ledger.

        Returns:
            Today's realized P&L.
        """
        conn = await self._pool.acquire()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        async with conn.execute(
            "SELECT COALESCE(SUM(pnl_amount), 0) as total FROM realized_pnl_ledger WHERE trade_date = ?",
            (today,),
        ) as cursor:
            row = await cursor.fetchone()
            return Decimal(str(row["total"])) if row else Decimal("0")

    async def get_pnl_by_type(self) -> dict[str, Decimal]:
        """Get P&L breakdown by type.

        Returns:
            Dict mapping pnl_type to total amount.
        """
        conn = await self._pool.acquire()
        result: dict[str, Decimal] = {}
        async with conn.execute(
            """
            SELECT pnl_type, COALESCE(SUM(pnl_amount), 0) as total
            FROM realized_pnl_ledger
            GROUP BY pnl_type
            """
        ) as cursor:
            async for row in cursor:
                result[row["pnl_type"]] = Decimal(str(row["total"]))
        return result

    async def get_daily_pnl_breakdown(self, for_date: Optional[str] = None) -> list[RealizedPnlEntry]:
        """Get P&L breakdown for a specific day.

        Args:
            for_date: Date in YYYY-MM-DD format (default: today).

        Returns:
            List of P&L entries for the day.
        """
        conn = await self._pool.acquire()
        target_date = for_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        entries = []
        async with conn.execute(
            """
            SELECT * FROM realized_pnl_ledger
            WHERE trade_date = ?
            ORDER BY created_at ASC
            """,
            (target_date,),
        ) as cursor:
            async for row in cursor:
                entries.append(
                    RealizedPnlEntry(
                        id=row["id"],
                        created_at=row["created_at"],
                        trade_id=row["trade_id"],
                        trade_date=row["trade_date"],
                        pnl_amount=Decimal(str(row["pnl_amount"])),
                        pnl_type=row["pnl_type"],
                        notes=row["notes"],
                    )
                )
        return entries

    # ============ Event Handlers ============

    async def _on_order_filled(self, data: dict) -> None:
        """Handle order filled event."""
        # Record fill details for slippage analysis
        if "fill_id" in data:
            await self.save_fill(
                fill_id=data["fill_id"],
                trade_id=data.get("trade_id", ""),
                order_id=data.get("order_id", ""),
                token_id=data.get("token_id", ""),
                side=data.get("side", ""),
                size=Decimal(str(data.get("size", 0))),
                price=Decimal(str(data.get("price", 0))),
                fee=Decimal(str(data.get("fee", 0))),
            )

    async def _on_position_opened(self, data: dict) -> None:
        """Handle position opened event."""
        await self.increment_daily_stats(positions_opened=1)

    async def _on_position_closed(self, data: dict) -> None:
        """Handle position closed event."""
        realized = Decimal(str(data.get("realized_pnl", 0)))
        await self.increment_daily_stats(positions_closed=1, realized_pnl=realized)

    # ============ Database Statistics ============

    async def get_database_stats(self) -> dict[str, Any]:
        """Get database table sizes and row counts.

        Returns:
            Dict with table names and their row counts.
        """
        conn = await self._pool.acquire()
        tables = [
            "trades", "positions", "settlement_queue", "daily_stats",
            "fills", "fill_records", "trade_telemetry", "rebalance_trades",
            "circuit_breaker_state", "realized_pnl_ledger",
        ]

        stats: dict[str, Any] = {}
        for table in tables:
            try:
                async with conn.execute(f"SELECT COUNT(*) as count FROM {table}") as cursor:
                    row = await cursor.fetchone()
                    stats[table] = row["count"] if row else 0
            except aiosqlite.OperationalError:
                stats[table] = -1  # Table doesn't exist

        # Get database file size
        try:
            stats["db_size_mb"] = round(Path(self._db_path).stat().st_size / (1024 * 1024), 2)
        except Exception:
            stats["db_size_mb"] = -1

        return stats

    # ============ Health Check ============

    async def health_check(self) -> dict[str, Any]:
        """Check database health.

        Returns:
            Health check result dictionary.
        """
        if not self.is_connected:
            return {
                "status": "unhealthy",
                "message": "Database not connected",
            }

        try:
            conn = await self._pool.acquire()
            async with conn.execute("SELECT 1") as cursor:
                await cursor.fetchone()

            schema_version = await self.get_schema_version()

            return {
                "status": "healthy",
                "message": "Database connected",
                "db_path": self._db_path,
                "schema_version": schema_version,
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "message": f"Database error: {e}",
            }
