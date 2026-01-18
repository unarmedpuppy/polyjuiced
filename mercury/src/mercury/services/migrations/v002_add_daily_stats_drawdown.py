"""Migration V002: Add max_drawdown to daily_stats.

Adds max_drawdown column to track maximum intraday drawdown for risk metrics.
"""

VERSION = 2
DESCRIPTION = "Add max_drawdown column to daily_stats table"

UP_SQL = """
-- Add max_drawdown column to daily_stats for risk tracking
ALTER TABLE daily_stats ADD COLUMN max_drawdown REAL DEFAULT 0;

-- Update schema version
INSERT OR REPLACE INTO schema_version (version) VALUES (3);
"""

DOWN_SQL = """
-- Note: SQLite < 3.35.0 doesn't support DROP COLUMN
-- This is a no-op for older versions

-- Revert schema version
INSERT OR REPLACE INTO schema_version (version) VALUES (2);
"""
