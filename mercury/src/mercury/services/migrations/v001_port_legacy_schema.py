"""Migration V001: Port database schema from polyjuiced legacy.

Ports tables from legacy/src/persistence.py:
- trades: Enhanced with execution tracking, liquidity context, hedge ratio
- settlement_queue: Enhanced with token_id, asset, claim tracking
- daily_stats: Enhanced with opportunities tracking
- fill_records: Detailed fill tracking for slippage analysis
- trade_telemetry: Execution timing data
- rebalance_trades: Individual rebalancing trades
- circuit_breaker_state: Daily loss tracking
- realized_pnl_ledger: Audit trail for realized P&L

NOT ported (per task spec):
- markets: rediscovered dynamically
- logs: use structlog instead
- liquidity_snapshots: not needed
"""

VERSION = 1
DESCRIPTION = "Port database schema from polyjuiced legacy"

UP_SQL = """
-- =========================================================================
-- ENHANCED TRADES TABLE
-- Adds execution tracking, liquidity context, and hedge ratio fields
-- =========================================================================

-- Add new columns to trades table (if not exists checks via temp approach)
-- Using separate ALTER TABLE statements for compatibility

-- Execution tracking columns
ALTER TABLE trades ADD COLUMN condition_id TEXT;
ALTER TABLE trades ADD COLUMN asset TEXT;
ALTER TABLE trades ADD COLUMN yes_price REAL;
ALTER TABLE trades ADD COLUMN no_price REAL;
ALTER TABLE trades ADD COLUMN yes_cost REAL;
ALTER TABLE trades ADD COLUMN no_cost REAL;
ALTER TABLE trades ADD COLUMN spread REAL;
ALTER TABLE trades ADD COLUMN expected_profit REAL;
ALTER TABLE trades ADD COLUMN actual_profit REAL;
ALTER TABLE trades ADD COLUMN market_end_time TEXT;
ALTER TABLE trades ADD COLUMN market_slug TEXT;
ALTER TABLE trades ADD COLUMN dry_run BOOLEAN DEFAULT 0;

-- Shares and hedge tracking
ALTER TABLE trades ADD COLUMN yes_shares REAL;
ALTER TABLE trades ADD COLUMN no_shares REAL;
ALTER TABLE trades ADD COLUMN hedge_ratio REAL;
ALTER TABLE trades ADD COLUMN execution_status TEXT;
ALTER TABLE trades ADD COLUMN yes_order_status TEXT;
ALTER TABLE trades ADD COLUMN no_order_status TEXT;

-- Liquidity context at execution time
ALTER TABLE trades ADD COLUMN yes_liquidity_at_price REAL;
ALTER TABLE trades ADD COLUMN no_liquidity_at_price REAL;
ALTER TABLE trades ADD COLUMN yes_book_depth_total REAL;
ALTER TABLE trades ADD COLUMN no_book_depth_total REAL;

-- Resolution tracking
ALTER TABLE trades ADD COLUMN resolved_at TIMESTAMP;

-- Create additional indexes for trades
CREATE INDEX IF NOT EXISTS idx_trades_condition ON trades(condition_id);
CREATE INDEX IF NOT EXISTS idx_trades_asset ON trades(asset);
CREATE INDEX IF NOT EXISTS idx_trades_execution_status ON trades(execution_status);

-- =========================================================================
-- ENHANCED SETTLEMENT QUEUE
-- Adds token_id, asset, claim tracking fields
-- =========================================================================

ALTER TABLE settlement_queue ADD COLUMN trade_id TEXT;
ALTER TABLE settlement_queue ADD COLUMN token_id TEXT;
ALTER TABLE settlement_queue ADD COLUMN asset TEXT;
ALTER TABLE settlement_queue ADD COLUMN shares REAL;
ALTER TABLE settlement_queue ADD COLUMN entry_cost REAL;
ALTER TABLE settlement_queue ADD COLUMN market_end_time TIMESTAMP;
ALTER TABLE settlement_queue ADD COLUMN claimed BOOLEAN DEFAULT 0;
ALTER TABLE settlement_queue ADD COLUMN claim_proceeds REAL;
ALTER TABLE settlement_queue ADD COLUMN claim_profit REAL;
ALTER TABLE settlement_queue ADD COLUMN claim_attempts INTEGER DEFAULT 0;
ALTER TABLE settlement_queue ADD COLUMN last_claim_error TEXT;

-- Create additional indexes for settlement_queue
CREATE INDEX IF NOT EXISTS idx_settlement_unclaimed ON settlement_queue(claimed, market_end_time);
CREATE INDEX IF NOT EXISTS idx_settlement_trade ON settlement_queue(trade_id);

-- =========================================================================
-- ENHANCED DAILY STATS
-- Adds opportunities tracking
-- =========================================================================

ALTER TABLE daily_stats ADD COLUMN wins INTEGER DEFAULT 0;
ALTER TABLE daily_stats ADD COLUMN losses INTEGER DEFAULT 0;
ALTER TABLE daily_stats ADD COLUMN exposure REAL DEFAULT 0;
ALTER TABLE daily_stats ADD COLUMN opportunities_detected INTEGER DEFAULT 0;
ALTER TABLE daily_stats ADD COLUMN opportunities_executed INTEGER DEFAULT 0;

-- =========================================================================
-- FILL RECORDS TABLE
-- Detailed fill tracking for slippage analysis (ported from legacy)
-- =========================================================================

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

-- Indexes for fill_records
CREATE INDEX IF NOT EXISTS idx_fill_records_timestamp ON fill_records(timestamp);
CREATE INDEX IF NOT EXISTS idx_fill_records_token ON fill_records(token_id);
CREATE INDEX IF NOT EXISTS idx_fill_records_asset ON fill_records(asset);
CREATE INDEX IF NOT EXISTS idx_fill_records_condition ON fill_records(condition_id);

-- =========================================================================
-- TRADE TELEMETRY TABLE
-- Detailed timing telemetry for each trade (ported from legacy)
-- =========================================================================

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
    actual_profit REAL
);

-- Indexes for trade_telemetry
CREATE INDEX IF NOT EXISTS idx_telemetry_detected ON trade_telemetry(opportunity_detected_at);
CREATE INDEX IF NOT EXISTS idx_telemetry_resolved ON trade_telemetry(resolved_at);

-- =========================================================================
-- REBALANCE TRADES TABLE
-- Individual rebalancing trades (ported from legacy)
-- =========================================================================

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
    order_id TEXT
);

-- Indexes for rebalance_trades
CREATE INDEX IF NOT EXISTS idx_rebalance_trade_id ON rebalance_trades(trade_id);
CREATE INDEX IF NOT EXISTS idx_rebalance_status ON rebalance_trades(status);
CREATE INDEX IF NOT EXISTS idx_rebalance_attempted ON rebalance_trades(attempted_at);

-- =========================================================================
-- CIRCUIT BREAKER STATE TABLE
-- Persists across restarts, tracks daily loss limits
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

-- Initialize singleton row
INSERT OR IGNORE INTO circuit_breaker_state (id, date)
VALUES (1, date('now'));

-- =========================================================================
-- REALIZED PNL LEDGER TABLE
-- Audit trail for realized P&L entries
-- =========================================================================

CREATE TABLE IF NOT EXISTS realized_pnl_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    trade_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,               -- YYYY-MM-DD for daily aggregation
    pnl_amount REAL NOT NULL,               -- Actual profit/loss
    pnl_type TEXT NOT NULL,                 -- 'resolution', 'settlement', 'rebalance', 'historical_import'
    notes TEXT,
    UNIQUE(trade_id, pnl_type)              -- Prevent duplicate entries
);

-- Indexes for realized_pnl_ledger
CREATE INDEX IF NOT EXISTS idx_pnl_ledger_date ON realized_pnl_ledger(trade_date);
CREATE INDEX IF NOT EXISTS idx_pnl_ledger_trade ON realized_pnl_ledger(trade_id);
CREATE INDEX IF NOT EXISTS idx_pnl_ledger_type ON realized_pnl_ledger(pnl_type);

-- Update schema version
INSERT OR REPLACE INTO schema_version (version) VALUES (2);
"""

# Note: DOWN_SQL is not fully reversible for ALTER TABLE ADD COLUMN in SQLite
# as SQLite doesn't support DROP COLUMN before version 3.35.0
DOWN_SQL = """
-- WARNING: This migration cannot be fully reversed in older SQLite versions.
-- DROP TABLE statements for new tables only.

DROP TABLE IF EXISTS realized_pnl_ledger;
DROP TABLE IF EXISTS circuit_breaker_state;
DROP TABLE IF EXISTS rebalance_trades;
DROP TABLE IF EXISTS trade_telemetry;
DROP TABLE IF EXISTS fill_records;

-- Revert schema version
INSERT OR REPLACE INTO schema_version (version) VALUES (1);
"""
