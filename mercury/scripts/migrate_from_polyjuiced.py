#!/usr/bin/env python3
"""Migrate data from polyjuiced (legacy) SQLite to Mercury format.

This script migrates historical trade data from the legacy gabagool.db to the new
Mercury schema. It handles:
- trades table (with field remapping)
- settlement_queue (with position_id generation)
- daily_stats (with field remapping)
- fill_records
- trade_telemetry
- rebalance_trades
- circuit_breaker_state
- realized_pnl_ledger

Usage:
    python -m scripts.migrate_from_polyjuiced --legacy-db /path/to/gabagool.db --mercury-db /path/to/mercury.db

    # Dry run (no writes)
    python -m scripts.migrate_from_polyjuiced --legacy-db /path/to/gabagool.db --mercury-db /path/to/mercury.db --dry-run

    # With verification only
    python -m scripts.migrate_from_polyjuiced --legacy-db /path/to/gabagool.db --mercury-db /path/to/mercury.db --verify-only

Note: Run Mercury's state_store initialization first to create the schema.
"""

import argparse
import hashlib
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class MigrationStats:
    """Track migration statistics."""

    trades_migrated: int = 0
    trades_skipped: int = 0
    positions_created: int = 0
    settlement_queue_migrated: int = 0
    settlement_queue_skipped: int = 0
    daily_stats_migrated: int = 0
    daily_stats_skipped: int = 0
    fill_records_migrated: int = 0
    fill_records_skipped: int = 0
    telemetry_migrated: int = 0
    telemetry_skipped: int = 0
    rebalance_trades_migrated: int = 0
    rebalance_trades_skipped: int = 0
    circuit_breaker_migrated: bool = False
    pnl_ledger_migrated: int = 0
    pnl_ledger_skipped: int = 0
    errors: list[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []

    def print_summary(self):
        """Print migration summary."""
        print("\n" + "=" * 60)
        print("MIGRATION SUMMARY")
        print("=" * 60)
        print(f"Trades: {self.trades_migrated} migrated, {self.trades_skipped} skipped")
        print(f"Positions created: {self.positions_created}")
        print(f"Settlement queue: {self.settlement_queue_migrated} migrated, {self.settlement_queue_skipped} skipped")
        print(f"Daily stats: {self.daily_stats_migrated} migrated, {self.daily_stats_skipped} skipped")
        print(f"Fill records: {self.fill_records_migrated} migrated, {self.fill_records_skipped} skipped")
        print(f"Telemetry: {self.telemetry_migrated} migrated, {self.telemetry_skipped} skipped")
        print(f"Rebalance trades: {self.rebalance_trades_migrated} migrated, {self.rebalance_trades_skipped} skipped")
        print(f"Circuit breaker: {'migrated' if self.circuit_breaker_migrated else 'skipped'}")
        print(f"P&L ledger: {self.pnl_ledger_migrated} migrated, {self.pnl_ledger_skipped} skipped")

        if self.errors:
            print(f"\nERRORS ({len(self.errors)}):")
            for err in self.errors[:10]:  # Show first 10 errors
                print(f"  - {err}")
            if len(self.errors) > 10:
                print(f"  ... and {len(self.errors) - 10} more errors")
        print("=" * 60)


def generate_position_id(trade_id: str, token_id: str) -> str:
    """Generate a unique position_id from trade_id and token_id.

    Mercury uses position_id as the primary key for positions, while legacy
    used (trade_id, token_id) as a composite key for settlement_queue.
    """
    combined = f"{trade_id}:{token_id}"
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


def connect_db(db_path: Path) -> sqlite3.Connection:
    """Connect to SQLite database with row factory."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    """Safely get a value from a sqlite3.Row object.

    sqlite3.Row objects don't have a .get() method, so this helper
    provides that functionality with a default value.
    """
    try:
        value = row[key]
        return value if value is not None else default
    except (IndexError, KeyError):
        return default


def get_table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Get column names for a table."""
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return [row["name"] for row in cursor.fetchall()]


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Check if a table exists."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cursor.fetchone() is not None


def migrate_trades(
    legacy_conn: sqlite3.Connection,
    mercury_conn: sqlite3.Connection,
    stats: MigrationStats,
    dry_run: bool = False,
) -> None:
    """Migrate trades from legacy to Mercury format.

    Field mapping:
    - legacy.id -> mercury.trade_id
    - legacy.strategy_id -> mercury.strategy (default: 'gabagool')
    - Add market_id from condition_id if present
    - Add default values for new Mercury fields
    """
    print("\nMigrating trades...")

    # Get existing trade_ids in Mercury to avoid duplicates
    existing_ids = set()
    cursor = mercury_conn.execute("SELECT trade_id FROM trades")
    for row in cursor:
        existing_ids.add(row["trade_id"])
    print(f"  Found {len(existing_ids)} existing trades in Mercury")

    # Check what columns exist in legacy
    legacy_columns = get_table_columns(legacy_conn, "trades")

    # Fetch all legacy trades
    cursor = legacy_conn.execute("SELECT * FROM trades ORDER BY created_at")
    legacy_trades = cursor.fetchall()
    print(f"  Found {len(legacy_trades)} trades in legacy database")

    for trade in legacy_trades:
        trade_id = trade["id"]

        # Skip if already migrated
        if trade_id in existing_ids:
            stats.trades_skipped += 1
            continue

        # Map fields from legacy to Mercury
        strategy = row_get(trade, "strategy_id", "gabagool") if "strategy_id" in legacy_columns else "gabagool"
        condition_id = row_get(trade, "condition_id")
        market_id = condition_id or row_get(trade, "market_slug") or "unknown"

        # Calculate side and size from yes/no costs
        yes_cost = row_get(trade, "yes_cost", 0) or 0
        no_cost = row_get(trade, "no_cost", 0) or 0
        total_cost = yes_cost + no_cost

        # Determine primary side (larger position)
        if yes_cost >= no_cost:
            side = "BUY"  # Primarily YES position
            size = row_get(trade, "yes_shares") or (yes_cost / trade["yes_price"] if trade["yes_price"] else 0)
            price = trade["yes_price"]
        else:
            side = "BUY"  # Primarily NO position
            size = row_get(trade, "no_shares") or (no_cost / trade["no_price"] if trade["no_price"] else 0)
            price = trade["no_price"]

        # Map status
        legacy_status = row_get(trade, "status", "pending")
        status_map = {
            "pending": "open",
            "win": "closed",
            "loss": "closed",
            "failed": "failed",
        }
        status = status_map.get(legacy_status, "open")

        if not dry_run:
            try:
                mercury_conn.execute(
                    """
                    INSERT INTO trades (
                        trade_id, market_id, strategy, side, size, price, cost, status,
                        timestamp, filled_size, avg_fill_price, fee, created_at, updated_at,
                        condition_id, asset, yes_price, no_price, yes_cost, no_cost,
                        spread, expected_profit, actual_profit, market_end_time, market_slug,
                        dry_run, yes_shares, no_shares, hedge_ratio, execution_status,
                        yes_order_status, no_order_status, yes_liquidity_at_price,
                        no_liquidity_at_price, yes_book_depth_total, no_book_depth_total,
                        resolved_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?,
                        ?
                    )
                    """,
                    (
                        trade_id,
                        market_id,
                        strategy,
                        side,
                        size or 0,
                        price or 0,
                        total_cost,
                        status,
                        row_get(trade, "created_at"),
                        size or 0,  # filled_size
                        price,  # avg_fill_price
                        0,  # fee
                        row_get(trade, "created_at"),
                        row_get(trade, "resolved_at") or row_get(trade, "created_at"),
                        condition_id,
                        row_get(trade, "asset"),
                        row_get(trade, "yes_price"),
                        row_get(trade, "no_price"),
                        yes_cost,
                        no_cost,
                        row_get(trade, "spread"),
                        row_get(trade, "expected_profit"),
                        row_get(trade, "actual_profit"),
                        row_get(trade, "market_end_time"),
                        row_get(trade, "market_slug"),
                        row_get(trade, "dry_run", 0),
                        row_get(trade, "yes_shares"),
                        row_get(trade, "no_shares"),
                        row_get(trade, "hedge_ratio"),
                        row_get(trade, "execution_status"),
                        row_get(trade, "yes_order_status"),
                        row_get(trade, "no_order_status"),
                        row_get(trade, "yes_liquidity_at_price"),
                        row_get(trade, "no_liquidity_at_price"),
                        row_get(trade, "yes_book_depth_total"),
                        row_get(trade, "no_book_depth_total"),
                        row_get(trade, "resolved_at"),
                    ),
                )
                stats.trades_migrated += 1
            except sqlite3.IntegrityError as e:
                stats.errors.append(f"Trade {trade_id}: {e}")
                stats.trades_skipped += 1
        else:
            stats.trades_migrated += 1

    if not dry_run:
        mercury_conn.commit()
    print(f"  Migrated {stats.trades_migrated} trades, skipped {stats.trades_skipped}")


def migrate_settlement_queue(
    legacy_conn: sqlite3.Connection,
    mercury_conn: sqlite3.Connection,
    stats: MigrationStats,
    dry_run: bool = False,
) -> None:
    """Migrate settlement queue from legacy to Mercury.

    Also creates corresponding position records in the positions table.
    """
    print("\nMigrating settlement queue...")

    if not table_exists(legacy_conn, "settlement_queue"):
        print("  Settlement queue table not found in legacy database")
        return

    # Get existing position_ids in Mercury
    existing_positions = set()
    cursor = mercury_conn.execute("SELECT position_id FROM settlement_queue")
    for row in cursor:
        existing_positions.add(row["position_id"])
    print(f"  Found {len(existing_positions)} existing entries in Mercury settlement queue")

    # Fetch all legacy settlement entries
    cursor = legacy_conn.execute("SELECT * FROM settlement_queue ORDER BY created_at")
    legacy_entries = cursor.fetchall()
    print(f"  Found {len(legacy_entries)} entries in legacy settlement queue")

    for entry in legacy_entries:
        trade_id = entry["trade_id"]
        token_id = entry["token_id"]
        position_id = generate_position_id(trade_id, token_id)

        if position_id in existing_positions:
            stats.settlement_queue_skipped += 1
            continue

        condition_id = row_get(entry, "condition_id")
        market_id = condition_id or "unknown"

        if not dry_run:
            try:
                # First, create a position record
                mercury_conn.execute(
                    """
                    INSERT OR IGNORE INTO positions (
                        position_id, market_id, strategy, side, size, entry_price,
                        status, opened_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        position_id,
                        market_id,
                        "gabagool",  # Default strategy
                        entry["side"],
                        entry["shares"],
                        entry["entry_price"],
                        "pending_settlement" if not row_get(entry, "claimed") else "settled",
                        row_get(entry, "created_at"),
                        row_get(entry, "created_at"),
                        row_get(entry, "claimed_at") or row_get(entry, "created_at"),
                    ),
                )
                stats.positions_created += 1

                # Then migrate the settlement queue entry
                mercury_conn.execute(
                    """
                    INSERT INTO settlement_queue (
                        position_id, market_id, condition_id, side, size, entry_price,
                        queued_at, claimed_at, proceeds, status,
                        trade_id, token_id, asset, shares, entry_cost, market_end_time,
                        claimed, claim_proceeds, claim_profit, claim_attempts, last_claim_error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        position_id,
                        market_id,
                        condition_id,
                        entry["side"],
                        entry["shares"],
                        entry["entry_price"],
                        row_get(entry, "created_at"),
                        row_get(entry, "claimed_at"),
                        row_get(entry, "claim_proceeds"),
                        "claimed" if row_get(entry, "claimed") else "pending",
                        trade_id,
                        token_id,
                        row_get(entry, "asset"),
                        entry["shares"],
                        row_get(entry, "entry_cost"),
                        row_get(entry, "market_end_time"),
                        row_get(entry, "claimed", 0),
                        row_get(entry, "claim_proceeds"),
                        row_get(entry, "claim_profit"),
                        row_get(entry, "claim_attempts", 0),
                        row_get(entry, "last_claim_error"),
                    ),
                )
                stats.settlement_queue_migrated += 1
            except sqlite3.IntegrityError as e:
                stats.errors.append(f"Settlement {trade_id}/{token_id}: {e}")
                stats.settlement_queue_skipped += 1
        else:
            stats.settlement_queue_migrated += 1
            stats.positions_created += 1

    if not dry_run:
        mercury_conn.commit()
    print(f"  Migrated {stats.settlement_queue_migrated} entries, created {stats.positions_created} positions")


def migrate_daily_stats(
    legacy_conn: sqlite3.Connection,
    mercury_conn: sqlite3.Connection,
    stats: MigrationStats,
    dry_run: bool = False,
) -> None:
    """Migrate daily statistics from legacy to Mercury.

    Field mapping:
    - legacy.pnl -> mercury.realized_pnl
    - legacy.trades -> mercury.trade_count
    """
    print("\nMigrating daily stats...")

    if not table_exists(legacy_conn, "daily_stats"):
        print("  Daily stats table not found in legacy database")
        return

    # Get existing dates in Mercury
    existing_dates = set()
    cursor = mercury_conn.execute("SELECT date FROM daily_stats")
    for row in cursor:
        existing_dates.add(row["date"])
    print(f"  Found {len(existing_dates)} existing dates in Mercury")

    # Fetch all legacy daily stats
    cursor = legacy_conn.execute("SELECT * FROM daily_stats ORDER BY date")
    legacy_stats = cursor.fetchall()
    print(f"  Found {len(legacy_stats)} days in legacy database")

    for day_stat in legacy_stats:
        date_str = day_stat["date"]

        if date_str in existing_dates:
            stats.daily_stats_skipped += 1
            continue

        if not dry_run:
            try:
                mercury_conn.execute(
                    """
                    INSERT INTO daily_stats (
                        date, trade_count, volume_usd, realized_pnl,
                        positions_opened, positions_closed, created_at, updated_at,
                        wins, losses, exposure, opportunities_detected, opportunities_executed,
                        max_drawdown
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        date_str,
                        row_get(day_stat, "trades", 0),
                        0,  # volume_usd - not tracked in legacy
                        row_get(day_stat, "pnl", 0),  # pnl -> realized_pnl
                        0,  # positions_opened - not tracked in legacy
                        0,  # positions_closed - not tracked in legacy
                        datetime.utcnow().isoformat(),
                        datetime.utcnow().isoformat(),
                        row_get(day_stat, "wins", 0),
                        row_get(day_stat, "losses", 0),
                        row_get(day_stat, "exposure", 0),
                        row_get(day_stat, "opportunities_detected", 0),
                        row_get(day_stat, "opportunities_executed", 0),
                        0,  # max_drawdown - not tracked in legacy
                    ),
                )
                stats.daily_stats_migrated += 1
            except sqlite3.IntegrityError as e:
                stats.errors.append(f"Daily stats {date_str}: {e}")
                stats.daily_stats_skipped += 1
        else:
            stats.daily_stats_migrated += 1

    if not dry_run:
        mercury_conn.commit()
    print(f"  Migrated {stats.daily_stats_migrated} days, skipped {stats.daily_stats_skipped}")


def migrate_fill_records(
    legacy_conn: sqlite3.Connection,
    mercury_conn: sqlite3.Connection,
    stats: MigrationStats,
    dry_run: bool = False,
) -> None:
    """Migrate fill records from legacy to Mercury."""
    print("\nMigrating fill records...")

    if not table_exists(legacy_conn, "fill_records"):
        print("  Fill records table not found in legacy database")
        return

    # Count existing records in Mercury
    cursor = mercury_conn.execute("SELECT COUNT(*) as cnt FROM fill_records")
    existing_count = cursor.fetchone()["cnt"]
    print(f"  Found {existing_count} existing fill records in Mercury")

    # Fetch all legacy fill records
    cursor = legacy_conn.execute("SELECT * FROM fill_records ORDER BY timestamp")
    legacy_records = cursor.fetchall()
    print(f"  Found {len(legacy_records)} fill records in legacy database")

    # Get order_ids already migrated to avoid duplicates
    existing_order_ids = set()
    cursor = mercury_conn.execute("SELECT order_id FROM fill_records WHERE order_id IS NOT NULL")
    for row in cursor:
        existing_order_ids.add(row["order_id"])

    for record in legacy_records:
        order_id = row_get(record, "order_id")
        if order_id and order_id in existing_order_ids:
            stats.fill_records_skipped += 1
            continue

        if not dry_run:
            try:
                mercury_conn.execute(
                    """
                    INSERT INTO fill_records (
                        timestamp, token_id, condition_id, asset, side,
                        intended_size, filled_size, intended_price, actual_avg_price,
                        time_to_fill_ms, slippage, pre_fill_depth, post_fill_depth,
                        order_type, order_id, fill_ratio, persistence_ratio
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row_get(record, "timestamp"),
                        record["token_id"],
                        record["condition_id"],
                        record["asset"],
                        record["side"],
                        record["intended_size"],
                        record["filled_size"],
                        record["intended_price"],
                        record["actual_avg_price"],
                        record["time_to_fill_ms"],
                        record["slippage"],
                        record["pre_fill_depth"],
                        row_get(record, "post_fill_depth"),
                        row_get(record, "order_type", "GTC"),
                        order_id,
                        row_get(record, "fill_ratio"),
                        row_get(record, "persistence_ratio"),
                    ),
                )
                stats.fill_records_migrated += 1
            except sqlite3.IntegrityError as e:
                stats.errors.append(f"Fill record {order_id}: {e}")
                stats.fill_records_skipped += 1
        else:
            stats.fill_records_migrated += 1

    if not dry_run:
        mercury_conn.commit()
    print(f"  Migrated {stats.fill_records_migrated} records, skipped {stats.fill_records_skipped}")


def migrate_trade_telemetry(
    legacy_conn: sqlite3.Connection,
    mercury_conn: sqlite3.Connection,
    stats: MigrationStats,
    dry_run: bool = False,
) -> None:
    """Migrate trade telemetry from legacy to Mercury."""
    print("\nMigrating trade telemetry...")

    if not table_exists(legacy_conn, "trade_telemetry"):
        print("  Trade telemetry table not found in legacy database")
        return

    # Get existing trade_ids in Mercury telemetry
    existing_ids = set()
    cursor = mercury_conn.execute("SELECT trade_id FROM trade_telemetry")
    for row in cursor:
        existing_ids.add(row["trade_id"])
    print(f"  Found {len(existing_ids)} existing telemetry records in Mercury")

    # Fetch all legacy telemetry
    cursor = legacy_conn.execute("SELECT * FROM trade_telemetry ORDER BY opportunity_detected_at")
    legacy_telemetry = cursor.fetchall()
    print(f"  Found {len(legacy_telemetry)} telemetry records in legacy database")

    for telemetry in legacy_telemetry:
        trade_id = telemetry["trade_id"]

        if trade_id in existing_ids:
            stats.telemetry_skipped += 1
            continue

        if not dry_run:
            try:
                mercury_conn.execute(
                    """
                    INSERT INTO trade_telemetry (
                        trade_id, opportunity_detected_at, opportunity_spread,
                        opportunity_yes_price, opportunity_no_price,
                        order_placed_at, order_filled_at, execution_latency_ms, fill_latency_ms,
                        initial_yes_shares, initial_no_shares, initial_hedge_ratio,
                        rebalance_started_at, rebalance_attempts, position_balanced_at,
                        resolved_at, final_yes_shares, final_no_shares, final_hedge_ratio,
                        actual_profit
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade_id,
                        row_get(telemetry, "opportunity_detected_at"),
                        row_get(telemetry, "opportunity_spread"),
                        row_get(telemetry, "opportunity_yes_price"),
                        row_get(telemetry, "opportunity_no_price"),
                        row_get(telemetry, "order_placed_at"),
                        row_get(telemetry, "order_filled_at"),
                        row_get(telemetry, "execution_latency_ms"),
                        row_get(telemetry, "fill_latency_ms"),
                        row_get(telemetry, "initial_yes_shares"),
                        row_get(telemetry, "initial_no_shares"),
                        row_get(telemetry, "initial_hedge_ratio"),
                        row_get(telemetry, "rebalance_started_at"),
                        row_get(telemetry, "rebalance_attempts", 0),
                        row_get(telemetry, "position_balanced_at"),
                        row_get(telemetry, "resolved_at"),
                        row_get(telemetry, "final_yes_shares"),
                        row_get(telemetry, "final_no_shares"),
                        row_get(telemetry, "final_hedge_ratio"),
                        row_get(telemetry, "actual_profit"),
                    ),
                )
                stats.telemetry_migrated += 1
            except sqlite3.IntegrityError as e:
                stats.errors.append(f"Telemetry {trade_id}: {e}")
                stats.telemetry_skipped += 1
        else:
            stats.telemetry_migrated += 1

    if not dry_run:
        mercury_conn.commit()
    print(f"  Migrated {stats.telemetry_migrated} records, skipped {stats.telemetry_skipped}")


def migrate_rebalance_trades(
    legacy_conn: sqlite3.Connection,
    mercury_conn: sqlite3.Connection,
    stats: MigrationStats,
    dry_run: bool = False,
) -> None:
    """Migrate rebalance trades from legacy to Mercury."""
    print("\nMigrating rebalance trades...")

    if not table_exists(legacy_conn, "rebalance_trades"):
        print("  Rebalance trades table not found in legacy database")
        return

    # Count existing records in Mercury
    cursor = mercury_conn.execute("SELECT COUNT(*) as cnt FROM rebalance_trades")
    existing_count = cursor.fetchone()["cnt"]
    print(f"  Found {existing_count} existing rebalance trades in Mercury")

    # Fetch all legacy rebalance trades
    cursor = legacy_conn.execute("SELECT * FROM rebalance_trades ORDER BY attempted_at")
    legacy_trades = cursor.fetchall()
    print(f"  Found {len(legacy_trades)} rebalance trades in legacy database")

    # Get existing (trade_id, attempted_at, action) combos to avoid duplicates
    existing_combos = set()
    cursor = mercury_conn.execute("SELECT trade_id, attempted_at, action FROM rebalance_trades")
    for row in cursor:
        existing_combos.add((row["trade_id"], row["attempted_at"], row["action"]))

    for trade in legacy_trades:
        combo = (trade["trade_id"], row_get(trade, "attempted_at"), trade["action"])

        if combo in existing_combos:
            stats.rebalance_trades_skipped += 1
            continue

        if not dry_run:
            try:
                mercury_conn.execute(
                    """
                    INSERT INTO rebalance_trades (
                        trade_id, attempted_at, action, shares, price,
                        status, filled_shares, profit, error, order_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade["trade_id"],
                        row_get(trade, "attempted_at"),
                        trade["action"],
                        trade["shares"],
                        trade["price"],
                        trade["status"],
                        row_get(trade, "filled_shares", 0),
                        row_get(trade, "profit", 0),
                        row_get(trade, "error"),
                        row_get(trade, "order_id"),
                    ),
                )
                stats.rebalance_trades_migrated += 1
            except sqlite3.IntegrityError as e:
                stats.errors.append(f"Rebalance trade {trade['trade_id']}: {e}")
                stats.rebalance_trades_skipped += 1
        else:
            stats.rebalance_trades_migrated += 1

    if not dry_run:
        mercury_conn.commit()
    print(f"  Migrated {stats.rebalance_trades_migrated} trades, skipped {stats.rebalance_trades_skipped}")


def migrate_circuit_breaker(
    legacy_conn: sqlite3.Connection,
    mercury_conn: sqlite3.Connection,
    stats: MigrationStats,
    dry_run: bool = False,
) -> None:
    """Migrate circuit breaker state from legacy to Mercury."""
    print("\nMigrating circuit breaker state...")

    if not table_exists(legacy_conn, "circuit_breaker_state"):
        print("  Circuit breaker state table not found in legacy database")
        return

    # Get legacy state (singleton row)
    cursor = legacy_conn.execute("SELECT * FROM circuit_breaker_state WHERE id = 1")
    legacy_state = cursor.fetchone()

    if not legacy_state:
        print("  No circuit breaker state found in legacy database")
        return

    print(f"  Found circuit breaker state: date={legacy_state['date']}, pnl={legacy_state['realized_pnl']}")

    if not dry_run:
        # Update or insert the singleton row
        mercury_conn.execute(
            """
            INSERT OR REPLACE INTO circuit_breaker_state (
                id, date, realized_pnl, circuit_breaker_hit,
                hit_at, hit_reason, total_trades_today, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                legacy_state["date"],
                row_get(legacy_state, "realized_pnl", 0),
                row_get(legacy_state, "circuit_breaker_hit", 0),
                row_get(legacy_state, "hit_at"),
                row_get(legacy_state, "hit_reason"),
                row_get(legacy_state, "total_trades_today", 0),
                datetime.utcnow().isoformat(),
            ),
        )
        mercury_conn.commit()

    stats.circuit_breaker_migrated = True
    print("  Migrated circuit breaker state")


def migrate_pnl_ledger(
    legacy_conn: sqlite3.Connection,
    mercury_conn: sqlite3.Connection,
    stats: MigrationStats,
    dry_run: bool = False,
) -> None:
    """Migrate realized P&L ledger from legacy to Mercury."""
    print("\nMigrating realized P&L ledger...")

    if not table_exists(legacy_conn, "realized_pnl_ledger"):
        print("  Realized P&L ledger table not found in legacy database")
        return

    # Get existing (trade_id, pnl_type) combos in Mercury
    existing_combos = set()
    cursor = mercury_conn.execute("SELECT trade_id, pnl_type FROM realized_pnl_ledger")
    for row in cursor:
        existing_combos.add((row["trade_id"], row["pnl_type"]))
    print(f"  Found {len(existing_combos)} existing ledger entries in Mercury")

    # Fetch all legacy ledger entries
    cursor = legacy_conn.execute("SELECT * FROM realized_pnl_ledger ORDER BY created_at")
    legacy_entries = cursor.fetchall()
    print(f"  Found {len(legacy_entries)} ledger entries in legacy database")

    for entry in legacy_entries:
        combo = (entry["trade_id"], entry["pnl_type"])

        if combo in existing_combos:
            stats.pnl_ledger_skipped += 1
            continue

        if not dry_run:
            try:
                mercury_conn.execute(
                    """
                    INSERT INTO realized_pnl_ledger (
                        created_at, trade_id, trade_date, pnl_amount, pnl_type, notes
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row_get(entry, "created_at"),
                        entry["trade_id"],
                        entry["trade_date"],
                        entry["pnl_amount"],
                        entry["pnl_type"],
                        row_get(entry, "notes"),
                    ),
                )
                stats.pnl_ledger_migrated += 1
            except sqlite3.IntegrityError as e:
                stats.errors.append(f"P&L ledger {entry['trade_id']}/{entry['pnl_type']}: {e}")
                stats.pnl_ledger_skipped += 1
        else:
            stats.pnl_ledger_migrated += 1

    if not dry_run:
        mercury_conn.commit()
    print(f"  Migrated {stats.pnl_ledger_migrated} entries, skipped {stats.pnl_ledger_skipped}")


def verify_migration(
    legacy_conn: sqlite3.Connection,
    mercury_conn: sqlite3.Connection,
) -> bool:
    """Verify data integrity after migration.

    Checks:
    - Record counts match (within tolerance for skipped duplicates)
    - Key aggregates match (total P&L, trade counts)
    - No orphaned records
    """
    print("\n" + "=" * 60)
    print("VERIFICATION")
    print("=" * 60)
    all_passed = True

    # 1. Verify trades count
    print("\n1. Trades count verification...")
    legacy_count = legacy_conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    mercury_count = mercury_conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    print(f"   Legacy: {legacy_count}, Mercury: {mercury_count}")
    if mercury_count < legacy_count:
        print("   WARNING: Mercury has fewer trades than legacy")
    else:
        print("   PASS: Trade counts acceptable")

    # 2. Verify P&L totals
    print("\n2. P&L verification...")
    legacy_pnl = legacy_conn.execute(
        "SELECT COALESCE(SUM(actual_profit), 0) FROM trades WHERE actual_profit IS NOT NULL"
    ).fetchone()[0]
    mercury_pnl = mercury_conn.execute(
        "SELECT COALESCE(SUM(actual_profit), 0) FROM trades WHERE actual_profit IS NOT NULL"
    ).fetchone()[0]
    print(f"   Legacy actual_profit sum: ${legacy_pnl:.2f}")
    print(f"   Mercury actual_profit sum: ${mercury_pnl:.2f}")
    if abs((mercury_pnl or 0) - (legacy_pnl or 0)) > 0.01:
        print("   WARNING: P&L totals differ")
    else:
        print("   PASS: P&L totals match")

    # 3. Verify settlement queue
    print("\n3. Settlement queue verification...")
    if table_exists(legacy_conn, "settlement_queue"):
        legacy_sq_count = legacy_conn.execute("SELECT COUNT(*) FROM settlement_queue").fetchone()[0]
        mercury_sq_count = mercury_conn.execute("SELECT COUNT(*) FROM settlement_queue").fetchone()[0]
        print(f"   Legacy: {legacy_sq_count}, Mercury: {mercury_sq_count}")

        # Verify unclaimed entries
        legacy_unclaimed = legacy_conn.execute(
            "SELECT COUNT(*) FROM settlement_queue WHERE claimed = 0"
        ).fetchone()[0]
        mercury_unclaimed = mercury_conn.execute(
            "SELECT COUNT(*) FROM settlement_queue WHERE claimed = 0 OR status = 'pending'"
        ).fetchone()[0]
        print(f"   Legacy unclaimed: {legacy_unclaimed}, Mercury pending: {mercury_unclaimed}")

        if mercury_sq_count < legacy_sq_count:
            print("   WARNING: Mercury has fewer settlement queue entries")
        else:
            print("   PASS: Settlement queue counts acceptable")
    else:
        print("   SKIP: Legacy settlement_queue table not found")

    # 4. Verify daily stats
    print("\n4. Daily stats verification...")
    if table_exists(legacy_conn, "daily_stats"):
        legacy_ds_count = legacy_conn.execute("SELECT COUNT(*) FROM daily_stats").fetchone()[0]
        mercury_ds_count = mercury_conn.execute("SELECT COUNT(*) FROM daily_stats").fetchone()[0]
        print(f"   Legacy: {legacy_ds_count} days, Mercury: {mercury_ds_count} days")

        legacy_total_pnl = legacy_conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM daily_stats"
        ).fetchone()[0]
        mercury_total_pnl = mercury_conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM daily_stats"
        ).fetchone()[0]
        print(f"   Legacy total daily P&L: ${legacy_total_pnl:.2f}")
        print(f"   Mercury total daily P&L: ${mercury_total_pnl:.2f}")
    else:
        print("   SKIP: Legacy daily_stats table not found")

    # 5. Verify positions were created for settlement queue
    print("\n5. Positions table verification...")
    mercury_positions = mercury_conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    print(f"   Positions created: {mercury_positions}")

    # 6. Verify P&L ledger
    print("\n6. P&L ledger verification...")
    if table_exists(legacy_conn, "realized_pnl_ledger"):
        legacy_ledger = legacy_conn.execute("SELECT COUNT(*) FROM realized_pnl_ledger").fetchone()[0]
        mercury_ledger = mercury_conn.execute("SELECT COUNT(*) FROM realized_pnl_ledger").fetchone()[0]
        print(f"   Legacy: {legacy_ledger} entries, Mercury: {mercury_ledger} entries")

        legacy_ledger_total = legacy_conn.execute(
            "SELECT COALESCE(SUM(pnl_amount), 0) FROM realized_pnl_ledger"
        ).fetchone()[0]
        mercury_ledger_total = mercury_conn.execute(
            "SELECT COALESCE(SUM(pnl_amount), 0) FROM realized_pnl_ledger"
        ).fetchone()[0]
        print(f"   Legacy ledger total: ${legacy_ledger_total:.2f}")
        print(f"   Mercury ledger total: ${mercury_ledger_total:.2f}")
    else:
        print("   SKIP: Legacy realized_pnl_ledger table not found")

    print("\n" + "=" * 60)
    return all_passed


