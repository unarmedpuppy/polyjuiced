"""State Store Service - SQLite persistence layer.

This service:
- Persists trades, positions, and settlement queue
- Provides query interface for historical data
- Tracks daily statistics
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import aiosqlite
import structlog

from mercury.core.config import ConfigManager
from mercury.core.events import EventBus
from mercury.core.lifecycle import BaseComponent, HealthCheckResult, HealthStatus
from mercury.domain.order import Fill, Position, PositionStatus

log = structlog.get_logger()

# Schema version for migrations
SCHEMA_VERSION = 1

SCHEMA_SQL = """
-- Trades table
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    side TEXT NOT NULL,
    yes_token_id TEXT,
    no_token_id TEXT,
    yes_size REAL DEFAULT 0,
    no_size REAL DEFAULT 0,
    yes_price REAL DEFAULT 0,
    no_price REAL DEFAULT 0,
    total_cost REAL NOT NULL,
    guaranteed_pnl REAL DEFAULT 0,
    status TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Positions table
CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    trade_id TEXT REFERENCES trades(id),
    yes_shares REAL DEFAULT 0,
    no_shares REAL DEFAULT 0,
    cost_basis REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMP,
    settlement_proceeds REAL,
    realized_pnl REAL
);

-- Settlement queue
CREATE TABLE IF NOT EXISTS settlement_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT REFERENCES positions(id),
    market_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    queued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    attempts INTEGER DEFAULT 0,
    last_attempt TIMESTAMP,
    status TEXT DEFAULT 'PENDING',
    error TEXT
);

-- Daily statistics
CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    total_trades INTEGER DEFAULT 0,
    total_volume REAL DEFAULT 0,
    realized_pnl REAL DEFAULT 0,
    unrealized_pnl REAL DEFAULT 0,
    positions_opened INTEGER DEFAULT 0,
    positions_closed INTEGER DEFAULT 0
);

-- Fills for slippage analysis
CREATE TABLE IF NOT EXISTS fills (
    id TEXT PRIMARY KEY,
    trade_id TEXT REFERENCES trades(id),
    order_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    requested_size REAL NOT NULL,
    filled_size REAL NOT NULL,
    requested_price REAL NOT NULL,
    filled_price REAL NOT NULL,
    slippage_cents REAL,
    latency_ms REAL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_id);
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_settlement_status ON settlement_queue(status);
CREATE INDEX IF NOT EXISTS idx_fills_trade ON fills(trade_id);

