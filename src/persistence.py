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

            -- =========================================================================
            -- Liquidity Data Tables (for building persistence/slippage models)
            -- See docs/LIQUIDITY_SIZING.md for rationale
            -- =========================================================================

            -- Fill records: captures every order execution with slippage data
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

            -- Liquidity snapshots: periodic order book depth captures
            CREATE TABLE IF NOT EXISTS liquidity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                token_id TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                asset TEXT NOT NULL,
                bid_levels TEXT,  -- JSON array of [price, size] tuples
                ask_levels TEXT,  -- JSON array of [price, size] tuples
                total_bid_depth REAL NOT NULL,
                total_ask_depth REAL NOT NULL
            );

            -- Indexes for liquidity queries
            CREATE INDEX IF NOT EXISTS idx_fills_timestamp ON fill_records(timestamp);
            CREATE INDEX IF NOT EXISTS idx_fills_token ON fill_records(token_id);
            CREATE INDEX IF NOT EXISTS idx_fills_asset ON fill_records(asset);
            CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON liquidity_snapshots(timestamp);
            CREATE INDEX IF NOT EXISTS idx_snapshots_token ON liquidity_snapshots(token_id);
            CREATE INDEX IF NOT EXISTS idx_snapshots_asset ON liquidity_snapshots(asset);
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

    # ========== Liquidity Data Operations ==========

    async def save_fill_record(
        self,
        token_id: str,
        condition_id: str,
        asset: str,
        side: str,
        intended_size: float,
        filled_size: float,
        intended_price: float,
        actual_avg_price: float,
        time_to_fill_ms: int,
        slippage: float,
        pre_fill_depth: float,
        post_fill_depth: float = None,
        order_type: str = "GTC",
        order_id: str = None,
        fill_ratio: float = None,
        persistence_ratio: float = None,
    ) -> None:
        """Save a fill record for slippage analysis.

        Args:
            token_id: Token that was traded
            condition_id: Market condition ID
            asset: Asset symbol (BTC, ETH, SOL)
            side: "BUY" or "SELL"
            intended_size: Shares we intended to trade
            filled_size: Shares actually filled
            intended_price: Price we tried to get
            actual_avg_price: Average fill price achieved
            time_to_fill_ms: Milliseconds from order to fill
            slippage: Price slippage (positive = worse)
            pre_fill_depth: Depth before order
            post_fill_depth: Depth after order (optional)
            order_type: Order type (GTC, FOK, etc.)
            order_id: Exchange order ID
            fill_ratio: filled_size / intended_size
            persistence_ratio: filled_size / pre_fill_depth
        """
        # Calculate ratios if not provided
        if fill_ratio is None and intended_size > 0:
            fill_ratio = filled_size / intended_size
        if persistence_ratio is None and pre_fill_depth > 0:
            persistence_ratio = filled_size / pre_fill_depth

        async with self._lock:
            await self._conn.execute(
                """
                INSERT INTO fill_records (
                    token_id, condition_id, asset, side,
                    intended_size, filled_size, intended_price, actual_avg_price,
                    time_to_fill_ms, slippage, pre_fill_depth, post_fill_depth,
                    order_type, order_id, fill_ratio, persistence_ratio
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_id, condition_id, asset, side,
                    intended_size, filled_size, intended_price, actual_avg_price,
                    time_to_fill_ms, slippage, pre_fill_depth, post_fill_depth,
                    order_type, order_id, fill_ratio, persistence_ratio,
                ),
            )
            await self._conn.commit()

    async def save_liquidity_snapshot(
        self,
        token_id: str,
        condition_id: str,
        asset: str,
        bid_levels: list,
        ask_levels: list,
        total_bid_depth: float,
        total_ask_depth: float,
    ) -> None:
        """Save an order book depth snapshot.

        Args:
            token_id: Token ID
            condition_id: Market condition ID
            asset: Asset symbol
            bid_levels: List of [price, size] tuples
            ask_levels: List of [price, size] tuples
            total_bid_depth: Sum of all bid sizes
            total_ask_depth: Sum of all ask sizes
        """
        import json

        async with self._lock:
            await self._conn.execute(
                """
                INSERT INTO liquidity_snapshots (
                    token_id, condition_id, asset,
                    bid_levels, ask_levels,
                    total_bid_depth, total_ask_depth
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_id, condition_id, asset,
                    json.dumps(bid_levels), json.dumps(ask_levels),
                    total_bid_depth, total_ask_depth,
                ),
            )
            await self._conn.commit()

    async def get_recent_fills(
        self,
        token_id: str = None,
        asset: str = None,
        limit: int = 100,
        lookback_minutes: int = None,
    ) -> List[Dict[str, Any]]:
        """Get recent fill records for analysis.

        Args:
            token_id: Filter by token (optional)
            asset: Filter by asset (optional)
            limit: Maximum records to return
            lookback_minutes: Only get fills from last N minutes (optional)

        Returns:
            List of fill record dicts
        """
        query = "SELECT * FROM fill_records WHERE 1=1"
        params = []

        if token_id:
            query += " AND token_id = ?"
            params.append(token_id)

        if asset:
            query += " AND asset = ?"
            params.append(asset)

        if lookback_minutes:
            cutoff = (datetime.utcnow() - timedelta(minutes=lookback_minutes)).isoformat()
            query += " AND timestamp >= ?"
            params.append(cutoff)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        async with self._conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_recent_snapshots(
        self,
        token_id: str = None,
        asset: str = None,
        limit: int = 100,
        lookback_minutes: int = None,
    ) -> List[Dict[str, Any]]:
        """Get recent liquidity snapshots.

        Args:
            token_id: Filter by token (optional)
            asset: Filter by asset (optional)
            limit: Maximum records to return
            lookback_minutes: Only get snapshots from last N minutes (optional)

        Returns:
            List of snapshot dicts
        """
        import json

        query = "SELECT * FROM liquidity_snapshots WHERE 1=1"
        params = []

        if token_id:
            query += " AND token_id = ?"
            params.append(token_id)

        if asset:
            query += " AND asset = ?"
            params.append(asset)

        if lookback_minutes:
            cutoff = (datetime.utcnow() - timedelta(minutes=lookback_minutes)).isoformat()
            query += " AND timestamp >= ?"
            params.append(cutoff)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        async with self._conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            snapshots = []
            for row in rows:
                snapshot = dict(row)
                # Parse JSON arrays
                if snapshot.get("bid_levels"):
                    snapshot["bid_levels"] = json.loads(snapshot["bid_levels"])
                if snapshot.get("ask_levels"):
                    snapshot["ask_levels"] = json.loads(snapshot["ask_levels"])
                snapshots.append(snapshot)
            return snapshots

    async def get_slippage_stats(
        self,
        token_id: str = None,
        asset: str = None,
        lookback_minutes: int = 60,
    ) -> Dict[str, Any]:
        """Get aggregated slippage statistics.

        Args:
            token_id: Filter by token (optional)
            asset: Filter by asset (optional)
            lookback_minutes: Analysis window

        Returns:
            Dict with slippage statistics
        """
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
        cutoff = (datetime.utcnow() - timedelta(minutes=lookback_minutes)).isoformat()
        params = [cutoff]

        if token_id:
            query += " AND token_id = ?"
            params.append(token_id)

        if asset:
            query += " AND asset = ?"
            params.append(asset)

        async with self._conn.execute(query, params) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else {}

    async def get_depth_stats(
        self,
        token_id: str = None,
        asset: str = None,
        lookback_minutes: int = 60,
    ) -> Dict[str, Any]:
        """Get aggregated depth statistics.

        Args:
            token_id: Filter by token (optional)
            asset: Filter by asset (optional)
            lookback_minutes: Analysis window

        Returns:
            Dict with depth statistics
        """
        query = """
            SELECT
                COUNT(*) as snapshot_count,
                AVG(total_ask_depth) as avg_ask_depth,
                AVG(total_bid_depth) as avg_bid_depth,
                MAX(total_ask_depth) as max_ask_depth,
                MIN(total_ask_depth) as min_ask_depth
            FROM liquidity_snapshots
            WHERE timestamp >= ?
        """
        cutoff = (datetime.utcnow() - timedelta(minutes=lookback_minutes)).isoformat()
        params = [cutoff]

        if token_id:
            query += " AND token_id = ?"
            params.append(token_id)

        if asset:
            query += " AND asset = ?"
            params.append(asset)

        async with self._conn.execute(query, params) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else {}

    async def cleanup_old_liquidity_data(self, days: int = 30) -> Dict[str, int]:
        """Delete liquidity data older than specified days.

        Args:
            days: Delete data older than this many days

        Returns:
            Dict with counts of deleted records
        """
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        deleted = {"fills": 0, "snapshots": 0}

        async with self._lock:
            cursor = await self._conn.execute(
                "DELETE FROM fill_records WHERE timestamp < ?",
                (cutoff,),
            )
            deleted["fills"] = cursor.rowcount

            cursor = await self._conn.execute(
                "DELETE FROM liquidity_snapshots WHERE timestamp < ?",
                (cutoff,),
            )
            deleted["snapshots"] = cursor.rowcount

            await self._conn.commit()

        return deleted

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

    async def get_pnl_history(self, timeframe: str = "all") -> List[Dict[str, Any]]:
        """Get P&L history for charting.

        Args:
            timeframe: "24h", "7d", or "all"

        Returns:
            List of {timestamp, cumulative_pnl} points in chronological order
        """
        # Build time filter
        if timeframe == "24h":
            cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
        elif timeframe == "7d":
            cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
        else:
            cutoff = None

        # Query resolved trades with actual profit
        if cutoff:
            query = """
                SELECT resolved_at, actual_profit
                FROM trades
                WHERE status IN ('win', 'loss')
                  AND resolved_at IS NOT NULL
                  AND resolved_at >= ?
                ORDER BY resolved_at ASC
            """
            params = (cutoff,)
        else:
            query = """
                SELECT resolved_at, actual_profit
                FROM trades
                WHERE status IN ('win', 'loss')
                  AND resolved_at IS NOT NULL
                ORDER BY resolved_at ASC
            """
            params = ()

        async with self._conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()

        # Calculate cumulative P&L
        points = []
        cumulative = 0.0
        for row in rows:
            cumulative += row["actual_profit"] or 0
            points.append({
                "timestamp": row["resolved_at"],
                "cumulative_pnl": round(cumulative, 2),
            })

        return points


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
