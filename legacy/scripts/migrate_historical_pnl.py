#!/usr/bin/env python3
"""One-time migration: Add historical P&L to realized_pnl_ledger.

This script adds a single entry to realized_pnl_ledger representing the
total P&L from historical trades that were imported from Polymarket.

These trades exist in settlement_queue (53 entries, $346.70 total cost)
but were marked as claimed without recording actual P&L.

The known total P&L from these historical trades is $71.51.

Run this script ONCE to establish the historical baseline.
"""
import sqlite3
from datetime import datetime

DB_PATH = '/app/data/gabagool.db'
HISTORICAL_PNL = 71.51
TRADE_DATE = '2025-12-14'  # Use the date the import was done

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Check if we already have a historical_import entry
    cur = conn.execute('''
        SELECT COUNT(*) as cnt FROM realized_pnl_ledger
        WHERE pnl_type = 'historical_import'
    ''')
    existing = cur.fetchone()['cnt']

    if existing > 0:
        print(f"ERROR: Already have {existing} historical_import entries in realized_pnl_ledger")
        print("This migration should only run once. Aborting.")
        conn.close()
        return

    # Insert the historical P&L entry
    conn.execute('''
        INSERT INTO realized_pnl_ledger (
            trade_id,
            trade_date,
            pnl_amount,
            pnl_type,
            notes,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
    ''', (
        'historical-import-2025-12-14',
        TRADE_DATE,
        HISTORICAL_PNL,
        'historical_import',
        f'Manually imported from Polymarket historical trades. '
        f'53 positions with $346.70 total entry cost. '
        f'Actual P&L confirmed as ${HISTORICAL_PNL:.2f}.',
        datetime.utcnow().isoformat(),
    ))
    conn.commit()

    print(f"SUCCESS: Added historical P&L entry")
    print(f"  - Amount: ${HISTORICAL_PNL:.2f}")
    print(f"  - Type: historical_import")
    print(f"  - Date: {TRADE_DATE}")

    # Verify
    cur = conn.execute('SELECT SUM(pnl_amount) as total FROM realized_pnl_ledger')
    total = cur.fetchone()['total']
    print(f"\nTotal P&L in realized_pnl_ledger: ${total:.2f}")

    conn.close()

if __name__ == '__main__':
    main()