-- Schema version
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
INSERT OR IGNORE INTO schema_version (version) VALUES (1);
"""


@dataclass
class Trade:
    """A recorded trade."""

    id: str
    market_id: str
    strategy: str
    side: str
    total_cost: Decimal
    status: str
    yes_token_id: Optional[str] = None
    no_token_id: Optional[str] = None
    yes_size: Decimal = Decimal("0")
    no_size: Decimal = Decimal("0")
    yes_price: Decimal = Decimal("0")
    no_price: Decimal = Decimal("0")
    guaranteed_pnl: Decimal = Decimal("0")
    created_at: Optional[datetime] = None


@dataclass
class DailyStats:
    """Daily trading statistics."""

    date: date
    total_trades: int = 0
    total_volume: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    positions_opened: int = 0
    positions_closed: int = 0


class StateStore(BaseComponent):
    """SQLite-based persistence for trading state.

    Stores:
    - Trade history
    - Open and closed positions
    - Settlement queue
    - Daily statistics
    - Fill records for slippage analysis
    """

    def __init__(
        self,
        config: ConfigManager,
        event_bus: Optional[EventBus] = None,
    ):
        """Initialize the state store.

        Args:
            config: Configuration manager.
            event_bus: Optional EventBus for event subscription.
        """
        super().__init__()
        self._config = config
        self._event_bus = event_bus
        self._log = log.bind(component="state_store")

        db_path = config.get("database.path", "./data/mercury.db")
        self._db_path = Path(db_path)
        self._db: Optional[aiosqlite.Connection] = None

    async def start(self) -> None:
        """Connect to database and run migrations."""
        self._start_time = time.time()
        self._log.info("starting_state_store", db_path=str(self._db_path))

        # Ensure directory exists
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        # Connect
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row

        # Run schema
        await self._db.executescript(SCHEMA_SQL)
        await self._db.commit()

        # Subscribe to events
        if self._event_bus:
            await self._event_bus.subscribe("order.filled", self._on_order_filled)
            await self._event_bus.subscribe("position.opened", self._on_position_opened)
            await self._event_bus.subscribe("position.closed", self._on_position_closed)

        self._log.info("state_store_started")

    async def stop(self) -> None:
        """Close database connection."""
        if self._db:
            await self._db.close()
            self._db = None

        self._log.info("state_store_stopped")

    async def health_check(self) -> HealthCheckResult:
        """Check database health."""
        if self._db is None:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message="Database not connected",
            )

        try:
            async with self._db.execute("SELECT 1") as cursor:
                await cursor.fetchone()

            return HealthCheckResult(
                status=HealthStatus.HEALTHY,
                message="Database connected",
            )
        except Exception as e:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message=f"Database error: {e}",
            )

    # ============ Trade Operations ============

    async def save_trade(self, trade: Trade) -> None:
        """Save a trade record."""
        if self._db is None:
            raise RuntimeError("Database not connected")

        await self._db.execute(
            """
            INSERT OR REPLACE INTO trades
            (id, market_id, strategy, side, yes_token_id, no_token_id,
             yes_size, no_size, yes_price, no_price, total_cost,
             guaranteed_pnl, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.id,
                trade.market_id,
                trade.strategy,
                trade.side,
                trade.yes_token_id,
                trade.no_token_id,
                float(trade.yes_size),
                float(trade.no_size),
                float(trade.yes_price),
                float(trade.no_price),
                float(trade.total_cost),
                float(trade.guaranteed_pnl),
                trade.status,
                trade.created_at or datetime.now(timezone.utc),
                datetime.now(timezone.utc),
            )
        )
        await self._db.commit()

    async def get_trade(self, trade_id: str) -> Optional[Trade]:
        """Get a trade by ID."""
        if self._db is None:
            return None

        async with self._db.execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return None

        return self._row_to_trade(row)

    async def get_trades(
        self,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[Trade]:
        """Get recent trades."""
        if self._db is None:
            return []

        if since:
            query = "SELECT * FROM trades WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?"
            params = (since, limit)
        else:
            query = "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?"
            params = (limit,)

        trades = []
        async with self._db.execute(query, params) as cursor:
            async for row in cursor:
                trades.append(self._row_to_trade(row))

        return trades

    def _row_to_trade(self, row) -> Trade:
        """Convert a database row to Trade."""
        return Trade(
            id=row["id"],
            market_id=row["market_id"],
            strategy=row["strategy"],
            side=row["side"],
            yes_token_id=row["yes_token_id"],
            no_token_id=row["no_token_id"],
            yes_size=Decimal(str(row["yes_size"])),
            no_size=Decimal(str(row["no_size"])),
            yes_price=Decimal(str(row["yes_price"])),
            no_price=Decimal(str(row["no_price"])),
            total_cost=Decimal(str(row["total_cost"])),
            guaranteed_pnl=Decimal(str(row["guaranteed_pnl"])),
            status=row["status"],
            created_at=row["created_at"],
        )

    # ============ Position Operations ============

    async def save_position(self, position: Position) -> None:
        """Save a position."""
        if self._db is None:
            raise RuntimeError("Database not connected")

        await self._db.execute(
            """
            INSERT OR REPLACE INTO positions
            (id, market_id, trade_id, yes_shares, no_shares, cost_basis,
             status, opened_at, closed_at, settlement_proceeds, realized_pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                position.position_id,
                position.market_id,
                position.trade_id,
                float(position.yes_shares),
                float(position.no_shares),
                float(position.cost_basis),
                position.status.value,
                position.opened_at,
                position.closed_at,
                float(position.settlement_proceeds) if position.settlement_proceeds else None,
                float(position.realized_pnl) if position.realized_pnl else None,
            )
        )
        await self._db.commit()

    async def get_open_positions(self) -> list[Position]:
        """Get all open positions."""
        if self._db is None:
            return []

        positions = []
        async with self._db.execute(
            "SELECT * FROM positions WHERE status = 'OPEN'"
        ) as cursor:
            async for row in cursor:
                positions.append(self._row_to_position(row))

        return positions

    async def get_position(self, position_id: str) -> Optional[Position]:
        """Get a position by ID."""
        if self._db is None:
            return None

        async with self._db.execute(
            "SELECT * FROM positions WHERE id = ?", (position_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return None

        return self._row_to_position(row)

    def _row_to_position(self, row) -> Position:
        """Convert a database row to Position."""
        return Position(
            position_id=row["id"],
            market_id=row["market_id"],
            trade_id=row["trade_id"],
            yes_shares=Decimal(str(row["yes_shares"])),
            no_shares=Decimal(str(row["no_shares"])),
            cost_basis=Decimal(str(row["cost_basis"])),
            status=PositionStatus(row["status"]),
            opened_at=row["opened_at"],
            closed_at=row["closed_at"],
            settlement_proceeds=Decimal(str(row["settlement_proceeds"])) if row["settlement_proceeds"] else None,
            realized_pnl=Decimal(str(row["realized_pnl"])) if row["realized_pnl"] else None,
        )

    # ============ Settlement Queue ============

    async def queue_for_settlement(self, position_id: str, market_id: str, condition_id: str) -> None:
        """Add a position to the settlement queue."""
        if self._db is None:
            raise RuntimeError("Database not connected")

        await self._db.execute(
            """
            INSERT INTO settlement_queue (position_id, market_id, condition_id)
            VALUES (?, ?, ?)
            """,
            (position_id, market_id, condition_id)
        )
        await self._db.commit()

    async def get_claimable_positions(self, max_attempts: int = 5) -> list[dict]:
        """Get positions pending settlement."""
        if self._db is None:
            return []

        items = []
        async with self._db.execute(
            """
            SELECT * FROM settlement_queue
            WHERE status = 'PENDING' AND attempts < ?
            ORDER BY queued_at
            """,
            (max_attempts,)
        ) as cursor:
            async for row in cursor:
                items.append(dict(row))

        return items

    async def mark_settlement_attempt(self, queue_id: int, success: bool, error: Optional[str] = None) -> None:
        """Record a settlement attempt."""
        if self._db is None:
            return

        if success:
            await self._db.execute(
                "UPDATE settlement_queue SET status = 'CLAIMED', last_attempt = ? WHERE id = ?",
                (datetime.now(timezone.utc), queue_id)
            )
        else:
            await self._db.execute(
                "UPDATE settlement_queue SET attempts = attempts + 1, last_attempt = ?, error = ? WHERE id = ?",
                (datetime.now(timezone.utc), error, queue_id)
            )
        await self._db.commit()

    # ============ Daily Stats ============

    async def get_daily_stats(self, for_date: Optional[date] = None) -> DailyStats:
        """Get statistics for a date."""
        if self._db is None:
            return DailyStats(date=for_date or date.today())

        target_date = for_date or date.today()

        async with self._db.execute(
            "SELECT * FROM daily_stats WHERE date = ?",
            (target_date.isoformat(),)
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return DailyStats(date=target_date)

        return DailyStats(
            date=target_date,
            total_trades=row["total_trades"],
            total_volume=Decimal(str(row["total_volume"])),
            realized_pnl=Decimal(str(row["realized_pnl"])),
            unrealized_pnl=Decimal(str(row["unrealized_pnl"])),
            positions_opened=row["positions_opened"],
            positions_closed=row["positions_closed"],
        )

    async def update_daily_stats(
        self,
        for_date: Optional[date] = None,
        trades: int = 0,
        volume: Decimal = Decimal("0"),
        realized_pnl: Decimal = Decimal("0"),
        positions_opened: int = 0,
        positions_closed: int = 0,
    ) -> None:
        """Update daily statistics."""
        if self._db is None:
            return

        target_date = (for_date or date.today()).isoformat()

        await self._db.execute(
            """
            INSERT INTO daily_stats (date, total_trades, total_volume, realized_pnl,
                                     positions_opened, positions_closed)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                total_trades = total_trades + excluded.total_trades,
                total_volume = total_volume + excluded.total_volume,
                realized_pnl = realized_pnl + excluded.realized_pnl,
                positions_opened = positions_opened + excluded.positions_opened,
                positions_closed = positions_closed + excluded.positions_closed
            """,
            (target_date, trades, float(volume), float(realized_pnl),
             positions_opened, positions_closed)
        )
        await self._db.commit()

    # ============ Event Handlers ============

    async def _on_order_filled(self, data: dict) -> None:
        """Handle order filled event."""
        # Will be implemented to record fill details
        pass

    async def _on_position_opened(self, data: dict) -> None:
        """Handle position opened event."""
        await self.update_daily_stats(positions_opened=1)

    async def _on_position_closed(self, data: dict) -> None:
        """Handle position closed event."""
        realized = Decimal(str(data.get("realized_pnl", 0)))
        await self.update_daily_stats(positions_closed=1, realized_pnl=realized)
