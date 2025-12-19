#!/usr/bin/env python3
"""Check full portfolio value."""
import sqlite3

conn = sqlite3.connect("/app/data/gabagool.db")
c = conn.cursor()

print("=== SETTLEMENT QUEUE (open positions) ===")
c.execute("SELECT condition_id, side, shares, entry_price, entry_cost FROM settlement_queue WHERE claimed = 0")
total_cost = 0
for r in c.fetchall():
    cid, side, shares, price, cost = r
    shares = float(shares or 0)
    price = float(price or 0)
    cost = float(cost or 0)
    print(f"{side}: {shares:.1f} shares @ ${price:.3f} (cost: ${cost:.2f})")
    total_cost += cost

print(f"\nTotal position cost: ${total_cost:.2f}")
print(f"Liquid USDC: ~$1.29")
print(f"Estimated total: ${total_cost + 1.29:.2f}")

print("\n=== RECENT REAL TRADES (last 10) ===")
c.execute("""
    SELECT created_at, market_slug, execution_status, yes_shares, no_shares, yes_cost, no_cost, actual_profit
    FROM trades
    WHERE dry_run = 0
    ORDER BY created_at DESC
    LIMIT 10
""")
for r in c.fetchall():
    created, market, exec_status, yes_s, no_s, yes_c, no_c, profit = r
    market = (market or "unknown")[:30]
    yes_s = float(yes_s or 0)
    no_s = float(no_s or 0)
    yes_c = float(yes_c or 0)
    no_c = float(no_c or 0)
    total = yes_c + no_c
    print(f"{created}: {market}...")
    print(f"  {exec_status}: YES {yes_s:.1f}/${yes_c:.2f}, NO {no_s:.1f}/${no_c:.2f} = ${total:.2f}")
    if profit is not None:
        print(f"  Profit: ${profit:.2f}")

conn.close()
