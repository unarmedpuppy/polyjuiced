"""State Store Service - Async SQLite persistence layer.

This service:
- Persists trades, positions, and settlement queue
- Provides query interface for historical data
- Tracks daily statistics
- Uses connection pooling for concurrent access
"""

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import aiosqlite
import structlog

from mercury.core.config import ConfigManager
from mercury.core.events import EventBus

log = structlog.get_logger()

# Schema version for migrations
SCHEMA_VERSION = 1

SCHEMA_SQL = """
-- Trades table
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
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

-- Settlement queue
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
    status TEXT DEFAULT 'pending'
);

-- Daily statistics
CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    trade_count INTEGER DEFAULT 0,
    volume_usd REAL DEFAULT 0,
    realized_pnl REAL DEFAULT 0,
    positions_opened INTEGER DEFAULT 0,
    positions_closed INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Fills for slippage analysis
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

-- Indexes
CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_id);
CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id);
CREATE INDEX IF NOT EXISTS idx_settlement_status ON settlement_queue(status);
CREATE INDEX IF NOT EXISTS idx_fills_trade ON fills(trade_id);
CREATE INDEX IF NOT EXISTS idx_daily_stats_date ON daily_stats(date);

-- Schema version
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
INSERT OR IGNORE INTO schema_version (version) VALUES (1);
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


class StateStore:
    """SQLite-based persistence for trading state.

    Stores:
    - Trade history
    - Open and closed positions
    - Settlement queue
    - Daily statistics
    - Fill records for slippage analysis

    Uses connection pooling for concurrent access with proper locking
    for write operations.
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

        # Run schema
        conn = await self._pool.acquire()
        async with self._pool.lock:
            await conn.executescript(SCHEMA_SQL)
            await conn.commit()

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

    # ============ Trade Operations ============

    async def save_trade(self, trade: Trade) -> None:
        """Save a trade record."""
        conn = await self._pool.acquire()
        async with self._pool.lock:
            await conn.execute(
                """
                INSERT OR REPLACE INTO trades
                (trade_id, market_id, strategy, side, size, price, cost,
                 status, timestamp, filled_size, avg_fill_price, fee, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    ) -> list[Trade]:
        """Get trades with optional filters.

        Args:
            since: Only return trades after this time.
            limit: Maximum number of trades to return.
            market_id: Filter by market ID.
            strategy: Filter by strategy name.

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

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        trades = []
        async with conn.execute(query, params) as cursor:
            async for row in cursor:
                trades.append(self._row_to_trade(row))

        return trades

    def _row_to_trade(self, row: aiosqlite.Row) -> Trade:
        """Convert a database row to Trade."""
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

    async def queue_for_settlement(self, position: Position, condition_id: Optional[str] = None) -> None:
        """Add a position to the settlement queue.

        Args:
            position: Position to queue for settlement.
            condition_id: Market condition ID for settlement.
        """
        conn = await self._pool.acquire()
        async with self._pool.lock:
            await conn.execute(
                """
                INSERT OR REPLACE INTO settlement_queue
                (position_id, market_id, condition_id, side, size, entry_price, status)
                VALUES (?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    position.position_id,
                    position.market_id,
                    condition_id,
                    position.side,
                    float(position.size),
                    float(position.entry_price),
                ),
            )
            await conn.commit()

        self._log.debug(
            "position_queued_for_settlement",
            position_id=position.position_id,
        )

    async def get_claimable_positions(self, max_attempts: int = 5) -> list[Position]:
        """Get positions pending settlement.

        Returns:
            List of positions that can be claimed.
        """
        conn = await self._pool.acquire()

        positions = []
        async with conn.execute(
            """
            SELECT sq.*, p.strategy, p.opened_at
            FROM settlement_queue sq
            LEFT JOIN positions p ON sq.position_id = p.position_id
            WHERE sq.status = 'pending'
            ORDER BY sq.queued_at
            """
        ) as cursor:
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
    ) -> None:
        """Mark a position as successfully claimed.

        Args:
            position_id: Position that was claimed.
            proceeds: Settlement proceeds received.
        """
        conn = await self._pool.acquire()
        async with self._pool.lock:
            await conn.execute(
                """
                UPDATE settlement_queue
                SET status = 'claimed',
                    claimed_at = ?,
                    proceeds = ?
                WHERE position_id = ?
                """,
                (datetime.now(timezone.utc), float(proceeds), position_id),
            )
            await conn.commit()

        self._log.info(
            "position_claimed",
            position_id=position_id,
            proceeds=str(proceeds),
        )

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

        return DailyStats(
            date=target_date,
            trade_count=row["trade_count"],
            volume_usd=Decimal(str(row["volume_usd"])),
            realized_pnl=Decimal(str(row["realized_pnl"])),
            positions_opened=row["positions_opened"],
            positions_closed=row["positions_closed"],
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
                (date, trade_count, volume_usd, realized_pnl, positions_opened, positions_closed, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    trade_count = excluded.trade_count,
                    volume_usd = excluded.volume_usd,
                    realized_pnl = excluded.realized_pnl,
                    positions_opened = excluded.positions_opened,
                    positions_closed = excluded.positions_closed,
                    updated_at = excluded.updated_at
                """,
                (
                    stats.date.isoformat(),
                    stats.trade_count,
                    float(stats.volume_usd),
                    float(stats.realized_pnl),
                    stats.positions_opened,
                    stats.positions_closed,
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
    ) -> None:
        """Increment daily statistics.

        Args:
            for_date: Date to update (defaults to today).
            trades: Number of trades to add.
            volume: Volume to add.
            realized_pnl: Realized PnL to add.
            positions_opened: Positions opened to add.
            positions_closed: Positions closed to add.
        """
        target_date = (for_date or date.today()).isoformat()
        conn = await self._pool.acquire()

        async with self._pool.lock:
            await conn.execute(
                """
                INSERT INTO daily_stats
                (date, trade_count, volume_usd, realized_pnl, positions_opened, positions_closed)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    trade_count = trade_count + excluded.trade_count,
                    volume_usd = volume_usd + excluded.volume_usd,
                    realized_pnl = realized_pnl + excluded.realized_pnl,
                    positions_opened = positions_opened + excluded.positions_opened,
                    positions_closed = positions_closed + excluded.positions_closed,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (target_date, trades, float(volume), float(realized_pnl), positions_opened, positions_closed),
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

            return {
                "status": "healthy",
                "message": "Database connected",
                "db_path": self._db_path,
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "message": f"Database error: {e}",
            }
