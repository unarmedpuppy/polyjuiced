#!/usr/bin/env python3
"""Test script to execute a real $1 trade on an active 15-minute market.

This script:
1. Finds an active 15-minute BTC or ETH market
2. Buys $1 worth of the UP (YES) side
3. Reports the result

Run with: python3 scripts/test_real_trade.py
"""

import asyncio
import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.client.polymarket import PolymarketClient
from src.client.gamma import GammaClient
from src.config import load_config

async def main():
    print("=" * 60)
    print("TEST REAL TRADE - $1 on active 15-minute market")
    print("=" * 60)

    # Load config
    config = load_config()

    print(f"\nDry run mode: {config.gabagool.dry_run}")
    if config.gabagool.dry_run:
        print("WARNING: Dry run is enabled - no real trade will execute!")
        print("Set GABAGOOL_DRY_RUN=false to execute real trades")

    # Initialize clients
    print("\n[1/4] Initializing Polymarket client...")
    poly_client = PolymarketClient(config.polymarket)  # Pass polymarket config, not full config
    poly_client.connect()  # Sync connect (not async)

    # Check balance
    balance_info = poly_client.get_balance()
    balance = balance_info.get("balance", 0)
    print(f"       Wallet balance: ${balance:.2f}")

    if balance < 1.0:
        print("ERROR: Insufficient balance for $1 trade")
        return

    # Find active market
    print("\n[2/4] Finding active 15-minute market...")
    gamma_client = GammaClient(config)

    # Get BTC markets
    markets = await gamma_client.find_15min_markets("BTC")

    if not markets:
        print("       No active BTC markets, trying ETH...")
        markets = await gamma_client.find_15min_markets("ETH")

    if not markets:
        print("ERROR: No active 15-minute markets found")
        return

    # Pick the first tradeable market
    market = None
    for m in markets:
        if m.is_tradeable and m.seconds_remaining > 120:  # At least 2 min left
            market = m
            break

    if not market:
        print("ERROR: No tradeable market with enough time remaining")
        return

    print(f"       Found: {market.asset} - {market.question[:50]}...")
    print(f"       Time remaining: {market.seconds_remaining:.0f} seconds")
    print(f"       UP token: {market.yes_token_id[:20]}...")
    print(f"       DOWN token: {market.no_token_id[:20]}...")

    # Get current prices
    print("\n[3/4] Getting current prices...")
    up_price = market.up_price or 0.50
    down_price = market.down_price or 0.50
    print(f"       UP price: ${up_price:.3f}")
    print(f"       DOWN price: ${down_price:.3f}")

    # Execute trade - buy $1 of UP (YES)
    trade_amount = 1.00
    token_id = market.yes_token_id
    side = "BUY"

    print(f"\n[4/4] Executing trade...")
    print(f"       Action: BUY ${trade_amount:.2f} of UP")
    print(f"       Token ID: {token_id[:30]}...")

    if config.gabagool.dry_run:
        print("\n       [DRY RUN] Would execute trade but dry_run=true")
        expected_shares = trade_amount / up_price
        print(f"       Expected shares: {expected_shares:.4f}")
        return

    # Execute real trade
    try:
        result = await poly_client.execute_single_order(
            token_id=token_id,
            side=side,
            amount_usd=trade_amount,
            timeout_seconds=30,
        )

        print("\n" + "=" * 60)
        print("TRADE RESULT:")
        print("=" * 60)

        if result.get("success"):
            print("✅ TRADE SUCCESSFUL!")
            order = result.get("order", {})
            print(f"   Order response: {order}")

            # Check new balance
            new_balance_info = poly_client.get_balance()
            new_balance = new_balance_info.get("balance", 0)
            print(f"\n   Previous balance: ${balance:.2f}")
            print(f"   New balance: ${new_balance:.2f}")
            print(f"   Spent: ${balance - new_balance:.2f}")
        else:
            print("❌ TRADE FAILED!")
            print(f"   Error: {result.get('error', 'Unknown error')}")
            print(f"   Full result: {result}")

    except Exception as e:
        print(f"\n❌ EXCEPTION: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
