#!/usr/bin/env python3
"""Check P&L data across all sources."""
import sqlite3

conn = sqlite3.connect('/app/data/gabagool.db')
conn.row_factory = sqlite3.Row

print("=" * 60)
print("P&L DATA SOURCE ANALYSIS")
print("=" * 60)

# Check trades table - resolved trades
cur = conn.execute('''
    SELECT COUNT(*) as cnt, SUM(actual_profit) as total
    FROM trades
    WHERE status IN ('win','loss') AND dry_run=0
''')
row = cur.fetchone()
print(f"\n1. TRADES TABLE (resolved, real):")
print(f"   Count: {row['cnt']}")
print(f"   Total P&L: ${row['total'] or 0:.2f}")

# Check all trades with null actual_profit
cur = conn.execute('''
    SELECT COUNT(*) as cnt
    FROM trades
    WHERE actual_profit IS NULL AND dry_run=0
''')
row = cur.fetchone()
print(f"   Trades with NULL actual_profit: {row['cnt']}")

# Check realized_pnl_ledger
cur = conn.execute('SELECT COUNT(*) as cnt, SUM(pnl_amount) as total FROM realized_pnl_ledger')
row = cur.fetchone()
print(f"\n2. REALIZED_PNL_LEDGER:")
print(f"   Count: {row['cnt']}")
print(f"   Total P&L: ${row['total'] or 0:.2f}")

# Breakdown by pnl_type
cur = conn.execute('''
    SELECT pnl_type, COUNT(*) as cnt, SUM(pnl_amount) as total
    FROM realized_pnl_ledger
    GROUP BY pnl_type
''')
for row in cur.fetchall():
    print(f"   - {row['pnl_type']}: {row['cnt']} entries, ${row['total'] or 0:.2f}")

# Check settlement_queue
cur = conn.execute('''
    SELECT COUNT(*) as cnt, SUM(claim_profit) as total
    FROM settlement_queue
    WHERE claimed=1
''')
row = cur.fetchone()
print(f"\n3. SETTLEMENT_QUEUE (claimed):")
print(f"   Count: {row['cnt']}")
print(f"   Total P&L: ${row['total'] or 0:.2f}")

# Check by strategy
cur = conn.execute('''
    SELECT strategy_id, COUNT(*) as cnt, SUM(actual_profit) as total
    FROM trades
    WHERE dry_run=0
    GROUP BY strategy_id
''')
print(f"\n4. TRADES BY STRATEGY:")
for row in cur.fetchall():
    print(f"   - {row['strategy_id'] or 'NULL'}: {row['cnt']} trades, P&L: ${row['total'] or 0:.2f}")

# Show trades with NULL actual_profit (these are likely the historical imports)
cur = conn.execute('''
    SELECT id, asset, created_at, yes_cost, no_cost, status, actual_profit, strategy_id
    FROM trades
    WHERE actual_profit IS NULL AND dry_run=0
    ORDER BY created_at DESC
    LIMIT 10
''')
rows = cur.fetchall()
print(f"\n5. SAMPLE TRADES WITH NULL P&L (likely historical imports):")
for r in rows:
    total_cost = (r['yes_cost'] or 0) + (r['no_cost'] or 0)
    print(f"   {r['id'][:8]}... | {r['asset'] or 'N/A':4} | {r['status']:8} | cost: ${total_cost:.2f} | strategy: {r['strategy_id']}")

# Count trades that might be historical imports (no actual_profit set)
cur = conn.execute('''
    SELECT COUNT(*) as cnt, SUM(yes_cost + no_cost) as total_cost
    FROM trades
    WHERE actual_profit IS NULL AND dry_run=0 AND status != 'pending'
''')
row = cur.fetchone()
print(f"\n6. HISTORICAL IMPORTS (NULL P&L, not pending):")
print(f"   Count: {row['cnt']}")
print(f"   Total Cost: ${row['total_cost'] or 0:.2f}")

# Check circuit breaker state
cur = conn.execute('SELECT * FROM circuit_breaker_state WHERE id=1')
row = cur.fetchone()
if row:
    print(f"\n7. CIRCUIT BREAKER STATE:")
    print(f"   Date: {row['date']}")
    print(f"   Realized P&L: ${row['realized_pnl'] or 0:.2f}")
    print(f"   Trades Today: {row['total_trades_today']}")

conn.close()
