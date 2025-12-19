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

# Check what statuses count as wins/losses
cursor.execute('''
    SELECT
        SUM(CASE WHEN status = 'win' THEN 1 ELSE 0 END) as wins,
        SUM(CASE WHEN status = 'loss' THEN 1 ELSE 0 END) as losses
    FROM trades WHERE dry_run = 0
''')
r = cursor.fetchone()
print(f"Trades with status='win': {r['wins']}")
print(f"Trades with status='loss': {r['losses']}")

print()
print("NOTE: The dashboard counts trades with status='win' or 'loss'")
print("If trades have other statuses like 'resolved', they won't count as wins/losses")

conn.close()
