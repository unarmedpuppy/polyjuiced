#!/usr/bin/env python3
"""Analyze settlement_queue for historical P&L reconstruction."""
import sqlite3

conn = sqlite3.connect('/app/data/gabagool.db')
conn.row_factory = sqlite3.Row

print("=" * 80)
print("SETTLEMENT QUEUE ANALYSIS")
print("=" * 80)

# Get all settlement queue entries
cur = conn.execute('''
    SELECT *
    FROM settlement_queue
    ORDER BY created_at ASC
''')
rows = cur.fetchall()

print(f"\nTotal entries: {len(rows)}")
print(f"\nSample entries:")
print("-" * 80)

total_entry_cost = 0
total_claim_proceeds = 0
total_claim_profit = 0

for i, r in enumerate(rows[:20]):
    entry_cost = r['entry_cost'] or 0
    claim_proceeds = r['claim_proceeds'] or 0
    claim_profit = r['claim_profit'] or 0
    total_entry_cost += entry_cost
    total_claim_proceeds += claim_proceeds
    total_claim_profit += claim_profit

    print(f"{i+1:3}. {r['side']:3} | shares: {r['shares'] or 0:8.2f} | "
          f"entry: ${entry_cost:6.2f} | proceeds: ${claim_proceeds:6.2f} | "
          f"profit: ${claim_profit:6.2f} | claimed: {r['claimed']}")

if len(rows) > 20:
    print(f"... and {len(rows) - 20} more entries")
    # Sum the rest
    for r in rows[20:]:
        total_entry_cost += r['entry_cost'] or 0
        total_claim_proceeds += r['claim_proceeds'] or 0
        total_claim_profit += r['claim_profit'] or 0

print("-" * 80)
print(f"TOTALS:")
print(f"  Total entry cost: ${total_entry_cost:.2f}")
print(f"  Total claim proceeds: ${total_claim_proceeds:.2f}")
print(f"  Total claim profit: ${total_claim_profit:.2f}")

# Check for patterns in the data - group by claimed status
print(f"\n\nGROUPED BY CLAIMED STATUS:")
cur = conn.execute('''
    SELECT
        claimed,
        COUNT(*) as cnt,
        SUM(entry_cost) as total_entry,
        SUM(claim_proceeds) as total_proceeds,
        SUM(claim_profit) as total_profit
    FROM settlement_queue
    GROUP BY claimed
''')
for r in cur.fetchall():
    print(f"  claimed={r['claimed']}: {r['cnt']} entries | "
          f"entry: ${r['total_entry'] or 0:.2f} | "
          f"proceeds: ${r['total_proceeds'] or 0:.2f} | "
          f"profit: ${r['total_profit'] or 0:.2f}")

# Show schema
print(f"\n\nSETTLEMENT_QUEUE SCHEMA:")
cur = conn.execute("PRAGMA table_info(settlement_queue)")
for col in cur.fetchall():
    print(f"  {col[1]:20} {col[2]:10} {'NOT NULL' if col[3] else ''}")

# Look at unique trade_ids to understand the relationship
print(f"\n\nUNIQUE TRADE IDS: {len(set(r['trade_id'] for r in rows))}")

conn.close()
