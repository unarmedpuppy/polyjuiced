"""SQLite persistence for historical trades, markets, and logs."""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite
import structlog

log = structlog.get_logger()

# Default database path (can be overridden via config)
DEFAULT_DB_PATH = Path("/app/data/gabagool.db")


class Database:
    """Async SQLite database for persisting bot history."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        """Initialize database.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Connect to database and create tables."""
        # Ensure directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row

        await self._create_tables()
        log.info("Database connected", path=str(self.db_path))

    async def close(self) -> None:
        """Close database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _create_tables(self) -> None:
        """Create database tables if they don't exist."""
        await self._conn.executescript("""
            -- Trades table
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
                dry_run BOOLEAN DEFAULT 0
            );

            -- Markets table (track discovered markets)
            CREATE TABLE IF NOT EXISTS markets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                condition_id TEXT UNIQUE NOT NULL,
                question TEXT,
                asset TEXT NOT NULL,
                start_time TIMESTAMP,
                end_time TIMESTAMP,
                yes_token_id TEXT,
                no_token_id TEXT,
                was_traded BOOLEAN DEFAULT 0,
                trade_count INTEGER DEFAULT 0
            );

            -- Logs table (persist important logs)
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                extra TEXT
            );

            -- Daily stats table (track daily performance)
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

            -- Create indexes for common queries
            CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at);
            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            CREATE INDEX IF NOT EXISTS idx_markets_asset ON markets(asset);
            CREATE INDEX IF NOT EXISTS idx_markets_end_time ON markets(end_time);
            CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at);
            CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level);
        """)
        await self._conn.commit()

    # ========== Trade Operations ==========

    async def save_trade(
        self,
        trade_id: str,
        asset: str,
        yes_price: float,
        no_price: float,
        yes_cost: float,
        no_cost: float,
        spread: float,
        expected_profit: float,
        market_end_time: str = None,
        market_slug: str = None,
        condition_id: str = None,
        dry_run: bool = False,
    ) -> None:
        """Save a new trade to database."""
        async with self._lock:
            await self._conn.execute(
                """
                INSERT INTO trades (
                    id, asset, yes_price, no_price, yes_cost, no_cost,
                    spread, expected_profit, market_end_time, market_slug,
                    condition_id, dry_run
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id, asset, yes_price, no_price, yes_cost, no_cost,
                    spread, expected_profit, market_end_time, market_slug,
                    condition_id, dry_run,
                ),
            )
            await self._conn.commit()

    async def resolve_trade(
        self,
        trade_id: str,
        won: bool,
        actual_profit: float,
    ) -> None:
        """Update trade with resolution result."""
        status = "win" if won else "loss"
        async with self._lock:
            await self._conn.execute(
                """
                UPDATE trades
                SET status = ?, actual_profit = ?, resolved_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, actual_profit, trade_id),
            )
            await self._conn.commit()

    async def get_recent_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent trades."""
        async with self._conn.execute(
            """
            SELECT * FROM trades
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_trades_by_date(
        self,
        start_date: datetime,
        end_date: datetime = None,
    ) -> List[Dict[str, Any]]:
        """Get trades within a date range."""
        if end_date is None:
            end_date = datetime.utcnow()

        async with self._conn.execute(
            """
            SELECT * FROM trades
            WHERE created_at >= ? AND created_at <= ?
            ORDER BY created_at DESC
            """,
            (start_date.isoformat(), end_date.isoformat()),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_pending_trades(self) -> List[Dict[str, Any]]:
        """Get all pending trades."""
        async with self._conn.execute(
            """
            SELECT * FROM trades
            WHERE status = 'pending'
            ORDER BY created_at DESC
            """,
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # ========== Market Operations ==========

    async def save_market(
        self,
        condition_id: str,
        question: str,
        asset: str,
        start_time: datetime,
        end_time: datetime,
        yes_token_id: str,
        no_token_id: str,
    ) -> None:
        """Save or update a discovered market."""
        async with self._lock:
            await self._conn.execute(
                """
                INSERT INTO markets (
                    condition_id, question, asset, start_time, end_time,
                    yes_token_id, no_token_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(condition_id) DO UPDATE SET
                    question = excluded.question,
                    start_time = excluded.start_time,
                    end_time = excluded.end_time
                """,
                (
                    condition_id, question, asset,
                    start_time.isoformat() if start_time else None,
                    end_time.isoformat() if end_time else None,
                    yes_token_id, no_token_id,
                ),
            )
            await self._conn.commit()

    async def mark_market_traded(self, condition_id: str) -> None:
        """Mark a market as having been traded."""
        async with self._lock:
            await self._conn.execute(
                """
                UPDATE markets
                SET was_traded = 1, trade_count = trade_count + 1
                WHERE condition_id = ?
                """,
                (condition_id,),
            )
            await self._conn.commit()

    async def get_recent_markets(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recently discovered markets."""
        async with self._conn.execute(
            """
            SELECT * FROM markets
            ORDER BY discovered_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_markets_by_asset(
        self,
        asset: str,
        include_expired: bool = False,
    ) -> List[Dict[str, Any]]:
        """Get markets for a specific asset."""
        if include_expired:
            query = """
                SELECT * FROM markets
                WHERE asset = ?
                ORDER BY end_time DESC
            """
            params = (asset,)
        else:
            query = """
                SELECT * FROM markets
                WHERE asset = ? AND end_time > ?
                ORDER BY end_time ASC
            """
            params = (asset, datetime.utcnow().isoformat())

        async with self._conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # ========== Log Operations ==========

    async def save_log(
        self,
        level: str,
        message: str,
        extra: Dict[str, Any] = None,
    ) -> None:
        """Save a log entry."""
        import json
        extra_str = json.dumps(extra) if extra else None

        async with self._lock:
            await self._conn.execute(
                """
                INSERT INTO logs (level, message, extra)
                VALUES (?, ?, ?)
                """,
                (level, message, extra_str),
            )
            await self._conn.commit()

    async def get_recent_logs(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recent log entries."""
        import json

        async with self._conn.execute(
            """
            SELECT * FROM logs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            logs = []
            for row in rows:
                log_entry = dict(row)
                if log_entry.get("extra"):
                    log_entry["extra"] = json.loads(log_entry["extra"])
                logs.append(log_entry)
            return logs

    async def cleanup_old_logs(self, days: int = 7) -> int:
        """Delete logs older than specified days."""
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        async with self._lock:
            cursor = await self._conn.execute(
                """
                DELETE FROM logs WHERE created_at < ?
                """,
                (cutoff,),
            )
            deleted = cursor.rowcount
            await self._conn.commit()
            return deleted

    # ========== Daily Stats Operations ==========

    async def update_daily_stats(
        self,
        date: str = None,
        pnl_delta: float = 0,
        trades_delta: int = 0,
        wins_delta: int = 0,
        losses_delta: int = 0,
        exposure_delta: float = 0,
        opportunities_detected_delta: int = 0,
        opportunities_executed_delta: int = 0,
    ) -> None:
        """Update daily stats (creates if doesn't exist)."""
        if date is None:
            date = datetime.utcnow().strftime("%Y-%m-%d")

        async with self._lock:
            # Upsert daily stats
            await self._conn.execute(
                """
                INSERT INTO daily_stats (date, pnl, trades, wins, losses, exposure,
                                         opportunities_detected, opportunities_executed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    pnl = pnl + excluded.pnl,
                    trades = trades + excluded.trades,
                    wins = wins + excluded.wins,
                    losses = losses + excluded.losses,
                    exposure = exposure + excluded.exposure,
                    opportunities_detected = opportunities_detected + excluded.opportunities_detected,
                    opportunities_executed = opportunities_executed + excluded.opportunities_executed
                """,
                (
                    date, pnl_delta, trades_delta, wins_delta, losses_delta,
                    exposure_delta, opportunities_detected_delta, opportunities_executed_delta,
                ),
            )
            await self._conn.commit()

    async def get_daily_stats(self, date: str = None) -> Optional[Dict[str, Any]]:
        """Get stats for a specific date."""
        if date is None:
            date = datetime.utcnow().strftime("%Y-%m-%d")

        async with self._conn.execute(
            """
            SELECT * FROM daily_stats WHERE date = ?
            """,
            (date,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_stats_range(
        self,
        start_date: str,
        end_date: str = None,
    ) -> List[Dict[str, Any]]:
        """Get stats for a date range."""
        if end_date is None:
            end_date = datetime.utcnow().strftime("%Y-%m-%d")

        async with self._conn.execute(
            """
            SELECT * FROM daily_stats
            WHERE date >= ? AND date <= ?
            ORDER BY date DESC
            """,
            (start_date, end_date),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # ========== Summary Statistics ==========

    async def get_all_time_stats(self) -> Dict[str, Any]:
        """Get all-time trading statistics."""
        async with self._conn.execute(
            """
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN status = 'win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN status = 'loss' THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN actual_profit IS NOT NULL THEN actual_profit ELSE 0 END) as total_pnl,
                AVG(CASE WHEN actual_profit IS NOT NULL THEN actual_profit END) as avg_profit,
                MAX(actual_profit) as best_trade,
                MIN(actual_profit) as worst_trade,
                SUM(yes_cost + no_cost) as total_volume
            FROM trades
            """,
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else {}

    async def get_today_stats(self) -> Dict[str, Any]:
        """Get today's trading statistics."""
        today = datetime.utcnow().strftime("%Y-%m-%d")

        async with self._conn.execute(
            """
            SELECT
                COUNT(*) as trades,
                SUM(CASE WHEN status = 'win' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN status = 'loss' THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN actual_profit IS NOT NULL THEN actual_profit ELSE 0 END) as pnl,
                SUM(yes_cost + no_cost) as exposure
            FROM trades
            WHERE date(created_at) = ?
            """,
            (today,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else {}


# Global database instance
_db: Optional[Database] = None


async def get_database(db_path: Path = DEFAULT_DB_PATH) -> Database:
    """Get or create the global database instance."""
    global _db
    if _db is None:
        _db = Database(db_path)
        await _db.connect()
    return _db


async def close_database() -> None:
    """Close the global database connection."""
    global _db
    if _db:
        await _db.close()
        _db = None
