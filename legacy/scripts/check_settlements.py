#!/usr/bin/env python3
"""Check settlement status of trades."""

import sqlite3
import sys
from datetime import datetime
import os

def main():
    # Try multiple database paths
    db_paths = [
        "/app/data/gabagool.db",
        "/app/data/trades.db",
        "/app/data/polymarket.db",
    ]

    conn = None
    for path in db_paths:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            try:
                conn = sqlite3.connect(path)
                conn.row_factory = sqlite3.Row
                c = conn.cursor()
                c.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = [r[0] for r in c.fetchall()]
                print(f"Using database: {path}")
                print(f"Tables: {tables}")
                if "trades" in tables or "gabagool_trades" in tables:
                    break
                conn.close()
                conn = None
            except Exception as e:
                print(f"Error with {path}: {e}")
                if conn:
                    conn.close()
                conn = None

    if not conn:
        print("No valid database found!")
        return

    c = conn.cursor()

    # Try to find the trades table name
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%trade%'")
    trade_tables = [r[0] for r in c.fetchall()]
    print(f"Trade-related tables: {trade_tables}")

    # Check schema of first trade table
    if trade_tables:
        table = trade_tables[0]
        c.execute(f"PRAGMA table_info({table})")
        columns = [r[1] for r in c.fetchall()]
        print(f"\nColumns in {table}: {columns}")

        # Get sample data
        c.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 5")
        rows = c.fetchall()
        print(f"\nRecent rows ({len(rows)} shown):")
        for row in rows:
            print(f"  {dict(row)}")

    # Check for tracked_positions or similar
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%position%'")
    position_tables = [r[0] for r in c.fetchall()]
    print(f"\nPosition-related tables: {position_tables}")

    # Check telemetry
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%telemetry%'")
    telemetry_tables = [r[0] for r in c.fetchall()]
    print(f"Telemetry-related tables: {telemetry_tables}")

    conn.close()

if __name__ == "__main__":
    main()
