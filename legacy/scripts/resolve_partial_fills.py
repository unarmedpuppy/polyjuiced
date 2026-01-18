#!/usr/bin/env python3
"""Resolve partial fill trades by calculating actual P&L.

For one-sided trades (one_leg_only), the outcome depends on what happened:
- If we held YES and market went UP, we won (shares * $1)
- If we held YES and market went DOWN, we lost (cost)
- If we held NO and market went DOWN, we won (shares * $1)
- If we held NO and market went UP, we lost (cost)

Since these markets have already resolved and positions were claimed via wallet,
we mark them as losses equal to their cost (worst case assumption).
"""

import argparse
import sqlite3


def main():
    parser = argparse.ArgumentParser(description="Resolve partial fill trades with P&L")
    parser.add_argument("--db", default="/app/data/gabagool.db", help="Database path")
    parser.add_argument("--fix", action="store_true", help="Actually resolve the trades")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Get all needs_review trades
    cursor.execute("""
        SELECT id, market_slug, yes_shares, no_shares, yes_cost, no_cost,
               execution_status, created_at
        FROM trades
        WHERE status = 'needs_review'
        ORDER BY created_at DESC
    """)

    trades = cursor.fetchall()
    print(f"Found {len(trades)} trades needing resolution\n")

    total_loss = 0.0
    for t in trades:
        yes_shares = float(t['yes_shares'] or 0)
        no_shares = float(t['no_shares'] or 0)
        yes_cost = float(t['yes_cost'] or 0)
        no_cost = float(t['no_cost'] or 0)
        total_cost = yes_cost + no_cost

        # Determine position type
        if yes_shares > 0 and no_shares == 0:
            position = f"YES only: {yes_shares:.1f} shares"
        elif no_shares > 0 and yes_shares == 0:
            position = f"NO only: {no_shares:.1f} shares"
        else:
            position = f"Mixed: YES {yes_shares:.1f}, NO {no_shares:.1f}"

        # For unhedged positions, assume worst case (total loss)
        # In reality some may have won, but without market resolution data
        # we mark as loss. User can manually correct if needed.
        loss = total_cost

        print(f"ID {t['id']}: {t['market_slug'][:35]}...")
        print(f"  {position} | Cost: ${total_cost:.2f} | Loss: -${loss:.2f}")

        total_loss += loss

    print(f"\n{'=' * 60}")
    print(f"TOTAL LOSS (worst case): -${total_loss:.2f}")
    print(f"{'=' * 60}")

    if args.fix:
        print("\nResolving trades as losses...")

        # Update each trade with its actual_profit = -cost
        cursor.execute("""
            UPDATE trades
            SET status = 'resolved',
                actual_profit = -(COALESCE(yes_cost, 0) + COALESCE(no_cost, 0))
            WHERE status = 'needs_review'
        """)

        print(f"Updated {cursor.rowcount} trades")
        conn.commit()

        # Show new status
        cursor.execute("SELECT status, COUNT(*) as count FROM trades GROUP BY status ORDER BY count DESC")
        print("\nNew trade statuses:")
        for r in cursor.fetchall():
            print(f"  {r['status']}: {r['count']}")

        # Show total P&L
        cursor.execute("SELECT SUM(actual_profit) as total FROM trades WHERE actual_profit IS NOT NULL")
        result = cursor.fetchone()
        if result and result['total']:
            print(f"\nTotal recorded P&L: ${result['total']:.2f}")
    else:
        print("\nRun with --fix to mark these as resolved losses")

    conn.close()


if __name__ == "__main__":
    main()
