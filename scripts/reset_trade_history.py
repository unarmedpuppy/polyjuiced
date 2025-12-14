#!/usr/bin/env python3
"""Reset all trade history data in the database.

This script clears:
- trades table
- daily_stats table
- logs table
- fill_records table
- liquidity_snapshots table

It preserves:
- markets table (market discovery data)

Usage:
    python scripts/reset_trade_history.py

Or from within the Docker container:
    docker exec -it polymarket-bot python scripts/reset_trade_history.py
"""

import asyncio
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from persistence import get_database, close_database


async def main():
    print("=" * 60)
    print("Polymarket Bot - Trade History Reset")
    print("=" * 60)
    print()
    print("This will DELETE all:")
    print("  - Trade records")
    print("  - Daily statistics")
    print("  - Log entries")
    print("  - Fill records")
    print("  - Liquidity snapshots")
    print()
    print("Markets discovery data will be PRESERVED.")
    print()

    # Confirm
    confirm = input("Type 'RESET' to confirm: ")
    if confirm != "RESET":
        print("Cancelled.")
        return

    print()
    print("Connecting to database...")
    db = await get_database()

    print("Resetting trade data...")
    deleted = await db.reset_all_trade_data()

    print()
    print("Reset complete!")
    print("-" * 40)
    for table, count in deleted.items():
        print(f"  {table}: {count} records deleted")
    print("-" * 40)
    print()

    await close_database()
    print("Done. Dashboard will now show clean slate.")


if __name__ == "__main__":
    asyncio.run(main())