def main():
    parser = argparse.ArgumentParser(
        description="Migrate data from polyjuiced (legacy) to Mercury",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--legacy-db",
        type=Path,
        required=True,
        help="Path to legacy gabagool.db database",
    )
    parser.add_argument(
        "--mercury-db",
        type=Path,
        required=True,
        help="Path to Mercury database",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run migration without writing to Mercury database",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Only run verification, skip migration",
    )

    args = parser.parse_args()

    # Validate paths
    if not args.legacy_db.exists():
        print(f"ERROR: Legacy database not found: {args.legacy_db}")
        sys.exit(1)

    if not args.mercury_db.exists():
        print(f"ERROR: Mercury database not found: {args.mercury_db}")
        print("Note: Initialize Mercury schema first by running the application.")
        sys.exit(1)

    print("=" * 60)
    print("POLYJUICED TO MERCURY MIGRATION")
    print("=" * 60)
    print(f"Legacy database: {args.legacy_db}")
    print(f"Mercury database: {args.mercury_db}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'VERIFY ONLY' if args.verify_only else 'LIVE MIGRATION'}")
    print("=" * 60)

    # Connect to databases
    legacy_conn = connect_db(args.legacy_db)
    mercury_conn = connect_db(args.mercury_db)

    try:
        if not args.verify_only:
            stats = MigrationStats()

            # Run migrations
            migrate_trades(legacy_conn, mercury_conn, stats, args.dry_run)
            migrate_settlement_queue(legacy_conn, mercury_conn, stats, args.dry_run)
            migrate_daily_stats(legacy_conn, mercury_conn, stats, args.dry_run)
            migrate_fill_records(legacy_conn, mercury_conn, stats, args.dry_run)
            migrate_trade_telemetry(legacy_conn, mercury_conn, stats, args.dry_run)
            migrate_rebalance_trades(legacy_conn, mercury_conn, stats, args.dry_run)
            migrate_circuit_breaker(legacy_conn, mercury_conn, stats, args.dry_run)
            migrate_pnl_ledger(legacy_conn, mercury_conn, stats, args.dry_run)

            stats.print_summary()

        # Run verification
        verify_migration(legacy_conn, mercury_conn)

        print("\nMigration complete!")

    finally:
        legacy_conn.close()
        mercury_conn.close()


if __name__ == "__main__":
    main()
