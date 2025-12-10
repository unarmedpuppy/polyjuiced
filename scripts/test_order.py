#!/usr/bin/env python3
"""Test script to validate order execution on Polymarket."""

import asyncio
import sys
sys.path.insert(0, '/app')

from src.config import load_config
from src.client.polymarket import PolymarketClient


async def test_order():
    """Test order placement."""
    # Load config
    config = load_config()

    # Create client
    client = PolymarketClient(config.polymarket)

    # Connect
    connected = await client.connect()
    if not connected:
        print('ERROR: Failed to connect to Polymarket')
        return

    print('Connected to Polymarket CLOB')

    # Get balance
    balance = client.get_balance()
    print(f'Wallet balance: ${balance["balance"]:.2f}')
    print(f'Allowance: ${balance["allowance"]:.2f}')

    # Check we have enough balance
    if balance['balance'] < 1.0:
        print('WARNING: Balance too low for test trade')
        return

    # Get a current BTC market to test with
    from src.client.gamma import GammaClient
    gamma = GammaClient(config.polymarket.gamma_api_url)

    # Find an active market
    markets = await gamma.find_15min_markets('BTC')
    if not markets:
        print('No active BTC markets found')
        return

    market = markets[0]
    # find_15min_markets returns dicts
    slug = market.get('slug', 'unknown')
    yes_token = market.get('yes_token_id') or market.get('up_token_id')
    no_token = market.get('no_token_id') or market.get('down_token_id')

    print(f'\nTest market: {slug}')
    print(f'YES token: {yes_token[:20] if yes_token else "None"}...')
    print(f'NO token: {no_token[:20] if no_token else "None"}...')
    print(f'End time: {market.get("end_time")}')

    if not yes_token or not no_token:
        print('ERROR: Could not get token IDs')
        print(f'Market data: {market}')
        return

    # Get current prices
    try:
        yes_price = client.get_price(yes_token, 'buy')
        no_price = client.get_price(no_token, 'buy')
        print(f'\nCurrent prices:')
        print(f'  YES: ${yes_price:.4f}')
        print(f'  NO: ${no_price:.4f}')
        print(f'  Sum: ${yes_price + no_price:.4f}')
        print(f'  Spread: {(1.0 - yes_price - no_price) * 100:.2f} cents')
    except Exception as e:
        print(f'Error getting prices: {e}')
        return

    # Test placing a VERY small order ($0.10) - just to verify API works
    test_amount = 0.10
    print(f'\n=== TEST ORDER ===')
    print(f'Placing test BUY order: ${test_amount:.2f} on YES token')

    try:
        result = client.create_market_order(
            token_id=yes_token,
            amount_usd=test_amount,
            side='BUY'
        )
        print(f'Order result: {result}')
        print('\nSUCCESS: Order API is working!')
    except Exception as e:
        print(f'Order FAILED: {e}')
        import traceback
        traceback.print_exc()
        print('\nThis error tells us what is wrong with order execution')


if __name__ == '__main__':
    asyncio.run(test_order())
