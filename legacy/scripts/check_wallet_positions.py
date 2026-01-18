#!/usr/bin/env python3
"""Check wallet positions and calculate portfolio value."""
import os
import sys
sys.path.insert(0, '/app/src')

def main():
    from client.polymarket import PolymarketClient
    from config import AppConfig

    print("=" * 60)
    print("WALLET POSITIONS CHECK")
    print("=" * 60)

    # Load config and create client
    config = AppConfig.load()
    client = PolymarketClient(config.polymarket)

    # Get USDC balance
    try:
        balance_info = client.get_balance()
        usdc_balance = balance_info.get("balance", 0.0)
        print(f"\nUSDC Balance (cash): ${usdc_balance:.2f}")
    except Exception as e:
        print(f"Error getting balance: {e}")
        usdc_balance = 0.0

    # Get open positions (shares held)
    try:
        # The py-clob-client may have a method to get positions
        # Check what methods are available
        print(f"\nClient methods: {[m for m in dir(client) if not m.startswith('_')]}")

        # Try to get positions/shares
        if hasattr(client, 'get_positions'):
            positions = client.get_positions()
            print(f"\nPositions: {positions}")
    except Exception as e:
        print(f"Error getting positions: {e}")

    print("\n" + "=" * 60)
    print("EXPLANATION")
    print("=" * 60)
    print("""
The discrepancy is likely:
- $113.75 = Cash USDC balance (available to trade)
- $171.51 = Total portfolio value (from Polymarket UI)
- Difference = $57.76 = Value of held shares

This means you have open positions worth ~$57.76 that
are NOT tracked in our database (they might be:
1. Old manual trades
2. Historical imports that resolved but shares weren't sold
3. Positions from other strategies)
""")

if __name__ == '__main__':
    main()
