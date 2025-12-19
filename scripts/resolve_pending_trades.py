#!/usr/bin/env python3
"""Resolve pending trades by marking them with appropriate status.

Pending trades with $0 values are artifacts from the partial fill tracking bug.
This script:
1. Shows the current state of pending trades
2. Marks them as 'resolved_stale' so they're no longer cluttering the dashboard
"""

import argparse
import sqlite3
from datetime import datetime


def main():
    parser = argparse.ArgumentParser(description="Resolve pending trades")
    parser.add_argument("--db", default="/app/data/gabagool.db", help="Database path")
    parser.add_argument("--fix", action="store_true", help="Actually resolve the trades")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Check current status counts
    cursor.execute("SELECT status, COUNT(*) as count FROM trades GROUP BY status ORDER BY count DESC")
    print("Current trade statuses:")
    for r in cursor.fetchall():
        print(f"  {r['status']}: {r['count']}")

    # Get pending trades
    cursor.execute("SELECT * FROM trades WHERE status = 'pending' ORDER BY created_at DESC")
    pending = cursor.fetchall()

    print(f"\nPending trades: {len(pending)}")

    if not pending:
        print("No pending trades to resolve!")
        conn.close()
        return

    # Analyze pending trades
    zero_value = []
    full_fills = []
    partial_fills = []

    for t in pending:
        yes_cost = float(t['yes_cost'] or 0)
        no_cost = float(t['no_cost'] or 0)
        yes_shares = float(t['yes_shares'] or 0)
        no_shares = float(t['no_shares'] or 0)
        exec_status = t['execution_status'] or ''

        if yes_cost == 0 and no_cost == 0 and yes_shares == 0 and no_shares == 0:
            zero_value.append(t)
        elif exec_status == 'full_fill':
            full_fills.append(t)
        else:
            partial_fills.append(t)

    print(f"\n  Zero-value (bug artifacts): {len(zero_value)}")
    print(f"  Full fills (complete trades): {len(full_fills)}")
    print(f"  Partial fills (need review): {len(partial_fills)}")

    # Show samples
    if partial_fills:
        print("\n  Partial fills (showing first 5):")
        for t in partial_fills[:5]:
            market_name = t['market_slug'] or t['condition_id'] or 'unknown'
            print(f"    {market_name[:50]}...")
            print(f"      YES: {t['yes_shares']} shares @ ${t['yes_cost']}")
            print(f"      NO:  {t['no_shares']} shares @ ${t['no_cost']}")
            print(f"      exec_status: {t['execution_status']}")

    if args.fix or args.dry_run:
        action = "Would resolve" if args.dry_run else "Resolving"

        # Resolve zero-value trades as stale artifacts
        if zero_value:
            print(f"\n{action} {len(zero_value)} zero-value trades as 'resolved_stale'...")
            if not args.dry_run:
                cursor.execute("""
                    UPDATE trades
                    SET status = 'resolved_stale'
                    WHERE status = 'pending'
                    AND (yes_cost IS NULL OR yes_cost = 0)
                    AND (no_cost IS NULL OR no_cost = 0)
                    AND (yes_shares IS NULL OR yes_shares = 0)
                    AND (no_shares IS NULL OR no_shares = 0)
                """)
                print(f"  Updated {cursor.rowcount} rows")

        # Mark full fill trades as resolved
        if full_fills:
            print(f"\n{action} {len(full_fills)} full fill trades as 'resolved'...")
            if not args.dry_run:
                cursor.execute("""
                    UPDATE trades
                    SET status = 'resolved'
                    WHERE status = 'pending'
                    AND execution_status = 'full_fill'
                """)
                print(f"  Updated {cursor.rowcount} rows")

        # Mark partial fills for manual review
        if partial_fills:
            print(f"\n{action} {len(partial_fills)} partial fill trades as 'needs_review'...")
            if not args.dry_run:
                cursor.execute("""
                    UPDATE trades
                    SET status = 'needs_review'
                    WHERE status = 'pending'
                    AND execution_status != 'full_fill'
                    AND NOT (
                        (yes_cost IS NULL OR yes_cost = 0)
                        AND (no_cost IS NULL OR no_cost = 0)
                        AND (yes_shares IS NULL OR yes_shares = 0)
                        AND (no_shares IS NULL OR no_shares = 0)
                    )
                """)
                print(f"  Updated {cursor.rowcount} rows")

        if not args.dry_run:
            conn.commit()
            print("\nChanges committed!")

            # Show new status counts
            cursor.execute("SELECT status, COUNT(*) as count FROM trades GROUP BY status ORDER BY count DESC")
            print("\nNew trade statuses:")
            for r in cursor.fetchall():
                print(f"  {r['status']}: {r['count']}")
    else:
        print("\nRun with --fix to resolve these trades, or --dry-run to preview changes")

    conn.close()


if __name__ == "__main__":
    main()
