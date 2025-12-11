#!/usr/bin/env python3
"""Test script to execute a real $1 trade on an active 15-minute market.

This script:
1. Finds an active 15-minute BTC or ETH market
2. Buys $1 worth of the UP (YES) side
3. Reports the result

Run with: python3 scripts/test_real_trade.py
"""

import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, BalanceAllowanceParams
import httpx

def main():
    print("=" * 60)
    print("TEST REAL TRADE - $1 on active 15-minute market")
    print("=" * 60)

    # Get env vars directly
    dry_run = os.getenv("GABAGOOL_DRY_RUN", "true").lower() == "true"
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    api_key = os.getenv("POLYMARKET_API_KEY", "")
    api_secret = os.getenv("POLYMARKET_API_SECRET", "")
    api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE", "")
    proxy_wallet = os.getenv("POLYMARKET_PROXY_WALLET", "")
    clob_url = os.getenv("POLYMARKET_CLOB_HTTP_URL", "https://clob.polymarket.com")
    signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))

    print(f"\nDry run mode: {dry_run}")
    if dry_run:
        print("WARNING: Dry run is enabled - no real trade will execute!")
        print("Set GABAGOOL_DRY_RUN=false to execute real trades")

    if not private_key:
        print("ERROR: POLYMARKET_PRIVATE_KEY not set")
        return

    # Initialize CLOB client directly
    print("\n[1/4] Connecting to Polymarket CLOB...")
    print(f"       Signature type: {signature_type}")
    client = ClobClient(
        host=clob_url,
        key=private_key,
        chain_id=137,  # Polygon Mainnet
        signature_type=signature_type,
        funder=proxy_wallet or None,
    )

    # Set API credentials
    if api_key:
        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )
        client.set_api_creds(creds)

    # Test connection
    try:
        client.get_ok()
        print("       Connected!")
    except Exception as e:
        print(f"ERROR: Failed to connect: {e}")
        return

    # Check balance
    print("\n[2/4] Checking balance...")
    try:
        params = BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=signature_type)
        balance_info = client.get_balance_allowance(params)
        balance = float(balance_info.get("balance", 0)) / 1e6  # Convert from USDC decimals
        print(f"       Balance: ${balance:.2f}")

        if balance < 1.0:
            print("ERROR: Insufficient balance for $1 trade")
            return
    except Exception as e:
        print(f"ERROR: Failed to get balance: {e}")
        return

    # Find active market via Gamma API
    print("\n[3/4] Finding active 15-minute market...")

    # Get current time slot
    import time
    slot_duration = 900  # 15 minutes
    current_slot = (int(time.time()) // slot_duration) * slot_duration
    next_slot = current_slot + slot_duration

    # Try to find BTC market
    proxy = os.getenv("HTTP_PROXY", None)
    client_kwargs = {"proxy": proxy} if proxy else {}

    market_slug = f"btc-updown-15m-{next_slot}"
    gamma_url = f"https://gamma-api.polymarket.com/markets/slug/{market_slug}"

    try:
        with httpx.Client(**client_kwargs) as http_client:
            response = http_client.get(gamma_url, timeout=10)
            if response.status_code == 200:
                market = response.json()
            else:
                print(f"       BTC market not found, trying ETH...")
                market_slug = f"eth-updown-15m-{next_slot}"
                gamma_url = f"https://gamma-api.polymarket.com/markets/slug/{market_slug}"
                response = http_client.get(gamma_url, timeout=10)
                if response.status_code == 200:
                    market = response.json()
                else:
                    print(f"ERROR: No active market found")
                    return
    except Exception as e:
        print(f"ERROR: Failed to find market: {e}")
        return

    # Extract token IDs
    # Tokens are in clobTokenIds as JSON string: '["token1", "token2"]'
    # Outcomes are in outcomes as JSON string: '["Up", "Down"]'
    # Prices are in outcomePrices as JSON string: '["0.495", "0.505"]'
    import json

    clob_token_ids = json.loads(market.get("clobTokenIds", "[]"))
    outcomes = json.loads(market.get("outcomes", "[]"))
    outcome_prices = json.loads(market.get("outcomePrices", "[]"))

    print(f"       Outcomes: {outcomes}")
    print(f"       Token IDs: {[t[:30] + '...' for t in clob_token_ids]}")
    print(f"       Prices: {outcome_prices}")

    # Find Up token (index 0 if outcomes[0] == "Up")
    token_id = None
    token_price = 0.50
    for i, outcome in enumerate(outcomes):
        if outcome.lower() == "up":
            token_id = clob_token_ids[i]
            token_price = float(outcome_prices[i]) if i < len(outcome_prices) else 0.50
            break

    if not token_id:
        print("ERROR: Could not find UP token")
        return

    print(f"       Found: {market.get('question', 'Unknown')[:60]}...")
    print(f"       UP token: {token_id[:30]}...")
    print(f"       UP price: ${token_price:.3f}")

    # Execute trade - use $5 which is Polymarket's minimum order size
    trade_amount = 5.00

    print(f"\n[4/4] Executing trade...")
    print(f"       Action: BUY ${trade_amount:.2f} of UP")

    if dry_run:
        print("\n       [DRY RUN] Would execute trade but dry_run=true")
        expected_shares = trade_amount / token_price
        print(f"       Expected shares: {expected_shares:.4f}")
        return

    # Execute real trade
    try:
        # Round to 2 decimals for taker amount (Polymarket requirement)
        rounded_amount = round(trade_amount, 2)
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=rounded_amount,
            side="BUY",
        )

        # create_market_order just SIGNS the order, we need to POST it too
        print("       Creating and signing order...")
        signed_order = client.create_market_order(order_args)
        print(f"       Signed order created: {type(signed_order)}")

        # Debug: print the order details
        order = signed_order.order
        print(f"       Order maker_amount: {order.makerAmount}")
        print(f"       Order taker_amount: {order.takerAmount}")

        # Now POST the signed order to execute it
        print("       Posting order to exchange...")
        result = client.post_order(signed_order)
        print(f"       Post result: {result}")

        print("\n" + "=" * 60)
        print("TRADE RESULT:")
        print("=" * 60)
        print(f"✅ Order posted: {result}")

        # Check new balance
        new_balance_info = client.get_balance_allowance(params)
        new_balance = float(new_balance_info.get("balance", 0)) / 1e6
        print(f"\n   Previous balance: ${balance:.2f}")
        print(f"   New balance: ${new_balance:.2f}")
        print(f"   Spent: ${balance - new_balance:.2f}")

    except Exception as e:
        print(f"\n❌ TRADE FAILED: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
