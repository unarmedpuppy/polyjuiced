#!/usr/bin/env python3
"""Check trade status counts in database."""
import sqlite3
from datetime import datetime

conn = sqlite3.connect('/app/data/gabagool.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

today = datetime.utcnow().strftime('%Y-%m-%d')
print(f'Today (UTC): {today}')
print()

# Check today's stats
cursor.execute('''
    SELECT status, COUNT(*) as count
    FROM trades
    WHERE date(created_at) = ? AND dry_run = 0
    GROUP BY status
''', (today,))
rows = cursor.fetchall()
print("Today's trades by status:")
if rows:
    for r in rows:
        print(f"  {r['status']}: {r['count']}")
else:
    print("  (none)")

print()

# Check all-time stats
cursor.execute('''
    SELECT status, COUNT(*) as count
    FROM trades
    WHERE dry_run = 0
    GROUP BY status
    ORDER BY count DESC
''')
rows = cursor.fetchall()
print("All-time trades by status:")
for r in rows:
    print(f"  {r['status']}: {r['count']}")

print()

# Check what statuses count as wins/losses (using NEW logic)
cursor.execute('''
    SELECT
        SUM(CASE
            WHEN status = 'win' THEN 1
            WHEN status = 'resolved' AND actual_profit > 0 THEN 1
            ELSE 0
        END) as wins,
        SUM(CASE
            WHEN status = 'loss' THEN 1
            WHEN status = 'resolved' AND actual_profit < 0 THEN 1
            ELSE 0
        END) as losses
    FROM trades WHERE dry_run = 0
''')
r = cursor.fetchone()
print(f"Wins (resolved w/ positive profit): {r['wins']}")
print(f"Losses (resolved w/ negative profit): {r['losses']}")

print()
print("NOTE: Wins/losses are counted from 'resolved' trades based on actual_profit")

print()
print("Today's trades details:")
cursor.execute('''
    SELECT id, market_slug, status, actual_profit, execution_status,
           yes_shares, no_shares, yes_cost, no_cost
    FROM trades
    WHERE date(created_at) = ? AND dry_run = 0
''', (today,))
rows = cursor.fetchall()
if rows:
    for r in rows:
        trade_id = (r['id'] or 'N/A')[:20]
        market = (r['market_slug'] or 'unknown')[:35]
        print(f"  {trade_id}...")
        print(f"    Market: {market}...")
        print(f"    Status: {r['status']}, Profit: {r['actual_profit']}, Exec: {r['execution_status']}")
        print(f"    YES: {r['yes_shares']} @ ${r['yes_cost']}, NO: {r['no_shares']} @ ${r['no_cost']}")
else:
    print("  (none)")

conn.close()
