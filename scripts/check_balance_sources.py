#!/usr/bin/env python3
"""Check all sources of balance/P&L data to debug discrepancies."""
import sqlite3

DB_PATH = '/app/data/gabagool.db'

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("=" * 60)
    print("BALANCE SOURCE ANALYSIS")
    print("=" * 60)

    # 1. realized_pnl_ledger (source of truth for P&L)
    cur = conn.execute('SELECT SUM(pnl_amount) as total FROM realized_pnl_ledger')
    row = cur.fetchone()
    ledger_total = row['total'] or 0
    print(f"\n1. realized_pnl_ledger total P&L: ${ledger_total:.2f}")

    # Breakdown by type
    cur = conn.execute('''
        SELECT pnl_type, COUNT(*) as cnt, SUM(pnl_amount) as total
        FROM realized_pnl_ledger GROUP BY pnl_type
    ''')
    for row in cur.fetchall():
        print(f"   - {row['pnl_type']}: {row['cnt']} entries, ${row['total'] or 0:.2f}")

    # 2. circuit_breaker_state
    cur = conn.execute('SELECT * FROM circuit_breaker_state WHERE id=1')
    row = cur.fetchone()
    if row:
        print(f"\n2. circuit_breaker_state:")
        print(f"   realized_pnl: ${row['realized_pnl'] or 0:.2f}")
        print(f"   date: {row['date']}")

    # 3. trades table
    cur = conn.execute('''
        SELECT
            SUM(CASE WHEN status IN ('win', 'loss') THEN actual_profit ELSE 0 END) as realized,
            SUM(CASE WHEN status = 'pending' THEN (yes_cost + no_cost) ELSE 0 END) as pending_cost
        FROM trades WHERE dry_run = 0
    ''')
    row = cur.fetchone()
    trades_realized = row['realized'] or 0
    pending_cost = row['pending_cost'] or 0
    print(f"\n3. trades table:")
    print(f"   realized P&L: ${trades_realized:.2f}")
    print(f"   pending positions cost: ${pending_cost:.2f}")

    # 4. settlement_queue
    cur = conn.execute('SELECT SUM(claim_profit) as total FROM settlement_queue WHERE claimed=1')
    row = cur.fetchone()
    settlement_profit = row['total'] or 0
    print(f"\n4. settlement_queue claimed profit: ${settlement_profit:.2f}")

    # 5. Dashboard calculation breakdown
    print("\n" + "=" * 60)
    print("DASHBOARD CALCULATION")
    print("=" * 60)

    # The dashboard likely calculates: starting_balance + realized_pnl + unrealized
    starting_balance = 100.0  # Assumed starting balance

    print(f"\nIf dashboard shows $171.51:")
    print(f"  Starting balance: ${starting_balance:.2f}")
    print(f"  Implied total P&L: ${171.51 - starting_balance:.2f}")

    print(f"\nActual from realized_pnl_ledger: ${ledger_total:.2f}")

    # Check if dashboard is double-counting
    print("\n" + "=" * 60)
    print("POSSIBLE ISSUES")
    print("=" * 60)

    # Check for positions that might be counted as unrealized value
    cur = conn.execute('''
        SELECT COUNT(*) as cnt, SUM(yes_cost + no_cost) as total_cost
        FROM trades
        WHERE dry_run = 0 AND status = 'pending'
    ''')
    row = cur.fetchone()
    pending_count = row['cnt'] or 0
    pending_total = row['total_cost'] or 0
    print(f"\nPending trades: {pending_count} trades, ${pending_total:.2f} total cost")

    # Check settlement queue unclaimed
    cur = conn.execute('''
        SELECT COUNT(*) as cnt, SUM(entry_cost) as total_cost
        FROM settlement_queue WHERE claimed = 0
    ''')
    row = cur.fetchone()
    unclaimed_count = row['cnt'] or 0
    unclaimed_cost = row['total_cost'] or 0
    print(f"Unclaimed settlements: {unclaimed_count} entries, ${unclaimed_cost:.2f} entry cost")

    # Show what CLOB balance represents
    print("\n" + "=" * 60)
    print("CLOB BALANCE INTERPRETATION")
    print("=" * 60)
    print("\nCLOB balance ($113.75) = Available USDC in wallet")
    print("This is cash not currently in positions.")
    print("\nDashboard balance ($171.51) likely includes:")
    print("  - USDC balance")
    print("  - Value of open positions (shares * current price)")
    print("  - Possibly unrealized P&L estimates")

    # Calculate what positions are worth
    cur = conn.execute('''
        SELECT
            id, asset, yes_cost, no_cost, yes_shares, no_shares, status
        FROM trades
        WHERE dry_run = 0 AND status = 'pending'
        ORDER BY created_at DESC
        LIMIT 10
    ''')
    rows = cur.fetchall()
    if rows:
        print(f"\nOpen positions (pending trades):")
        for r in rows:
            total_cost = (r['yes_cost'] or 0) + (r['no_cost'] or 0)
            print(f"  {r['id'][:8]}... | {r['asset'] or 'N/A'} | cost: ${total_cost:.2f}")

    conn.close()

if __name__ == '__main__':
    main()
