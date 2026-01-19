"""Migration V003: Add retry tracking fields to settlement_queue.

Adds next_retry_at column to track when a failed claim can be retried,
enabling exponential backoff for claim retries.
"""

VERSION = 3
DESCRIPTION = "Add next_retry_at column to settlement_queue for retry scheduling"

UP_SQL = """
-- Add next_retry_at column to settlement_queue for retry scheduling
ALTER TABLE settlement_queue ADD COLUMN next_retry_at TIMESTAMP;

-- Add index for efficient querying of retryable claims
CREATE INDEX IF NOT EXISTS idx_settlement_next_retry ON settlement_queue(next_retry_at);

-- Update schema version
INSERT OR REPLACE INTO schema_version (version) VALUES (4);
"""

DOWN_SQL = """
-- Note: SQLite < 3.35.0 doesn't support DROP COLUMN
-- This is a no-op for older versions

-- Revert schema version
INSERT OR REPLACE INTO schema_version (version) VALUES (3);
"""
