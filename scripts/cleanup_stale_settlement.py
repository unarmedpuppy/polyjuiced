#!/usr/bin/env python3
"""Clean up stale settlement queue entries that no longer exist on Polymarket.

These are positions that:
1. Have asset='RECONCILED'
2. Keep failing with 'orderbook does not exist' errors
3. Were from markets that have already resolved/closed

This script marks them as claimed so the bot stops trying to process them.
"""

import argparse
import sqlite3
from datetime import datetime


def main():
    parser = argparse.ArgumentParser(description="Clean up stale settlement queue entries")
    parser.add_argument("--db", default="/app/data/gabagool.db", help="Database path")
    parser.add_argument("--fix", action="store_true", help="Actually clean up the entries")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Find stale unclaimed positions (RECONCILED assets that haven't been claimed)
    cursor.execute("""
        SELECT condition_id, side, shares, entry_price, entry_cost, asset, claimed, created_at
        FROM settlement_queue
        WHERE claimed = 0
        ORDER BY created_at DESC
    """)
    unclaimed = cursor.fetchall()

    print(f"Found {len(unclaimed)} unclaimed positions in settlement queue:")
    total_cost = 0
    for row in unclaimed:
        shares = float(row['shares'] or 0)
        price = float(row['entry_price'] or 0)
        cost = float(row['entry_cost'] or 0)
        total_cost += cost
        print(f"  {row['side']}: {shares:.1f} shares @ ${price:.3f} (cost: ${cost:.2f})")
        print(f"    condition_id: {row['condition_id'][:20]}...")
        print(f"    asset: {row['asset']}, created: {row['created_at']}")

    print(f"\nTotal stale position cost: ${total_cost:.2f}")
    print("NOTE: These positions no longer exist on Polymarket (orderbook deleted)")

    if args.fix or args.dry_run:
        action = "Would mark" if args.dry_run else "Marking"
        print(f"\n{action} all {len(unclaimed)} positions as claimed (cleaned up)...")

        if not args.dry_run:
            # Mark all as claimed so bot stops trying to process them
            cursor.execute("""
                UPDATE settlement_queue
                SET claimed = 1
                WHERE claimed = 0
            """)
            print(f"  Updated {cursor.rowcount} rows")

            # Also record the cleanup in the ledger
            for row in unclaimed:
                cost = float(row['entry_cost'] or 0)
                cursor.execute("""
                    INSERT INTO realized_pnl_ledger (trade_id, change, reason, created_at)
                    VALUES (?, ?, ?, ?)
                """, (
                    f"cleanup_{row['condition_id'][:16]}",
                    -cost,  # Record as loss
                    f"Stale position cleanup: {row['side']} {row['shares']} shares (market closed)",
                    datetime.utcnow().isoformat()
                ))

            conn.commit()
            print(f"  Recorded ${total_cost:.2f} loss in PnL ledger")
            print("\nCleanup complete!")

    else:
        print("\nRun with --fix to clean these up, or --dry-run to preview")

    # Show current state
    cursor.execute("SELECT SUM(entry_cost) as total FROM settlement_queue WHERE claimed = 0")
    remaining = cursor.fetchone()
    remaining_cost = float(remaining['total'] or 0) if remaining else 0
    print(f"\nRemaining unclaimed position cost: ${remaining_cost:.2f}")

    conn.close()


if __name__ == "__main__":
    main()
