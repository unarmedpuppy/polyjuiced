#!/usr/bin/env python3
"""One-time script to resolve old pending trades after container restart."""

import sqlite3
from datetime import datetime

DB_PATH = "/app/data/gabagool.db"

def main():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Get all pending trades
    c.execute("SELECT id, expected_profit FROM trades WHERE status = 'pending'")
    pending = c.fetchall()

    if not pending:
        print("No pending trades to resolve")
        return

    total_profit = 0
    for trade_id, expected_profit in pending:
        # Arbitrage is deterministic - expected_profit IS actual_profit
        actual_profit = expected_profit
        won = actual_profit > 0
        status = "win" if won else "loss"
        total_profit += actual_profit

        c.execute(
            "UPDATE trades SET status = ?, actual_profit = ?, resolved_at = ? WHERE id = ?",
            (status, actual_profit, datetime.utcnow().isoformat(), trade_id)
        )
        print(f"Resolved {trade_id}: {status} ${actual_profit:.2f}")

    conn.commit()
    conn.close()

    print(f"\nTotal resolved: {len(pending)} trades")
    print(f"Total profit: ${total_profit:.2f}")

if __name__ == "__main__":
    main()
