#!/usr/bin/env python3
"""Reset trade history data in the database.

By default, this clears:
- trades table (incorrect P&L data)
- daily_stats table
- logs table

It PRESERVES (by default):
- fill_records (valuable for slippage modeling)
- liquidity_snapshots (valuable for persistence modeling)
- markets table (market discovery data)

Usage:
    # Safe reset - preserves liquidity data
    python scripts/reset_trade_history.py

    # Full reset - deletes everything including liquidity data
    python scripts/reset_trade_history.py --all

Or from within the Docker container:
    docker exec -it polymarket-bot python scripts/reset_trade_history.py
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from persistence import get_database, close_database


async def main(delete_all: bool = False):
    print("=" * 60)
    print("Polymarket Bot - Trade History Reset")
    print("=" * 60)
    print()

    if delete_all:
        print("WARNING: Full reset mode - this will DELETE:")
        print("  - Trade records")
        print("  - Daily statistics")
        print("  - Log entries")
        print("  - Fill records (slippage modeling data)")
        print("  - Liquidity snapshots (persistence modeling data)")
    else:
        print("Safe reset mode - this will DELETE:")
        print("  - Trade records (incorrect P&L)")
        print("  - Daily statistics")
        print("  - Log entries")
        print()
        print("This will PRESERVE:")
        print("  - Fill records (valuable for slippage modeling)")
        print("  - Liquidity snapshots (valuable for persistence modeling)")

    print()
    print("Markets discovery data will always be PRESERVED.")
    print()

    # Confirm
    confirm_word = "DELETE-ALL" if delete_all else "RESET"
    confirm = input(f"Type '{confirm_word}' to confirm: ")
    if confirm != confirm_word:
        print("Cancelled.")
        return

    print()
    print("Connecting to database...")
    db = await get_database()

    print("Resetting trade data...")
    deleted = await db.reset_trade_history(preserve_liquidity_data=not delete_all)

    print()
    print("Reset complete!")
    print("-" * 40)
    for table, count in deleted.items():
        print(f"  {table}: {count} records deleted")
    print("-" * 40)

    if not delete_all:
        print()
        print("Liquidity modeling data was PRESERVED.")

    print()
    await close_database()
    print("Done. Dashboard will now show clean slate.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reset trade history data")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Delete ALL data including liquidity modeling data (fill_records, liquidity_snapshots)",
    )
    args = parser.parse_args()

    asyncio.run(main(delete_all=args.all))
