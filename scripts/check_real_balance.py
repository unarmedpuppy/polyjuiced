#!/usr/bin/env python3
"""Check ACTUAL balance from Polymarket API."""
import asyncio
import os
import sys

# Add src to path
sys.path.insert(0, '/app')

from src.client.polymarket import PolymarketClient
from src.config import PolymarketSettings


async def main():
    print("=== POLYMARKET API BALANCE ===")
    settings = PolymarketSettings()
    client = PolymarketClient(settings)
    await client.connect()

    # Get USDC balance
    balance = await client.get_balance()
    print(f"\nUSDC Balance: ${balance:.2f}")

    # Get open orders
    orders = await client.get_open_orders()
    print(f"\nOpen Orders: {len(orders)}")
    for order in orders[:10]:  # Show first 10
        print(f"  {order.get('side', 'N/A')}: {order.get('original_size', 'N/A')} @ ${order.get('price', 'N/A')}")

    await client.close()
    print("\nDone!")


if __name__ == "__main__":
    asyncio.run(main())
