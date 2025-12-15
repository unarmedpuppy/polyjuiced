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
        await self._migrate_schema()
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
            -- IMPORTANT: Strategy owns persistence - dashboard is read-only
            -- See docs/STRATEGY_ARCHITECTURE.md for data flow
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
                -- Phase 2 fields: actual execution data (added 2025-12-14)
                yes_shares REAL,              -- Actual YES shares filled
                no_shares REAL,               -- Actual NO shares filled
                hedge_ratio REAL,             -- min(yes,no)/max(yes,no) - 1.0 = perfect hedge
                execution_status TEXT,        -- 'full_fill', 'partial_fill', 'one_leg_only', 'failed'
                yes_order_status TEXT,        -- 'MATCHED', 'LIVE', 'FAILED'
                no_order_status TEXT,         -- 'MATCHED', 'LIVE', 'FAILED'
                -- Liquidity context at execution time (Phase 7)
                yes_liquidity_at_price REAL,  -- Shares available at our limit price (YES)
                no_liquidity_at_price REAL,   -- Shares available at our limit price (NO)
                yes_book_depth_total REAL,    -- Total YES order book depth
                no_book_depth_total REAL      -- Total NO order book depth
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

            -- =========================================================================
            -- Trade Telemetry Tables (for active position management)
            -- See docs/REBALANCING_STRATEGY.md for rationale
            -- =========================================================================

            -- Detailed timing telemetry for each trade
            CREATE TABLE IF NOT EXISTS trade_telemetry (
                trade_id TEXT PRIMARY KEY,

                -- Opportunity timing
                opportunity_detected_at TIMESTAMP,
                opportunity_spread REAL,
                opportunity_yes_price REAL,
                opportunity_no_price REAL,

                -- Execution timing
                order_placed_at TIMESTAMP,
                order_filled_at TIMESTAMP,
                execution_latency_ms REAL,
                fill_latency_ms REAL,

                -- Initial position state
                initial_yes_shares REAL,
                initial_no_shares REAL,
                initial_hedge_ratio REAL,

                -- Rebalancing tracking
                rebalance_started_at TIMESTAMP,
                rebalance_attempts INTEGER DEFAULT 0,
                position_balanced_at TIMESTAMP,

                -- Resolution
                resolved_at TIMESTAMP,
                final_yes_shares REAL,
                final_no_shares REAL,
                final_hedge_ratio REAL,
                actual_profit REAL,

                -- Foreign key to trades table
                FOREIGN KEY (trade_id) REFERENCES trades(id)
            );

            -- Individual rebalancing trades
            CREATE TABLE IF NOT EXISTS rebalance_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT NOT NULL,
                attempted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                action TEXT NOT NULL,              -- SELL_YES, BUY_NO, SELL_NO, BUY_YES
                shares REAL NOT NULL,
                price REAL NOT NULL,
                status TEXT NOT NULL,              -- SUCCESS, FAILED, PARTIAL
                filled_shares REAL DEFAULT 0,
                profit REAL DEFAULT 0,
                error TEXT,
                order_id TEXT,

                FOREIGN KEY (trade_id) REFERENCES trades(id)
            );

            -- Indexes for telemetry queries
            CREATE INDEX IF NOT EXISTS idx_telemetry_detected ON trade_telemetry(opportunity_detected_at);
            CREATE INDEX IF NOT EXISTS idx_telemetry_resolved ON trade_telemetry(resolved_at);
            CREATE INDEX IF NOT EXISTS idx_rebalance_trade_id ON rebalance_trades(trade_id);
            CREATE INDEX IF NOT EXISTS idx_rebalance_status ON rebalance_trades(status);

            -- =========================================================================
            -- Position Settlement Queue (survives restarts)
            -- Tracks positions that need to be claimed/sold after market resolution
            -- =========================================================================

            CREATE TABLE IF NOT EXISTS settlement_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                trade_id TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                side TEXT NOT NULL,            -- YES or NO
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

            CREATE INDEX IF NOT EXISTS idx_settlement_unclaimed ON settlement_queue(claimed, market_end_time);
            CREATE INDEX IF NOT EXISTS idx_settlement_condition ON settlement_queue(condition_id);

            -- =========================================================================
            -- Circuit Breaker State (persists across restarts)
            -- Tracks realized PnL and circuit breaker status
            -- =========================================================================

            CREATE TABLE IF NOT EXISTS circuit_breaker_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),  -- Singleton row
                date TEXT NOT NULL,                      -- Current trading day (YYYY-MM-DD)
                realized_pnl REAL DEFAULT 0.0,           -- Actual P&L from resolved trades today
                circuit_breaker_hit BOOLEAN DEFAULT 0,   -- Whether loss limit was triggered
                hit_at TIMESTAMP,                        -- When circuit breaker was triggered
                hit_reason TEXT,                         -- Reason for trigger
                total_trades_today INTEGER DEFAULT 0,    -- Trade count for the day
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Initialize singleton row if not exists
            INSERT OR IGNORE INTO circuit_breaker_state (id, date)
            VALUES (1, date('now'));

            -- Realized PnL ledger: individual entries for audit trail
            CREATE TABLE IF NOT EXISTS realized_pnl_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                trade_id TEXT NOT NULL,
                trade_date TEXT NOT NULL,               -- YYYY-MM-DD for daily aggregation
                pnl_amount REAL NOT NULL,               -- Actual profit/loss
                pnl_type TEXT NOT NULL,                 -- 'resolution', 'settlement', 'rebalance'
                notes TEXT,
                UNIQUE(trade_id, pnl_type)              -- Prevent duplicate entries
            );

            CREATE INDEX IF NOT EXISTS idx_pnl_ledger_date ON realized_pnl_ledger(trade_date);
            CREATE INDEX IF NOT EXISTS idx_pnl_ledger_trade ON realized_pnl_ledger(trade_id);
        """)
        await self._conn.commit()

    async def _migrate_schema(self) -> None:
        """Apply schema migrations for existing databases.

        This adds new columns to existing tables without losing data.
        Safe to run multiple times (idempotent).
        """
        # Phase 2 migration: Add execution tracking columns to trades table
        # Multi-strategy migration: Add strategy_id column to trades table
        migrations = [
            ("trades", "yes_shares", "REAL"),
            ("trades", "no_shares", "REAL"),
            ("trades", "hedge_ratio", "REAL"),
            ("trades", "execution_status", "TEXT"),
            ("trades", "yes_order_status", "TEXT"),
            ("trades", "no_order_status", "TEXT"),
            ("trades", "yes_liquidity_at_price", "REAL"),
            ("trades", "no_liquidity_at_price", "REAL"),
            ("trades", "yes_book_depth_total", "REAL"),
            ("trades", "no_book_depth_total", "REAL"),
            # Multi-strategy support (Dec 2025)
            ("trades", "strategy_id", "TEXT DEFAULT 'gabagool'"),
        ]

        for table, column, col_type in migrations:
            try:
                # Check if column exists
                cursor = await self._conn.execute(f"PRAGMA table_info({table})")
                columns = await cursor.fetchall()
                column_names = [col[1] for col in columns]

                if column not in column_names:
                    await self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
                    )
                    log.info(f"Migration: Added column {column} to {table}")
            except Exception as e:
                log.debug(f"Migration skipped for {table}.{column}: {e}")

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

    async def save_arbitrage_trade(
        self,
        trade_id: str,
        asset: str,
        condition_id: str,
        yes_price: float,
        no_price: float,
        yes_cost: float,
        no_cost: float,
        spread: float,
        expected_profit: float,
        yes_shares: float,
        no_shares: float,
        hedge_ratio: float,
        execution_status: str,
        yes_order_status: str,
        no_order_status: str,
        market_end_time: str = None,
        market_slug: str = None,
        dry_run: bool = False,
        yes_liquidity_at_price: float = None,
        no_liquidity_at_price: float = None,
        yes_book_depth_total: float = None,
        no_book_depth_total: float = None,
    ) -> None:
        """Save an arbitrage trade with full execution details.

        This is the primary method for strategy to persist trades.
        Dashboard should NOT call this - it's read-only.

        Args:
            trade_id: Unique trade identifier
            asset: Asset symbol (BTC, ETH, SOL)
            condition_id: Market condition ID
            yes_price: Limit price used for YES leg
            no_price: Limit price used for NO leg
            yes_cost: USD spent on YES
            no_cost: USD spent on NO
            spread: Spread in cents at execution
            expected_profit: Expected profit based on spread
            yes_shares: Actual YES shares filled
            no_shares: Actual NO shares filled
            hedge_ratio: min(yes,no)/max(yes,no) - 1.0 = perfect hedge
            execution_status: 'full_fill', 'partial_fill', 'one_leg_only', 'failed'
            yes_order_status: 'MATCHED', 'LIVE', 'FAILED'
            no_order_status: 'MATCHED', 'LIVE', 'FAILED'
            market_end_time: Market resolution time
            market_slug: Market slug for reference
            dry_run: Whether this is a dry run trade
            yes_liquidity_at_price: Shares available at our YES limit (optional)
            no_liquidity_at_price: Shares available at our NO limit (optional)
            yes_book_depth_total: Total YES book depth (optional)
            no_book_depth_total: Total NO book depth (optional)
        """
        # Determine status based on execution
        if execution_status == 'failed':
            status = 'failed'
        elif execution_status == 'full_fill':
            status = 'pending'  # Awaiting market resolution
        else:
            # partial_fill or one_leg_only - still pending but flagged
            status = 'pending'

        async with self._lock:
            await self._conn.execute(
                """
                INSERT INTO trades (
                    id, asset, condition_id, yes_price, no_price, yes_cost, no_cost,
                    spread, expected_profit, market_end_time, market_slug, dry_run,
                    yes_shares, no_shares, hedge_ratio, execution_status,
                    yes_order_status, no_order_status, status,
                    yes_liquidity_at_price, no_liquidity_at_price,
                    yes_book_depth_total, no_book_depth_total
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id, asset, condition_id, yes_price, no_price, yes_cost, no_cost,
                    spread, expected_profit, market_end_time, market_slug, dry_run,
                    yes_shares, no_shares, hedge_ratio, execution_status,
                    yes_order_status, no_order_status, status,
                    yes_liquidity_at_price, no_liquidity_at_price,
                    yes_book_depth_total, no_book_depth_total,
                ),
            )
            await self._conn.commit()

        log.debug(
            "Arbitrage trade saved",
            trade_id=trade_id,
            asset=asset,
            execution_status=execution_status,
            hedge_ratio=f"{hedge_ratio:.2%}" if hedge_ratio else "N/A",
        )

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

    async def get_recent_trades(
        self,
        limit: int = 50,
        exclude_dry_runs: bool = False,
    ) -> List[Dict[str, Any]]:
        """Get recent trades.

        Args:
            limit: Maximum number of trades to return
            exclude_dry_runs: If True, only returns real (non-dry-run) trades
        """
        if exclude_dry_runs:
            query = """
                SELECT * FROM trades
                WHERE dry_run = 0
                ORDER BY created_at DESC
                LIMIT ?
            """
        else:
            query = """
                SELECT * FROM trades
                ORDER BY created_at DESC
                LIMIT ?
            """
        async with self._conn.execute(query, (limit,)) as cursor:
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

    async def record_trade(
        self,
        condition_id: str,
        side: str,
        shares: float,
        price: float,
        cost: float,
        strategy_id: str = "gabagool",
        asset: str = None,
        trade_id: str = None,
    ) -> str:
        """Record a simple one-leg trade (used by Vol Happens and other strategies).

        This is a simpler method than save_arbitrage_trade for single-leg trades.

        Args:
            condition_id: Market condition ID
            side: "YES" or "NO"
            shares: Number of shares
            price: Price per share
            cost: Total cost (shares * price)
            strategy_id: Strategy identifier (e.g., "vol_happens", "gabagool")
            asset: Asset symbol (optional)
            trade_id: Custom trade ID (generated if not provided)

        Returns:
            trade_id: The trade ID used
        """
        import uuid

        if trade_id is None:
            trade_id = str(uuid.uuid4())

        async with self._lock:
            # Use appropriate column based on side
            if side.upper() == "YES":
                await self._conn.execute(
                    """
                    INSERT INTO trades (
                        id, condition_id, asset, yes_price, no_price,
                        yes_cost, no_cost, yes_shares, no_shares,
                        spread, expected_profit, status, strategy_id
                    ) VALUES (?, ?, ?, ?, 0, ?, 0, ?, 0, 0, 0, 'pending', ?)
                    """,
                    (trade_id, condition_id, asset, price, cost, shares, strategy_id),
                )
            else:
                await self._conn.execute(
                    """
                    INSERT INTO trades (
                        id, condition_id, asset, yes_price, no_price,
                        yes_cost, no_cost, yes_shares, no_shares,
                        spread, expected_profit, status, strategy_id
                    ) VALUES (?, ?, ?, 0, ?, 0, ?, 0, ?, 0, 0, 'pending', ?)
                    """,
                    (trade_id, condition_id, asset, price, cost, shares, strategy_id),
                )
            await self._conn.commit()

        log.debug(
            "Recorded trade",
            trade_id=trade_id[:8] + "...",
            strategy=strategy_id,
            side=side,
            shares=shares,
        )
        return trade_id

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
        """Get all-time trading statistics (excludes dry runs)."""
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
            WHERE dry_run = 0
            """,
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else {}

    async def get_today_stats(self) -> Dict[str, Any]:
        """Get today's trading statistics (excludes dry runs)."""
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
            WHERE date(created_at) = ? AND dry_run = 0
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

        # Query resolved trades with actual profit (exclude dry runs)
        if cutoff:
            query = """
                SELECT resolved_at, actual_profit
                FROM trades
                WHERE status IN ('win', 'loss')
                  AND resolved_at IS NOT NULL
                  AND resolved_at >= ?
                  AND dry_run = 0
                ORDER BY resolved_at ASC
            """
            params = (cutoff,)
        else:
            query = """
                SELECT resolved_at, actual_profit
                FROM trades
                WHERE status IN ('win', 'loss')
                  AND resolved_at IS NOT NULL
                  AND dry_run = 0
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

    async def reset_trade_history(self, preserve_liquidity_data: bool = True) -> Dict[str, int]:
        """Reset trade history data.

        This clears trades, daily_stats, and logs (incorrect P&L data).

        By default, PRESERVES fill_records and liquidity_snapshots which are
        valuable for building persistence/slippage prediction models.

        Args:
            preserve_liquidity_data: If True (default), keep fill_records and
                liquidity_snapshots. Set to False to delete everything.

        Returns:
            Dict with counts of deleted records per table
        """
        deleted = {
            "trades": 0,
            "daily_stats": 0,
            "logs": 0,
        }

        async with self._lock:
            # Clear trades (incorrect P&L data)
            cursor = await self._conn.execute("DELETE FROM trades")
            deleted["trades"] = cursor.rowcount

            # Clear daily stats
            cursor = await self._conn.execute("DELETE FROM daily_stats")
            deleted["daily_stats"] = cursor.rowcount

            # Clear logs
            cursor = await self._conn.execute("DELETE FROM logs")
            deleted["logs"] = cursor.rowcount

            # Only clear liquidity data if explicitly requested
            if not preserve_liquidity_data:
                cursor = await self._conn.execute("DELETE FROM fill_records")
                deleted["fill_records"] = cursor.rowcount

                cursor = await self._conn.execute("DELETE FROM liquidity_snapshots")
                deleted["liquidity_snapshots"] = cursor.rowcount

            await self._conn.commit()

        log.info("Reset trade history", deleted=deleted, preserved_liquidity=preserve_liquidity_data)
        return deleted

    async def reset_all_trade_data(self) -> Dict[str, int]:
        """Reset ALL trade data including liquidity modeling data.

        WARNING: This deletes valuable liquidity data used for slippage modeling.
        Use reset_trade_history() instead to preserve that data.

        Returns:
            Dict with counts of deleted records per table
        """
        return await self.reset_trade_history(preserve_liquidity_data=False)

    # ========== Trade Telemetry Operations ==========

    async def save_trade_telemetry(self, telemetry: Dict[str, Any]) -> None:
        """Save trade telemetry data.

        Args:
            telemetry: Dict with telemetry fields from TradeTelemetry.to_dict()
        """
        async with self._lock:
            await self._conn.execute(
                """
                INSERT OR REPLACE INTO trade_telemetry (
                    trade_id,
                    opportunity_detected_at, opportunity_spread,
                    opportunity_yes_price, opportunity_no_price,
                    order_placed_at, order_filled_at,
                    execution_latency_ms, fill_latency_ms,
                    initial_yes_shares, initial_no_shares, initial_hedge_ratio,
                    rebalance_started_at, rebalance_attempts, position_balanced_at,
                    resolved_at, final_yes_shares, final_no_shares,
                    final_hedge_ratio, actual_profit
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    telemetry.get("trade_id"),
                    telemetry.get("opportunity_detected_at"),
                    telemetry.get("opportunity_spread"),
                    telemetry.get("opportunity_yes_price"),
                    telemetry.get("opportunity_no_price"),
                    telemetry.get("order_placed_at"),
                    telemetry.get("order_filled_at"),
                    telemetry.get("execution_latency_ms"),
                    telemetry.get("fill_latency_ms"),
                    telemetry.get("initial_yes_shares"),
                    telemetry.get("initial_no_shares"),
                    telemetry.get("initial_hedge_ratio"),
                    telemetry.get("rebalance_started_at"),
                    telemetry.get("rebalance_attempts"),
                    telemetry.get("position_balanced_at"),
                    telemetry.get("resolved_at"),
                    telemetry.get("final_yes_shares"),
                    telemetry.get("final_no_shares"),
                    telemetry.get("final_hedge_ratio"),
                    telemetry.get("actual_profit"),
                ),
            )
            await self._conn.commit()

    async def save_rebalance_trade(
        self,
        trade_id: str,
        attempted_at: str,
        action: str,
        shares: float,
        price: float,
        status: str,
        filled_shares: float = 0,
        profit: float = 0,
        error: str = None,
        order_id: str = None,
    ) -> int:
        """Save a rebalancing trade record.

        Args:
            trade_id: Parent trade ID
            attempted_at: When the rebalance was attempted
            action: SELL_YES, BUY_NO, SELL_NO, BUY_YES
            shares: Number of shares attempted
            price: Price per share
            status: SUCCESS, FAILED, PARTIAL
            filled_shares: Shares actually filled
            profit: Profit from the trade
            error: Error message if failed
            order_id: Exchange order ID

        Returns:
            ID of the inserted record
        """
        async with self._lock:
            cursor = await self._conn.execute(
                """
                INSERT INTO rebalance_trades (
                    trade_id, attempted_at, action, shares, price,
                    status, filled_shares, profit, error, order_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id, attempted_at, action, shares, price,
                    status, filled_shares, profit, error, order_id,
                ),
            )
            await self._conn.commit()
            return cursor.lastrowid

    async def get_trade_telemetry(self, trade_id: str) -> Optional[Dict[str, Any]]:
        """Get telemetry for a specific trade.

        Args:
            trade_id: Trade ID

        Returns:
            Telemetry dict or None
        """
        async with self._conn.execute(
            "SELECT * FROM trade_telemetry WHERE trade_id = ?",
            (trade_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_rebalance_trades(self, trade_id: str) -> List[Dict[str, Any]]:
        """Get all rebalancing trades for a position.

        Args:
            trade_id: Parent trade ID

        Returns:
            List of rebalance trade records
        """
        async with self._conn.execute(
            """
            SELECT * FROM rebalance_trades
            WHERE trade_id = ?
            ORDER BY attempted_at ASC
            """,
            (trade_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_execution_latency_stats(
        self,
        lookback_hours: int = 24,
    ) -> Dict[str, Any]:
        """Get execution latency statistics.

        Args:
            lookback_hours: How many hours to analyze

        Returns:
            Dict with latency statistics
        """
        cutoff = (datetime.utcnow() - timedelta(hours=lookback_hours)).isoformat()

        async with self._conn.execute(
            """
            SELECT
                COUNT(*) as total_trades,
                AVG(execution_latency_ms) as avg_execution_latency_ms,
                MIN(execution_latency_ms) as min_execution_latency_ms,
                MAX(execution_latency_ms) as max_execution_latency_ms,
                AVG(fill_latency_ms) as avg_fill_latency_ms,
                COUNT(CASE WHEN rebalance_started_at IS NOT NULL THEN 1 END) as trades_needing_rebalance,
                COUNT(CASE WHEN position_balanced_at IS NOT NULL THEN 1 END) as successfully_balanced,
                AVG(rebalance_attempts) as avg_rebalance_attempts
            FROM trade_telemetry
            WHERE opportunity_detected_at >= ?
            """,
            (cutoff,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else {}

    async def get_rebalancing_success_rate(
        self,
        lookback_hours: int = 24,
    ) -> Dict[str, Any]:
        """Get rebalancing success rate statistics.

        Args:
            lookback_hours: How many hours to analyze

        Returns:
            Dict with success rate statistics
        """
        cutoff = (datetime.utcnow() - timedelta(hours=lookback_hours)).isoformat()

        async with self._conn.execute(
            """
            SELECT
                COUNT(*) as total_rebalance_trades,
                SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) as successful,
                SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN status = 'PARTIAL' THEN 1 ELSE 0 END) as partial,
                AVG(CASE WHEN status = 'SUCCESS' THEN profit ELSE NULL END) as avg_profit_on_success,
                SUM(CASE WHEN status = 'SUCCESS' THEN profit ELSE 0 END) as total_rebalance_profit
            FROM rebalance_trades
            WHERE attempted_at >= ?
            """,
            (cutoff,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else {}


    # ========== Settlement Queue Operations ==========

    async def add_to_settlement_queue(
        self,
        trade_id: str,
        condition_id: str,
        token_id: str,
        side: str,
        asset: str,
        shares: float,
        entry_price: float,
        entry_cost: float,
        market_end_time: datetime,
    ) -> None:
        """Add a position to the settlement queue.

        Called after a successful trade to track positions for claiming
        after market resolution.

        Args:
            trade_id: Associated trade ID
            condition_id: Market condition ID
            token_id: Token ID (YES or NO token)
            side: "YES" or "NO"
            asset: Asset symbol (BTC, ETH, SOL)
            shares: Number of shares held
            entry_price: Price per share at entry
            entry_cost: Total cost (shares * entry_price)
            market_end_time: When the market resolves
        """
        async with self._lock:
            await self._conn.execute(
                """
                INSERT OR REPLACE INTO settlement_queue (
                    trade_id, condition_id, token_id, side, asset,
                    shares, entry_price, entry_cost, market_end_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id, condition_id, token_id, side, asset,
                    shares, entry_price, entry_cost,
                    market_end_time.isoformat() if market_end_time else None,
                ),
            )
            await self._conn.commit()

        log.debug(
            "Added position to settlement queue",
            trade_id=trade_id[:8] + "...",
            asset=asset,
            side=side,
            shares=shares,
        )

    async def get_unclaimed_positions(self) -> List[Dict[str, Any]]:
        """Get all positions that haven't been claimed yet.

        Returns:
            List of unclaimed position dicts
        """
        async with self._conn.execute(
            """
            SELECT * FROM settlement_queue
            WHERE claimed = 0
            ORDER BY market_end_time ASC
            """,
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_claimable_positions(
        self,
        min_time_since_end_seconds: int = 600,
    ) -> List[Dict[str, Any]]:
        """Get positions ready to be claimed (market ended + wait period).

        Args:
            min_time_since_end_seconds: Minimum seconds after market end (default 10 min)

        Returns:
            List of positions ready for claiming
        """
        from datetime import datetime, timedelta

        # Calculate cutoff - positions where market_end_time + wait_period < now
        cutoff = (datetime.utcnow() - timedelta(seconds=min_time_since_end_seconds)).isoformat()

        async with self._conn.execute(
            """
            SELECT * FROM settlement_queue
            WHERE claimed = 0
              AND market_end_time <= ?
            ORDER BY market_end_time ASC
            """,
            (cutoff,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def mark_position_claimed(
        self,
        trade_id: str,
        token_id: str,
        proceeds: float,
        profit: float,
    ) -> None:
        """Mark a position as successfully claimed.

        Args:
            trade_id: Trade ID
            token_id: Token ID that was sold
            proceeds: USD received from sale
            profit: Profit (proceeds - entry_cost)
        """
        async with self._lock:
            await self._conn.execute(
                """
                UPDATE settlement_queue
                SET claimed = 1,
                    claimed_at = CURRENT_TIMESTAMP,
                    claim_proceeds = ?,
                    claim_profit = ?
                WHERE trade_id = ? AND token_id = ?
                """,
                (proceeds, profit, trade_id, token_id),
            )
            await self._conn.commit()

        log.info(
            "Position marked as claimed",
            trade_id=trade_id[:8] + "...",
            proceeds=f"${proceeds:.2f}",
            profit=f"${profit:.2f}",
        )

    async def record_claim_attempt(
        self,
        trade_id: str,
        token_id: str,
        error: str = None,
    ) -> None:
        """Record a claim attempt (successful or not).

        Args:
            trade_id: Trade ID
            token_id: Token ID
            error: Error message if failed (None if successful)
        """
        async with self._lock:
            await self._conn.execute(
                """
                UPDATE settlement_queue
                SET claim_attempts = claim_attempts + 1,
                    last_claim_error = ?
                WHERE trade_id = ? AND token_id = ?
                """,
                (error, trade_id, token_id),
            )
            await self._conn.commit()

    async def get_settlement_stats(self) -> Dict[str, Any]:
        """Get settlement queue statistics.

        Returns:
            Dict with queue statistics
        """
        async with self._conn.execute(
            """
            SELECT
                COUNT(*) as total_positions,
                SUM(CASE WHEN claimed = 0 THEN 1 ELSE 0 END) as unclaimed,
                SUM(CASE WHEN claimed = 1 THEN 1 ELSE 0 END) as claimed,
                SUM(CASE WHEN claimed = 0 THEN shares * entry_price ELSE 0 END) as unclaimed_value,
                SUM(CASE WHEN claimed = 1 THEN claim_profit ELSE 0 END) as total_claim_profit,
                AVG(claim_attempts) as avg_claim_attempts
            FROM settlement_queue
            """,
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else {}

    async def get_settlement_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get settlement queue history for dashboard display.

        Returns positions ordered by most recent first (by created_at or market_end_time).

        Args:
            limit: Maximum number of positions to return

        Returns:
            List of position dicts with relevant fields for display
        """
        async with self._conn.execute(
            """
            SELECT
                id,
                created_at,
                trade_id,
                condition_id,
                side,
                asset,
                shares,
                entry_price,
                entry_cost,
                market_end_time,
                claimed,
                claimed_at,
                claim_proceeds,
                claim_profit
            FROM settlement_queue
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def cleanup_old_claimed_positions(self, days: int = 30) -> int:
        """Delete claimed positions older than specified days.

        Args:
            days: Delete positions claimed more than this many days ago

        Returns:
            Number of deleted records
        """
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        async with self._lock:
            cursor = await self._conn.execute(
                """
                DELETE FROM settlement_queue
                WHERE claimed = 1 AND claimed_at < ?
                """,
                (cutoff,),
            )
            deleted = cursor.rowcount
            await self._conn.commit()
            return deleted

    # ========== Circuit Breaker State Operations ==========

    async def get_circuit_breaker_state(self) -> Dict[str, Any]:
        """Get current circuit breaker state.

        Handles daily reset automatically - if the stored date is not today,
        resets the state for the new day.

        Returns:
            Dict with circuit breaker state
        """
        today = datetime.utcnow().strftime("%Y-%m-%d")

        async with self._conn.execute(
            "SELECT * FROM circuit_breaker_state WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()

            if row is None:
                # Initialize if not exists
                await self._conn.execute(
                    """
                    INSERT INTO circuit_breaker_state (id, date)
                    VALUES (1, ?)
                    """,
                    (today,),
                )
                await self._conn.commit()
                return {
                    "date": today,
                    "realized_pnl": 0.0,
                    "circuit_breaker_hit": False,
                    "hit_at": None,
                    "hit_reason": None,
                    "total_trades_today": 0,
                }

            state = dict(row)

            # Check if we need to reset for new day
            if state["date"] != today:
                log.info(
                    "New trading day - resetting circuit breaker state",
                    previous_date=state["date"],
                    previous_pnl=f"${state['realized_pnl']:.2f}",
                    new_date=today,
                )
                await self._conn.execute(
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
                await self._conn.commit()
                return {
                    "date": today,
                    "realized_pnl": 0.0,
                    "circuit_breaker_hit": False,
                    "hit_at": None,
                    "hit_reason": None,
                    "total_trades_today": 0,
                }

            return {
                "date": state["date"],
                "realized_pnl": state["realized_pnl"] or 0.0,
                "circuit_breaker_hit": bool(state["circuit_breaker_hit"]),
                "hit_at": state["hit_at"],
                "hit_reason": state["hit_reason"],
                "total_trades_today": state["total_trades_today"] or 0,
            }

    async def record_realized_pnl(
        self,
        trade_id: str,
        pnl_amount: float,
        pnl_type: str,
        max_daily_loss: float,
        notes: str = None,
    ) -> Dict[str, Any]:
        """Record realized P&L and check circuit breaker.

        Args:
            trade_id: Associated trade ID
            pnl_amount: Actual profit/loss amount
            pnl_type: Type of P&L ('resolution', 'settlement', 'rebalance')
            max_daily_loss: Maximum daily loss threshold (positive number)
            notes: Optional notes

        Returns:
            Updated circuit breaker state including whether it was triggered
        """
        today = datetime.utcnow().strftime("%Y-%m-%d")

        async with self._lock:
            # Insert into ledger (ignore if duplicate)
            try:
                await self._conn.execute(
                    """
                    INSERT INTO realized_pnl_ledger (trade_id, trade_date, pnl_amount, pnl_type, notes)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (trade_id, today, pnl_amount, pnl_type, notes),
                )
            except Exception as e:
                # Duplicate entry - already recorded
                log.debug(
                    "PnL already recorded for trade",
                    trade_id=trade_id,
                    pnl_type=pnl_type,
                    error=str(e),
                )
                # Still need to return current state
                return await self.get_circuit_breaker_state()

            # Get current state (handles daily reset)
            state = await self.get_circuit_breaker_state()
            new_pnl = state["realized_pnl"] + pnl_amount
            new_trade_count = state["total_trades_today"] + 1

            # Check if we need to trigger circuit breaker
            circuit_breaker_hit = state["circuit_breaker_hit"]
            hit_at = state["hit_at"]
            hit_reason = state["hit_reason"]

            if not circuit_breaker_hit and new_pnl <= -max_daily_loss:
                circuit_breaker_hit = True
                hit_at = datetime.utcnow().isoformat()
                hit_reason = f"Daily loss limit exceeded: ${abs(new_pnl):.2f} >= ${max_daily_loss:.2f}"
                log.warning(
                    "CIRCUIT BREAKER TRIGGERED",
                    realized_pnl=f"${new_pnl:.2f}",
                    max_daily_loss=f"${max_daily_loss:.2f}",
                    trigger_trade=trade_id,
                )

            # Update state
            await self._conn.execute(
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
                (new_pnl, circuit_breaker_hit, hit_at, hit_reason, new_trade_count),
            )
            await self._conn.commit()

        log.info(
            "Recorded realized P&L",
            trade_id=trade_id[:8] + "..." if len(trade_id) > 8 else trade_id,
            pnl=f"${pnl_amount:.2f}",
            pnl_type=pnl_type,
            daily_total=f"${new_pnl:.2f}",
            circuit_breaker_hit=circuit_breaker_hit,
        )

        return {
            "date": today,
            "realized_pnl": new_pnl,
            "circuit_breaker_hit": circuit_breaker_hit,
            "hit_at": hit_at,
            "hit_reason": hit_reason,
            "total_trades_today": new_trade_count,
        }

    async def reset_circuit_breaker(self, reason: str = "Manual reset") -> None:
        """Manually reset circuit breaker (keeps PnL, clears hit flag).

        Args:
            reason: Reason for manual reset
        """
        async with self._lock:
            await self._conn.execute(
                """
                UPDATE circuit_breaker_state
                SET circuit_breaker_hit = 0,
                    hit_at = NULL,
                    hit_reason = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
                """
            )
            await self._conn.commit()

        log.info("Circuit breaker manually reset", reason=reason)

    async def get_daily_pnl_breakdown(self, date: str = None) -> List[Dict[str, Any]]:
        """Get P&L breakdown for a specific day.

        Args:
            date: Date in YYYY-MM-DD format (default: today)

        Returns:
            List of P&L entries for the day
        """
        if date is None:
            date = datetime.utcnow().strftime("%Y-%m-%d")

        async with self._conn.execute(
            """
            SELECT trade_id, pnl_amount, pnl_type, notes, created_at
            FROM realized_pnl_ledger
            WHERE trade_date = ?
            ORDER BY created_at ASC
            """,
            (date,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


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
