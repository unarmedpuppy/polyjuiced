#!/usr/bin/env python3
"""Redeem winning positions via direct CTF contract call.

This script redeems resolved Polymarket positions by calling redeemPositions()
on the Conditional Tokens Framework contract. This is the proper way to claim
winnings - much more reliable than trying to sell at 0.99.

Usage:
    # Dry run - show what would be redeemed
    python scripts/redeem_positions.py --dry-run
    
    # Redeem one position (test)
    python scripts/redeem_positions.py --limit 1
    
    # Redeem all unclaimed positions
    python scripts/redeem_positions.py
"""

import argparse
import asyncio
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import PolymarketSettings
from src.client.polymarket import PolymarketClient


def get_unclaimed_positions(db_path: str) -> list:
    """Get unique condition IDs for unclaimed positions."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    cursor = conn.execute("""
        SELECT 
            condition_id,
            asset,
            SUM(shares) as total_shares,
            SUM(entry_cost) as total_cost,
            MIN(market_end_time) as market_end_time
        FROM settlement_queue 
        WHERE claimed = 0
        GROUP BY condition_id
        ORDER BY market_end_time ASC
    """)
    
    positions = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return positions


def mark_as_redeemed(db_path: str, condition_id: str, tx_hash: str):
    """Mark positions as redeemed in the database."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        UPDATE settlement_queue 
        SET claimed = 1, 
            claimed_at = CURRENT_TIMESTAMP,
            claim_proceeds = entry_cost,
            claim_profit = 0
        WHERE condition_id = ? AND claimed = 0
    """, (condition_id,))
    conn.commit()
    conn.close()


async def main():
    parser = argparse.ArgumentParser(description="Redeem Polymarket positions via CTF contract")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be redeemed")
    parser.add_argument("--limit", type=int, default=None, help="Max positions to redeem")
    parser.add_argument("--db", default="/app/data/gabagool.db", help="Database path")
    args = parser.parse_args()
    
    db_path = args.db
    if not os.path.exists(db_path):
        db_path = "data/gabagool.db"
    if not os.path.exists(db_path):
        print(f"Database not found at {args.db} or data/gabagool.db")
        sys.exit(1)
    
    positions = get_unclaimed_positions(db_path)
    
    if not positions:
        print("No unclaimed positions found.")
        return
    
    print(f"\n{'='*60}")
    print(f"POLYMARKET POSITION REDEMPTION")
    print(f"{'='*60}\n")
    
    total_cost = sum(p['total_cost'] for p in positions)
    print(f"Found {len(positions)} unique markets with unclaimed positions")
    print(f"Total entry cost: ${total_cost:.2f}\n")
    
    if args.limit:
        positions = positions[:args.limit]
        print(f"Limited to {args.limit} position(s)\n")
    
    print("Positions to redeem:")
    print("-" * 60)
    for i, pos in enumerate(positions, 1):
        print(f"  {i}. {pos['asset']} | {pos['total_shares']:.2f} shares | ${pos['total_cost']:.2f}")
        print(f"     condition: {pos['condition_id'][:40]}...")
    print()
    
    if args.dry_run:
        print("DRY RUN - No actual redemptions made")
        print("Run without --dry-run to actually redeem")
        return
    
    settings = PolymarketSettings()
    if not settings.private_key:
        print("ERROR: POLYMARKET_PRIVATE_KEY not set")
        sys.exit(1)
    
    client = PolymarketClient(settings)
    
    print("Starting redemptions...")
    print("-" * 60)
    
    success_count = 0
    fail_count = 0
    
    for pos in positions:
        condition_id = pos['condition_id']
        print(f"\nRedeeming {pos['asset']} (${pos['total_cost']:.2f})...")
        
        result = await client.redeem_positions_direct(condition_id)
        
        if result['success']:
            print(f"  ✅ Success! TX: {result['tx_hash']}")
            print(f"     Gas used: {result.get('gas_used', 'N/A')}")
            mark_as_redeemed(db_path, condition_id, result['tx_hash'])
            success_count += 1
        else:
            print(f"  ❌ Failed: {result.get('error', 'Unknown error')}")
            fail_count += 1
        
        await asyncio.sleep(1)
    
    print(f"\n{'='*60}")
    print(f"REDEMPTION COMPLETE")
    print(f"  Success: {success_count}")
    print(f"  Failed:  {fail_count}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(main())
