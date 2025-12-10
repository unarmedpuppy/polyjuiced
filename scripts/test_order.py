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
    print(f'\nTest market: {market.slug}')
    print(f'YES token: {market.yes_token_id[:20]}...')
    print(f'NO token: {market.no_token_id[:20]}...')
    print(f'End time: {market.end_time}')

    # Get current prices
    try:
        yes_price = client.get_price(market.yes_token_id, 'buy')
        no_price = client.get_price(market.no_token_id, 'buy')
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
            token_id=market.yes_token_id,
            amount_usd=test_amount,
            side='BUY'
        )
        print(f'Order result: {result}')
        print('\nSUCCESS: Order API is working!')
    except Exception as e:
        print(f'Order FAILED: {e}')
        print('\nThis error tells us what is wrong with order execution')


if __name__ == '__main__':
    asyncio.run(test_order())
