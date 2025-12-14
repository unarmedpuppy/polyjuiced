#!/usr/bin/env python3
"""Check settlement status of trades."""

import sqlite3
import sys
from datetime import datetime

def main():
    conn = sqlite3.connect("/app/data/trades.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Check for unresolved trades
    c.execute("""
        SELECT trade_id, asset, condition_id, yes_shares, no_shares,
               execution_status, resolved_at, created_at
        FROM trades
        WHERE resolved_at IS NULL
        ORDER BY created_at DESC
        LIMIT 20
    """)

    rows = c.fetchall()
    print(f"=== Unresolved Trades ({len(rows)} found) ===")
    for row in rows:
        d = dict(row)
        yes = float(d["yes_shares"] or 0)
        no = float(d["no_shares"] or 0)
        print(f"  {d['trade_id'][:8]}... {d['asset']:4} yes={yes:7.2f} no={no:7.2f} status={d['execution_status']}")
        print(f"    condition: {d['condition_id'][:16]}...")
        print(f"    created: {d['created_at']}")

    # Check recently resolved
    c.execute("""
        SELECT trade_id, asset, yes_shares, no_shares, resolved_at
        FROM trades
        WHERE resolved_at IS NOT NULL
        ORDER BY resolved_at DESC
        LIMIT 5
    """)

    rows = c.fetchall()
    print(f"\n=== Recently Resolved ({len(rows)} shown) ===")
    for row in rows:
        d = dict(row)
        yes = float(d["yes_shares"] or 0)
        no = float(d["no_shares"] or 0)
        print(f"  {d['trade_id'][:8]}... {d['asset']:4} yes={yes:7.2f} no={no:7.2f} resolved={d['resolved_at']}")

    # Count totals
    c.execute("SELECT COUNT(*) FROM trades WHERE resolved_at IS NULL")
    unresolved = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM trades WHERE resolved_at IS NOT NULL")
    resolved = c.fetchone()[0]

    print(f"\n=== Summary ===")
    print(f"  Total unresolved: {unresolved}")
    print(f"  Total resolved: {resolved}")

    conn.close()

if __name__ == "__main__":
    main()
